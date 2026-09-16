#!/usr/bin/env bash
# Idempotent dependency bootstrap for the horse_racing_prediction project.
#
# The repository is a Python project. This script installs project dependencies
# from whichever manifest is present. It is intentionally tolerant of an empty
# repository (no manifest yet) so environment setup succeeds before any
# application code lands, and automatically starts installing dependencies once
# a manifest is added.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

PYTHON="$(command -v python3)"
echo "Using $("$PYTHON" --version) at $PYTHON"

# The base image is a PEP 668 "externally managed" environment, so pip installs
# into the per-user location (~/.local) with the override flag. ~/.local/bin is
# already on the login PATH, so console scripts work without a virtualenv or any
# activation step, and packages are importable from every shell.
PIP=("$PYTHON" -m pip install --user --break-system-packages)

"${PIP[@]}" --upgrade pip

installed_something=0

if [ -f requirements.txt ]; then
  echo "Installing from requirements.txt"
  "${PIP[@]}" -r requirements.txt
  installed_something=1
fi

if [ -f requirements-dev.txt ]; then
  echo "Installing from requirements-dev.txt"
  "${PIP[@]}" -r requirements-dev.txt
  installed_something=1
fi

if [ "$installed_something" -eq 0 ] && [ -f pyproject.toml ]; then
  echo "Installing project from pyproject.toml (editable)"
  "${PIP[@]}" -e . || "${PIP[@]}" .
  installed_something=1
fi

if [ "$installed_something" -eq 0 ] && [ -f Pipfile ]; then
  echo "Installing pipenv and syncing from Pipfile"
  "${PIP[@]}" pipenv
  PIPENV_VENV_IN_PROJECT=1 "$PYTHON" -m pipenv install --dev || true
  installed_something=1
fi

if [ "$installed_something" -eq 0 ]; then
  echo "No dependency manifest found (requirements.txt / pyproject.toml / Pipfile)."
  echo "Skipping dependency install. Add a manifest and this step will install it automatically."
fi

echo "Install complete. Python ready: $("$PYTHON" --version)"
