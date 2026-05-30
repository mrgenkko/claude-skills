# Guía: MCP en Cursor (config global `~/.cursor`)

Cursor IDE usa **otro archivo** que la extensión VSCode de Claude Code: no lee `~/.claude/mcp.json` ni `~/.claude.json` para los MCP del agente de Cursor. Los servidores MCP de Cursor se declaran en JSON y el IDE los arranca por proceso stdio (o URL remota).

Esta guía documenta la opción que usamos en el ecosistema Melquiades: **un solo archivo global** para que los mismos MCP estén disponibles en **cualquier carpeta/proyecto** que abras.

---

## Dónde va la configuración

| Alcance | Ruta | Cuándo usarla |
|---------|------|----------------|
| **Global (usuario)** | `~/.cursor/mcp.json` | Herramientas que quieres en **todos** los workspaces: gcloud, postgres, SSH, Obsidian, Lottie, etc. |
| **Por proyecto** | `<repo>/.cursor/mcp.json` | Solo si un repo necesita MCP distintos o argumentos distintos al resto. |

En ambos casos el formato es el mismo: clave raíz `mcpServers` y un objeto por servidor.

---

## Cómo se “habilitan” en la UI

Los servidores definidos en `mcp.json` los carga Cursor al usar el agente / herramientas conectadas al protocolo MCP.

1. Abre **Settings** (preferencias de Cursor).
2. Ve a **Features → Model Context Protocol** (en builds recientes puede aparecer agrupado bajo herramientas del agente; el nombre exacto puede variar ligeramente según versión).
3. Ahí verás el estado de cada servidor (conectado, error, deshabilitado) y puedes **activar o desactivar** toggles sin borrar el JSON.
4. En el chat del agente, las herramientas MCP disponibles aparecen cuando el modelo las elige o cuando pides usarlas; si un servidor está apagado en Settings, no estará disponible.

Tras editar `~/.cursor/mcp.json`, conviene **recargar la ventana** de Cursor o reiniciar el IDE para que vuelva a leer el archivo.

---

## Formato mínimo (stdio, Python)

Los MCP custom de este repo son scripts Python bajo `~/.claude/mcp-servers/` y el intérprete del venv de skills:

```json
{
  "mcpServers": {
    "obsidian": {
      "type": "stdio",
      "command": "${userHome}/Mrgenkko Skills/.venv/bin/python",
      "args": [
        "${userHome}/.claude/mcp-servers/obsidian/server.py",
        "--vault-path=${userHome}/ObsidianVault"
      ],
      "env": {}
    }
  }
}
```

Campos habituales para stdio (según documentación de Cursor):

| Campo | Uso |
|-------|-----|
| `type` | `"stdio"` para procesos locales |
| `command` | Ejecutable (ruta absoluta o variable interpolada) |
| `args` | Lista de argumentos (incluida la ruta al `server.py` y flags `--host=`, etc.) |
| `env` | Variables de entorno solo para ese proceso |
| `envFile` | (Opcional) Archivo `.env` para cargar más variables en ese servidor |

---

## Interpolación (rutas y secretos)

Cursor resuelve variables en `command`, `args`, `env`, `url` y `headers`. Las más útiles:

- `${userHome}` — directorio home del usuario (evita hardcodear `/home/tuusuario`).
- `${workspaceFolder}` — raíz del workspace **solo tiene sentido en `mcp.json` del proyecto** (carpeta que contiene ese `.cursor/mcp.json`). En **`~/.cursor/mcp.json` global** no debes depender de `workspaceFolder` para el Python del venv; usa por ejemplo `${userHome}/Mrgenkko Skills/.venv/bin/python`.
- `${env:NOMBRE}` — variable de entorno del proceso que arranca Cursor (útil para contraseñas: `--password=${env:MRGENKKO_POSTGRES_PASSWORD}`).

Así el JSON se puede versionar sin contraseñas; las credenciales viven en el entorno o en un `envFile` local gitignoreado.

