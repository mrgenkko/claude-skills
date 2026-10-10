#!/usr/bin/env python3
"""
Claves de los MCP en el llavero del SO (GNOME Keyring / Administrador de credenciales).

Uso:
    mcp-secrets.py set <id>                 guarda o reemplaza una clave (la pide sin mostrarla)
    mcp-secrets.py delete <id> [--yes]      borra una clave del llavero
    mcp-secrets.py list                     ids guardados en el llavero (nunca valores)
    mcp-secrets.py check                    resuelve cada keyring:<id> de secrets.json
    mcp-secrets.py migrate --dry-run        qué valores en texto plano pasaría al llavero
    mcp-secrets.py migrate                  los pasa, verifica y reescribe secrets.json
    mcp-secrets.py migrate --finalize [--yes]   borra secrets.json.bak

No se llama secrets.py a propósito: ese nombre tapa el módulo `secrets` de la
stdlib para todo lo que corra desde scripts/ (incluidos los tests).

Nunca imprime valores. Ver pendientes/llavero-plan.md.
"""

import getpass
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import secret_refs as refs  # noqa: E402

SKILLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRETS_FILE = os.path.join(SKILLS_DIR, "scripts", "secrets.json")


class Llavero:
    """El llavero real. Los tests inyectan uno en memoria con la misma interfaz."""

    def __init__(self):
        import keyring  # recién acá: el resto del script no depende de él
        self._k = keyring

    def get(self, rid):
        return self._k.get_password(refs.SERVICE, rid)

    def set(self, rid, value):
        self._k.set_password(refs.SERVICE, rid, value)

    def delete(self, rid):
        self._k.delete_password(refs.SERVICE, rid)

    def ids(self):
        """Ids guardados bajo SERVICE, o None si el backend no sabe enumerar.

        keyring no tiene API para listar; en Linux se busca por atributo en el
        Secret Service (buscar no requiere desbloquear la colección).
        """
        try:
            import secretstorage
        except ImportError:
            return None
        conn = secretstorage.dbus_init()
        try:
            coleccion = secretstorage.get_default_collection(conn)
            items = coleccion.search_items({"service": refs.SERVICE})
            return sorted(i.get_attributes().get("username", "?") for i in items)
        finally:
            conn.close()


class Conflicto(Exception):
    pass


