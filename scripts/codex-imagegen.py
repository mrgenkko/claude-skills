#!/usr/bin/env python3
"""
codex-imagegen.py — genera o edita imágenes con el `image_gen` integrado de Codex CLI.

Claude Code no genera imágenes; Codex sí, con la sesión de ChatGPT que ya tiene
iniciada (no pide OPENAI_API_KEY). Este script le pasa el prompt a `codex exec`,
espera la imagen y la copia adonde se le indique, con su `.prompt.txt` al lado.
Lo usa la skill /codex-imagegen.

    python3 codex-imagegen.py "Use case: ... Primary request: ..." -o docs/design/x/faro.png
    python3 codex-imagegen.py "Edit target: ... PRESERVE ..." -r v02.png -o docs/design/x/v03.png
    python3 codex-imagegen.py "un faro gótico" --rewrite -n 3 -o propuestas/faro.png

Por defecto el prompt llega CRUDO a image_gen: el agente de Codex lo pasa tal cual.
Con --rewrite el agente lo reescribe y amplía a su criterio (sin contexto del
proyecto: corre en un directorio vacío).

El prompt que de verdad recibió image_gen (`revisedPrompt`) sólo queda en el registro
de sesión de Codex, no en el stream --json: el script lo lee de ahí, lo guarda en
`<salida>.prompt.txt` y borra el registro, que repite la imagen en base64.

Por stdout imprime la ruta final de cada imagen, una por línea; el diagnóstico va a stderr.
Códigos de salida: 0 ok · 1 Codex no devolvió imagen · 2 falta Codex, la sesión o un argumento.

Sin dependencias: stdlib puro.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

EXTENSIONES_IMAGEN = {".png", ".jpg", ".jpeg", ".webp"}
SUFIJO_PROMPT = ".prompt.txt"

# Explícitos a propósito: con --ignore-user-config Codex no lee el `model` del
# config.toml del usuario y caería en su default. Pedido del usuario: astra en high.
MODELO_DEFAULT = "gpt-6-astra"
ESFUERZO_DEFAULT = "high"

# Dónde instalan Codex las extensiones de VSCode y sus forks. El binario no
# siempre está en el PATH: la extensión de ChatGPT lo trae adentro y la ruta
# cambia con cada versión.
RAICES_EXTENSIONES = (
    ".vscode/extensions",
    ".vscode-server/extensions",
    ".vscode-insiders/extensions",
    ".cursor/extensions",
)


def version_de(carpeta):
    """`openai.chatgpt-26.930.31730-linux-x64` → (26, 930, 31730)."""
    m = re.search(r"-(\d+(?:\.\d+)*)", carpeta.name)
    return tuple(int(p) for p in m.group(1).split(".")) if m else ()


def binarios_de_extension(home):
    """Los `codex` que traen las extensiones instaladas, el más nuevo primero."""
    encontrados = []
    for raiz in RAICES_EXTENSIONES:
        for ext in (home / raiz).glob("openai.chatgpt-*"):
            for nombre in ("codex", "codex.exe"):
                encontrados.extend(p for p in ext.glob(f"bin/*/{nombre}") if p.is_file())
    return sorted(encontrados, key=lambda p: version_de(p.parents[2]), reverse=True)


def resolver_codex(explicito=None, env=None, home=None, which=shutil.which):
    """--codex › $CODEX_BIN › `codex` en el PATH › el de la extensión más nueva."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    for candidato in (explicito, env.get("CODEX_BIN")):
        if candidato:
            return Path(candidato)
    en_path = which("codex")
    if en_path:
        return Path(en_path)
    de_extension = binarios_de_extension(home)
    return de_extension[0] if de_extension else None


def codex_home(env=None):
    env = os.environ if env is None else env
    return Path(env.get("CODEX_HOME") or Path.home() / ".codex")


