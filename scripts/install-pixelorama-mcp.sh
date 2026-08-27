#!/usr/bin/env bash
set -euo pipefail
# Instala el MCP pixelorama (dibujar pixel art y entregarlo en .pxo):
#   1. la dependencia Pillow en el venv compartido del repo,
#   2. copia el server.py a ~/.claude/mcp-servers/pixelorama/,
#   3. avisa si Pixelorama (la app) no está — el MCP funciona igual, pero `launch` no,
#      y sin ella no se puede verificar un .pxo contra su consumidor real.
#
# El venv de este repo está hecho con uv y NO trae pip; por eso se instala con
# `uv pip install` apuntando VIRTUAL_ENV, no con "${VENV}/bin/pip".
SKILLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${SKILLS_DIR}/.venv"
DEST="${HOME}/.claude/mcp-servers/pixelorama"

if command -v uv >/dev/null 2>&1; then
  VIRTUAL_ENV="${VENV}" uv pip install "Pillow>=10.0"
else
  "${VENV}/bin/python" -m pip install "Pillow>=10.0"
fi

mkdir -p "${DEST}"
cp "${SKILLS_DIR}/deployed/pixelorama/server.py" "${DEST}/server.py"
cp "${SKILLS_DIR}/deployed/pixelorama/requirements.txt" "${DEST}/requirements.txt"

# La app es opcional para dibujar, pero es quien valida el formato de verdad.
PX=""
for cand in "$(command -v pixelorama || true)" \
            "${HOME}/.local/opt/pixelorama/Pixelorama.x86_64" \
            "/usr/bin/pixelorama"; do
  if [ -n "${cand}" ] && [ -x "${cand}" ]; then PX="${cand}"; break; fi
done

echo "Instalado: ${DEST}/server.py"
if [ -n "${PX}" ]; then
  echo "Pixelorama: ${PX}"
  echo "Comprobar un .pxo sin abrir la ventana:"
  echo "  ${PX} --headless --quit -- --size --framecount archivo.pxo"
else
  echo "⚠ Pixelorama NO encontrado. El MCP dibuja y guarda igual, pero \`launch\` falla."
  echo "  Release oficial (sin sudo, sin FUSE):"
  echo "    https://github.com/Orama-Interactive/Pixelorama/releases  (Linux-64bit.tar.gz)"
  echo "  Descomprimir en ~/.local/opt/pixelorama/ — es la ruta que el MCP autodetecta."
fi
echo "Registrar en un proyecto:"
echo "  python3 \"${SKILLS_DIR}/scripts/add-mcp-to-project.py\" /ruta/al/proyecto --only pixelorama"
