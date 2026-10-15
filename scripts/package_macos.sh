#!/usr/bin/env bash
# Empaqueta la app SwiftUI como AI Trading Desk.app (sin firmar ni notarizar).
# Uso: scripts/package_macos.sh [directorio_salida]
# La firma y notarización quedan para el Hito F.
set -euo pipefail
if [ "$(uname)" != "Darwin" ]; then
  echo "El empaquetado requiere macOS." >&2
  exit 1
fi
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/dist}"
APP="$OUT/AI Trading Desk.app"
VERSION="0.2.0"
PYTHON="${PYTHON:-python3}"

(cd "$ROOT/app" && swift build -c release --arch arm64)
BIN="$(cd "$ROOT/app" && swift build -c release --arch arm64 --show-bin-path)/AITradingDesk"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/AITradingDesk"
# Entorno Python del servicio dentro del bundle: la app lo arranca al abrirse y lo detiene al salir.
# Usa el Python 3.11+ de este Mac (el entorno enlaza con él, no lo copia).
RUNTIME="$APP/Contents/Resources/service-runtime"
"$PYTHON" -m venv "$RUNTIME"
"$RUNTIME/bin/python" -m pip install --quiet --upgrade pip
"$RUNTIME/bin/python" -m pip install --quiet "$ROOT/service"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>AI Trading Desk</string>
  <key>CFBundleDisplayName</key><string>AI Trading Desk</string>
  <key>CFBundleIdentifier</key><string>local.aitradingdesk.app</string>
  <key>CFBundleExecutable</key><string>AITradingDesk</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleDevelopmentRegion</key><string>es</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSArchitecturePriority</key><array><string>arm64</string></array>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key>
  <dict>
    <!-- Solo se permite HTTP sin TLS hacia el servicio local -->
    <key>NSAllowsLocalNetworking</key><true/>
  </dict>
</dict>
</plist>
PLIST

echo "Creado: $APP (sin firmar). Ábrelo con clic derecho > Abrir la primera vez."
echo "La app arranca sola el servicio local (fuente IBKR Paper; abre antes IB Gateway en Negociación simulada)."
