#!/usr/bin/env python3
"""Mide la densidad de prosa (comentarios + docstrings) del código de un repo git.

    prose_check.py                    # gate: falla si un archivo supera el techo
    prose_check.py --changed [REF]    # solo lo modificado respecto a REF (HEAD): el gate útil
    prose_check.py --top 40           # ranking de los peores
    prose_check.py --solo-prosa REF   # la poda no tocó código (REF o A..B)

Ratio = prosa / (prosa + código), sin blancos; una línea con código y comentario cuenta
como prosa (LAIT-DEC-445). Mide el repo del directorio actual (o `-C DIR`), configurado
por su `.prose-check.toml`; ver la skill /prosa.
"""
from __future__ import annotations

import argparse
import ast
import bisect
import difflib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
import tomllib
import warnings

DEFAULTS = {"paths": [], "skip": [], "langs": [], "ceiling": 25.0, "target": 15.0,
            "min_code": 30, "baseline_ref": "", "baseline_file": ""}
CONFIG = ".prose-check.toml"
ROOT = ""

EXTENSIONES = {
    "py": {".py", ".pyi"},
    "c": {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".proto"},
    "ts": {".ts", ".tsx", ".mts", ".cts"},
    "js": {".js", ".jsx", ".mjs", ".cjs"},
    "go": {".go"},
    "sh": {".sh", ".bash"},
    "html": {".html", ".htm"},
}
GENERADO_RUTA = re.compile(r"(^|/)(node_modules|vendor)/|_pb2(_grpc)?\.pyi?$|\.pb(\.gw)?\.go$|\.min\.js$")
GENERADO_CABECERA = re.compile(r"^[ \t]*(?://|#|/?\*)[ \t]*(?:Code generated .* DO NOT EDIT|@generated\b)", re.M)
# La exención vive en la cabecera del archivo, con su razón: una lista central se desincroniza.
# Solo cuenta si abre una línea; citada a mitad de una frase no exime.
EXEMPT = re.compile(r"^[ \t]*(?:#+|//+|/\*+|\*|<!--|\"\"\"|''')?[ \t]*prose-check:[ \t]*exento[ \t]*[—–-][ \t]*(.+)",
                    re.M)

RAW = re.compile(r'(?:u8|u|U|L)?R"([^()\\ \t\n]{0,16})\(')
MARCA_CPP = re.compile(r'# (\d+) "[^\n]*')
CORRIDA = re.compile(r"[^\s\"'\\uULR]+")
PRAGMA = re.compile(r"^[ \t]*#[ \t]*pragma\b[^\n]*", re.M)
EFECTO_PY = re.compile(r"#\s*(noqa|type:|pragma\b|fmt:|pylint:|isort:|mypy:|pyright:)", re.I)
CODING = re.compile(r"#.*?coding[:=]")
DOC_NODOS = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
# Un `#` de shell abre comentario al empezar palabra: tras blanco o un operador.
SH_PALABRA = set(" \t\n;&|()<>")
HEREDOC = re.compile(r"<<(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z_0-9]*)\2")
EFECTO_SH = re.compile(r"#\s*shellcheck\b")
# Las directivas del parser de Docker sólo valen en la cabecera, antes de cualquier otra línea.
DIRECTIVA_DOCKER = re.compile(r"#\s*(syntax|escape|check)\s*=", re.I)
HEREDOC_DOCKER = re.compile(r"^[ \t]*(RUN|COPY|ADD)\b[^\n]*<<", re.M | re.I)
CORCHETE = re.compile(r"\[(=*)\[")
BLOQUE_HTML = re.compile(r"<!--.*?-->|<(script|style)\b[^>]*>(.*?)</\1\s*>", re.S | re.I)
# Comentarios que lee el compilador o una herramienta: quitarlos no es podar prosa.
EFECTO_GO = re.compile(r"//(go:|line |export |extern |nolint)|// \+build")
EFECTO_TS = re.compile(r"/[/*]+\s*(@ts-|eslint|prettier-ignore|biome-ignore|istanbul|c8 |@jsx|webpack|vite-ignore"
                       r"|#__PURE__|@__PURE__|<reference|<amd)")
JS_ANTES_DE_REGEX = {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw",
                     "case", "do", "else", "yield", "await"}
# Sin `<` ni `>`: en JSX, `</b>` abriría una regex que se traga lo que siga en la línea.
TS_REGEX_TRAS = set("(,=:[!&|?{;+-*%~^")
LECTORES_DOC = {"help", "getdoc", "render_doc"}


def tipo(rel: str) -> str | None:
    base, ext = os.path.basename(rel), os.path.splitext(rel)[1]
    if base == "CMakeLists.txt" or ext == ".cmake":
        return "cmake"
    if base == "Dockerfile" or base.startswith("Dockerfile.") or base.endswith(".Dockerfile"):
        return "docker"
    return next((k for k, exts in EXTENSIONES.items() if ext in exts), None)


def _filas(src: str) -> list[str]:
    """Filas separadas solo por `\\n`, como las cuentan cpp y tokenize (`splitlines` corta en más)."""
    filas = src.split("\n")
    return filas[:-1] if filas[-1] == "" else filas


def _fila(saltos: list[int], k: int) -> int:
    return bisect.bisect_left(saltos, k) + 1


def _lineas_de(src: str, trozos) -> set[int]:
    saltos = [m.start() for m in re.finditer("\n", src)]
    lineas: set[int] = set()
    for i, j in trozos:
        lineas.update(range(_fila(saltos, i), _fila(saltos, max(i, j - 1)) + 1))
    return lineas


def _fin_cita_sh(src: str, i: int, escapes: bool = True) -> int:
    """Fin de la cita que abre `src[i]` (`'`, `"` o `` ` ``); en la `'` de shell no hay escapes."""
    c, j, n = src[i], i + 1, len(src)
    while j < n and src[j] != c:
        j += 2 if src[j] == "\\" and (escapes and c != "'") else 1
    return min(j + 1, n)


