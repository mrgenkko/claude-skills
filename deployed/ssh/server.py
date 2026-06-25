#!/usr/bin/env python3
"""MCP server para control de servidores Ubuntu vía SSH.

Modos de `shell`:
- one-shot (sin `session`): conexión nueva por llamada, `exec_command`, sin estado entre
  llamadas. Idéntico al comportamiento histórico.
- sesión persistente (`session="nombre"`): el comando corre dentro de una sesión tmux
  server-side creada on-demand. El estado (cwd, env, venv) PERSISTE entre llamadas y los
  procesos largos sobreviven al timeout. tmux queda oculto tras el vocabulario propio
  (shell+session / sessions / end_session / interrupt_session).

Un watcher en background (reaper) apaga sesiones tras N segundos de INACTIVIDAD real
(pane en prompt de shell, sin comando corriendo) — un build largo nunca se mata porque su
output mantiene fresca la actividad de la sesión.
"""

import argparse
import asyncio
import hashlib
import os
import re
import shlex
import stat
import time
import uuid
import paramiko
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

parser = argparse.ArgumentParser()
parser.add_argument("--host", required=True)
parser.add_argument("--port", type=int, default=22)
parser.add_argument("--user", required=True)
parser.add_argument("--key-file", default=None)
parser.add_argument("--password", default=None)
parser.add_argument("--sudo-password", default=None)
parser.add_argument("--download-dir", default="/tmp")
parser.add_argument("--name", default=None)
# Sesiones persistentes: ON por defecto; opt-out por instancia (estilo --allow-flush de redis).
parser.add_argument("--forbid-sessions", dest="forbid_sessions", action="store_true",
                    help="Deshabilita las sesiones persistentes (shell one-shot sigue activo).")
# Watcher: apaga sesiones tras N segundos de inactividad real. 0 = watcher desactivado.
parser.add_argument("--session-idle-timeout", dest="session_idle_timeout", type=int, default=1800,
                    help="Segundos de inactividad tras los que el watcher apaga una sesión idle (0=off).")
args, _ = parser.parse_known_args()

SERVER_LABEL = args.name or args.host

# Umbral por encima del cual la salida deja de devolverse como texto y redirige a download_file.
READ_TEXT_LIMIT = 256 * 1024  # 256 KB

# Raíz de los temporales por-sesión en el server remoto (centinelas + salida capturada).
SESSION_BASE = "/tmp/.mcp-ssh"
# Nombres de sesión válidos: anti-inyección en tmux/shell.
_SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# Cadencia del polling del archivo-centinela (segundos).
POLL_INTERVAL = 0.3
# Comandos de pane que cuentan como "idle" (prompt de shell, sin proceso corriendo).
_IDLE_SHELLS = {"bash", "sh", "zsh", "fish", "dash", "ksh", "-bash", "-sh", "-zsh", "-fish"}

# Inyección de sudo en sesiones: reescribe cada `sudo` → `sudo -A` (usa SUDO_ASKPASS) salvo que
# el usuario ya haya puesto un flag explícito (-A/-S/-n/-k/-v) tras él. Word-boundary para no
# tocar `sudo` dentro de rutas/strings (`/usr/bin/sudo`, `pseudo`).
_SUDO_INJECT_RE = re.compile(r"\bsudo\b(?!\s+-[ASnkv]\b)")

# Prompts interactivos conocidos: si un comando de sesión queda colgado leyendo uno de estos,
# se reporta como "bloqueado esperando <X>" en vez del genérico "corriendo".
_PROMPT_PATTERNS = [
    (re.compile(r"\[sudo\] password for", re.I), "contraseña de sudo"),
    (re.compile(r"Enter passphrase for", re.I), "passphrase de clave SSH"),
    (re.compile(r"Are you sure you want to continue connecting", re.I), "confirmación de host-key SSH (yes/no)"),
    (re.compile(r"\(yes/no(?:/\[fingerprint\])?\)\??", re.I), "confirmación yes/no"),
    (re.compile(r"\[Y/n\]|\[y/N\]"), "confirmación [Y/n]"),
    (re.compile(r"(?:^|\n)\s*[Pp]assword:\s*$"), "contraseña"),
]


