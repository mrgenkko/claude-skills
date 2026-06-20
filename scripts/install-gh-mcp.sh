#!/usr/bin/env bash
set -euo pipefail
# Instala el MCP gh (GitHub CLI: despliegues, Actions, PRs, releases):
#   1. verifica que el binario `gh` esté disponible (lo usa por subprocess),
#   2. copia el server.py a ~/.claude/mcp-servers/gh/.
# No instala dependencias pip: el venv compartido ya tiene `mcp` y el server
# envuelve el binario `gh` del sistema.
SKILLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${HOME}/.claude/mcp-servers/gh"

if ! command -v gh >/dev/null 2>&1; then
  echo "ERROR: 'gh' (GitHub CLI) no está instalado o no está en PATH." >&2
  echo "Instálalo desde https://cli.github.com (ej. Debian/Ubuntu: 'sudo apt install gh')." >&2
  exit 1
fi
echo "gh detectado: $(gh --version | head -n1)"

mkdir -p "${DEST}"
cp "${SKILLS_DIR}/deployed/gh/server.py" "${DEST}/server.py"

echo "Instalado: ${DEST}/server.py"
echo
echo "Siguiente paso:"
echo "  1. Crea un PAT por org (ver permisos en guides/mcp-gh.md):"
echo "     classic       -> scope 'repo' (+ 'workflow' si disparas workflows)"
echo "     fine-grained  -> Read de Metadata/Actions/Deployments/Pull requests/Contents/Checks"
echo "  2. Agrega una entrada tipo 'gh' por org en scripts/secrets.json"
echo "  3. Registra en un proyecto:"
echo "     python3 \"${SKILLS_DIR}/scripts/add-mcp-to-project.py\" /ruta/al/proyecto --only gh-mi-org"
