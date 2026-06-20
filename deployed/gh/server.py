#!/usr/bin/env python3
"""MCP server para GitHub CLI (gh): despliegues, Actions, PRs, releases.

Multi-account sin deriva: cada instancia recibe su propio GH_TOKEN (vía env o
--token). Con GH_TOKEN seteado, gh es *stateless* — no lee ni escribe
~/.config/gh/hosts.yml — así que la "cuenta activa" no puede derivar entre
instancias (a diferencia de gcloud, que necesita CLOUDSDK_CONFIG para aislar
el estado en disco). El equivalente --config-dir (GH_CONFIG_DIR) queda solo
como defensa en profundidad para subcomandos que toquen config local.

Escritura gateada (patrón --allow-flush de redis): merge/release/delete/run-de-
workflow y demás mutaciones están BLOQUEADAS salvo que la instancia se registre
con --allow-write.
"""

import argparse
import os
import subprocess
import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

parser = argparse.ArgumentParser()
parser.add_argument("--owner", required=True, help="Org/usuario por defecto, ej: lait")
parser.add_argument("--token", default=None, help="PAT; si se omite se usa GH_TOKEN del entorno")
parser.add_argument("--name", default=None, help="Label de la instancia (gh-lait)")
parser.add_argument("--config-dir", default=None, help="GH_CONFIG_DIR aislado (defensa en profundidad)")
# Por defecto las mutaciones (merge, release, delete, workflow run...) están
# bloqueadas. Registrar la instancia con --allow-write para habilitarlas.
parser.add_argument("--allow-write", action="store_true")
args, _ = parser.parse_known_args()

OWNER = args.owner
ALLOW_WRITE = args.allow_write
SERVER_LABEL = args.name or OWNER

# Env de los subprocess gh:
# - GH_TOKEN: autenticación por instancia → gh stateless, sin deriva de cuenta.
# - GH_PROMPT_DISABLED: nunca pedir confirmaciones interactivas (colgarían el server).
# - GH_PAGER=cat: sin pager (cat escupe todo y termina; un pager esperaría un tty).
# - GH_NO_UPDATE_NOTIFIER / NO_COLOR: salida limpia y determinista.
_ENV = {
    **os.environ,
    "GH_PROMPT_DISABLED": "1",
    "GH_PAGER": "cat",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "NO_COLOR": "1",
    "CLICOLOR": "0",
}
if args.token:
    _ENV["GH_TOKEN"] = args.token
if args.config_dir:
    _ENV["GH_CONFIG_DIR"] = os.path.expanduser(args.config_dir)

app = Server(f"gh-{SERVER_LABEL}")


# Subcomandos (cmd, sub) que mutan estado: gateados por --allow-write.
WRITE_VERBS = {
    ("pr", "merge"), ("pr", "close"), ("pr", "edit"), ("pr", "comment"),
    ("pr", "review"), ("pr", "reopen"), ("pr", "ready"), ("pr", "create"),
    ("release", "create"), ("release", "delete"), ("release", "edit"), ("release", "upload"),
    ("repo", "delete"), ("repo", "archive"), ("repo", "edit"), ("repo", "create"),
    ("repo", "rename"), ("repo", "fork"),
    ("workflow", "run"), ("workflow", "enable"), ("workflow", "disable"),
    ("run", "cancel"), ("run", "rerun"), ("run", "delete"),
    ("secret", "set"), ("secret", "delete"),
    ("variable", "set"), ("variable", "delete"),
    ("issue", "create"), ("issue", "edit"), ("issue", "close"),
    ("issue", "comment"), ("issue", "reopen"), ("issue", "delete"),
    ("label", "create"), ("label", "delete"), ("label", "edit"),
    ("gist", "create"), ("gist", "delete"), ("gist", "edit"),
    ("cache", "delete"),
}

TIMEOUT_DEFAULT = 30
TIMEOUT_MAX = 300


def _clamp_timeout(value) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return TIMEOUT_DEFAULT
    return max(1, min(value, TIMEOUT_MAX))


def _verb_pair(args_list: list) -> tuple:
    """(comando, subcomando) — los dos primeros tokens que no son flags."""
    toks = [a for a in args_list if not a.startswith("-")]
    cmd = toks[0].lower() if len(toks) > 0 else ""
    sub = toks[1].lower() if len(toks) > 1 else ""
    return cmd, sub


