---
name: codex-imagegen
description: Genera o edita imágenes raster (fotos, ilustraciones, mockups de UI, propuestas visuales, fondos, texturas, sprites, recortes con fondo transparente) usando el image_gen integrado de Codex CLI, porque Claude no genera imágenes por sí mismo. Guarda cada imagen con su .prompt.txt al lado. Úsala cuando el usuario pida crear, editar o variar una imagen, "usa imagegen", o una propuesta visual antes de implementar. Uso: /codex-imagegen <pedido> [-o salida.png] [-r referencia.png] [-n variantes] [--rewrite]
argument-hint: <pedido> [-o salida.png] [-r referencia.png] [-n variantes] [--rewrite]
allowed-tools: Bash, Read
---

# codex-imagegen · Imágenes con el image_gen de Codex

Claude Code no genera imágenes; Codex CLI sí, con la sesión de ChatGPT que el usuario ya tiene iniciada (sin `OPENAI_API_KEY`). **Vos escribís el prompt final**; el script se lo pasa a Codex, que llama a `image_gen` con ese texto **tal cual**. Después copia la imagen adonde le digas y guarda el prompt que de verdad recibió `image_gen` en `<salida>.prompt.txt`.

**Script:** `~/Mrgenkko Skills/scripts/codex-imagegen.py` (stdlib, corre con cualquier `python3`)

## Cuándo NO usarla

- El repo ya tiene un sistema de iconos, logos o ilustraciones vectoriales → extenderlo en SVG/código, no generar un raster que no encaja.
- Diagramas, wireframes simples, formas, gráficos de datos → SVG, HTML/CSS o canvas.

## Uso

```bash
IMG="$HOME/Mrgenkko Skills/scripts/codex-imagegen.py"

# El prompt va por stdin con heredoc: es multilínea y así no hay que escapar comillas
python3 "$IMG" - -o docs/design/login/login-v10.png -r docs/design/login/login-v09.png <<'EOF'
Use case: precise-object-edit
Asset type: login screen mockup
Input images: Image 1: edit target (login-v09)
Primary request: ...
Constraints: change only ...; keep ... unchanged
EOF
```

- Por **stdout** imprime la ruta final de cada imagen, una por línea. El diagnóstico va a stderr.
- Con `-o` deja **`x.png` + `x.prompt.txt`** juntos. Sin `-o` la imagen queda en `~/.codex/generated_images/<thread>/` y no se guarda el prompt: sirve para previsualizar, **no** para referenciarla desde un proyecto.
- **Nunca pisa:** si `x.png` o `x.prompt.txt` ya existen, guarda `x-v2`, `x-v3`… Mejor nombrá la versión vos (`login-v10.png`), como en `docs/design`.
- Exit `0` ok · `1` Codex no devolvió imagen (el motivo sale en stderr) · `2` falta Codex, la sesión o un argumento.
- **Tarda ~45 s por imagen.** Llamá a Bash con `timeout: 600000`; con `-n 3` o más, `run_in_background: true` y esperá la notificación.

| Flag | Default | Para qué |
|---|---|---|
| `-o, --out` | — | dónde copiar la imagen y su `.prompt.txt` |
| `-r, --ref` | — | imagen a editar o de referencia; repetible, en el orden de `Image 1`, `Image 2`… |
| `-n, --count` | 1 | variantes: una corrida por variante, en serie → `x-1.png`, `x-2.png`… |
| `--rewrite` | apagado | deja que Codex reescriba y amplíe el prompt (ver abajo) |
| `--transparent` | apagado | fondo transparente real (PNG con alfa) |
| `--model` | `gpt-6-astra` | modelo de Codex que hace la llamada a `image_gen` |
| `--effort` | `high` | razonamiento de ese modelo: `low`…`ultra` |
| `--timeout` | 300 | segundos por imagen |
| `--codex` | auto | binario de Codex (también `$CODEX_BIN`) |

## Flujo

1. **Juntá el contexto.** Si es una iteración o una propuesta para algo existente, mirá con `Read` la versión anterior, su `.prompt.txt` y el `README.md` de la carpeta. Codex no ve el proyecto: corre en un directorio vacío.
2. **Escribí el prompt completo** con el esquema de abajo.
3. **Generá.** Una imagen por pedido; variantes con `-n`, nunca varias llamadas en paralelo.
4. **Mirá el resultado con `Read`** antes de decir nada. Si no cumple (texto mal escrito, composición equivocada, se perdió algo que había que conservar), corregí el prompt y regenerá. No entregues algo que no miraste.
5. **Reportá** las rutas como links y describí en una línea qué salió. Si era una propuesta, esperá la elección del usuario antes de implementar o borrar versiones.

## Cada llamada empieza de cero

No hay sesión ni memoria entre llamadas: cada una es un `codex exec` nuevo que genera, responde y termina. No hay nada que cerrar. La continuidad la ponés vos:

- pasá la versión anterior con `-r`;
- **repetí en cada prompt todas las invariantes** (qué no cambia), no sólo el cambio nuevo;
- un cambio por iteración.

