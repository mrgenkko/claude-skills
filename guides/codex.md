# Guía: MCP en Codex CLI (por proyecto)

Codex CLI usa **otro archivo y otro formato** que la extensión VSCode de Claude Code: TOML en vez de JSON, y sus propios nombres de campo. Esta guía documenta cómo replicar en Codex el mismo mapeo proyecto → MCPs que ya existe en `~/.claude.json`, con la misma fuente de verdad (`scripts/secrets.json`).

Verificado contra **codex-cli 0.148.0-alpha.9**.

---

## Dónde va la configuración

| Alcance | Ruta | Cuándo usarla |
|---------|------|----------------|
| **Global (usuario)** | `~/.codex/config.toml` → `[mcp_servers.X]` | Lo que querés en todos los proyectos: `focusyn`. |
| **Por proyecto** | `<repo>/.codex/config.toml` → `[mcp_servers.X]` | Todo lo demás: las credenciales y el scope son por proyecto. |

Los dos se **mergean**: el config del proyecto suma servidores al global, no lo reemplaza.

El binario no siempre está en el `PATH`: si Codex vino con la extensión ChatGPT de VSCode, vive en
`~/.vscode/extensions/openai.chatgpt-*/bin/linux-x86_64/codex`.

---

## Las dos trampas

### 1. El gate de trust (silencioso)

Codex lee `<repo>/.codex/config.toml` **solo si el proyecto está marcado como confiable** en el config global:

```toml
[projects."/home/melquiades/lait-landing-02"]
trust_level = "trusted"
```

Si no lo está, ignora el archivo entero **sin ningún warning**: los MCPs simplemente no existen y no hay nada en la salida que lo explique. `add-mcp-to-project.py --target codex` marca el trust automáticamente.

### 2. El archivo vive dentro del repo

Con las credenciales en el llavero ([secrets.md](secrets.md)), el TOML **ya no lleva ninguna**: solo `--secret VAR=keyring:<id>` para el lanzador, y `http_headers_helper` para focusyn. Pero una entrada que siga en texto plano en `secrets.json` todavía escribe `--password=` o `GH_TOKEN` en `<repo>/.codex/config.toml`, a un `git add .` de publicarse. Por eso los guards siguen:

- agrega `.codex/` al `.gitignore` del repo,
- **aborta** si `.codex/config.toml` ya está trackeado en git (y te dice cómo sacarlo del índice),
- escribe el archivo con permisos `0600`.

Si un config llegó a viajar a un remoto, rotá las credenciales: sacarlo del índice no lo borra del historial.

---

## Uso

```bash
PYTHON="$HOME/Mrgenkko Skills/.venv/bin/python"
SCRIPT="$HOME/Mrgenkko Skills/scripts/add-mcp-to-project.py"

# Solo Codex
"$PYTHON" "$SCRIPT" /ruta/al/proyecto --target codex --only gh-lait,webprobe

# Claude y Codex a la vez (recomendado: no derivan)
"$PYTHON" "$SCRIPT" /ruta/al/proyecto --target both --only gh-lait,webprobe

# Actualizar tras cambiar secrets.json
"$PYTHON" "$SCRIPT" /ruta/al/proyecto --target both --update

# Re-registrar en TODOS los proyectos donde ya están (Claude + Codex)
"$PYTHON" "$SCRIPT" --sweep --dry-run

# Quitar uno
"$PYTHON" "$SCRIPT" /ruta/al/proyecto --target codex --remove --only webprobe

# focusyn: global, una sola vez (su agent_key no debe caer en ningún repo)
"$PYTHON" "$SCRIPT" --global --target codex --only focusyn
```

O vía la skill: `/mcp-project add lait-landing-02 gh-lait,webprobe --both`.

---

## Formato generado

El script escribe un bloque delimitado y **respeta el resto del archivo** (tu `model`, tus `hooks`, tus comentarios):

```toml
model = "gpt-5.6-sol"          # tuyo, se conserva

# >>> mrgenkko-skills: mcp_servers (generado) >>>
[mcp_servers."gh-lait"]
command = "/home/melquiades/Mrgenkko Skills/.venv/bin/python"
args = [
    "/home/melquiades/Mrgenkko Skills/scripts/mcp-launch.py",
    "--secret",
    "GH_TOKEN=keyring:gh-lait/token",
    "--",
    "/home/melquiades/.claude/mcp-servers/gh/server.py",
    "--owner=juandelgadolait",
    "--name=lait",
]
env_vars = ["DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"]
startup_timeout_sec = 30
# <<< mrgenkko-skills: mcp_servers <<<
```

El bloque va **al final** a propósito: en TOML una clave suelta después de una tabla pertenece a esa tabla, así que insertarlo en el medio reasignaría en silencio lo que viniera después. Antes de escribir, el script re-parsea el resultado y aborta si no queda válido.

