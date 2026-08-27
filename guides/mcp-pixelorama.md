# MCP Pixelorama — dibujar pixel art y entregarlo en `.pxo`

Servidor: `~/.claude/mcp-servers/pixelorama/server.py` (fuente en `deployed/pixelorama/`).
Versión 0.1.0. Depende de `Pillow>=10` y `mcp>=1.0`, en el venv de este repo.

## Por qué no "maneja la app"

Pixelorama es una GUI hecha en Godot: no tiene API de automatización ni CLI de dibujo
(su CLI solo abre, informa y exporta). Automatizar la ventana habría sido frágil y
además inútil — el agente no ve la pantalla.

Así que este MCP **habla su formato**. Escribe y lee `.pxo`, y el archivo es el puente:
el agente dibuja, el humano abre el resultado en la app y lo retoca con todas sus
herramientas. Un editor gráfico ya sabe hacer eso muy bien; lo que le faltaba era alguien
que le dejara el archivo hecho.

## El formato `.pxo`

Verificado contra el fuente de v1.2.1 (`pxo_version` 7) y **comprobado a máquina**: la
propia Pixelorama abre lo que escribimos y su exportación sale idéntica byte a byte.

```
archivo.pxo = ZIP
  data.json                        Project.serialize() (JSON plano)
  mimetype                         "application/x-pixelorama"
  preview.png                      miniatura
  image_data/frames/{F}/layer_{L}  RGBA8 CRUDO, w*h*4 bytes
  image_data/final_images/{F}      el frame compuesto (se escribe, no se lee al abrir)
```

Tres cosas que cuesta descubrir y que aquí ya están pagadas:

- **F y L van en BASE 1.** Con base 0 el proyecto abre vacío y sin un solo error.
- Al deserializar casi todo es opcional (`.get` con default). **Solo dos campos se leen
  directo**, y si faltan la carga revienta: `opacity` en cada cel y `new_cels_linked` en
  cada capa de píxeles.
- `color_mode` es un `Image.Format` de Godot: RGBA8 es **5**. El modo indexado es `-1` y
  arrastra un `indices_layer_{L}` aparte; este MCP no lo maneja y lo dice al abrir.

## Las tools

| Tool | Para qué |
|---|---|
| `status` | Proyecto en memoria: tamaño, capas con su caja de contenido, cambios sin guardar |
| `new_project` / `open_project` | Lienzo nuevo, o abrir un `.pxo` (solo RGBA con capas de píxeles) |
| `save_project` | Escribe el `.pxo` — **esto es lo que se abre en la app** |
| `export_png` | PNG; `scale` con vecino más cercano, `crop_alpha` recorta al contenido |
| `launch` | Abre Pixelorama con un archivo. La ventana la ve el usuario, no el agente |
| `add_layer` / `set_layer` / `delete_layer` | Capas. **El índice 0 es la de ABAJO** (al revés del panel) |
| `add_frame` / `set_frame_duration` | Animación; `copy_from` duplica un frame |
| `draw` | Lote de ops: pixel · line · rect · ellipse · polygon · fill (cubeta) · replace · clear |
| `draw_ascii` | **Dibuja desde un mapa de caracteres + paleta** |
| `read_ascii` | Lee una región como mapa + paleta, con reglas de coordenadas |
| `import_png` | Pega una imagen; `key_color` para quitar un fondo |
| `transform` | flip · rotate90 · shift · center · **outline** · clear |
| `view` | **Devuelve la imagen inline**, escalada a vecino y sobre damero |
| `palette_from_image` | Colores dominantes de una referencia, con su porcentaje |

### Las tres que hacen que esto funcione con un modelo

**`view`** devuelve `ImageContent`, no texto. Dibujar sin mirar no funciona: en el estreno,
las tres primeras pasadas se corrigieron mirando, no razonando.

**`draw_ascii` / `read_ascii`** convierten el pixel art en algo que un modelo puede
escribir y corregir sin contar coordenadas a ciegas:

```jsonc
{"map": "..##..\n.####.\n..##..", "palette": {"#": "#c0c0c0"}, "x": 5, "y": 5}
```

`.` y ` ` significan «no tocar» (no «pintar transparente»). Un símbolo que no está en la
paleta se ignora **y se reporta**, igual que lo que caiga fuera del lienzo — los dos
silencios habituales de este tipo de herramienta.

**`palette_from_image`** evita inventar colores: se saca la paleta de la referencia. Los
píxeles transparentes se excluyen del conteo, así que sobre un PNG ya recortado da la
paleta del objeto y no la del fondo.

### Detalles con consecuencias

- **Todo el dibujo REEMPLAZA el píxel**, no compone, salvo que la op pida `blend: true`.
  En pixel art se quiere el color exacto que se pidió.
- **Sin antialias en ninguna primitiva.** Los bordes salen duros, que es lo correcto.
- `transform outline` contornea **una capa**. Con el dibujo repartido en capas eso mete
  bordes por dentro; para el contorno del sprite entero va
  `{"op": "outline", "from_composite": true, "layer": 0}`, que compone todo y escribe el
  borde en la capa que se le diga (tiene que estar debajo).
- `view` y `export_png` componen **siempre en modo normal**. El `blend_mode` de una capa
  se guarda en el `.pxo` y Pixelorama lo respeta, pero la previsualización de aquí no lo
  aplica: si el dibujo depende de un multiply, hay que mirarlo en la app.
- Topes por memoria (`--max-pixels`, default 1024×1024): un lienzo ocupa `w*h*4` bytes
  **por capa y por frame**, y eso se multiplica rápido al animar.

## Registrarlo en un proyecto

```bash
python3 "~/Mrgenkko Skills/scripts/add-mcp-to-project.py" /ruta/al/proyecto --only pixelorama
```

La instancia es **genérica**: sin `workdir` en `secrets.json`, las rutas relativas se
resuelven contra el directorio desde el que el cliente MCP arranca el server, o sea el
proyecto. Fijar `workdir` solo tiene sentido para atarlo a un repo concreto.

Hay que **reiniciar la ventana** de Claude Code para que cargue.

## Verificar que un `.pxo` está bien, sin abrir la ventana

La CLI de Pixelorama es la única fuente de verdad que no somos nosotros mismos:

```bash
P=~/.local/opt/pixelorama/Pixelorama.x86_64
$P --headless --quit -- --size --framecount archivo.pxo       # metadatos
$P --headless --quit -- --export -o salida.png archivo.pxo    # un PNG por frame
```

Si `--size` imprime el tamaño correcto, lo cargó. Y comparar su export con el de
`export_png` es la prueba de que los píxeles también viajaron. **La ausencia de errores en
el log no prueba nada**: un `.pxo` con los índices en base 0 abre limpio, vacío y callado.

## Gotcha de dibujo: la paridad de las diagonales

Una barra que corre en `(+1,-1)` y engorda en su perpendicular `(+1,+1)` —lo que parece
natural— pinta **medio tablero de ajedrez**: la suma de coordenadas queda siempre par y la
pieza sale con textura de rejilla. Es de las cosas que no se ven razonando y saltan a la
primera mirada.

La regla que funciona: lo que corre en `(+1,+1)` engorda en **horizontal**, lo que corre en
`(+1,-1)` engorda en **vertical**. Cada paso mueve una coordenada en 1, así que la fila (o
columna) contigua cubre sin huecos.

## Estreno

`melquiades-cursor/temp/gothic-anim-2/` — la daga «01 Normal» del kit gótico, 64×64 en seis
capas, con su generador en `fuentes/`.
