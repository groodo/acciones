#!/usr/bin/env bash
# Arranca el servicio local (modo Paper, datos SIMULADOS) en 127.0.0.1:8765.
# Crea un entorno virtual en service/.venv la primera vez.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/service/.venv"
PYTHON="${PYTHON:-python3}"

if [ ! -x "$VENV/bin/python" ]; then
  echo "Creando entorno virtual en $VENV"
  "$PYTHON" -m venv "$VENV"
  "$VENV/bin/pip" install --upgrade pip >/dev/null
  "$VENV/bin/pip" install -e "$ROOT/service[dev]"
fi

exec "$VENV/bin/python" -m tradingdesk
