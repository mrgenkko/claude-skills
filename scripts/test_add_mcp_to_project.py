#!/usr/bin/env python3
"""
Tests de add-mcp-to-project.py — portabilidad Windows/POSIX y no-regresión.

Sin dependencias: stdlib puro, corre igual en Linux, macOS y Windows.

    python3 -m unittest discover -s scripts -p "test_*.py" -v

El caso más importante es test_env_no_hereda_el_entorno: el `env` que este script
escribe en ~/.claude.json GANA sobre el entorno que hereda el server (el cliente
MCP spawnea con {...whitelist, ...env_declarado}), así que volcar os.environ ahí
filtra secretos a disco y pisa los overrides deliberados.
"""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
import unittest.mock
from unittest import mock

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "add-mcp-to-project.py")


def cargar_modulo():
    """El script tiene guiones en el nombre: no es importable con `import`."""
    spec = importlib.util.spec_from_file_location("add_mcp_to_project", SCRIPT)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


mod = cargar_modulo()

# Un entry de cada tipo soportado, con los campos obligatorios.
SECRETS_COMPLETO = [
    {"name": "gcloud-demo", "type": "gcloud", "project": "p", "region": "us-central1",
     "workdir": "/w", "account": "a@b.com", "config_dir": "~/.config/gcloud-demo"},
    {"name": "gcloud-sin-configdir", "type": "gcloud", "project": "p", "region": "us-central1",
     "workdir": "/w", "account": "a@b.com"},
    {"name": "postgres-demo", "type": "postgres", "host": "h", "port": 5432,
     "db": "d", "user": "u", "password": "pw"},
    {"name": "ssh-demo", "type": "ssh", "host": "h", "user": "u", "key_file": "~/k"},
    {"name": "redis-demo", "type": "redis", "host": "h"},
    {"name": "gh-demo", "type": "gh", "owner": "o", "token": "ghp_desde_secrets"},
    {"name": "obsidian-demo", "type": "obsidian", "vault_path": "/v"},
    {"name": "webprobe", "type": "webprobe"},
    {"name": "focusyn", "type": "focusyn", "url": "https://x/mcp", "agent_key": "k"},
]

# Entorno sucio: snap de VSCode + secretos sueltos, como el de una terminal real.
ENTORNO_SUCIO = {
    "GIO_MODULE_DIR": "/home/u/snap/code/common/.cache/gio-modules",
    "LD_LIBRARY_PATH": "/snap/code/current/usr/lib",
    "LOCPATH": "/snap/code/current/usr/lib/locale",
    "GTK_PATH": "/snap/code/current/usr/lib/gtk-3.0",
    "GH_TOKEN": "ghp_del_entorno_NO_debe_filtrarse",
    "AWS_SECRET_ACCESS_KEY": "secreto_NO_debe_filtrarse",
    "CLOUDSDK_CONFIG": "/home/u/.config/gcloud-ajeno",
}


class TestEnvNoSeContamina(unittest.TestCase):
    """Regresión: el env declarado gana sobre el heredado, así que debe ser mínimo."""

    def test_env_no_hereda_el_entorno(self):
        with mock.patch.dict(os.environ, ENTORNO_SUCIO):
            servers = mod.build_mcp_servers(SECRETS_COMPLETO)
        for nombre, cfg in servers.items():
            env = cfg.get("env", {})
            with self.subTest(mcp=nombre):
                self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
                self.assertNotIn("LD_LIBRARY_PATH", env)
                self.assertNotIn("LOCPATH", env)
                self.assertNotIn("GTK_PATH", env)
                # Cota dura: solo overrides deliberados, nunca decenas de vars.
                self.assertLessEqual(len(env), 3, f"env inflado en {nombre}: {sorted(env)}")

    def test_gh_token_sale_de_secrets_no_del_entorno(self):
        with mock.patch.dict(os.environ, ENTORNO_SUCIO):
            servers = mod.build_mcp_servers(SECRETS_COMPLETO)
        self.assertEqual(servers["gh-demo"]["env"]["GH_TOKEN"], "ghp_desde_secrets")

    def test_gcloud_sin_config_dir_no_hereda_cloudsdk_config(self):
        """Heredarlo haría derivar la cuenta activa entre instancias dev/prod."""
        with mock.patch.dict(os.environ, ENTORNO_SUCIO):
            servers = mod.build_mcp_servers(SECRETS_COMPLETO)
        self.assertNotIn("CLOUDSDK_CONFIG", servers["gcloud-sin-configdir"]["env"])
        self.assertTrue(
            servers["gcloud-demo"]["env"]["CLOUDSDK_CONFIG"].endswith("gcloud-demo")
        )


