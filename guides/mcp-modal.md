# MCP Modal — apps, endpoints de inferencia y costo en modal.com

Servidor: `~/.claude/mcp-servers/modal/server.py` (fuente en `deployed/modal/`).
Versión 0.1.4. Envuelve la CLI `modal` (probado contra **1.5.5**), instalada en el venv
de este repo. Instalar con `scripts/install-modal-mcp.sh`.

## Por qué el token va por env (y no hay `modal setup`)

La resolución de config de Modal es **`env MODAL_<KEY>` > perfil de `~/.modal.toml` > default**.
Poniendo `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` en el entorno del subprocess, la CLI queda
**stateless**: no lee ni escribe el perfil activo, así que el workspace no puede derivar
entre instancias. Es exactamente lo que hace `gh` con `GH_TOKEN` — *más simple que gcloud,
que necesita `CLOUDSDK_CONFIG` para aislar el estado en disco*. `MODAL_CONFIG_PATH` apunta
igual a un TOML aparte, como defensa en profundidad para cualquier subcomando que intente
tocar config local.

Consecuencia práctica: **`modal setup` y `modal token new` están bloqueados** y no hacen
falta. El token se genera una vez en el dashboard (`Settings → API Tokens`) y se guarda en el
llavero del sistema (`mcp-secrets.py set modal-<ws>/token_id` y `.../token_secret`). El
lanzador (`scripts/mcp-launch.py`) lo pone en el entorno del server al arrancar, nunca en
`args`: la línea de comandos de un proceso la lee cualquier usuario con `ps aux`, mientras que
`/proc/<pid>/environ` es 0400 del dueño. Ver [secrets.md](secrets.md).

## Tres gates, porque acá el cómputo se paga por segundo

En los otros MCPs alcanza con `allow_write`. En Modal no: leer logs y **prender una GPU**
no pueden ser el mismo permiso.

| Nivel | Cubre | Cómo se habilita |
|---|---|---|
| lectura | `list`, `logs`, `history`, `billing` | siempre |
| escritura | `deploy`, `app stop\|rollback`, `secret/volume create\|rm` | `allow_write: true` |
| cómputo | `run`, `endpoint create` — arrancan hardware facturado | `allow_gpu: true` (**además** de write) |
| bloqueado | `setup`, `token new\|set`, `shell`, `serve`, `bootstrap`, `dashboard`, `profile`, `config set`, `container exec`, y **cualquier `--follow`** | no hay opt-in |

Lo bloqueado no es una preferencia: todos esos comandos esperan un tty, abren el navegador
o no terminan nunca. Cualquiera de ellos **cuelga el canal JSON-RPC** y mata la sesión MCP.
Para logs se usa `app_logs` con `tail`/`since`, que leen un rango cerrado.

`deploy` es escritura pero **no** cómputo: construye la imagen y registra la app; el
hardware se prende recién cuando se invoca una función. Por eso vive en `allow_write`.

## Las tools

| Tool | Para qué |
|---|---|
| `whoami` | Contra qué workspace apunta la instancia (`token info`). Primera llamada útil |
| `app_list` | Apps desplegadas/corriendo/detenidas — devuelve `app_id` (`ap-*`), estado, tareas |
| `app_history` | Versiones de deploy, tags, cuál está activa (para elegir el rollback) |
| `app_logs` | Logs en rango **cerrado**: `tail`, `since` ('30m','2h','1d'), `until`, `search`, `source` |
| `container_list` | Contenedores vivos ahora — o sea, **lo que está facturando** |
| `container_logs` | Logs de un contenedor puntual (`ta-*`). No acepta `-e`: el ID ya lo identifica |
| `endpoint_list` | Endpoints de inferencia LLM gestionados |
| `volume_list` / `volume_ls` | Volúmenes persistentes y su contenido — ahí viven los pesos |
| `secret_list` | Nombres de secrets (nunca los valores) |
| `environment_list` | Environments del workspace (`main`, `dev`...) |
| `billing` | `summary` = consumo del workspace · `rates` = precios vigentes por hardware |
| `dashboard_url` | **Construye** la URL, no abre el navegador (`modal dashboard` está bloqueado) |
| `costo_ahora` | Qué se está facturando **en este momento**: contenedores vivos y su antigüedad |
| `image_logs` | Log de build de una imagen — lo único que explica un deploy que falla construyendo |
| `modal` | Escape hatch: cualquier comando de la CLI, con los mismos gates aplicados |
| `deploy` | Despliega desde archivo o módulo. `cwd` = raíz del proyecto. Timeout default 600 s |
| `app_stop` · `app_rollback` | Detener (corta la facturación) · volver a una versión anterior |
| `volume_create/put/get/rm` · `secret_create` · `endpoint_stop` | Escritura gateada |
| `run` | Corre una función. **Prende cómputo** |
| `endpoint_create` | Levanta un servidor de inferencia LLM desde un modelo de HF, sin escribir código. **Prende cómputo** |

Todas aceptan `environment` (default el de la instancia) y las de listado devuelven
**`--json` por defecto** (`json: false` da la tabla de rich).

## Config en `secrets.json`

```json
{
  "name": "modal-mrgenkko",
  "type": "modal",
  "workspace": "mrgenkko",
  "environment": "main",
  "token_id": "keyring:modal-mrgenkko/token_id",
  "token_secret": "keyring:modal-mrgenkko/token_secret",
  "config_path": "/home/melquiades/.config/modal/mrgenkko.toml",
  "modal_bin": "",
  "allow_write": false,
  "allow_gpu": false,
  "timeout_ms": 900000
}
```

