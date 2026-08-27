#!/usr/bin/env python3
"""
Registra MCPs en un proyecto, para Claude Code (VSCode extension) y/o Codex CLI.

Uso:
    python3 "~/Skills/scripts/add-mcp-to-project.py" /ruta/al/proyecto [--update] [--only name1,name2,...]
    python3 "~/Skills/scripts/add-mcp-to-project.py" /ruta/al/proyecto --target codex
    python3 "~/Skills/scripts/add-mcp-to-project.py" --global --target both --only focusyn

Por qué es necesario:
    La VSCode extension de Claude Code lee los MCPs por proyecto desde
    ~/.claude.json → projects["/ruta/proyecto"]["mcpServers"].
    Los archivos ~/.claude/mcp.json y ~/.claude/settings.json son ignorados
    por la extensión de VSCode. Hay que registrar los servidores directamente
    en ~/.claude.json para cada proyecto.

    Codex CLI usa otro formato y otra ubicación: TOML en <proyecto>/.codex/config.toml
    (por proyecto, se mergea con el global) o ~/.codex/config.toml (global). Ver
    apply_codex() para los detalles feos —trust gate, secretos dentro del repo—.

Configuración:
    Todos los MCPs y sus credenciales se definen en scripts/secrets.json.
    Copiar scripts/secrets.example.json → scripts/secrets.json y completar.
    secrets.json es la fuente única: build_mcp_servers() produce la forma canónica
    (la de Claude) y el backend de Codex la traduce; no hay dos catálogos que sincronizar.

Portabilidad:
    Corre en Linux/macOS y en Windows. Las diferencias de plataforma están
    aisladas en venv_python() (layout del venv) y en el branch de webprobe
    (GIO_MODULE_DIR es POSIX-only). Tests: scripts/test_add_mcp_to_project.py
"""

import json
import math
import os
import stat
import subprocess
import sys
import tempfile

try:
    import tomllib  # 3.11+
except ModuleNotFoundError:  # pragma: no cover - solo en Pythons viejos
    tomllib = None

HOME = os.path.expanduser("~")
CLAUDE_JSON = os.path.join(HOME, ".claude.json")
SKILLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRETS_FILE = os.path.join(SKILLS_DIR, "scripts", "secrets.json")
MCP_SERVERS_DIR = os.path.join(HOME, ".claude", "mcp-servers")
# Módulos GIO del sistema — antídoto al GIO_MODULE_DIR que inyecta el snap de VSCode.
GIO_MODULE_DIR_SYS = "/usr/lib/x86_64-linux-gnu/gio/modules"

# --- Codex CLI ------------------------------------------------------------
# CODEX_HOME lo respeta el propio codex; respetarlo acá permite probar sin tocar
# la config real (y es lo que hacen los tests).
CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")
CODEX_CONFIG = os.path.join(CODEX_HOME, "config.toml")
CODEX_PROJECT_DIR = ".codex"
# El bloque de MCPs se delimita para poder regenerarlo sin pisar lo que el usuario
# escriba a mano en el mismo archivo (model, hooks, sandbox...).
CODEX_MARK_BEGIN = "# >>> mrgenkko-skills: mcp_servers (generado) >>>"
CODEX_MARK_END = "# <<< mrgenkko-skills: mcp_servers <<<"
# Arranque de un server Python del venv: import de deps + handshake. El default de
# Codex es corto y da timeouts espurios en la primera llamada del día.
CODEX_STARTUP_TIMEOUT_SEC = 30


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