def armar_prompt(prompt, n_referencias=0, reescribir=False, transparente=False):
    """El mensaje para el agente de Codex: que llame a image_gen y nada más."""
    fondo = "transparente" if transparente else "opaco"
    if n_referencias:
        refs = (f"Incluí en la llamada las {n_referencias} imágenes adjuntas: el prompt dice "
                "si se editan o se toman de referencia.")
    else:
        refs = "No hay imágenes adjuntas."
    reglas = (
        "No leas archivos, no ejecutes comandos ni uses otras herramientas.\n"
        "Al terminar, respondé solo con la ruta absoluta del archivo generado.\n"
    )
    if reescribir:
        return (
            "Tarea: generá UNA imagen con tu herramienta integrada image_gen a partir del pedido "
            "de abajo. Podés reescribirlo y ampliarlo como prompt para image_gen.\n"
            f"{refs} Fondo {fondo}.\n{reglas}\n"
            f"Pedido:\n{prompt.strip()}\n"
        )
    return (
        "Tarea: llamá UNA vez a tu herramienta integrada image_gen.\n"
        "Su prompt es el texto entre <<<PROMPT y PROMPT>>>, EXACTAMENTE igual, carácter por "
        "carácter: no lo reescribas, no lo traduzcas, no lo amplíes ni lo resumas.\n"
        f"{refs} Fondo {fondo}.\n{reglas}\n"
        f"<<<PROMPT\n{prompt.strip()}\nPROMPT>>>\n"
    )


def armar_comando(codex, cwd, referencias=(), esfuerzo=ESFUERZO_DEFAULT, modelo=MODELO_DEFAULT):
    """El prompt NO va en el argv: `-i <FILE>...` es variádico y se lo come como
    si fuera otra imagen ("No prompt provided via stdin"). Viaja por stdin.

    Sin --ephemeral: el `revisedPrompt` sólo queda en el registro de sesión."""
    cmd = [
        str(codex), "exec", "--json",
        # Sin el config.toml del usuario: no arranca sus MCPs ni su memoria en cada
        # imagen. La sesión (auth.json) se sigue leyendo de CODEX_HOME.
        "--ignore-user-config",
        "--skip-git-repo-check", "--sandbox", "read-only",
        "-C", str(cwd),
        "-c", f"model_reasoning_effort={esfuerzo}",
    ]
    if modelo:
        cmd += ["-m", modelo]
    for ref in referencias:
        cmd += ["-i", str(ref)]
    return cmd


def parsear_eventos(texto):
    """De la salida JSONL de `codex exec --json`: thread_id, último mensaje y errores."""
    estado = {"thread_id": None, "mensaje": "", "errores": []}
    for linea in texto.splitlines():
        try:
            ev = json.loads(linea)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        tipo = ev.get("type")
        if tipo == "thread.started":
            estado["thread_id"] = ev.get("thread_id")
        elif tipo == "item.completed":
            item = ev.get("item") or {}
            if item.get("type") == "agent_message":
                estado["mensaje"] = item.get("text", "")
            elif item.get("type") == "error":
                estado["errores"].append(item.get("message", ""))
        elif tipo in ("error", "turn.failed"):
            err = ev.get("error")
            estado["errores"].append(err.get("message", "") if isinstance(err, dict) else ev.get("message", ""))
    # Codex manda el mismo motivo como `error` y otra vez en `turn.failed`
    # (el límite de uso del plan llega así): una vez basta.
    estado["errores"] = list(dict.fromkeys(e for e in estado["errores"] if e))
    return estado


def registro_de(home_codex, thread_id):
    """El rollout de la sesión: `sessions/AAAA/MM/DD/rollout-<fecha>-<thread_id>.jsonl`."""
    return next((home_codex / "sessions").glob(f"*/*/*/rollout-*-{thread_id}.jsonl"), None)


def generaciones_de(registro):
    """Las llamadas a image_gen del registro: [{ruta, prompt, fallo}], en orden."""
    por_id = {}
    for linea in registro.read_text(encoding="utf-8").splitlines():
        try:
            ev = json.loads(linea)
        except ValueError:
            continue
        item = (ev.get("payload") or {}).get("item") if isinstance(ev, dict) else None
        if isinstance(item, dict) and item.get("kind") == "image_gen.generation":
            por_id[item.get("id")] = {
                "ruta": Path(item["savedPath"]) if item.get("savedPath") else None,
                "prompt": item.get("revisedPrompt"),
                "fallo": item.get("failure") or (None if item.get("status") == "completed"
                                                 else item.get("status")),
            }
    return list(por_id.values())


