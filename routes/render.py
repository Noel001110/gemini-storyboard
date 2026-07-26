"""routes/render.py — Prefix /api/render_start, /api/render_stop.

Zehnte Route-Gruppe aus dem dashboard.py-Handler (Refactor Phase 4, Teil 10).
Startet/stoppt den Render-Worker (workers/render.py, bereits in Phase 3
extrahiert) -- gleiches Muster wie routes/batch.py (Teil 9). Status-Polling
(GET /api/render_status) lebt schon seit Teil 4 in routes/job_status.py;
keine Präfix-Überschneidung mit _start/_stop/_status (alle drei Suffixe
divergieren früh genug, siehe Teil 9 für den analogen Batch-Check).

workers.render und RENDER_TARGETS (engine/render.py) werden direkt
importiert (Top-Level-Import sicher). Die noch nicht extrahierten
dashboard.py-Job-Dicts (RENDER_JOBS/_RENDER_JOBS_LOCK) bleiben lazy
importiert.
"""
from __future__ import annotations

import json
import threading
import time

from core.paths import v_plan
from engine.render import RENDER_TARGETS
from workers.render import run as _render_worker


def handle(method, path, handler, qs, cid, vid, body):
    if method != "POST":
        return False, None

    if path == "/api/render_start":
        import dashboard
        if not vid:
            handler._send(400, {"error": "Kein Video ausgewählt"})
            return True, None
        d = body or {}
        force = bool(d.get("force", False))
        target = d.get("target", "longform")
        if target not in RENDER_TARGETS:
            handler._send(400, {"error": f"Unbekanntes render target: {target}"})
            return True, None
        key = (cid, vid, target)
        with dashboard._RENDER_JOBS_LOCK:
            if dashboard.RENDER_JOBS.get(key, {}).get("running"):
                handler._send(200, {"ok": True, "already_running": True})
                return True, None
            # Juli 2026 Fix (Audit 4.2): _apply_sync_invariant stretches whatever
            # scenes DO have a generated file across the FULL audio duration — a
            # render with e.g. 20/96 rendered scenes silently produces a full-length
            # video where each of those 20 images is shown ~5x longer than intended,
            # no warning anywhere. Surface the partial-render fact BEFORE starting so
            # the frontend can confirm() with the user; `force: true` skips this once
            # confirmed (same pattern as /api/generate_all_start's `force`).
            if not force:
                try:
                    plan_for_check = json.load(open(v_plan(cid, vid)))
                    total_scenes = len(plan_for_check.get("scenes", []))
                    rendered_scenes = sum(1 for s in plan_for_check.get("scenes", []) if s.get("file"))
                except Exception:
                    total_scenes = rendered_scenes = 0
                if total_scenes and rendered_scenes < total_scenes:
                    handler._send(200, {
                        "ok": False, "partial": True,
                        "rendered": rendered_scenes, "total": total_scenes,
                    })
                    return True, None
            # Same atomic "set running=True before the thread exists" fix as
            # generate_all_start above — avoids two rapid start calls each
            # spinning up their own render worker on the same video.
            # started_ts: für die ETA-Berechnung im Frontend (Fortschrittsbalken).
            # stage() unten aktualisiert den Job-Dict nur per .update(), started_ts
            # bleibt also über die gesamte Render-Laufzeit erhalten.
            dashboard.RENDER_JOBS[key] = {"running": True, "stop_requested": False, "stage": "startet",
                                           "done": 0, "total": 0, "error": None, "file": None,
                                           "started_ts": time.time()}
        threading.Thread(target=_render_worker, args=(cid, vid, target), daemon=True).start()
        handler._send(200, {"ok": True, "already_running": False})
        return True, None

    if path == "/api/render_stop":
        import dashboard
        d = body or {}
        target = d.get("target", "longform")
        key = (cid, vid, target)
        with dashboard._RENDER_JOBS_LOCK:
            if key in dashboard.RENDER_JOBS:
                dashboard.RENDER_JOBS[key]["stop_requested"] = True
        handler._send(200, {"ok": True})
        return True, None

    return False, None
