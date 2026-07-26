"""tests/conftest.py — pytest-Brücke für die hand-gerollten t_*-Testdateien.

Refactor Phase 0: migriert NUR den Runner, nicht die 242 Testfunktionen selbst.
test_pipeline_fixes.py und test_cinematic_e2e.py definieren beide je ein eigenes
`setup() -> tmp_home` und `teardown(tmp_home)` mit identischer Signatur (temp HOME,
Fake-Key-Datei, Test-Kanal/-Video anlegen). Statt diese Logik zu duplizieren, ruft
diese Fixture die modul-eigenen Funktionen generisch auf -- funktioniert für jede
Testdatei, die dem Muster folgt, auch künftige.

Die Datei-eigenen `main()`/`run()`-Runner bleiben unverändert erhalten (Aufruf per
`python3 tests/test_*.py` funktioniert weiterhin identisch); pytest ruft die
t_*-Funktionen jetzt zusätzlich direkt auf (siehe pyproject.toml [tool.pytest.ini_options]).
"""
import pytest


@pytest.fixture(scope="module", autouse=True)
def _module_test_env(request):
    mod = request.module
    if not hasattr(mod, "setup") or not hasattr(mod, "teardown"):
        yield
        return
    tmp_home = mod.setup()
    try:
        yield
    finally:
        mod.teardown(tmp_home)
