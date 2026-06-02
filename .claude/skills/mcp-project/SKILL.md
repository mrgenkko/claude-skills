---
name: mcp-project
description: Agrega, actualiza o elimina MCPs en proyectos Claude Code (extensión VSCode). Uso: /mcp-project <add|update|remove|list> [proyecto] [mcp1,mcp2,...]
argument-hint: <add|update|remove|list> [proyecto] [mcps]
allowed-tools: Bash, Read
---

# mcp-project · Gestión de MCPs por proyecto

La extensión VSCode de Claude Code ignora `~/.claude/mcp.json` y lee los MCPs desde `~/.claude.json` por proyecto. Esta skill administra esas entradas usando el script de gestión.

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

1. Parsear `$ARGUMENTS`: extraer operación, proyecto (si existe), y lista de MCPs (si existe).
2. Si el proyecto es un nombre parcial, resolver la ruta absoluta buscando en `~/.claude.json`.
3. Si la operación es `list` sin proyecto, ejecutar el script sin argumentos para mostrar todos.
4. Si la operación es `remove` sin MCPs especificados, pedir confirmación al usuario antes de eliminar todos.
5. Ejecutar el comando con Bash.
6. Reportar el resultado (agregados / actualizados / eliminados / ya existían / no encontrados).
7. Recordar al usuario que **reinicie Claude Code (VSCode)** para que los cambios apliquen.

## MCPs disponibles

Los MCPs disponibles están definidos en `~/Mrgenkko Skills/scripts/secrets.json`. Ejecutar el script sin argumentos para ver la lista completa junto con los proyectos actuales.
