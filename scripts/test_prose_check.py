#!/usr/bin/env python3
"""Valida prose_check.py: fixtures por lenguaje, oráculos, mutantes, --solo-prosa y repos git temporales.

    python3 scripts/test_prose_check.py                         # fixtures + podas + git
    python3 scripts/test_prose_check.py --corpus ~/repo-a ~/repo-b   # + diferencial sobre repos reales

Oráculos, si están: TypeScript (`PROSE_TS=<ruta a node_modules/typescript>`, o el primero que haya en
un corpus), `go/scanner` (`go`), `cpp` y bash. Un oráculo que falta se salta y se dice; ninguno es
obligatorio para las fixtures, que llevan su resultado escrito a mano.
"""
import argparse
import contextlib
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prose_check as pc  # noqa: E402

FIX = {
    "ts": (r"""const a = 1 / 2; // si
const r = /a\/[/]b/g; /* si */
const t = `x ${ {k: 1}.k /* si */ } // no ${`y ${"}"} // no`}`; // si
const s = "// no", q = '/* no */';
const u = /\/\//.source;
let x = a
/ 2 // si
const v = typeof /x\/\//.source;
const w = `http://x`;
const h = sql`${u} ~ ${`^https?://${w}/`}`;
const el = <p className="a">{x}</p>; // si
const j = <b>{x}</b>; const k = `y // no`;
const m = <div>{/* si */}</div>;
const z = <p>{`a ${s ? `c` : 'd'} // no`}</p>;
const g = /[//]/;
const n = <p>don't</p>;
// si
""", {1, 2, 3, 7, 11, 13, 17}),
    "go": ("""package x

import "fmt" // si

var a = `raw // no
/* no */ ` // si
var b = "// no /* no */"
var c = '/' // si
var d = '\\'' /* si */ + 1
/* bloque

   sigue */ var e = 1
func f() { fmt.Println(a, b, c, d, e) } // si
//go:generate echo x
""", {3, 6, 8, 9, 10, 12, 13, 14}),
    "py": ('def f():\n    """doc\n    larga"""\n    x = "# no"  # si\n    return x\n', {2, 3, 4}),
    "c": (r'''#include <x>
int a = 1; // inline
/* bloque
   de varias

   lineas */ int b = 2;
const char* s = "no // es comentario /* tampoco */";
const char* r = R"xy(raw // no
/* tampoco */ )xy";
char c = '/'; char d = '"'; // si
int big = 1'000'000; // separador par
auto u = u8R"(x)"; auto l = L'x'; // si
// solo comentario
std::string url = "http://x";
#define M(x) x /* en macro */
int k = 1'000; // separador impar
int z = 0; // tras impar
''', {2, 3, 4, 6, 10, 11, 12, 13, 15, 16, 17}),
    "sh": (r"""#!/usr/bin/env bash
# shellcheck disable=SC2086
a=1 # si
echo "# no" '# no' $# ${#a} ${a#x} a#b
cat <<EOF
# no, heredoc
EOF
cat <<-'FIN'
	# tampoco
	FIN
x=$((16#ff)) # si
y=$(echo hola # si
)
echo $'a\'#' # si
echo ${x:- #no}
""", {3, 11, 12, 14}),
    "docker": ("""# syntax=docker/dockerfile:1
# escape=\\
# comentario
FROM x AS y
RUN a \\
  # comentario en continuación
  && b # no es comentario
   # con sangría
ENV A=b#c
""", {3, 6, 8}),
    "cmake": ("""set(x "a # no" [[b # no]] c) # si
#[[ bloque
de dos ]] message(y)
set(z a\\\\#b) # si
if(1) #[=[ corto ]=] endif()
set(w [==[ ]] # no ]==])
""", {1, 2, 3, 4, 5}),
    "html": ("""<!doctype html>
<!-- si -->
<style>
a { content: "/* no */"; } /* si */
b { content: "/* no */"; }
</style>
<script>
let s = "</p> // no"; // si
</script>
<p>// no</p>
""", {2, 4, 8}),
}