def imagenes_de(home_codex, thread_id):
    """Las imágenes que Codex guardó para ese thread, en el orden en que salieron."""
    carpeta = home_codex / "generated_images" / thread_id
    if not carpeta.is_dir():
        return []
    imagenes = [p for p in carpeta.iterdir() if p.suffix.lower() in EXTENSIONES_IMAGEN]
    return sorted(imagenes, key=lambda p: (p.stat().st_mtime, p.name))


def ruta_prompt(imagen):
    """`faro.png` → `faro.prompt.txt`: la convención de docs/design."""
    return imagen.with_suffix(SUFIJO_PROMPT)


def destino_libre(ruta):
    """Nunca pisa: `faro.png` (o su `.prompt.txt`) ocupado → `faro-v2.png`, `faro-v3.png`…"""
    ocupada = lambda r: r.exists() or ruta_prompt(r).exists()
    if not ocupada(ruta):
        return ruta
    n = 2
    while ocupada(candidato := ruta.with_name(f"{ruta.stem}-v{n}{ruta.suffix}")):
        n += 1
    return candidato


def destinos(salida, origenes):
    """Una imagen → `salida` tal cual; varias → `salida-1`, `salida-2`… La extensión
    la manda el archivo real: un PNG no se guarda como `.jpg`."""
    rutas = []
    for i, origen in enumerate(origenes, 1):
        stem = salida.stem if len(origenes) == 1 else f"{salida.stem}-{i}"
        rutas.append(salida.with_name(stem + origen.suffix.lower()))
    return rutas


def normalizar(texto):
    return " ".join((texto or "").split())


def generar_una(codex, prompt, referencias, esfuerzo, modelo, timeout, home_codex,
                reescribir=False, transparente=False):
    """Una corrida de `codex exec` → [{ruta, prompt}] (vacía si no salió imagen)."""
    mensaje = armar_prompt(prompt, len(referencias), reescribir, transparente)
    with tempfile.TemporaryDirectory(prefix="codex-imagegen-") as cwd:
        # cwd vacío: que Codex no cargue el AGENTS.md del proyecto en el que estamos.
        cmd = armar_comando(codex, cwd, referencias, esfuerzo, modelo)
        try:
            proc = subprocess.run(cmd, input=mensaje, cwd=cwd, capture_output=True,
                                  text=True, encoding="utf-8", timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"codex-imagegen: Codex no terminó en {timeout} s (subí --timeout).", file=sys.stderr)
            return []
    estado = parsear_eventos(proc.stdout)
    tid = estado["thread_id"]

    generadas, fallos = [], []
    registro = registro_de(home_codex, tid) if tid else None
    if registro:
        for g in generaciones_de(registro):
            if g["ruta"] and g["ruta"].is_file():
                generadas.append(g)
            elif g["fallo"]:
                fallos.append(str(g["fallo"]))
        registro.unlink()  # repite la imagen en base64 y no lo vuelve a leer nadie
    if not generadas and tid:
        # Sin registro (¿cambió el formato?): la imagen igual está en generated_images,
        # pero no sabemos con qué prompt.
        generadas = [{"ruta": p, "prompt": None} for p in imagenes_de(home_codex, tid)]
        if generadas:
            print("codex-imagegen: no encontré el revisedPrompt en el registro de sesión; "
                  "la imagen sale sin .prompt.txt.", file=sys.stderr)

    if not generadas:
        print(f"codex-imagegen: Codex no devolvió imagen (exit {proc.returncode}).", file=sys.stderr)
        for err in estado["errores"] + fallos:
            print(f"  error: {err}", file=sys.stderr)
        if estado["mensaje"]:
            print(f"  respuesta de Codex: {estado['mensaje']}", file=sys.stderr)
        cola = [l for l in proc.stderr.splitlines() if l.strip() and "Reading prompt from stdin" not in l]
        for linea in cola[-5:]:
            print(f"  stderr: {linea}", file=sys.stderr)
    elif not reescribir:
        for g in generadas:
            if g["prompt"] is not None and normalizar(g["prompt"]) != normalizar(prompt):
                print("codex-imagegen: el agente de Codex alteró el prompt crudo; el .prompt.txt "
                      "guarda el que recibió image_gen de verdad.", file=sys.stderr)
    return generadas


