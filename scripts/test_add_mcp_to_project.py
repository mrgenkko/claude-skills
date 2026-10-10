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

import contextlib
import importlib.util
import io
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


# Mismos MCPs que SECRETS_COMPLETO, pero con TODAS las credenciales en el llavero.
SECRETS_CON_LLAVERO = [
    {"name": "postgres-demo", "type": "postgres", "host": "h", "port": 5432,
     "db": "d", "user": "u", "password": "keyring:postgres-demo/password"},
    {"name": "ssh-demo", "type": "ssh", "host": "h", "user": "u",
     "password": "keyring:ssh-demo/password", "sudo_password": "keyring:ssh-demo/sudo_password"},
    {"name": "redis-demo", "type": "redis", "host": "h", "password": "keyring:redis-demo/password"},
    {"name": "gh-demo", "type": "gh", "owner": "o", "token": "keyring:gh-demo/token"},
    {"name": "modal-demo", "type": "modal", "workspace": "w",
     "token_id": "keyring:modal-demo/token_id", "token_secret": "keyring:modal-demo/token_secret"},
    {"name": "focusyn", "type": "focusyn", "url": "https://x/mcp", "agent_key": "keyring:focusyn/agent_key"},
    {"name": "webprobe", "type": "webprobe"},
]


class TestRegistroConLlavero(unittest.TestCase):
    """Con referencias keyring:, al registro va el NOMBRE de cada clave y nunca un valor."""

    def setUp(self):
        self.servers = mod.build_mcp_servers(SECRETS_CON_LLAVERO)
        self.pedidos = mod.header_helpers_pedidos(SECRETS_CON_LLAVERO, self.servers)

    def test_stdio_arranca_por_el_lanzador(self):
        args = self.servers["gh-demo"]["args"]
        self.assertEqual(self.servers["gh-demo"]["command"], mod.venv_python())
        self.assertEqual(args[:3], [mod.LAUNCHER, "--secret", "GH_TOKEN=keyring:gh-demo/token"])
        corte = args.index("--")
        self.assertEqual(args[corte + 1], mod.server_path("gh", "server.py"))
        self.assertIn("--owner=o", args[corte + 1:])
        self.assertNotIn("GH_TOKEN", self.servers["gh-demo"]["env"])

    def test_cada_tipo_mapea_su_variable(self):
        esperado = {
            "postgres-demo": ["MCP_PASSWORD=keyring:postgres-demo/password"],
            "ssh-demo": ["MCP_PASSWORD=keyring:ssh-demo/password",
                         "MCP_SUDO_PASSWORD=keyring:ssh-demo/sudo_password"],
            "redis-demo": ["MCP_PASSWORD=keyring:redis-demo/password"],
            "modal-demo": ["MODAL_TOKEN_ID=keyring:modal-demo/token_id",
                           "MODAL_TOKEN_SECRET=keyring:modal-demo/token_secret"],
        }
        for nombre, secretos in esperado.items():
            with self.subTest(mcp=nombre):
                args = self.servers[nombre]["args"]
                previos = args[:args.index("--")]
                self.assertEqual([previos[i + 1] for i, a in enumerate(previos) if a == "--secret"], secretos)
                self.assertFalse(any(a.startswith(("--password", "--sudo-password")) for a in args))

    def test_sin_credenciales_no_usa_lanzador(self):
        self.assertNotIn(mod.LAUNCHER, self.servers["webprobe"]["args"])

    def test_focusyn_usa_un_helper_sin_espacios_ni_argumentos(self):
        cfg = self.servers["focusyn"]
        self.assertNotIn("headers", cfg)
        helper = cfg["headersHelper"]
        self.assertNotIn(" ", helper, "Codex no documenta si parte el string: ruta sola y sin espacios")
        self.assertEqual(list(self.pedidos), [helper])
        contenido = self.pedidos[helper]
        self.assertIn("X-Agent-Key=keyring:focusyn/agent_key", contenido)

    def test_codex_traduce_helper_y_pide_el_bus_de_sesion(self):
        http = mod.to_codex_server(self.servers["focusyn"])
        self.assertEqual(http["http_headers_helper"], self.servers["focusyn"]["headersHelper"])
        stdio = mod.to_codex_server(self.servers["gh-demo"])
        if os.name != "nt":
            self.assertEqual(stdio["env_vars"], mod.LAUNCHER_ENV_VARS)
        self.assertNotIn("env_vars", mod.to_codex_server(self.servers["webprobe"]))
        datos = tomllib.loads(mod.render_codex_block({"focusyn": http, "gh-demo": stdio}))
        self.assertEqual(datos["mcp_servers"]["focusyn"], http)
        self.assertEqual(datos["mcp_servers"]["gh-demo"], stdio)

    def test_ningun_valor_llega_al_registro(self):
        """Lo serializado (Claude y Codex) solo puede tener referencias, nunca algo más."""
        texto = json.dumps(self.servers) + mod.render_codex_block(
            {n: mod.to_codex_server(c) for n, c in self.servers.items()})
        texto_helpers = "".join(self.pedidos.values())
        for entry in SECRETS_CON_LLAVERO:
            for campo in mod.secret_refs.secret_fields(entry):
                with self.subTest(campo=f"{entry['name']}.{campo}"):
                    # la referencia sí está: en el registro, o en el helper de headers (focusyn)...
                    self.assertIn(entry[campo], texto + texto_helpers)
        # ...y nada con forma de clave en texto plano.
        self.assertNotIn("--password=", texto)
        self.assertNotIn('"GH_TOKEN": "', texto)
        self.assertNotIn("X-Agent-Key\": \"", texto)

    def test_todo_tipo_con_credenciales_esta_cubierto(self):
        """Un tipo nuevo en SECRET_FIELDS sin su caso acá no tendría el test de arriba."""
        tipos = {e["type"] for e in SECRETS_CON_LLAVERO}
        self.assertEqual(set(mod.secret_refs.SECRET_FIELDS) - tipos, set())
        for entry in SECRETS_CON_LLAVERO:
            for campo in mod.secret_refs.secret_fields(entry):
                with self.subTest(campo=f"{entry['name']}.{campo}"):
                    self.assertTrue(mod.secret_refs.is_ref(entry.get(campo)))

    def test_helpers_no_se_escriben_en_dry_run(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(mod, "HEADER_HELPERS_DIR", d), \
                contextlib.redirect_stdout(io.StringIO()):
            pedidos = {os.path.join(d, "headers-focusyn"): "#!/bin/sh\n"}
            mod.escribir_header_helpers(pedidos, dry_run=True)
            self.assertFalse(os.path.exists(os.path.join(d, "headers-focusyn")))
            mod.escribir_header_helpers(pedidos, dry_run=False)
            self.assertTrue(os.path.exists(os.path.join(d, "headers-focusyn")))
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, "headers-focusyn")).st_mode), 0o700)


