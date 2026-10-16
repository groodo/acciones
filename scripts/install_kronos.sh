#!/usr/bin/env bash
# Instala Kronos-small para uso local (PRD §8, Hito C).
#
# 1. Clona el repositorio oficial https://github.com/shiyu-coder/Kronos (licencia MIT) en
#    <datos>/kronos/src, en el commit KRONOS_COMMIT si se indica (si no, el actual, que queda anotado).
# 2. Instala sus dependencias (torch, pandas, einops, huggingface_hub, safetensors…) en service/.venv.
# 3. Descarga NeoQuasar/Kronos-small y NeoQuasar/Kronos-Tokenizer-base de Hugging Face.
# 4. Escribe <datos>/kronos/kronos.lock.json con el commit, las revisiones y el SHA-256 de cada archivo.
#    El servicio comprueba esos hashes antes de cargar el modelo.
#
# Después funciona sin conexión. Variables: TRADINGDESK_DATA_DIR, KRONOS_COMMIT,
# KRONOS_MODEL_REVISION, KRONOS_TOKENIZER_REVISION.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/service/.venv"
if [ "$(uname)" = "Darwin" ]; then
  DATA_DIR="${TRADINGDESK_DATA_DIR:-$HOME/Library/Application Support/AITradingDesk}"
else
  DATA_DIR="${TRADINGDESK_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/ai-trading-desk}"
fi
KDIR="$DATA_DIR/kronos"

if [ ! -x "$VENV/bin/python" ]; then
  echo "Primero arranca una vez scripts/run_service.sh para crear el entorno del servicio." >&2
  exit 1
fi

mkdir -p "$KDIR"
if [ ! -d "$KDIR/src/.git" ]; then
  git clone https://github.com/shiyu-coder/Kronos.git "$KDIR/src"
fi
if [ -n "${KRONOS_COMMIT:-}" ]; then
  git -C "$KDIR/src" fetch --quiet origin
  git -C "$KDIR/src" checkout --quiet "$KRONOS_COMMIT"
fi
COMMIT="$(git -C "$KDIR/src" rev-parse HEAD)"
echo "Kronos en el commit $COMMIT"

echo "Instalando dependencias de Kronos en $VENV (puede tardar: incluye PyTorch)…"
"$VENV/bin/pip" install -r "$KDIR/src/requirements.txt"
"$VENV/bin/pip" install "huggingface_hub>=0.23" safetensors

KDIR="$KDIR" COMMIT="$COMMIT" "$VENV/bin/python" - <<'PY'
import hashlib, json, os
from pathlib import Path
from huggingface_hub import snapshot_download, HfApi

kdir = Path(os.environ["KDIR"])
api = HfApi()
specs = {
    "model": ("NeoQuasar/Kronos-small", os.environ.get("KRONOS_MODEL_REVISION")),
    "tokenizer": ("NeoQuasar/Kronos-Tokenizer-base", os.environ.get("KRONOS_TOKENIZER_REVISION")),
}
lock = {"repo": "https://github.com/shiyu-coder/Kronos", "repo_commit": os.environ["COMMIT"], "files": {}}
for name, (repo_id, revision) in specs.items():
    sha = revision or api.model_info(repo_id).sha
    target = kdir / "weights" / name
    snapshot_download(repo_id, revision=sha, local_dir=target)
    lock[f"{name}_id"] = repo_id
    lock[f"{name}_revision"] = sha
    for path in sorted(target.rglob("*")):
        if path.is_file() and ".cache" not in path.parts:
            lock["files"][str(path.relative_to(kdir))] = hashlib.sha256(path.read_bytes()).hexdigest()
(kdir / "kronos.lock.json").write_text(json.dumps(lock, indent=2))
print(f"Pesos descargados y verificados: {len(lock['files'])} archivos")
print(f"Modelo {lock['model_id']}@{lock['model_revision'][:10]} · tokenizer {lock['tokenizer_id']}@{lock['tokenizer_revision'][:10]}")
PY

echo "Prueba de inferencia con datos sintéticos…"
TRADINGDESK_DATA_DIR="$DATA_DIR" "$VENV/bin/python" -m tradingdesk.kronos_selftest
