#!/usr/bin/env python3
"""MCP server para Modal (modal.com): apps, endpoints de inferencia, volúmenes, logs y costo.

Multi-workspace sin deriva: cada instancia recibe su propio token por env
(MODAL_TOKEN_ID/MODAL_TOKEN_SECRET). La resolución de config de Modal es
`env var MODAL_<KEY>` > perfil de `~/.modal.toml` > default, así que con el token
en el env la CLI queda *stateless*: no lee ni escribe el perfil activo y el
workspace no puede derivar entre instancias. Es el patrón de gh con GH_TOKEN —
nada del CLOUDSDK_CONFIG que gcloud necesita. MODAL_CONFIG_PATH apunta a un TOML
aparte como defensa en profundidad, para que ningún subcomando toque el global.

Tres gates, no uno: en Modal el cómputo se paga por segundo, así que "mutar" y
"prender hardware" no son el mismo permiso.

  - sin flag        lectura: list / logs / history / billing
  - --allow-write   mutaciones: deploy, app stop|rollback, secret|volume create|rm
  - --allow-gpu     además, lo que ARRANCA cómputo pagado: run, endpoint create
  - lista negra     sin opt-in posible: lo interactivo o que abre el browser
                    (setup, token new|set, shell, serve, bootstrap, dashboard,
                    profile, container exec) y cualquier --follow. Todos esperan
                    un tty o no terminan nunca: colgarían el canal JSON-RPC.

`deploy` es write pero NO compute: construye la imagen y registra la app; el
hardware se prende recién cuando alguien invoca una función. `endpoint create`
sí levanta contenedores de inferencia, y pide los dos permisos.
"""

import argparse
import os
import re
import sys
import asyncio
import subprocess
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

VERSION = "0.1.4"

parser = argparse.ArgumentParser()
parser.add_argument("--workspace", required=True, help="Workspace de Modal, ej: mrgenkko")
parser.add_argument("--environment", default=None, help="Environment por defecto (ej: main)")
parser.add_argument("--token-id", default=None, help="ak-...; si se omite se usa MODAL_TOKEN_ID del entorno")
parser.add_argument("--token-secret", default=None, help="as-...; si se omite se usa MODAL_TOKEN_SECRET")
parser.add_argument("--config-path", default=None, help="MODAL_CONFIG_PATH aislado (defensa en profundidad)")
parser.add_argument("--modal-bin", default=None, help="Ruta al binario modal (default: el del venv que corre este server)")
parser.add_argument("--name", default=None, help="Label de la instancia (modal-mrgenkko)")
# Por defecto solo lectura. Las mutaciones necesitan --allow-write; lo que además
# prende hardware pagado necesita --allow-gpu (los dos son independientes).
parser.add_argument("--allow-write", action="store_true")
parser.add_argument("--allow-gpu", action="store_true")
args, _ = parser.parse_known_args()

WORKSPACE = args.workspace
ENVIRONMENT = args.environment
ALLOW_WRITE = args.allow_write
ALLOW_GPU = args.allow_gpu
SERVER_LABEL = args.name or WORKSPACE

# El binario vive en el mismo venv que este server (el cliente MCP nos lanza con
# <venv>/bin/python). No se resuelve por PATH: el cliente solo hereda una
# whitelist de variables —HOME/PATH/SHELL/TERM/USER— y ese PATH normalmente no
# incluye el bin/ del venv, así que un `modal` pelado daría FileNotFoundError.
MODAL_BIN = args.modal_bin or os.path.join(os.path.dirname(sys.executable), "modal")

# Env de los subprocess modal:
# - MODAL_TOKEN_ID/SECRET: autenticación por instancia → CLI stateless, sin deriva.
# - MODAL_CONFIG_PATH: perfil aislado; ningún subcomando escribe el ~/.modal.toml global.
# - MODAL_ENVIRONMENT: environment por defecto (igual se pasa -e explícito en cada tool).
# - COLUMNS alto: la CLI usa rich, que TRUNCA las tablas al ancho del terminal.
#   Sin esto los nombres de app y los IDs salen cortados con "…" y son inservibles.
# - NO_COLOR/TERM=dumb: sin ANSI, salida parseable.
_ENV = {
    **os.environ,
    "NO_COLOR": "1",
    "TERM": "dumb",
    "COLUMNS": "200",
    "PYTHONUNBUFFERED": "1",
}
if args.token_id:
    _ENV["MODAL_TOKEN_ID"] = args.token_id
if args.token_secret:
    _ENV["MODAL_TOKEN_SECRET"] = args.token_secret
if args.config_path:
    _ENV["MODAL_CONFIG_PATH"] = os.path.expanduser(args.config_path)
if ENVIRONMENT:
    _ENV["MODAL_ENVIRONMENT"] = ENVIRONMENT

app = Server(f"modal-{SERVER_LABEL}")


# Comandos que esperan un tty, abren el navegador o no terminan nunca. No hay
# opt-in: colgarían el stdio JSON-RPC del server y matarían la sesión MCP.
BLACKLIST = {
    ("setup", ""),        # flujo de onboarding interactivo
    ("bootstrap", ""),    # scaffolding interactivo
    ("shell", ""),        # shell dentro de un contenedor (tty)
    ("serve", ""),        # corre hasta que lo matan (hot-reload)
    ("dashboard", ""),    # abre el browser
    ("token", "new"),     # abre el browser para autenticar
    ("token", "set"),     # escribiría el perfil: el token va por env, no en disco
    ("profile", ""),      # cambia el perfil activo global (deriva de workspace)
    ("config", "set"),    # escribe el .modal.toml
    ("container", "exec"),  # ejecución arbitraria + tty
    ("app", "dashboard"),
    ("volume", "dashboard"),
    ("skills", ""),
}

