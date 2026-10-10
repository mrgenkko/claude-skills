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
    Todos los MCPs se definen en scripts/secrets.json; sus credenciales, en el llavero
    del SO, referenciadas como keyring:<id> (scripts/mcp-secrets.py, guides/secrets.md).
    Con referencia, al registro va solo el NOMBRE de la clave: scripts/mcp-launch.py la
    lee al arrancar el MCP. Copiar scripts/secrets.example.json → scripts/secrets.json.
    secrets.json es la fuente única: build_mcp_servers() produce la forma canónica
    (la de Claude) y el backend de Codex la traduce; no hay dos catálogos que sincronizar.

Portabilidad:
    Corre en Linux/macOS y en Windows. Las diferencias de plataforma están
    aisladas en venv_python() (layout del venv) y en el branch de webprobe
    (GIO_MODULE_DIR es POSIX-only). Tests: scripts/test_add_mcp_to_project.py
"""

import contextlib
import io
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

# secret_refs vive al lado; se agrega la carpeta porque este script también se
# carga por ruta (importlib) desde los tests, sin scripts/ en el sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import secret_refs  # noqa: E402

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


# --- Credenciales: llavero del SO ----------------------------------------------
# En secrets.json un campo secreto es `keyring:<id>` (el valor vive en el llavero,
# ver scripts/mcp-secrets.py) o texto plano (legacy). Con referencia, al registro
# va solo el NOMBRE: el lanzador lee el valor al arrancar el MCP y se lo pasa por
# env. Así ~/.claude.json y los .codex/config.toml no guardan ninguna clave.
LAUNCHER = os.path.join(SKILLS_DIR, "scripts", "mcp-launch.py")
# Helpers de headers (MCP remotos): un ejecutable por server, en una ruta sin
# espacios y SIN argumentos. Codex solo acepta un string en http_headers_helper y
# no documenta si lo pasa por un shell; así funciona de cualquier forma.
HEADER_HELPERS_DIR = os.path.join(MCP_SERVERS_DIR, "_launch")
# Variables que el lanzador necesita para hablar con el Secret Service. Codex
# filtra el entorno de sus MCP (por eso existe env_vars): hay que pedirlas.
LAUNCHER_ENV_VARS = ["DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"]

def credencial(entry, campo, args, env, refs_out, requerido=False, vacio_ok=False):
    """Ubica una credencial de secrets.json en el registro. Variable y flag salen
    de secret_refs.SECRET_FIELDS.

    keyring:<id> → refs_out recibe "VAR=keyring:<id>" (solo el nombre).
    texto plano  → como siempre: `flag=valor` en args, o env[var] si no hay flag.
    requerido    → falta o vacía = error al registrar (gh sin token caería en la
                   cuenta logueada en hosts.yml, sin avisar).
    vacio_ok     → "" se registra como `flag=` (postgres con auth trust o .pgpass).
    """
    var, flag = secret_refs.destino(entry["type"], campo)
    valor = entry.get(campo)
    if valor == "" and vacio_ok and flag:
        args.append(f"{flag}=")
        return
    if not valor:
        if requerido:
            print(f"ERROR: {entry['name']}: falta `{campo}` en secrets.json "
                  f"(\"keyring:{secret_refs.default_id(entry['name'], campo)}\" + mcp-secrets.py set).")
            sys.exit(1)
        return
    if secret_refs.is_ref(valor):
        refs_out.append(f"{var}={valor}")
    elif flag:
        args.append(f"{flag}={valor}")
    else:
        env[var] = valor


def con_lanzador(args, refs_out):
    """args del server → args del lanzador que lee las claves y hace exec al server."""
    if not refs_out:
        return args
    secretos = [x for r in refs_out for x in ("--secret", r)]
    return [LAUNCHER, *secretos, "--", *args]


def ruta_header_helper(name):
    """Dónde vive el helper de headers de `name`: sin espacios ni argumentos."""
    seguro = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return os.path.join(HEADER_HELPERS_DIR, f"headers-{seguro}" + (".cmd" if os.name == "nt" else ""))


def contenido_header_helper(name, header, ref):
    linea = [venv_python(), LAUNCHER, "headers", f"{header}={ref}"]
    if os.name == "nt":
        return "@echo off\r\n" + subprocess.list2cmdline(linea) + "\r\n"
    import shlex
    return ("#!/bin/sh\n"
            f"# Generado por add-mcp-to-project.py: headers de {name}. Solo ids, nunca valores.\n"
            "exec " + " ".join(shlex.quote(p) for p in linea) + "\n")


def header_helpers_pedidos(servers_config: list, nombres) -> dict:
    """ruta → contenido de los helpers que necesitan los MCPs `nombres`."""
    nombres, pedidos = set(nombres), {}
    for e in servers_config:
        if e.get("name") in nombres and e.get("type") == "focusyn" and secret_refs.is_ref(e.get("agent_key")):
            header, _ = secret_refs.destino("focusyn", "agent_key")
            pedidos[ruta_header_helper(e["name"])] = contenido_header_helper(e["name"], header, e["agent_key"])
    return pedidos


def helpers_desactualizados(pedidos: dict) -> dict:
    """Los pedidos cuyo archivo falta o tiene otro contenido (repo o venv movidos)."""
    out = {}
    for ruta, contenido in pedidos.items():
        try:
            with open(ruta, encoding="utf-8") as f:
                if f.read() == contenido:
                    continue
        except OSError:
            pass
        out[ruta] = contenido
    return out


def escribir_header_helpers(pedidos: dict, dry_run) -> None:
    for ruta, contenido in sorted(pedidos.items()):
        if dry_run:
            print(f"  (escribiría el helper de headers {ruta})")
            continue
        os.makedirs(os.path.dirname(ruta), exist_ok=True)
        write_text_atomic(ruta, contenido, keep_mode=False)
        if os.name != "nt":
            os.chmod(ruta, 0o700)


def limpiar_header_helpers(dry_run) -> None:
    """Borra los helpers que ya no referencia ningún config (Claude ni Codex).

    Un helper se comparte entre scopes (focusyn global + overrides por proyecto):
    solo se borra cuando nadie más lo usa.
    """
    if not os.path.isdir(HEADER_HELPERS_DIR):
        return
    d = read_json(CLAUDE_JSON)
    usados = set()
    for _, srv in scopes_claude(d):
        usados |= {c["headersHelper"] for c in srv.values() if isinstance(c, dict) and c.get("headersHelper")}
    for _, destino, texto in configs_codex(d):
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                srv = servidores_del_bloque(texto, destino)
        except SystemExit:
            # Un config ilegible: no se puede saber qué helpers usa. Ante la duda,
            # no se borra nada (y no se corta el programa después de haber escrito).
            print(f"  (no se limpian helpers: {destino} no se pudo leer)")
            return
        usados |= {c["http_headers_helper"] for c in srv.values() if c.get("http_headers_helper")}
    # Comparar rutas normalizadas: un config escrito a mano con "~/..." o por un
    # symlink sigue usando el helper aunque el string no coincida.
    usados = {os.path.realpath(os.path.expanduser(u)) for u in usados if isinstance(u, str)}
    for nombre in sorted(os.listdir(HEADER_HELPERS_DIR)):
        ruta = os.path.join(HEADER_HELPERS_DIR, nombre)
        if nombre.startswith("headers-") and os.path.realpath(ruta) not in usados:
            print(f"  {'(borraría' if dry_run else '(borrado'} el helper huérfano {ruta})")
            if not dry_run:
                os.unlink(ruta)


def abortar_si_refs_invalidas(servers_config: list, nombres) -> None:
    """`keyring:` solo vale en un campo secreto y con id. Fuera de eso se
    registraría el texto literal (un arg, una ruta) o fallaría recién al arrancar."""
    malas = []
    for e in servers_config:
        if e.get("name") not in set(nombres):
            continue
        for campo, valor in e.items():
            if not secret_refs.is_ref(valor):
                continue
            if campo not in secret_refs.secret_fields(e):
                malas.append(f"{e['name']}.{campo}: este campo no es una credencial (no admite keyring:)")
            elif not secret_refs.ref_id(valor).strip():
                malas.append(f"{e['name']}.{campo}: referencia sin id (\"keyring:\")")
    if malas:
        print("ERROR: referencias al llavero inválidas en secrets.json:")
        for m in malas:
            print(f"         {m}")
        print("       No se tocó nada.")
        sys.exit(1)


def avisar_texto_plano(servers_config: list, nombres) -> None:
    nombres = set(nombres)
    planos = sorted(f"{e['name']}.{c}" for e in servers_config if e.get("name") in nombres
                    for c in secret_refs.secret_fields(e)
                    if e.get(c) and not secret_refs.is_ref(e[c]))
    if planos:
        print(f"WARN: credenciales en texto plano en secrets.json: {', '.join(planos)}")
        print("      Se registran como antes (visibles en el config). Pasalas al llavero con:")
        print("        mcp-secrets.py migrate")
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
        # "VAR=keyring:<id>" de las credenciales que están en el llavero.
        refs_out = []

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
            ]
            credencial(entry, "password", args, env, refs_out, requerido=True, vacio_ok=True)

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
            else:
                credencial(entry, "password", args, env, refs_out)
            credencial(entry, "sudo_password", args, env, refs_out)
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
            credencial(entry, "password", args, env, refs_out)
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
            credencial(entry, "token", args, env, refs_out, requerido=True)
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
            result[name] = {"type": "http", "url": entry["url"]}
            if secret_refs.is_ref(entry["agent_key"]):
                # Claude Code corre el helper en cada conexión y usa los headers que imprime.
                result[name]["headersHelper"] = ruta_header_helper(name)
            else:
                result[name]["headers"] = {"X-Agent-Key": entry["agent_key"]}
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

        elif kind == "modal":
            args = [
                server_path("modal", "server.py"),
                f"--workspace={entry['workspace']}",
                f"--name={name}",
            ]
            if entry.get("environment"):
                args.append(f"--environment={entry['environment']}")
            if entry.get("modal_bin"):
                args.append(f"--modal-bin={entry['modal_bin']}")
            # Perfil aislado: ningún subcomando toca el ~/.modal.toml global, así que
            # el workspace activo no puede derivar entre instancias.
            if entry.get("config_path"):
                args.append(f"--config-path={entry['config_path']}")
            # Dos gates independientes: escribir (deploy/stop/rm) y prender cómputo
            # facturado por segundo (run, endpoint create). Ambos apagados por defecto.
            if entry.get("allow_write"):
                args.append("--allow-write")
            if entry.get("allow_gpu"):
                args.append("--allow-gpu")
            # El token va por ENV, no por args: la línea de comandos de un proceso la
            # lee cualquier usuario de la máquina con `ps aux`, mientras que
            # /proc/<pid>/environ es 0400 del dueño. La CLI de modal resuelve
            # `env MODAL_*` ANTES que el perfil en disco, así que con esto queda
            # stateless — es el mismo efecto que GH_TOKEN en gh.
            credencial(entry, "token_id", args, env, refs_out)
            credencial(entry, "token_secret", args, env, refs_out)

        else:
            print(f"WARN: tipo desconocido '{kind}' para '{name}', ignorando.")
            continue

        result[name] = {"type": "stdio", "command": venv_python(),
                        "args": con_lanzador(args, refs_out), "env": env}
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
        if config.get("headersHelper"):
            out["http_headers_helper"] = config["headersHelper"]
    else:
        out["command"] = config["command"]
        out["args"] = list(config.get("args", []))
        if config.get("env"):
            out["env"] = dict(config["env"])
        # El lanzador necesita el bus de sesión para leer el llavero, y Codex no
        # reenvía el entorno padre a sus MCP salvo lo que se pida en env_vars.
        if out["args"][:1] == [LAUNCHER] and os.name != "nt":
            out["env_vars"] = list(LAUNCHER_ENV_VARS)
        out["startup_timeout_sec"] = CODEX_STARTUP_TIMEOUT_SEC
    if config.get("timeout"):
        out["tool_timeout_sec"] = max(1, math.ceil(config["timeout"] / 1000))
    return out


def render_codex_block(servers: dict) -> str:
    """Renderiza el bloque delimitado con todas las entradas [mcp_servers.X]."""
    lines = [CODEX_MARK_BEGIN,
             "# Generado por Mrgenkko Skills desde scripts/secrets.json — no editar a mano.",
             "# Puede contener credenciales sin migrar al llavero: este archivo NO se commitea."]
    for name in sorted(servers):
        cfg = servers[name]
        lines.append("")
        lines.append(f"[mcp_servers.{toml_escape(name)}]")
        for key in ("command", "url", "http_headers_helper"):
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
        if cfg.get("env_vars"):
            lines.append("env_vars = [" + ", ".join(toml_escape(v) for v in cfg["env_vars"]) + "]")
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


def partir_codex(text: str) -> tuple:
    """(antes, bloque, después) del bloque generado; bloque = "" si no hay.
    Sale con error si falta el marcador de cierre."""
    inicio = text.find(CODEX_MARK_BEGIN)
    if inicio == -1:
        return text, "", ""
    fin = text.find(CODEX_MARK_END, inicio)
    if fin == -1:
        print(f"ERROR: falta el marcador de cierre '{CODEX_MARK_END}'.")
        print("       Restaurá o borrá el bloque generado y volvé a correr.")
        sys.exit(1)
    return text[:inicio], text[inicio:fin], text[fin + len(CODEX_MARK_END):].lstrip("\n")


def strip_codex_block(text: str) -> tuple:
    """Parte el archivo en (antes, después) del bloque generado.

    Si no hay bloque, el bloque nuevo va al final: en TOML una clave suelta
    después de una tabla pertenece a esa tabla, así que insertarlo en el medio
    reasignaría silenciosamente las claves top-level que vinieran después.
    """
    antes, _, despues = partir_codex(text)
    return antes, despues


def servidores_del_bloque(texto: str, destino: str) -> dict:
    """[mcp_servers.*] del bloque generado (vacío si no hay bloque). Sale con error
    si falta el marcador de cierre, igual que strip_codex_block."""
    _, bloque, _ = partir_codex(texto)
    return parse_toml(bloque, destino).get("mcp_servers", {}) if bloque else {}


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


def abortar_si_codex_trackeado(project_path: str):
    """Sale con error si <proyecto>/.codex/config.toml está trackeado en git.
    Devuelve la raíz del repo (o None si no es repo)."""
    toplevel = git_toplevel(project_path)
    if not toplevel:
        return None
    rel = os.path.relpath(os.path.join(project_path, CODEX_PROJECT_DIR, "config.toml"),
                          toplevel).replace(os.sep, "/")
    tracked = subprocess.run(["git", "-C", toplevel, "ls-files", "--error-unmatch", rel],
                             capture_output=True, text=True, check=False)
    if tracked.returncode == 0:
        print(f"ERROR: {rel} ya está trackeado en git.")
        print("       Ese archivo puede llevar contraseñas en texto plano. Sacalo del índice antes:")
        print(f'         git -C "{toplevel}" rm --cached "{rel}"')
        print("       y rotá cualquier credencial que ya haya viajado a un remoto.")
        sys.exit(1)
    return toplevel


def proteger_del_repo(project_path: str, dry_run: bool = False) -> None:
    """.codex/ al .gitignore, y abortar si el config ya quedó trackeado."""
    toplevel = abortar_si_codex_trackeado(project_path)
    if not toplevel:
        return

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
                global_scope, dry_run=False, solo_bloque=False):
    """Escribe los MCPs en el config TOML de Codex (global o del proyecto).

    solo_bloque: reescribir únicamente el bloque generado, sin tocar trust ni
    .gitignore. Lo usa --sweep, que valida todo antes y promete cambiar solo
    credenciales.
    """
    pfx = "[dry-run] " if dry_run else ""
    if global_scope:
        destino = CODEX_CONFIG
        if not dry_run:
            os.makedirs(CODEX_HOME, exist_ok=True)
    else:
        destino = os.path.join(project_path, CODEX_PROJECT_DIR, "config.toml")
        if not solo_bloque:
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
    existentes = servidores_del_bloque(texto, destino)

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


# --------------------------------------------------------------------------
# Barrido (--sweep): re-registrar, en cada scope donde YA están, los MCPs de
# secrets.json. Es como correr --update proyecto por proyecto, pero leyendo y
# escribiendo ~/.claude.json UNA vez, sin agregar MCPs a ningún scope y sin
# tocar los que no salen de secrets.json (figma, blender, los registrados a mano).
# --------------------------------------------------------------------------

# Derivados de la única tabla (secret_refs.SECRET_FIELDS): un tipo nuevo con
# credenciales queda cubierto sin tocar esto.
_DESTINOS = [d for campos in secret_refs.SECRET_FIELDS.values() for d in campos.values()]
_FLAGS_CREDENCIAL = tuple(sorted({f"{flag}=" for _, flag in _DESTINOS if flag}))
_NOMBRES_CREDENCIAL = {var for var, _ in _DESTINOS}  # variables de entorno y headers


def sin_credenciales(cfg: dict) -> dict:
    """La entrada sin nada que tenga que ver con credenciales: ni valores, ni
    referencias, ni el lanzador. Si dos entradas difieren después de esto, el
    barrido cambia algo MÁS que las claves, y eso hay que mirarlo."""
    # Sirve para la forma de Claude y la de Codex (http_headers, env_vars...).
    out = {k: v for k, v in cfg.items()
           if k not in ("headers", "http_headers", "headersHelper", "http_headers_helper",
                        "env_vars", "args", "env")}
    for clave in ("headers", "http_headers"):
        resto = {k: v for k, v in cfg.get(clave, {}).items() if k not in _NOMBRES_CREDENCIAL}
        if resto:
            out[clave] = resto
    if "args" in cfg:
        args = list(cfg["args"])
        if args[:1] == [LAUNCHER] and "--" in args:
            args = args[args.index("--") + 1:]
        out["args"] = [a for a in args if not a.startswith(_FLAGS_CREDENCIAL)]
    # env vacío = sin env: Codex omite la tabla cuando no queda nada que declarar.
    env = {k: v for k, v in (cfg.get("env") or {}).items() if k not in _NOMBRES_CREDENCIAL}
    if env:
        out["env"] = env
    return out


def diferencias_ajenas(viejo: dict, nuevo: dict) -> list:
    """Qué cambia además de las credenciales, en texto seguro de mostrar."""
    a, b = sin_credenciales(viejo), sin_credenciales(nuevo)
    difs = []
    for k in sorted(set(a) | set(b)):
        if a.get(k) == b.get(k):
            continue
        if k == "args":
            quita = [x for x in a.get("args", []) if x not in b.get("args", [])]
            pone = [x for x in b.get("args", []) if x not in a.get("args", [])]
            difs.append(f"args -{quita} +{pone}" if quita or pone else "args (orden)")
        elif k == "env":
            difs.append(f"env {a.get('env')} → {b.get('env')}")
        else:
            difs.append(f"{k}: {a.get(k)!r} → {b.get(k)!r}")
    return difs


def scopes_claude(d: dict) -> list:
    """[(etiqueta, mcpServers)] de ~/.claude.json: el global y cada proyecto."""
    scopes = [("(global)", d.get("mcpServers") or {})]
    scopes += [(p, c.get("mcpServers") or {}) for p, c in sorted(d.get("projects", {}).items())]
    return scopes


def configs_codex(d_claude: dict) -> list:
    """[(proyecto | None, destino, texto)]: el config global de Codex y cada
    <proyecto>/.codex/config.toml con el bloque generado. Lee cada archivo una vez.

    El proyecto cuyo .codex/config.toml ES el global (el home, con CODEX_HOME por
    defecto) no se lista como proyecto: tratarlo como tal lo marcaría trusted entero.
    """
    def leer(ruta):
        try:
            with open(ruta, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None

    salida = []
    texto_global = leer(CODEX_CONFIG)
    if texto_global is not None:
        salida.append((None, CODEX_CONFIG, texto_global))
    proyectos = set(d_claude.get("projects", {}))
    if texto_global and tomllib is not None:
        proyectos |= set(parse_toml(texto_global, CODEX_CONFIG).get("projects", {}))
    global_real = os.path.realpath(CODEX_CONFIG)
    for p in sorted(proyectos):
        destino = os.path.join(p, CODEX_PROJECT_DIR, "config.toml")
        if os.path.realpath(destino) == global_real:
            continue
        texto = leer(destino)
        if texto and CODEX_MARK_BEGIN in texto:
            salida.append((p, destino, texto))
    return salida


def plan_sweep(servers: dict, nombres_secrets: set, target: str) -> dict:
    """Todo lo que haría el barrido, sin escribir nada."""
    plan = {"d": None, "claude": [], "codex": [], "ajenas": [], "ignorados": set(), "presentes": set()}
    d = read_json(CLAUDE_JSON)
    if target in ("claude", "both"):
        plan["d"] = d
        for etiqueta, existing in scopes_claude(d):
            # Contra TODO secrets.json, no contra lo filtrado por --only: si no, un
            # MCP de secrets.json fuera del filtro pasaría por "registrado a mano".
            plan["ignorados"] |= set(existing) - nombres_secrets
            plan["presentes"] |= set(existing) & servers.keys()
            cambian = sorted(n for n in set(existing) & servers.keys() if existing[n] != servers[n])
            for n in cambian:
                plan["ajenas"].extend(f"{etiqueta} · {n}: {dif}" for dif in diferencias_ajenas(existing[n], servers[n]))
            if cambian:
                plan["claude"].append((etiqueta, existing, cambian))
    if target in ("codex", "both"):
        for proyecto, destino, texto in configs_codex(d):
            # Todo lo que haría abortar a apply_codex se valida ACÁ, antes de que se
            # escriba nada: si no, el barrido podía quedar aplicado a medias.
            existentes = servidores_del_bloque(texto, destino)  # sale si falta el cierre
            antes, despues = strip_codex_block(texto)
            assert_sin_mcp_a_mano(antes + "\n" + despues, destino)
            if proyecto is not None:
                abortar_si_codex_trackeado(proyecto)
            etiqueta = "(codex global)" if proyecto is None else f"(codex) {proyecto}"
            plan["ignorados"] |= existentes.keys() - nombres_secrets
            plan["presentes"] |= existentes.keys() & servers.keys()
            cambian = {}
            for n in sorted(existentes.keys() & servers.keys()):
                nuevo = to_codex_server(servers[n])
                if existentes[n] != nuevo:
                    cambian[n] = servers[n]
                    plan["ajenas"].extend(f"{etiqueta} · {n}: {dif}"
                                          for dif in diferencias_ajenas(existentes[n], nuevo))
            if cambian:
                plan["codex"].append((proyecto, etiqueta, cambian))
    return plan


def sweep(servers: dict, servers_config: list, target: str, dry_run: bool, yes: bool = False) -> None:
    plan = plan_sweep(servers, {e["name"] for e in servers_config}, target)
    # Helpers de TODO MCP presente, no solo de los que cambian: uno borrado o con
    # rutas viejas (repo o venv movidos) se regenera aunque el config esté igual.
    pedidos = helpers_desactualizados(header_helpers_pedidos(servers_config, plan["presentes"]))
    pfx = "[dry-run] " if dry_run else ""

    if target in ("claude", "both"):
        print(f"{pfx}Claude Code: {CLAUDE_JSON}")
        for etiqueta, _, cambian in plan["claude"]:
            print(f"  {etiqueta}: {', '.join(cambian)}")
        print(f"  → {len(plan['claude'])} scope(s) con cambios.")
    if target in ("codex", "both"):
        print(f"{pfx}Codex:")
        for _, etiqueta, cambian in plan["codex"]:
            print(f"  {etiqueta}: {', '.join(sorted(cambian))}")
        print(f"  → {len(plan['codex'])} config(s) con cambios.")
    if pedidos:
        print(f"Helpers de headers a (re)generar: {', '.join(sorted(pedidos))}")
    if plan["ignorados"]:
        print(f"No salen de secrets.json, no se tocan: {', '.join(sorted(plan['ignorados']))}")
    if plan["ajenas"]:
        print()
        print("OJO: además de las credenciales, el barrido cambia esto:")
        for linea in plan["ajenas"]:
            print(f"  {linea}")
    else:
        print("Fuera de las credenciales, el barrido no cambia nada más.")
    print()

    if dry_run:
        print("dry-run: no se escribió nada.")
        return
    # Se frena ANTES de escribir: el aviso de arriba no sirve si ya se aplicó.
    if plan["ajenas"] and not yes:
        print("ERROR: hay cambios además de las credenciales. Revisalos arriba y, si están bien,")
        print("       volvé a correr con --yes. No se escribió nada.")
        sys.exit(1)

    escribir_header_helpers(pedidos, dry_run=False)  # antes que los configs que los nombran
    if plan["claude"]:
        for _, existing, cambian in plan["claude"]:
            for n in cambian:
                existing[n] = servers[n]
        write_json_atomic(CLAUDE_JSON, plan["d"])
    for proyecto, _, cambian in plan["codex"]:
        apply_codex(proyecto, cambian, update_mode=True, remove_mode=False, only_filter=None,
                    global_scope=proyecto is None, dry_run=False, solo_bloque=True)
    limpiar_header_helpers(dry_run=False)
    print("Listo. Reiniciá Claude Code (VSCode) y Codex para que los MCPs arranquen con el registro nuevo.")


def print_usage():
    print("Uso: python3 add-mcp-to-project.py /ruta/absoluta/al/proyecto [opciones]")
    print("     python3 add-mcp-to-project.py --global --only NOMBRE [opciones]")
    print()
    print("  --update           sobreescribe entradas existentes con los valores de secrets.json")
    print("  --remove           elimina los MCPs del proyecto (usar con --only para especificar cuáles)")
    print("  --only A,B,C       aplica la operación solo a esos MCPs")
    print("  --target T         claude (default) | codex | both")
    print("  --global           scope global en vez de por proyecto")
    print("  --sweep            re-registra, en TODOS los scopes donde ya están, los MCPs de")
    print("                     secrets.json (Claude y Codex salvo --target). No agrega ninguno.")
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
    sweep_mode = "--sweep" in sys.argv
    target = None
    only_filter = None
    args_clean = []
    i = 1
    while i < len(sys.argv):
        a = sys.argv[i]
        if a in ("--update", "--remove", "--global", "--dry-run", "--yes", "-y", "--sweep"):
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

    # El barrido es para dejar limpio todo: por defecto cubre Claude y Codex.
    target = target or ("both" if sweep_mode else "claude")
    if target not in ("claude", "codex", "both"):
        print(f"ERROR: --target inválido '{target}'. Opciones: claude | codex | both")
        sys.exit(1)
    if sweep_mode and (args_clean or global_scope or remove_mode):
        print("ERROR: --sweep recorre todos los scopes: no lleva ruta, --global ni --remove.")
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

    if not remove_mode:
        abortar_si_refs_invalidas(secrets["mcp_servers"], MCP_SERVERS.keys())
        avisar_texto_plano(secrets["mcp_servers"], MCP_SERVERS.keys())

    if sweep_mode:
        sweep(MCP_SERVERS, secrets["mcp_servers"], target, dry_run, yes=assume_yes)
        return

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

    if not remove_mode:
        # Antes que los configs que los nombran: un MCP no puede quedar apuntando
        # a un helper que todavía no existe.
        escribir_header_helpers(header_helpers_pedidos(secrets["mcp_servers"], MCP_SERVERS.keys()), dry_run)
    dispatch(dry_run)
    # En todo modo: un --update que vuelve a texto plano (o un --remove) deja de
    # usar un helper, y no tiene que quedar suelto.
    limpiar_header_helpers(dry_run)


if __name__ == "__main__":
    main()