# ── C / C++ / proto ─────────────────────────────────────────────────────────

def _es_separador(src: str, i: int) -> bool:
    """`'` de `1'000'000`: va dentro de un literal numérico, no abre un carácter."""
    if i == 0 or not src[i - 1].isalnum():
        return False
    k = i - 1
    while k >= 0 and (src[k].isalnum() or src[k] in "'."):
        k -= 1
    return src[k + 1].isdigit()


def _fin_literal(s: str, i: int) -> int:
    """Fin del literal que empieza en `i`, o i + 1 si ahí no empieza ninguno."""
    if (m := RAW.match(s, i)) and (i == 0 or not (s[i - 1].isalnum() or s[i - 1] == "_")):
        j = s.find(")" + m.group(1) + '"', m.end())
        return len(s) if j < 0 else j + len(m.group(1)) + 2
    c = s[i]
    if c not in "\"'" or _es_separador(s, i):
        return i + 1
    j = i + 1
    while j < len(s) and s[j] != c and s[j] != "\n":
        j += 2 if s[j] == "\\" else 1
    # Sin cerrar, acaba en el salto de línea, que no se come.
    return j + 1 if j < len(s) and s[j] == c else min(j, len(s))


def _trozos_c(src: str):
    """(tipo, inicio, fin) de cada comentario (`//`, `/*`) y literal (`"`) de `src`, en orden."""
    i, n = 0, len(src)
    while i < n:
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            yield "//", i, j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            yield "/*", i, j
        else:
            j = _fin_literal(src, i)
            if j > i + 1:
                yield '"', i, j
        i = j


def comentarios_c(src: str) -> set[int]:
    return _lineas_de(src, [(i, j) for t, i, j in _trozos_c(src) if t != '"'])


# ── Python ──────────────────────────────────────────────────────────────────

def _docstrings(tree: ast.AST) -> list[tuple[ast.AST, ast.Expr]]:
    return [(n, n.body[0]) for n in ast.walk(tree)
            if isinstance(n, DOC_NODOS) and n.body and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant) and isinstance(n.body[0].value.value, str)]