# Mutan estado en Modal: gateadas por --allow-write.
WRITE_VERBS = {
    ("deploy", ""),
    ("app", "stop"), ("app", "rollback"), ("app", "rollover"),
    ("container", "stop"),
    ("endpoint", "stop"),
    ("secret", "create"), ("secret", "delete"),
    ("volume", "create"), ("volume", "delete"), ("volume", "rename"),
    ("volume", "put"), ("volume", "rm"), ("volume", "cp"),
    ("environment", "create"), ("environment", "delete"), ("environment", "update"),
    ("dict", "create"), ("dict", "delete"), ("dict", "clear"),
    ("queue", "create"), ("queue", "delete"), ("queue", "clear"), ("queue", "peek"),
    ("image", "delete"),
}

# Arrancan cómputo facturado por segundo: gateadas por --allow-gpu (además de write).
COMPUTE_VERBS = {
    ("run", ""),
    ("endpoint", "create"),
}

#: `billing rates` nombra las GPUs distinto que el parámetro `gpu=` de las apps
#: ("RTX-PRO-6000" vs "gpu_hour_cost_rtx6000"). Sin esta tabla no se puede pasar de
#: una a la otra, y no hay forma de derivarla: son dos nomenclaturas separadas.
ALIAS_GPU = {
    "t4": "t4", "l4": "l4", "a10": "a10g", "a10g": "a10g", "l40s": "l40s",
    "a100": "a100_80gb", "a100-40gb": "a100_40gb", "a100-80gb": "a100_80gb",
    "rtx-pro-6000": "rtx6000", "rtx6000": "rtx6000",
    "h100": "h100", "h200": "h200", "b200": "b200", "b300": "b300",
}

TIMEOUT_DEFAULT = 60
TIMEOUT_MAX = 900
TIMEOUT_DEPLOY = 600

# Tope de salida. La CLI usa rich con COLUMNS=200, así que un traceback o un
# volcado de logs puede ser de decenas de miles de caracteres: entra entero al
# contexto del agente y no aporta. Se conservan cabeza y cola —en un traceback
# lo que importa está al final, en un log también— y se avisa del recorte.
MAX_OUTPUT = 12000
HEAD_KEEP = 7000
TAIL_KEEP = 4000


def _cap(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    recortado = len(text) - HEAD_KEEP - TAIL_KEEP
    return (
        text[:HEAD_KEEP]
        + f"\n\n[... {recortado} caracteres recortados por el MCP "
          f"(salida de {len(text)}, tope {MAX_OUTPUT}). Acotá con tail/since/search ...]\n\n"
        + text[-TAIL_KEEP:]
    )


#: Los tokens de Modal son `ak-...` (id) y `as-...` (secret). `modal token info` los
#: imprime enteros, y cualquier traceback puede escupirlos. Se redactan en TODA salida
#: antes de devolverla: lo que entra al contexto del agente queda escrito en el
#: transcript de la conversación, que es exactamente donde un secreto no debe estar.
_CRED_RE = re.compile(r"\b(ak|as)-[A-Za-z0-9_-]{6,}\b")


def _redactar(texto: str) -> str:
    return _CRED_RE.sub(lambda m: f"{m.group(1)}-«redactado»", texto)


def _clamp_timeout(value, default: int = TIMEOUT_DEFAULT) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, TIMEOUT_MAX))


def _verb_pair(args_list: list) -> tuple:
    """(comando, subcomando) — los dos primeros tokens que no son flags.

    Para los comandos de un solo nivel (`deploy app.py`, `run app.py::fn`) el
    segundo token es un argumento, no un subcomando; por eso se prueba también
    el par (cmd, "").
    """
    toks = [a for a in args_list if not a.startswith("-")]
    cmd = toks[0].lower() if len(toks) > 0 else ""
    sub = toks[1].lower() if len(toks) > 1 else ""
    return cmd, sub


def _match(args_list: list, verbs: set) -> bool:
    cmd, sub = _verb_pair(args_list)
    return (cmd, sub) in verbs or (cmd, "") in verbs


def _is_follow(args_list: list) -> bool:
    """--follow no termina nunca. -f solo es --follow en los subcomandos de logs."""
    if "--follow" in args_list:
        return True
    return "-f" in args_list and "logs" in [a.lower() for a in args_list]


def _gate(args_list: list) -> str | None:
    """Devuelve el motivo del bloqueo, o None si el comando puede correr."""
    cmd, sub = _verb_pair(args_list)
    verb = f"{cmd} {sub}".strip()
    if _match(args_list, BLACKLIST):
        return (
            f"Bloqueado: '{verb}' es interactivo o no termina (espera un tty, abre el "
            f"navegador o corre indefinidamente) y colgaría el canal MCP. No hay opt-in. "
            f"Para logs usá app_logs con tail/since en vez de --follow."
        )
    if _is_follow(args_list):
        return (
            "Bloqueado: --follow deja el comando corriendo hasta que lo maten y colgaría "
            "el canal MCP. Usá tail/since para leer un rango cerrado."
        )
    if _match(args_list, COMPUTE_VERBS) and not ALLOW_GPU:
        return (
            f"Bloqueado: '{verb}' ARRANCA cómputo facturado por segundo en {SERVER_LABEL}. "
            f"Re-registrá la instancia con allow_gpu:true en secrets.json (flag --allow-gpu)."
        )
    if (_match(args_list, WRITE_VERBS) or _match(args_list, COMPUTE_VERBS)) and not ALLOW_WRITE:
        return (
            f"Bloqueado: '{verb}' es una operación de escritura deshabilitada en {SERVER_LABEL}. "
            f"Re-registrá la instancia con allow_write:true en secrets.json (flag --allow-write)."
        )
    return None


