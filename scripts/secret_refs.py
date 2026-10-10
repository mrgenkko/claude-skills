"""
Referencias a claves del llavero del SO, compartidas por mcp-secrets.py,
add-mcp-to-project.py y (fase 2) el lanzador de los MCP.

En secrets.json un campo secreto vale o un texto plano (legacy) o una referencia
`keyring:<id>`. El valor real vive en el llavero del sistema —GNOME Keyring en
Linux, Administrador de credenciales en Windows— bajo el servicio SERVICE y el
usuario <id>. Ver pendientes/llavero-plan.md.

Stdlib puro: `keyring` se importa recién al tocar el llavero, para que los tests
y add-mcp-to-project.py no dependan de él.
"""

SERVICE = "mrgenkko-mcp"
PREFIX = "keyring:"

# Campos de secrets.json que son credenciales, por tipo de MCP, y cómo le llegan
# al server: (variable de entorno, flag legacy en texto plano o None). Es la única
# tabla: el registro (add-mcp-to-project.py) y la comparación del --sweep salen de
# acá. focusyn es remoto: su "variable" es el nombre del header. gcloud.key_file
# NO está: es una ruta a un archivo, y gcloud necesita el archivo.
SECRET_FIELDS = {
    "postgres": {"password": ("MCP_PASSWORD", "--password")},
    "redis": {"password": ("MCP_PASSWORD", "--password")},
    "ssh": {"password": ("MCP_PASSWORD", "--password"),
            "sudo_password": ("MCP_SUDO_PASSWORD", "--sudo-password")},
    "gh": {"token": ("GH_TOKEN", None)},
    "modal": {"token_id": ("MODAL_TOKEN_ID", None), "token_secret": ("MODAL_TOKEN_SECRET", None)},
    "focusyn": {"agent_key": ("X-Agent-Key", None)},
}


def is_ref(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def ref_id(value: str) -> str:
    return value[len(PREFIX):]


def make_ref(rid: str) -> str:
    return PREFIX + rid


def default_id(name: str, field: str) -> str:
    """Id que asigna la migración: una entrada por instancia."""
    return f"{name}/{field}"


def secret_fields(entry: dict) -> tuple:
    return tuple(SECRET_FIELDS.get(entry.get("type"), {}))


def destino(kind: str, campo: str) -> tuple:
    """(variable o header, flag legacy o None) de un campo secreto."""
    return SECRET_FIELDS[kind][campo]


def explain(exc: BaseException, rid: str) -> str:
    """Mensaje accionable para una falla del llavero. Nunca incluye valores.

    Los casos salen de la fase 0 (pendientes/llavero-fase0.md): sin D-Bus keyring
    dice «No recommended backend» —engañoso: el backend está, falta la sesión—, y
    un desbloqueo cancelado llega como KeyringLocked.
    """
    kind = type(exc).__name__
    if kind == "NoKeyringError":
        return (f"no hay llavero disponible para leer keyring:{rid}. Fuera de la sesión de "
                f"escritorio (SSH, cron) no hay D-Bus: estas claves solo se leen desde la sesión gráfica.")
    if kind == "KeyringLocked":
        return (f"el llavero está bloqueado o se canceló el desbloqueo (keyring:{rid}). Desbloqueá "
                f"«Inicio de sesión» en Contraseñas y claves y reintentá.")
    return f"no se pudo usar el llavero para keyring:{rid} ({kind})."
