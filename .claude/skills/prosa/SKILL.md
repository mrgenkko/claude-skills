---
name: prosa
description: Mide y gatea la densidad de prosa (comentarios + docstrings) del código con el medidor único del ecosistema (LAIT-DEC-445): techo del 25 % por archivo, objetivo 15 %. Úsala antes de cerrar cualquier cambio de código (gate --changed), cuando el usuario pida podar comentarios o bajar la verbosidad, o para verificar que una poda solo tocó prosa (--solo-prosa). Cubre Python, TypeScript/JavaScript/JSX, Go, C/C++/proto, shell, CMake, Dockerfile y HTML. Uso: /prosa [changed|top|poda <ruta>|solo-prosa <REF|A..B>]
argument-hint: "[changed|top|poda <ruta>|solo-prosa <REF|A..B>]"
allowed-tools: Bash, Read, Edit
---

# prosa · densidad de prosa en el código

**El relato vive en el vault; el código guarda la consecuencia** (LAIT-DEC-445). Un comentario que narra un incidente envejece, nadie lo actualiza y termina mintiendo.

**Script:** `~/Mrgenkko Skills/scripts/prose_check.py` (stdlib, Python ≥ 3.11). Mide el repo git del directorio actual; no hay copias por proyecto.

```bash
PROSA="$HOME/Mrgenkko Skills/scripts/prose_check.py"
python3 "$PROSA" --changed          # EL GATE: solo lo tocado respecto a HEAD (o --changed REF)
python3 "$PROSA"                    # todo el alcance, incluida la deuda heredada
python3 "$PROSA" --top 30           # ranking de los peores, con su bloque de prosa más largo
python3 "$PROSA" --solo-prosa HEAD  # la poda del working tree no tocó código (o A..B entre commits)
```

Exit `0` verde · `1` algo sobre el techo, un heredado que empeoró, un archivo que no se pudo medir o una poda rechazada · `2` error de uso o de git (un ref malo nunca pasa por «nada que ver»).

## La métrica

`ratio = prosa / (prosa + código)`, sin líneas en blanco. **Una línea con código y comentario cuenta como prosa.** Solo se evalúan archivos con ≥ 30 líneas de código: en uno de 12, dos comentarios ya son el 30 % y el dato no significa nada.

| Argumento | Qué hace |
|---|---|
| *(vacío)* o `changed` | `--changed`. Es lo que se deja en verde antes de dar un cambio por cerrado |
| `top` | `--top 30` y comentá los peores |
| `poda <ruta>` | Podar esa ruta con el protocolo de abajo |
| `solo-prosa <REF\|A..B>` | Verificar una poda ya hecha |

## Qué se queda y qué se va

**Se queda, en corto:** qué hace la pieza y su contrato; la trampa accionable («el default NO puede ser USD»); el porqué de un valor no obvio; un puntero de UNA línea al ADR.

**Se va:** la crónica (fechas, cifras de una corrida, duración de un incidente); la narrativa histórica («antes se hacía X», «ya no»); la justificación de más de ~3 líneas (eso es un ADR); lo que repite el código; el código comentado; el TODO sin dueño; las referencias por número de línea (`archivo.py:903`: se cita el símbolo, que no se mueve); los punteros a `temp/` o a docs que ya no existen.

**Al escribir código nuevo, apuntá al 15 %.** Un bloque de prosa de más de ~3 líneas seguidas casi siempre es un ADR disfrazado: la columna `bloque` del `--top` lo delata.

## Protocolo de poda

1. **Por tandas, nunca de golpe**, en commits de solo poda, sin mezclar arreglos.
2. **Antes de borrar un bloque grande, decidí su destino:** si guarda un hecho que costó descubrir y no está ya en un ADR, va al vault primero (con el MCP `focusyn`) y en el código queda el puntero.
3. **Verificá cada tanda con `--solo-prosa`**. Prueba que el código no cambió, **no** que la prosa diga la verdad: releé lo que quedó, porque podar también deja comentarios falsos.
4. Cerrá con `--changed` en verde.

