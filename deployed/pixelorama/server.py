#!/usr/bin/env python3
"""
MCP para Pixelorama — dibujar pixel art y entregarlo en su formato nativo.

Pixelorama es una app GUI (Godot): no tiene CLI de dibujo ni API de automatización,
así que este MCP no "maneja la ventana" — habla su FORMATO. Escribe y lee `.pxo`
(el proyecto de Pixelorama: capas, frames, duraciones, paleta), de modo que el agente
dibuja y el humano abre el resultado y lo retoca a mano con todas las herramientas
de la app. El puente humano↔agente es el archivo, que es justo lo que un editor
gráfico sabe hacer bien.

El formato (verificado contra el fuente de v1.2.1, `pxo_version` 7):
    <archivo>.pxo = ZIP
      · data.json                            proyecto serializado (Project.serialize)
      · mimetype                             "application/x-pixelorama"
      · preview.png                          miniatura del frame actual
      · image_data/frames/{F}/layer_{L}      RGBA8 CRUDO, w*h*4 bytes, F y L en BASE 1
      · image_data/final_images/{F}          frame compuesto (se escribe, no se lee)
    Al deserializar casi todo es opcional (`.get` con default). Solo dos campos se
    leen directo y su ausencia rompe la carga: `opacity` en cada cel y
    `new_cels_linked` en cada capa de píxeles. Ambos van siempre.

Dos decisiones que hacen esto usable por un modelo:
  · `draw_ascii` / `read_ascii` — el pixel art se escribe y se lee como un mapa de
    caracteres con su paleta. Es la única forma de colocar píxeles exactos sin
    contar coordenadas a ciegas, y hace que corregir sea barato: leés la zona,
    cambiás dos caracteres, la volvés a escribir.
  · `view` devuelve la imagen INLINE (ImageContent) escalada con vecino más cercano
    y sobre damero. Dibujar sin mirar no es dibujar.

Todo el dibujo REEMPLAZA el píxel (no compone) salvo que la op pida `blend: true`:
en pixel art se quiere el color exacto que se pidió, no el resultado de mezclarlo.

Uso:
    pip install "Pillow>=10" "mcp>=1.0"
    python3 server.py --workdir /ruta/proyecto
"""

import argparse
import asyncio
import base64
import io
import json
import os
import shutil
import subprocess
import zipfile
from collections import Counter

from PIL import Image, ImageDraw

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

VERSION = "0.1.0"

# --- Constantes del formato (fuente: Pixelorama v1.2.1) -----------------------
PXO_VERSION = 7
PIXELORAMA_VERSION = "1.2.1-stable"
COLOR_MODE_RGBA8 = 5  # Image.FORMAT_RGBA8 de Godot 4
LAYER_TYPE_PIXEL = 0  # Global.LayerTypes.PIXEL
MIMETYPE = "application/x-pixelorama"

BLEND_MODES = {
    "normal": 0, "darken": 1, "multiply": 2, "color_burn": 3, "linear_burn": 4,
    "lighten": 5, "screen": 6, "color_dodge": 7, "add": 8, "overlay": 9,
    "soft_light": 10, "hard_light": 11, "difference": 12, "exclusion": 13,
    "subtract": 14, "divide": 15, "hue": 16, "saturation": 17, "color": 18,
    "luminosity": 19,
}

parser = argparse.ArgumentParser()
parser.add_argument("--name", default="pixelorama")
parser.add_argument("--workdir", default=os.getcwd(),
                    help="raíz contra la que se resuelven las rutas relativas")
parser.add_argument("--pixelorama-bin", default="",
                    help="ejecutable de Pixelorama; vacío = autodetectar")
parser.add_argument("--max-pixels", type=int, default=1048576,
                    help="tope de ancho*alto por lienzo (default 1024x1024)")
parser.add_argument("--max-layers", type=int, default=64)
parser.add_argument("--max-frames", type=int, default=256)
args, _ = parser.parse_known_args()

app = Server(f"pixelorama-{args.name}")


# ── Rutas ─────────────────────────────────────────────────────────────────────
def _path(p: str) -> str:
    """Resuelve contra --workdir. Las absolutas pasan tal cual (es el disco del usuario)."""
    p = os.path.expanduser(p)
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(args.workdir, p))


def _rel(p: str) -> str:
    try:
        return os.path.relpath(p, args.workdir)
    except ValueError:
        return p


def _find_pixelorama() -> str:
    if args.pixelorama_bin:
        return os.path.expanduser(args.pixelorama_bin)
    for cand in (shutil.which("pixelorama"),
                 os.path.expanduser("~/.local/opt/pixelorama/Pixelorama.x86_64"),
                 "/usr/bin/pixelorama", "/opt/pixelorama/Pixelorama.x86_64"):
        if cand and os.path.exists(cand):
            return cand
    return ""


# ── Color ─────────────────────────────────────────────────────────────────────
def _color(spec) -> tuple:
    """'#RGB' | '#RGBA' | '#RRGGBB' | '#RRGGBBAA' | 'none' | [r,g,b,a] → (r,g,b,a)."""
    if spec is None:
        return (0, 0, 0, 0)
    if isinstance(spec, (list, tuple)):
        v = list(spec) + [255] * (4 - len(spec))
        return tuple(max(0, min(255, int(x))) for x in v[:4])
    s = str(spec).strip().lower()
    if s in ("none", "transparent", "clear", ""):
        return (0, 0, 0, 0)
    s = s.lstrip("#")
    if len(s) in (3, 4):
        s = "".join(c * 2 for c in s)
    if len(s) == 6:
        s += "ff"
    if len(s) != 8:
        raise ValueError(f"color inválido: {spec!r} (usá #RRGGBB, #RRGGBBAA o 'none')")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4, 6))


def _hex(rgba: tuple) -> str:
    r, g, b, a = rgba
    return f"#{r:02x}{g:02x}{b:02x}" + ("" if a == 255 else f"{a:02x}")


# ── Modelo ────────────────────────────────────────────────────────────────────
class Capa:
    def __init__(self, name, opacity=1.0, visible=True, locked=False, blend_mode=0):
        self.name = name
        self.opacity = float(opacity)
        self.visible = bool(visible)
        self.locked = bool(locked)
        self.blend_mode = int(blend_mode)