def leer_secrets(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def escribir_secrets(path, data):
    """Temporal + os.replace. mkstemp crea 0600: el resultado queda 0600 (antes era 0644)."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".mcp-secrets-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def escribir_respaldo(path, contenido: bytes):
    """0600 desde que nace (O_EXCL + modo en os.open), no chmod después."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(contenido)


def campos(data):
    """(entrada, campo, valor) de cada campo secreto presente en secrets.json."""
    for entry in data.get("mcp_servers", []):
        for campo in refs.secret_fields(entry):
            if entry.get(campo):
                yield entry, campo, entry[campo]


def confirmar(pregunta, yes):
    if yes:
        return True
    if not sys.stdin.isatty():
        print("ERROR: hace falta confirmación y stdin no es interactivo. Agregá --yes.")
        return False
    try:
        return input(f"{pregunta} Escribí 'si' para confirmar: ").strip().lower() in ("si", "sí")
    except (EOFError, KeyboardInterrupt):
        return False


def falla(exc, rid):
    print(f"ERROR: {refs.explain(exc, rid)}")
    return 1


# --- comandos -------------------------------------------------------------

def cmd_set(store, rid):
    valor = getpass.getpass(f"Valor para keyring:{rid} (no se muestra): ")
    if not valor:
        print("ERROR: valor vacío, no se guardó nada.")
        return 1
    try:
        previo = store.get(rid)
        store.set(rid, valor)
        ok = store.get(rid) == valor
    except Exception as exc:
        return falla(exc, rid)
    if not ok:
        print(f"ERROR: keyring:{rid} no se leyó igual después de guardarla.")
        return 1
    accion = "reemplazada" if previo is not None else "guardada"
    print(f"keyring:{rid} {accion} ({len(valor)} caracteres).")
    return 0


def cmd_delete(store, rid, yes, secrets_file):
    usada = []
    if os.path.exists(secrets_file):
        usada = [f"{e['name']}.{c}" for e, c, v in campos(leer_secrets(secrets_file))
                 if refs.is_ref(v) and refs.ref_id(v) == rid]
    if usada:
        print(f"OJO: keyring:{rid} está referenciada en secrets.json por: {', '.join(usada)}")
        print("     Esos MCPs no van a arrancar sin ella.")
    if not confirmar(f"Borrar keyring:{rid} del llavero.", yes):
        print("Cancelado.")
        return 1
    try:
        if store.get(rid) is None:
            print(f"keyring:{rid} no existe.")
            return 1
        store.delete(rid)
    except Exception as exc:
        return falla(exc, rid)
    print(f"keyring:{rid} borrada.")
    return 0


def cmd_list(store, secrets_file):
    try:
        ids = store.ids()
    except Exception as exc:
        return falla(exc, "*")
    if ids is None:
        print("Este backend no permite listar el llavero. Usá `check` para ver las referencias de secrets.json.")
        return 0
    usadas = set()
    if os.path.exists(secrets_file):
        usadas = {refs.ref_id(v) for _, _, v in campos(leer_secrets(secrets_file)) if refs.is_ref(v)}
    if not ids:
        print(f"No hay claves bajo el servicio '{refs.SERVICE}'.")
        return 0
    for rid in ids:
        print(f"  {rid}{'' if rid in usadas else '   (huérfana: ningún MCP la referencia)'}")
    print(f"{len(ids)} clave(s) en el llavero.")
    return 0


def cmd_check(store, secrets_file):
    data = leer_secrets(secrets_file)
    faltan = planos = 0
    for entry, campo, valor in campos(data):
        etiqueta = f"{entry['name']}.{campo}"
        if not refs.is_ref(valor):
            planos += 1
            print(f"  PLANO   {etiqueta}  (texto plano: migrate lo pasa al llavero)")
            continue
        rid = refs.ref_id(valor)
        try:
            existe = store.get(rid) is not None
        except Exception as exc:
            return falla(exc, rid)
        if not existe:
            faltan += 1
        print(f"  {'OK   ' if existe else 'FALTA'}   {etiqueta}  → keyring:{rid}")
    print()
    print(f"{faltan} faltante(s), {planos} en texto plano.")
    if faltan:
        print("Cargá las faltantes con: mcp-secrets.py set <id>")
    return 1 if faltan else 0


def plan_migracion(data):
    plan = [(e, c, v, refs.default_id(e["name"], c)) for e, c, v in campos(data) if not refs.is_ref(v)]
    ids = [rid for *_, rid in plan]
    repetidos = sorted({rid for rid in ids if ids.count(rid) > 1})
    if repetidos:
        raise Conflicto(f"ids repetidos (¿MCPs con el mismo name?): {', '.join(repetidos)}")
    return plan


def cmd_migrate(store, secrets_file, dry_run):
    bak = secrets_file + ".bak"
    data = leer_secrets(secrets_file)
    try:
        plan = plan_migracion(data)
    except Conflicto as exc:
        print(f"ERROR: {exc}")
        return 1
    if not plan:
        print("Nada que migrar: secrets.json no tiene claves en texto plano.")
        return 0

    # Pre-pasada de solo lectura: si el llavero ya tiene OTRO valor con ese id, no se
    # pisa en silencio. Mismo valor = una migración anterior que quedó a medias: OK.
    estados = {}
    try:
        for _, _, valor, rid in plan:
            actual = store.get(rid)
            estados[rid] = "nueva" if actual is None else ("ya está" if actual == valor else "CONFLICTO")
    except Exception as exc:
        return falla(exc, rid)

    for entry, campo, _, rid in plan:
        print(f"  {entry['name']}.{campo}  → keyring:{rid}   [{estados[rid]}]")
    conflictos = [rid for rid, est in estados.items() if est == "CONFLICTO"]
    print()
    if conflictos:
        print(f"ERROR: el llavero ya tiene un valor DISTINTO para: {', '.join(conflictos)}")
        print("       Revisalo (o borralo con `delete`) antes de migrar. No se tocó nada.")
        return 1
    if dry_run:
        print(f"{len(plan)} clave(s) se pasarían al llavero. (--dry-run: no se escribió nada)")
        return 0

    if os.path.exists(bak):
        print(f"ERROR: ya existe {bak}: hay una migración sin finalizar.")
        print("       Verificá con `check` y cerrala con `migrate --finalize`.")
        return 1
    with open(secrets_file, "rb") as f:
        original = f.read()
    escribir_respaldo(bak, original)

    try:
        for entry, campo, valor, rid in plan:
            store.set(rid, valor)
            if store.get(rid) != valor:
                raise Conflicto(f"keyring:{rid} no se leyó igual después de guardarla")
    except Exception as exc:
        os.unlink(bak)  # secrets.json sigue intacto: el respaldo sobra
        if isinstance(exc, Conflicto):
            print(f"ERROR: {exc}. secrets.json no se modificó.")
            return 1
        print("secrets.json no se modificó.")
        return falla(exc, rid)

    for entry, campo, _, rid in plan:
        entry[campo] = refs.make_ref(rid)
    escribir_secrets(secrets_file, data)

    print(f"{len(plan)} clave(s) pasadas al llavero y verificadas. secrets.json reescrito (0600).")
    print(f"Respaldo con los valores viejos: {bak} (0600).")
    print("OJO: hasta la fase 2, add-mcp-to-project.py se niega a registrar MCPs con referencias.")
    print("Cuando todo funcione: mcp-secrets.py migrate --finalize")
    return 0


def cmd_finalize(store, secrets_file, yes):
    bak = secrets_file + ".bak"
    if not os.path.exists(bak):
        print("No hay respaldo que borrar.")
        return 0
    data = leer_secrets(secrets_file)
    planos = [f"{e['name']}.{c}" for e, c, v in campos(data) if not refs.is_ref(v)]
    if planos:
        print(f"ERROR: secrets.json todavía tiene texto plano ({', '.join(planos)}). No se borra el respaldo.")
        return 1
    for _, _, v in campos(data):
        rid = refs.ref_id(v)
        try:
            if store.get(rid) is None:
                print(f"ERROR: keyring:{rid} no está en el llavero: el respaldo es la única copia. No se borra.")
                return 1
        except Exception as exc:
            return falla(exc, rid)
    if not confirmar(f"Todas las referencias resuelven. Borrar {bak}.", yes):
        print("Cancelado.")
        return 1
    os.unlink(bak)
    print(f"{bak} borrado.")
    return 0


def main(argv=None, store=None, secrets_file=SECRETS_FILE):
    argv = list(sys.argv[1:] if argv is None else argv)
    yes = "--yes" in argv or "-y" in argv
    flags = {"--yes", "-y", "--dry-run", "--finalize"}
    uso = __doc__.strip().split("\n\n")[1]
    if not argv or "-h" in argv or "--help" in argv:
        print(uso)
        return 0
    pos = [a for a in argv if a not in flags]
    desconocidas = [a for a in pos if a.startswith("-")]
    if desconocidas:
        print(f"ERROR: opción desconocida: {' '.join(desconocidas)}\n\n{uso}")
        return 1

    cmd, args = pos[0], pos[1:]
    con_id = {"set", "delete"}
    if cmd in con_id and len(args) != 1:
        print(f"ERROR: `{cmd}` lleva exactamente un <id> (ej. postgres-gz-prod/password).")
        return 1
    if cmd not in con_id | {"list", "check", "migrate"}:
        print(f"ERROR: comando desconocido '{cmd}'.")
        return 1
    if cmd in ("check", "migrate") and not os.path.exists(secrets_file):
        print(f"ERROR: no existe {secrets_file}.")
        return 1

    if store is None:
        try:
            store = Llavero()
        except ImportError:
            venv_py = (os.path.join(SKILLS_DIR, ".venv", "Scripts", "python.exe") if os.name == "nt"
                       else os.path.join(SKILLS_DIR, ".venv", "bin", "python"))
            print("ERROR: falta el paquete `keyring` en este Python. Usá el del venv:")
            print(f'       "{venv_py}" "{os.path.abspath(__file__)}" ...')
            return 1

    if cmd == "set":
        return cmd_set(store, args[0])
    if cmd == "delete":
        return cmd_delete(store, args[0], yes, secrets_file)
    if cmd == "list":
        return cmd_list(store, secrets_file)
    if cmd == "check":
        return cmd_check(store, secrets_file)
    if "--finalize" in argv:
        return cmd_finalize(store, secrets_file, yes)
    return cmd_migrate(store, secrets_file, "--dry-run" in argv)


if __name__ == "__main__":
    sys.exit(main())