def comentarios_py(src: str) -> set[int]:
    """Líneas con comentario o docstring. Un archivo que no parsea lanza SyntaxError."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tree = ast.parse(src)
    lineas = {t.start[0] for t in tokenize.generate_tokens(io.StringIO(src).readline)
              if t.type == tokenize.COMMENT}
    for _, doc in _docstrings(tree):
        lineas.update(range(doc.lineno, (doc.end_lineno or doc.lineno) + 1))
    return lineas


# ── Go ──────────────────────────────────────────────────────────────────────

def _trozos_go(src: str) -> list[tuple[int, int]]:
    """Comentarios de Go, fuera de `"…"`, `'…'` y de los raw strings `` `…` ``, que cruzan líneas."""
    res, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            res.append((i, j))
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            res.append((i, j))
            i = j
        elif c == "`":
            j = src.find("`", i + 1)
            i = n if j < 0 else j + 1
        elif c in "\"'":
            j = i + 1
            while j < n and src[j] not in (c, "\n"):
                j += 2 if src[j] == "\\" else 1
            i = j + 1
        else:
            i += 1
    return res


def comentarios_go(src: str) -> set[int]:
    return _lineas_de(src, _trozos_go(src))


# ── TypeScript / JavaScript ─────────────────────────────────────────────────

def _regex_puede_empezar(antes: str) -> bool:
    """¿Una `/` tras este código abre una expresión regular? En la duda es división."""
    if not antes or antes.endswith("=>"):
        return True
    if antes.endswith(("++", "--")) or re.search(r"[\w$)\]]!$", antes):
        return False
    if palabra := re.search(r"(\.?)([A-Za-z_$][\w$]*)$", antes):
        return not palabra.group(1) and palabra.group(2) in JS_ANTES_DE_REGEX
    return antes[-1] in TS_REGEX_TRAS


def _fin_regex(src: str, i: int) -> int:
    """Fin del literal que abre la `/` de `i`, o -1 si la línea no lo cierra."""
    j, en_clase, n = i + 1, False, len(src)
    while j < n and src[j] != "\n":
        c = src[j]
        if c == "\\":
            j += 2
            continue
        if en_clase:
            en_clase = c != "]"
        elif c == "[":
            en_clase = True
        elif c == "/":
            j += 1
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            return j
        j += 1
    return -1


def _trozos_ts(src: str, i: int = 0, fin: int | None = None) -> list[tuple[int, int]]:
    """Comentarios de TS/JS/JSX: sabe de cadenas, plantillas con `${}` anidados y expresiones regulares.

    `cola` guarda el final del código ya visto, con cada literal reducido a `0`, para decidir si una
    `/` divide o abre una regex. Una comilla sin cerrar acaba en el salto de línea: así el apóstrofo
    del texto JSX (`<p>don't</p>`) no se come el resto del archivo.
    """
    fin = len(src) if fin is None else fin
    res, cola, pila = [], "", []

    def plantilla(j: int) -> int:
        while j < fin:
            if src[j] == "\\":
                j += 2
            elif src[j] == "`":
                return j + 1
            elif src.startswith("${", j):
                pila.append(0)
                return j + 2
            else:
                j += 1
        return fin

    while i < fin:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i, fin)
            j = fin if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2, fin)
            j = fin if j < 0 else j + 2
        else:
            j = None
        if j is not None:
            res.append((i, j))
            i = j
            continue
        if c in "'\"":
            j = i + 1
            while j < fin and src[j] not in (c, "\n"):
                j += 2 if src[j] == "\\" else 1
            i, cola = min(j + 1, fin) if j < fin and src[j] == c else j, cola + "0"
        elif c == "`" or (c == "}" and pila and pila[-1] == 0):
            if c == "}":
                pila.pop()
            antes = len(pila)
            i = plantilla(i + 1)
            cola += "(" if len(pila) > antes else "0"
        elif c == "/" and _regex_puede_empezar(cola.rstrip()) and (j := _fin_regex(src, i)) != -1:
            i, cola = j, cola + "0"
        elif c == "\\":
            i += 2
        else:
            if pila and c in "{}":
                pila[-1] += 1 if c == "{" else -1
            i, cola = i + 1, cola + c
        cola = cola[-48:]
    return res


def comentarios_ts(src: str) -> set[int]:
    return _lineas_de(src, _trozos_ts(src))


# ── shell, Dockerfile, CMake, HTML ──────────────────────────────────────────

def _cierre_sh(src: str, i: int) -> int:
    """Fin de `${...}` o de `$((...))`, donde un `#` nunca es comentario."""
    abre, cierra = ("{", "}") if src.startswith("${", i) else ("(", ")")
    nivel, j, n = 0, i + 1, len(src)
    while j < n:
        if src[j] in "'\"`":
            j = _fin_cita_sh(src, j)
            continue
        nivel += (src[j] == abre) - (src[j] == cierra)
        j += 2 if src[j] == "\\" else 1
        if nivel == 0:
            return j
    return n


def _trozos_sh(src: str) -> list[tuple[int, int]]:
    """(inicio, fin) de cada comentario de shell, fuera de citas, expansiones y heredocs."""
    res, pendientes, i, n = [], [], 0, len(src)
    while i < n:
        c = src[i]
        if c == "\n":
            i += 1
            for menos, fin in pendientes:
                while i < n:
                    j = src.find("\n", i)
                    j = n if j < 0 else j
                    linea, i = src[i:j], j + 1
                    if (linea.lstrip("\t") if menos else linea) == fin:
                        break
            pendientes = []
        elif c == "\\":
            i += 2
        elif c in "'\"`":
            i = _fin_cita_sh(src, i)
        elif src.startswith("$'", i):
            j = i + 2
            while j < n and src[j] != "'":
                j += 2 if src[j] == "\\" else 1
            i = j + 1
        elif src.startswith("${", i) or src.startswith("$((", i):
            i = _cierre_sh(src, i)
        elif src.startswith("<<", i) and not src.startswith("<<<", i) and (m := HEREDOC.match(src, i)):
            pendientes.append((m.group(1) == "-", m.group(3)))
            i = m.end()
        elif c == "#" and (i == 0 or src[i - 1] in SH_PALABRA):
            j = src.find("\n", i)
            j = n if j < 0 else j
            res.append((i, j))
            i = j
        else:
            i += 1
    return res


def _con_efecto_sh(src: str, i: int, j: int) -> bool:
    return (i == 0 and src.startswith("#!")) or bool(EFECTO_SH.match(src, i, j))


def comentarios_sh(src: str) -> set[int]:
    return _lineas_de(src, [(i, j) for i, j in _trozos_sh(src) if not _con_efecto_sh(src, i, j)])


def comentarios_docker(src: str) -> set[int]:
    """Líneas que empiezan por `#`, salvo las directivas de la cabecera. Sin heredocs: allí `#` es texto."""
    if HEREDOC_DOCKER.search(src):
        raise ValueError("Dockerfile con heredoc: no se mide")
    lineas, cabecera = set(), True
    for k, linea in enumerate(_filas(src), 1):
        s = linea.lstrip()
        if cabecera and DIRECTIVA_DOCKER.match(s):
            continue
        cabecera = False
        if s.startswith("#"):
            lineas.add(k)
    return lineas


def _fin_corchete(src: str, m: re.Match) -> int:
    fin = src.find("]" + m.group(1) + "]", m.end())
    return len(src) if fin < 0 else fin + len(m.group(1)) + 2


def _trozos_cmake(src: str) -> list[tuple[int, int]]:
    """Comentarios de CMake: `#` hasta el fin de línea, o `#[[...]]`; nunca dentro de `"..."` ni `[[...]]`."""
    res, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c == "\\":
            i += 2
        elif c == '"':
            i = _fin_cita_sh(src, i)
        elif c == "[" and (i == 0 or src[i - 1] in " \t\n(") and (m := CORCHETE.match(src, i)):
            i = _fin_corchete(src, m)
        elif c == "#":
            if m := CORCHETE.match(src, i + 1):
                j = _fin_corchete(src, m)
            else:
                j = src.find("\n", i)
                j = n if j < 0 else j
            res.append((i, j))
            i = j
        else:
            i += 1
    return res


def comentarios_cmake(src: str) -> set[int]:
    return _lineas_de(src, _trozos_cmake(src))


def _trozos_css(src: str, i: int, fin: int) -> list[tuple[int, int]]:
    res = []
    while i < fin:
        if src[i] in "'\"":
            i = _fin_cita_sh(src, i)
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2, fin)
            j = fin if j < 0 else j + 2
            res.append((i, j))
            i = j
        else:
            i += 1
    return res


def _trozos_html(src: str) -> list[tuple[int, int]]:
    res = []
    for m in BLOQUE_HTML.finditer(src):
        if not m.group(1):
            res.append((m.start(), m.end()))
        elif m.group(1).lower() == "script":
            res += _trozos_ts(src, m.start(2), m.end(2))
        else:
            res += _trozos_css(src, m.start(2), m.end(2))
    return res


def comentarios_html(src: str) -> set[int]:
    return _lineas_de(src, _trozos_html(src))


COMENTARIOS = {"c": comentarios_c, "py": comentarios_py, "go": comentarios_go, "ts": comentarios_ts,
               "js": comentarios_ts, "sh": comentarios_sh, "docker": comentarios_docker,
               "cmake": comentarios_cmake, "html": comentarios_html}


def _bloque_max(prose: set[int]) -> int:
    """Corrida más larga de líneas de prosa seguidas: más de ~3 es un ADR, no un comentario."""
    mejor = run = 0
    for ln in sorted(prose):
        run = run + 1 if ln - 1 in prose else 1
        mejor = max(mejor, run)
    return mejor


def ratio(prose: int, code: int) -> float:
    return round(prose / max(1, prose + code) * 100, 1)


def medir(src: str, clase: str) -> dict:
    """`clase`: un lenguaje de COMENTARIOS (`py`, `go`...)."""
    lines = _filas(src)
    blank = {i for i, line in enumerate(lines, 1) if not line.strip()}
    prose = {ln for ln in COMENTARIOS[clase](src) if ln <= len(lines)} - blank
    code = len(lines) - len(prose) - len(blank)
    return {"prose": len(prose), "code": code, "ratio": ratio(len(prose), code), "bloque": _bloque_max(prose)}


def exencion(src: str) -> str | None:
    m = EXEMPT.search("\n".join(src.splitlines()[:40]))
    return m.group(1).strip() if m else None


def analizar(rel: str, ref: str | None = None) -> dict | None:
    src = _leer(rel, ref)
    if not src.strip():
        return None
    return {"path": rel, **medir(src, tipo(rel)), "exempt": exencion(src)}


# ── --solo-prosa: el código que queda al quitar la prosa ────────────────────

class Codigo:
    """El código extraído de un fuente: `clave` es lo que se compara; `lineas`, (fila, texto)
    para enseñar la diferencia."""

    def __init__(self, clave, lineas: list[tuple[int, str]]):
        self.clave, self.lineas = clave, lineas

    def __eq__(self, otro) -> bool:
        return isinstance(otro, Codigo) and self.clave == otro.clave

    __hash__ = None


def _lineas_c(txt: str) -> list[tuple[int, bool, str]]:
    """Líneas lógicas de la salida de cpp: (fila, ¿directiva?, texto).

    Empalma `\\`+salto como el compilador y colapsa el espacio, salvo dentro de los literales,
    que van tal cual. Una directiva nunca se une a la línea siguiente.
    """
    res, fila, i, n = [], 1, 0, len(txt)
    while i < n:
        if m := MARCA_CPP.match(txt, i):
            fila, i = int(m.group(1)), m.end() + 1
            continue
        inicio, trozos, hueco = fila, [], False
        while i < n and txt[i] != "\n":
            if m := CORRIDA.match(txt, i):
                j = m.end()
            elif txt.startswith("\\\n", i):
                i, fila = i + 2, fila + 1
                continue
            elif txt[i] in " \t\v\f":
                i, hueco = i + 1, True
                continue
            else:
                j = _fin_literal(txt, i)
            if hueco and trozos:
                trozos.append(" ")
            trozos.append(txt[i:j])
            fila += txt.count("\n", i, j)
            i, hueco = j, False
        if trozos:
            texto = "".join(trozos)
            res.append((inicio, texto.startswith("#"), texto))
        i, fila = i + 1, fila + 1
    return res


def _crudo_c(src: str) -> list[tuple[int, str]]:
    """Lo que el compilador ve y `cpp -fpreprocessed` no: los `#pragma` (descarta `once`,
    `poison`...) y los `//` que acaban en `\\`, que se comen la línea siguiente."""
    saltos = [m.start() for m in re.finditer("\n", src)]
    res, piezas, k = [], [], 0
    for t, i, j in _trozos_c(src):
        if t == "//" and src[i:j].rstrip(" \t").endswith("\\"):
            res.append((_fila(saltos, i), src[i:j]))
        if t != '"' or "\n" in src[i:j]:
            piezas += [src[k:i], " " + "\n" * src.count("\n", i, j)]
            k = j
    limpio = "".join(piezas) + src[k:]
    for m in PRAGMA.finditer(limpio):
        res.append((limpio.count("\n", 0, m.start()) + 1, _lineas_c(m.group())[0][2]))
    return sorted(res)


def codigo_c(src: str) -> Codigo:
    """Lo que ve el compilador sin comentarios, más lo que cpp calla (`_crudo_c`)."""
    if not shutil.which("cpp"):
        raise RuntimeError("falta `cpp` para verificar C/C++")
    out = subprocess.run(["cpp", "-fpreprocessed", "-dD", "-E", "-x", "c++", "-std=c++17", "-"],
                         input=src.encode("utf-8"), capture_output=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.decode("utf-8", "replace").strip()[:200])
    lineas = _lineas_c(out.stdout.decode("utf-8", "replace"))
    unidades, seguidas = [], []
    for _, directiva, texto in lineas:
        if directiva:
            unidades += [" ".join(seguidas), texto] if seguidas else [texto]
            seguidas = []
        else:
            seguidas.append(texto)
    unidades += [" ".join(seguidas)] if seguidas else []
    crudo = _crudo_c(src)
    vista = sorted([(f, t) for f, _, t in lineas] + [(f, f"[crudo] {t}") for f, t in crudo], key=lambda x: x[0])
    return Codigo((tuple(unidades), tuple(t for _, t in crudo)), vista)


def _leidos(tree: ast.Module) -> set:
    """Dueños de los docstrings que el propio archivo lee: con `__doc__`, `help()` o `getdoc()`.

    Límite: no ve un `__doc__` leído desde otro módulo (ni doctest ni pytest --doctest-modules).
    """
    defs: dict[str, list] = {}
    for n in ast.walk(tree):
        if isinstance(n, DOC_NODOS[1:]):
            defs.setdefault(n.name, []).append(n)
    todos = [n for ns in defs.values() for n in ns]
    leidos: set = set()

    def objeto(expr: ast.AST) -> None:
        leidos.update(defs.get(expr.id, todos) if isinstance(expr, ast.Name) else todos)

    def visita(nodo: ast.AST, clase) -> None:
        for h in ast.iter_child_nodes(nodo):
            if isinstance(h, ast.Name) and h.id == "__doc__":
                leidos.update({tree, clase} - {None})
            elif isinstance(h, ast.Attribute) and h.attr == "__doc__":
                objeto(h.value)
            elif isinstance(h, ast.Constant) and h.value == "__doc__":
                leidos.update([tree, *todos])
            elif isinstance(h, ast.Call) and h.args and \
                    getattr(h.func, "id", getattr(h.func, "attr", None)) in LECTORES_DOC:
                objeto(h.args[0])
            en_funcion = isinstance(h, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            visita(h, h if isinstance(h, ast.ClassDef) else None if en_funcion else clase)

    visita(tree, None)
    return leidos


def _con_efecto(com: str, fila: int) -> bool:
    """Comentario que cambia algo: shebang, codificación, o lo lee una herramienta (noqa...)."""
    return (fila == 1 and com.startswith("#!")) or (fila <= 2 and bool(CODING.match(com))) \
        or bool(EFECTO_PY.search(com))


def _col(filas: list[str], fila: int, col_bytes: int) -> int:
    """Columna en caracteres de un offset en bytes UTF-8, que es como la da `ast`."""
    return len(filas[fila - 1].encode("utf-8")[:col_bytes].decode("utf-8", "replace"))


def codigo_py(src: str) -> Codigo:
    """El texto exacto de cada fila de código, sin comentarios, blancos ni docstrings.

    El AST no basta: pierde el espacio y los paréntesis. Un archivo que no compila lanza
    SyntaxError. Un docstring solo en el cuerpo equivale a `pass`.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        compile(src, "<poda>", "exec", dont_inherit=True)
        tree = ast.parse(src)
    filas = io.StringIO(src).readlines()
    cortes: dict[int, list[tuple[int, int, str]]] = {}
    protegidas: set[int] = set()  # su salto de línea va dentro de un literal
    for t in tokenize.generate_tokens(io.StringIO(src).readline):
        if t.type == tokenize.COMMENT and not _con_efecto(t.string, t.start[0]):
            cortes.setdefault(t.start[0], []).append((t.start[1], t.end[1], ""))
        protegidas.update(range(t.start[0], t.end[0]))
    leidos = _leidos(tree)
    for dueno, doc in _docstrings(tree):
        if dueno in leidos:
            continue
        r0, r1 = doc.lineno, doc.end_lineno or doc.lineno
        c0, c1 = _col(filas, r0, doc.col_offset), _col(filas, r1, doc.end_col_offset or 0)
        sust = "pass" if len(dueno.body) == 1 and not isinstance(dueno, ast.Module) else ""
        for r in range(r0, r1 + 1):
            cortes.setdefault(r, []).append((c0 if r == r0 else 0, c1 if r == r1 else len(filas[r - 1]),
                                             sust if r == r0 else ""))
        protegidas.difference_update(range(r0, r1 + 1))
    lineas = []
    for r, texto in enumerate(filas, 1):
        for c0, c1, sust in sorted(cortes.get(r, ()), reverse=True):
            texto = texto[:c0] + sust + texto[c1:]
        texto = texto.rstrip("\n") if r in protegidas else texto.rstrip()
        if r in protegidas or texto.strip():
            lineas.append((r, texto))
    return Codigo(tuple(t for _, t in lineas), lineas)