## Cómo escribir el prompt

Es el esquema de la skill `imagegen` de Codex: líneas cortas con etiqueta, en este orden, y sólo las que aporten.

```text
Use case: <slug de la lista de abajo>
Asset type: <dónde se usa: hero de landing, mockup de pantalla, fondo de consola…>
Primary request: <el pedido principal>
Input images: <Image 1: edit target; Image 2: style reference> (si hay -r)
Scene/backdrop: <entorno>
Subject: <sujeto principal>
Style/medium: <foto, ilustración, 3D, pixel art…>
Composition/framing: <encuadre, punto de vista, aire para UI o texto>
Lighting/mood: <luz y clima>
Color palette: <colores; hex si están en el design system>
Materials/textures: <superficies>
Text (verbatim): "<texto exacto>"
Constraints: <lo que se conserva / formato y dimensiones>
Avoid: <lo que no tiene que aparecer>
```

**Use case** (slug exacto):
- Generar: `photorealistic-natural` · `product-mockup` · `ui-mockup` · `infographic-diagram` · `scientific-educational` · `ads-marketing` · `productivity-visual` · `logo-brand` · `illustration-story` · `stylized-concept` · `historical-scene`
- Editar: `text-localization` · `identity-preserve` · `precise-object-edit` · `lighting-weather` · `background-extraction` · `style-transfer` · `compositing` · `sketch-to-render`

Reglas que más pesan:
- **Edición:** `change only X; keep Y unchanged`, y enumerá lo que se conserva (dimensiones, encuadre, tipografía, logo, colores). Sin eso el modelo reinterpreta todo.
- **Referencias:** nombralas por índice y rol (`Image 1: edit target`, `Image 2: style reference`), en el mismo orden que los `-r`. No toda imagen adjunta es para editar.
- **Texto dentro de la imagen:** literal entre comillas, con tipografía, tamaño, color y ubicación; las palabras raras, letra por letra. Revisalo letra por letra al mirar el resultado.
- **Formato:** decilo explícito (cuadrado, 16:9, 4:5, 9:16, o las dimensiones de la base, ej. `1672x941`).
- **Transparencia:** `--transparent` y además `Use case: background-extraction` o «genuinely transparent background» en el prompt.
- **No inventes** personajes, objetos, paletas de marca ni slogans que el usuario no pidió.

## `--rewrite`: cuando conviene que Codex escriba el prompt

Por defecto el prompt llega crudo. Si el agente de Codex lo altera igual, el script avisa por stderr, y el `.prompt.txt` guarda lo que llegó de verdad. Con `--rewrite`, Codex lo reescribe y amplía: «una vela encendida, ilustración plana» se convirtió en seis líneas con colores, materiales, fondo y formato. Sirve para explorar desde un pedido escueto. **No sirve** para iterar sobre un diseño, porque Codex no tiene el contexto del proyecto.

## Qué hace por dentro

`codex exec --json --ignore-user-config --sandbox read-only -m gpt-6-astra -c model_reasoning_effort=high`, en un directorio temporal vacío y con el prompt por **stdin**: `-i <FILE>...` es variádico y se comería un prompt posicional como si fuera otra imagen.

- **El prompt real sale del registro de sesión.** El stream `--json` no trae la llamada a `image_gen`. El registro (`~/.codex/sessions/…/rollout-*-<thread_id>.jsonl`) sí: `revisedPrompt` y `savedPath`. El script los lee y **borra el registro**, que repite la imagen en base64 (~1–1,6 MB). Por eso no usa `--ephemeral`.
- `--ignore-user-config`: no arranca los MCPs ni la memoria del `~/.codex/config.toml` del usuario en cada imagen. La sesión (`auth.json`) se sigue leyendo. Como tampoco lee su `model`, el modelo y el esfuerzo van explícitos.
- El binario se resuelve así: `--codex` › `$CODEX_BIN` › `codex` en el `PATH` › el de la extensión ChatGPT de VSCode más nueva (`~/.vscode*/extensions/openai.chatgpt-*/bin/*/codex`).

## Problemas

| Síntoma | Qué hacer |
|---|---|
| `no encontré Codex` | instalar la extensión ChatGPT de VSCode o `npm i -g @openai/codex`, o pasar `--codex` |
| `no tiene sesión iniciada` | el usuario corre `codex login` (es interactivo; no lo hagas vos) |
| `Codex no devolvió imagen` + respuesta de Codex | suele ser un rechazo por política o un límite de uso del plan: leé el mensaje y ajustá el prompt o avisá |
| `alteró el prompt crudo` | el agente tocó el texto; mirá el `.prompt.txt` y la imagen antes de seguir |
| `sin .prompt.txt` | no apareció el registro de sesión (¿cambió el formato de Codex?): la imagen sirve, el prompt no quedó guardado |
| `no terminó en 300 s` | `--timeout 600`; si se repite, puede ser saturación del servicio |

Cada imagen consume la cuota del plan de ChatGPT del usuario: no generes variantes que nadie pidió.