class Proyecto:
    def __init__(self, name, w, h, fps=6.0):
        self.name = name
        self.w, self.h = int(w), int(h)
        self.fps = float(fps)
        self.layers = [Capa("Capa 1")]
        self.durations = [1.0]
        # cels[frame][layer] = PIL.Image RGBA
        self.cels = [[self._vacia()]]
        self.path = ""
        self.sucio = False

    def _vacia(self):
        return Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))

    @property
    def n_frames(self):
        return len(self.cels)

    def cel(self, frame, layer):
        if not (0 <= frame < self.n_frames):
            raise ValueError(f"frame {frame} fuera de rango (hay {self.n_frames})")
        if not (0 <= layer < len(self.layers)):
            raise ValueError(f"capa {layer} fuera de rango (hay {len(self.layers)})")
        return self.cels[frame][layer]

    def componer(self, frame=0, layer=None):
        """Compone las capas visibles de abajo (índice 0) hacia arriba. Solo modo normal."""
        if layer is not None:
            base = self._vacia()
            src = self.cel(frame, layer)
            if self.layers[layer].opacity < 1.0:
                src = self._con_opacidad(src, self.layers[layer].opacity)
            return Image.alpha_composite(base, src)
        out = self._vacia()
        for i, capa in enumerate(self.layers):
            if not capa.visible or capa.opacity <= 0:
                continue
            src = self.cels[frame][i]
            if capa.opacity < 1.0:
                src = self._con_opacidad(src, capa.opacity)
            out = Image.alpha_composite(out, src)
        return out

    @staticmethod
    def _con_opacidad(img, op):
        img = img.copy()
        a = img.getchannel("A").point(lambda v: int(v * op))
        img.putalpha(a)
        return img


_proyecto: Proyecto | None = None


def _p() -> Proyecto:
    if _proyecto is None:
        raise ValueError("no hay proyecto abierto — usá new_project(...) u open_project(path)")
    return _proyecto


def _tope(w, h):
    if w < 1 or h < 1:
        raise ValueError("ancho y alto deben ser >= 1")
    if w * h > args.max_pixels:
        raise ValueError(
            f"{w}x{h} = {w * h} px supera el tope de {args.max_pixels} "
            f"(un lienzo son {w * h * 4 / 1e6:.1f} MB por capa y frame)")


# ── .pxo: escribir ────────────────────────────────────────────────────────────
def _data_json(p: Proyecto) -> dict:
    return {
        "pixelorama_version": PIXELORAMA_VERSION,
        "pxo_version": PXO_VERSION,
        "size_x": p.w,
        "size_y": p.h,
        "color_mode": COLOR_MODE_RGBA8,
        "tile_mode_x_basis_x": 0, "tile_mode_x_basis_y": 0,
        "tile_mode_y_basis_x": 0, "tile_mode_y_basis_y": 0,
        "layers": [{
            "name": c.name,
            "visible": c.visible,
            "locked": c.locked,
            "blend_mode": c.blend_mode,
            "clipping_mask": False,
            "opacity": c.opacity,
            "parent": -1,
            "effects": [],
            "type": LAYER_TYPE_PIXEL,
            "new_cels_linked": False,   # obligatorio: PixelLayer lo lee directo
        } for c in p.layers],
        "frames": [{
            "cels": [{"opacity": 1.0, "z_index": 0} for _ in p.layers],  # opacity obligatorio
            "duration": d,
        } for d in p.durations],
        "tags": [],
        "guides": [],
        "symmetry_points": [p.w / 2.0, p.h / 2.0],
        "current_frame": 0,
        "current_layer": max(0, len(p.layers) - 1),
        "brushes": [],
        "palettes": [],
        "project_current_palette_name": "",
        "reference_images": [],
        "tilesets": [],
        "vanishing_points": [],
        "fps": p.fps,
        "user_data": "",
    }