def _inject_askpass_sudo(command: str) -> str:
    """Reescribe los `sudo` del comando a `sudo -A` para que tomen la contraseña vía SUDO_ASKPASS."""
    return _SUDO_INJECT_RE.sub("sudo -A", command)


def _detect_prompt(pane: str) -> str | None:
    """Si las últimas líneas del pane muestran un prompt interactivo conocido, devuelve su etiqueta."""
    tail = "\n".join([ln for ln in pane.splitlines() if ln.strip()][-5:])
    for rx, label in _PROMPT_PATTERNS:
        if rx.search(tail):
            return label
    return None

SESSIONS_DISABLED_MSG = (
    f"Las sesiones persistentes están deshabilitadas en {SERVER_LABEL}. "
    "Re-registrá la instancia con allow_sessions:true (sin --forbid-sessions) en secrets.json "
    "para habilitarlas. El modo one-shot de shell sigue disponible."
)

app = Server(f"ssh-{SERVER_LABEL}")

# Metadata en memoria de las sesiones que conoce ESTE proceso (sobrevive entre tool calls;
# NO entre reinicios del MCP — tmux es la verdad de liveness). Solo enriquece el listado.
_sessions: dict[str, dict] = {}
# Lock por sesión: serializa comandos dentro de una misma sesión (dos shell(session="x")
# concurrentes no deben intercalar send-keys en el mismo pane). Entre sesiones, sin lock.
_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


class SessionError(Exception):
    """Error de sesión con mensaje listo para el usuario (sin prefijo 'Error SSH:')."""


def _text(s: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=s)]