`modal_bin` vacío = el binario del mismo venv que corre el server. **No se resuelve por
PATH**: el cliente MCP solo hereda una whitelist (`HOME/PATH/SHELL/TERM/USER`) y ese PATH
no incluye el `bin/` del venv, así que un `modal` pelado daría `FileNotFoundError`.

`timeout_ms` es el corte del lado cliente y debe ser **≥** el timeout máximo interno del
server (900 s), para que gane el mensaje `[timeout]` del MCP en vez del corte seco.

```bash
# instalar y registrar
scripts/install-modal-mcp.sh
python3 scripts/add-mcp-to-project.py /ruta/al/proyecto --only modal-mrgenkko
```

## Credenciales: dos defensas

**Toda salida se redacta antes de devolverla.** Los tokens de Modal son `ak-…`/`as-…` y
`modal token info` los imprime enteros — sin esto, la primera llamada a `whoami` deja el
token escrito en el transcript de la conversación. Un regex los reemplaza en cada salida,
incluidos los tracebacks.

**`secret_create` prefiere archivos a argumentos.** Con `values` el secreto viaja por
`argv`: queda en la conversación y en `ps aux` para cualquier usuario de la máquina. Con
`from_dotenv` o `from_json` no pasa por ahí. El server exige **exactamente una** fuente.

## Gotchas

- **`COLUMNS=200` no es cosmético.** La CLI usa `rich`, que *trunca* las tablas al ancho
  del terminal: sin esto los nombres de app y los IDs salen cortados con `…` e inservibles.
- **La salida se recorta a 12 000 caracteres** (cabeza + cola, con aviso). Un traceback de
  `rich` a 200 columnas son varios KB de marco ASCII que no aportan nada al contexto.
- **Un script inexistente se detecta antes de invocar la CLI**: `modal deploy /no/existe.py`
  vuelca un traceback de ~6 KB; el MCP devuelve una línea. Con `module: true` no se chequea
  el disco, porque el target es una ruta de módulo.
- **`deploy` nunca usa `--stream-logs`** y `run` va con `-q`: ambos dejarían el proceso
  escuchando. Si `deploy` da timeout, **el trabajo puede haber seguido del lado de Modal** —
  verificar con `app_list` antes de reintentar.
- `whoami` sin token responde `Token missing`, no un crash: el MCP levanta igual sin
  credenciales.
- **`app_list` NO trae la GPU** — la CLI no la expone ahí (sólo id, nombre, estado y
  tareas), y `container_list` tampoco. Esa lista no sirve para auditar qué hardware quedó
  declarado: hay que mirar el código de la app o el dashboard. Fue el agujero por el que
  una app vieja quedó viva con una H100 sin que se notara desde el MCP.
- **Renombrar la app NO reemplaza la anterior**: crea una nueva y la vieja sigue
  desplegada, con su GPU. Una app `deployed` sin contenedores no factura, pero cualquier
  request a su URL la despierta — hay que detenerla con `app_stop`.
- **`deploy` y `run` aceptan `env`**, y es del proceso `modal` LOCAL, no del contenedor.
  Sirve porque el script de la app se ejecuta acá durante el deploy: así una misma app
  puede publicar variantes (`{"QWEN38_VARIANTE": "nvfp4"}`) sin duplicar el archivo.
- **`deploy` devuelve las URLs al frente** (endpoint y dashboard), extraídas del texto de
  la CLI, que las corta en varias líneas.
- **Las mutaciones confirman estado.** `app stop` no imprime nada al salir bien, y
  "(sin output)" no distingue el éxito de la nada: `app_stop`, `app_rollback`,
  `endpoint_stop` y `volume_create` re-consultan y devuelven cómo quedó.
- **`costo_ahora` lee las claves en snake_case** (`app_name`, `container_id`, `start_time`):
  la CLI las emite así en `--json` aunque la tabla humana las titule "App Name". Con las
  otras salía `? [?]` (corregido en 0.1.4). Un contenedor en `Pending` **ya factura**
  aunque todavía no tenga hora de inicio.
- **`costo_ahora` necesita que le digas la GPU.** La CLI no dice qué hardware usa cada
  contenedor, así que sin el parámetro `gpu` devuelve los tiempos vivos y la tabla de
  precios, y con él calcula lo acumulado. `billing summary` no sirve para esto: no es
  tiempo real.
- **Las opciones van SIEMPRE antes del target** (corregido en 0.1.1). `modal run` entrega
  a la app todo lo que venga *después* del script —así se le pasan argumentos al
  entrypoint— así que un `-q` ahí se parsea como opción del usuario y la CLI corta con
  `Error: No such option '-q'`. Vale para `run` y, por simetría, para `deploy`.
- **`run` crea una app EFÍMERA, distinta de la desplegada.** Verificado: `deploy` dejó
  `ap-f6ic…` y el `run` siguiente creó `ap-zDhH…`, que se detuvo solo a los 5 s. Por eso
  `app_logs` sobre la app *deployed* vuelve vacío después de un `run`: el stdout salió por
  la efímera. Para ver esos logs hay que pedirlos por el `app_id` que devuelve `app_list`.
- **`billing summary` no es tiempo real**: después de un run completo seguía en `0E-8`.
  Para saber qué está facturando *ahora* sirve `container_list`, que lista lo que corre.
