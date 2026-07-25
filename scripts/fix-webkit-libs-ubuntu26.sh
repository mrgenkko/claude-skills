#!/usr/bin/env bash
# Repara el bundle WebKit de Playwright en Ubuntu 26.04.
#
# El bundle webkit que baja Playwright 1.49 se compiló en Ubuntu 24.04 y linkea
# contra sonames que 26.04 ya no tiene (icu 74 -> 78, libxml2 .so.2 -> .so.16,
# vpx 9 -> 12, x264 164 -> 165). `playwright install-deps` falla porque apt no
# encuentra esos paquetes: no es que falten, es que la versión ya no existe.
#
# La solución es traer las libs de Noble y dejarlas dentro del propio bundle, en
# el sys/lib que sus wrappers ya incluyen en LD_LIBRARY_PATH. No se toca el
# sistema, no hace falta sudo y no interfiere con las libs de 26.04.
#
# Es idempotente: volvé a correrlo después de cada `playwright install webkit`,
# que borra el bundle y se lleva estas libs puestas.
#
# Uso: scripts/fix-webkit-libs-ubuntu26.sh
set -euo pipefail

POOL="http://archive.ubuntu.com/ubuntu/pool"
COMPAT="${HOME}/.cache/ms-playwright/compat-libs"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

# Paquetes de Noble (24.04). libjxl.so.0.8 no figura acá a propósito: ninguna
# release de Ubuntu la tiene (0.7 -> 0.10 -> 0.11) y el bundle ya la trae en sys/lib.
DEBS=(
  "main/i/icu/libicu74_74.2-1ubuntu3.1_amd64.deb"
  "main/libx/libxml2/libxml2_2.9.14+dfsg-1.3ubuntu3.8_amd64.deb"
  "main/libv/libvpx/libvpx9_1.14.0-1ubuntu2.3_amd64.deb"
  "main/libe/libevent/libevent-2.1-7t64_2.1.12-stable-9ubuntu2_amd64.deb"
  "main/w/woff2/libwoff1_1.0.2-2build1_amd64.deb"
  "main/libm/libmanette/libmanette-0.2-0_0.2.7-1build2_amd64.deb"
  "main/a/abseil/libabsl20220623t64_20220623.1-3.1ubuntu3.2_amd64.deb"
  "universe/liba/libavif/libavif16_1.0.4-1ubuntu3_amd64.deb"
  "universe/libg/libgav1/libgav1-1_0.18.0-1build3_amd64.deb"
  "universe/r/rust-rav1e/librav1e0_0.7.1-2_amd64.deb"
  "universe/s/svt-av1/libsvtav1enc1d1_1.7.0+dfsg-2build1_amd64.deb"
  "universe/liby/libyuv/libyuv0_0.0~git202401110.af6ac82-1_amd64.deb"
  "universe/libw/libwpe/libwpe-1.0-1_1.12.0-1_amd64.deb"
)

echo "==> Descargando ${#DEBS[@]} paquetes de Noble"
mkdir -p "${WORK}/debs"
for d in "${DEBS[@]}"; do
  curl -sSL --fail -o "${WORK}/debs/$(basename "${d}")" "${POOL}/${d}"
done

echo "==> Extrayendo a ${COMPAT}"
rm -rf "${COMPAT}"
mkdir -p "${COMPAT}/x"
for d in "${WORK}"/debs/*.deb; do dpkg-deb -x "${d}" "${COMPAT}/x"; done
find "${COMPAT}/x" \( -name "*.so" -o -name "*.so.*" \) \( -type f -o -type l \) \
  -exec cp -a {} "${COMPAT}/" \;
rm -rf "${COMPAT}/x"

# Los wrappers MiniBrowser exportan LD_LIBRARY_PATH="<bundle>/lib:<bundle>/sys/lib"
# pisando el del entorno, así que las libs tienen que vivir adentro del bundle.
# Headless usa minibrowser-wpe y headed minibrowser-gtk: hay que cubrir los dos.
found=0
for sysdir in "${HOME}"/.cache/ms-playwright/webkit-*/minibrowser-{gtk,wpe}/sys/lib; do
  [ -d "${sysdir}" ] || continue
  cp -a "${COMPAT}"/*.so* "${sysdir}/"
  echo "==> Instalado en ${sysdir#${HOME}/}"
  found=$((found + 1))
done

if [ "${found}" -eq 0 ]; then
  echo "ERROR: no hay bundles webkit. Corré primero: playwright install webkit" >&2
  exit 1
fi

echo "==> Verificando enlazado"
fail=0
for mb in "${HOME}"/.cache/ms-playwright/webkit-*/minibrowser-{gtk,wpe}/bin/MiniBrowser; do
  [ -f "${mb}" ] || continue
  bundle="$(dirname "$(dirname "${mb}")")"
  miss="$(LD_LIBRARY_PATH="${bundle}/lib:${bundle}/sys/lib" ldd "${mb}" 2>/dev/null |
    grep "not found" | sort -u || true)"
  if [ -n "${miss}" ]; then
    echo "  FALTA en ${bundle##*/}:"; echo "${miss}"; fail=1
  else
    echo "  ${bundle##*/}: OK"
  fi
done

[ "${fail}" -eq 0 ] || exit 1

cat <<'NOTA'

Listo. Recordá que además hace falta GIO_MODULE_DIR=/usr/lib/x86_64-linux-gnu/gio/modules
en el env del MCP: el snap de VSCode inyecta sus propios módulos GIO, linkeados contra la
glibc de core20, y el proceso de red de WebKit muere al cargarlos. Eso ya lo genera
add-mcp-to-project.py para los MCP de tipo webprobe.
NOTA