def _run(cli_args: list, timeout: int = TIMEOUT_DEFAULT, extra_env: dict | None = None) -> str:
    """Corre `modal <cli_args>` aplicando los gates. cli_args NO incluye el binario."""
    blocked = _gate(cli_args)
    if blocked:
        return blocked
    entorno = {**_ENV, **{k: str(v) for k, v in (extra_env or {}).items()}}
    # stdin=DEVNULL: nunca heredar el canal JSON-RPC del protocolo stdio. Si la CLI
    # hereda el stdin del proceso MCP puede colgarse esperando una confirmación o
    # robar bytes del protocolo — ambos casos terminan en timeouts del cliente.
    try:
        result = subprocess.run(
            [MODAL_BIN] + cli_args, capture_output=True, text=True,
            timeout=timeout, stdin=subprocess.DEVNULL, env=entorno,
        )
    except subprocess.TimeoutExpired:
        return (
            f"[timeout] El comando tardó más de {timeout}s y fue cancelado. "
            f"OJO: si era un deploy, puede haber quedado corriendo del lado de Modal — "
            f"verificá con app_list antes de reintentar."
        )
    except FileNotFoundError:
        return (
            f"[error] No se encontró el binario modal en {MODAL_BIN}. "
            f"Instalalo con scripts/install-modal-mcp.sh o pasá --modal-bin."
        )
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode != 0:
        return _redactar(_cap(f"[exit {result.returncode}]\n{err or out}"))
    return _redactar(_cap(out or err or "(sin output)"))


def _estado_app(nombre: str) -> str:
    """Estado de una app después de mutarla.

    `app stop` y compañía no imprimen nada al terminar bien, y "(sin output)" no
    distingue "salió perfecto" de "no hizo nada". Se re-consulta y se dice el estado.
    """
    import json as _json

    salida = _run(["app", "list", "--json"] + (["-e", ENVIRONMENT] if ENVIRONMENT else []))
    try:
        apps = _json.loads(salida)
    except Exception:
        return f"(no se pudo confirmar el estado: {salida[:200]})"
    for a in apps:
        if nombre in (a.get("description"), a.get("app_id")):
            return f"{a.get('description')} [{a.get('app_id')}] -> estado: {a.get('state')}, tareas: {a.get('tasks')}"
    return f"'{nombre}' ya no aparece en la lista de apps."


def _urls_del_deploy(salida: str) -> str:
    """Extrae las URLs que la CLI escupe en prosa, para no tener que leerlas a ojo.

    La CLI las parte en varias líneas cuando son largas, así que se junta todo antes
    de buscar.
    """
    plano = re.sub(r"\s+", " ", salida)
    endpoints = re.findall(r"https://[\w.-]+\.modal\.(?:direct|run)\S*", plano)
    dashboard = re.findall(r"https://modal\.com/apps/\S+", plano)
    lineas = []
    for u in dict.fromkeys(endpoints):
        lineas.append(f"endpoint : {u}")
    for u in dict.fromkeys(dashboard):
        lineas.append(f"dashboard: {u}")
    return ("\n".join(lineas) + "\n\n") if lineas else ""


def _env_flag(arguments: dict) -> list:
    """-e <env>: el de la llamada, si no el de la instancia, si no nada."""
    env = arguments.get("environment") or ENVIRONMENT
    return ["-e", env] if env else []


def _json_flag(arguments: dict) -> list:
    return ["--json"] if arguments.get("json", True) else []


# Propiedades comunes a casi todas las tools.
_ENV_PROP = {
    "environment": {
        "type": "string",
        "description": f"Environment de Modal (default de la instancia: {ENVIRONMENT or 'el del workspace'}).",
    }
}
_JSON_PROP = {
    "json": {
        "type": "boolean",
        "description": "Salida JSON parseable (default true). false devuelve la tabla de rich.",
    }
}
_TIMEOUT_PROP = {
    "timeout": {
        "type": "integer",
        "description": f"Timeout en segundos (default {TIMEOUT_DEFAULT}, máx {TIMEOUT_MAX}).",
    }
}