# (función de medida, texto original, sustituto, lenguaje cuya fixture tiene que cambiar)
MUTANTES = {
    "ts sin plantillas anidadas": ("pila.append(0)", "pass", "ts"),
    "ts sin regex": ("and _regex_puede_empezar(cola.rstrip())", "and False", "ts"),
    "ts con < antes de regex": ('TS_REGEX_TRAS = set("(,=:[!&|?{;+-*%~^")', 'TS_REGEX_TRAS = set("(,=:[!&|?{;+-*%~^<>")', "ts"),
    "ts sin cortar la comilla en el salto": ('while j < fin and src[j] not in (c, "\\n"):', "while j < fin and src[j] != c:", "ts"),
    "go sin raw strings": ('elif c == "`":\n            j = src.find("`", i + 1)', 'elif False:\n            j = src.find("`", i + 1)', "go"),
    "go sin cadenas": ("elif c in \"\\\"'\":\n            j = i + 1\n            while j < n and src[j] not in (c, \"\\n\"):",
                       "elif False:\n            j = i + 1\n            while j < n and src[j] not in (c, \"\\n\"):", "go"),
    "c sin raw strings": ("if (m := RAW.match(s, i)) and", "if (m := None) and", "c"),
    "c sin separador": ("return src[k + 1].isdigit()", "return False", "c"),
    "sh sin heredoc": ('pendientes.append((m.group(1) == "-", m.group(3)))', "pass", "sh"),
    "docker sin directivas": ("if cabecera and DIRECTIVA_DOCKER.match(s):", "if False:", "docker"),
    "cmake sin corchetes": ('elif c == "[" and (i == 0 or src[i - 1] in " \\t\\n(") and (m := CORCHETE.match(src, i)):',
                            "elif False:", "cmake"),
    "html sin javascript": ("res += _trozos_ts(src, m.start(2), m.end(2))", "pass", "html"),
}

# (lenguaje, antes, después, ¿solo prosa?). Los False son el control negativo.
PODAS = [
    ("c", "int a = 1; // largo\n// bloque\nint b;\n", "int a = 1;\nint b;\n", True),
    ("c", "int a = 1; // x\n", "int a = 2; // x\n", False),
    ("c", 'auto s = "// x";\n', 'auto s = "";\n', False),
    ("c", "int a/**/b;\n", "int ab;\n", False),
    ("c", "// a \\\nint x;\nint y;\n", "// a\nint x;\nint y;\n", False),
    ("c", "#pragma once\nint x;\n", "int x;\n", False),
    ("c", 'auto r = R"(a  b\n  c)";\n', 'auto r = R"(a b\n c)";\n', False),
    ("c", "int g(a, /* x\n  y */ b);\n", "int g(a, b);\n", True),
    ("py", 'def f():\n    """doc\n    larga"""\n    return 1  # c\n', 'def f():\n    """doc"""\n    return 1\n', True),
    ("py", 'def f():\n    """doc"""\n', "def f():\n    pass\n", True),
    ("py", 'x = "# no"\n', 'x = ""\n', False),
    ("py", 'def f():\n    """a"""\nprint(f.__doc__)\n', "def f():\n    pass\nprint(f.__doc__)\n", False),
    ("py", "#!/usr/bin/env python3\nx = 1\n", "x = 1\n", False),
    ("py", "import os  # noqa: F401\n", "import os\n", False),
    ("py", "x = ( 1 )  # c\n", "x = 1\n", False),
    ("go", "package x\n\nvar a = 1 // c\n", "package x\n\nvar a = 1\n", True),
    ("go", "package x\n\n/* a\n   b */\nvar a = 1\n", "package x\n\nvar a = 1\n", True),
    ("go", "package x\n\nvar a = 1\n", "package x\n\nvar a = 2\n", False),
    ("go", "//go:build linux\n\npackage x\n", "package x\n", False),
    ("go", "package x\n\nvar a = `x // y`\n", "package x\n\nvar a = `x`\n", False),
    ("go", "package x\n\nvar a = b /* c\n*/ + d\n", "package x\n\nvar a = b + d\n", False),
    ("go", "package x\n\n// #include <a.h>\nimport \"C\"\n", "package x\n\nimport \"C\"\n", False),
    ("ts", "const a = 1; // c\n/** doc */\nexport {};\n", "const a = 1;\nexport {};\n", True),
    ("ts", "// @ts-ignore\nconst a: number = 'x';\n", "const a: number = 'x';\n", False),
    ("ts", "const s = '// no';\n", "const s = '';\n", False),
    ("ts", "const a = b/**/c;\n", "const a = bc;\n", False),
    ("tsx", "const e = (\n  <div>\n    {/* c */}\n    <p />\n  </div>\n);\n",
     "const e = (\n  <div>\n    <p />\n  </div>\n);\n", True),
    ("tsx", "const f = () => {/* nada */};\n", "const f = () => ;\n", False),
    ("sh", "a=1 # c\necho $a\n", "a=1\necho $a\n", True),
    ("sh", 'echo "# x"\n', 'echo ""\n', False),
    ("sh", "#!/bin/bash\necho a\n", "echo a\n", False),
    ("docker", "# c\nFROM x\nRUN a \\\n  # c\n  && b\n", "FROM x\nRUN a \\\n  && b\n", True),
    ("docker", "# syntax=docker/dockerfile:1\nFROM x\n", "FROM x\n", False),
    ("cmake", "set(x a b)\n", "set(x\n  a\n  b)\n", True),
    ("cmake", 'set(x "a # b")\n', 'set(x "a")\n', False),
    ("html", "<script>let a = 1; // c\n</script>\n", "<script>let a = 1;\n</script>\n", True),
]