def codigo_sh(src: str) -> Codigo:
    """Lo que bash reimprime del script envuelto en una función (`declare -f`): sin comentarios, y con
    el shebang y las directivas de shellcheck aparte, porque bash los calla.

    Corre en un bash restringido, sin entorno ni PATH: el `eval` sólo define la función, y aunque un
    script roto la cerrase antes de tiempo no podría lanzar comandos ni redirigir a ficheros.
    """
    prog = 'IFS= read -r -d "" __src || true; eval "__f() {"$\'\\n\'"$__src"$\'\\n\'"}" && declare -f __f'
    bash = shutil.which("bash") or "/bin/bash"
    out = subprocess.run([bash, "--norc", "--noprofile", "-r", "-c", prog], input=src.encode("utf-8"),
                         capture_output=True, env={"PATH": "/sin-comandos", "LC_ALL": "C.UTF-8"}, timeout=30)
    if out.returncode != 0 or not out.stdout:
        raise RuntimeError(out.stderr.decode("utf-8", "replace").strip()[:200] or "bash no reimprimió nada")
    lineas = list(enumerate(out.stdout.decode("utf-8", "replace").splitlines(), 1))
    efecto = tuple(src[i:j] for i, j in _trozos_sh(src) if _con_efecto_sh(src, i, j))
    return Codigo((tuple(t for _, t in lineas), efecto), lineas)


