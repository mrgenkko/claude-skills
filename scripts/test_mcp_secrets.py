#!/usr/bin/env python3
"""
Tests de mcp-secrets.py y secret_refs.py — con un llavero en memoria.

Stdlib puro, como test_add_mcp_to_project.py: no hace falta `keyring` instalado.

    python3 -m unittest discover -s scripts -p "test_*.py" -v

El invariante que importa: NINGÚN valor secreto aparece jamás en la salida. Cada
caso lo verifica (Base.salida) además de lo suyo.
"""

import contextlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
import secret_refs as refs  # noqa: E402


def cargar(nombre, archivo):
    """Los scripts tienen guiones en el nombre: no son importables con `import`."""
    spec = importlib.util.spec_from_file_location(nombre, os.path.join(AQUI, archivo))
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


mod = cargar("mcp_secrets", "mcp-secrets.py")


class KeyringLocked(Exception):
    """Mismo nombre que la de keyring: explain() clasifica por nombre de clase."""


class NoKeyringError(Exception):
    pass


class LlaveroFalso:
    def __init__(self, inicial=None, falla_en_set=None, set_corrupto=False, ids_soportado=True):
        self.datos = dict(inicial or {})
        self.falla_en_set = falla_en_set      # (n, excepción): falla en el set número n (1-based)
        self.set_corrupto = set_corrupto      # guarda otra cosa: la verificación tiene que fallar
        self.ids_soportado = ids_soportado
        self.sets = 0

    def get(self, rid):
        return self.datos.get(rid)

    def set(self, rid, valor):
        self.sets += 1
        if self.falla_en_set and self.sets == self.falla_en_set[0]:
            raise self.falla_en_set[1]
        self.datos[rid] = valor + "-corrupto" if self.set_corrupto else valor

    def delete(self, rid):
        del self.datos[rid]

    def ids(self):
        return sorted(self.datos) if self.ids_soportado else None


SECRETOS = {
    "postgres-a.password": "VALOR-pg-a",
    "ssh-a.password": "VALOR-ssh-pw",
    "ssh-a.sudo_password": "VALOR-ssh-sudo",
    "gh-a.token": "VALOR-ghp",
    "modal-a.token_id": "VALOR-ak",
    "modal-a.token_secret": "VALOR-as",
    "focusyn.agent_key": "VALOR-agent",
}