GO_ORACULO = """package main

import (
	"encoding/json"
	"go/scanner"
	"go/token"
	"os"
	"strings"
)

func main() {
	var files []string
	json.NewDecoder(os.Stdin).Decode(&files)
	out := map[string][]int{}
	for _, f := range files {
		src, _ := os.ReadFile(f)
		lines := strings.Split(string(src), "\\n")
		fset := token.NewFileSet()
		file := fset.AddFile(f, fset.Base(), len(src))
		var s scanner.Scanner
		s.Init(file, src, nil, scanner.ScanComments)
		res := []int{}
		for {
			pos, tok, lit := s.Scan()
			if tok == token.EOF {
				break
			}
			if tok == token.COMMENT {
				l := fset.Position(pos).Line
				for i := 0; i <= strings.Count(lit, "\\n"); i++ {
					if l+i-1 < len(lines) && strings.TrimSpace(lines[l+i-1]) != "" {
						res = append(res, l+i)
					}
				}
			}
		}
		out[f] = res
	}
	json.NewEncoder(os.Stdout).Encode(out)
}
"""

TS_ORACULO = r"""
const ts = require(process.argv[1]); const fs = require('fs');
const out = {};
for (const f of JSON.parse(fs.readFileSync(0, 'utf8'))) {
  const src = fs.readFileSync(f, 'utf8').replace(/\r\n?/g, '\n');
  const ls = [0]; for (let i = 0; i < src.length; i++) if (src[i] === '\n') ls.push(i + 1);
  const fila = p => { let lo = 0, hi = ls.length - 1; while (lo < hi) { const m = (lo + hi + 1) >> 1; if (ls[m] <= p) lo = m; else hi = m - 1; } return lo; };
  const K = ts.ScriptKind, kind = /\.tsx$/.test(f) ? K.TSX : /\.jsx$/.test(f) ? K.JSX : /\.[mc]?js$/.test(f) ? K.JS : K.TS;
  const sf = ts.createSourceFile('f', src, ts.ScriptTarget.Latest, true, kind);
  const prosa = new Set(), vistos = new Set();
  // getChildren() incluye la puntuación, cuyos comentarios forEachChild no ve; y el de fin de línea es trivia trasera.
  const marca = pos => { if (vistos.has(pos)) return; vistos.add(pos);
    for (const c of [...(ts.getLeadingCommentRanges(src, pos) ?? []), ...(ts.getTrailingCommentRanges(src, pos) ?? [])])
      for (let i = fila(c.pos); i <= fila(Math.max(c.end - 1, c.pos)); i++) prosa.add(i + 1); };
  const visita = n => { marca(n.getFullStart()); marca(n.getEnd()); for (const c of n.getChildren(sf)) visita(c); };
  visita(sf);
  const lineas = src.split('\n');
  out[f] = [...prosa].filter(n => (lineas[n - 1] ?? '').trim() !== '');
}
process.stdout.write(JSON.stringify(out));
"""