def _sin_trozos(src: str, trozos) -> str:
    """`src` sin los trozos: cada uno deja sus saltos de línea, o un espacio si no tiene, para que
    `a/**/b` no se lea `ab`."""
    piezas, k = [], 0
    for i, j in sorted(trozos):
        saltos = src.count("\n", i, j)
        piezas += [src[k:i], "\n" * saltos if saltos else " "]
        k = j
    return "".join(piezas) + src[k:]


def _codigo_por_trozos(src: str, trozos) -> Codigo:
    lineas = [(r, t.rstrip()) for r, t in enumerate(_filas(_sin_trozos(src, trozos)), 1) if t.strip()]
    return Codigo(tuple(t for _, t in lineas), lineas)


def codigo_go(src: str) -> Codigo:
    """Las filas de código sin comentarios. Las directivas (`//go:build`, `//nolint`...) son código.

    Compara filas, no fichas: unir dos líneas cambia dónde Go inserta `;`, y se rechaza.
    """
    if re.search(r'^\s*import\s+"C"', src, re.M):
        raise ValueError("cgo: el preámbulo es un comentario que compila")
    return _codigo_por_trozos(src, [(i, j) for i, j in _trozos_go(src) if not EFECTO_GO.match(src, i, j)])


def _hijo_jsx(src: str, i: int, j: int) -> tuple[int, int] | None:
    """`{/* … */}` hijo de un elemento JSX: tras `>` (que no sea `=>`) o tras `}`, y con las llaves
    envolviendo solo el comentario. Entonces las llaves también son prosa. Nunca un bloque `{}`."""
    a, b = i - 1, j
    while a >= 0 and src[a] in " \t\n":
        a -= 1
    while b < len(src) and src[b] in " \t\n":
        b += 1
    if a < 0 or src[a] != "{" or b >= len(src) or src[b] != "}":
        return None
    p = a - 1
    while p >= 0 and src[p] in " \t\n":
        p -= 1
    return (a, b + 1) if p >= 0 and (src[p] == "}" or (src[p] == ">" and src[p - 1:p + 1] != "=>")) else None