def _write_atomic(path: str, emit, keep_mode: bool = True) -> None:
    """Escribe vía temporal + os.replace.

    ~/.claude.json es la config de TODOS los proyectos. Un dump directo trunca el
    archivo antes de escribir: si falla a mitad (disco lleno, valor no serializable,
    Ctrl-C) la config queda destruida. os.replace es atómico tanto en POSIX como
    en Windows.

    keep_mode=False deja el 0600 de mkstemp: es lo que corresponde para los TOML
    de Codex, que llevan contraseñas en texto plano.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".add-mcp-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            emit(f)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp crea 0600; conservar los permisos del archivo original.
        if keep_mode and os.path.exists(path):
            os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_json_atomic(path: str, data: dict) -> None:
    _write_atomic(path, lambda f: json.dump(data, f, indent=2))


def write_text_atomic(path: str, text: str, keep_mode: bool = True) -> None:
    _write_atomic(path, lambda f: f.write(text), keep_mode=keep_mode)


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

        elif kind == "pixelorama":
            args = [
                server_path("pixelorama", "server.py"),
                f"--name={entry['name']}",
            ]
            # Sin workdir la instancia es genérica: el cliente MCP spawnea el server con
            # cwd = el directorio del proyecto, y ahí se resuelven las rutas relativas.
            # Fijarlo solo tiene sentido para una instancia atada a un repo concreto.
            if entry.get("workdir"):
                args.append(f"--workdir={entry['workdir']}")
            # Vacío = autodetectar (PATH, ~/.local/opt/pixelorama, /usr/bin).
            if entry.get("pixelorama_bin"):
                args.append(f"--pixelorama-bin={entry['pixelorama_bin']}")
            # Topes de memoria: un lienzo ocupa w*h*4 bytes POR capa y POR frame.
            for k, flag in (("max_pixels", "--max-pixels"),
                            ("max_layers", "--max-layers"),
                            ("max_frames", "--max-frames")):
                if entry.get(k) is not None:
                    args.append(f"{flag}={entry[k]}")

        else:
            print(f"WARN: tipo desconocido '{kind}' para '{name}', ignorando.")
            continue

        result[name] = {"type": "stdio", "command": venv_python(), "args": args, "env": env}
        # timeout por servidor (ms) del lado cliente — debe ser >= que el timeout máx
        # interno del server para que gane su mensaje "[timeout]" en vez del corte seco.
        if entry.get("timeout_ms"):
            result[name]["timeout"] = entry["timeout_ms"]

    return result


# --------------------------------------------------------------------------
# Backend Codex CLI
#
# Codex lee MCPs de dos lugares y los MERGEA (verificado en codex-cli 0.148):
#   ~/.codex/config.toml            → [mcp_servers.X]  global, todos los proyectos
#   <proyecto>/.codex/config.toml   → [mcp_servers.X]  solo ese proyecto
#
# Dos trampas que este backend cubre:
#   1. El config del proyecto SOLO se lee si el proyecto está marcado
#      trust_level = "trusted" en ~/.codex/config.toml. Si no lo está, Codex
#      ignora el archivo en silencio, sin warning: parece que el MCP no existe.
#   2. El archivo vive DENTRO del repo y nuestros MCPs llevan credenciales en
#      args (--password=) y env (GH_TOKEN). Un `git add .` distraído las publica.
#      Por eso: .codex/ al .gitignore, 0600, y abortar si ya está trackeado.
# --------------------------------------------------------------------------

def toml_escape(value: str) -> str:
    """Basic string TOML. Los control chars van escapados o el archivo no parsea."""
    out = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def toml_inline_table(data: dict) -> str:
    return "{ " + ", ".join(f"{toml_escape(k)} = {toml_escape(v)}"
                            for k, v in data.items()) + " }"


def to_codex_server(config: dict) -> dict:
    """Traduce una entrada canónica (forma Claude) a la forma de Codex.

    Claude y Codex nombran distinto lo mismo: headers→http_headers,
    timeout (ms, por tool call)→tool_timeout_sec.
    """
    out = {}
    if config.get("type") == "http":
        out["url"] = config["url"]
        if config.get("headers"):
            out["http_headers"] = dict(config["headers"])
    else:
        out["command"] = config["command"]
        out["args"] = list(config.get("args", []))
        if config.get("env"):
            out["env"] = dict(config["env"])
        out["startup_timeout_sec"] = CODEX_STARTUP_TIMEOUT_SEC
    if config.get("timeout"):
        out["tool_timeout_sec"] = max(1, math.ceil(config["timeout"] / 1000))
    return out


def render_codex_block(servers: dict) -> str:
    """Renderiza el bloque delimitado con todas las entradas [mcp_servers.X]."""
    lines = [CODEX_MARK_BEGIN,
             "# Generado por Mrgenkko Skills desde scripts/secrets.json — no editar a mano.",
             "# Contiene credenciales en texto plano: este archivo NO se commitea."]
    for name in sorted(servers):
        cfg = servers[name]
        lines.append("")
        lines.append(f"[mcp_servers.{toml_escape(name)}]")
        for key in ("command", "url"):
            if key in cfg:
                lines.append(f"{key} = {toml_escape(cfg[key])}")
        if cfg.get("args"):
            if len(cfg["args"]) == 1:
                lines.append(f"args = [{toml_escape(cfg['args'][0])}]")
            else:
                lines.append("args = [")
                lines.extend(f"    {toml_escape(a)}," for a in cfg["args"])
                lines.append("]")
        elif "command" in cfg:
            lines.append("args = []")
        for key in ("env", "http_headers"):
            if cfg.get(key):
                lines.append(f"{key} = {toml_inline_table(cfg[key])}")
        for key in ("startup_timeout_sec", "tool_timeout_sec"):
            if key in cfg:
                lines.append(f"{key} = {cfg[key]}")
    lines.append("")
    lines.append(CODEX_MARK_END)
    return "\n".join(lines) + "\n"


def parse_toml(text: str, origen: str) -> dict:
    """Parsea TOML; sin tomllib (Python <3.11) devuelve {} y el caller degrada."""
    if tomllib is None:
        return {}
    try:
        return tomllib.loads(text)
    except Exception as exc:
        print(f"ERROR: {origen} no es TOML válido: {exc}")
        sys.exit(1)


def strip_codex_block(text: str) -> tuple:
    """Parte el archivo en (antes, después) del bloque generado.

    Si no hay bloque, el bloque nuevo va al final: en TOML una clave suelta
    después de una tabla pertenece a esa tabla, así que insertarlo en el medio
    reasignaría silenciosamente las claves top-level que vinieran después.
    """
    inicio = text.find(CODEX_MARK_BEGIN)
    if inicio == -1:
        return text, ""
    fin = text.find(CODEX_MARK_END, inicio)
    if fin == -1:
        print(f"ERROR: falta el marcador de cierre '{CODEX_MARK_END}'.")
        print("       Restaurá o borrá el bloque generado y volvé a correr.")
        sys.exit(1)
    return text[:inicio], text[fin + len(CODEX_MARK_END):].lstrip("\n")


def assert_sin_mcp_a_mano(text_fuera: str, path: str) -> None:
    """Aborta si hay [mcp_servers.*] escrito a mano fuera del bloque generado.

    Regenerar duplicaría la tabla y Codex fallaría al parsear. Se resuelve a
    mano una vez, no adivinando qué quiso decir el usuario.
    """
    for linea in text_fuera.splitlines():
        limpia = linea.strip()
        if limpia.startswith("[mcp_servers") or limpia.startswith("mcp_servers"):
            print(f"ERROR: {path} tiene MCPs declarados fuera del bloque generado:")
            print(f"       {limpia}")
            print("       Borralos (o movelos dentro del bloque) y volvé a correr.")
            sys.exit(1)


def git_toplevel(path: str):
    """Raíz del repo que contiene path, o None si no es repo (o no hay git)."""
    try:
        out = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=False)
    except (OSError, ValueError):
        return None
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


def proteger_del_repo(project_path: str, dry_run: bool = False) -> None:
    """.codex/ al .gitignore, y abortar si el config ya quedó trackeado."""
    toplevel = git_toplevel(project_path)
    if not toplevel:
        return

    rel = os.path.relpath(os.path.join(project_path, CODEX_PROJECT_DIR, "config.toml"),
                          toplevel).replace(os.sep, "/")
    tracked = subprocess.run(["git", "-C", toplevel, "ls-files", "--error-unmatch", rel],
                             capture_output=True, text=True, check=False)
    if tracked.returncode == 0:
        print(f"ERROR: {rel} ya está trackeado en git.")
        print("       Ese archivo lleva contraseñas en texto plano. Sacalo del índice antes:")
        print(f'         git -C "{toplevel}" rm --cached "{rel}"')
        print("       y rotá cualquier credencial que ya haya viajado a un remoto.")
        sys.exit(1)

    gitignore = os.path.join(toplevel, ".gitignore")
    patron = f"{CODEX_PROJECT_DIR}/"
    actual = ""
    if os.path.exists(gitignore):
        with open(gitignore, encoding="utf-8") as f:
            actual = f.read()
        if any(l.strip() in (patron, CODEX_PROJECT_DIR, f"/{patron}", f"/{CODEX_PROJECT_DIR}")
               for l in actual.splitlines()):
            return
    if dry_run:
        print(f"  .gitignore  : agregaría '{patron}' en {toplevel}")
        return
    sufijo = "" if (not actual or actual.endswith("\n")) else "\n"
    write_text_atomic(
        gitignore,
        actual + sufijo + f"\n# Config de MCPs de Codex — credenciales en claro\n{patron}\n",
    )
    print(f"  .gitignore  : agregado '{patron}' en {toplevel}")


def asegurar_trust(project_path: str, dry_run: bool = False) -> None:
    """Marca el proyecto como trusted en ~/.codex/config.toml.

    Sin esto Codex ignora <proyecto>/.codex/config.toml sin decir nada.
    """
    texto = ""
    if os.path.exists(CODEX_CONFIG):
        with open(CODEX_CONFIG, encoding="utf-8") as f:
            texto = f.read()

    datos = parse_toml(texto, CODEX_CONFIG)
    entrada = (datos.get("projects") or {}).get(project_path) or {}
    if entrada.get("trust_level") == "trusted":
        return

    header = f"[projects.{toml_escape(project_path)}]"
    if header in texto:
        # La sección existe pero no está trusted: tocar solo su trust_level.
        lineas = texto.splitlines()
        idx = lineas.index(header)
        fin = next((i for i in range(idx + 1, len(lineas))
                    if lineas[i].lstrip().startswith("[")), len(lineas))
        for i in range(idx + 1, fin):
            if lineas[i].split("=")[0].strip() == "trust_level":
                lineas[i] = 'trust_level = "trusted"'
                break
        else:
            lineas.insert(idx + 1, 'trust_level = "trusted"')
        nuevo = "\n".join(lineas) + "\n"
    elif datos.get("projects", {}).get(project_path) is not None:
        # Existe con otro quoting del que no podemos deducir la forma exacta.
        print(f"WARN: {project_path} ya figura en {CODEX_CONFIG} con otro formato.")
        print('      Ponele trust_level = "trusted" a mano o Codex ignorará sus MCPs.')
        return
    else:
        sufijo = "" if (not texto or texto.endswith("\n")) else "\n"
        nuevo = texto + sufijo + f'\n{header}\ntrust_level = "trusted"\n'

    if dry_run:
        print(f"  trust       : marcaría {project_path} trusted en {CODEX_CONFIG}")
        return
    os.makedirs(CODEX_HOME, exist_ok=True)
    write_text_atomic(CODEX_CONFIG, nuevo)
    print(f"  trust       : {project_path} marcado trusted en {CODEX_CONFIG}")


def apply_codex(project_path, servers, update_mode, remove_mode, only_filter,
                global_scope, dry_run=False):
    """Escribe los MCPs en el config TOML de Codex (global o del proyecto)."""
    pfx = "[dry-run] " if dry_run else ""
    if global_scope:
        destino = CODEX_CONFIG
        if not dry_run:
            os.makedirs(CODEX_HOME, exist_ok=True)
    else:
        destino = os.path.join(project_path, CODEX_PROJECT_DIR, "config.toml")
        proteger_del_repo(project_path, dry_run)
        asegurar_trust(project_path, dry_run)
        if not dry_run:
            os.makedirs(os.path.dirname(destino), exist_ok=True)

    texto = ""
    if os.path.exists(destino):
        with open(destino, encoding="utf-8") as f:
            texto = f.read()
    antes, despues = strip_codex_block(texto)
    assert_sin_mcp_a_mano(antes + "\n" + despues, destino)

    # Estado actual: lo que ya declara el bloque generado.
    bloque_actual = ""
    if CODEX_MARK_BEGIN in texto:
        i = texto.find(CODEX_MARK_BEGIN)
        bloque_actual = texto[i:texto.find(CODEX_MARK_END, i)]
    existentes = parse_toml(bloque_actual, destino).get("mcp_servers", {})

    added, updated, skipped, removed, not_found = [], [], [], [], []

    if remove_mode:
        objetivo = only_filter if only_filter else set(existentes.keys())
        for name in sorted(objetivo):
            if name in existentes:
                del existentes[name]
                removed.append(name)
            else:
                not_found.append(name)
    else:
        for name, config in servers.items():
            nuevo = to_codex_server(config)
            if name in existentes:
                if update_mode:
                    existentes[name] = nuevo
                    updated.append(name)
                else:
                    skipped.append(name)
            else:
                existentes[name] = nuevo
                added.append(name)

    if existentes:
        cuerpo = antes.rstrip("\n")
        cuerpo = (cuerpo + "\n\n") if cuerpo else ""
        final = cuerpo + render_codex_block(existentes)
        if despues.strip():
            final += "\n" + despues
    else:
        final = (antes.rstrip("\n") + "\n" + despues) if despues.strip() else antes

    # Red de seguridad: no dejar un TOML roto en disco. Codex no arranca con él.
    if tomllib is not None:
        try:
            verificado = tomllib.loads(final)
        except Exception as exc:
            print(f"ERROR: el TOML generado para {destino} no parsea: {exc}")
            print("       No se escribió nada.")
            sys.exit(1)
        if set(verificado.get("mcp_servers", {})) != set(existentes):
            print(f"ERROR: el TOML generado para {destino} no contiene los MCPs esperados.")
            print("       No se escribió nada.")
            sys.exit(1)

    if not dry_run:
        write_text_atomic(destino, final, keep_mode=False)

    print(f"{pfx}Codex ({'global' if global_scope else 'proyecto'}): {destino}")
    for etiqueta, valores in (("Agregados   ", added), ("Actualizados", updated),
                              ("Ya existían ", skipped), ("Eliminados  ", removed),
                              ("No existían ", not_found)):
        if valores:
            print(f"  {etiqueta}: {', '.join(valores)}")
    print()
    if dry_run:
        print("dry-run: no se escribió nada.")
    else:
        print("Reinicia Codex para que carguen los MCPs (`codex mcp list` para verificar).")


def apply_claude(project_path, servers, update_mode, remove_mode, only_filter,
                 global_scope, dry_run=False):
    """Escribe los MCPs en ~/.claude.json (scope de proyecto o global)."""
    d = read_json(CLAUDE_JSON)

    if global_scope:
        existing = d.setdefault("mcpServers", {})
        destino = f"{CLAUDE_JSON} → mcpServers (global)"
    else:
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
        existing = d["projects"][project_path].setdefault("mcpServers", {})
        destino = project_path

    added, updated, skipped, removed, not_found = [], [], [], [], []

    if remove_mode:
        objetivo = only_filter if only_filter else set(existing.keys())
        for name in sorted(objetivo):
            if name in existing:
                del existing[name]
                removed.append(name)
            else:
                not_found.append(name)
    else:
        for name, config in servers.items():
            if name in existing:
                if update_mode:
                    existing[name] = config
                    updated.append(name)
                else:
                    skipped.append(name)
            else:
                existing[name] = config
                added.append(name)

    if not dry_run:
        write_json_atomic(CLAUDE_JSON, d)

    print(f"{'[dry-run] ' if dry_run else ''}Claude Code: {destino}")
    for etiqueta, valores in (("Agregados   ", added), ("Actualizados", updated),
                              ("Ya existían ", skipped), ("Eliminados  ", removed),
                              ("No existían ", not_found)):
        if valores:
            print(f"  {etiqueta}: {', '.join(valores)}")
    print()
    if dry_run:
        print("dry-run: no se escribió nada.")
    else:
        print("Reinicia Claude Code (VSCode) para que carguen los MCPs.")


def print_usage():
    print("Uso: python3 add-mcp-to-project.py /ruta/absoluta/al/proyecto [opciones]")
    print("     python3 add-mcp-to-project.py --global --only NOMBRE [opciones]")
    print()
    print("  --update           sobreescribe entradas existentes con los valores de secrets.json")
    print("  --remove           elimina los MCPs del proyecto (usar con --only para especificar cuáles)")
    print("  --only A,B,C       aplica la operación solo a esos MCPs")
    print("  --target T         claude (default) | codex | both")
    print("  --global           scope global en vez de por proyecto")
    print("  --dry-run          muestra lo que haría sin escribir nada")
    print("  --yes, -y          no pedir confirmación en --remove")
    print("  --help, -h         muestra esta ayuda")
    print()
    print("Proyectos disponibles en ~/.claude.json:")
    d = read_json(CLAUDE_JSON)
    for p in sorted(d.get("projects", {}).keys()):
        srv = list(d["projects"][p].get("mcpServers", {}).keys())
        tag = f"  [{', '.join(srv)}]" if srv else ""
        print(f"  {p}{tag}")


def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print_usage()
        sys.exit(0)

    update_mode = "--update" in sys.argv
    remove_mode = "--remove" in sys.argv
    global_scope = "--global" in sys.argv
    dry_run = "--dry-run" in sys.argv
    assume_yes = "--yes" in sys.argv or "-y" in sys.argv
    target = "claude"
    only_filter = None
    args_clean = []
    i = 1
    while i < len(sys.argv):
        a = sys.argv[i]
        if a in ("--update", "--remove", "--global", "--dry-run", "--yes", "-y"):
            pass
        elif a == "--only":
            i += 1
            if i >= len(sys.argv):
                print("ERROR: --only necesita una lista de MCPs (ej: --only pixelorama).")
                sys.exit(1)
            only_filter = {n.strip() for n in sys.argv[i].split(",") if n.strip()}
        elif a.startswith("--only="):
            only_filter = {n.strip() for n in a.split("=", 1)[1].split(",") if n.strip()}
        elif a == "--target":
            i += 1
            if i >= len(sys.argv):
                print("ERROR: --target necesita un valor: claude | codex | both.")
                sys.exit(1)
            target = sys.argv[i].strip().lower()
        elif a.startswith("--target="):
            target = a.split("=", 1)[1].strip().lower()
        elif a.startswith("-"):
            # Sin esto, un flag mal escrito (--codex, --help) caía en args_clean y se
            # trataba como ruta de proyecto: registraba los MCPs en un proyecto fantasma.
            print(f"ERROR: opción desconocida '{a}'.")
            print()
            print_usage()
            sys.exit(1)
        else:
            args_clean.append(a)
        i += 1

    if target not in ("claude", "codex", "both"):
        print(f"ERROR: --target inválido '{target}'. Opciones: claude | codex | both")
        sys.exit(1)

    secrets = load_secrets()
    MCP_SERVERS = build_mcp_servers(secrets["mcp_servers"])

    if only_filter is not None and not remove_mode:
        missing = only_filter - MCP_SERVERS.keys()
        if missing:
            print(f"ERROR: MCPs no encontrados en secrets.json: {', '.join(sorted(missing))}")
            print(f"Disponibles: {', '.join(sorted(MCP_SERVERS.keys()))}")
            sys.exit(1)
        MCP_SERVERS = {k: v for k, v in MCP_SERVERS.items() if k in only_filter}

    if not args_clean and not global_scope:
        print_usage()
        sys.exit(0)

    if global_scope:
        if args_clean:
            print("ERROR: --global no lleva ruta de proyecto.")
            sys.exit(1)
        project_path = None
    else:
        if len(args_clean) > 1:
            # Casi siempre es una ruta con espacios sin comillar ("Mrgenkko Skills").
            print(f"ERROR: se esperaba UNA ruta de proyecto, llegaron {len(args_clean)}:")
            for a in args_clean:
                print(f"  {a}")
            print("Si la ruta tiene espacios, encerrala entre comillas.")
            sys.exit(1)
        project_path = os.path.abspath(args_clean[0])
        # En --remove no se exige que exista: limpiar la entrada de un proyecto ya
        # borrado del disco es legítimo. Al registrar sí, o queda un proyecto fantasma.
        if not remove_mode and not os.path.isdir(project_path):
            print(f"ERROR: el proyecto no existe en disco: {project_path}")
            print("Registrar MCPs ahí sólo crearía una entrada fantasma en ~/.claude.json.")
            print("Creá el directorio primero, o revisá la ruta.")
            sys.exit(1)

    if not remove_mode:
        warn_if_no_venv()

    def dispatch(dry):
        if target in ("claude", "both"):
            apply_claude(project_path, MCP_SERVERS, update_mode, remove_mode,
                         only_filter, global_scope, dry)
        if target == "both":
            print()
        if target in ("codex", "both"):
            apply_codex(project_path, MCP_SERVERS, update_mode, remove_mode,
                        only_filter, global_scope, dry)

    # --remove es destructivo y sin --only barre TODOS los MCPs del scope: se
    # confirma sobre una vista previa real, no sobre lo que el usuario creyó pedir.
    if remove_mode and not assume_yes and not dry_run:
        print("Vista previa — esto es lo que se eliminaría:")
        print()
        dispatch(True)
        print()
        alcance = ", ".join(sorted(only_filter)) if only_filter else "TODOS los MCPs del scope"
        print(f"A eliminar: {alcance}")
        if not sys.stdin.isatty():
            print("ERROR: --remove pide confirmación y stdin no es interactivo.")
            print("       Agregá --yes si estás seguro (o --dry-run para ver sin tocar).")
            sys.exit(1)
        try:
            resp = input("Escribí 'si' para confirmar: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            resp = ""
        if resp not in ("si", "sí"):
            print("Cancelado. No se escribió nada.")
            sys.exit(1)
        print()

    dispatch(dry_run)


if __name__ == "__main__":
    main()
