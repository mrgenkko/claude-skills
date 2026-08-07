#!/usr/bin/env python3
"""
Registra MCPs en un proyecto de Claude Code (VSCode extension).

Uso:
    python3 "~/Skills/scripts/add-mcp-to-project.py" /ruta/al/proyecto [--update] [--only name1,name2,...]

Por qué es necesario:
    La VSCode extension de Claude Code lee los MCPs por proyecto desde
    ~/.claude.json → projects["/ruta/proyecto"]["mcpServers"].
    Los archivos ~/.claude/mcp.json y ~/.claude/settings.json son ignorados
    por la extensión de VSCode. Hay que registrar los servidores directamente
    en ~/.claude.json para cada proyecto.

Configuración:
    Todos los MCPs y sus credenciales se definen en scripts/secrets.json.
    Copiar scripts/secrets.example.json → scripts/secrets.json y completar.

Portabilidad:
    Corre en Linux/macOS y en Windows. Las diferencias de plataforma están
    aisladas en venv_python() (layout del venv) y en el branch de webprobe
    (GIO_MODULE_DIR es POSIX-only). Tests: scripts/test_add_mcp_to_project.py
"""

import json
import os
import stat
import sys
import tempfile

HOME = os.path.expanduser("~")
CLAUDE_JSON = os.path.join(HOME, ".claude.json")
SKILLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRETS_FILE = os.path.join(SKILLS_DIR, "scripts", "secrets.json")
MCP_SERVERS_DIR = os.path.join(HOME, ".claude", "mcp-servers")
# Módulos GIO del sistema — antídoto al GIO_MODULE_DIR que inyecta el snap de VSCode.
GIO_MODULE_DIR_SYS = "/usr/lib/x86_64-linux-gnu/gio/modules"


def venv_python() -> str:
    """Ruta al intérprete del venv del repo.

    En Windows el venv no tiene bin/: `python -m venv` crea Scripts/python.exe.
    La ruta POSIX directamente no existe ahí, así que hay que elegir por plataforma.
    """
    if os.name == "nt":
        return os.path.join(SKILLS_DIR, ".venv", "Scripts", "python.exe")
    return os.path.join(SKILLS_DIR, ".venv", "bin", "python")


def server_path(*parts: str) -> str:
    """Ruta a un server.py bajo ~/.claude/mcp-servers, con el separador nativo."""
    return os.path.join(MCP_SERVERS_DIR, *parts)


