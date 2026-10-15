#!/usr/bin/env bash
# Ejecuta los tests del servicio y, en macOS con Swift, los de la app.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/service/.venv"

# Reinstala si cambian las dependencias (pyproject.toml más nuevo que la última instalación).
if [ ! -x "$VENV/bin/python" ] || [ "$ROOT/service/pyproject.toml" -nt "$VENV/.installed" ]; then
  [ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
  "$VENV/bin/pip" install -e "$ROOT/service[dev]"
  touch "$VENV/.installed"
fi
(cd "$ROOT/service" && "$VENV/bin/python" -m pytest)

if [ "$(uname)" = "Darwin" ] && command -v swift >/dev/null; then
  (cd "$ROOT/app" && swift build && swift test)
else
  echo "Se omiten los tests Swift: requieren macOS con Xcode o Swift 5.9+."
fi