def _sha256(path: str) -> str:
    """sha256 de un archivo local leyendo en bloques (no carga todo en RAM)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _remote_sha256(client: paramiko.SSHClient, remote_path: str) -> str | None:
    """sha256 del archivo remoto vía `sha256sum`; None si no se pudo calcular."""
    _, stdout, _ = client.exec_command(f"sha256sum {shlex.quote(remote_path)}", timeout=120)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    if stdout.channel.recv_exit_status() != 0 or not out:
        return None
    return out.split()[0]


def _connect() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = dict(hostname=args.host, port=args.port, username=args.user, timeout=30)
    if args.key_file:
        kwargs["key_filename"] = args.key_file
    elif args.password:
        kwargs["password"] = args.password
    client.connect(**kwargs)
    return client


# ─────────────────────────── Sesiones persistentes ───────────────────────────

def _valid_session(name) -> bool:
    return isinstance(name, str) and bool(_SESSION_NAME_RE.match(name))


async def _get_lock(session: str) -> asyncio.Lock:
    async with _locks_guard:
        if session not in _locks:
            _locks[session] = asyncio.Lock()
        return _locks[session]


def _touch_session(session: str, command: str) -> None:
    meta = _sessions.setdefault(session, {})
    meta.setdefault("created", time.time())
    meta["last_used"] = time.time()
    first = command.strip().splitlines()[0] if command.strip() else ""
    meta["last_command"] = (first[:60] + "…") if len(first) > 60 else first


def _forget_session(session: str) -> None:
    _sessions.pop(session, None)
    _locks.pop(session, None)


def _read_remote_text(sftp, path: str) -> str:
    """Lee un archivo de texto remoto con el guard de tamaño de read_file."""
    try:
        size = sftp.stat(path).st_size
    except IOError:
        return ""
    if size > READ_TEXT_LIMIT:
        return (f"(salida de {size} bytes — excede el límite de {READ_TEXT_LIMIT}. "
                f"Redirigí la salida a un archivo dentro del comando y traelo con download_file.)")
    with sftp.open(path, "r") as f:
        return f.read().decode("utf-8", errors="replace")


def _session_run_blocking(session: str, command: str, timeout: int):
    """Ejecuta `command` en la sesión tmux preservando estado, captura stdout/stderr+exit code.

    Patrón source + archivo-centinela: el comando se escribe VERBATIM a un .sh (sin escaping)
    y se `source`-ea en la shell viva (el brace-group corre en la shell actual → cwd/env
    persisten). La salida va a .out y el exit code a .rc; se hace polling de .rc.
    Bloqueante: invocar vía asyncio.to_thread.
    Devuelve (output, exit_code|None, timed_out, blocked_on|None) — blocked_on es la etiqueta del
    prompt interactivo si el comando quedó colgado leyéndolo (sudo, passphrase, yes/no…), o None.
    """
    sdir = f"{SESSION_BASE}/{session}"
    cid = uuid.uuid4().hex
    cmd_path = f"{sdir}/{cid}.sh"
    out_path = f"{sdir}/{cid}.out"
    rc_path = f"{sdir}/{cid}.rc"
    # Askpass por-sesión: helper estático que `cat`-ea un archivo de contraseña 0600 (la contraseña
    # nunca pasa por la línea de comando ni por el pane; los archivos se borran al matar la sesión).
    askpass_path = f"{sdir}/.askpass"
    pass_path = f"{sdir}/.sudopass"

    client = _connect()
    try:
        # ¿tmux disponible? (no auto-instalar; surface con hint)
        _, so, _ = client.exec_command("command -v tmux >/dev/null 2>&1 && echo ok")
        if so.read().decode("utf-8", errors="replace").strip() != "ok":
            raise SessionError(
                "tmux no está instalado en el servidor remoto; es necesario para las sesiones "
                "persistentes. Instalalo con: sudo apt install tmux (el shell one-shot no lo requiere)."
            )

        # Asegurar la sesión (creación on-demand) + el dir de temporales (0700: protege la contraseña).
        setup = (
            f"tmux has-session -t {shlex.quote(session)} 2>/dev/null || "
            f"tmux new-session -d -s {shlex.quote(session)}; "
            f"mkdir -p {shlex.quote(sdir)} && chmod 700 {shlex.quote(sdir)}"
        )
        _, so, _ = client.exec_command(setup)
        so.channel.recv_exit_status()

        sftp = client.open_sftp()

        # sudo en sesión: igual que en one-shot, el MCP inyecta la contraseña. Se hace vía
        # SUDO_ASKPASS (no por stdin, que no controlamos en la shell sourced): un helper lee un
        # archivo de contraseña 0600 y cada `sudo` se reescribe a `sudo -A`.
        prologue = ""
        run_command = command
        if args.sudo_password:
            with sftp.open(pass_path, "w") as f:
                f.write((args.sudo_password + "\n").encode("utf-8"))
            sftp.chmod(pass_path, 0o600)
            askpass_body = f"#!/bin/sh\nexec cat {shlex.quote(pass_path)}\n"
            with sftp.open(askpass_path, "w") as f:
                f.write(askpass_body.encode("utf-8"))
            sftp.chmod(askpass_path, 0o700)
            prologue = f"export SUDO_ASKPASS={shlex.quote(askpass_path)}\n"
            run_command = _inject_askpass_sudo(command)

        # Comando del usuario VERBATIM (sin escaping): solo nuestras rutas se quotean.
        wrapper = (
            prologue
            + "{\n"
            + run_command
            + "\n} > " + shlex.quote(out_path) + " 2>&1\n"
            + "echo $? > " + shlex.quote(rc_path) + "\n"
        )
        with sftp.open(cmd_path, "w") as f:
            f.write(wrapper.encode("utf-8"))

        # Teclear el `source` en la shell viva (-l = literal; la línea solo tiene rutas nuestras).
        source_line = f"source {shlex.quote(cmd_path)}"
        send = (
            f"tmux send-keys -t {shlex.quote(session)} -l {shlex.quote(source_line)} && "
            f"tmux send-keys -t {shlex.quote(session)} Enter"
        )
        _, so, _ = client.exec_command(send)
        so.channel.recv_exit_status()

        # Polling del centinela .rc hasta completar o agotar timeout.
        deadline = time.monotonic() + max(1, timeout)
        completed = False
        while time.monotonic() < deadline:
            try:
                sftp.stat(rc_path)
                completed = True
                break
            except IOError:
                time.sleep(POLL_INTERVAL)

        output = _read_remote_text(sftp, out_path)
        exit_code = None
        blocked_on = None
        if completed:
            rc_txt = _read_remote_text(sftp, rc_path).strip()
            try:
                exit_code = int(rc_txt)
            except ValueError:
                exit_code = None
            for p in (cmd_path, out_path, rc_path):  # limpiar temporales del comando
                try:
                    sftp.remove(p)
                except IOError:
                    pass
        else:
            # No completó: ¿está colgado leyendo un prompt interactivo? (sudo, passphrase, yes/no…)
            # El prompt va a la tty del pane —no al .out redirigido—, así que se lee con capture-pane.
            _, so, _ = client.exec_command(
                f"tmux capture-pane -p -t {shlex.quote(session)} 2>/dev/null")
            pane = so.read().decode("utf-8", errors="replace")
            blocked_on = _detect_prompt(pane)
            if blocked_on and not output.strip():
                output = "\n".join([ln for ln in pane.splitlines() if ln.strip()][-3:])
        sftp.close()
        return output, exit_code, (not completed), blocked_on
    finally:
        client.close()


def _session_list_blocking() -> str:
    client = _connect()
    try:
        fmt = "#{session_name}\t#{session_created}\t#{session_activity}\t#{pane_current_command}"
        _, so, _ = client.exec_command(f"tmux ls -F {shlex.quote(fmt)} 2>/dev/null")
        out = so.read().decode("utf-8", errors="replace").strip()
        return out if so.channel.recv_exit_status() == 0 else ""
    finally:
        client.close()


def _session_kill_blocking(session: str) -> None:
    sdir = f"{SESSION_BASE}/{session}"
    client = _connect()
    try:
        cmd = (f"tmux kill-session -t {shlex.quote(session)} 2>/dev/null; "
               f"rm -rf {shlex.quote(sdir)}")
        _, so, _ = client.exec_command(cmd)
        so.channel.recv_exit_status()
    finally:
        client.close()


def _session_interrupt_blocking(session: str) -> None:
    client = _connect()
    try:
        _, so, _ = client.exec_command(
            f"tmux has-session -t {shlex.quote(session)} 2>/dev/null && echo ok")
        if so.read().decode("utf-8", errors="replace").strip() != "ok":
            raise SessionError(f"No existe la sesión '{session}'.")
        _, so, _ = client.exec_command(f"tmux send-keys -t {shlex.quote(session)} C-c")
        so.channel.recv_exit_status()
    finally:
        client.close()


def _format_sessions(raw: str) -> str:
    if not raw:
        return "(no hay sesiones persistentes activas)"
    now = time.time()
    lines = [f"sesiones persistentes en {SERVER_LABEL}:"]
    for line in raw.splitlines():
        parts = line.split("\t")
        sname = parts[0]
        created = parts[1] if len(parts) > 1 else ""
        activity = parts[2] if len(parts) > 2 else ""
        pane_cmd = parts[3] if len(parts) > 3 else ""
        busy = "idle" if pane_cmd in _IDLE_SHELLS else f"corriendo:{pane_cmd}"
        try:
            idle_s = int(now - int(activity))
            idle_str = f" · inactiva {idle_s}s"
        except ValueError:
            idle_str = ""
        try:
            created_str = " · creada " + time.strftime("%Y-%m-%d %H:%M", time.localtime(int(created)))
        except ValueError:
            created_str = ""
        last_cmd = _sessions.get(sname, {}).get("last_command")
        last_str = f" · último: {last_cmd}" if last_cmd else ""
        lines.append(f"  {sname}  [{busy}]{idle_str}{created_str}{last_str}")
    return "\n".join(lines)


async def _handle_session_tool(name: str, arguments: dict, session):
    if name == "shell":
        command = arguments["command"]
        timeout = arguments.get("timeout", 60)
        if not _valid_session(session):
            raise SessionError(
                f"Nombre de sesión inválido: {session!r}. Permitido: letras, dígitos, '.', '_', '-' (1-64).")
        lock = await _get_lock(session)
        async with lock:
            output, exit_code, timed_out, blocked_on = await asyncio.to_thread(
                _session_run_blocking, session, command, timeout)
        _touch_session(session, command)
        if blocked_on:
            ctx = f"\n{output.strip()}" if output.strip() else ""
            note = (f"\n\n[la sesión '{session}' está BLOQUEADA esperando input interactivo: {blocked_on}. "
                    f"No va a avanzar sola. Cortá con interrupt_session('{session}') y reintentá sin el prompt: "
                    f"si es sudo, registrá --sudo-password en el MCP o configurá NOPASSWD; si es apt, agregá -y; "
                    f"si es host-key SSH, pre-aceptá la key (ssh-keyscan).]")
            return _text(f"[bloqueado · esperando {blocked_on} · sesión '{session}']{ctx}{note}")
        if timed_out:
            body = output.strip() or "(sin salida todavía)"
            note = (f"\n\n[el comando sigue corriendo en la sesión '{session}'. "
                    f"Volvé con shell(session='{session}', …) para seguir, "
                    f"interrupt_session('{session}') para cortarlo, o end_session('{session}') para terminarla.]")
            return _text(f"[corriendo · sesión '{session}']\n{body}{note}")
        if exit_code not in (0, None):
            return _text(f"[exit {exit_code} · sesión '{session}']\n{output.strip() or '(sin output)'}")
        return _text(output.strip() or "(sin output)")

    if name == "sessions":
        raw = await asyncio.to_thread(_session_list_blocking)
        return _text(_format_sessions(raw))

    if name == "end_session":
        session = arguments["session"]
        if not _valid_session(session):
            raise SessionError(f"Nombre de sesión inválido: {session!r}.")
        await asyncio.to_thread(_session_kill_blocking, session)
        _forget_session(session)
        return _text(f"Sesión '{session}' terminada y sus temporales limpiados.")

    if name == "interrupt_session":
        session = arguments["session"]
        if not _valid_session(session):
            raise SessionError(f"Nombre de sesión inválido: {session!r}.")
        await asyncio.to_thread(_session_interrupt_blocking, session)
        _touch_session(session, "<interrupt>")
        return _text(f"Ctrl-C enviado a la sesión '{session}' (la sesión sigue viva).")

    raise SessionError(f"Tool de sesión desconocido: {name}")


# ─────────────────────── Watcher: apaga sesiones idle ────────────────────────

def _reap_idle_sessions() -> None:
    """Apaga sesiones gestionadas por nosotros que llevan > timeout INACTIVAS.

    Inactiva = pane en prompt de shell (sin proceso corriendo) Y sin actividad en el pane
    durante > timeout segundos. Un build largo produce output → actividad fresca → NO se mata.
    Solo toca sesiones nuestras (las que tienen dir bajo SESSION_BASE), nunca sesiones tmux
    creadas a mano por el usuario. También limpia dirs huérfanos (sesión muerta, dir colgado).
    Bloqueante: invocar vía asyncio.to_thread.
    """
    timeout = args.session_idle_timeout
    if timeout <= 0:
        return
    client = _connect()
    try:
        _, so, _ = client.exec_command(f"ls -1 {shlex.quote(SESSION_BASE)} 2>/dev/null")
        managed = [d for d in so.read().decode("utf-8", errors="replace").split() if d]
        now = time.time()
        for sname in managed:
            if not _valid_session(sname):
                continue
            sdir = f"{SESSION_BASE}/{sname}"
            fmt = "#{session_activity}\t#{pane_current_command}"
            _, so, _ = client.exec_command(
                f"tmux display-message -p -t {shlex.quote(sname)} {shlex.quote(fmt)} 2>/dev/null")
            info = so.read().decode("utf-8", errors="replace").strip()
            if so.channel.recv_exit_status() != 0 or not info:
                # Sesión ya no existe en tmux pero quedó el dir → limpiar huérfano.
                client.exec_command(f"rm -rf {shlex.quote(sdir)}")[1].channel.recv_exit_status()
                _forget_session(sname)
                continue
            parts = info.split("\t")
            try:
                idle = now - int(parts[0])
            except (ValueError, IndexError):
                continue
            pane_cmd = parts[1] if len(parts) > 1 else ""
            if pane_cmd in _IDLE_SHELLS and idle > timeout:
                client.exec_command(
                    f"tmux kill-session -t {shlex.quote(sname)} 2>/dev/null; "
                    f"rm -rf {shlex.quote(sdir)}")[1].channel.recv_exit_status()
                _forget_session(sname)
    finally:
        client.close()


async def _reaper_loop() -> None:
    interval = max(15, min(60, args.session_idle_timeout))
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(_reap_idle_sessions)
        except Exception:
            pass  # el watcher nunca tumba el server


# ──────────────────────────────── Tools ─────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    sessions_on = not args.forbid_sessions
    shell_desc = (
        f"Ejecuta un comando de shell en {SERVER_LABEL} vía SSH. Los comandos con sudo se manejan "
        "automáticamente: el servidor inyecta la contraseña vía stdin (sudo -S)."
    )
    shell_props = {
        "command": {"type": "string", "description": "Comando bash a ejecutar"},
        "timeout": {"type": "integer", "default": 60, "description": "Timeout en segundos"},
    }
    if sessions_on:
        shell_desc += (
            " Pasá `session` (un nombre, ej. \"deploy\") para correrlo en una SESIÓN PERSISTENTE "
            "server-side: el estado (cwd, export, venv) PERSISTE entre llamadas y los procesos largos "
            "sobreviven al timeout (al expirar devuelve la salida parcial sin matar la sesión). sudo "
            "funciona igual que en one-shot (el MCP inyecta la contraseña, también dentro de la sesión). "
            "La sesión se crea sola la primera vez. Sin `session`, es one-shot sin estado. Las "
            f"sesiones idle se apagan solas tras {args.session_idle_timeout}s de inactividad."
        )
        shell_props["session"] = {
            "type": "string",
            "description": "Nombre de la sesión persistente ([A-Za-z0-9_.-], 1-64). Si se omite, one-shot.",
        }

    tools = [
        types.Tool(
            name="shell",
            description=shell_desc,
            inputSchema={"type": "object", "properties": shell_props, "required": ["command"]},
        ),
        types.Tool(
            name="read_file",
            description=f"Lee el contenido de un archivo de texto en {SERVER_LABEL}. Solo para texto: si el archivo es binario o grande (>256 KB) la herramienta redirige a download_file en vez de devolver el contenido.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Ruta absoluta del archivo"},
                },
                "required": ["path"],
            },
        ),
        types.Tool(
            name="write_file",
            description=f"Escribe contenido de texto a un archivo en {SERVER_LABEL}. Solo para texto. Para binarios o archivos grandes usá upload_file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Ruta absoluta del archivo"},
                    "content": {"type": "string", "description": "Contenido a escribir"},
                },
                "required": ["path", "content"],
            },
        ),
        types.Tool(
            name="download_file",
            description=f"Descarga un archivo desde {SERVER_LABEL} a la máquina local vía SFTP (disco-a-disco). Es la vía correcta para CUALQUIER archivo —incluidos binarios de decenas de MB—: los bytes no pasan por el chat, solo se devuelve la ruta local y metadata. Usar esto en vez de read_file/scp para traer binarios.",
            inputSchema={
                "type": "object",
                "properties": {
                    "remote_path": {"type": "string", "description": "Ruta absoluta del archivo en el servidor remoto"},
                    "local_path": {"type": "string", "description": f"Ruta local destino. Si se omite, se usa <download-dir>/<nombre> (download-dir actual: {args.download_dir})"},
                    "verify": {"type": "boolean", "default": False, "description": "Si true, compara el sha256 local contra el sha256sum remoto y reporta verified"},
                },
                "required": ["remote_path"],
            },
        ),
        types.Tool(
            name="upload_file",
            description=f"Sube un archivo local a {SERVER_LABEL} vía SFTP (disco-a-disco). Es la vía correcta para CUALQUIER archivo —incluidos binarios de decenas de MB—: los bytes no pasan por el chat. Crea el directorio remoto destino si no existe. Usar esto en vez de write_file/scp para subir binarios.",
            inputSchema={
                "type": "object",
                "properties": {
                    "local_path": {"type": "string", "description": "Ruta del archivo local a subir"},
                    "remote_path": {"type": "string", "description": "Ruta absoluta destino en el servidor remoto"},
                    "verify": {"type": "boolean", "default": False, "description": "Si true, compara el sha256 local contra el sha256sum remoto y reporta verified"},
                },
                "required": ["local_path", "remote_path"],
            },
        ),
        types.Tool(
            name="list_dir",
            description=f"Lista el contenido de un directorio en {SERVER_LABEL}.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Ruta del directorio", "default": "/"},
                },
            },
        ),
        types.Tool(
            name="server_info",
            description=(
                f"Resumen del entorno de {SERVER_LABEL} en una llamada (discovery): usuario, hostname, "
                "home, si sudo está disponible (NOPASSWD / contraseña inyectada / sin acceso), si docker "
                "es accesible (con o sin sudo) y si tmux está instalado (sesiones persistentes). Llamalo "
                "al empezar a trabajar en el server para no descubrir el entorno a fuerza de prueba y error."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
    ]

    if sessions_on:
        tools += [
            types.Tool(
                name="sessions",
                description=f"Lista las sesiones persistentes activas en {SERVER_LABEL} (nombre, idle/corriendo, inactividad, creación, último comando). Las sesiones idle se apagan solas tras {args.session_idle_timeout}s de inactividad.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="end_session",
                description=f"Termina una sesión persistente en {SERVER_LABEL} (mata el proceso y limpia sus temporales). Usar para cerrar explícitamente una sesión y todo lo que esté corriendo en ella.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {"type": "string", "description": "Nombre de la sesión a terminar"},
                    },
                    "required": ["session"],
                },
            ),
            types.Tool(
                name="interrupt_session",
                description=f"Envía Ctrl-C al comando que corre en una sesión persistente de {SERVER_LABEL} SIN terminar la sesión (para cortar un comando colgado/runaway y reusar la sesión).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session": {"type": "string", "description": "Nombre de la sesión a interrumpir"},
                    },
                    "required": ["session"],
                },
            ),
        ]

    return tools


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    # ── Sesiones persistentes: conexión propia en thread (no bloquea el event loop) ──
    session = arguments.get("session") if name == "shell" else None
    is_session_tool = name in ("sessions", "end_session", "interrupt_session") or session is not None
    if is_session_tool:
        if args.forbid_sessions:
            return _text(SESSIONS_DISABLED_MSG)
        try:
            return await _handle_session_tool(name, arguments, session)
        except SessionError as e:
            return _text(str(e))
        except Exception as e:
            return _text(f"Error SSH (sesión): {e}")

    # ── One-shot / archivos: comportamiento histórico (conexión por llamada) ──
    try:
        client = _connect()

        if name == "shell":
            timeout = arguments.get("timeout", 60)
            command = arguments["command"]
            if args.sudo_password and "sudo" in command and "sudo -S" not in command:
                command = command.replace("sudo", "sudo -S", 1)
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            if args.sudo_password and "sudo -S" in command:
                stdin.write(args.sudo_password + "\n")
                stdin.flush()
            out = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()
            exit_code = stdout.channel.recv_exit_status()
            if exit_code != 0:
                output = f"[exit {exit_code}]\n{err or out}"
            else:
                output = out or err or "(sin output)"

        elif name == "read_file":
            path = arguments["path"]
            sftp = client.open_sftp()
            size = sftp.stat(path).st_size
            if size > READ_TEXT_LIMIT:
                sftp.close()
                output = (
                    f"Archivo demasiado grande para texto ({size} bytes). "
                    f"Usá download_file(remote_path={path!r}) para traerlo a disco sin pasarlo por el contexto."
                )
            else:
                with sftp.open(path, "r") as f:
                    raw = f.read()
                sftp.close()
                if b"\x00" in raw:
                    output = (
                        f"El archivo parece binario ({size} bytes). "
                        f"Usá download_file(remote_path={path!r}) para traerlo a disco sin pasarlo por el contexto."
                    )
                else:
                    output = raw.decode("utf-8", errors="replace")

        elif name == "download_file":
            remote_path = arguments["remote_path"]
            local_path = arguments.get("local_path") or os.path.join(
                args.download_dir, os.path.basename(remote_path.rstrip("/"))
            )
            local_path = os.path.abspath(os.path.expanduser(local_path))
            os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
            sftp = client.open_sftp()
            sftp.get(remote_path, local_path)
            sftp.close()
            size = os.path.getsize(local_path)
            sha = _sha256(local_path)
            lines = [
                f"Descargado: {remote_path} → {local_path}",
                f"bytes: {size}",
                f"sha256: {sha}",
            ]
            if arguments.get("verify"):
                remote_sha = _remote_sha256(client, remote_path)
                if remote_sha is None:
                    lines.append("verified: desconocido (no se pudo calcular sha256sum remoto)")
                else:
                    lines.append(f"verified: {str(remote_sha == sha).lower()} (remoto {remote_sha})")
            output = "\n".join(lines)

        elif name == "upload_file":
            local_path = os.path.abspath(os.path.expanduser(arguments["local_path"]))
            remote_path = arguments["remote_path"]
            if not os.path.isfile(local_path):
                raise FileNotFoundError(f"No existe el archivo local: {local_path}")
            remote_dir = os.path.dirname(remote_path.rstrip("/"))
            if remote_dir:
                _, mk_out, _ = client.exec_command(f"mkdir -p {shlex.quote(remote_dir)}")
                mk_out.channel.recv_exit_status()  # esperar a que mkdir termine
            sftp = client.open_sftp()
            sftp.put(local_path, remote_path)
            sftp.close()
            size = os.path.getsize(local_path)
            sha = _sha256(local_path)
            lines = [
                f"Subido: {local_path} → {remote_path}",
                f"bytes: {size}",
                f"sha256: {sha}",
            ]
            if arguments.get("verify"):
                remote_sha = _remote_sha256(client, remote_path)
                if remote_sha is None:
                    lines.append("verified: desconocido (no se pudo calcular sha256sum remoto)")
                else:
                    lines.append(f"verified: {str(remote_sha == sha).lower()} (remoto {remote_sha})")
            output = "\n".join(lines)

        elif name == "write_file":
            sftp = client.open_sftp()
            with sftp.open(arguments["path"], "w") as f:
                f.write(arguments["content"].encode("utf-8"))
            sftp.close()
            output = f"Archivo escrito: {arguments['path']}"

        elif name == "list_dir":
            path = arguments.get("path", "/")
            sftp = client.open_sftp()
            entries = sftp.listdir_attr(path)
            sftp.close()
            lines = []
            for e in sorted(entries, key=lambda x: x.filename):
                prefix = "d" if stat.S_ISDIR(e.st_mode) else "-"
                lines.append(f"{prefix}  {e.filename}")
            output = "\n".join(lines) or "(directorio vacío)"

        elif name == "server_info":
            info_script = (
                'printf "usuario:  %s\\n" "$(whoami)"; '
                'printf "hostname: %s\\n" "$(hostname)"; '
                'printf "home:     %s\\n" "$HOME"; '
                'command -v tmux >/dev/null 2>&1 '
                '&& echo "tmux:     instalado (sesiones persistentes OK)" '
                '|| echo "tmux:     AUSENTE (sin sesiones persistentes: sudo apt install tmux)"; '
                'if command -v docker >/dev/null 2>&1; then '
                '  if docker ps >/dev/null 2>&1; then echo "docker:   accesible SIN sudo"; '
                '  else echo "docker:   instalado, requiere sudo (probá: sudo docker …)"; fi; '
                'else echo "docker:   no instalado"; fi'
            )
            _, stdout, _ = client.exec_command(info_script, timeout=30)
            info = stdout.read().decode("utf-8", errors="replace").strip()
            if args.sudo_password:
                sudo_line = "sudo:     contraseña configurada — el MCP la inyecta solo (one-shot y sesiones)"
            else:
                _, so, _ = client.exec_command("sudo -n true 2>/dev/null && echo yes || echo no")
                ok = so.read().decode("utf-8", errors="replace").strip() == "yes"
                sudo_line = ("sudo:     NOPASSWD (no pide contraseña)" if ok else
                             "sudo:     pide contraseña y NO hay --sudo-password en este MCP (los sudo se colgarán)")
            output = f"Entorno de {SERVER_LABEL}:\n{info}\n{sudo_line}"

        else:
            output = f"Tool desconocido: {name}"

        client.close()

    except Exception as e:
        output = f"Error SSH: {e}"

    return [types.TextContent(type="text", text=output)]


async def main():
    # Watcher en background: apaga sesiones idle (salvo que las sesiones estén deshabilitadas).
    if not args.forbid_sessions and args.session_idle_timeout > 0:
        asyncio.create_task(_reaper_loop())
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
