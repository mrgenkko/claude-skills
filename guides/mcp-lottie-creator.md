# Guía: MCP de LottieFiles Creator

## ¿Qué hace este MCP?

Conecta el agente con el editor web **LottieFiles Creator** ([creator.lottiefiles.com](https://creator.lottiefiles.com)).
Puedes crear y editar animaciones Lottie en el browser mediante lenguaje natural.

A diferencia de los MCPs custom (Python), este es un paquete npm oficial de LottieFiles.

## Tipo de conexión

```
Agente (Cursor / Claude Code)
    ←stdio→  @lottiefiles/creator-mcp (Node local, ~/.claude/mcp-servers/lottie)
    ←WebSocket→  LottieFiles Creator (browser)
```

El proceso local usa **`/usr/bin/node`** sobre una instalación fija en disco (no `npx` en cada arranque).
No requiere API key — hace falta Creator abierto en el browser con el bridge MCP activo.

## Tools disponibles

| Área | Descripción |
|------|-------------|
| Escenas | Nueva escena, cambiar entre escenas, exportar JSON / dotLottie, importar SVG |
| Formas | Rectángulos, elipses, polígonos, estrellas, paths custom |
| Capas | Visibilidad, bloqueo, timing, modos de fusión, máscaras, transformaciones |
| Rellenos / trazos | Sólidos, gradientes lineales y radiales, trazos animables |
| Animación | Keyframes, easing, posición / rotación / escala / opacidad |
| Assets | Listar, clonar, eliminar, gestionar reproducción |

## Instalación (una sola vez)

Desde el repo o a mano:

```bash
bash ~/Mrgenkko\ Skills/scripts/install-lottie-mcp.sh
```

Equivalente manual:

```bash
mkdir -p ~/.claude/mcp-servers/lottie
cd ~/.claude/mcp-servers/lottie
NPM_CONFIG_PREFIX=/usr npm install @lottiefiles/creator-mcp@0.1.2
```

### Por qué no usar `npx` en Cursor

Cuando Cursor arranca MCPs, el `PATH` incluye el Node de `.cursor-server`. `npx` hereda un `prefix` inválido (`~/.cursor-server/bin`) y falla con:

`ENOENT: no such file or directory, lstat '.../.cursor-server/bin/lib'`

Tampoco conviene poner el `command` en una ruta con espacios (`Mrgenkko Skills/...`): Cursor puede partir mal el ejecutable. La instalación fija bajo `~/.claude/mcp-servers/lottie/` evita ambos problemas.

## Registro en Cursor (global)

Archivo: `~/.cursor/mcp.json` — aplica a **todos** los workspaces.

```json
"lottiefiles-creator": {
  "type": "stdio",
  "command": "/usr/bin/node",
  "args": [
    "${userHome}/.claude/mcp-servers/lottie/node_modules/@lottiefiles/creator-mcp/dist/index.mjs"
  ],
  "env": {}
}
```

Tras editar: **recargar ventana** de Cursor → **Settings → MCP** → verificar que `lottiefiles-creator` esté conectado.

Detalle del formato global: `guides/cursor.md`.

## Registro en Claude Code (por proyecto)

La extensión VSCode **no** lee `~/.cursor/mcp.json`. Registrar en `~/.claude.json` bajo `projects["/ruta/absoluta"]["mcpServers"]`:

```bash
claude mcp add lottiefiles-creator -- "${HOME}/Mrgenkko Skills/scripts/run-lottie-mcp.sh"
```

O manualmente:

```json
"lottiefiles-creator": {
  "type": "stdio",
  "command": "/home/TU_USUARIO/Mrgenkko Skills/scripts/run-lottie-mcp.sh",
  "args": [],
  "env": {}
}
```

El script `scripts/run-lottie-mcp.sh` delega al mismo `index.mjs` instalado en `~/.claude/mcp-servers/lottie/`.

Usar `scripts/add-mcp-to-project.py` solo para MCPs definidos en `secrets.json` (Python). Lottie se registra aparte.

## Activar la conexión en el browser

1. Abrir [creator.lottiefiles.com](https://creator.lottiefiles.com)
2. **Settings → MCP Settings → Enable MCP**
3. Mensaje **"Local MCP bridge connected"** en verde
4. Si falla: recargar ventana de **Cursor** o VSCode (el IDE levanta el proceso stdio)

## Uso con Obsidian

Con **`obsidian`** y **`lottiefiles-creator`** activos en el mismo agente:

1. **Lottie**: diseñar o editar la animación en Creator; exportar JSON o dotLottie al disco (o copiar ruta del export).
2. **focusyn** `add_attachment(source_path, vault, alt="<descripción>")`: sube el archivo al NAS vía el gateway (fuera de Git); devuelve `markdown_ref` (`![alt](/v1/attachment/{file_id})`).
3. **focusyn** `write_note` / `append_note` / `edit_note`: pega el `markdown_ref` en la nota con alt text que describa la animación, no solo el nombre del archivo.

Antes de crear notas nuevas, `get_context` en Obsidian (convenciones del vault). Ver `guides/mcp-obsidian.md`.

## Scripts en este repo

| Script | Uso |
|--------|-----|
| `scripts/install-lottie-mcp.sh` | Instala el paquete en `~/.claude/mcp-servers/lottie/` |
| `scripts/run-lottie-mcp.sh` | Arranque para Claude Code / terminal (misma entrada `index.mjs`) |

## Troubleshooting

| Síntoma | Acción |
|---------|--------|
| `ENOENT` en `.cursor-server/bin/lib` | No usar `npx` en `mcp.json`; usar bloque `node` + ruta fija de arriba |
| MCP no arranca / command not found | Verificar instalación: `ls ~/.claude/mcp-servers/lottie/node_modules/@lottiefiles/creator-mcp/dist/index.mjs` |
| Bridge desconectado en Creator | Recargar IDE; comprobar toggle MCP en Settings del IDE |
| Puerto 3847 en uso | Otra instancia del MCP abierta; cerrar procesos `creator-mcp` o reiniciar IDE |
| Falta instalación al usar `run-lottie-mcp.sh` | Ejecutar `install-lottie-mcp.sh` |

## Requisitos

- Node.js 18+ (`/usr/bin/node` en WSL)
- Instalación en `~/.claude/mcp-servers/lottie/`
- LottieFiles Creator abierto con MCP bridge habilitado durante el uso

## Dónde está configurado

| Herramienta | Alcance | Archivo |
|-------------|---------|---------|
| **Cursor IDE** | Global (todos los proyectos) | `~/.cursor/mcp.json` |
| **Claude Code VSCode** | Por proyecto | `~/.claude.json` → `projects[...]` |
| **Binario en disco** | Usuario | `~/.claude/mcp-servers/lottie/node_modules/.../dist/index.mjs` |