def codigo_ts(src: str, jsx: bool = False) -> Codigo:
    """Las filas de código sin comentarios; los que lee una herramienta (`@ts-ignore`...) son código."""
    trozos = [(i, j) for i, j in _trozos_ts(src) if not EFECTO_TS.match(src, i, j)]
    if jsx:
        trozos = [_hijo_jsx(src, i, j) or (i, j) for i, j in trozos]
    return _codigo_por_trozos(src, trozos)


def codigo_docker(src: str) -> Codigo:
    comentarios = comentarios_docker(src)
    lineas = [(r, t.rstrip()) for r, t in enumerate(_filas(src), 1) if t.strip() and r not in comentarios]
    return Codigo(tuple(t for _, t in lineas), lineas)


def codigo_cmake(src: str) -> Codigo:
    """Sin comentarios, y con los blancos entre argumentos colapsados: para CMake no significan nada."""
    base = _codigo_por_trozos(src, _trozos_cmake(src))
    sin = _sin_trozos(src, _trozos_cmake(src))
    fichas, i, n = [], 0, len(sin)
    while i < n:
        if sin[i].isspace():
            i += 1
            continue
        if sin[i] == '"':
            j = _fin_cita_sh(sin, i)
        elif m := CORCHETE.match(sin, i):
            j = _fin_corchete(sin, m)
        else:
            j = i
            while j < n and not sin[j].isspace() and sin[j] not in '"()':
                j += 2 if sin[j] == "\\" else 1
            j = max(j, i + 1)
        fichas.append(sin[i:j])
        i = j
    return Codigo(tuple(fichas), base.lineas)


def codigo_html(src: str) -> Codigo:
    return _codigo_por_trozos(src, _trozos_html(src))


CODIGO = {"c": codigo_c, "py": codigo_py, "go": codigo_go, "ts": codigo_ts, "js": codigo_ts, "sh": codigo_sh,
          "docker": codigo_docker, "cmake": codigo_cmake, "html": codigo_html}


def diferencia(a: Codigo, b: Codigo, maximo: int = 24) -> list[str]:
    """Diff unificado del código extraído, con la fila del fuente de cada lado en `@@`."""
    ta, tb = [t for _, t in a.lineas], [t for _, t in b.lineas]
    res = []
    for grupo in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_grouped_opcodes(1):
        fa = a.lineas[min(grupo[0][1], len(ta) - 1)][0] if ta else 0
        fb = b.lineas[min(grupo[0][3], len(tb) - 1)][0] if tb else 0
        res.append(f"@@ -{fa} +{fb} @@")
        for op, i1, i2, j1, j2 in grupo:
            if op == "equal":
                res += [f" {t}" for t in ta[i1:i2]]
                continue
            res += [f"-{t}" for t in ta[i1:i2]] + [f"+{t}" for t in tb[j1:j2]]
    return res[:maximo] + ([f"… {len(res) - maximo} líneas más"] if len(res) > maximo else [])


def solo_prosa(cfg: dict, rango: str) -> int:
    """Falla si algún cambio de `rango` toca algo más que prosa. `REF` compara REF con el working
    tree; `A..B`, el commit A con el B."""
    if "..." in rango:
        _abortar("--solo-prosa admite REF o A..B, no A...B")
    viejo_ref, dos, nuevo_ref = rango.partition("..")
    viejo_ref, nuevo_ref = viejo_ref or "HEAD", (nuevo_ref or "HEAD") if dos else None
    campos = _git("diff", "--name-status", "-z", viejo_ref, *filter(None, [nuevo_ref]),
                  "--", *cfg["paths"]).split("\0")
    cambios, k = [], 0
    while k < len(campos) and campos[k]:
        n = 2 if campos[k][0] in "RC" else 1
        cambios.append((campos[k][0], campos[k + n]))
        k += 1 + n
    if nuevo_ref is None:
        cambios += [("A", f) for f in _rutas("ls-files", "-z", "--others", "--exclude-standard", "--", *cfg["paths"])]
    ok, malos = [], []
    for estado, f in cambios:
        if f.endswith(".md"):
            continue
        if estado != "M" or not _medible(cfg, f):
            malos.append((f"{estado} {f}: no es una poda verificable", []))
            continue
        viejo, nuevo = _leer(f, viejo_ref), _leer(f, nuevo_ref)
        clase = tipo(f)
        norma = (lambda t: codigo_ts(t, jsx=True)) if f.endswith(("x", "X")) and clase in ("ts", "js") else CODIGO[clase]
        try:
            a, b = norma(viejo), norma(nuevo)
            linea = f"{f}: prosa {medir(viejo, clase)['prose']} → {medir(nuevo, clase)['prose']}"
        except (SyntaxError, ValueError, RuntimeError, tokenize.TokenError, subprocess.TimeoutExpired) as e:
            malos.append((f"{f}: no se puede extraer el código ({type(e).__name__}: {e})", []))
            continue
        if a == b:
            ok.append(linea)
        else:
            malos.append((linea, diferencia(a, b)))
    for linea in ok:
        print(f"  solo prosa  {linea}")
    for linea, diff in malos:
        print(f"  RECHAZADO   {linea}")
        for d in diff:
            print(f"      {d}")
    if not ok and not malos:
        print(f"Nada que verificar en {rango}.")
        return 1
    print(f"{len(ok)} solo prosa · {len(malos)} rechazado(s)")
    return 1 if malos else 0


