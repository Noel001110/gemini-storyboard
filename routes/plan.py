"""routes/plan.py — Prefix /api/plan (exakt, NICHT /api/plan_status*).

Achte Route-Gruppe aus dem dashboard.py-Handler (Refactor Phase 4, Teil 8).
GET liefert den gespeicherten Szenenplan (reiner Read); POST startet den
Plan-Worker-Thread (workers/plan.py, bereits in Phase 3 extrahiert) --
gleiches Muster wie routes/thumbnail.py (Teil 7): Job-Dict-Guard
(PLAN_JOBS/_PLAN_JOBS_LOCK) + threading.Thread(...).start().

Präfix-Falle (wichtig für main()-Mount-Reihenfolge): "/api/plan" ist ein
String-Präfix von "/api/plan_status" UND "/api/plan_status_reset". routes/
__init__.py:dispatch() matched den ERSTEN registrierten Präfix, unabhängig
davon ob dessen handle() den Pfad tatsächlich exakt kennt -- ein `return
False, None` bei Nicht-Match fällt NICHT zum nächsten Mount durch, sondern
landet direkt in der alten dashboard.py-Kette (die für bereits verschobene
Routen nicht mehr existiert). Deshalb: /api/plan_status UND
/api/plan_status_reset leben BEIDE in routes/job_status.py (gleiche
Präfix-Familie), und mount("/api/plan_status", ...) muss in main() VOR
mount("/api/plan", ...) registriert werden. Dieses Modul kennt nur den
exakten Pfad "/api/plan", nie ein "/api/plan_*"-Präfix.

workers.plan wird direkt importiert (Top-Level-Import sicher). Die noch
nicht extrahierten dashboard.py-Helfer (clean_script, save_v_script,
PLAN_JOBS/_PLAN_JOBS_LOCK) bleiben lazy importiert.
"""
from __future__ import annotations

import json
import threading
import time

from core.paths import v_plan
from workers.plan import run as _plan_generate_worker


def handle(method, path, handler, qs, cid, vid, body):
    if method == "GET" and path == "/api/plan":
        try:
            handler._send(200, json.load(open(v_plan(cid, vid))))
        except Exception:
            handler._send(200, {"scenes": []})
        return True, None

    if method == "POST" and path == "/api/plan":
        import dashboard
        d = body or {}
        wpm = float(d.get("wpm", 150))
        sec = float(d.get("sec", 4))
        text = dashboard.clean_script(d.get("script", ""))
        if not text:
            handler._send(200, {"scenes": []})
            return True, None
        if not vid:
            handler._send(400, {"error": "Kein Video ausgewählt"})
            return True, None

        # Script speichern! Wenn der User klickt und sofort reloadet, bricht der Timeout ab
        try:
            payload = {
                "text": d.get("script", ""),
                "language": d.get("language", "en"),
                "preset": d.get("preset", "flat_cartoon_doc"),
                "updatedAt": int(time.time()),
            }
            dashboard.save_v_script(cid, vid, payload)
        except Exception as e:
            print(f"WARNUNG: /api/plan Skript speichern: {e}", flush=True)

        key = (cid, vid)
        with dashboard._PLAN_JOBS_LOCK:
            if dashboard.PLAN_JOBS.get(key, {}).get("running"):
                handler._send(200, {"ok": True, "already_running": True})
                return True, None
            # Set running=True atomically with the check, before the worker thread
            # exists — same fix as the image batch job race (see generate_all_start).
            dashboard.PLAN_JOBS[key] = {"running": True, "step": "Startet …", "error": None, "done": False}
        threading.Thread(target=_plan_generate_worker, args=(cid, vid, text, wpm, sec), daemon=True).start()
        handler._send(200, {"ok": True, "already_running": False})
        return True, None

    return False, None