def _is_write(args_list: list) -> bool:
    """Heurística de mutación: verbo conocido de escritura, o `api` con método ≠ GET."""
    cmd, sub = _verb_pair(args_list)
    if (cmd, sub) in WRITE_VERBS:
        return True
    if cmd == "api":
        method = "GET"
        for i, a in enumerate(args_list):
            low = a.lower()
            if low in ("-x", "--method") and i + 1 < len(args_list):
                method = args_list[i + 1].upper()
            elif low.startswith("--method="):
                method = a.split("=", 1)[1].upper()
            # -f/-F/--field/--raw-field implican POST por defecto en gh api
            elif low in ("-f", "-F", "--field", "--raw-field"):
                method = "POST" if method == "GET" else method
        return method != "GET"
    return False


def _blocked(verb: str) -> str:
    return (
        f"Bloqueado: '{verb}' es una operación de escritura deshabilitada en {SERVER_LABEL}. "
        f"Re-registra la instancia con allow_write:true en secrets.json (flag --allow-write) para permitirlo."
    )


def _run(cmd: list, timeout: int = TIMEOUT_DEFAULT) -> str:
    # stdin=DEVNULL: nunca heredar el canal JSON-RPC del protocolo stdio. Si gh
    # hereda el stdin del proceso MCP puede colgarse esperando un prompt o robar
    # bytes del protocolo — ambos casos producen timeouts en la app.
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, stdin=subprocess.DEVNULL, env=_ENV,
        )
    except subprocess.TimeoutExpired:
        return f"[timeout] El comando tardó más de {timeout}s y fue cancelado."
    except FileNotFoundError:
        return "[error] 'gh' no está instalado o no está en PATH. Instala GitHub CLI (https://cli.github.com)."
    out = result.stdout.strip()
    err = result.stderr.strip()
    if result.returncode != 0:
        return f"[exit {result.returncode}]\n{err or out}"
    return out or err or "(sin output)"


def _repo_flag(arguments: dict) -> list:
    """-R owner/repo si se pasa `repo`; si no, gh intenta el repo del cwd (suele fallar)."""
    repo = arguments.get("repo")
    if not repo:
        return []
    if "/" not in repo:
        repo = f"{OWNER}/{repo}"
    return ["-R", repo]


