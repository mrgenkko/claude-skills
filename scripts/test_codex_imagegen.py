#!/usr/bin/env python3
"""
Tests de codex-imagegen.py — sin dependencias ni red: no llaman al Codex real.

    python3 -m unittest discover -s scripts -p "test_*.py" -v

Dos casos sostienen el diseño:

- test_el_prompt_no_va_en_el_argv: `codex exec -i <FILE>...` es variádico y, con el
  prompt como argumento posicional detrás, se lo come como si fuera otra imagen y
  Codex corta con "No prompt provided via stdin".
- test_sin_ephemeral: el prompt que de verdad recibió image_gen (`revisedPrompt`) sólo
  queda en el registro de sesión; con --ephemeral no hay registro y no hay .prompt.txt.
"""

import contextlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codex-imagegen.py")


def cargar_modulo():
    """El script tiene guiones en el nombre: no es importable con `import`."""
    spec = importlib.util.spec_from_file_location("codex_imagegen", SCRIPT)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


mod = cargar_modulo()


def tocar(ruta, contenido=b""):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(contenido)
    return ruta


def evento_generacion(gen_id, ruta=None, prompt=None, status="completed", failure=None):
    """Una línea del rollout tal como la escribe codex-cli 0.160.0."""
    return json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "item": {
        "type": "Extension", "kind": "image_gen.generation", "id": gen_id, "status": status,
        "revisedPrompt": prompt, "result": "iVBOR...", "failure": failure,
        "savedPath": str(ruta) if ruta else None}}})


class ResolverCodex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def extension(self, carpeta, plataforma="linux-x86_64", nombre="codex", raiz=".vscode/extensions"):
        return tocar(self.home / raiz / carpeta / "bin" / plataforma / nombre)

    def test_elige_la_extension_mas_nueva_por_version_no_por_orden_alfabetico(self):
        self.extension("openai.chatgpt-26.930.31730-linux-x64")
        nueva = self.extension("openai.chatgpt-26.1001.100-linux-x64")
        self.assertEqual(mod.resolver_codex(env={}, home=self.home, which=lambda _: None), nueva)

    def test_busca_tambien_en_vscode_server_y_el_exe_de_windows(self):
        exe = self.extension("openai.chatgpt-27.1.0-win32-x64", "windows-x86_64", "codex.exe",
                             raiz=".vscode-server/extensions")
        self.assertEqual(mod.resolver_codex(env={}, home=self.home, which=lambda _: None), exe)

    def test_precedencia_flag_env_path_extension(self):
        self.extension("openai.chatgpt-26.930.31730-linux-x64")
        en_path = lambda _: "/usr/bin/codex"
        self.assertEqual(mod.resolver_codex("/flag/codex", {"CODEX_BIN": "/env/codex"}, self.home, en_path),
                         Path("/flag/codex"))
        self.assertEqual(mod.resolver_codex(None, {"CODEX_BIN": "/env/codex"}, self.home, en_path),
                         Path("/env/codex"))
        self.assertEqual(mod.resolver_codex(None, {}, self.home, en_path), Path("/usr/bin/codex"))

    def test_sin_codex_devuelve_none(self):
        self.assertIsNone(mod.resolver_codex(env={}, home=self.home, which=lambda _: None))


class Comando(unittest.TestCase):
    def test_el_prompt_no_va_en_el_argv(self):
        cmd = mod.armar_comando("codex", "/tmp/x", [Path("a.png"), Path("b.png")])
        self.assertNotIn("-", cmd)
        self.assertEqual(cmd[-4:], ["-i", "a.png", "-i", "b.png"])

    def test_sin_ephemeral(self):
        cmd = mod.armar_comando("codex", "/tmp/x")
        self.assertNotIn("--ephemeral", cmd)

    def test_no_carga_el_config_del_usuario(self):
        cmd = mod.armar_comando("codex", "/tmp/x")
        for flag in ("--json", "--ignore-user-config", "--skip-git-repo-check"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "read-only")

    def test_por_defecto_astra_en_high(self):
        cmd = mod.armar_comando("codex", "/tmp/x")
        self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-6-astra")
        self.assertIn("model_reasoning_effort=high", cmd)

    def test_modelo_y_esfuerzo(self):
        cmd = mod.armar_comando("codex", "/tmp/x", esfuerzo="low", modelo="gpt-x")
        self.assertIn("model_reasoning_effort=low", cmd)
        self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-x")