def read_json(path: str) -> dict:
    """Lee JSON forzando UTF-8.

    Sin encoding explícito Windows abre con la codepage ANSI (cp1252) y
    ~/.claude.json revienta con UnicodeDecodeError apenas una ruta de proyecto
    o un valor traiga acentos o emoji.
    """
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json_atomic(path: str, data: dict) -> None:
    """Escribe JSON vía temporal + os.replace.

    ~/.claude.json es la config de TODOS los proyectos. Un json.dump() directo
    trunca el archivo antes de escribir: si falla a mitad (disco lleno, valor no
    serializable, Ctrl-C) la config queda destruida. os.replace es atómico tanto
    en POSIX como en Windows.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".add-mcp-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp crea 0600; conservar los permisos del archivo original.
        if os.path.exists(path):
            os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def load_secrets() -> dict:
    if not os.path.exists(SECRETS_FILE):
        print(f"ERROR: No se encontró {SECRETS_FILE}")
        print("Copiar scripts/secrets.example.json → scripts/secrets.json y completar.")
        sys.exit(1)
    return read_json(SECRETS_FILE)


def warn_if_no_venv() -> None:
    """Avisa si el intérprete que se va a registrar no existe.

    No es fatal (se puede registrar antes de crear el venv), pero un MCP que
    apunta a un binario inexistente falla al arrancar sin decir por qué.
    """
    python_bin = venv_python()
    if os.path.exists(python_bin):
        return
    venv_dir = os.path.join(SKILLS_DIR, ".venv")
    reqs = os.path.join(SKILLS_DIR, "requirements.txt")
    creator = "py -3 -m venv" if os.name == "nt" else "python3 -m venv"
    print(f"WARN: no existe el intérprete del venv en {python_bin}")
    print("      Los MCPs se registran igual, pero no van a arrancar. Crearlo con:")
    print(f'        {creator} "{venv_dir}"')
    print(f'        "{python_bin}" -m pip install -r "{reqs}"')
    print()


def build_mcp_servers(servers_config: list) -> dict:
    result = {}
    for entry in servers_config:
        name = entry["name"]
        kind = entry["type"]

        # NO heredar os.environ acá. El cliente MCP ya hace el merge al spawnear:
        #     env: {...whitelist_heredada, ...env_declarado}
        # donde la whitelist es HOME/LOGNAME/PATH/SHELL/TERM/USER en POSIX y
        # APPDATA/HOMEDRIVE/HOMEPATH/LOCALAPPDATA/PATH/PROCESSOR_ARCHITECTURE/
        # SYSTEMDRIVE/SYSTEMROOT/TEMP/USERNAME/USERPROFILE/PROGRAMFILES en Windows
        # (verificado en Claude Code 2.1.223). O sea: SystemRoot y PATH ya llegan
        # solos —no hay que reenviarlos— y lo que se declare acá GANA sobre lo
        # heredado. Volcar os.environ haría tres daños: (1) persistiría el entorno
        # entero, secretos incluidos, en texto plano en ~/.claude.json; (2) pisaría
        # el GIO_MODULE_DIR del sistema con el del snap de VSCode, reviviendo el
        # crash de webkit; (3) congelaría un snapshot del entorno del momento del
        # registro en vez de heredar el vivo. Este dict es solo para overrides.
        env = {}

        if kind == "gcloud":
            args = [
                server_path("gcloud", "server.py"),
                f"--project={entry['project']}",
                f"--region={entry['region']}",
                f"--workdir={entry['workdir']}",
                f"--account={entry['account']}",
            ]
            if entry.get("key_file"):
                args.append(f"--key-file={entry['key_file']}")
            # Aislamiento multi-cuenta: cada MCP gcloud usa su propio ~/.config/gcloud
            # para que la cuenta activa no derive entre instancias (ej. dev vs prod).
            if entry.get("config_dir"):
                env["CLOUDSDK_CONFIG"] = os.path.expanduser(entry["config_dir"])

        elif kind == "postgres":
            args = [
                server_path("postgres", "server.py"),
                f"--host={entry['host']}",
                f"--port={entry['port']}",
                f"--db={entry['db']}",
                f"--user={entry['user']}",
                f"--password={entry['password']}",
            ]

        elif kind == "ssh":
            server_label = name.removeprefix("ssh-") or entry["host"]
            args = [
                server_path("ssh", "server.py"),
                f"--host={entry['host']}",
                f"--port={entry.get('port', 22)}",
                f"--user={entry['user']}",
                f"--name={server_label}",
            ]
            if entry.get("key_file"):
                args.append(f"--key-file={entry['key_file']}")
            elif entry.get("password"):
                args.append(f"--password={entry['password']}")
            if entry.get("sudo_password"):
                args.append(f"--sudo-password={entry['sudo_password']}")
            # Sesiones persistentes (tmux server-side): ON por defecto; opt-out por instancia.
            if entry.get("allow_sessions", True) is False:
                args.append("--forbid-sessions")
            # Watcher: apaga sesiones idle tras N segundos de inactividad (default server: 1800; 0=off).
            if entry.get("session_idle_timeout") is not None:
                args.append(f"--session-idle-timeout={entry['session_idle_timeout']}")

        elif kind == "redis":
            server_label = name.removeprefix("redis-") or entry["host"]
            args = [
                server_path("redis", "server.py"),
                f"--host={entry['host']}",
                f"--port={entry.get('port', 6379)}",
                f"--db={entry.get('db', 0)}",
                f"--name={server_label}",
            ]
            if entry.get("password"):
                args.append(f"--password={entry['password']}")
            # FLUSHALL/FLUSHDB bloqueados por defecto; opt-in explícito por instancia.
            if entry.get("allow_flush"):
                args.append("--allow-flush")

        elif kind == "gh":
            server_label = name.removeprefix("gh-") or entry["owner"]
            args = [
                server_path("gh", "server.py"),
                f"--owner={entry['owner']}",
                f"--name={server_label}",
            ]
            # Multi-cuenta sin deriva: GH_TOKEN por instancia hace a gh stateless
            # (no toca ~/.config/gh/hosts.yml). config_dir es defensa en profundidad.
            env["GH_TOKEN"] = entry["token"]
            if entry.get("config_dir"):
                env["GH_CONFIG_DIR"] = os.path.expanduser(entry["config_dir"])
            # Mutaciones (merge, release, delete, workflow run...) bloqueadas por
            # defecto; opt-in explícito por instancia.
            if entry.get("allow_write"):
                args.append("--allow-write")

        elif kind == "obsidian":
            args = [
                server_path("obsidian", "server.py"),
                f"--vault-path={entry['vault_path']}",
            ]

        elif kind == "focusyn":
            # MCP remoto Streamable HTTP del gateway (/mcp, in-process). Reemplaza al
            # wrapper stdio legacy. Auth por header X-Agent-Key (key del agente de máquina).
            # El registro canónico de focusyn es user-scope GLOBAL (mcpServers top-level
            # de ~/.claude.json, disponible en todos los proyectos); este branch sirve
            # para overrides per-proyecto (local scope, que prevalece sobre el global).
            result[name] = {
                "type": "http",
                "url": entry["url"],
                "headers": {"X-Agent-Key": entry["agent_key"]},
            }
            if entry.get("timeout_ms"):
                result[name]["timeout"] = entry["timeout_ms"]
            continue

        elif kind == "webprobe":
            args = [
                server_path("webprobe", "server.py"),
                f"--browser={entry.get('browser', 'chromium')}",
                f"--name={entry['name']}",
            ]
            if entry.get("base_url"):
                args.append(f"--base-url={entry['base_url']}")
            if entry.get("device"):
                args.append(f"--device={entry['device']}")
            # capacidad (CPU/red/cores/RAM) y backend gráfico por defecto de la instancia.
            # Lo normal es no fijarlos y que el agente los pase por tab / con set_mode.
            if entry.get("device_class"):
                args.append(f"--device-class={entry['device_class']}")
            if entry.get("gpu_tier"):
                args.append(f"--gpu-tier={entry['gpu_tier']}")
            for k, flag in (("viewport_w", "--viewport-w"),
                            ("viewport_h", "--viewport-h"),
                            ("dpr", "--dpr")):
                if entry.get(k) is not None:
                    args.append(f"{flag}={entry[k]}")
            if entry.get("headless", True):
                args.append("--headless")
            else:
                args.append("--headed")
            # headed conmutable en runtime salvo opt-out explícito (estilo allow_flush de redis).
            if entry.get("allow_headed", True) is False:
                args.append("--forbid-headed")
            if entry.get("persistent_profile"):
                args.append(f"--persistent-profile={entry['persistent_profile']}")
            for k, flag in (("max_tabs", "--max-tabs"),
                            ("tab_idle_timeout", "--tab-idle-timeout"),
                            ("browser_idle_timeout", "--browser-idle-timeout"),
                            ("artifact_dir", "--artifact-dir"),
                            ("artifact_ttl", "--artifact-ttl"),
                            ("max_artifacts", "--max-artifacts")):
                if entry.get(k) is not None:
                    args.append(f"{flag}={entry[k]}")

            # VSCode instalado como snap exporta GIO_MODULE_DIR apuntando a los módulos
            # GIO del snap, linkeados contra la glibc de core20. El proceso de red de
            # WebKit los carga y muere con "undefined symbol: __libc_pthread_init".
            # Lo pisamos con los módulos del sistema; sin esto webkit no navega.
            # POSIX-only: en Windows esa ruta no existe y GIO no interviene.
            if os.name != "nt":
                env.setdefault("GIO_MODULE_DIR", GIO_MODULE_DIR_SYS)

        else:
            print(f"WARN: tipo desconocido '{kind}' para '{name}', ignorando.")
            continue

        result[name] = {"type": "stdio", "command": venv_python(), "args": args, "env": env}
        # timeout por servidor (ms) del lado cliente — debe ser >= que el timeout máx
        # interno del server para que gane su mensaje "[timeout]" en vez del corte seco.
        if entry.get("timeout_ms"):
            result[name]["timeout"] = entry["timeout_ms"]

    return result


def main():
    update_mode = "--update" in sys.argv
    remove_mode = "--remove" in sys.argv
    only_filter = None
    args_clean = []
    i = 1
    while i < len(sys.argv):
        a = sys.argv[i]
        if a in ("--update", "--remove"):
            pass
        elif a == "--only":
            i += 1
            only_filter = {n.strip() for n in sys.argv[i].split(",") if n.strip()}
        elif a.startswith("--only="):
            only_filter = {n.strip() for n in a.split("=", 1)[1].split(",") if n.strip()}
        else:
            args_clean.append(a)
        i += 1

    secrets = load_secrets()
    MCP_SERVERS = build_mcp_servers(secrets["mcp_servers"])

    if only_filter is not None and not remove_mode:
        missing = only_filter - MCP_SERVERS.keys()
        if missing:
            print(f"ERROR: MCPs no encontrados en secrets.json: {', '.join(sorted(missing))}")
            print(f"Disponibles: {', '.join(sorted(MCP_SERVERS.keys()))}")
            sys.exit(1)
        MCP_SERVERS = {k: v for k, v in MCP_SERVERS.items() if k in only_filter}

    if not args_clean:
        print("Uso: python3 add-mcp-to-project.py /ruta/absoluta/al/proyecto [--update|--remove] [--only name1,name2,...]")
        print()
        print("  --update          sobreescribe entradas existentes con los valores de secrets.json")
        print("  --remove          elimina los MCPs del proyecto (usar con --only para especificar cuáles)")
        print("  --only A,B,C      aplica la operación solo a esos MCPs")
        print()
        print("Proyectos disponibles en ~/.claude.json:")
        d = read_json(CLAUDE_JSON)
        for p in sorted(d.get("projects", {}).keys()):
            srv = list(d["projects"][p].get("mcpServers", {}).keys())
            tag = f"  [{', '.join(srv)}]" if srv else ""
            print(f"  {p}{tag}")
        sys.exit(0)

    project_path = os.path.abspath(args_clean[0])

    if not remove_mode:
        warn_if_no_venv()

    d = read_json(CLAUDE_JSON)

    if "projects" not in d:
        d["projects"] = {}

    if project_path not in d["projects"]:
        d["projects"][project_path] = {
            "allowedTools": [],
            "mcpContextUris": [],
            "mcpServers": {},
            "enabledMcpjsonServers": [],
            "disabledMcpjsonServers": [],
            "hasTrustDialogAccepted": False,
            "ignorePatterns": [],
            "projectOnboardingSeenCount": 0
        }

    existing = d["projects"][project_path].get("mcpServers", {})

    if remove_mode:
        to_remove = only_filter if only_filter else set(existing.keys())
        removed, not_found = [], []
        for name in sorted(to_remove):
            if name in existing:
                del existing[name]
                removed.append(name)
            else:
                not_found.append(name)
        d["projects"][project_path]["mcpServers"] = existing
        write_json_atomic(CLAUDE_JSON, d)
        print(f"Proyecto: {project_path}")
        if removed:
            print(f"  Eliminados  : {', '.join(removed)}")
        if not_found:
            print(f"  No existían : {', '.join(not_found)}")
        print()
        print("Reinicia Claude Code (VSCode) para que apliquen los cambios.")
        return

    added, updated, skipped = [], [], []

    for name, config in MCP_SERVERS.items():
        if name in existing:
            if update_mode:
                existing[name] = config
                updated.append(name)
            else:
                skipped.append(name)
        else:
            existing[name] = config
            added.append(name)

    d["projects"][project_path]["mcpServers"] = existing

    write_json_atomic(CLAUDE_JSON, d)

    print(f"Proyecto: {project_path}")
    if added:
        print(f"  Agregados   : {', '.join(added)}")
    if updated:
        print(f"  Actualizados: {', '.join(updated)}")
    if skipped:
        print(f"  Ya existían : {', '.join(skipped)}")
    print()
    print("Reinicia Claude Code (VSCode) para que carguen los MCPs.")


if __name__ == "__main__":
    main()