`--solo-prosa` compara el código sin comentarios: en C/C++ la salida de `cpp`; en Python el texto exacto de cada línea (más los docstrings que el archivo lee con `__doc__`); en shell lo que reimprime bash; en Go, TS, JS y HTML, fila por fila. Los comentarios que lee una herramienta son código y quitarlos se rechaza: `#!`, `# noqa`, `# type:`, `//go:build`, `//nolint`, `// @ts-ignore`, `eslint-disable`, `# shellcheck`… En `.tsx`/`.jsx` quitar un hijo `{/* … */}` entero, llaves incluidas, cuenta como poda. Es conservador: un comentario a mitad de línea (`f(a /* x */, b)`) o unir dos líneas se rechaza aunque fuese inocuo.

## Configuración por proyecto: `.prose-check.toml` (opcional, versionado)

En la raíz del repo. Sin él: todo lo versionado, todos los lenguajes, una vara.

```toml
paths = ["apps", "frontend/src"]    # alcance; vacío = todo lo versionado (y lo nuevo sin ignorar)
langs = ["py", "ts"]                # py ts js go c sh cmake docker html; vacío = todos
skip = ["(^|/)migrations/"]         # regex sobre la ruta relativa
ceiling = 25.0
target = 15.0
min_code = 30
baseline_ref = "25b0449"            # activa las dos varas (LAIT-DEC-530)
baseline_file = ""                  # default: .git/prose-baseline.json
```

**Dos varas.** Con `baseline_ref`, un archivo que ya existía en ese commit es *heredado*: solo se le exige no empeorar su propio ratio. Uno nuevo debe quedar bajo el techo. Un archivo renombrado sigue siendo heredado, con o sin `git add`. La línea base es dato derivado: vive en `.git/`, no se versiona y se reconstruye sola si falta o si cambian el ref, el alcance o el medidor (una base congelada por un medidor viejo tapa regresiones reales). **Mover la frontera es cambiar `baseline_ref`, una decisión explícita**: borra la deuda heredada.

Siempre se excluyen `node_modules/`, `vendor/`, `*_pb2.py`, `*.pb.go`, `*.min.js` y todo archivo con `// Code generated … DO NOT EDIT` o `@generated` en su cabecera.

## Exención

La prosa que es **interfaz** (la lee un consumidor externo: docstrings de tools MCP, un `.proto` público) se exime en la cabecera del propio archivo, con su razón. Nunca en una lista central, que se desincroniza:

```python
# prose-check: exento — los docstrings de las tools los lee el agente que las consume
```

Vale en las primeras 40 líneas y solo si abre la línea (tras `#`, `//`, `/*`, `*`, `<!--` o `"""`); citada a mitad de una frase no exime. El gate la lista aparte en vez de fallar.

## Validación

`python3 "$HOME/Mrgenkko Skills/scripts/test_prose_check.py" [--corpus ~/repo …]`: fixtures adversariales por lenguaje, oráculos (parser de TypeScript, `go/scanner`, `cpp`), mutantes que tienen que morir, las podas de `--solo-prosa` y repos git temporales. Con `--corpus`, diferencial línea a línea sobre repos reales. Cualquier cambio al medidor pasa por ahí antes de commitear.

**Límite conocido:** el texto suelto de JSX (`<span>a // b</span>`) cuenta ese `//` como comentario. Falla sobreestimando, que es la dirección segura; si un archivo lo sufre, se exime con su razón.

## Enganchar un proyecto

```bash
mkdir -p .claude/skills
ln -s "$HOME/Mrgenkko Skills/.claude/skills/prosa" .claude/skills/prosa
echo ".claude/skills/prosa" >> .git/info/exclude    # el enlace apunta a una ruta de esta máquina
```

Atajos opcionales del proyecto (Makefile, `package.json` o un hook `pre-commit`) llaman al mismo script: `python3 "$HOME/Mrgenkko Skills/scripts/prose_check.py" --changed`.
