"""routes/batch.py — Prefix /api/generate_all_start, /api/generate_all_stop.

Neunte Route-Gruppe aus dem dashboard.py-Handler (Refactor Phase 4, Teil 9).
Startet/stoppt den Batch-Bild-Worker (workers/batch.py, bereits in Phase 3
extrahiert) -- gleiches Muster wie routes/thumbnail.py (Teil 7) und
routes/plan.py (Teil 8): Job-Dict-Guard (BATCH_JOBS/_BATCH_JOBS_LOCK) +
threading.Thread(...).start(). Status-Polling (GET /api/generate_all_status)
lebt schon seit Teil 4 in routes/job_status.py.

/api/generate_one (Einzelbild-Generierung) ist bewusst NICHT Teil dieser
Gruppe -- die Route enthält ~130 Zeilen inline Business-Logik (Prompt-Bau,
Charsheet-/Chain-/Style-Ref-Auflösung), die anders als bei batch/render/plan/
thumbnail/produce NIE in ein workers/-Modul extrahiert wurde. Das ist ein
größerer, eigenständiger Schnitt als ein reiner Route-Move und wird separat
angegangen.

workers.batch wird direkt importiert (Top-Level-Import sicher). Die noch
nicht extrahierten dashboard.py-Job-Dicts (BATCH_JOBS/_BATCH_JOBS_LOCK)
bleiben lazy importiert.
"""
from __future__ import annotations

import threading

from workers.batch import run as _batch_generate_worker


def handle(method, path, handler, qs, cid, vid, body):
    if method != "POST":
        return False, None

    if path == "/api/generate_all_start":
        import dashboard
        if not vid:
            handler._send(400, {"error": "Kein Video ausgewählt"})
            return True, None
        d = body or {}
        # force=True: auch bereits generierte Bilder neu erzeugen ("Alle neu generieren")
        force = bool(d.get("force", False))
        key = (cid, vid)
        with dashboard._BATCH_JOBS_LOCK:
            if dashboard.BATCH_JOBS.get(key, {}).get("running"):
                handler._send(200, {"ok": True, "already_running": True})
                return True, None
            # Set running=True HERE, atomically with the check above, before the
            # worker thread even exists — not inside the thread itself. Setting it
            # later left a window where two rapid start calls (e.g. a user
            # double-clicking, or stop-then-immediately-start) could both see
            # "not running" and each spin up their own worker, causing multiple
            # concurrent generation loops hammering KIE in parallel.
            dashboard.BATCH_JOBS[key] = {"running": True, "stop_requested": False, "done": 0,
                                          "total": 0, "current_i": [], "error": None}
        threading.Thread(target=_batch_generate_worker, args=(cid, vid, force), daemon=True).start()
        handler._send(200, {"ok": True, "already_running": False})
        return True, None

    if path == "/api/generate_all_stop":
        import dashboard
        key = (cid, vid)
        with dashboard._BATCH_JOBS_LOCK:
            if key in dashboard.BATCH_JOBS:
                dashboard.BATCH_JOBS[key]["stop_requested"] = True
        handler._send(200, {"ok": True})
        return True, None

    return False, None
