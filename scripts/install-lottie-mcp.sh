#!/usr/bin/env bash
set -euo pipefail
LOTTIE_DIR="${HOME}/.claude/mcp-servers/lottie"
mkdir -p "${LOTTIE_DIR}"
cd "${LOTTIE_DIR}"
export NPM_CONFIG_PREFIX="${NPM_CONFIG_PREFIX:-/usr}"
npm install @lottiefiles/creator-mcp@0.1.2
echo "Instalado en ${LOTTIE_DIR}"
echo "Verificar: /usr/bin/node node_modules/@lottiefiles/creator-mcp/dist/index.mjs"
