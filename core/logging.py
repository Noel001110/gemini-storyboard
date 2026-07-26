"""core/logging.py — zentrales strukturiertes Logging (Refactor Phase 1).

Ursprünglich als `dashboard._log()` eingeführt (Phase 3.4, #40): menschenlesbares
key=value-Format per Default, kompaktes JSON per `LOG_JSON=1` (für Log-Aggregation
wie ELK/Loki). Hierher verschoben, damit auch engine/, routes/, workers/ es nutzen
können, ohne über `import dashboard` zu gehen (siehe core/__init__.py).

Ersetzt bestehende print()-Aufrufe NICHT automatisch -- die Migration ist bewusst
schrittweise, Modul für Modul (siehe Refactoring-Plan Phase 1). dashboard.py
re-exportiert `log_event` weiterhin als `_log` für seine bestehenden Call-Sites.
"""
from __future__ import annotations

import json
import os
import time


def json_mode() -> bool:
    # Bewusst pro Aufruf gelesen statt einmalig beim Modul-Import eingefroren --
    # ein frozen Import-Time-Flag bräuchte einen vollen Modul-Reload, um auf eine
    # geänderte LOG_JSON-Env-Var zu reagieren (unpraktisch für Tests UND für einen
    # langlaufenden Server-Prozess).
    return os.environ.get("LOG_JSON", "0") == "1"


def log_event(level: str, event: str, **fields: object) -> None:
    """Strukturierte Log-Zeile. Im JSON-Modus: kompakte JSON-Zeile. Sonst: key=value-Format.
    Beispiel: log_event("INFO", "render_complete", video_id="v1", duration_s=42.5)"""
    if json_mode():
        out = {"ts": time.time(), "level": level, "event": event, **fields}
        try:
            print(json.dumps(out, ensure_ascii=False), flush=True)
        except (TypeError, ValueError):
            # Fallback bei nicht-serialisierbaren Werten
            print(f'{{"ts":{time.time()},"level":"{level}","event":"{event}"}}', flush=True)
    else:
        if fields:
            kvs = " ".join(f"{k}={v!r}" for k, v in fields.items())
            print(f"  [{level}] {event} {kvs}", flush=True)
        else:
            print(f"  [{level}] {event}", flush=True)
