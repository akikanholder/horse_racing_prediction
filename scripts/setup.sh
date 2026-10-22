#!/usr/bin/env bash
# Create or reuse backend/.venv from the pinned lock, then run an offline self-check.
# Idempotent. Pass --force to delete backend/.venv and create it again.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -n "${PYTHON:-}" ]]; then
  PY="$PYTHON"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  echo "[setup] FAIL Python was not found on PATH. Install Python 3.11 or newer and rerun." >&2
  exit 1
fi

exec "$PY" "$ROOT/scripts/setup_support.py" "$@"