def lineas(clase: str, src: str) -> set[int]:
    filas = pc._filas(src)
    blancas = {i for i, f in enumerate(filas, 1) if not f.strip()}
    return {x for x in pc.COMENTARIOS[clase](src) if x <= len(filas)} - blancas


def mutante(viejo: str, nuevo: str) -> dict | None:
    codigo = open(pc.__file__, encoding="utf-8").read()
    if viejo not in codigo:
        return None
    ns: dict = {"__name__": "mutante", "__file__": pc.__file__}
    exec(compile(codigo.replace(viejo, nuevo), "mutante", "exec"), ns)
    return ns


class Oraculos:
    """Cada método devuelve {ruta: líneas} o None si su herramienta no está."""

    def __init__(self, corpus: list[str]):
        self.ts = os.environ.get("PROSE_TS") or next(
            (p for d in corpus for p in [os.path.join(d, "node_modules/typescript")] if os.path.isdir(p)), None)
        self.tmp = tempfile.mkdtemp(prefix="prose_oraculos_")
        self.go = None
        if shutil.which("go"):
            src = os.path.join(self.tmp, "main.go")
            open(src, "w").write(GO_ORACULO)
            binario = os.path.join(self.tmp, "gooraculo")
            if subprocess.run(["go", "build", "-o", binario, src], capture_output=True).returncode == 0:
                self.go = binario

    def de_ts(self, rutas: list[str]) -> dict | None:
        if not (self.ts and shutil.which("node")):
            return None
        res = {}
        for k in range(0, len(rutas), 400):
            out = subprocess.run(["node", "-e", TS_ORACULO, self.ts], input=json.dumps(rutas[k:k + 400]),
                                 capture_output=True, text=True, check=True)
            res.update({f: set(v) for f, v in json.loads(out.stdout).items()})
        return res

    def de_go(self, rutas: list[str]) -> dict | None:
        if not self.go:
            return None
        out = subprocess.run([self.go], input=json.dumps(rutas), capture_output=True, text=True, check=True)
        return {f: set(v or []) for f, v in json.loads(out.stdout).items()}

    def de_c(self, src: str) -> set[int] | None:
        if not shutil.which("cpp"):
            return None
        out = subprocess.run(["cpp", "-fpreprocessed", "-dD", "-E", "-x", "c++", "-std=c++17", "-"],
                             input=src, capture_output=True, text=True).stdout.splitlines()
        proc, ln = {}, 1
        for linea in out:
            if m := re.match(r'^# (\d+) "', linea):
                ln = int(m.group(1))
                continue
            proc[ln] = linea
            ln += 1
        sin = lambda s: re.sub(r"\s", "", s)  # noqa: E731
        return {i for i, l in enumerate(src.splitlines(), 1)
                if sin(l) and l.strip() != "#pragma once" and sin(l) != sin(proc.get(i, ""))}

    def cerrar(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def fixtures(ora: Oraculos) -> int:
    fallos = 0
    for clase, (src, esperado) in FIX.items():
        mio = lineas(clase, src)
        print(f"fixture {clase}: {'exacta' if mio == esperado else f'FALLA de más={sorted(mio - esperado)} de menos={sorted(esperado - mio)}'}")
        fallos += mio != esperado
    with tempfile.TemporaryDirectory() as d:
        for clase, ext, metodo in (("ts", "tsx", ora.de_ts), ("go", "go", ora.de_go)):
            ruta = os.path.join(d, f"fixture.{ext}")
            open(ruta, "w").write(FIX[clase][0])
            if (res := metodo([ruta])) is None:
                print(f"oráculo {clase}: SALTADO (falta la herramienta)")
                continue
            ok = res[ruta] == FIX[clase][1]
            print(f"oráculo {clase} sobre su fixture: {'coincide' if ok else f'FALLA {sorted(res[ruta])}'}")
            fallos += not ok
    if (c := ora.de_c(FIX["c"][0])) is not None:
        ok = c == FIX["c"][1]
        print(f"oráculo cpp sobre su fixture: {'coincide' if ok else f'FALLA {sorted(c)}'}")
        fallos += not ok
    for nombre, (viejo, nuevo, clase) in MUTANTES.items():
        if (ns := mutante(viejo, nuevo)) is None:
            print(f"  FALLA mutante «{nombre}»: el texto a mutar ya no existe")
            fallos += 1
            continue
        src = FIX[clase][0]
        muere = ns["COMENTARIOS"][clase](src) != pc.COMENTARIOS[clase](src)
        print(f"mutante «{nombre}»: {'muere' if muere else 'SOBREVIVE'}")
        fallos += not muere
    return fallos


def podas() -> int:
    fallos = 0
    for lang, antes, despues, esperado in PODAS:
        norma = (lambda t: pc.codigo_ts(t, jsx=True)) if lang == "tsx" else pc.CODIGO[lang]
        if lang == "c" and not shutil.which("cpp"):
            continue
        try:
            igual = norma(antes) == norma(despues)
        except (SyntaxError, ValueError, RuntimeError):
            igual = False
        if igual != esperado:
            print(f"  FALLA poda {lang} {antes!r} → {despues!r}: esperaba {esperado}")
            fallos += 1
    print(f"solo-prosa: {len(PODAS) - fallos}/{len(PODAS)} podas con el veredicto esperado")
    return fallos


def corpus(ora: Oraculos, repos: list[str]) -> int:
    """Diferencial línea a línea contra los oráculos, sobre todo lo medible de cada repo."""
    por_clase: dict[str, list[str]] = {}
    for repo in repos:
        pc.ROOT = os.path.abspath(repo)
        cfg = pc.cargar_config(pc.ROOT, os.devnull)
        for rel in pc.archivos(cfg):
            por_clase.setdefault(pc.tipo(rel), []).append(os.path.join(pc.ROOT, rel))
    fallos = 0
    for clase, metodo in (("ts", ora.de_ts), ("js", ora.de_ts), ("go", ora.de_go)):
        rutas = por_clase.get(clase, [])
        if not rutas:
            continue
        if (res := metodo(rutas)) is None:
            print(f"corpus {clase}: SALTADO (falta el oráculo)")
            continue
        malos = []
        for f in rutas:
            src = open(f, encoding="utf-8", errors="replace").read().replace("\r\n", "\n").replace("\r", "\n")
            if (mio := lineas(clase, src)) != res[f]:
                malos.append(f"{f} de más={sorted(mio - res[f])[:5]} de menos={sorted(res[f] - mio)[:5]}")
        print(f"corpus {clase}: {len(rutas) - len(malos)}/{len(rutas)} archivos exactos")
        for m in malos[:20]:
            print(f"  FALLA {m}")
        fallos += bool(malos)
    rutas = por_clase.get("c", [])
    if rutas and shutil.which("cpp"):
        malos = [f for f in rutas if lineas("c", src := open(f, encoding="utf-8", errors="replace").read()) != ora.de_c(src)]
        print(f"corpus c: {len(rutas) - len(malos)}/{len(rutas)} archivos exactos")
        for m in malos[:20]:
            print(f"  FALLA {m}")
        fallos += bool(malos)
    return fallos


# ── repos git temporales ────────────────────────────────────────────────────

def _git(d: str, *args: str) -> str:
    return subprocess.run(["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                           "-c", "core.hooksPath=/dev/null", *args], check=True, capture_output=True, text=True).stdout


def _escribir(d: str, archivos: dict[str, str | None]) -> None:
    for ruta, texto in archivos.items():
        p = os.path.join(d, ruta)
        if texto is None:
            os.remove(p)
            continue
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(texto)


def _commit(d: str, archivos: dict[str, str | None]) -> str:
    if not os.path.isdir(os.path.join(d, ".git")):
        _git(d, "init", "-q")
    _escribir(d, archivos)
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "x", "--allow-empty")
    return _git(d, "rev-parse", "HEAD").strip()


def _main(d: str, *argv: str) -> tuple[int, str]:
    viejo, sys.argv = sys.argv, ["prose_check.py", "-C", d, *argv]
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            try:
                rc = pc.main()
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue()
    finally:
        sys.argv = viejo


def _py(prosa: int, codigo: int = 40) -> str:
    return "".join(f"# p{i}\n" for i in range(prosa)) + "".join(f"x{i} = {i}\n" for i in range(codigo))


def caso_techo(d):
    _commit(d, {"a.py": _py(20)})
    rc, out = _main(d)
    return rc == 1 and "a.py" in out, f"rc={rc}"


def caso_mixta_es_prosa(d):
    _commit(d, {"a.go": "package x\n" + "".join(f"var v{i} = {i} // c\n" for i in range(40))})
    rc, out = _main(d)
    return out.startswith("1 archivos · prosa 40 · código 1 "), out.splitlines()[0] if out else "sin salida"


def caso_ref_malo(d):
    _commit(d, {"a.py": "x = 1\n"})
    rc, _ = _main(d, "--changed", "no-existe-este-ref")
    return rc == 2, f"rc={rc}"


def caso_changed(d):
    _commit(d, {"a.py": _py(20), "b.py": _py(0)})
    _escribir(d, {"b.py": _py(1)})
    rc, out = _main(d, "--changed")
    return rc == 0 and out.startswith("1 archivos"), out.splitlines()[0] if out else "sin salida"


def caso_config(d):
    _commit(d, {"src/a.py": _py(20), "otro/b.py": _py(20), "src/gen/c.py": _py(20), "src/d.go": "package x\n// c\n" * 30,
                ".prose-check.toml": 'paths = ["src"]\nlangs = ["py"]\nskip = ["/gen/"]\n'})
    rc, out = _main(d)
    return out.startswith("1 archivos") and "src/a.py" in out, out.splitlines()[0] if out else "sin salida"


def caso_clave_desconocida(d):
    _commit(d, {"a.py": "x = 1\n", ".prose-check.toml": "techo = 30\n"})
    rc, _ = _main(d)
    return rc == 2, f"rc={rc}"


def caso_exencion(d):
    cabecera = "# prose-check: exento — lo lee el agente\n"
    citada = "# se exime con `# prose-check: exento — <razón>` en la cabecera\n"
    _commit(d, {"a.py": cabecera + _py(20), "b.py": citada + _py(20)})
    rc, out = _main(d)
    return rc == 1 and "exento" in out and "b.py" in out.split("por encima del techo")[-1], f"rc={rc}"


def caso_generado(d):
    _commit(d, {"a.go": "// Code generated by x. DO NOT EDIT.\npackage x\n" + "// c\nvar a = 1\n" * 40,
                "b.py": 'GENERADO = re.compile(r"Code generated .* DO NOT EDIT")\n' + _py(0)})
    rc, out = _main(d)
    return out.startswith("1 archivos") and "a.go" not in out, out.splitlines()[0] if out else "sin salida"


def caso_dos_varas(d):
    base = _commit(d, {"viejo.py": _py(30), "intacto.py": _py(30)})
    _escribir(d, {".prose-check.toml": f'baseline_ref = "{base}"\n'})
    _commit(d, {"viejo.py": _py(30, 41)})  # heredado que mejora: pasa aunque siga sobre el techo
    rc1, _ = _main(d)
    _escribir(d, {"viejo.py": _py(31)})  # heredado que empeora
    rc2, out2 = _main(d, "--changed")
    _escribir(d, {"viejo.py": _py(30), "nuevo.py": _py(30)})  # nuestro sobre el techo
    rc3, out3 = _main(d, "--changed")
    ok = rc1 == 0 and rc2 == 1 and "EMPEORARON" in out2 and rc3 == 1 and "nuevo.py" in out3
    return ok, f"mejora rc={rc1} · empeora rc={rc2} · nuevo rc={rc3}"


def caso_renombre_hereda(d):
    base = _commit(d, {"viejo.py": _py(30)})
    _commit(d, {".prose-check.toml": f'baseline_ref = "{base}"\n'})
    os.rename(os.path.join(d, "viejo.py"), os.path.join(d, "movido.py"))  # sin git add
    rc, out = _main(d, "--changed")
    return rc == 0, f"rc={rc} {out.splitlines()[-1] if out else ''}"


def caso_huella(d):
    base = _commit(d, {"viejo.py": _py(30)})
    _commit(d, {".prose-check.toml": f'baseline_ref = "{base}"\n'})
    _main(d)
    ruta = os.path.join(d, ".git", "prose-baseline.json")
    datos = json.load(open(ruta))
    datos["huella"], datos["files"]["viejo.py"] = "de-otro-medidor", 99.0
    json.dump(datos, open(ruta, "w"))
    _main(d)
    return json.load(open(ruta))["files"]["viejo.py"] != 99.0, "la base de otro medidor no se reconstruyó"


def caso_solo_prosa(d):
    c1 = _commit(d, {"a.go": "package x\n\nvar a = 1 // largo\n", "a.ts": "const a = 1; // c\n"})
    c2 = _commit(d, {"a.go": "package x\n\nvar a = 1\n", "a.ts": "const a = 1;\n"})
    c3 = _commit(d, {"a.go": "package x\n\nvar a = 2\n"})
    rc12, _ = _main(d, "--solo-prosa", f"{c1}..{c2}")
    rc23, out = _main(d, "--solo-prosa", f"{c2}..{c3}")
    return rc12 == 0 and rc23 == 1 and "+var a = 2" in out, f"poda rc={rc12} · cambio rc={rc23}"


def caso_no_parsea(d):
    _commit(d, {"a.py": "def f(:\n    '''doc'''\n"})
    rc, out = _main(d)
    return rc == 1 and "NO SE MIDE" in out, f"rc={rc}"


CASOS_GIT = {
    "techo de una vara": caso_techo,
    "la línea mixta es prosa": caso_mixta_es_prosa,
    "ref malo en --changed": caso_ref_malo,
    "--changed mide solo lo tocado": caso_changed,
    "paths, langs y skip del .toml": caso_config,
    "clave desconocida en el .toml": caso_clave_desconocida,
    "exención en cabecera, no citada": caso_exencion,
    "archivo generado": caso_generado,
    "dos varas": caso_dos_varas,
    "renombre sin git add hereda": caso_renombre_hereda,
    "base de otro medidor se reconstruye": caso_huella,
    "--solo-prosa A..B en Go y TS": caso_solo_prosa,
    "Python que no parsea": caso_no_parsea,
}


def repos_git() -> int:
    fallos = 0
    for nombre, caso in CASOS_GIT.items():
        with tempfile.TemporaryDirectory(prefix="prose_check_") as d:
            try:
                ok, detalle = caso(d)
            except Exception as e:  # noqa: BLE001 — que reviente también es FALLA
                ok, detalle = False, f"{type(e).__name__}: {e}"
        print(f"{nombre}: {'ok' if ok else f'FALLA ({detalle})'}")
        fallos += not ok
    return fallos


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", nargs="*", default=[], help="repos reales para el diferencial contra los oráculos")
    args = ap.parse_args()
    ora = Oraculos([os.path.expanduser(c) for c in args.corpus])
    try:
        fallos = fixtures(ora) + podas() + repos_git()
        if args.corpus:
            fallos += corpus(ora, [os.path.expanduser(c) for c in args.corpus])
    finally:
        ora.cerrar()
    print("\nTODO OK" if not fallos else f"\n{fallos} FALLA(S)")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
