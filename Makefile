.PHONY: run test lint fmt install-dev

PYTHON := ./.venv_whisper/bin/python

# Refactor Phase 0: einheitliche Kommandos für lokal + CI (identische Pins,
# siehe requirements-dev.txt / .github/workflows/lint.yml).

run:
	./start.sh

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .
	$(PYTHON) -m mypy engine/ core/ 2>/dev/null || $(PYTHON) -m mypy engine/

fmt:
	$(PYTHON) -m ruff format .
	$(PYTHON) -m ruff check --fix .

install-dev:
	$(PYTHON) -m pip install -r requirements-dev.txt -r requirements-whisper.txt