class Prompt(unittest.TestCase):
    def test_crudo_delimita_el_prompt_y_prohibe_reescribirlo(self):
        msg = mod.armar_prompt("  Use case: x\nPrimary request: y  ")
        self.assertIn("<<<PROMPT\nUse case: x\nPrimary request: y\nPROMPT>>>", msg)
        self.assertIn("EXACTAMENTE", msg)
        self.assertIn("No hay imágenes adjuntas", msg)
        self.assertIn("Fondo opaco", msg)
        self.assertIn("no ejecutes comandos", msg)

    def test_referencias_y_transparencia(self):
        msg = mod.armar_prompt("x", n_referencias=2, transparente=True)
        self.assertIn("las 2 imágenes adjuntas", msg)
        self.assertIn("Fondo transparente", msg)

    def test_rewrite_permite_ampliar(self):
        msg = mod.armar_prompt("un faro", reescribir=True)
        self.assertIn("Podés reescribirlo", msg)
        self.assertNotIn("<<<PROMPT", msg)
        self.assertTrue(msg.rstrip().endswith("un faro"))


class Eventos(unittest.TestCase):
    def test_thread_mensaje_y_ruido(self):
        salida = "\n".join([
            "Reading prompt from stdin...",
            json.dumps({"type": "thread.started", "thread_id": "t-1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "/ruta.png"}}),
            "[1, 2]",
        ])
        estado = mod.parsear_eventos(salida)
        self.assertEqual(estado["thread_id"], "t-1")
        self.assertEqual(estado["mensaje"], "/ruta.png")
        self.assertEqual(estado["errores"], [])

    def test_errores(self):
        salida = "\n".join([
            json.dumps({"type": "error", "message": "límite de uso"}),
            json.dumps({"type": "turn.failed", "error": {"message": "falló"}}),
            json.dumps({"type": "item.completed", "item": {"type": "error", "message": "item roto"}}),
        ])
        self.assertEqual(mod.parsear_eventos(salida)["errores"], ["límite de uso", "falló", "item roto"])

    def test_el_mismo_error_dos_veces_se_muestra_una(self):
        limite = "You've hit your usage limit. … try again at 1:58 PM."
        salida = "\n".join([
            json.dumps({"type": "error", "message": limite}),
            json.dumps({"type": "turn.failed", "error": {"message": limite}}),
        ])
        self.assertEqual(mod.parsear_eventos(salida)["errores"], [limite])


class Registro(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_registro_de_busca_por_thread_id(self):
        ruta = tocar(self.home / "sessions/2026/10/03/rollout-2026-10-03T11-07-58-t-1.jsonl")
        self.assertEqual(mod.registro_de(self.home, "t-1"), ruta)
        self.assertIsNone(mod.registro_de(self.home, "t-2"))

    def test_generaciones_de_saca_ruta_y_prompt_sin_duplicar(self):
        registro = self.home / "r.jsonl"
        registro.write_text("\n".join([
            json.dumps({"type": "session_meta", "payload": {"id": "t-1"}}),
            "no es json",
            evento_generacion("g1", "/a.png", "prompt A"),
            evento_generacion("g1", "/a.png", "prompt A"),  # el mismo item, otra vez
            evento_generacion("g2", None, None, status="failed", failure="política"),
        ]), encoding="utf-8")
        g1, g2 = mod.generaciones_de(registro)
        self.assertEqual((g1["ruta"], g1["prompt"], g1["fallo"]), (Path("/a.png"), "prompt A", None))
        self.assertEqual((g2["ruta"], g2["fallo"]), (None, "política"))


class Archivos(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_imagenes_de_filtra_por_extension(self):
        carpeta = self.dir / "generated_images" / "t-1"
        tocar(carpeta / "a.png")
        tocar(carpeta / "b.WEBP")
        tocar(carpeta / "notas.txt")
        nombres = sorted(p.name for p in mod.imagenes_de(self.dir, "t-1"))
        self.assertEqual(nombres, ["a.png", "b.WEBP"])
        self.assertEqual(mod.imagenes_de(self.dir, "no-existe"), [])

    def test_ruta_prompt_sigue_la_convencion_de_docs_design(self):
        self.assertEqual(mod.ruta_prompt(Path("d/login-v09.png")), Path("d/login-v09.prompt.txt"))

    def test_destino_libre_nunca_pisa_ni_la_imagen_ni_su_prompt(self):
        ruta = self.dir / "faro.png"
        self.assertEqual(mod.destino_libre(ruta), ruta)
        tocar(ruta)
        self.assertEqual(mod.destino_libre(ruta), self.dir / "faro-v2.png")
        tocar(self.dir / "faro-v2.prompt.txt")  # prompt huérfano: también ocupa el nombre
        self.assertEqual(mod.destino_libre(ruta), self.dir / "faro-v3.png")

    def test_destinos_numera_varias_y_respeta_la_extension_real(self):
        salida = self.dir / "vault.jpg"
        self.assertEqual(mod.destinos(salida, [Path("x.png")]), [self.dir / "vault.png"])
        self.assertEqual(mod.destinos(salida, [Path("x.png"), Path("y.png")]),
                         [self.dir / "vault-1.png", self.dir / "vault-2.png"])


@unittest.skipIf(os.name == "nt", "el Codex falso es un script con shebang")
class PuntaAPunta(unittest.TestCase):
    """Corre main() contra un Codex falso que imita `exec --json`, el registro de
    sesión y `login status`. Palabras clave en el prompt cambian su conducta:
    SIN-REGISTRO no escribe el rollout; ALTERAR cambia el prompt en modo crudo."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.codex_home = self.dir / "codex-home"
        self.log = self.dir / "llamadas.jsonl"
        self.codex = self.dir / "codex"
        falso = self.dir / "codex_falso.py"
        falso.write_text(textwrap.dedent(f"""\
            import json, pathlib, re, sys, uuid
            args = sys.argv[1:]
            if args[:2] == ["login", "status"]:
                sys.exit(0)
            mensaje = sys.stdin.read()
            with open({str(self.log)!r}, "a") as f:
                f.write(json.dumps({{"args": args, "mensaje": mensaje}}) + "\\n")
            home = pathlib.Path({str(self.codex_home)!r})
            tid = uuid.uuid4().hex
            imagen = home / "generated_images" / tid / "exec-1.png"
            imagen.parent.mkdir(parents=True)
            imagen.write_bytes(b"PNG")
            crudo = re.search(r"<<<PROMPT\\n(.*)\\nPROMPT>>>", mensaje, re.S)
            prompt = crudo.group(1) if crudo else "REESCRITO: " + mensaje.split("Pedido:\\n", 1)[1].strip()
            if "ALTERAR" in prompt:
                prompt += " (con algo más)"
            if "SIN-REGISTRO" not in mensaje:
                registro = home / "sessions/2026/10/03" / f"rollout-2026-10-03T11-00-00-{{tid}}.jsonl"
                registro.parent.mkdir(parents=True, exist_ok=True)
                registro.write_text(json.dumps({{"type": "event_msg", "payload": {{"type": "item_completed",
                    "item": {{"type": "Extension", "kind": "image_gen.generation", "id": "g-" + tid,
                              "status": "completed", "revisedPrompt": prompt, "failure": None,
                              "savedPath": str(imagen)}}}}}}) + "\\n")
            print(json.dumps({{"type": "thread.started", "thread_id": tid}}))
            print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": str(imagen)}}}}))
        """))
        # Wrapper sh y no `#!{sys.executable}`: el shebang se corta en el primer
        # espacio, y el venv vive en «Mrgenkko Skills».
        self.codex.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{falso}" "$@"\n')
        self.codex.chmod(self.codex.stat().st_mode | stat.S_IEXEC)
        self.env_previo = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(self.codex_home)

    def tearDown(self):
        if self.env_previo is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self.env_previo
        self.tmp.cleanup()

    def llamadas(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()]

    def registros(self):
        return list((self.codex_home / "sessions").rglob("rollout-*.jsonl"))

    def main(self, *argv):
        """main() en silencio; stderr queda en self.stderr para revisarlo."""
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            try:
                return mod.main([*argv, "--codex", str(self.codex)])
            finally:
                self.stderr = err.getvalue()

    def test_crudo_copia_sin_pisar_guarda_el_prompt_y_borra_el_registro(self):
        salida = self.dir / "out" / "faro.png"
        tocar(salida, b"previa")
        ref = tocar(self.dir / "ref.png")
        prompt = "Use case: precise-object-edit\nEdit target: ref.png"
        self.assertEqual(self.main(prompt, "-o", str(salida), "-r", str(ref)), 0)
        self.assertEqual(salida.read_bytes(), b"previa")
        self.assertEqual((self.dir / "out" / "faro-v2.png").read_bytes(), b"PNG")
        self.assertEqual((self.dir / "out" / "faro-v2.prompt.txt").read_text(encoding="utf-8"), prompt + "\n")
        self.assertEqual(self.registros(), [])
        llamada, = self.llamadas()
        self.assertIn("<<<PROMPT", llamada["mensaje"])
        self.assertNotIn("Use case", " ".join(llamada["args"]))
        self.assertEqual(llamada["args"][-2:], ["-i", str(ref)])
        self.assertEqual(self.stderr, "")

    def test_referencias_relativas_llegan_absolutas(self):
        # Codex corre en un directorio temporal: una ruta relativa al proyecto no le resuelve.
        tocar(self.dir / "refs" / "base.png")
        previo = os.getcwd()
        os.chdir(self.dir)
        try:
            self.assertEqual(self.main("x", "-r", "refs/base.png", "-o", "out.png"), 0)
        finally:
            os.chdir(previo)
        llamada, = self.llamadas()
        self.assertEqual(llamada["args"][-2:], ["-i", str((self.dir / "refs" / "base.png").resolve())])

    def test_rewrite_guarda_el_prompt_que_escribio_el_agente(self):
        salida = self.dir / "faro.png"
        self.assertEqual(self.main("un faro", "--rewrite", "-o", str(salida)), 0)
        self.assertEqual((self.dir / "faro.prompt.txt").read_text(encoding="utf-8"), "REESCRITO: un faro\n")
        self.assertNotIn("<<<PROMPT", self.llamadas()[0]["mensaje"])

    def test_avisa_si_el_agente_altera_el_prompt_crudo(self):
        self.assertEqual(self.main("ALTERAR esto", "-o", str(self.dir / "a.png")), 0)
        self.assertIn("alteró el prompt crudo", self.stderr)
        self.assertEqual((self.dir / "a.prompt.txt").read_text(encoding="utf-8"),
                         "ALTERAR esto (con algo más)\n")

    def test_sin_registro_entrega_la_imagen_sin_prompt(self):
        self.assertEqual(self.main("SIN-REGISTRO", "-o", str(self.dir / "s.png")), 0)
        self.assertTrue((self.dir / "s.png").exists())
        self.assertFalse((self.dir / "s.prompt.txt").exists())
        self.assertIn("sin .prompt.txt", self.stderr)

    def test_count_hace_una_corrida_por_variante(self):
        salida = self.dir / "v.png"
        self.assertEqual(self.main("x", "-n", "2", "-o", str(salida)), 0)
        self.assertEqual(len(self.llamadas()), 2)
        for i in (1, 2):
            self.assertTrue((self.dir / f"v-{i}.png").exists())
            self.assertTrue((self.dir / f"v-{i}.prompt.txt").exists())

    def test_referencia_inexistente_corta_antes_de_llamar_a_codex(self):
        with self.assertRaises(SystemExit) as ctx:
            self.main("x", "-r", str(self.dir / "no.png"))
        self.assertEqual(ctx.exception.code, 2)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