def _repo_name(arguments: dict) -> str | None:
    """owner/repo normalizado (para subcomandos que lo toman posicional, ej. `gh repo view`)."""
    repo = arguments.get("repo")
    if not repo:
        return None
    return repo if "/" in repo else f"{OWNER}/{repo}"


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    write_state = "habilitada (allow_write)" if ALLOW_WRITE else "BLOQUEADA (registra con allow_write:true para habilitar)"
    repo_prop = {
        "repo": {
            "type": "string",
            "description": f"owner/repo (o solo repo, asume owner={OWNER}). Pásalo siempre: sin él gh intenta inferirlo del directorio actual y suele fallar.",
        }
    }
    return [
        # ---------- Lectura ----------
        types.Tool(
            name="gh",
            description=(
                f"Ejecuta un comando gh CLI arbitrario en {SERVER_LABEL}. "
                f"Lectura libre; las mutaciones están {write_state}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": 'Ej: ["run", "list", "--limit", "10"] o ["pr", "view", "42"]',
                    },
                    "timeout": {
                        "type": "integer",
                        "description": f"Timeout en segundos (default {TIMEOUT_DEFAULT}, máx {TIMEOUT_MAX}).",
                    },
                },
                "required": ["args"],
            },
        ),
        types.Tool(
            name="api",
            description=(
                f"Llama a la API REST de GitHub vía `gh api` en {SERVER_LABEL}. "
                f"Usa el endpoint con owner/repo explícito (ej: repos/lait/ui-kit/deployments). "
                f"GET libre; métodos de escritura {write_state}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "endpoint": {"type": "string", "description": "Ej: repos/lait/ui-kit/deployments"},
                    "method": {"type": "string", "default": "GET", "description": "GET, POST, PATCH, PUT, DELETE"},
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": 'Pares k=v para -f, ej: ["state=success"]',
                    },
                },
                "required": ["endpoint"],
            },
        ),
        types.Tool(
            name="run_list",
            description="Lista runs de GitHub Actions (despliegues/CI).",
            inputSchema={
                "type": "object",
                "properties": {
                    **repo_prop,
                    "limit": {"type": "integer", "default": 20},
                    "workflow": {"type": "string", "description": "Nombre o archivo del workflow (opcional)"},
                    "status": {"type": "string", "description": "completed, in_progress, failure, success... (opcional)"},
                },
            },
        ),
        types.Tool(
            name="run_view",
            description="Detalle de un run de Actions; con log=true incluye el log.",
            inputSchema={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    **repo_prop,
                    "log": {"type": "boolean", "default": False, "description": "Incluir log completo"},
                    "log_failed": {"type": "boolean", "default": False, "description": "Solo el log de los pasos fallidos"},
                },
                "required": ["run_id"],
            },
        ),
        types.Tool(
            name="workflow_list",
            description="Lista los workflows de Actions del repo.",
            inputSchema={"type": "object", "properties": {**repo_prop}},
        ),
        types.Tool(
            name="pr_list",
            description="Lista pull requests.",
            inputSchema={
                "type": "object",
                "properties": {
                    **repo_prop,
                    "state": {"type": "string", "default": "open", "description": "open, closed, merged, all"},
                    "limit": {"type": "integer", "default": 20},
                },
            },
        ),
        types.Tool(
            name="pr_view",
            description="Detalle de un pull request.",
            inputSchema={
                "type": "object",
                "properties": {"number": {"type": "string"}, **repo_prop},
                "required": ["number"],
            },
        ),
        types.Tool(
            name="pr_checks",
            description="Estado de los checks/CI de un pull request.",
            inputSchema={
                "type": "object",
                "properties": {"number": {"type": "string"}, **repo_prop},
                "required": ["number"],
            },
        ),
        types.Tool(
            name="deployment_list",
            description="Lista deployments del repo (vía API REST).",
            inputSchema={
                "type": "object",
                "properties": {
                    **repo_prop,
                    "environment": {"type": "string", "description": "Filtrar por environment (opcional)"},
                },
            },
        ),
        types.Tool(
            name="release_list",
            description="Lista releases del repo.",
            inputSchema={
                "type": "object",
                "properties": {**repo_prop, "limit": {"type": "integer", "default": 20}},
            },
        ),
        types.Tool(
            name="repo_view",
            description="Información general del repo.",
            inputSchema={"type": "object", "properties": {**repo_prop}},
        ),
        # ---------- Escritura (gateada) ----------
        types.Tool(
            name="pr_merge",
            description=f"[escritura {write_state}] Mergea un PR.",
            inputSchema={
                "type": "object",
                "properties": {
                    "number": {"type": "string"},
                    **repo_prop,
                    "method": {"type": "string", "default": "merge", "description": "merge, squash, rebase"},
                },
                "required": ["number"],
            },
        ),
        types.Tool(
            name="pr_comment",
            description=f"[escritura {write_state}] Comenta en un PR.",
            inputSchema={
                "type": "object",
                "properties": {"number": {"type": "string"}, "body": {"type": "string"}, **repo_prop},
                "required": ["number", "body"],
            },
        ),
        types.Tool(
            name="release_create",
            description=f"[escritura {write_state}] Crea un release.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag": {"type": "string"},
                    **repo_prop,
                    "title": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["tag"],
            },
        ),
        types.Tool(
            name="workflow_run",
            description=f"[escritura {write_state}] Dispara un workflow de Actions (workflow_dispatch).",
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow": {"type": "string", "description": "Nombre o archivo del workflow"},
                    **repo_prop,
                    "ref": {"type": "string", "description": "Branch/tag (opcional)"},
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": 'Inputs k=v para -f (opcional)',
                    },
                },
                "required": ["workflow"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    repo = _repo_flag(arguments)

    # ---------- Lectura ----------
    if name == "gh":
        a = arguments["args"]
        if _is_write(a) and not ALLOW_WRITE:
            output = _blocked(" ".join(_verb_pair(a)).strip())
        else:
            output = _run(["gh"] + a, timeout=_clamp_timeout(arguments.get("timeout", TIMEOUT_DEFAULT)))

    elif name == "api":
        endpoint = arguments["endpoint"]
        method = arguments.get("method", "GET").upper()
        fields = arguments.get("fields", [])
        if method != "GET" and not ALLOW_WRITE:
            output = _blocked(f"api {method}")
        else:
            cmd = ["gh", "api", endpoint]
            if method != "GET":
                cmd += ["--method", method]
            for f in fields:
                cmd += ["-f", f]
            output = _run(cmd, timeout=_clamp_timeout(arguments.get("timeout", TIMEOUT_DEFAULT)))

    elif name == "run_list":
        cmd = ["gh", "run", "list", "--limit", str(arguments.get("limit", 20))] + repo
        if arguments.get("workflow"):
            cmd += ["--workflow", arguments["workflow"]]
        if arguments.get("status"):
            cmd += ["--status", arguments["status"]]
        output = _run(cmd)

    elif name == "run_view":
        cmd = ["gh", "run", "view", str(arguments["run_id"])] + repo
        if arguments.get("log"):
            cmd.append("--log")
        if arguments.get("log_failed"):
            cmd.append("--log-failed")
        output = _run(cmd, timeout=120)

    elif name == "workflow_list":
        output = _run(["gh", "workflow", "list"] + repo)

    elif name == "pr_list":
        cmd = ["gh", "pr", "list", "--state", arguments.get("state", "open"),
               "--limit", str(arguments.get("limit", 20))] + repo
        output = _run(cmd)

    elif name == "pr_view":
        output = _run(["gh", "pr", "view", str(arguments["number"])] + repo)

    elif name == "pr_checks":
        output = _run(["gh", "pr", "checks", str(arguments["number"])] + repo)

    elif name == "deployment_list":
        # Con `repo` se construye el path explícito; sin él se deja el placeholder
        # {owner}/{repo}, que gh solo resuelve si el cwd resulta ser un repo git.
        rs = arguments.get("repo")
        path = f"repos/{rs if '/' in rs else OWNER + '/' + rs}/deployments" if rs else "repos/{owner}/{repo}/deployments"
        if arguments.get("environment"):
            path += f"?environment={arguments['environment']}"
        output = _run(["gh", "api", path])

    elif name == "release_list":
        output = _run(["gh", "release", "list", "--limit", str(arguments.get("limit", 20))] + repo)

    elif name == "repo_view":
        # `gh repo view` toma el repo como argumento posicional, no con -R.
        rn = _repo_name(arguments)
        output = _run(["gh", "repo", "view"] + ([rn] if rn else []))

    # ---------- Escritura (gateada) ----------
    elif name == "pr_merge":
        if not ALLOW_WRITE:
            output = _blocked("pr merge")
        else:
            method = arguments.get("method", "merge")
            flag = {"squash": "--squash", "rebase": "--rebase"}.get(method, "--merge")
            output = _run(["gh", "pr", "merge", str(arguments["number"]), flag] + repo)

    elif name == "pr_comment":
        if not ALLOW_WRITE:
            output = _blocked("pr comment")
        else:
            output = _run(["gh", "pr", "comment", str(arguments["number"]),
                           "--body", arguments["body"]] + repo)

    elif name == "release_create":
        if not ALLOW_WRITE:
            output = _blocked("release create")
        else:
            cmd = ["gh", "release", "create", arguments["tag"]] + repo
            if arguments.get("title"):
                cmd += ["--title", arguments["title"]]
            if arguments.get("notes"):
                cmd += ["--notes", arguments["notes"]]
            output = _run(cmd)

    elif name == "workflow_run":
        if not ALLOW_WRITE:
            output = _blocked("workflow run")
        else:
            cmd = ["gh", "workflow", "run", arguments["workflow"]] + repo
            if arguments.get("ref"):
                cmd += ["--ref", arguments["ref"]]
            for f in arguments.get("fields", []):
                cmd += ["-f", f]
            output = _run(cmd)

    else:
        output = f"Tool desconocido: {name}"

    return [types.TextContent(type="text", text=output)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
