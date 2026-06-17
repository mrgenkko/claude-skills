#!/usr/bin/env bash
set -euo pipefail
# Instala el MCP webprobe (diagnóstico de landings sobre Playwright):
#   1. la dependencia `playwright` en el venv compartido del repo,
#   2. el browser chromium (machine-global en ~/.cache/ms-playwright; suele ser no-op),
#   3. copia el server.py a ~/.claude/mcp-servers/webprobe/.
SKILLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PIP="${SKILLS_DIR}/.venv/bin/pip"
VENV_PW="${SKILLS_DIR}/.venv/bin/playwright"
DEST="${HOME}/.claude/mcp-servers/webprobe"

"${VENV_PIP}" install "playwright==1.49.0"
"${VENV_PW}" install chromium

mkdir -p "${DEST}"
cp "${SKILLS_DIR}/deployed/webprobe/server.py" "${DEST}/server.py"

echo "Instalado: ${DEST}/server.py"
echo "Registrar en un proyecto:"
echo "  python3 \"${SKILLS_DIR}/scripts/add-mcp-to-project.py\" /ruta/al/proyecto --only webprobe"