Si declaraste MCPs a mano fuera del bloque, el script aborta en vez de duplicar la tabla.

### Equivalencias de campos

| Claude (`~/.claude.json`) | Codex (`config.toml`) |
|---|---|
| `command`, `args`, `env` | igual |
| `type: "http"` + `url` | `url` |
| `headers` | `http_headers` |
| `timeout` (ms, por tool call) | `tool_timeout_sec` (segundos) |
| — | `startup_timeout_sec` (30; el default de Codex es corto y da timeouts espurios al arrancar un server Python del venv) |

---

## Verificación

```bash
cd /ruta/al/proyecto && codex mcp list
```

Lista el global + los del proyecto ya mergeados. Si falta uno, revisá el trust (trampa 1).

Para comprobar que además **arrancan**, no alcanza `mcp list` (solo lee config): hay que hacerles el handshake MCP o abrir Codex y llamar una tool.

---

## Aprobación de tool calls

En `codex exec` (no interactivo, `approval: never`) las llamadas a tools MCP se **auto-cancelan**: aparece `mcp: <server>/<tool> (failed)` con `user cancelled MCP tool call`. El server arrancó bien — es la política de aprobación, no un problema de registro. En el TUI interactivo aparece el prompt y se aprueba a mano.

Los candidatos de config son `mcp_servers.<name>.approval_mode` y `default_tools_approval_mode`, pero **`approval_mode = "never"` no evita la cancelación** en `exec` (probado), y ni `codex mcp list` ni `codex doctor` validan esos valores, así que el enum aceptado quedó sin confirmar.

---

## Diferencias que importan frente a Claude Code

- **No hay `codex mcp add` por proyecto**: ese subcomando escribe únicamente el config global. El TOML por proyecto se genera con el script.
- **El trust es un prerrequisito**, no un detalle de UX: sin él no hay MCPs.
- **El config vive dentro del repo.** Con el llavero ya no lleva secretos, pero una entrada en texto plano los metería ahí: es la razón de los guards.
- **Codex filtra el entorno de sus MCP.** El lanzador necesita el bus de sesión para leer el llavero, por eso las entradas que lo usan llevan `env_vars = ["DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"]` (verificado que 0.162 lo acepta).
- **`http_headers_helper` es un string** (0.162 rechaza lista y tabla) y no está documentado si pasa por un shell: por eso el helper es una ruta sola, sin espacios ni argumentos.

---

## Generar imágenes desde Claude (`/codex-imagegen`)

Claude Code no genera imágenes; el `image_gen` integrado de Codex sí, con la sesión de ChatGPT (sin `OPENAI_API_KEY`). La skill `/codex-imagegen` lo envuelve en `scripts/codex-imagegen.py`: Claude escribe el prompt, Codex llama a `image_gen` con ese texto tal cual, y la imagen sale con su `.prompt.txt` al lado. Verificado contra **codex-cli 0.160.0** (2026-10-03): generación, edición con imagen de referencia y modo `--rewrite`, ~45 s por imagen con `gpt-6-astra` en `high`.

Lo que costó descubrir:

- **El agente reescribe el prompt si lo dejás.** «Un círculo rojo plano sobre fondo negro» le llegó a `image_gen` con hex, proporciones y exclusiones agregadas. Para que llegue crudo hay que pedirlo explícito (el script lo delimita entre `<<<PROMPT` y `PROMPT>>>`); `--rewrite` habilita la reescritura.
- **El prompt real sólo está en el registro de sesión.** `~/.codex/sessions/AAAA/MM/DD/rollout-*-<thread_id>.jsonl` trae un item `kind: "image_gen.generation"` con `revisedPrompt` y `savedPath`. El stream `--json` no lo emite. Con `--ephemeral` no hay registro → no hay prompt; por eso el script no lo usa, y borra el registro después de leerlo (repite la imagen en base64, ~1–1,6 MB).
- **Las sesiones de `exec` no entran en `session_index.jsonl`**, el índice del historial.
- **`-i <FILE>...` es variádico.** `codex exec -i ref.png "prompt"` se come el prompt como otra imagen y corta con `No prompt provided via stdin`. El prompt va por stdin.
- **`image_gen` recibe tres cosas:** `prompt`, cuántas de las imágenes adjuntas incluir (`num_last_images_to_include`) y `transparent_background`. Calidad, tamaño y máscaras son del modo API, no del integrado.
- **`--ignore-user-config`** evita arrancar los MCPs globales (focusyn) y la memoria en cada imagen; la auth se sigue leyendo de `CODEX_HOME`. Tampoco lee el `model` del config: el script fija `gpt-6-astra` en `high`.
- Correr en un directorio temporal vacío, para que no cargue el `AGENTS.md` del proyecto.