# ── git, alcance y configuración ────────────────────────────────────────────

def _abortar(motivo: str):
    """Error de uso o de git: sale con 2, para no confundirse con un gate en rojo."""
    print(motivo, file=sys.stderr)
    sys.exit(2)


def _git(*args: str, crudo: bool = False, env: dict | None = None):
    """Salida de git; si git falla, aborta: un ref malo no puede pasar por «nada que ver»."""
    out = subprocess.run(["git", *args], capture_output=True, cwd=ROOT, env=env)
    if out.returncode != 0:
        _abortar(f"git {' '.join(args)}: {out.stderr.decode('utf-8', 'replace').strip()}")
    return out.stdout if crudo else out.stdout.decode("utf-8", "surrogateescape")


def _rutas(*args: str) -> list[str]:
    """Rutas de un comando git con `-z`: sin él, core.quotePath escapa las que no son ASCII."""
    return [f for f in _git(*args).split("\0") if f]


def _leer(rel: str, ref: str | None = None) -> str:
    """El texto de `rel` en `ref`, o en el working tree si no hay ref, decodificado igual."""
    datos = _git("show", f"{ref}:{rel}", crudo=True) if ref else open(os.path.join(ROOT, rel), "rb").read()
    return datos.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")


def cargar_config(raiz: str, ruta: str | None = None) -> dict:
    cfg = dict(DEFAULTS)
    ruta = ruta or os.path.join(raiz, CONFIG)
    if os.path.exists(ruta):
        with open(ruta, "rb") as fh:
            propio = tomllib.load(fh)
        if desconocidas := set(propio) - set(DEFAULTS):
            _abortar(f"{CONFIG}: claves desconocidas {sorted(desconocidas)}")
        cfg.update(propio)
    if malos := set(cfg["langs"]) - set(COMENTARIOS):
        _abortar(f"{CONFIG}: lenguajes desconocidos {sorted(malos)}; hay {sorted(COMENTARIOS)}")
    cfg["skip_re"] = [re.compile(p) for p in cfg["skip"]]
    return cfg


def _medible(cfg: dict, rel: str) -> bool:
    clase = tipo(rel)
    if clase is None or (cfg["langs"] and clase not in cfg["langs"]) or GENERADO_RUTA.search(rel):
        return False
    return not any(p.search(rel) for p in cfg["skip_re"])


def _generado(rel: str) -> bool:
    try:
        with open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace") as fh:
            return bool(GENERADO_CABECERA.search(fh.read(2048)))
    except OSError:
        return False


def _filtrar(cfg: dict, rutas: list[str]) -> list[str]:
    return sorted({f for f in rutas if _medible(cfg, f) and os.path.isfile(os.path.join(ROOT, f))
                   and not _generado(f)})


def archivos(cfg: dict) -> list[str]:
    return _filtrar(cfg, _rutas("ls-files", "-z", "--cached", "--others", "--exclude-standard",
                                "--", *cfg["paths"]))


def modificados(cfg: dict, ref: str) -> list[str]:
    return _filtrar(cfg, _rutas("diff", "--name-only", "-z", "--diff-filter=d", ref, "--", *cfg["paths"])
                    + _rutas("ls-files", "-z", "--others", "--exclude-standard", "--", *cfg["paths"]))


# ── las dos varas (LAIT-DEC-530) ────────────────────────────────────────────

def _ruta_base(cfg: dict) -> str:
    if cfg["baseline_file"]:
        return os.path.join(ROOT, cfg["baseline_file"])
    return os.path.join(ROOT, _git("rev-parse", "--git-path", "prose-baseline.json").strip())


def _huella(cfg: dict) -> str:
    """Ref, medidor y alcance de la línea base: si cambia cualquiera, los ratios guardados ya no
    son comparables. Una base congelada por un medidor anterior tapa regresiones reales."""
    with open(__file__, "rb") as fh:
        medidor = fh.read()
    alcance = json.dumps([_git("rev-parse", cfg["baseline_ref"]).strip(), cfg["paths"], cfg["skip"], cfg["langs"]])
    return hashlib.sha256(medidor + alcance.encode()).hexdigest()[:16]


def congelar(cfg: dict) -> dict[str, float]:
    """El ratio de cada archivo tal como está en `baseline_ref`, también los de menos de min_code
    líneas: si no, al crecer pasarían por nuestros siendo heredados."""
    ref, data = cfg["baseline_ref"], {}
    for rel in _rutas("ls-tree", "-r", "-z", "--name-only", ref, "--", *cfg["paths"]):
        if _medible(cfg, rel):
            try:
                if r := analizar(rel, ref):
                    data[rel] = r["ratio"]
            except (SyntaxError, ValueError):
                continue
    if not data:
        _abortar(f"No se pudo medir ningún archivo en «{ref}». Sin línea base el gate trataría "
                         f"lo heredado como nuestro, así que se detiene.")
    ruta = _ruta_base(cfg)
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as fh:
        json.dump({"huella": _huella(cfg), "ref": ref, "files": data}, fh, indent=1, sort_keys=True)
        fh.write("\n")
    print(f"Línea base congelada en {ref} · {len(data)} archivos heredados → {os.path.relpath(ruta, ROOT)}",
          file=sys.stderr)
    return data


def cargar_base(cfg: dict) -> dict[str, float]:
    """Los ratios heredados, o {} con una sola vara. Es dato derivado y no se versiona: si falta o su
    huella no coincide, se reconstruye; seguir sin ella exigiría el techo a lo heredado, en silencio.
    Mover la frontera es cambiar `baseline_ref` en el .prose-check.toml, no un flag."""
    if not cfg["baseline_ref"]:
        return {}
    try:
        with open(_ruta_base(cfg), encoding="utf-8") as fh:
            base = json.load(fh)
        if base.get("huella") == _huella(cfg):
            return base["files"]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    print(f"Reconstruyendo la línea base desde {cfg['baseline_ref']}…", file=sys.stderr)
    return congelar(cfg)


