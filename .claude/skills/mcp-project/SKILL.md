---
name: mcp-project
description: Agrega, actualiza o elimina MCPs en proyectos Claude Code (extensión VSCode) y Codex CLI. Uso: /mcp-project <add|update|remove|list> [proyecto] [mcp1,mcp2,...] [--codex|--both]
argument-hint: <add|update|remove|list> [proyecto] [mcps] [--codex|--both]
allowed-tools: Bash, Read
---

# mcp-project · Gestión de MCPs por proyecto

La extensión VSCode de Claude Code ignora `~/.claude/mcp.json` y lee los MCPs desde `~/.claude.json` por proyecto. Codex CLI usa otro formato y otro archivo: `<proyecto>/.codex/config.toml`. Esta skill administra ambos con el mismo script.

**Script:** `~/Mrgenkko Skills/scripts/add-mcp-to-project.py`  
**Python:** `~/Mrgenkko Skills/.venv/bin/python`

## Argumentos

`$ARGUMENTS` contiene la operación y sus parámetros:

| Forma | Descripción |
|-------|-------------|
| `list` | Lista todos los proyectos con sus MCPs |
| `list <proyecto>` | Lista los MCPs de un proyecto específico |
| `add <proyecto> [mcp1,mcp2]` | Agrega MCPs al proyecto (omite los ya existentes) |
| `update <proyecto> [mcp1,mcp2]` | Agrega y sobreescribe MCPs existentes |
| `remove <proyecto> <mcp1,mcp2>` | Elimina MCPs del proyecto |

### Destino (Claude / Codex)

Sufijo opcional en cualquier operación; por defecto solo Claude Code:

| Sufijo | Se traduce a | Efecto |
|--------|--------------|--------|
| *(ninguno)* | `--target claude` | Solo `~/.claude.json` |
| `--codex` | `--target codex` | Solo `<proyecto>/.codex/config.toml` |
| `--both` | `--target both` | Los dos, en la misma corrida |

Con `--codex` el script además marca el proyecto como `trusted` en `~/.codex/config.toml` (sin eso Codex ignora el archivo sin avisar) y agrega `.codex/` al `.gitignore` del repo, porque el TOML lleva credenciales en texto plano.

## Resolución de ruta de proyecto

Si `<proyecto>` **no empieza con `/`**, buscar en los proyectos registrados en `~/.claude.json` el que contenga ese string en su ruta (case-insensitive). Si hay más de un match, mostrarlos y pedir al usuario que elija. Usar siempre la ruta absoluta al llamar el script.

Para obtener la lista de proyectos disponibles:
```bash
"$HOME/Mrgenkko Skills/.venv/bin/python" "$HOME/Mrgenkko Skills/scripts/add-mcp-to-project.py"
```

## Comandos por operación

Variables base (sustituir en los comandos):
```
PYTHON="$HOME/Mrgenkko Skills/.venv/bin/python"
SCRIPT="$HOME/Mrgenkko Skills/scripts/add-mcp-to-project.py"
```

### list
```bash
# Todos los proyectos:
"$PYTHON" "$SCRIPT"

# MCPs de un proyecto específico:
python3 -c "
import json, os
d = json.load(open(os.path.expanduser('~/.claude.json')))
p = d['projects'].get('/ruta/al/proyecto', {})
mcps = list(p.get('mcpServers', {}).keys())
print('\n'.join(mcps) if mcps else '(sin MCPs registrados)')
"
```

### add
```bash
# Todos los MCPs de secrets.json (omite existentes):
"$PYTHON" "$SCRIPT" /ruta/al/proyecto

# Solo algunos MCPs:
"$PYTHON" "$SCRIPT" /ruta/al/proyecto --only mcp1,mcp2

# Mismos MCPs, también en Codex:
"$PYTHON" "$SCRIPT" /ruta/al/proyecto --only mcp1,mcp2 --target both
```

### update
```bash
# Todos los MCPs (sobreescribe existentes):
"$PYTHON" "$SCRIPT" /ruta/al/proyecto --update

# Solo algunos:
"$PYTHON" "$SCRIPT" /ruta/al/proyecto --update --only mcp1,mcp2
```

### remove
```bash
# MCPs específicos:
"$PYTHON" "$SCRIPT" /ruta/al/proyecto --remove --only mcp1,mcp2

# Todos los MCPs del proyecto:
"$PYTHON" "$SCRIPT" /ruta/al/proyecto --remove
```

## Flujo de ejecución

1. Parsear `$ARGUMENTS`: extraer operación, proyecto (si existe), lista de MCPs (si existe) y destino (`--codex` / `--both`).
2. Si el proyecto es un nombre parcial, resolver la ruta absoluta buscando en `~/.claude.json`.
3. Si la operación es `list` sin proyecto, ejecutar el script sin argumentos para mostrar todos.
4. Si la operación es `remove` sin MCPs especificados, pedir confirmación al usuario antes de eliminar todos.
5. Ejecutar el comando con Bash.
6. Reportar el resultado (agregados / actualizados / eliminados / ya existían / no encontrados).
7. Recordar al usuario que **reinicie Claude Code (VSCode)** —y **Codex**, si se usó `--codex`/`--both`— para que los cambios apliquen.
8. Con `--codex`/`--both`, si el script tocó el `.gitignore` del proyecto, avisarlo: es un cambio en el repo del usuario que queda sin commitear.

## Verificación (Codex)

```bash
cd /ruta/al/proyecto && codex mcp list   # lista global + los del proyecto, ya mergeados
```

Si un MCP no aparece, lo más probable es que el proyecto no esté `trusted` en `~/.codex/config.toml`: Codex descarta el config del proyecto **en silencio**.

## MCPs disponibles

Los MCPs disponibles están definidos en `~/Mrgenkko Skills/scripts/secrets.json`. Ejecutar el script sin argumentos para ver la lista completa junto con los proyectos actuales.
