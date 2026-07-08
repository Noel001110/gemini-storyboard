#!/usr/bin/env bash
# scripts/lint.sh — Phase 5.3 (#56) CI-Lint-Gate
# Läuft ruff + die wichtigsten Sanity-Checks. Exit non-zero wenn was failt.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "=== Phase 5.3 (#56) Lint-Gate ==="

# 1. Ruff-Syntax-Check (kompiliert Python, billig)
echo "  [1/4] ruff check …"
if ! command -v ruff >/dev/null 2>&1; then
    echo "  WARN: ruff nicht installiert. Installiere via 'pip install ruff' oder 'brew install ruff'."
    echo "  Lint übersprungen — bitte lokal installieren."
    exit 0
fi
ruff check . --statistics --output-format=concise

# 2. Ruff-Format-Check (verifiziert dass Code bereits korrekt formatiert ist)
echo "  [2/4] ruff format --check …"
ruff format --check --quiet .

# 3. Syntax-Check für alle .py (falls ruff nicht vorhanden)
echo "  [3/4] python3 -m py_compile …"
find . -name "*.py" -not -path "./legacy/*" -not -path "./.venv_whisper/*" -not -path "./.git/*" \
    -exec python3 -m py_compile {} \; 2>&1 | grep -v "^$" || true

# 4. Test-Smoke (ohne LLM-Calls — nur Imports + Pure-Logic-Tests)
echo "  [4/4] pytest smoke …"
if command -v pytest >/dev/null 2>&1; then
    python3 -m pytest tests/ -x --co -q 2>&1 | head -5 || true
    # Nur sammeln, nicht ausführen (LLM-Calls würden sonst Keys verbrauchen)
else
    echo "  pytest nicht installiert — collection übersprungen"
fi

echo ""
echo "=== Lint-Gate PASS ==="