def secrets_demo():
    return {"mcp_servers": [
        {"name": "gcloud-a", "type": "gcloud", "key_file": "/keys/a.json", "project": "p"},
        {"name": "postgres-a", "type": "postgres", "host": "h", "password": "VALOR-pg-a"},
        {"name": "redis-sin-clave", "type": "redis", "host": "h"},
        {"name": "ssh-a", "type": "ssh", "host": "h", "password": "VALOR-ssh-pw",
         "sudo_password": "VALOR-ssh-sudo"},
        {"name": "gh-a", "type": "gh", "owner": "o", "token": "VALOR-ghp"},
        {"name": "modal-a", "type": "modal", "workspace": "w", "token_id": "VALOR-ak",
         "token_secret": "VALOR-as"},
        {"name": "focusyn", "type": "focusyn", "url": "https://x/mcp", "agent_key": "VALOR-agent"},
        {"name": "webprobe", "type": "webprobe"},
    ]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "secrets.json")
        self.escribir(secrets_demo())

    def tearDown(self):
        self.tmp.cleanup()

    def escribir(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.chmod(self.path, 0o644)

    def leer(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def bytes_(self):
        with open(self.path, "rb") as f:
            return f.read()

    def correr(self, *argv, store=None, entrada=None):
        """Corre main(); devuelve (exit, salida) y verifica que no se filtró ningún valor."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            if entrada is not None:
                with mock.patch.object(mod.getpass, "getpass", return_value=entrada):
                    rc = mod.main(list(argv), store=store, secrets_file=self.path)
            else:
                rc = mod.main(list(argv), store=store, secrets_file=self.path)
        salida = out.getvalue()
        for valor in [*SECRETOS.values(), *(entrada and [entrada] or [])]:
            self.assertNotIn(valor, salida, f"se filtró un valor en la salida de {argv}")
        return rc, salida


class TestMigrate(Base):
    def test_dry_run_no_escribe_nada(self):
        antes, store = self.bytes_(), LlaveroFalso()
        rc, salida = self.correr("migrate", "--dry-run", store=store)
        self.assertEqual(rc, 0)
        self.assertEqual(self.bytes_(), antes)
        self.assertEqual(store.datos, {})
        self.assertFalse(os.path.exists(self.path + ".bak"))
        self.assertIn("keyring:postgres-a/password", salida)
        self.assertIn("7 clave(s)", salida)

    def test_migra_todo_a_referencias_una_por_instancia(self):
        store = LlaveroFalso()
        rc, _ = self.correr("migrate", store=store)
        self.assertEqual(rc, 0)
        esperado = {refs.default_id(*k.split(".")): v for k, v in SECRETOS.items()}
        self.assertEqual(store.datos, esperado)
        data = self.leer()
        por_nombre = {e["name"]: e for e in data["mcp_servers"]}
        self.assertEqual(por_nombre["postgres-a"]["password"], "keyring:postgres-a/password")
        self.assertEqual(por_nombre["ssh-a"]["sudo_password"], "keyring:ssh-a/sudo_password")
        # Lo que no es secreto queda igual.
        self.assertEqual(por_nombre["gcloud-a"]["key_file"], "/keys/a.json")
        self.assertEqual(por_nombre["postgres-a"]["host"], "h")
        self.assertNotIn("password", por_nombre["redis-sin-clave"])
        self.assertNotIn(b"VALOR-", self.bytes_())

    @unittest.skipIf(os.name == "nt", "permisos POSIX")
    def test_respaldo_y_secrets_quedan_0600(self):
        original = self.bytes_()
        self.correr("migrate", store=LlaveroFalso())
        bak = self.path + ".bak"
        self.assertEqual(stat.S_IMODE(os.stat(bak).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        with open(bak, "rb") as f:
            self.assertEqual(f.read(), original)

    def test_idempotente(self):
        store = LlaveroFalso()
        self.correr("migrate", store=store)
        rc, salida = self.correr("migrate", store=store)
        self.assertEqual(rc, 0)
        self.assertIn("Nada que migrar", salida)

    def test_no_pisa_un_valor_distinto_del_llavero(self):
        antes = self.bytes_()
        store = LlaveroFalso({"gh-a/token": "OTRO-valor"})
        rc, salida = self.correr("migrate", store=store)
        self.assertEqual(rc, 1)
        self.assertIn("CONFLICTO", salida)
        self.assertEqual(self.bytes_(), antes)
        self.assertEqual(store.datos, {"gh-a/token": "OTRO-valor"})
        self.assertFalse(os.path.exists(self.path + ".bak"))

    def test_mismo_valor_previo_es_ok(self):
        store = LlaveroFalso({"gh-a/token": "VALOR-ghp"})
        rc, salida = self.correr("migrate", store=store)
        self.assertEqual(rc, 0)
        self.assertIn("[ya está]", salida)

    def test_falla_del_llavero_a_mitad_no_toca_secrets(self):
        antes = self.bytes_()
        store = LlaveroFalso(falla_en_set=(3, KeyringLocked("x")))
        rc, salida = self.correr("migrate", store=store)
        self.assertEqual(rc, 1)
        self.assertIn("bloqueado", salida)
        self.assertEqual(self.bytes_(), antes)
        self.assertFalse(os.path.exists(self.path + ".bak"), "el respaldo sobra si secrets.json no cambió")

    def test_verificacion_fallida_aborta(self):
        antes = self.bytes_()
        rc, salida = self.correr("migrate", store=LlaveroFalso(set_corrupto=True))
        self.assertEqual(rc, 1)
        self.assertIn("no se leyó igual", salida)
        self.assertEqual(self.bytes_(), antes)

    def test_aborta_si_quedo_un_respaldo(self):
        with open(self.path + ".bak", "w") as f:
            f.write("{}")
        antes = self.bytes_()
        rc, salida = self.correr("migrate", store=LlaveroFalso())
        self.assertEqual(rc, 1)
        self.assertIn("sin finalizar", salida)
        self.assertEqual(self.bytes_(), antes)

    def test_nombres_repetidos_abortan(self):
        data = secrets_demo()
        data["mcp_servers"].append({"name": "gh-a", "type": "gh", "token": "VALOR-ghp"})
        self.escribir(data)
        rc, salida = self.correr("migrate", "--dry-run", store=LlaveroFalso())
        self.assertEqual(rc, 1)
        self.assertIn("repetidos", salida)


class TestFinalize(Base):
    def migrado(self):
        store = LlaveroFalso()
        self.correr("migrate", store=store)
        return store

    def test_borra_el_respaldo_si_todo_resuelve(self):
        store = self.migrado()
        rc, _ = self.correr("migrate", "--finalize", "--yes", store=store)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.path + ".bak"))

    def test_no_borra_si_falta_una_clave(self):
        store = self.migrado()
        del store.datos["gh-a/token"]
        rc, salida = self.correr("migrate", "--finalize", "--yes", store=store)
        self.assertEqual(rc, 1)
        self.assertIn("única copia", salida)
        self.assertTrue(os.path.exists(self.path + ".bak"))

    def test_no_borra_si_queda_texto_plano(self):
        store = self.migrado()
        data = self.leer()
        data["mcp_servers"][1]["password"] = "VALOR-pg-a"
        self.escribir(data)
        rc, _ = self.correr("migrate", "--finalize", "--yes", store=store)
        self.assertEqual(rc, 1)
        self.assertTrue(os.path.exists(self.path + ".bak"))

    def test_sin_tty_ni_yes_no_borra(self):
        store = self.migrado()
        with mock.patch.object(mod.sys.stdin, "isatty", return_value=False):
            rc, _ = self.correr("migrate", "--finalize", store=store)
        self.assertEqual(rc, 1)
        self.assertTrue(os.path.exists(self.path + ".bak"))


class TestCheck(Base):
    def test_ok_falta_y_plano(self):
        store = LlaveroFalso()
        self.correr("migrate", store=store)
        del store.datos["modal-a/token_secret"]
        data = self.leer()
        data["mcp_servers"][4]["token"] = "VALOR-ghp"  # gh-a vuelve a texto plano
        self.escribir(data)
        rc, salida = self.correr("check", store=store)
        self.assertEqual(rc, 1)
        self.assertIn("FALTA   modal-a.token_secret", salida)
        self.assertIn("OK      postgres-a.password", salida)
        self.assertIn("PLANO   gh-a.token", salida)
        self.assertIn("1 faltante(s), 1 en texto plano", salida)

    def test_todo_ok_sale_0(self):
        store = LlaveroFalso()
        self.correr("migrate", store=store)
        rc, _ = self.correr("check", store=store)
        self.assertEqual(rc, 0)

    def test_sin_dbus_explica_el_porque(self):
        data = self.leer()
        data["mcp_servers"][1]["password"] = "keyring:postgres-a/password"
        self.escribir(data)

        class SinDbus(LlaveroFalso):
            def get(self, rid):
                raise NoKeyringError("No recommended backend was available")

        rc, salida = self.correr("check", store=SinDbus())
        self.assertEqual(rc, 1)
        self.assertIn("SSH", salida)


class TestSetDeleteList(Base):
    def test_set_guarda_y_no_muestra_el_valor(self):
        store = LlaveroFalso()
        rc, salida = self.correr("set", "gh-a/token", store=store, entrada="nuevo-TOKEN-xyz")
        self.assertEqual(rc, 0)
        self.assertEqual(store.datos, {"gh-a/token": "nuevo-TOKEN-xyz"})
        self.assertIn("guardada (15 caracteres)", salida)

    def test_set_reemplaza(self):
        store = LlaveroFalso({"gh-a/token": "viejo"})
        rc, salida = self.correr("set", "gh-a/token", store=store, entrada="nuevo-TOKEN-xyz")
        self.assertEqual(rc, 0)
        self.assertIn("reemplazada", salida)

    def test_set_vacio_no_guarda(self):
        store = LlaveroFalso()
        rc, _ = self.correr("set", "gh-a/token", store=store, entrada="")
        self.assertEqual(rc, 1)
        self.assertEqual(store.datos, {})

    def test_set_sin_id_es_error(self):
        rc, _ = self.correr("set", store=LlaveroFalso())
        self.assertEqual(rc, 1)

    def test_delete_avisa_si_esta_referenciada(self):
        store = LlaveroFalso()
        self.correr("migrate", store=store)
        rc, salida = self.correr("delete", "gh-a/token", "--yes", store=store)
        self.assertEqual(rc, 0)
        self.assertIn("referenciada", salida)
        self.assertNotIn("gh-a/token", store.datos)

    def test_list_marca_huerfanas(self):
        store = LlaveroFalso()
        self.correr("migrate", store=store)
        store.datos["vieja/clave"] = "x"
        rc, salida = self.correr("list", store=store)
        self.assertEqual(rc, 0)
        self.assertIn("vieja/clave   (huérfana", salida)
        self.assertIn("8 clave(s)", salida)

    def test_list_sin_soporte_deriva_a_check(self):
        rc, salida = self.correr("list", store=LlaveroFalso(ids_soportado=False))
        self.assertEqual(rc, 0)
        self.assertIn("check", salida)


class TestCli(Base):
    def test_opcion_desconocida(self):
        rc, salida = self.correr("migrate", "--dryrun", store=LlaveroFalso())
        self.assertEqual(rc, 1)
        self.assertIn("desconocida", salida)

    def test_ayuda(self):
        rc, salida = self.correr("--help", store=LlaveroFalso())
        self.assertEqual(rc, 0)
        self.assertIn("migrate --dry-run", salida)

    def test_comando_desconocido(self):
        rc, _ = self.correr("borrar", store=LlaveroFalso())
        self.assertEqual(rc, 1)


class TestSecretRefs(unittest.TestCase):
    def test_referencias(self):
        self.assertTrue(refs.is_ref("keyring:a/b"))
        self.assertFalse(refs.is_ref("a/b"))
        self.assertFalse(refs.is_ref(None))
        self.assertEqual(refs.ref_id(refs.make_ref("a/b")), "a/b")

    def test_key_file_de_gcloud_no_es_secreto(self):
        self.assertEqual(refs.secret_fields({"type": "gcloud"}), ())

    def test_explain_no_incluye_el_mensaje_de_la_excepcion(self):
        # El texto de una excepción ajena podría traer cualquier cosa: solo el tipo.
        msg = refs.explain(RuntimeError("VALOR-filtrado"), "x/y")
        self.assertNotIn("VALOR-filtrado", msg)
        self.assertIn("RuntimeError", msg)


class TestGuardiaDeAddMcp(unittest.TestCase):
    """Hasta la fase 2, add-mcp-to-project.py no puede registrar referencias."""

    @classmethod
    def setUpClass(cls):
        cls.add = cargar("add_mcp_to_project_guardia", "add-mcp-to-project.py")

    def test_aborta_si_un_mcp_elegido_tiene_referencia(self):
        cfg = [{"name": "pg", "type": "postgres", "password": "keyring:pg/password"}]
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit):
            self.add.abortar_si_hay_refs_al_llavero(cfg, ["pg"])
        self.assertIn("pg.password", out.getvalue())

    def test_deja_pasar_si_no_esta_elegido_o_es_plano(self):
        cfg = [{"name": "pg", "type": "postgres", "password": "keyring:pg/password"},
               {"name": "gh", "type": "gh", "token": "plano"},
               {"name": "webprobe", "type": "webprobe"}]
        self.add.abortar_si_hay_refs_al_llavero(cfg, ["gh", "webprobe"])  # no sale


if __name__ == "__main__":
    unittest.main()
