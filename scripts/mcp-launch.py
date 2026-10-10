#!/usr/bin/env python3
"""
Lanzador de los MCP: lee sus claves del llavero del SO al arrancar.

Modo stdio (servers locales), lo que registra add-mcp-to-project.py:
    mcp-launch.py [--timeout S] --secret VAR=keyring:<id> [--secret ...] -- <server.py> [args...]

    Pone cada valor en el entorno (VAR) y reemplaza este proceso por
    `python <server.py> args` con os.execv: mismo PID —el cliente MCP sigue
    hablando con el mismo proceso— y un cmdline limpio, sin ids ni valores.

Modo headers (MCP remotos: headersHelper de Claude Code, http_headers_helper de Codex):
    mcp-launch.py headers [--timeout S] HEADER=keyring:<id> [...]

    Imprime {"HEADER": "<valor>"} en stdout y nada más.

Si una clave no se puede leer, sale con exit 1 y un motivo en stderr. Nunca hay
respaldo en texto plano. Ver pendientes/llavero-fase0.md (por qué el timeout y el
lock) y pendientes/llavero-fase2.md.

Vive en scripts/ y no en ~/.claude/mcp-servers/: usa secret_refs.py y el venv
del repo, del que los MCP registrados ya dependen.
"""

import json
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import secret_refs as refs  # noqa: E402

# Por debajo del arranque que tolera el cliente (Codex: 30 s, lo fija
# add-mcp-to-project.py), para que gane nuestro mensaje y no un timeout genérico.
TIMEOUT_STDIO = 25.0
# headersHelper de Claude Code corre con un tope de 10 s.
TIMEOUT_HEADERS = 8.0


class Uso(Exception):
    pass


def leer_llavero(rid):
    import keyring  # recién acá: los tests inyectan su propio lector
    return keyring.get_password(refs.SERVICE, rid)


def _ruta_lock():
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return os.path.join(base, "mrgenkko-mcp-launch.lock")


def _con_lock(fn):
    """Una lectura del llavero a la vez entre todos los lanzadores.

    Con el llavero bloqueado, N MCPs arrancando juntos abrían N ventanas de
    desbloqueo apiladas (fase 0). Serializado, sale una sola; los demás esperan y
    leen cuando el llavero ya está abierto. Windows no pide desbloqueo: sin lock.

    Si el lock no se puede tomar (p. ej. un /tmp/...lock de otro usuario cuando
    falta XDG_RUNTIME_DIR), se lee sin él: el lock solo evita ventanas apiladas,
    no protege nada, y no puede ser la razón de que un MCP no arranque.
    """
    if os.name == "nt":
        return fn()
    import fcntl
    try:
        fd = os.open(_ruta_lock(), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return fn()
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            pass
        return fn()
    finally:
        os.close(fd)  # libera el flock


def resolver(pares, leer, timeout):
    """[(nombre, 'keyring:<id>')] → {nombre: valor}, o Uso/RuntimeError con el motivo.

    Todo corre en un hilo con tope: con el llavero bloqueado y nadie frente a la
    pantalla, keyring espera la ventana de desbloqueo PARA SIEMPRE (fase 0). El
    tope cubre también la espera del lock.
    """
    resultado, error = {}, []

    def trabajo():
        for nombre, ref in pares:
            rid = refs.ref_id(ref)
            try:
                valor = leer(rid)
            except Exception as exc:  # noqa: BLE001 — se traduce, nunca se muestra el texto
                error.append(refs.explain(exc, rid))
                return
            if valor is None:
                error.append(f"keyring:{rid} no existe. Cargala con: mcp-secrets.py set {rid}")
                return
            resultado[nombre] = valor

    def en_hilo():
        try:
            _con_lock(trabajo)
        except BaseException as exc:  # noqa: BLE001 — que nada muera en silencio en el hilo
            error.append(f"falla inesperada leyendo el llavero ({type(exc).__name__}).")

    hilo = threading.Thread(target=en_hilo, daemon=True)
    hilo.start()
    hilo.join(timeout)
    if hilo.is_alive():
        raise RuntimeError(f"nadie desbloqueó el llavero en {timeout:g} s (¿pantalla bloqueada o desatendida?). "
                           f"Desbloqueá «Inicio de sesión» en Contraseñas y claves y reconectá el MCP.")
    if error:
        raise RuntimeError(error[0])
    # Última defensa: nunca lanzar un server con una clave de menos. Un gh sin
    # GH_TOKEN usaría la cuenta logueada en hosts.yml —la deriva que esto evita—.
    faltan = [nombre for nombre, _ in pares if nombre not in resultado]
    if faltan:
        raise RuntimeError(f"no se resolvieron todas las claves (faltan: {', '.join(faltan)}).")
    return resultado


def _par(spec):
    nombre, sep, ref = spec.partition("=")
    if not sep or not nombre or not refs.is_ref(ref) or not refs.ref_id(ref):
        raise Uso(f"se esperaba NOMBRE=keyring:<id>, llegó: {spec!r}")
    return nombre, ref


def parsear(argv):
    """→ (modo, timeout, pares, resto)."""
    modo = "headers" if argv[:1] == ["headers"] else "stdio"
    args = argv[1:] if modo == "headers" else list(argv)
    timeout = TIMEOUT_HEADERS if modo == "headers" else TIMEOUT_STDIO
    pares, resto = [], []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            resto = args[i + 1:]
            break
        if a == "--timeout":
            try:
                timeout = float(args[i + 1])
            except (IndexError, ValueError):
                raise Uso("--timeout lleva un número de segundos")
            i += 2
            continue
        if modo == "stdio":
            if a != "--secret" or i + 1 >= len(args):
                raise Uso(f"argumento inesperado: {a!r}")
            pares.append(_par(args[i + 1]))
            i += 2
        else:
            pares.append(_par(a))
            i += 1
    if modo == "stdio" and not resto:
        raise Uso("falta `-- <server.py> [args...]`")
    if not pares:
        raise Uso("no hay ninguna clave que leer")
    return modo, timeout, pares, resto


def lanzar(resto, env_extra):
    """Reemplaza este proceso por el server. En Windows execv no conserva el
    proceso para el cliente: se corre como hijo con la E/S heredada."""
    os.environ.update(env_extra)
    argv = [sys.executable, *resto]
    if os.name == "nt":
        import subprocess
        sys.exit(subprocess.call(argv))
    os.execv(sys.executable, argv)


def main(argv=None, leer=leer_llavero, exec_fn=lanzar, out=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    out = out or sys.stdout
    try:
        modo, timeout, pares, resto = parsear(argv)
        valores = resolver(pares, leer, timeout)
    except Uso as exc:
        print(f"mcp-launch: {exc}\n\n{__doc__.strip().split(chr(10) + chr(10))[1]}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"mcp-launch: {exc}", file=sys.stderr)
        return 1
    if modo == "headers":
        out.write(json.dumps(valores))
        out.flush()
        return 0
    return exec_fn(resto, valores)


if __name__ == "__main__":
    sys.exit(main())