class TestGioModuleDir(unittest.TestCase):
    def test_apunta_al_sistema_pese_al_snap(self):
        """Sin esto, webkit muere con 'undefined symbol: __libc_pthread_init'."""
        with mock.patch.object(os, "name", "posix"), \
             mock.patch.dict(os.environ, ENTORNO_SUCIO):
            servers = mod.build_mcp_servers(SECRETS_COMPLETO)
        self.assertEqual(
            servers["webprobe"]["env"]["GIO_MODULE_DIR"], mod.GIO_MODULE_DIR_SYS
        )

    def test_no_se_setea_en_windows(self):
        """La ruta es de Linux; en Windows GIO no interviene."""
        with mock.patch.object(os, "name", "nt"):
            servers = mod.build_mcp_servers(SECRETS_COMPLETO)
        self.assertNotIn("GIO_MODULE_DIR", servers["webprobe"]["env"])


class TestRutasPorPlataforma(unittest.TestCase):
    def test_venv_python_en_posix(self):
        with mock.patch.object(os, "name", "posix"):
            self.assertTrue(
                mod.venv_python().endswith(os.path.join(".venv", "bin", "python"))
            )

    def test_venv_python_en_windows(self):
        with mock.patch.object(os, "name", "nt"):
            self.assertTrue(
                mod.venv_python().endswith(os.path.join(".venv", "Scripts", "python.exe"))
            )

    def test_command_registrado_sigue_la_plataforma(self):
        with mock.patch.object(os, "name", "nt"):
            servers = mod.build_mcp_servers(SECRETS_COMPLETO)
        self.assertTrue(servers["postgres-demo"]["command"].endswith("python.exe"))

    def test_server_path_usa_separador_nativo(self):
        esperado = os.path.join(mod.MCP_SERVERS_DIR, "webprobe", "server.py")
        self.assertEqual(mod.server_path("webprobe", "server.py"), esperado)

    def test_rutas_base_sin_separadores_mezclados(self):
        """os.path.expanduser('~/x') deja '/' crudos en Windows; os.path.join no."""
        for ruta in (mod.CLAUDE_JSON, mod.MCP_SERVERS_DIR):
            with self.subTest(ruta=ruta):
                if os.name == "nt":
                    self.assertNotIn("/", ruta)
                else:
                    self.assertNotIn("\\", ruta)


class TestFormaDeLasEntradas(unittest.TestCase):
    def test_todos_los_tipos_producen_entradas_serializables(self):
        servers = mod.build_mcp_servers(SECRETS_COMPLETO)
        self.assertEqual(len(servers), len(SECRETS_COMPLETO))
        json.dumps(servers)  # explota si hay algo no serializable
        for nombre, cfg in servers.items():
            with self.subTest(mcp=nombre):
                self.assertIn(cfg["type"], ("stdio", "http"))
                if cfg["type"] != "stdio":
                    continue
                self.assertTrue(os.path.isabs(cfg["command"]))
                self.assertTrue(all(isinstance(a, str) for a in cfg["args"]))
                self.assertTrue(
                    all(isinstance(k, str) and isinstance(v, str)
                        for k, v in cfg["env"].items())
                )

    def test_focusyn_es_http_sin_command_ni_env(self):
        servers = mod.build_mcp_servers(SECRETS_COMPLETO)
        focusyn = servers["focusyn"]
        self.assertEqual(focusyn["type"], "http")
        self.assertNotIn("command", focusyn)
        self.assertNotIn("env", focusyn)

    def test_tipo_desconocido_se_ignora(self):
        servers = mod.build_mcp_servers([{"name": "x", "type": "inexistente"}])
        self.assertEqual(servers, {})


