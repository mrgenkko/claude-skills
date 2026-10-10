#!/usr/bin/env python3
"""
Tests de mcp-launch.py — con un lector de llavero inyectado, sin `keyring`.

    python3 -m unittest discover -s scripts -p "test_*.py" -v

Los casos de falla son los que midió la fase 0 (pendientes/llavero-fase0.md):
llavero bloqueado sin nadie frente a la pantalla = espera infinita, así que el
timeout no es opcional.
"""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

AQUI = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("mcp_launch", os.path.join(AQUI, "mcp-launch.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

VALORES = {"gh/token": "VALOR-gh", "modal/id": "VALOR-ak", "focusyn/agent_key": "VALOR-agent"}


class KeyringLocked(Exception):
    pass


class NoKeyringError(Exception):
    pass


def leer_falso(rid):
    return VALORES.get(rid)


class Base(unittest.TestCase):
    def setUp(self):
        # El lock real va en XDG_RUNTIME_DIR; acá, en un directorio propio.
        self.tmp = tempfile.TemporaryDirectory()
        self._xdg = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = self.tmp.name

    def tearDown(self):
        if self._xdg is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = self._xdg
        self.tmp.cleanup()

    def correr(self, *argv, leer=leer_falso):
        """main() con exec capturado; devuelve (rc, stdout, stderr, exec) y verifica que stderr no filtra."""
        capturado = {}

        def exec_falso(resto, env):
            capturado.update(resto=resto, env=env)
            return 0

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = mod.main(list(argv), leer=leer, exec_fn=exec_falso, out=out)
        for v in VALORES.values():
            self.assertNotIn(v, err.getvalue())
        return rc, out.getvalue(), err.getvalue(), capturado


class TestStdio(Base):
    def test_pone_las_claves_en_env_y_lanza_el_server(self):
        rc, _, _, ex = self.correr("--secret", "GH_TOKEN=keyring:gh/token",
                                   "--secret", "MODAL_TOKEN_ID=keyring:modal/id",
                                   "--", "/srv/server.py", "--owner=o")
        self.assertEqual(rc, 0)
        self.assertEqual(ex["env"], {"GH_TOKEN": "VALOR-gh", "MODAL_TOKEN_ID": "VALOR-ak"})
        self.assertEqual(ex["resto"], ["/srv/server.py", "--owner=o"])

    def test_args_del_server_con_guiones_no_se_confunden(self):
        rc, _, _, ex = self.correr("--secret", "X=keyring:gh/token", "--", "s.py", "--secret", "--timeout")
        self.assertEqual(rc, 0)
        self.assertEqual(ex["resto"], ["s.py", "--secret", "--timeout"])

    def test_clave_inexistente_no_lanza(self):
        rc, _, err, ex = self.correr("--secret", "X=keyring:no/existe", "--", "s.py")
        self.assertEqual(rc, 1)
        self.assertEqual(ex, {})
        self.assertIn("mcp-secrets.py set no/existe", err)

    def test_llavero_bloqueado_o_cancelado(self):
        def leer(rid):
            raise KeyringLocked("Failed to unlock the collection!")
        rc, _, err, ex = self.correr("--secret", "X=keyring:gh/token", "--", "s.py", leer=leer)
        self.assertEqual(rc, 1)
        self.assertEqual(ex, {})
        self.assertIn("bloqueado", err)

    def test_sin_sesion_grafica(self):
        def leer(rid):
            raise NoKeyringError("No recommended backend was available")
        rc, _, err, _ = self.correr("--secret", "X=keyring:gh/token", "--", "s.py", leer=leer)
        self.assertEqual(rc, 1)
        self.assertIn("SSH", err)

    def test_nadie_desbloquea_corta_por_timeout(self):
        """Fase 0: sin nadie frente a la ventana, keyring espera para siempre."""
        liberar = threading.Event()

        def leer(rid):
            liberar.wait(10)
            return "x"
        t0 = time.monotonic()
        rc, _, err, ex = self.correr("--timeout", "0.3", "--secret", "X=keyring:gh/token", "--", "s.py",
                                     leer=leer)
        liberar.set()
        self.assertLess(time.monotonic() - t0, 3)
        self.assertEqual(rc, 1)
        self.assertEqual(ex, {})
        self.assertIn("nadie desbloqueó", err)

    def test_errores_de_uso(self):
        for argv in (["--", "s.py"],                                  # sin claves
                     ["--secret", "X=keyring:gh/token"],              # sin server
                     ["--secret", "X=texto-plano", "--", "s.py"],     # no es referencia
                     ["--secret", "X=keyring:", "--", "s.py"],        # id vacío
                     ["--otro", "--", "s.py"]):
            with self.subTest(argv=argv):
                rc, _, _, ex = self.correr(*argv)
                self.assertEqual(rc, 2)
                self.assertEqual(ex, {})


class TestRevision(Base):
    """Hallazgos del code review: nunca lanzar un server con una clave de menos."""

    def test_si_el_lock_falla_lee_igual(self):
        # Un lock de otro usuario / ruta no escribible: el lock no protege nada,
        # no puede impedir que el MCP arranque.
        original = mod._ruta_lock
        mod._ruta_lock = lambda: "/proc/no-se-puede-crear/lock"
        try:
            rc, _, err, ex = self.correr("--secret", "GH_TOKEN=keyring:gh/token", "--", "s.py")
        finally:
            mod._ruta_lock = original
        self.assertEqual(rc, 0, err)
        self.assertEqual(ex["env"], {"GH_TOKEN": "VALOR-gh"})

    def test_falla_inesperada_en_el_hilo_no_lanza(self):
        original = mod._con_lock
        def roto(fn):
            raise PermissionError("x")
        mod._con_lock = roto
        try:
            rc, _, err, ex = self.correr("--secret", "GH_TOKEN=keyring:gh/token", "--", "s.py")
        finally:
            mod._con_lock = original
        self.assertEqual(rc, 1)
        self.assertEqual(ex, {})
        self.assertIn("PermissionError", err)

    def test_clave_sin_resolver_y_sin_error_no_lanza(self):
        # El hilo termina "bien" pero sin haber leído nada: igual no se lanza.
        original = mod._con_lock
        mod._con_lock = lambda fn: None
        try:
            rc, _, err, ex = self.correr("--secret", "GH_TOKEN=keyring:gh/token", "--", "s.py")
        finally:
            mod._con_lock = original
        self.assertEqual(rc, 1)
        self.assertEqual(ex, {})
        self.assertIn("GH_TOKEN", err)


class TestHeaders(Base):
    def test_imprime_solo_el_json(self):
        rc, out, _, ex = self.correr("headers", "X-Agent-Key=keyring:focusyn/agent_key")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {"X-Agent-Key": "VALOR-agent"})
        self.assertEqual(ex, {})

    def test_falla_sin_imprimir_nada_en_stdout(self):
        rc, out, _, _ = self.correr("headers", "X-Agent-Key=keyring:no/existe")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")

    def test_timeout_por_defecto_cabe_en_el_de_claude_code(self):
        # headersHelper de Claude Code tiene un tope de 10 s.
        self.assertLess(mod.TIMEOUT_HEADERS, 10)


@unittest.skipIf(os.name == "nt", "execv y /proc son POSIX")
class TestPuntaAPunta(unittest.TestCase):
    """El lanzador real como subproceso: exec de verdad, mismo PID, cmdline limpio."""

    def test_exec_real_conserva_pid_y_limpia_cmdline(self):
        with tempfile.TemporaryDirectory() as d:
            # Un "keyring" falso importable: el lanzador hace `import keyring` dentro de leer_llavero.
            with open(os.path.join(d, "keyring.py"), "w") as f:
                f.write("def get_password(s, u):\n    return {'gh/token': 'VALOR-gh'}.get(u)\n")
            server = os.path.join(d, "server.py")
            with open(server, "w") as f:
                f.write("import os, sys\n"
                        "print(os.getpid(), os.environ.get('GH_TOKEN') == 'VALOR-gh',\n"
                        "      open('/proc/self/cmdline','rb').read().replace(b'\\0', b' ').decode().strip())\n")
            lanzador = os.path.join(AQUI, "mcp-launch.py")
            env = {**os.environ, "PYTHONPATH": d, "XDG_RUNTIME_DIR": d}
            p = subprocess.Popen([sys.executable, lanzador, "--secret", "GH_TOKEN=keyring:gh/token", "--", server],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            salida, error = p.communicate(timeout=30)
            self.assertEqual(p.returncode, 0, error)
            pid, llego, cmdline = salida.strip().split(" ", 2)
            self.assertEqual(int(pid), p.pid, "el server tiene que ser el MISMO proceso que lanzó el cliente")
            self.assertEqual(llego, "True")
            self.assertNotIn("keyring", cmdline)
            self.assertNotIn("VALOR", cmdline)
            self.assertTrue(cmdline.endswith("server.py"))


if __name__ == "__main__":
    unittest.main()