def _escribir_pxo(p: Proyecto, path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    prev = io.BytesIO()
    p.componer(0).save(prev, format="PNG")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("data.json", json.dumps(_data_json(p)))
        z.writestr("mimetype", MIMETYPE)
        z.writestr("preview.png", prev.getvalue())
        for fi in range(p.n_frames):
            # BASE 1 en frame y capa — con base 0 Pixelorama abre el proyecto vacío.
            z.writestr(f"image_data/final_images/{fi + 1}", p.componer(fi).tobytes())
            for li in range(len(p.layers)):
                z.writestr(f"image_data/frames/{fi + 1}/layer_{li + 1}",
                           p.cels[fi][li].tobytes())


def _leer_pxo(path: str) -> Proyecto:
    with zipfile.ZipFile(path, "r") as z:
        d = json.loads(z.read("data.json").decode("utf-8"))
        w, h = int(d["size_x"]), int(d["size_y"])
        _tope(w, h)
        modo = int(d.get("color_mode", COLOR_MODE_RGBA8))
        if modo != COLOR_MODE_RGBA8:
            raise ValueError(
                f"color_mode {modo}: este MCP solo maneja RGBA8 ({COLOR_MODE_RGBA8}). "
                "Convertí el proyecto a RGBA en Pixelorama (Image > Color mode).")
        p = Proyecto(os.path.basename(path).removesuffix(".pxo"), w, h,
                     float(d.get("fps", 6.0)))
        p.layers = []
        for capa in d.get("layers", []):
            if int(capa.get("type", LAYER_TYPE_PIXEL)) != LAYER_TYPE_PIXEL:
                raise ValueError(
                    f"la capa {capa.get('name', '?')!r} no es de píxeles (grupo/3D/tilemap/audio); "
                    "este MCP solo maneja capas de píxeles")
            p.layers.append(Capa(capa.get("name", "Capa"), capa.get("opacity", 1.0),
                                 capa.get("visible", True), capa.get("locked", False),
                                 int(capa.get("blend_mode", 0))))
        p.durations, p.cels = [], []
        esperado = w * h * 4
        for fi, frame in enumerate(d.get("frames", [])):
            p.durations.append(float(frame.get("duration", 1.0)))
            fila = []
            for li in range(len(p.layers)):
                nombre = f"image_data/frames/{fi + 1}/layer_{li + 1}"
                try:
                    raw = z.read(nombre)
                except KeyError:
                    fila.append(Image.new("RGBA", (w, h), (0, 0, 0, 0)))
                    continue
                if len(raw) != esperado:
                    raise ValueError(f"{nombre}: {len(raw)} bytes, se esperaban {esperado}")
                fila.append(Image.frombytes("RGBA", (w, h), raw))
            p.cels.append(fila)
        if not p.layers or not p.cels:
            raise ValueError("el .pxo no tiene capas o frames")
        p.path = path
    return p


# ── Dibujo ────────────────────────────────────────────────────────────────────
def _flood(img, x, y, color, tolerancia=0):
    w, h = img.size
    if not (0 <= x < w and 0 <= y < h):
        raise ValueError(f"fill fuera del lienzo: ({x},{y})")
    px = img.load()
    objetivo = px[x, y]
    if objetivo == color:
        return 0
    def parecido(c):
        return all(abs(a - b) <= tolerancia for a, b in zip(c, objetivo))
    pila, vistos, n = [(x, y)], set(), 0
    while pila:
        cx, cy = pila.pop()
        if (cx, cy) in vistos or not (0 <= cx < w and 0 <= cy < h):
            continue
        vistos.add((cx, cy))
        if not parecido(px[cx, cy]):
            continue
        px[cx, cy] = color
        n += 1
        pila.extend([(cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)])
    return n


def _reemplazar(img, desde, hasta, tolerancia=0):
    px, w, h = img.load(), *img.size
    n = 0
    for yy in range(h):
        for xx in range(w):
            c = px[xx, yy]
            if all(abs(a - b) <= tolerancia for a, b in zip(c, desde)):
                px[xx, yy] = hasta
                n += 1
    return n


def _contorno(img, color, diagonales=False):
    """Rodea lo opaco con un borde de 1 px POR FUERA. El recurso más usado del pixel art."""
    w, h = img.size
    px = img.load()
    salida = img.copy()
    spx = salida.load()
    vecinos = [(-1, 0), (1, 0), (0, -1), (0, 1)]
    if diagonales:
        vecinos += [(-1, -1), (1, -1), (-1, 1), (1, 1)]
    n = 0
    for yy in range(h):
        for xx in range(w):
            if px[xx, yy][3] != 0:
                continue  # ya hay algo; el contorno va fuera
            for dx, dy in vecinos:
                nx, ny = xx + dx, yy + dy
                if 0 <= nx < w and 0 <= ny < h and px[nx, ny][3] > 0:
                    spx[xx, yy] = color
                    n += 1
                    break
    return salida, n


def _aplicar_ops(img, ops):
    d = ImageDraw.Draw(img)
    hechas = []
    for i, op in enumerate(ops):
        tipo = str(op.get("op", "")).lower()

        def col(k, default=None):
            return _color(op[k]) if op.get(k) is not None else default

        if tipo == "pixel":
            c = col("color")
            pts = op.get("points") or ([op["at"]] if op.get("at") else [])
            px = img.load()
            w, h = img.size
            n = 0
            for x, y in pts:
                x, y = int(x), int(y)
                if 0 <= x < w and 0 <= y < h:
                    px[x, y] = c
                    n += 1
            hechas.append(f"pixel×{n}")
        elif tipo == "line":
            d.line([tuple(op["from"]), tuple(op["to"])], fill=col("color"),
                   width=int(op.get("width", 1)))
            hechas.append("line")
        elif tipo == "rect":
            x0, y0, x1, y1 = op["xy"]
            d.rectangle([x0, y0, x1, y1], fill=col("fill"), outline=col("color"),
                        width=int(op.get("width", 1)))
            hechas.append("rect")
        elif tipo == "ellipse":
            x0, y0, x1, y1 = op["xy"]
            d.ellipse([x0, y0, x1, y1], fill=col("fill"), outline=col("color"),
                      width=int(op.get("width", 1)))
            hechas.append("ellipse")
        elif tipo == "polygon":
            d.polygon([tuple(pt) for pt in op["points"]], fill=col("fill"),
                      outline=col("color"))
            hechas.append("polygon")
        elif tipo == "fill":
            x, y = op["at"]
            n = _flood(img, int(x), int(y), col("color"), int(op.get("tolerance", 0)))
            hechas.append(f"fill×{n}")
        elif tipo == "replace":
            n = _reemplazar(img, _color(op["from"]), _color(op["to"]),
                            int(op.get("tolerance", 0)))
            hechas.append(f"replace×{n}")
        elif tipo == "clear":
            img.paste((0, 0, 0, 0), (0, 0) + img.size)
            hechas.append("clear")
        else:
            raise ValueError(f"op[{i}]: tipo desconocido {tipo!r} (pixel|line|rect|"
                             "ellipse|polygon|fill|replace|clear)")
    return hechas


# ── ASCII ─────────────────────────────────────────────────────────────────────
VACIOS = ".", " ", "·"


def _dibujar_ascii(img, mapa, paleta, ox, oy, vacio=None):
    filas = mapa.split("\n") if isinstance(mapa, str) else list(mapa)
    pal = {k: _color(v) for k, v in paleta.items()}
    vacios = set(VACIOS) if vacio is None else set(vacio)
    px, w, h = img.load(), *img.size
    puestos, fuera, desconocidos = 0, 0, Counter()
    for dy, fila in enumerate(filas):
        for dx, ch in enumerate(fila):
            if ch in vacios and ch not in pal:
                continue
            if ch not in pal:
                desconocidos[ch] += 1
                continue
            x, y = ox + dx, oy + dy
            if 0 <= x < w and 0 <= y < h:
                px[x, y] = pal[ch]
                puestos += 1
            else:
                fuera += 1
    return puestos, fuera, desconocidos, len(filas), max((len(f) for f in filas), default=0)


def _leer_ascii(img, x0, y0, w, h, paleta=None):
    """Región → (mapa, paleta). Si se pasa paleta, respeta sus símbolos."""
    px = img.load()
    iw, ih = img.size
    inv = {}
    if paleta:
        for k, v in paleta.items():
            inv[_color(v)] = k
    simbolos = "#@%*+=oO0xX$&nsuvzwmqpdbACDEFGHJKLMNPQRSTUVWYZ123456789"
    usados = set(inv.values())
    libres = [c for c in simbolos if c not in usados]
    filas = []
    for y in range(y0, y0 + h):
        fila = []
        for x in range(x0, x0 + w):
            if not (0 <= x < iw and 0 <= y < ih):
                fila.append(" ")
                continue
            c = px[x, y]
            if c[3] == 0:
                fila.append(".")
                continue
            if c not in inv:
                if not libres:
                    fila.append("?")
                    continue
                inv[c] = libres.pop(0)
            fila.append(inv[c])
        filas.append("".join(fila))
    pal = {v: _hex(k) for k, v in inv.items()}
    return filas, pal


# ── Render ────────────────────────────────────────────────────────────────────
def _damero(img, casilla=8):
    w, h = img.size
    fondo = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    d = ImageDraw.Draw(fondo)
    for y in range(0, h, casilla):
        for x in range(0, w, casilla):
            if ((x // casilla) + (y // casilla)) % 2:
                d.rectangle([x, y, x + casilla - 1, y + casilla - 1], fill=(203, 203, 203, 255))
    return Image.alpha_composite(fondo, img)


def _rejilla(img, paso, escala):
    d = ImageDraw.Draw(img)
    w, h = img.size
    for x in range(0, w + 1, paso * escala):
        d.line([(x, 0), (x, h)], fill=(0, 0, 0, 60))
    for y in range(0, h + 1, paso * escala):
        d.line([(0, y), (w, y)], fill=(0, 0, 0, 60))
    return img


def _recortar_alfa(img):
    caja = img.getbbox()
    return img.crop(caja) if caja else img


# ── Tools ─────────────────────────────────────────────────────────────────────
async def _tool_status(a):
    bin_px = _find_pixelorama()
    if _proyecto is None:
        return (f"pixelorama v{VERSION} — sin proyecto abierto.\n"
                f"workdir: {args.workdir}\n"
                f"ejecutable: {bin_px or 'NO ENCONTRADO'}\n"
                f"tope: {args.max_pixels} px por lienzo")
    p = _proyecto
    def _contenido(i):
        caja = p.cels[0][i].getbbox()
        if not caja:
            return "vacía en el frame 0"
        return (f"contenido {caja[2] - caja[0]}x{caja[3] - caja[1]} "
                f"en ({caja[0]},{caja[1]})")

    capas = "\n".join(
        f"  [{i}] {c.name!r}  {'visible' if c.visible else 'OCULTA'}"
        f"  opacidad {c.opacity:g}{'  BLOQUEADA' if c.locked else ''}"
        f"  {_contenido(i)}"
        for i, c in enumerate(p.layers))
    return (f"pixelorama v{VERSION} — proyecto {p.name!r}\n"
            f"lienzo: {p.w}x{p.h}  frames: {p.n_frames}  fps: {p.fps:g}\n"
            f"archivo: {_rel(p.path) if p.path else '(sin guardar)'}"
            f"{'  [CAMBIOS SIN GUARDAR]' if p.sucio else ''}\n"
            f"capas (0 = la de abajo):\n{capas}\n"
            f"ejecutable: {bin_px or 'NO ENCONTRADO'}")


async def _tool_new_project(a):
    global _proyecto
    w, h = int(a["width"]), int(a["height"])
    _tope(w, h)
    _proyecto = Proyecto(a.get("name", "sin-titulo"), w, h, float(a.get("fps", 6.0)))
    if a.get("background"):
        _proyecto.cels[0][0].paste(_color(a["background"]), (0, 0, w, h))
    _proyecto.layers[0].name = a.get("layer_name", "Capa 1")
    _proyecto.sucio = True
    return (f"Proyecto {_proyecto.name!r} creado: {w}x{h}, 1 capa, 1 frame.\n"
            "Dibujá con draw_ascii/draw y mirá con view.")


async def _tool_open_project(a):
    global _proyecto
    path = _path(a["path"])
    if not os.path.exists(path):
        raise ValueError(f"no existe: {path}")
    _proyecto = _leer_pxo(path)
    return await _tool_status({})


async def _tool_save_project(a):
    p = _p()
    path = _path(a["path"]) if a.get("path") else p.path
    if not path:
        raise ValueError("no hay ruta: pasá path=... la primera vez")
    if not path.endswith(".pxo"):
        path += ".pxo"
    _escribir_pxo(p, path)
    p.path, p.sucio = path, False
    peso = os.path.getsize(path)
    return (f"Guardado {_rel(path)} ({peso / 1024:.1f} KB) — "
            f"{p.w}x{p.h}, {len(p.layers)} capa(s), {p.n_frames} frame(s).\n"
            "Abrilo en Pixelorama con launch() o desde la app.")


async def _tool_export_png(a):
    p = _p()
    escala = max(1, int(a.get("scale", 1)))
    frame = int(a.get("frame", 0))
    capa = a.get("layer")
    img = p.componer(frame, int(capa) if capa is not None else None)
    if a.get("crop_alpha"):
        img = _recortar_alfa(img)
    dims_base = img.size
    if escala > 1:
        img = img.resize((img.width * escala, img.height * escala), Image.NEAREST)
    path = _path(a["path"])
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    img.save(path, format="PNG")
    return (f"Escrito {_rel(path)}: {img.width}x{img.height}"
            f"{f' (base {dims_base[0]}x{dims_base[1]} ×{escala} vecino más cercano)' if escala > 1 else ''}"
            f", {os.path.getsize(path)} bytes.")


async def _tool_launch(a):
    binario = _find_pixelorama()
    if not binario:
        raise ValueError("no encuentro el ejecutable de Pixelorama; pasá --pixelorama-bin")
    path = _path(a["path"]) if a.get("path") else (_proyecto.path if _proyecto else "")
    cmd = [binario] + ([path] if path else [])
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return f"Lanzado: {' '.join(cmd)}\n(la ventana la ve el usuario, no el agente)"


async def _tool_add_layer(a):
    p = _p()
    if len(p.layers) >= args.max_layers:
        raise ValueError(f"tope de {args.max_layers} capas")
    capa = Capa(a.get("name", f"Capa {len(p.layers) + 1}"), float(a.get("opacity", 1.0)),
                bool(a.get("visible", True)), False,
                BLEND_MODES.get(str(a.get("blend_mode", "normal")).lower(), 0))
    idx = int(a["index"]) if a.get("index") is not None else len(p.layers)
    idx = max(0, min(idx, len(p.layers)))
    p.layers.insert(idx, capa)
    for fila in p.cels:
        fila.insert(idx, Image.new("RGBA", (p.w, p.h), (0, 0, 0, 0)))
    p.sucio = True
    return (f"Capa {capa.name!r} en el índice {idx} (de {len(p.layers)}; "
            "0 es la de abajo, la última es la de arriba).")


async def _tool_set_layer(a):
    p = _p()
    i = int(a["index"])
    if not (0 <= i < len(p.layers)):
        raise ValueError(f"capa {i} fuera de rango (hay {len(p.layers)})")
    c = p.layers[i]
    for k in ("name", "visible", "locked"):
        if a.get(k) is not None:
            setattr(c, k, a[k])
    if a.get("opacity") is not None:
        c.opacity = float(a["opacity"])
    if a.get("blend_mode") is not None:
        c.blend_mode = BLEND_MODES.get(str(a["blend_mode"]).lower(), 0)
    p.sucio = True
    return (f"Capa [{i}] {c.name!r}: visible={c.visible} opacidad={c.opacity:g} "
            f"bloqueada={c.locked} blend={c.blend_mode}")


async def _tool_delete_layer(a):
    p = _p()
    i = int(a["index"])
    if len(p.layers) == 1:
        raise ValueError("no se puede borrar la única capa")
    if not (0 <= i < len(p.layers)):
        raise ValueError(f"capa {i} fuera de rango")
    nombre = p.layers.pop(i).name
    for fila in p.cels:
        fila.pop(i)
    p.sucio = True
    return f"Borrada la capa [{i}] {nombre!r}. Quedan {len(p.layers)}."


async def _tool_add_frame(a):
    p = _p()
    if p.n_frames >= args.max_frames:
        raise ValueError(f"tope de {args.max_frames} frames")
    origen = a.get("copy_from")
    if origen is not None:
        fila = [img.copy() for img in p.cels[int(origen)]]
    else:
        fila = [Image.new("RGBA", (p.w, p.h), (0, 0, 0, 0)) for _ in p.layers]
    p.cels.append(fila)
    p.durations.append(float(a.get("duration", 1.0)))
    p.sucio = True
    return (f"Frame {p.n_frames - 1} añadido (duración {p.durations[-1]:g}, "
            f"{'copia del ' + str(origen) if origen is not None else 'vacío'}). "
            f"Total: {p.n_frames}.")


async def _tool_set_frame_duration(a):
    p = _p()
    f = int(a["frame"])
    if not (0 <= f < p.n_frames):
        raise ValueError(f"frame {f} fuera de rango")
    p.durations[f] = float(a["duration"])
    p.sucio = True
    return (f"Frame {f}: duración {p.durations[f]:g} "
            f"(≈{p.durations[f] / p.fps * 1000:.0f} ms a {p.fps:g} fps)")


async def _tool_draw(a):
    p = _p()
    frame, capa = int(a.get("frame", 0)), int(a.get("layer", 0))
    if p.layers[capa].locked:
        raise ValueError(f"la capa [{capa}] {p.layers[capa].name!r} está bloqueada")
    img = p.cel(frame, capa)
    hechas = _aplicar_ops(img, a["ops"])
    p.sucio = True
    return f"capa[{capa}] frame[{frame}]: {', '.join(hechas)}"


async def _tool_draw_ascii(a):
    p = _p()
    frame, capa = int(a.get("frame", 0)), int(a.get("layer", 0))
    if p.layers[capa].locked:
        raise ValueError(f"la capa [{capa}] {p.layers[capa].name!r} está bloqueada")
    img = p.cel(frame, capa)
    puestos, fuera, desc, alto, ancho = _dibujar_ascii(
        img, a["map"], a["palette"], int(a.get("x", 0)), int(a.get("y", 0)),
        a.get("empty"))
    p.sucio = True
    avisos = []
    if fuera:
        avisos.append(f"{fuera} px CAÍAN FUERA del lienzo {p.w}x{p.h}")
    if desc:
        avisos.append("símbolos sin color en la paleta (ignorados): " +
                      ", ".join(f"{c!r}×{n}" for c, n in desc.most_common(6)))
    return (f"capa[{capa}] frame[{frame}]: {puestos} px desde un mapa de "
            f"{ancho}x{alto} en ({a.get('x', 0)},{a.get('y', 0)})."
            + ("\nAVISO: " + " · ".join(avisos) if avisos else ""))


async def _tool_read_ascii(a):
    p = _p()
    frame = int(a.get("frame", 0))
    capa = a.get("layer")
    img = p.componer(frame, int(capa) if capa is not None else None)
    x0, y0 = int(a.get("x", 0)), int(a.get("y", 0))
    w = int(a.get("width", p.w - x0))
    h = int(a.get("height", p.h - y0))
    if w * h > 16384:
        raise ValueError(f"región {w}x{h} demasiado grande para leer como texto "
                         "(máx 16384 px); pedí una zona menor")
    filas, pal = _leer_ascii(img, x0, y0, w, h, a.get("palette"))
    cab = "   " + "".join(str((x0 + i) % 10) for i in range(w))
    cuerpo = "\n".join(f"{(y0 + i) % 100:3d}{f}" for i, f in enumerate(filas))
    leyenda = "  ".join(f"{k}={v}" for k, v in sorted(pal.items()))
    return (f"región ({x0},{y0}) {w}x{h} de "
            f"{'la capa ' + str(capa) if capa is not None else 'el compuesto'}, "
            f"frame {frame}.  '.' = transparente\n{cab}\n{cuerpo}\n\npaleta: {leyenda}")


async def _tool_import_png(a):
    p = _p()
    frame, capa = int(a.get("frame", 0)), int(a.get("layer", 0))
    src = Image.open(_path(a["path"])).convert("RGBA")
    if a.get("width") or a.get("height"):
        nw = int(a.get("width") or src.width)
        nh = int(a.get("height") or src.height)
        filtro = {"nearest": Image.NEAREST, "lanczos": Image.LANCZOS,
                  "box": Image.BOX}[str(a.get("resample", "nearest")).lower()]
        src = src.resize((nw, nh), filtro)
    if a.get("key_color"):
        clave = _color(a["key_color"])[:3]
        tol = int(a.get("tolerance", 0))
        px = src.load()
        for y in range(src.height):
            for x in range(src.width):
                c = px[x, y]
                if all(abs(v - k) <= tol for v, k in zip(c[:3], clave)):
                    px[x, y] = (0, 0, 0, 0)
    destino = p.cel(frame, capa)
    x, y = int(a.get("x", 0)), int(a.get("y", 0))
    if a.get("blend", True):
        capa_tmp = Image.new("RGBA", destino.size, (0, 0, 0, 0))
        capa_tmp.paste(src, (x, y))
        destino.alpha_composite(capa_tmp)
    else:
        destino.paste(src, (x, y))
    p.sucio = True
    return (f"Pegado {_rel(_path(a['path']))} ({src.width}x{src.height}) en "
            f"capa[{capa}] frame[{frame}] en ({x},{y}).")


async def _tool_transform(a):
    p = _p()
    frame = int(a.get("frame", 0))
    capa = a.get("layer")
    op = str(a["op"]).lower()

    # Contorno del SPRITE, no de una capa suelta: con el dibujo repartido en capas
    # (hoja, guarda, gemas) el contorno de cada una mete bordes por dentro. Se calcula
    # sobre el compuesto y se escribe en una capa destino, que debe ir DEBAJO.
    if op == "outline" and a.get("from_composite"):
        if capa is None:
            raise ValueError("outline desde el compuesto necesita 'layer': la capa "
                             "DESTINO donde escribirlo (creála debajo de todo)")
        destino = int(capa)
        compuesto = p.componer(frame)
        borde, n = _contorno(compuesto, _color(a.get("color", "#000000")),
                             diagonales=bool(a.get("diagonals", False)))
        # _contorno devuelve el original + su borde; nos quedamos solo con el borde.
        solo = Image.new("RGBA", borde.size, (0, 0, 0, 0))
        solo.paste(borde, (0, 0), Image.eval(compuesto.getchannel("A"),
                                             lambda v: 255 if v == 0 else 0))
        p.cels[frame][destino].alpha_composite(solo)
        p.sucio = True
        return (f"Contorno del compuesto ({n} px) escrito en la capa [{destino}] "
                f"{p.layers[destino].name!r} del frame {frame}.")

    indices = range(len(p.layers)) if capa is None else [int(capa)]
    detalle = ""
    for i in indices:
        img = p.cel(frame, i)
        if op == "flip_h":
            nueva = img.transpose(Image.FLIP_LEFT_RIGHT)
        elif op == "flip_v":
            nueva = img.transpose(Image.FLIP_TOP_BOTTOM)
        elif op == "rotate90":
            veces = int(a.get("times", 1)) % 4
            nueva = img
            for _ in range(veces):
                nueva = nueva.transpose(Image.ROTATE_90)
            if nueva.size != (p.w, p.h):
                raise ValueError(f"rotar 90° en un lienzo {p.w}x{p.h} no cuadra "
                                 "(solo en lienzos cuadrados)")
        elif op == "shift":
            dx, dy = int(a.get("dx", 0)), int(a.get("dy", 0))
            nueva = Image.new("RGBA", img.size, (0, 0, 0, 0))
            nueva.paste(img, (dx, dy))
        elif op == "center":
            caja = img.getbbox()
            if not caja:
                continue
            cw, ch = caja[2] - caja[0], caja[3] - caja[1]
            nueva = Image.new("RGBA", img.size, (0, 0, 0, 0))
            nueva.paste(img.crop(caja), ((p.w - cw) // 2, (p.h - ch) // 2))
            detalle = f" (contenido {cw}x{ch})"
        elif op == "outline":
            nueva, n = _contorno(img, _color(a.get("color", "#000000")),
                                 diagonales=bool(a.get("diagonals", False)))
            detalle = f" ({n} px de contorno)"
        elif op == "clear":
            nueva = Image.new("RGBA", img.size, (0, 0, 0, 0))
        else:
            raise ValueError(f"op {op!r} desconocida (flip_h|flip_v|rotate90|shift|"
                             "center|outline|clear)")
        p.cels[frame][i] = nueva
    p.sucio = True
    return (f"{op} aplicado a {'todas las capas' if capa is None else f'la capa [{capa}]'}"
            f" del frame {frame}{detalle}.")


async def _tool_view(a):
    p = _p()
    frame = int(a.get("frame", 0))
    capa = a.get("layer")
    img = p.componer(frame, int(capa) if capa is not None else None)
    escala = int(a.get("scale", 0)) or max(1, min(12, 512 // max(p.w, p.h)))
    if a.get("checker", True):
        img = _damero(img, max(4, min(16, escala * 2)))
    img = img.resize((img.width * escala, img.height * escala), Image.NEAREST)
    if a.get("grid"):
        img = _rejilla(img, int(a.get("grid_step", 8)), escala)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return types.ImageContent(type="image", mimeType="image/png",
                              data=base64.b64encode(buf.getvalue()).decode("ascii"))


#: Verde imposible con el que se tapan los píxeles transparentes antes de cuantizar,
#: para que no arrastren la paleta hacia el negro.
_CENTINELA = (1, 254, 3)


async def _tool_palette_from_image(a):
    src = Image.open(_path(a["path"])).convert("RGBA")
    if a.get("crop"):
        x, y, w, h = [int(v) for v in a["crop"]]
        src = src.crop((x, y, x + w, y + h))
    n = int(a.get("colors", 16))
    mascara = src.getchannel("A").point(lambda v: 255 if v > 8 else 0)
    opacos = sum(c for c, v in (mascara.getcolors() or []) if v == 255)
    if not opacos:
        return "La imagen (o el recorte) no tiene píxeles opacos."
    # Una fila extra de centinela arriba: da un píxel de coordenada CONOCIDA con el
    # que leer el índice de su bucket. Identificarlo por parecido de color no sirve —
    # median cut promedia el bucket y el representativo se va lejos del centinela
    # (visto: un #4cf743 que no existía en la imagen colándose en la paleta).
    extra = 1 if opacos < src.width * src.height else 0
    plano = Image.new("RGB", (src.width, src.height + extra), _CENTINELA)
    plano.paste(src.convert("RGB"), (0, extra), mascara)
    cuant = plano.quantize(colors=n + extra, method=Image.MEDIANCUT)
    idx_centinela = cuant.getpixel((0, 0)) if extra else None
    pal = cuant.getpalette()
    filas = []
    for cuenta, idx in sorted(cuant.getcolors() or [], reverse=True):
        if idx == idx_centinela:
            continue  # el bucket de lo transparente
        r, g, b = pal[idx * 3: idx * 3 + 3]
        filas.append(f"  #{r:02x}{g:02x}{b:02x}  {cuenta * 100 / opacos:5.1f}%")
        if len(filas) == n:
            break
    return (f"{len(filas)} colores dominantes de {_rel(_path(a['path']))}"
            f"{' recorte ' + str(a['crop']) if a.get('crop') else ''} "
            f"({opacos} px opacos):\n" + "\n".join(filas))


_DISPATCH = {
    "status": _tool_status, "new_project": _tool_new_project,
    "open_project": _tool_open_project, "save_project": _tool_save_project,
    "export_png": _tool_export_png, "launch": _tool_launch,
    "add_layer": _tool_add_layer, "set_layer": _tool_set_layer,
    "delete_layer": _tool_delete_layer, "add_frame": _tool_add_frame,
    "set_frame_duration": _tool_set_frame_duration,
    "draw": _tool_draw, "draw_ascii": _tool_draw_ascii, "read_ascii": _tool_read_ascii,
    "import_png": _tool_import_png, "transform": _tool_transform,
    "view": _tool_view, "palette_from_image": _tool_palette_from_image,
}

_COLOR_DOC = "'#RRGGBB', '#RRGGBBAA', '#RGB' o 'none' (transparente)"


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="status",
            description="Estado del proyecto en memoria (tamaño, capas, frames, si hay cambios sin guardar) y si se encontró el ejecutable de Pixelorama. Barato: no toca disco.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="new_project",
            description="Crea un lienzo nuevo en memoria (una capa, un frame). No escribe nada hasta save_project. Para pixel art de cursor, 32x32 o 64x64.",
            inputSchema={"type": "object", "properties": {
                "width": {"type": "integer"}, "height": {"type": "integer"},
                "name": {"type": "string", "default": "sin-titulo"},
                "layer_name": {"type": "string", "default": "Capa 1"},
                "background": {"type": "string", "description": f"color de fondo, {_COLOR_DOC}; omitir = transparente"},
                "fps": {"type": "number", "default": 6.0, "description": "los frames duran 'duration' unidades a estos fps"},
            }, "required": ["width", "height"]},
        ),
        types.Tool(
            name="open_project",
            description="Abre un .pxo existente (capas, frames, duraciones). Solo proyectos RGBA con capas de píxeles: grupos, 3D, tilemap y audio se rechazan con el motivo.",
            inputSchema={"type": "object", "properties": {
                "path": {"type": "string"}}, "required": ["path"]},
        ),
        types.Tool(
            name="save_project",
            description="Escribe el .pxo (formato nativo de Pixelorama: ZIP con data.json + RGBA8 crudo por celda). Es LO QUE SE ABRE EN LA APP para retocar a mano.",
            inputSchema={"type": "object", "properties": {
                "path": {"type": "string", "description": "obligatorio la primera vez; después reusa la ruta"}}},
        ),
        types.Tool(
            name="export_png",
            description="Exporta a PNG. scale escala con VECINO MÁS CERCANO (nunca interpola: eso emborronaría el pixel art). Sin 'layer' exporta el compuesto de las capas visibles.",
            inputSchema={"type": "object", "properties": {
                "path": {"type": "string"},
                "scale": {"type": "integer", "default": 1},
                "frame": {"type": "integer", "default": 0},
                "layer": {"type": "integer", "description": "índice de capa; omitir = compuesto"},
                "crop_alpha": {"type": "boolean", "default": False, "description": "recorta al contenido no transparente"},
            }, "required": ["path"]},
        ),
        types.Tool(
            name="launch",
            description="Abre Pixelorama (la app real) con un archivo. La ventana la ve el USUARIO — el agente no puede leerla; para mirar el dibujo usá view.",
            inputSchema={"type": "object", "properties": {
                "path": {"type": "string", "description": "omitir = el .pxo guardado del proyecto actual"}}},
        ),
        types.Tool(
            name="add_layer",
            description="Añade una capa. OJO con el orden: el índice 0 es la capa de ABAJO y la última es la de arriba (al revés de como se ven en el panel de la app).",
            inputSchema={"type": "object", "properties": {
                "name": {"type": "string"},
                "index": {"type": "integer", "description": "dónde insertarla; omitir = arriba del todo"},
                "opacity": {"type": "number", "default": 1.0},
                "visible": {"type": "boolean", "default": True},
                "blend_mode": {"type": "string", "default": "normal", "description": "normal|multiply|add|screen|overlay|... Se guarda en el .pxo, pero view/export componen SIEMPRE como normal"},
            }},
        ),
        types.Tool(
            name="set_layer",
            description="Cambia nombre, visibilidad, opacidad, bloqueo o modo de fusión de una capa.",
            inputSchema={"type": "object", "properties": {
                "index": {"type": "integer"}, "name": {"type": "string"},
                "visible": {"type": "boolean"}, "locked": {"type": "boolean"},
                "opacity": {"type": "number"}, "blend_mode": {"type": "string"},
            }, "required": ["index"]},
        ),
        types.Tool(
            name="delete_layer",
            description="Borra una capa y su contenido en todos los frames.",
            inputSchema={"type": "object", "properties": {
                "index": {"type": "integer"}}, "required": ["index"]},
        ),
        types.Tool(
            name="add_frame",
            description="Añade un frame al final. copy_from duplica otro frame (lo normal para animar: copiar y retocar).",
            inputSchema={"type": "object", "properties": {
                "duration": {"type": "number", "default": 1.0},
                "copy_from": {"type": "integer", "description": "índice del frame a duplicar"},
            }},
        ),
        types.Tool(
            name="set_frame_duration",
            description="Duración de un frame, en unidades de fps del proyecto (1.0 = 1/fps segundos).",
            inputSchema={"type": "object", "properties": {
                "frame": {"type": "integer"}, "duration": {"type": "number"},
            }, "required": ["frame", "duration"]},
        ),
        types.Tool(
            name="draw",
            description=("Aplica un LOTE de operaciones de dibujo sobre una capa. Cada op REEMPLAZA el píxel "
                         "(no compone) salvo blend:true — en pixel art se quiere el color exacto pedido. Ops: "
                         "{op:'pixel', points:[[x,y],...], color} · {op:'line', from:[x,y], to:[x,y], color, width} · "
                         "{op:'rect', xy:[x0,y0,x1,y1], color(borde), fill, width} · {op:'ellipse', xy:[...], color, fill} · "
                         "{op:'polygon', points:[[x,y],...], fill, color} · {op:'fill', at:[x,y], color, tolerance} (cubeta) · "
                         "{op:'replace', from, to, tolerance} · {op:'clear'}. Sin antialias: el borde sale duro, como debe."),
            inputSchema={"type": "object", "properties": {
                "ops": {"type": "array", "items": {"type": "object"}},
                "layer": {"type": "integer", "default": 0},
                "frame": {"type": "integer", "default": 0},
            }, "required": ["ops"]},
        ),
        types.Tool(
            name="draw_ascii",
            description=("Dibuja desde un MAPA DE CARACTERES con su paleta — la forma fiable de colocar píxeles "
                         "exactos sin contar coordenadas. map: string con saltos de línea (o lista de filas); "
                         "palette: {'#':'#c0c0c0', 'o':'#3355ff'}. '.' y ' ' NO se tocan (dejan lo que hubiera). "
                         "Un símbolo que no esté en la paleta se ignora Y SE REPORTA, igual que lo que caiga fuera del lienzo."),
            inputSchema={"type": "object", "properties": {
                "map": {"type": ["string", "array"], "items": {"type": "string"}},
                "palette": {"type": "object", "description": "símbolo → color"},
                "x": {"type": "integer", "default": 0, "description": "esquina superior izquierda donde se pega el mapa"},
                "y": {"type": "integer", "default": 0},
                "layer": {"type": "integer", "default": 0},
                "frame": {"type": "integer", "default": 0},
                "empty": {"type": "string", "description": "caracteres que significan «no tocar» (default '. ')"},
            }, "required": ["map", "palette"]},
        ),
        types.Tool(
            name="read_ascii",
            description=("Lee una región como mapa de caracteres + la paleta detectada, con reglas de coordenadas. "
                         "Es el compañero de draw_ascii: leés la zona, corregís dos caracteres y la volvés a escribir. "
                         "Máx 16384 px por lectura."),
            inputSchema={"type": "object", "properties": {
                "x": {"type": "integer", "default": 0}, "y": {"type": "integer", "default": 0},
                "width": {"type": "integer"}, "height": {"type": "integer"},
                "layer": {"type": "integer", "description": "omitir = el compuesto"},
                "frame": {"type": "integer", "default": 0},
                "palette": {"type": "object", "description": "fija símbolos para colores que ya conocés"},
            }},
        ),
        types.Tool(
            name="import_png",
            description=("Pega un PNG/JPG en una capa. resample 'nearest' por defecto (no emborrona); 'lanczos' o 'box' "
                         "solo para reducir fotos. key_color + tolerance vuelve transparente un color de fondo."),
            inputSchema={"type": "object", "properties": {
                "path": {"type": "string"},
                "x": {"type": "integer", "default": 0}, "y": {"type": "integer", "default": 0},
                "width": {"type": "integer"}, "height": {"type": "integer"},
                "resample": {"type": "string", "enum": ["nearest", "lanczos", "box"], "default": "nearest"},
                "key_color": {"type": "string"}, "tolerance": {"type": "integer", "default": 0},
                "blend": {"type": "boolean", "default": True, "description": "false = pisa el destino, alfa incluida"},
                "layer": {"type": "integer", "default": 0}, "frame": {"type": "integer", "default": 0},
            }, "required": ["path"]},
        ),
        types.Tool(
            name="transform",
            description=("Transforma una capa (o todas). op: flip_h · flip_v · rotate90 (solo lienzo cuadrado, 'times') · "
                         "shift (dx,dy) · center (centra el contenido por su caja alfa) · outline (contorno de 1 px POR FUERA, "
                         "el recurso más usado del pixel art; 'diagonals' incluye esquinas) · clear. "
                         "outline con from_composite=true contornea el SPRITE ENTERO (todas las capas visibles) y "
                         "escribe el borde en la capa 'layer', que debe estar DEBAJO — es lo que se quiere con el "
                         "dibujo repartido en capas, donde contornear cada una mete bordes por dentro."),
            inputSchema={"type": "object", "properties": {
                "op": {"type": "string", "enum": ["flip_h", "flip_v", "rotate90", "shift", "center", "outline", "clear"]},
                "layer": {"type": "integer", "description": "omitir = todas las capas del frame"},
                "frame": {"type": "integer", "default": 0},
                "dx": {"type": "integer", "default": 0}, "dy": {"type": "integer", "default": 0},
                "times": {"type": "integer", "default": 1},
                "color": {"type": "string", "default": "#000000", "description": "color del contorno"},
                "diagonals": {"type": "boolean", "default": False},
                "from_composite": {"type": "boolean", "default": False, "description": "solo outline: contornea el compuesto y lo escribe en 'layer'"},
            }, "required": ["op"]},
        ),
        types.Tool(
            name="view",
            description=("Devuelve el dibujo INLINE como imagen para poder MIRARLO: escalado con vecino más cercano y "
                         "sobre damero para ver la transparencia. Sin 'layer' muestra el compuesto. Usalo tras cada "
                         "tanda de cambios — dibujar a ciegas no funciona."),
            inputSchema={"type": "object", "properties": {
                "scale": {"type": "integer", "description": "omitir = automático para que quepa en ~512 px"},
                "layer": {"type": "integer"}, "frame": {"type": "integer", "default": 0},
                "checker": {"type": "boolean", "default": True},
                "grid": {"type": "boolean", "default": False},
                "grid_step": {"type": "integer", "default": 8},
            }},
        ),
        types.Tool(
            name="palette_from_image",
            description=("Colores dominantes de una imagen (o de un recorte), con su porcentaje. Para sacar la paleta de "
                         "una referencia antes de dibujar, en vez de inventar los colores."),
            inputSchema={"type": "object", "properties": {
                "path": {"type": "string"},
                "colors": {"type": "integer", "default": 16},
                "crop": {"type": "array", "items": {"type": "integer"}, "description": "[x, y, ancho, alto]"},
            }, "required": ["path"]},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
    try:
        fn = _DISPATCH.get(name)
        if not fn:
            return [types.TextContent(type="text", text=f"Tool desconocido: {name}")]
        salida = await fn(arguments or {})
    except Exception as e:
        salida = f"Error pixelorama ({name}): {e}"
    if isinstance(salida, (types.ImageContent, types.TextContent)):
        return [salida]
    return [types.TextContent(type="text", text=str(salida))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
