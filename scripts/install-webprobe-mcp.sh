#!/usr/bin/env bash
set -euo pipefail
# Instala el MCP webprobe (diagnóstico de landings sobre Playwright):
#   1. la dependencia `playwright` en el venv compartido del repo,
#   2. los 3 motores chromium+firefox+webkit (machine-global en ~/.cache/ms-playwright;
#      una sola instancia del MCP los maneja vivos en paralelo — ver guides/mcp-webprobe.md),
#   3. las librerías del SO que los motores necesitan (webkit es el más exigente; requiere sudo),
#   4. copia el server.py a ~/.claude/mcp-servers/webprobe/.
SKILLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PIP="${SKILLS_DIR}/.venv/bin/pip"
VENV_PW="${SKILLS_DIR}/.venv/bin/playwright"
DEST="${HOME}/.claude/mcp-servers/webprobe"

"${VENV_PIP}" install "playwright==1.49.0"
"${VENV_PW}" install chromium firefox webkit

# Librerías del SO. Chromium/Firefox suelen traerlas; WebKit casi siempre necesita estas
# (libwoff1, libgstreamer*, libavif, libenchant, libsecret, libmanette). Requiere sudo: si no
# hay sudo no-interactivo, se omite con un aviso y el usuario lo corre a mano.
if sudo -n true 2>/dev/null; then
  sudo "${VENV_PW}" install-deps
else
  echo "⚠ deps del SO NO instaladas (sudo pidió contraseña). Corré a mano para habilitar WebKit:"
  echo "    sudo \"${VENV_PW}\" install-deps webkit"
fi

mkdir -p "${DEST}"
cp "${SKILLS_DIR}/deployed/webprobe/server.py" "${DEST}/server.py"

echo "Instalado: ${DEST}/server.py"
echo "Registrar en un proyecto:"
echo "  python3 \"${SKILLS_DIR}/scripts/add-mcp-to-project.py\" /ruta/al/proyecto --only webprobe"
