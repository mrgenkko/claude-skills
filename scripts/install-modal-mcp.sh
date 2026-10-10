#!/usr/bin/env bash
set -euo pipefail
# Instala el MCP modal (apps, endpoints de inferencia, volúmenes, logs y costo en modal.com):
#   1. la CLI `modal` en el venv compartido del repo,
#   2. copia el server.py a ~/.claude/mcp-servers/modal/,
#   3. informa si la instancia tiene token (sin abrir el navegador: `modal token new`
#      arranca un flujo web y este script no es el lugar para eso).
#
# El venv de este repo está hecho con uv y NO trae pip; por eso se instala con
# `uv pip install` apuntando VIRTUAL_ENV, no con "${VENV}/bin/pip".
#
# Por qué el venv compartido y no uno aislado: `modal` resolvió 23 paquetes nuevos
# (grpclib, protobuf, aiohttp, rich, synchronicity...) sin un solo upgrade ni
# downgrade de lo que ya vive ahí — no pisa click, pydantic, httpx, playwright ni
# pillow. Es aditivo, como playwright en webprobe o Pillow en pixelorama.
SKILLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${SKILLS_DIR}/.venv"
DEST="${HOME}/.claude/mcp-servers/modal"

if command -v uv >/dev/null 2>&1; then
  VIRTUAL_ENV="${VENV}" uv pip install "modal>=1.5,<2"
else
  "${VENV}/bin/python" -m pip install "modal>=1.5,<2"
fi

mkdir -p "${DEST}"
cp "${SKILLS_DIR}/deployed/modal/server.py" "${DEST}/server.py"

MODAL_BIN="${VENV}/bin/modal"
echo
echo "Instalado: ${DEST}/server.py"
echo "CLI:       ${MODAL_BIN} ($("${MODAL_BIN}" --version 2>&1 | tail -1))"

# ¿Hay credenciales? Se prueba con el env de la instancia si está seteado, si no
# con el perfil global. NO se corre `modal token new` (abre el navegador).
if MODAL_LOGLEVEL=ERROR "${MODAL_BIN}" token info >/dev/null 2>&1; then
  echo "Token:     OK (hay credenciales resolubles en este entorno)"
else
  echo "Token:     FALTA — el MCP levanta igual, pero toda llamada dirá 'Token missing'."
  echo "           Generá uno sin flujo interactivo desde el dashboard:"
  echo "             https://modal.com/settings/<workspace>/tokens  →  New token"
  echo "           y pegá token_id/token_secret en scripts/secrets.json."
  echo "           (\`modal setup\` / \`modal token new\` también sirven, pero abren el navegador"
  echo "            y escriben ~/.modal.toml; el MCP prefiere el token por env, sin estado en disco.)"
fi

echo
echo "Registrar en un proyecto:"
echo "  python3 \"${SKILLS_DIR}/scripts/add-mcp-to-project.py\" /ruta/al/proyecto --only modal-mrgenkko"