class TestIOdeArchivos(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.ruta = os.path.join(self.dir, "config.json")

    def tearDown(self):
        for f in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, f))
        os.rmdir(self.dir)

    def test_round_trip_utf8(self):
        """Windows abre con la codepage ANSI si no se fuerza el encoding."""
        datos = {"proyectos": {"C:/Users/José/Diseño": {"nota": "café ✓ 日本"}}}
        mod.write_json_atomic(self.ruta, datos)
        self.assertEqual(mod.read_json(self.ruta), datos)

    def test_lee_utf8_crudo_escrito_por_otro_proceso(self):
        """El caso real: ~/.claude.json lo escribe Claude Code, no este script.

        Claude Code guarda UTF-8 crudo (no escapado). En Windows, un open() sin
        encoding usa la codepage ANSI: cp1252 no falla con estos bytes, los
        decodifica MAL. La reescritura persiste ese mojibake y corrompe para
        siempre las rutas de proyecto con acentos.
        """
        datos = {"projects": {"C:/Users/José/Diseño": {"mcpServers": {}}}}
        with open(self.ruta, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False)  # como lo escribe Claude Code

        self.assertEqual(mod.read_json(self.ruta), datos)

        with open(self.ruta, "rb") as f:
            crudo = f.read()
        self.assertNotEqual(
            crudo.decode("utf-8"),
            crudo.decode("cp1252"),
            "el fixture debe tener bytes que cp1252 decodifique distinto",
        )

    def test_no_deja_temporales(self):
        mod.write_json_atomic(self.ruta, {"a": 1})
        self.assertEqual(os.listdir(self.dir), ["config.json"])

    def test_fallo_a_mitad_no_corrompe_el_original(self):
        mod.write_json_atomic(self.ruta, {"original": True})
        with self.assertRaises(TypeError):
            mod.write_json_atomic(self.ruta, {"no_serializable": {1, 2, 3}})
        self.assertEqual(mod.read_json(self.ruta), {"original": True})
        self.assertEqual(os.listdir(self.dir), ["config.json"])

    @unittest.skipIf(os.name == "nt", "los modos POSIX no aplican en Windows")
    def test_preserva_permisos_del_original(self):
        mod.write_json_atomic(self.ruta, {"a": 1})
        os.chmod(self.ruta, 0o600)
        mod.write_json_atomic(self.ruta, {"a": 2})
        modo = stat.S_IMODE(os.stat(self.ruta).st_mode)
        self.assertEqual(modo, 0o600)


class TestCodexTraduccion(unittest.TestCase):
    """La forma canónica es la de Claude; Codex es una traducción de esa."""

    def setUp(self):
        self.servers = mod.build_mcp_servers(SECRETS_COMPLETO)

    def test_stdio_conserva_command_args_env(self):
        codex = mod.to_codex_server(self.servers["gh-demo"])
        self.assertEqual(codex["command"], self.servers["gh-demo"]["command"])
        self.assertEqual(codex["args"], self.servers["gh-demo"]["args"])
        self.assertEqual(codex["env"]["GH_TOKEN"], "ghp_desde_secrets")
        self.assertEqual(codex["startup_timeout_sec"], mod.CODEX_STARTUP_TIMEOUT_SEC)

    def test_http_renombra_headers(self):
        """Codex llama http_headers a lo que Claude llama headers."""
        codex = mod.to_codex_server(self.servers["focusyn"])
        self.assertEqual(codex["url"], "https://x/mcp")
        self.assertEqual(codex["http_headers"], {"X-Agent-Key": "k"})
        self.assertNotIn("headers", codex)
        self.assertNotIn("command", codex)

    def test_timeout_ms_pasa_a_segundos(self):
        codex = mod.to_codex_server({"type": "stdio", "command": "c", "args": [],
                                     "env": {}, "timeout": 320_000})
        self.assertEqual(codex["tool_timeout_sec"], 320)

    def test_timeout_sub_segundo_no_queda_en_cero(self):
        """Truncar 200ms a 0s dejaría un timeout que vence antes de empezar."""
        codex = mod.to_codex_server({"type": "stdio", "command": "c", "args": [],
                                     "env": {}, "timeout": 200})
        self.assertEqual(codex["tool_timeout_sec"], 1)


class TestCodexRender(unittest.TestCase):
    def test_round_trip_por_tomllib(self):
        servers = {n: mod.to_codex_server(c)
                   for n, c in mod.build_mcp_servers(SECRETS_COMPLETO).items()}
        datos = tomllib.loads(mod.render_codex_block(servers))
        self.assertEqual(set(datos["mcp_servers"]), set(servers))
        for nombre, esperado in servers.items():
            with self.subTest(mcp=nombre):
                self.assertEqual(datos["mcp_servers"][nombre], esperado)

    def test_escapa_comillas_y_backslashes(self):
        """Una contraseña con comillas rompería el TOML y Codex no arrancaría."""
        raro = {"x": {"command": "c", "args": ['--password=a"b\\c\td'],
                      "env": {"K": 'v"1'}}}
        datos = tomllib.loads(mod.render_codex_block(raro))
        self.assertEqual(datos["mcp_servers"]["x"]["args"], ['--password=a"b\\c\td'])
        self.assertEqual(datos["mcp_servers"]["x"]["env"], {"K": 'v"1'})

    def test_nombre_con_punto_no_anida_tablas(self):
        """Sin comillas, [mcp_servers.a.b] crearía una tabla anidada, no un server."""
        datos = tomllib.loads(mod.render_codex_block({"a.b": {"command": "c", "args": []}}))
        self.assertEqual(list(datos["mcp_servers"]), ["a.b"])