class TestSweep(unittest.TestCase):
    """--sweep: re-registra lo que ya está, en todos los scopes, y nada más."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.claude_json = os.path.join(self.dir, "claude.json")
        self.codex_home = os.path.join(self.dir, "codex-home")
        os.makedirs(self.codex_home)
        self.proj_a = os.path.join(self.dir, "a")
        self.proj_b = os.path.join(self.dir, "b")
        os.makedirs(self.proj_a)
        os.makedirs(self.proj_b)
        self.parche = mock.patch.multiple(
            mod, CLAUDE_JSON=self.claude_json, CODEX_HOME=self.codex_home,
            CODEX_CONFIG=os.path.join(self.codex_home, "config.toml"),
            HEADER_HELPERS_DIR=os.path.join(self.dir, "_launch"))
        self.parche.start()
        # Estado "viejo": registrado con valores en texto plano. Se arma DESPUÉS del
        # parche: la ruta del helper de headers se calcula al construir la entrada.
        self.viejo = mod.build_mcp_servers(SECRETS_COMPLETO)
        self.nuevo = mod.build_mcp_servers(SECRETS_CON_LLAVERO)
        with open(self.claude_json, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {"focusyn": self.viejo["focusyn"]},
                       "projects": {
                           self.proj_a: {"mcpServers": {"gh-demo": self.viejo["gh-demo"],
                                                        "figma": {"type": "http", "url": "https://figma"}}},
                           self.proj_b: {"mcpServers": {"webprobe": self.viejo["webprobe"]}},
                       }}, f)

    def tearDown(self):
        self.parche.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def correr(self, dry_run):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            mod.sweep(self.nuevo, SECRETS_CON_LLAVERO, "claude", dry_run)
        return out.getvalue()

    def leer(self):
        with open(self.claude_json, encoding="utf-8") as f:
            return json.load(f)

    def test_dry_run_no_escribe(self):
        antes = self.leer()
        salida = self.correr(dry_run=True)
        self.assertEqual(self.leer(), antes)
        self.assertIn("gh-demo", salida)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "_launch")))

    def test_actualiza_solo_lo_existente(self):
        salida = self.correr(dry_run=False)
        d = self.leer()
        self.assertEqual(d["projects"][self.proj_a]["mcpServers"]["gh-demo"], self.nuevo["gh-demo"])
        self.assertEqual(d["mcpServers"]["focusyn"], self.nuevo["focusyn"])
        # No agrega gh-demo donde no estaba, ni toca lo que no sale de secrets.json.
        self.assertNotIn("gh-demo", d["projects"][self.proj_b]["mcpServers"])
        self.assertEqual(d["projects"][self.proj_a]["mcpServers"]["figma"], {"type": "http", "url": "https://figma"})
        self.assertIn("figma", salida)
        # El helper de focusyn se escribe al aplicar.
        self.assertTrue(os.path.exists(self.nuevo["focusyn"]["headersHelper"]))
        self.assertNotIn("ghp_desde_secrets", json.dumps(d))

    def test_solo_credenciales_no_se_marca_como_ajeno(self):
        self.assertEqual(mod.diferencias_ajenas(self.viejo["gh-demo"], self.nuevo["gh-demo"]), [])
        self.assertEqual(mod.diferencias_ajenas(self.viejo["focusyn"], self.nuevo["focusyn"]), [])

    def test_un_cambio_que_no_es_de_credenciales_se_marca(self):
        otro = dict(self.viejo["focusyn"], url="https://otra/mcp")
        difs = mod.diferencias_ajenas(otro, self.nuevo["focusyn"])
        self.assertEqual(len(difs), 1)
        self.assertIn("https://otra/mcp", difs[0])
        viejo_gh = dict(self.viejo["gh-demo"], args=[*self.viejo["gh-demo"]["args"], "--allow-write"])
        self.assertIn("--allow-write", mod.diferencias_ajenas(viejo_gh, self.nuevo["gh-demo"])[0])

    def codex_con(self, destino, servers):
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        with open(destino, "w", encoding="utf-8") as f:
            f.write(mod.render_codex_block({n: mod.to_codex_server(c) for n, c in servers.items()}))

    def test_codex_el_home_no_es_un_proyecto(self):
        """Con CODEX_HOME por defecto, <home>/.codex/config.toml ES el global."""
        global_en_proyecto = os.path.join(self.proj_a, ".codex", "config.toml")
        with mock.patch.multiple(mod, CODEX_HOME=os.path.dirname(global_en_proyecto),
                                 CODEX_CONFIG=global_en_proyecto):
            self.codex_con(global_en_proyecto, {"focusyn": self.viejo["focusyn"]})
            with contextlib.redirect_stdout(io.StringIO()):
                mod.sweep(self.nuevo, SECRETS_CON_LLAVERO, "codex", dry_run=False)
            datos = tomllib.loads(open(global_en_proyecto, encoding="utf-8").read())
        self.assertNotIn("projects", datos, "no puede marcar el proyecto trusted")
        self.assertIn("http_headers_helper", datos["mcp_servers"]["focusyn"])

    def test_codex_no_reescribe_lo_que_no_cambia(self):
        destino = os.path.join(self.proj_b, ".codex", "config.toml")
        self.codex_con(destino, {"webprobe": self.viejo["webprobe"], "gh-demo": self.viejo["gh-demo"]})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            mod.sweep(self.nuevo, SECRETS_CON_LLAVERO, "codex", dry_run=True)
        linea = next(l for l in out.getvalue().splitlines() if self.proj_b in l)
        self.assertTrue(linea.endswith(": gh-demo"), linea)

    def test_con_cambios_ajenos_frena_antes_de_escribir(self):
        d = self.leer()
        d["projects"][self.proj_a]["mcpServers"]["gh-demo"]["args"].append("--allow-write")
        with open(self.claude_json, "w", encoding="utf-8") as f:
            json.dump(d, f)
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit):
            mod.sweep(self.nuevo, SECRETS_CON_LLAVERO, "claude", dry_run=False)
        self.assertEqual(self.leer(), d, "no puede escribir antes de que el usuario confirme")
        self.assertIn("--allow-write", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            mod.sweep(self.nuevo, SECRETS_CON_LLAVERO, "claude", dry_run=False, yes=True)
        self.assertEqual(self.leer()["projects"][self.proj_a]["mcpServers"]["gh-demo"], self.nuevo["gh-demo"])

    def test_only_no_hace_pasar_por_manuales_a_los_de_secrets(self):
        solo_gh = {"gh-demo": self.nuevo["gh-demo"]}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            mod.sweep(solo_gh, SECRETS_CON_LLAVERO, "claude", dry_run=True)
        ignorados = next(l for l in out.getvalue().splitlines() if l.startswith("No salen de secrets.json"))
        self.assertIn("figma", ignorados)
        self.assertNotIn("focusyn", ignorados)
        self.assertNotIn("webprobe", ignorados)

    def test_helper_huerfano_se_borra_y_el_usado_no(self):
        with contextlib.redirect_stdout(io.StringIO()):
            mod.sweep(self.nuevo, SECRETS_CON_LLAVERO, "claude", dry_run=False)
        usado = self.nuevo["focusyn"]["headersHelper"]
        huerfano = os.path.join(os.path.dirname(usado), "headers-viejo")
        with open(huerfano, "w") as f:
            f.write("#!/bin/sh\n")
        with contextlib.redirect_stdout(io.StringIO()):
            mod.limpiar_header_helpers(dry_run=False)
        self.assertTrue(os.path.exists(usado))
        self.assertFalse(os.path.exists(huerfano))

    def test_regenera_un_helper_faltante_aunque_el_config_no_cambie(self):
        with contextlib.redirect_stdout(io.StringIO()):
            mod.sweep(self.nuevo, SECRETS_CON_LLAVERO, "claude", dry_run=False)
        helper = self.nuevo["focusyn"]["headersHelper"]
        os.unlink(helper)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            mod.sweep(self.nuevo, SECRETS_CON_LLAVERO, "claude", dry_run=False)
        self.assertIn("0 scope(s) con cambios", out.getvalue())
        self.assertTrue(os.path.exists(helper))

    def test_helper_referenciado_con_tilde_no_es_huerfano(self):
        """Un config escrito a mano con "~/..." sigue usando el helper."""
        with contextlib.redirect_stdout(io.StringIO()):
            mod.sweep(self.nuevo, SECRETS_CON_LLAVERO, "claude", dry_run=False)
        helper = self.nuevo["focusyn"]["headersHelper"]  # vive bajo self.dir/_launch
        d = self.leer()
        d["mcpServers"]["focusyn"]["headersHelper"] = "~" + helper[len(self.dir):]
        with open(self.claude_json, "w", encoding="utf-8") as f:
            json.dump(d, f)
        with mock.patch.dict(os.environ, {"HOME": self.dir}), contextlib.redirect_stdout(io.StringIO()):
            mod.limpiar_header_helpers(dry_run=False)
        self.assertTrue(os.path.exists(helper))

    def test_codex_trackeado_aborta_antes_de_escribir_nada(self):
        destino = os.path.join(self.proj_b, ".codex", "config.toml")
        self.codex_con(destino, {"gh-demo": self.viejo["gh-demo"]})
        subprocess.run(["git", "init", "-q", self.proj_b], check=True)
        subprocess.run(["git", "-C", self.proj_b, "add", "-f", ".codex/config.toml"], check=True)
        antes_claude, antes_codex = self.leer(), open(destino, encoding="utf-8").read()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            mod.sweep(self.nuevo, SECRETS_CON_LLAVERO, "both", dry_run=False)
        self.assertEqual(self.leer(), antes_claude, "Claude no puede quedar migrado a medias")
        self.assertEqual(open(destino, encoding="utf-8").read(), antes_codex)

    def test_sweep_no_toca_trust_ni_gitignore(self):
        destino = os.path.join(self.proj_b, ".codex", "config.toml")
        self.codex_con(destino, {"gh-demo": self.viejo["gh-demo"]})
        subprocess.run(["git", "init", "-q", self.proj_b], check=True)
        with contextlib.redirect_stdout(io.StringIO()):
            mod.sweep(self.nuevo, SECRETS_CON_LLAVERO, "codex", dry_run=False)
        self.assertFalse(os.path.exists(os.path.join(self.proj_b, ".gitignore")))
        global_ = os.path.join(self.codex_home, "config.toml")
        self.assertFalse(os.path.exists(global_) and "projects" in tomllib.loads(open(global_).read()))
        self.assertIn("keyring:gh-demo/token", open(destino, encoding="utf-8").read())

    def test_segunda_pasada_no_cambia_nada(self):
        self.correr(dry_run=False)
        antes = self.leer()
        salida = self.correr(dry_run=False)
        self.assertEqual(self.leer(), antes)
        self.assertIn("0 scope(s) con cambios", salida)


class TestRevisionRegistro(unittest.TestCase):
    """Hallazgos del code review sobre el registro."""

    def test_gh_sin_token_falla_al_registrar(self):
        # Sin GH_TOKEN, gh usaría la cuenta logueada en hosts.yml sin avisar.
        for token in (None, ""):
            entry = {"name": "gh-x", "type": "gh", "owner": "o"}
            if token is not None:
                entry["token"] = token
            with self.subTest(token=token), contextlib.redirect_stdout(io.StringIO()) as out, \
                    self.assertRaises(SystemExit):
                mod.build_mcp_servers([entry])
            self.assertIn("gh-x", out.getvalue())

    def test_postgres_con_password_vacia_sigue_funcionando(self):
        """auth trust o .pgpass: "" se registra como --password= (como antes)."""
        cfg = mod.build_mcp_servers([{"name": "pg", "type": "postgres", "host": "h", "port": 5432,
                                      "db": "d", "user": "u", "password": ""}])["pg"]
        self.assertIn("--password=", cfg["args"])
        self.assertNotIn(mod.LAUNCHER, cfg["args"])

    def test_postgres_sin_password_falla_al_registrar(self):
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            mod.build_mcp_servers([{"name": "pg", "type": "postgres", "host": "h", "port": 5432,
                                    "db": "d", "user": "u"}])

    def test_filtros_del_sweep_salen_de_la_tabla_unica(self):
        for kind, campos in mod.secret_refs.SECRET_FIELDS.items():
            for campo, (var, flag) in campos.items():
                with self.subTest(campo=f"{kind}.{campo}"):
                    self.assertIn(var, mod._NOMBRES_CREDENCIAL)
                    if flag:
                        self.assertIn(f"{flag}=", mod._FLAGS_CREDENCIAL)


class TestReferenciasInvalidas(unittest.TestCase):
    def abortar(self, entry):
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit):
            mod.abortar_si_refs_invalidas([entry], [entry["name"]])
        return out.getvalue()

    def test_keyring_en_un_campo_que_no_es_secreto(self):
        salida = self.abortar({"name": "gc", "type": "gcloud", "key_file": "keyring:gc/key"})
        self.assertIn("gc.key_file", salida)

    def test_referencia_sin_id(self):
        self.assertIn("sin id", self.abortar({"name": "gh", "type": "gh", "token": "keyring:"}))

    def test_referencias_validas_pasan(self):
        mod.abortar_si_refs_invalidas(SECRETS_CON_LLAVERO, [e["name"] for e in SECRETS_CON_LLAVERO])


class TestAvisoTextoPlano(unittest.TestCase):
    def test_avisa_texto_plano_sin_mostrar_valores(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            mod.avisar_texto_plano(SECRETS_COMPLETO, ["gh-demo", "webprobe"])
        self.assertIn("gh-demo.token", out.getvalue())
        self.assertNotIn("ghp_desde_secrets", out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
