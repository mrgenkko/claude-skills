#!/usr/bin/env python3
"""
Tests de los servers postgres / redis / ssh: la clave llega por env (fase 3).

    .venv/bin/python -m unittest discover -s scripts -p "test_*.py" -v

A diferencia del resto de scripts/test_*.py, estos necesitan las dependencias
de cada server (psycopg2, redis, paramiko, mcp): con un Python sin ellas se
saltean. Cada caso carga el server en un subproceso con run_name distinto de
"__main__" —parsea los argumentos sin arrancar el loop MCP— y reporta lo que
quedó en `args` y en el entorno. Ningún caso se conecta a nada.
"""

import importlib.util
import json
import os
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOYED = os.path.join(REPO, "deployed")

SONDA = r"""
import json, os, runpy, sys
server, campos, argv = sys.argv[1], sys.argv[2].split(","), sys.argv[3:]
sys.argv = [server, *argv]
g = runpy.run_path(server, run_name="sonda")
print(json.dumps({"args": {c: getattr(g["args"], c) for c in campos},
                  "env": sorted(k for k in os.environ if k.startswith("MCP_"))}))
"""


def disponible(*modulos):
    return all(importlib.util.find_spec(m) for m in modulos)


def sondear(server, campos, argv, env_extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("MCP_")}
    env.update(env_extra)
    p = subprocess.run([sys.executable, "-c", SONDA, os.path.join(DEPLOYED, server, "server.py"),
                        ",".join(campos), *argv],
                       capture_output=True, text=True, env=env, timeout=60, stdin=subprocess.DEVNULL)
    return p.returncode, (json.loads(p.stdout) if p.returncode == 0 else None), p.stderr


class Comun:
    """Casos compartidos; cada subclase fija server, campos y argv mínimos."""
    server = campos = argv = None
    var = "MCP_PASSWORD"

    def test_clave_por_env_y_no_queda_en_el_entorno(self):
        rc, d, err = sondear(self.server, self.campos, self.argv, {self.var: "desde-env"})
        self.assertEqual(rc, 0, err)
        self.assertEqual(d["args"]["password"], "desde-env")
        self.assertEqual(d["env"], [], "la variable tiene que borrarse apenas se lee")

    def test_el_flag_gana_y_la_variable_igual_se_borra(self):
        rc, d, err = sondear(self.server, self.campos, [*self.argv, "--password=desde-flag"],
                             {self.var: "desde-env"})
        self.assertEqual(rc, 0, err)
        self.assertEqual(d["args"]["password"], "desde-flag")
        self.assertEqual(d["env"], [])


@unittest.skipUnless(disponible("psycopg2", "mcp"), "faltan las deps de postgres (usar el venv)")
class TestPostgres(Comun, unittest.TestCase):
    server, campos, argv = "postgres", ["password"], ["--host=h", "--db=d", "--user=u"]

    def test_password_vacia_es_valida(self):
        """auth trust o .pgpass: --password= vacío no puede caer al error."""
        rc, d, err = sondear(self.server, self.campos, [*self.argv, "--password="], {})
        self.assertEqual(rc, 0, err)
        self.assertEqual(d["args"]["password"], "")

    def test_sin_clave_falla_con_mensaje(self):
        rc, _, err = sondear(self.server, self.campos, self.argv, {})
        self.assertEqual(rc, 2)
        self.assertIn("MCP_PASSWORD", err)


@unittest.skipUnless(disponible("redis", "mcp"), "faltan las deps de redis (usar el venv)")
class TestRedis(Comun, unittest.TestCase):
    server, campos, argv = "redis", ["password"], ["--host=h"]

    def test_sin_clave_sigue_siendo_opcional(self):
        rc, d, err = sondear(self.server, self.campos, self.argv, {})
        self.assertEqual(rc, 0, err)
        self.assertIsNone(d["args"]["password"])


@unittest.skipUnless(disponible("paramiko", "mcp"), "faltan las deps de ssh (usar el venv)")
class TestSsh(Comun, unittest.TestCase):
    server, campos, argv = "ssh", ["password", "sudo_password"], ["--host=h", "--user=u"]

    def test_sudo_por_env(self):
        rc, d, err = sondear(self.server, self.campos, self.argv,
                             {"MCP_PASSWORD": "pw", "MCP_SUDO_PASSWORD": "sudo"})
        self.assertEqual(rc, 0, err)
        self.assertEqual(d["args"], {"password": "pw", "sudo_password": "sudo"})
        self.assertEqual(d["env"], [])


if __name__ == "__main__":
    unittest.main()
