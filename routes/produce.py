"""routes/produce.py — Prefix /api/produce_start, /api/produce_stop.

Elfte Route-Gruppe aus dem dashboard.py-Handler (Refactor Phase 4, Teil 11).
Startet/stoppt den Ein-Knopf-Orchestrator (workers/produce.py, bereits in
Phase 3 extrahiert) -- gleiches Muster wie routes/batch.py (Teil 9) und
routes/render.py (Teil 10). Status-Polling (GET /api/produce_status) lebt
schon seit Teil 4 in routes/job_status.py; keine Präfix-Überschneidung mit
_start/_stop/_status (wie in Teil 9/10 geprüft).

workers.produce wird direkt importiert (Top-Level-Import sicher). Anders als
_plan_generate_worker/_batch_generate_worker/_render_worker hat
_produce_worker KEINE weiteren Call-Sites via `dashboard._produce_worker`
außerhalb von dashboard.py selbst (geprüft) -- der alte Re-Export
(`from workers.produce import run as _produce_worker`) wurde deshalb aus
dashboard.py entfernt, analog zu _thumbnail_generate_worker in Teil 7.

Die noch nicht extrahierten dashboard.py-Job-Dicts (PRODUCE_JOBS/
_PRODUCE_JOBS_LOCK, BATCH_JOBS/_BATCH_JOBS_LOCK, RENDER_JOBS/
_RENDER_JOBS_LOCK) bleiben lazy importiert.
"""
from __future__ import annotations

import threading

from workers.produce import run as _produce_worker


def handle(method, path, handler, qs, cid, vid, body):
    if method != "POST":
        return False, None

    if path == "/api/produce_start":
        import dashboard
        if not vid:
            handler._send(400, {"error": "Kein Video ausgewählt"})
            return True, None
        d = body or {}
        key = (cid, vid)
        text = d.get("text", "")
        wpm = float(d.get("wpm", 130))
        sec = float(d.get("sec", 4))
        with dashboard._PRODUCE_JOBS_LOCK:
            if dashboard.PRODUCE_JOBS.get(key, {}).get("running"):
                handler._send(200, {"ok": True, "already_running": True})
                return True, None
            # Same atomic "set running=True before the thread exists" fix as
            # generate_all_start/render_start above.
            dashboard.PRODUCE_JOBS[key] = {"running": True, "stage": "startet", "stop_requested": False,
                                            "error": None, "file": None}
        threading.Thread(target=_produce_worker, args=(cid, vid, text, wpm, sec), daemon=True).start()
        handler._send(200, {"ok": True, "already_running": False})
        return True, None

    if path == "/api/produce_stop":
        import dashboard
        key = (cid, vid)
        with dashboard._PRODUCE_JOBS_LOCK:
            if key in dashboard.PRODUCE_JOBS:
                dashboard.PRODUCE_JOBS[key]["stop_requested"] = True
        # Also forward the stop into whichever sub-job is CURRENTLY running --
        # _produce_worker only checks its own stop_requested BETWEEN stages, so a
        # stage already in flight (image batch or render) needs its own flag set
        # too, otherwise "Stop" would silently wait for that stage to finish first.
        with dashboard._BATCH_JOBS_LOCK:
            if dashboard.BATCH_JOBS.get(key, {}).get("running"):
                dashboard.BATCH_JOBS[key]["stop_requested"] = True
        # _produce_worker rendert intern immer (cid, vid, "longform") -- siehe dort.
        with dashboard._RENDER_JOBS_LOCK:
            render_key = (cid, vid, "longform")
            if dashboard.RENDER_JOBS.get(render_key, {}).get("running"):
                dashboard.RENDER_JOBS[render_key]["stop_requested"] = True
        handler._send(200, {"ok": True})
        return True, None

    return False, None