---

## MCP npm: LottieFiles Creator

Lottie no es Python: es un paquete Node instalado en `~/.claude/mcp-servers/lottie/`. En el global `~/.cursor/mcp.json`:

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

**Instalación previa** (una vez):

```bash
bash ~/Mrgenkko\ Skills/scripts/install-lottie-mcp.sh
```

Reglas específicas de Cursor:

- **No** usar `"command": "npx"` — el Node de `.cursor-server` rompe el `prefix` de npm (`ENOENT` en `bin/lib`).
- **No** poner el ejecutable en rutas con espacios en el campo `command` (p. ej. evitar `Mrgenkko Skills/scripts/...` como `command`; usar `node` + ruta bajo `~/.claude/`).
- Tras instalar o cambiar versión: recargar ventana de Cursor.

Guía completa (browser bridge, Obsidian, Claude Code): `guides/mcp-lottie-creator.md`.

---

## MCP oficial: Blender Lab

Blender Lab publica un MCP oficial para Blender con dos piezas:

- un add-on / extensión dentro de Blender
- un servidor MCP local que el cliente arranca por `stdio`

Si usas configuración JSON explícita en Cursor, la entrada luce así:

```json
"blender": {
  "type": "stdio",
  "command": "/ruta/absoluta/al/blender-mcp",
  "args": [],
  "env": {
    "BLENDER_HOST": "localhost",
    "BLENDER_PORT": "9876"
  }
}
```

Notas importantes:

- usar una **instalación conocida del servidor oficial** de Blender Lab
- no asumir que `uvx blender-mcp` apunta al proyecto oficial: hay forks comunitarios con nombres parecidos
- `BLENDER_HOST` y `BLENDER_PORT` deben coincidir con el add-on dentro de Blender
- si Blender corre en Windows y Cursor en WSL, `localhost` no cruza namespaces; en ese caso expón el add-on en Blender y apunta `BLENDER_HOST` a la IP del host Windows

Guía completa: `guides/mcp-blender.md`.

---

## Relación con Claude Code (VSCode)

| Herramienta | Archivo de MCP |
|-------------|----------------|
| **Cursor IDE** | `~/.cursor/mcp.json` (global) y/o `<proyecto>/.cursor/mcp.json` |
| **Claude Code extensión VSCode** | `~/.claude.json` → `projects["/ruta"]["mcpServers"]` (ver `scripts/add-mcp-to-project.py` y el README del repo) |

Puedes mantener **las mismas** rutas a `server.py` y el mismo venv en ambos mundos; solo cambia **dónde** se escribe el JSON de registro.

---

## Comprobar que funciona

1. Asegúrate de que el venv tiene dependencias (`pip install -r requirements.txt` en `Mrgenkko Skills`).
2. En Settings → MCP, revisa que el servidor no esté en error (ruta al Python, al script, permisos de clave, etc.).
3. **Obsidian**: `get_context` y luego `search_notes` en `wiki/`.
4. **Lottie**: Creator abierto con MCP bridge en verde; en el agente, pedir listar escena o crear una forma simple (el servidor stdio debe estar conectado en Settings → MCP).

---

## Referencias en este repo

- Plantilla de entradas MCP: `scripts/secrets.example.json` (convención de nombres y tipos).
- Servidores desplegados: `deployed/*/server.py` (copia o symlink en `~/.claude/mcp-servers/`).
- Lottie (npm): `scripts/install-lottie-mcp.sh`, `guides/mcp-lottie-creator.md`.
- Blender Lab oficial: `guides/mcp-blender.md`.
- Otras guías por transporte/stack: `guides/mcp-gcloud.md`, `guides/mcp-databases.md`, `guides/mcp-ssh.md`, `guides/mcp-obsidian.md`.

Documentación oficial de Cursor sobre MCP y rutas de configuración: [Cursor Docs — Model Context Protocol](https://cursor.com/docs/context/mcp).