class TestCodexEscritura(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.proj = os.path.join(self.dir, "proj")
        os.makedirs(self.proj)
        self.codex_home = os.path.join(self.dir, "codex-home")
        os.makedirs(self.codex_home)
        self.config_global = os.path.join(self.codex_home, "config.toml")
        self.destino = os.path.join(self.proj, ".codex", "config.toml")
        self.servers = mod.build_mcp_servers(SECRETS_COMPLETO)
        self.parche = mock.patch.multiple(mod, CODEX_HOME=self.codex_home,
                                          CODEX_CONFIG=self.config_global)
        self.parche.start()

    def tearDown(self):
        self.parche.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def aplicar(self, **kwargs):
        opciones = dict(project_path=self.proj, servers=self.servers, update_mode=False,
                        remove_mode=False, only_filter=None, global_scope=False)
        opciones.update(kwargs)
        with mock.patch("sys.stdout"):
            mod.apply_codex(**opciones)

    def leer(self):
        with open(self.destino, encoding="utf-8") as f:
            return f.read()

    def leer_global(self):
        with open(self.config_global, encoding="utf-8") as f:
            return f.read()

    def test_marca_el_proyecto_trusted(self):
        """Sin trust, Codex ignora el config del proyecto sin decir nada."""
        self.aplicar()
        datos = tomllib.loads(self.leer_global())
        self.assertEqual(datos["projects"][self.proj]["trust_level"], "trusted")

    def test_untrusted_previo_pasa_a_trusted(self):
        with open(self.config_global, "w", encoding="utf-8") as f:
            f.write(f'model = "x"\n\n[projects."{self.proj}"]\ntrust_level = "untrusted"\n')
        self.aplicar()
        datos = tomllib.loads(self.leer_global())
        self.assertEqual(datos["projects"][self.proj]["trust_level"], "trusted")
        self.assertEqual(datos["model"], "x")

    def test_archivo_no_es_legible_por_otros(self):
        """Lleva contraseñas en claro: 0600 y nada más."""
        self.aplicar()
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(self.destino).st_mode), 0o600)

    def test_preserva_config_del_usuario(self):
        os.makedirs(os.path.dirname(self.destino))
        with open(self.destino, "w", encoding="utf-8") as f:
            f.write('model = "gpt-5.6-sol"\n# comentario propio\n')
        self.aplicar()
        texto = self.leer()
        self.assertIn("# comentario propio", texto)
        datos = tomllib.loads(texto)
        self.assertEqual(datos["model"], "gpt-5.6-sol")
        self.assertEqual(set(datos["mcp_servers"]), set(self.servers))

    def test_reaplicar_no_duplica(self):
        self.aplicar()
        self.aplicar()
        datos = tomllib.loads(self.leer())
        self.assertEqual(set(datos["mcp_servers"]), set(self.servers))
        self.assertEqual(self.leer().count(mod.CODEX_MARK_BEGIN), 1)

    def test_remove_total_deja_el_resto_del_archivo(self):
        os.makedirs(os.path.dirname(self.destino))
        with open(self.destino, "w", encoding="utf-8") as f:
            f.write('model = "gpt-5.6-sol"\n')
        self.aplicar()
        self.aplicar(remove_mode=True)
        datos = tomllib.loads(self.leer())
        self.assertEqual(datos, {"model": "gpt-5.6-sol"})

    def test_aborta_si_hay_mcp_declarado_a_mano(self):
        """Regenerar sobre una tabla ajena duplicaría [mcp_servers.X]: TOML inválido."""
        os.makedirs(os.path.dirname(self.destino))
        with open(self.destino, "w", encoding="utf-8") as f:
            f.write('[mcp_servers.a-mano]\ncommand = "x"\n')
        with self.assertRaises(SystemExit):
            self.aplicar()

    def test_agrega_codex_al_gitignore(self):
        if not shutil.which("git"):
            self.skipTest("git no disponible")
        subprocess.run(["git", "-C", self.proj, "init", "-q"], check=True)
        self.aplicar()
        with open(os.path.join(self.proj, ".gitignore"), encoding="utf-8") as f:
            self.assertIn(".codex/", f.read())

    def test_aborta_si_el_config_ya_esta_trackeado(self):
        """Trackearlo publica contraseñas al primer push."""
        if not shutil.which("git"):
            self.skipTest("git no disponible")
        subprocess.run(["git", "-C", self.proj, "init", "-q"], check=True)
        os.makedirs(os.path.dirname(self.destino))
        with open(self.destino, "w", encoding="utf-8") as f:
            f.write("")
        subprocess.run(["git", "-C", self.proj, "add", "-f", ".codex/config.toml"], check=True)
        with self.assertRaises(SystemExit):
            self.aplicar()

    def test_global_no_toca_el_proyecto(self):
        self.aplicar(global_scope=True, servers={"focusyn": self.servers["focusyn"]})
        self.assertFalse(os.path.exists(self.destino))
        datos = tomllib.loads(self.leer_global())
        self.assertEqual(list(datos["mcp_servers"]), ["focusyn"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