def _schema(props: dict, required: list = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    write_state = "habilitada (allow_write)" if ALLOW_WRITE else "BLOQUEADA (registrá con allow_write:true)"
    gpu_state = "habilitado (allow_gpu)" if ALLOW_GPU else "BLOQUEADO (registrá con allow_gpu:true)"
    ctx = f"workspace {WORKSPACE}" + (f", environment {ENVIRONMENT}" if ENVIRONMENT else "")

    return [
        # ---------------- Lectura ----------------
        types.Tool(
            name="modal",
            description=(
                f"Ejecuta un comando arbitrario de la CLI modal en {ctx}. "
                f"Lectura libre; escritura {write_state}; cómputo {gpu_state}. "
                f"Lo interactivo (setup, shell, serve, token new, dashboard) y --follow "
                f"están bloqueados sin opt-in: colgarían el canal MCP."
            ),
            inputSchema=_schema({
                "args": {
                    "type": "array", "items": {"type": "string"},
                    "description": 'Ej: ["app", "list", "--json"] o ["volume", "ls", "modelos", "/"]',
                },
                **_TIMEOUT_PROP,
            }, ["args"]),
        ),
        types.Tool(
            name="whoami",
            description=(
                "Quién soy y contra qué workspace apunta esta instancia (`modal token info`). "
                "Primera llamada útil para confirmar que el token está bien y no hay deriva."
            ),
            inputSchema=_schema({}),
        ),
        types.Tool(
            name="app_list",
            description=(
                f"Apps desplegadas, corriendo o detenidas hace poco en {ctx}. Devuelve app_id "
                f"(ap-*), nombre, estado y tareas. OJO: NO trae la GPU — la CLI no la expone "
                f"acá, así que esta lista no sirve para auditar qué hardware quedó declarado; "
                f"para eso hay que mirar el código de la app o el dashboard."
            ),
            inputSchema=_schema({**_ENV_PROP, **_JSON_PROP}),
        ),
        types.Tool(
            name="app_history",
            description="Historial de deploys de una app: versiones, tags, cuál está activa (para elegir a dónde hacer rollback).",
            inputSchema=_schema({
                "app": {"type": "string", "description": "Nombre de la app o app_id (ap-*)."},
                **_ENV_PROP, **_JSON_PROP,
            }, ["app"]),
        ),
        types.Tool(
            name="app_logs",
            description=(
                "Logs de una app en un rango CERRADO (nunca --follow, que colgaría el canal). "
                "Sin filtros trae los últimos `tail`; con `since` un rango relativo ('30m', '2h', '1d')."
            ),
            inputSchema=_schema({
                "app": {"type": "string", "description": "Nombre de la app o app_id (ap-*)."},
                "tail": {"type": "integer", "description": "Últimas N líneas (default 100)."},
                "since": {"type": "string", "description": "Inicio del rango: ISO 8601 o relativo ('30m', '2h', '1d')."},
                "until": {"type": "string", "description": "Fin del rango, mismo formato que since."},
                "search": {"type": "string", "description": "Filtra por texto."},
                "source": {"type": "string", "description": "stdout | stderr | system."},
                "timestamps": {"type": "boolean", "description": "Prefijar cada línea con su timestamp."},
                **_ENV_PROP, **_TIMEOUT_PROP,
            }, ["app"]),
        ),
        types.Tool(
            name="container_list",
            description="Contenedores corriendo ahora (los que están facturando). Filtrable por app_id.",
            inputSchema=_schema({
                "app_id": {"type": "string", "description": "Solo los de esta app (ap-*)."},
                **_ENV_PROP, **_JSON_PROP,
            }),
        ),
        types.Tool(
            name="container_logs",
            description="Logs de un contenedor puntual (ta-*), en rango cerrado.",
            inputSchema=_schema({
                "container_id": {"type": "string", "description": "ID del contenedor (ta-*)."},
                "tail": {"type": "integer", "description": "Últimas N líneas (default 100)."},
                "since": {"type": "string", "description": "Inicio del rango ('30m', '2h'...)."},
                **_ENV_PROP, **_TIMEOUT_PROP,
            }, ["container_id"]),
        ),
        types.Tool(
            name="endpoint_list",
            description=(
                "Endpoints de inferencia LLM (Modal Endpoints) que están provisionando o corriendo. "
                "Son servidores de inferencia gestionados: se crean con endpoint_create, sin escribir código."
            ),
            inputSchema=_schema({**_ENV_PROP, **_JSON_PROP}),
        ),
        types.Tool(
            name="volume_list",
            description="Volúmenes persistentes (modal.Volume) del environment. Es donde viven los pesos de los modelos.",
            inputSchema=_schema({**_ENV_PROP, **_JSON_PROP}),
        ),
        types.Tool(
            name="volume_ls",
            description="Lista archivos y directorios dentro de un volumen.",
            inputSchema=_schema({
                "volume": {"type": "string", "description": "Nombre del volumen."},
                "path": {"type": "string", "description": "Ruta dentro del volumen (default /)."},
                **_ENV_PROP, **_JSON_PROP,
            }, ["volume"]),
        ),
        types.Tool(
            name="secret_list",
            description="Secrets publicados en el environment (nombres y fecha, nunca los valores).",
            inputSchema=_schema({**_ENV_PROP, **_JSON_PROP}),
        ),
        types.Tool(
            name="environment_list",
            description=f"Environments del workspace {WORKSPACE} (main, dev, ...).",
            inputSchema=_schema({**_JSON_PROP}),
        ),
        types.Tool(
            name="billing",
            description=(
                "Cuánto se está gastando. 'summary' = consumo del workspace; 'rates' = precios "
                "vigentes por tipo de hardware (útil para estimar antes de prender una GPU)."
            ),
            inputSchema=_schema({
                "report": {"type": "string", "description": "summary (default) | rates."},
                **_JSON_PROP, **_TIMEOUT_PROP,
            }),
        ),
        types.Tool(
            name="costo_ahora",
            description=(
                "Qué se está gastando EN ESTE MOMENTO: contenedores vivos y hace cuánto "
                "arrancaron, con los precios vigentes. Es la pregunta que `billing summary` "
                "no responde, porque el billing no es tiempo real. "
                "La CLI no dice qué GPU usa cada contenedor: pasá `gpu` para que calcule el "
                "costo asumiendo ese hardware; sin eso devuelve los tiempos y la tabla de precios."
            ),
            inputSchema=_schema({
                "gpu": {
                    "type": "string",
                    "description": "GPU asumida para el cálculo, como se declara en la app "
                                   "(RTX-PRO-6000, H100, A100-80GB, L40S...).",
                },
                **_ENV_PROP,
            }),
        ),
        types.Tool(
            name="image_logs",
            description=(
                "Log de build de una imagen. Es lo único que explica un deploy que falla "
                "mientras construye: el error de `deploy` sólo dice que la imagen no se armó."
            ),
            inputSchema=_schema({
                "image_id": {"type": "string", "description": "ID de la imagen (im-*)."},
                **_TIMEOUT_PROP,
            }, ["image_id"]),
        ),
        types.Tool(
            name="dashboard_url",
            description=(
                "Construye la URL del dashboard (NO abre el navegador — `modal dashboard` está "
                "bloqueado porque colgaría el canal). Sin app_id devuelve la del environment."
            ),
            inputSchema=_schema({
                "app_id": {"type": "string", "description": "app_id (ap-*) o nombre de la app."},
                **_ENV_PROP,
            }),
        ),

        # ---------------- Escritura (allow_write) ----------------
        types.Tool(
            name="deploy",
            description=(
                f"Despliega una app de Modal desde un archivo o módulo. Escritura {write_state}. "
                f"Construye la imagen y registra la app; NO prende GPU por sí solo (eso pasa cuando "
                f"se invoca una función). El build puede tardar minutos: timeout default {TIMEOUT_DEPLOY}s "
                f"y sin --stream-logs (colgaría el canal). Si da timeout, verificá con app_list: "
                f"el deploy puede haber seguido del lado de Modal."
            ),
            inputSchema=_schema({
                "target": {"type": "string", "description": "Ruta al script (ej: infra/modal_app.py) o módulo si module=true."},
                "module": {"type": "boolean", "description": "Interpretar target como módulo Python (-m)."},
                "name": {"type": "string", "description": "Nombre del deployment."},
                "tag": {"type": "string", "description": "Tag de versión del deploy."},
                "strategy": {"type": "string", "description": "rolling (default de Modal) | recreate."},
                "cwd": {"type": "string", "description": "Directorio desde el que correr (default: el del server)."},
                "env": {
                    "type": "object",
                    "description": "Variables para el proceso `modal` LOCAL (no el contenedor). "
                                   "El script de la app se ejecuta acá durante el deploy, así "
                                   'que puede leerlas: {"QWEN38_VARIANTE": "nvfp4"}.',
                },
                **_ENV_PROP, **_TIMEOUT_PROP,
            }, ["target"]),
        ),
        types.Tool(
            name="app_stop",
            description=f"Detiene una app permanentemente y termina sus contenedores (corta la facturación). Escritura {write_state}.",
            inputSchema=_schema({
                "app": {"type": "string", "description": "Nombre de la app o app_id (ap-*)."},
                **_ENV_PROP,
            }, ["app"]),
        ),
        types.Tool(
            name="app_rollback",
            description=f"Redespliega una versión anterior de la app (ver app_history). Escritura {write_state}.",
            inputSchema=_schema({
                "app": {"type": "string", "description": "Nombre de la app o app_id (ap-*)."},
                "version": {"type": "string", "description": "Versión o tag al que volver (default: la anterior)."},
                "strategy": {"type": "string", "description": "rolling | recreate."},
                **_ENV_PROP, **_TIMEOUT_PROP,
            }, ["app"]),
        ),
        types.Tool(
            name="volume_create",
            description=f"Crea un volumen persistente. Escritura {write_state}.",
            inputSchema=_schema({
                "volume": {"type": "string", "description": "Nombre del volumen."},
                **_ENV_PROP,
            }, ["volume"]),
        ),
        types.Tool(
            name="volume_put",
            description=(
                f"Sube un archivo o directorio local al volumen. Escritura {write_state}. "
                f"Subir pesos de un modelo tarda: subí el timeout."
            ),
            inputSchema=_schema({
                "volume": {"type": "string", "description": "Nombre del volumen."},
                "local_path": {"type": "string", "description": "Ruta local del archivo o directorio."},
                "remote_path": {"type": "string", "description": "Destino dentro del volumen (opcional)."},
                **_ENV_PROP, **_TIMEOUT_PROP,
            }, ["volume", "local_path"]),
        ),
        types.Tool(
            name="volume_get",
            description=f"Descarga un archivo o directorio del volumen al disco local. Escritura {write_state} (escribe local).",
            inputSchema=_schema({
                "volume": {"type": "string", "description": "Nombre del volumen."},
                "remote_path": {"type": "string", "description": "Ruta dentro del volumen."},
                "local_path": {"type": "string", "description": "Destino local (opcional)."},
                **_ENV_PROP, **_TIMEOUT_PROP,
            }, ["volume", "remote_path"]),
        ),
        types.Tool(
            name="volume_rm",
            description=f"Borra un archivo o directorio DENTRO del volumen (no borra el volumen). Escritura {write_state}.",
            inputSchema=_schema({
                "volume": {"type": "string", "description": "Nombre del volumen."},
                "remote_path": {"type": "string", "description": "Ruta a borrar dentro del volumen."},
                "recursive": {"type": "boolean", "description": "Borrado recursivo (-r)."},
                **_ENV_PROP,
            }, ["volume", "remote_path"]),
        ),
        types.Tool(
            name="secret_create",
            description=(
                f"Crea un secret. Escritura {write_state}. force=true lo sobreescribe.\n"
                f"PREFERIR from_dotenv o from_json: con `values` el secreto viaja como "
                f"argumento, y eso lo deja escrito en la conversación y visible en `ps aux` "
                f"para cualquier usuario de la máquina. Con un archivo, nunca pasa por argv."
            ),
            inputSchema=_schema({
                "name": {"type": "string", "description": "Nombre del secret."},
                "from_dotenv": {
                    "type": "string",
                    "description": "Ruta a un archivo .env con KEY=VALUE. La forma recomendada.",
                },
                "from_json": {"type": "string", "description": "Ruta a un JSON {clave: valor}."},
                "values": {
                    "type": "array", "items": {"type": "string"},
                    "description": 'Pares KEY=VALUE, ej: ["HF_TOKEN=hf_xxx"]. Último recurso: '
                                   'queda en el transcript y en la línea de comandos.',
                },
                "force": {"type": "boolean", "description": "Sobreescribir si existe."},
                **_ENV_PROP,
            }, ["name"]),
        ),
        types.Tool(
            name="endpoint_stop",
            description=f"Detiene un endpoint de inferencia y termina sus contenedores. Escritura {write_state}.",
            inputSchema=_schema({
                "endpoint": {"type": "string", "description": "Nombre o ID del endpoint."},
                **_ENV_PROP,
            }, ["endpoint"]),
        ),

        # ---------------- Cómputo facturado (allow_write + allow_gpu) ----------------
        types.Tool(
            name="run",
            description=(
                f"Corre una función o entrypoint de una app. PRENDE CÓMPUTO FACTURADO: {gpu_state}. "
                f"Siempre en modo detach=false y sin -i; el resultado vuelve cuando termina."
            ),
            inputSchema=_schema({
                "target": {"type": "string", "description": "archivo.py::funcion, o módulo si module=true."},
                "module": {"type": "boolean", "description": "Interpretar target como módulo Python (-m)."},
                "detach": {"type": "boolean", "description": "No matar la app si el proceso local muere (-d)."},
                "cwd": {"type": "string", "description": "Directorio desde el que correr."},
                "env": {
                    "type": "object",
                    "description": "Variables para el proceso `modal` LOCAL (no el contenedor). "
                                   "El script de la app se ejecuta acá durante el deploy, así "
                                   'que puede leerlas: {"QWEN38_VARIANTE": "nvfp4"}.',
                },
                **_ENV_PROP, **_TIMEOUT_PROP,
            }, ["target"]),
        ),
        types.Tool(
            name="endpoint_create",
            description=(
                f"Levanta un endpoint de inferencia LLM gestionado a partir de un modelo de Hugging Face, "
                f"sin escribir código. PRENDE CÓMPUTO FACTURADO: {gpu_state} (y escritura {write_state}). "
                f"Devuelve la URL del endpoint; para pesos propios usá custom_hf_repo o custom_volume_name."
            ),
            inputSchema=_schema({
                "model": {"type": "string", "description": "Repo de HF de la arquitectura base, ej: Qwen/Qwen3.6-27B-FP8."},
                "name": {"type": "string", "description": "Nombre del endpoint (default: derivado del modelo)."},
                "routing_region": {"type": "string", "description": "Región de ruteo (default us-west)."},
                "compute_region": {"type": "string", "description": "Región de cómputo (multiplica el precio)."},
                "unauthenticated": {"type": "boolean", "description": "Permitir HTTP sin auth (ojo: queda público)."},
                "custom_hf_repo": {"type": "string", "description": "Repo HF con pesos fine-tuneados."},
                "custom_hf_revision": {"type": "string", "description": "Revisión git de custom_hf_repo."},
                "custom_volume_name": {"type": "string", "description": "Volumen de Modal con pesos propios."},
                "custom_volume_path": {"type": "string", "description": "Ruta de los pesos dentro del volumen."},
                **_ENV_PROP, **_TIMEOUT_PROP,
            }, ["model"]),
        ),
    ]


def _cwd(arguments: dict):
    """Directorio de trabajo para deploy/run: ahí tiene que estar el código de la app."""
    d = arguments.get("cwd")
    if not d:
        return None
    d = os.path.expanduser(d)
    return d if os.path.isdir(d) else None


def _target_error(arguments: dict, workdir) -> str | None:
    """Un archivo inexistente hace que la CLI vuelque un traceback de rich de ~6 KB.

    Vale más chequearlo acá y devolver una línea. Solo aplica a targets de archivo:
    con module=true el target es una ruta de módulo Python y no se resuelve en disco.
    """
    if arguments.get("module"):
        return None
    target = arguments.get("target") or ""
    # `modal run` acepta archivo.py::funcion — el path es lo de antes del ::
    path = target.split("::", 1)[0]
    if not path:
        return None
    resolved = os.path.expanduser(path)
    if not os.path.isabs(resolved):
        resolved = os.path.join(workdir or os.getcwd(), resolved)
    if not os.path.exists(resolved):
        return (
            f"[error] No existe el script: {resolved}\n"
            f"Pasá una ruta absoluta, o cwd=<directorio del proyecto> con una relativa. "
            f"Si querías un módulo Python, usá module=true."
        )
    return None


def _run_in(cli_args: list, arguments: dict, timeout: int):
    """Como _run pero cambia de directorio y admite env local (deploy/run leen el script).

    El env NO es el del contenedor: es el del proceso `modal` que corre acá. Sirve
    porque el script de la app se ejecuta LOCAL durante el deploy, así que puede
    decidir modelo, GPU o nombre de app leyendo os.environ.
    """
    workdir = _cwd(arguments)
    if arguments.get("cwd") and not workdir:
        return f"[error] cwd no existe o no es un directorio: {arguments['cwd']}"
    target_error = _target_error(arguments, workdir)
    if target_error:
        return target_error
    blocked = _gate(cli_args)
    if blocked:
        return blocked
    try:
        result = subprocess.run(
            [MODAL_BIN] + cli_args, capture_output=True, text=True,
            timeout=timeout, stdin=subprocess.DEVNULL,
            env={**_ENV, **{k: str(v) for k, v in (arguments.get("env") or {}).items()}},
            cwd=workdir,
        )
    except subprocess.TimeoutExpired:
        return (
            f"[timeout] El comando tardó más de {timeout}s y fue cancelado. "
            f"El trabajo puede haber seguido del lado de Modal: verificá con app_list."
        )
    except FileNotFoundError:
        return f"[error] No se encontró el binario modal en {MODAL_BIN}."
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode != 0:
        return _redactar(_cap(f"[exit {result.returncode}]\n{err or out}"))
    return _redactar(_cap(out or err or "(sin output)"))


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    env = _env_flag(arguments)
    t = _clamp_timeout(arguments.get("timeout", TIMEOUT_DEFAULT))

    # ---------------- Lectura ----------------
    if name == "modal":
        output = _run(arguments["args"], timeout=t)

    elif name == "whoami":
        # token info no soporta --json; se devuelve la tabla tal cual.
        info = _run(["token", "info"])
        output = f"instancia: {SERVER_LABEL} (workspace={WORKSPACE}, environment={ENVIRONMENT or '—'})\n{info}"

    elif name == "app_list":
        output = _run(["app", "list"] + env + _json_flag(arguments))

    elif name == "app_history":
        output = _run(["app", "history", arguments["app"]] + env + _json_flag(arguments))

    elif name == "app_logs":
        cmd = ["app", "logs", arguments["app"]] + env
        cmd += ["-n", str(arguments.get("tail", 100))]
        for flag, key in (("--since", "since"), ("--until", "until"),
                          ("--search", "search"), ("-s", "source")):
            if arguments.get(key):
                cmd += [flag, str(arguments[key])]
        if arguments.get("timestamps"):
            cmd.append("--timestamps")
        output = _run(cmd, timeout=t)

    elif name == "container_list":
        cmd = ["container", "list"] + env + _json_flag(arguments)
        if arguments.get("app_id"):
            cmd += ["--app-id", arguments["app_id"]]
        output = _run(cmd)

    elif name == "container_logs":
        # `container logs` no acepta -e/--env: el contenedor ya identifica su environment.
        cmd = ["container", "logs", arguments["container_id"], "-n", str(arguments.get("tail", 100))]
        if arguments.get("since"):
            cmd += ["--since", str(arguments["since"])]
        output = _run(cmd, timeout=t)

    elif name == "endpoint_list":
        output = _run(["endpoint", "list"] + env + _json_flag(arguments))

    elif name == "volume_list":
        output = _run(["volume", "list"] + env + _json_flag(arguments))

    elif name == "volume_ls":
        cmd = ["volume", "ls", arguments["volume"]]
        if arguments.get("path"):
            cmd.append(arguments["path"])
        output = _run(cmd + env + _json_flag(arguments))

    elif name == "secret_list":
        output = _run(["secret", "list"] + env + _json_flag(arguments))

    elif name == "environment_list":
        output = _run(["environment", "list"] + _json_flag(arguments))

    elif name == "billing":
        report = (arguments.get("report") or "summary").lower()
        if report not in ("summary", "rates"):
            output = "[error] report debe ser 'summary' o 'rates'."
        else:
            output = _run(["billing", report] + _json_flag(arguments), timeout=t)

    elif name == "costo_ahora":
        import json as _json
        from datetime import datetime, timezone

        crudo = _run(["container", "list", "--json"] + env)
        try:
            conts = _json.loads(crudo)
        except Exception:
            conts = None
        if conts is None:
            output = f"[error] no se pudo leer container list:\n{crudo}"
        elif not conts:
            output = "Sin contenedores vivos: no se está facturando cómputo ahora mismo."
        else:
            tarifa = None
            gpu = (arguments.get("gpu") or "").lower()
            rates_crudo = _run(["billing", "rates", "--json"])
            try:
                rates = _json.loads(rates_crudo)
            except Exception:
                rates = {}
            if gpu:
                clave = ALIAS_GPU.get(gpu)
                if clave:
                    tarifa = float(rates.get(f"gpu_hour_cost_{clave}", 0)) or None
            lineas, total_h = [], 0.0
            ahora = datetime.now(timezone.utc)
            # La CLI devuelve las columnas en snake_case en el JSON ("app_name") y con el
            # título de la tabla en la salida humana ("App Name"). Se aceptan las dos para
            # no depender de cuál emite esta versión.
            def _campo(c: dict, *nombres):
                for n in nombres:
                    if c.get(n):
                        return c[n]
                return None

            for c in conts:
                inicio = _campo(c, "start_time", "Start Time") or ""
                mins = None
                try:
                    dt = datetime.fromisoformat(str(inicio).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    mins = (ahora - dt).total_seconds() / 60
                except Exception:
                    pass
                # "Pending" es un estado real: el contenedor está arrancando y YA factura,
                # pero todavía no tiene hora de inicio que restar.
                edad = (f"{mins:.1f} min" if mins is not None
                        else ("arrancando (Pending)" if str(inicio).lower() == "pending"
                              else f"inicio={inicio or '?'}"))
                app_c = _campo(c, "app_name", "App Name") or "?"
                id_c = _campo(c, "container_id", "Container ID") or "?"
                fila = f"  {app_c} [{id_c}] vivo hace {edad}"
                if tarifa and mins is not None:
                    fila += f" -> USD {tarifa * mins / 60:.2f} acumulados"
                    total_h += tarifa
                lineas.append(fila)
            cab = f"{len(conts)} contenedor(es) VIVO(s) — facturando ahora:"
            pie = ""
            if tarifa:
                pie = f"\nRitmo actual: USD {total_h:.2f}/hora (asumiendo {arguments['gpu']} en todos)."
            else:
                pie = ("\nSin `gpu` no se puede calcular el costo: la CLI no dice qué hardware usa "
                       "cada contenedor. Precios por hora:\n  "
                       + ", ".join(f"{k.replace('gpu_hour_cost_', '')}={v}"
                                   for k, v in sorted(rates.items()) if k.startswith("gpu_hour_cost_")))
            output = cab + "\n" + "\n".join(lineas) + pie

    elif name == "image_logs":
        output = _run(["image", "logs", arguments["image_id"]], timeout=t)

    elif name == "dashboard_url":
        e = arguments.get("environment") or ENVIRONMENT or "main"
        base = f"https://modal.com/apps/{WORKSPACE}/{e}"
        output = f"{base}/{arguments['app_id']}" if arguments.get("app_id") else base

    # ---------------- Escritura (allow_write) ----------------
    elif name == "deploy":
        # TODAS las opciones van ANTES del target. `modal run` (y por simetría deploy)
        # entrega a la app lo que venga DESPUÉS del script: un -q ahí se parsea como
        # opción del entrypoint del usuario y la CLI corta con "No such option".
        cmd = ["deploy"] + env
        for flag, key in (("--name", "name"), ("--tag", "tag"), ("--strategy", "strategy")):
            if arguments.get(key):
                cmd += [flag, str(arguments[key])]
        if arguments.get("module"):
            cmd.append("-m")
        cmd.append(arguments["target"])
        # Sin --stream-logs a propósito: dejaría el proceso escuchando y colgaría el canal.
        output = _run_in(cmd, arguments, _clamp_timeout(arguments.get("timeout"), TIMEOUT_DEPLOY))
        # Las URLs salen en prosa y cortadas en varias líneas; se extraen al frente
        # para no tener que leerlas a ojo del log del deploy.
        output = _urls_del_deploy(output) + output

    elif name == "app_stop":
        salida = _run(["app", "stop", arguments["app"], "-y"] + env, timeout=t)
        output = f"{salida}\n{_estado_app(arguments['app'])}"

    elif name == "app_rollback":
        cmd = ["app", "rollback", arguments["app"]]
        if arguments.get("version"):
            cmd.append(str(arguments["version"]))
        cmd += env
        if arguments.get("strategy"):
            cmd += ["--strategy", arguments["strategy"]]
        salida = _run(cmd, timeout=t)
        output = f"{salida}\n{_estado_app(arguments['app'])}"

    elif name == "volume_create":
        salida = _run(["volume", "create", arguments["volume"]] + env)
        output = f"{salida}\n" + _run(["volume", "list", "--json"] + env)

    elif name == "volume_put":
        cmd = ["volume", "put", arguments["volume"], os.path.expanduser(arguments["local_path"])]
        if arguments.get("remote_path"):
            cmd.append(arguments["remote_path"])
        output = _run(cmd + env, timeout=t)

    elif name == "volume_get":
        cmd = ["volume", "get", arguments["volume"], arguments["remote_path"]]
        if arguments.get("local_path"):
            cmd.append(os.path.expanduser(arguments["local_path"]))
        output = _run(cmd + env, timeout=t)

    elif name == "volume_rm":
        cmd = ["volume", "rm", arguments["volume"], arguments["remote_path"]]
        if arguments.get("recursive"):
            cmd.append("-r")
        output = _run(cmd + env, timeout=t)

    elif name == "secret_create":
        values = arguments.get("values") or []
        dotenv = arguments.get("from_dotenv")
        from_json = arguments.get("from_json")
        fuentes = [bool(values), bool(dotenv), bool(from_json)]
        cmd = ["secret", "create", arguments["name"]]
        if sum(fuentes) != 1:
            output = ("[error] pasá EXACTAMENTE una fuente: from_dotenv (recomendada), "
                      "from_json o values.")
        elif dotenv or from_json:
            ruta = os.path.expanduser(dotenv or from_json)
            if not os.path.isfile(ruta):
                output = f"[error] no existe el archivo: {ruta}"
            else:
                cmd += ["--from-dotenv" if dotenv else "--from-json", ruta] + env
                if arguments.get("force"):
                    cmd.append("--force")
                output = _run(cmd, timeout=t)
        else:
            bad = [v for v in values if "=" not in v]
            if bad:
                output = f"[error] estos values no tienen forma KEY=VALUE: {bad}"
            else:
                cmd += values + env
                if arguments.get("force"):
                    cmd.append("--force")
                output = _run(cmd, timeout=t)

    elif name == "endpoint_stop":
        salida = _run(["endpoint", "stop", arguments["endpoint"], "-y"] + env, timeout=t)
        output = f"{salida}\n" + _run(["endpoint", "list", "--json"] + env)

    # ---------------- Cómputo facturado (allow_write + allow_gpu) ----------------
    elif name == "run":
        # -q: sin barras de progreso de rich, que en un pipe salen como basura.
        # Igual que en deploy, todo va ANTES del target: lo que sigue al script se lo
        # queda el entrypoint de la app del usuario.
        cmd = ["run", "-q"] + env
        if arguments.get("detach"):
            cmd.append("-d")
        if arguments.get("module"):
            cmd.append("-m")
        cmd.append(arguments["target"])
        output = _run_in(cmd, arguments, t)

    elif name == "endpoint_create":
        cmd = ["endpoint", "create", "--model", arguments["model"]] + env
        for flag, key in (
            ("--name", "name"),
            ("--routing-region", "routing_region"),
            ("--compute-region", "compute_region"),
            ("--custom-hf-repo", "custom_hf_repo"),
            ("--custom-hf-revision", "custom_hf_revision"),
            ("--custom-volume-name", "custom_volume_name"),
            ("--custom-volume-path", "custom_volume_path"),
        ):
            if arguments.get(key):
                cmd += [flag, str(arguments[key])]
        if arguments.get("unauthenticated"):
            cmd.append("--unauthenticated")
        output = _run(cmd, timeout=_clamp_timeout(arguments.get("timeout"), TIMEOUT_DEPLOY))

    else:
        output = f"Tool desconocido: {name}"

    return [types.TextContent(type="text", text=output)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