def renombrados(ref: str) -> dict[str, str]:
    """Destino → origen de lo renombrado respecto a `ref`: un heredado que se mueve sigue siendo
    heredado. Lo no rastreado se da de alta (`add -N`) en una COPIA del índice, así el veredicto no
    depende de haber hecho `git add`."""
    out: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(tmp, "index")}
        real = os.path.join(ROOT, _git("rev-parse", "--git-path", "index").strip())
        if os.path.exists(real):
            shutil.copyfile(real, env["GIT_INDEX_FILE"])
        if nuevos := _rutas("ls-files", "-z", "--others", "--exclude-standard"):
            _git("add", "-N", "--", *nuevos, env=env)
        for linea in _git("diff", "--name-status", "-M", ref, env=env).splitlines():
            partes = linea.split("\t")
            if len(partes) == 3 and partes[0].startswith("R"):
                out[partes[2]] = partes[1]
    return out


# ── principal ───────────────────────────────────────────────────────────────

def main() -> int:
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-C", dest="dir", default=".", help="repo a medir (default: el directorio actual)")
    parser.add_argument("--config", help=f"otra configuración en vez del {CONFIG} del repo")
    parser.add_argument("--paths", nargs="*", help="sustituye los `paths` del .prose-check.toml")
    parser.add_argument("--top", type=int, default=0, help="listar los N peores en vez de aplicar el techo")
    parser.add_argument("--ceiling", type=float)
    parser.add_argument("--changed", metavar="REF", nargs="?", const="HEAD",
                        help="mide solo lo modificado respecto a REF (HEAD por defecto)")
    parser.add_argument("--solo-prosa", metavar="REF|A..B",
                        help="comprueba que los cambios respecto a REF, o de A a B, solo tocan prosa")
    args = parser.parse_args()

    r = subprocess.run(["git", "-C", args.dir, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if r.returncode != 0:
        _abortar(f"{os.path.abspath(args.dir)} no es un repo git")
    ROOT = r.stdout.strip()
    cfg = cargar_config(ROOT, args.config and os.path.abspath(args.config))
    if args.paths is not None:
        cfg["paths"] = args.paths
    ceiling = args.ceiling if args.ceiling is not None else float(cfg["ceiling"])

    if args.solo_prosa:
        return solo_prosa(cfg, args.solo_prosa)

    legacy = cargar_base(cfg)
    files = modificados(cfg, args.changed) if args.changed else archivos(cfg)
    if not files:
        print(f"Sin archivos de código modificados respecto a {args.changed}." if args.changed
              else "Sin archivos que medir.")
        return 0
    rows, errores = [], []
    for f in files:
        try:
            if row := analizar(f):
                rows.append(row)
        except SyntaxError as e:
            errores.append(f"{f}: {e.msg} (línea {e.lineno})")
        except ValueError as e:
            errores.append(f"{f}: {e}")
    rated = [r for r in rows if r["code"] >= cfg["min_code"]]
    tp, tc = sum(r["prose"] for r in rows), sum(r["code"] for r in rows)

    print(f"{len(rows)} archivos · prosa {tp} · código {tc} · global {ratio(tp, tc):.1f} %")
    print(f"objetivo {cfg['target']:.0f} % · techo {ceiling:.0f} % · evaluados {len(rated)} "
          f"(≥ {cfg['min_code']} líneas de código)" + (f" · heredados en base: {len(legacy)}" if legacy else ""))
    for e in errores:
        print(f"  NO SE MIDE  {e}")

    if args.top:
        print()
        for r in sorted(rated, key=lambda r: -r["ratio"])[: args.top]:
            tag = (" heredado" if r["path"] in legacy else " nuestro ") if legacy else ""
            flag = "!!" if r["ratio"] > ceiling and r["path"] not in legacy else "  "
            print(f" {flag} {r['ratio']:5.1f} %{tag}  prosa={r['prose']:4d} código={r['code']:5d}"
                  f"  bloque={r['bloque']:3d}  {r['path']}")
        return 1 if errores else 0

    renames = renombrados(args.changed) if args.changed and legacy else {}
    exentos, nuevos, peor = [], [], []
    for r in rated:
        if r["exempt"]:
            if r["ratio"] > ceiling:
                exentos.append(r)
            continue
        antes = legacy.get(r["path"], legacy.get(renames.get(r["path"], "")))
        if antes is not None:
            if r["ratio"] > antes:
                peor.append({**r, "antes": antes})
        elif r["ratio"] > ceiling:
            nuevos.append(r)

    if exentos:
        print(f"\n{len(exentos)} exento(s) declarado(s):")
        for r in sorted(exentos, key=lambda r: -r["ratio"]):
            print(f"  {r['ratio']:5.1f} %  {r['path']} — {r['exempt']}")
    if nuevos:
        print(f"\n{len(nuevos)} archivo(s) por encima del techo:")
        for r in sorted(nuevos, key=lambda r: -r["ratio"]):
            sobran = r["prose"] - r["code"] * ceiling / (100 - ceiling)
            print(f"  {r['ratio']:5.1f} %  sobran ~{sobran:4.0f} líneas  {r['path']}")
    if peor:
        print(f"\n{len(peor)} archivo(s) heredado(s) que EMPEORARON (basta con dejarlos como estaban):")
        for r in sorted(peor, key=lambda r: r["antes"] - r["ratio"]):
            print(f"  {r['antes']:5.1f} % → {r['ratio']:5.1f} %  {r['path']}")
    if nuevos or peor or errores:
        return 1
    print("\nNada que corregir.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
