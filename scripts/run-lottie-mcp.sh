#!/usr/bin/env bash
# Arranca Lottie Creator MCP (instalación fija, sin npx ni rutas con espacios).
set -euo pipefail
LOTTIE_HOME="${HOME}/.claude/mcp-servers/lottie"
ENTRY="${LOTTIE_HOME}/node_modules/@lottiefiles/creator-mcp/dist/index.mjs"
if [[ ! -f "${ENTRY}" ]]; then
  echo "Falta instalación. Ejecutar: cd ~/.claude/mcp-servers/lottie && NPM_CONFIG_PREFIX=/usr npm install @lottiefiles/creator-mcp@0.1.2" >&2
  exit 1
fi
exec /usr/bin/node "${ENTRY}" "$@"
