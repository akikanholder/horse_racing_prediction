.PHONY: setup setup-force

# Create or reuse backend/.venv, install the lock, and run the offline self-check.
setup:
	bash scripts/setup.sh

# Delete backend/.venv and install the pinned dependencies again.
setup-force:
	bash scripts/setup.sh --force