def sesion_iniciada(codex):
    try:
        proc = subprocess.run([str(codex), "login", "status"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Genera o edita imágenes con el image_gen integrado de Codex CLI.")
    ap.add_argument("prompt", help='el prompt para image_gen; "-" lo lee de stdin')
    ap.add_argument("-o", "--out", type=Path,
                    help="dónde copiar el resultado, con su .prompt.txt al lado (sin pisar: si "
                         "existe, agrega -v2). Sin -o queda en $CODEX_HOME/generated_images/")
    ap.add_argument("-r", "--ref", type=Path, action="append", default=[],
                    help="imagen a editar o de referencia (repetible)")
    ap.add_argument("-n", "--count", type=int, default=1,
                    help="cuántas variantes; se generan de a una, en serie (default 1)")
    ap.add_argument("--rewrite", action="store_true",
                    help="dejar que el agente de Codex reescriba y amplíe el prompt (default: crudo)")
    ap.add_argument("--transparent", action="store_true", help="fondo transparente (PNG con alfa)")
    # Sin `choices`: los niveles válidos dependen del modelo (luna no tiene `ultra`).
    ap.add_argument("--effort", default=ESFUERZO_DEFAULT,
                    help=f"razonamiento del modelo de Codex: low…ultra (default {ESFUERZO_DEFAULT})")
    ap.add_argument("--model", default=MODELO_DEFAULT,
                    help=f"modelo de Codex (default {MODELO_DEFAULT})")
    ap.add_argument("--timeout", type=int, default=300, help="segundos por imagen (default 300)")
    ap.add_argument("--codex", help="ruta al binario de Codex (o $CODEX_BIN)")
    args = ap.parse_args(argv)

    prompt = sys.stdin.read() if args.prompt == "-" else args.prompt
    if not prompt.strip():
        ap.error("el prompt está vacío")
    if args.count < 1:
        ap.error("--count tiene que ser 1 o más")
    faltantes = [str(r) for r in args.ref if not r.is_file()]
    if faltantes:
        ap.error(f"no existe la referencia: {', '.join(faltantes)}")
    # Absolutas: Codex corre en un directorio temporal y una ruta relativa al
    # proyecto no le resuelve ("las rutas indicadas no están disponibles").
    referencias = [r.resolve() for r in args.ref]

    codex = resolver_codex(args.codex)
    if codex is None or not codex.exists():
        print("codex-imagegen: no encontré Codex. Instalá la extensión ChatGPT de VSCode "
              "o `npm i -g @openai/codex`, o pasá --codex / $CODEX_BIN.", file=sys.stderr)
        return 2
    if not sesion_iniciada(codex):
        print(f"codex-imagegen: Codex no tiene sesión iniciada. Corré `{codex} login` "
              "(es interactivo: lo hace el usuario).", file=sys.stderr)
        return 2

    home_codex = codex_home()
    generadas = []
    for i in range(args.count):
        if args.count > 1:
            print(f"codex-imagegen: variante {i + 1}/{args.count}…", file=sys.stderr)
        generadas += generar_una(codex, prompt, referencias, args.effort, args.model, args.timeout,
                                 home_codex, args.rewrite, args.transparent)
    if not generadas:
        return 1

    if args.out is None:
        finales = [g["ruta"] for g in generadas]
        if args.rewrite:
            for g in generadas:
                if g["prompt"]:
                    print(f"codex-imagegen: prompt usado → {g['prompt']}", file=sys.stderr)
    else:
        finales = []
        origenes = [g["ruta"] for g in generadas]
        for g, destino in zip(generadas, destinos(args.out, origenes)):
            destino = destino_libre(destino)
            destino.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(g["ruta"], destino)
            if g["prompt"]:
                ruta_prompt(destino).write_text(g["prompt"].rstrip() + "\n", encoding="utf-8")
            finales.append(destino)

    for ruta in finales:
        print(Path(ruta).resolve())
    if len(generadas) < args.count:
        print(f"codex-imagegen: salieron {len(generadas)} de {args.count}.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
