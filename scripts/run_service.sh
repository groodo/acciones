#!/usr/bin/env bash
# Arranca el servicio local (modo Paper) en 127.0.0.1:8765.
# Por defecto usa datos SIMULADOS. Con la cuenta Paper de IBKR (IB Gateway abierto):
#   TRADINGDESK_DATA_SOURCE=ibkr scripts/run_service.sh
# Crea un entorno virtual en service/.venv la primera vez.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/service/.venv"
PYTHON="${PYTHON:-python3}"

# Reinstala si cambian las dependencias (pyproject.toml más nuevo que la última instalación).
if [ ! -x "$VENV/bin/python" ] || [ "$ROOT/service/pyproject.toml" -nt "$VENV/.installed" ]; then
  echo "Preparando el entorno virtual en $VENV"
  [ -x "$VENV/bin/python" ] || "$PYTHON" -m venv "$VENV"
  "$VENV/bin/pip" install --upgrade pip >/dev/null
  "$VENV/bin/pip" install -e "$ROOT/service[dev]"
  touch "$VENV/.installed"
fi

exec "$VENV/bin/python" -m tradingdesk
