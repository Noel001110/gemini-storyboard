"""routes/thumbnail.py — Prefix /api/generate_thumbnail.

Siebte Route-Gruppe aus dem dashboard.py-Handler (Refactor Phase 4, Teil 7).
Erste Gruppe, die einen Worker-Thread startet (workers/thumbnail.py, bereits
in Phase 3 aus dashboard.py extrahiert) -- höhere Risikoklasse als Teil 1-6
(reines CRUD/Settings/synchrone Calls), näher an den Phase-3-Worker-
Extraktionen. Status-Polling (GET /api/thumbnail_status) lebt bereits in
routes/job_status.py (Teil 4).

workers.thumbnail ist bereits ein eigenständiges Modul (Top-Level-Import
sicher). Die noch nicht extrahierten dashboard.py-Helfer (load_v_meta,
get_video_mode, read_master, THUMB_JOBS/_THUMB_JOBS_LOCK) bleiben lazy
importiert.
"""
from __future__ import annotations

import json
import threading
import time

from core.paths import ch_vid_master, v_plan
from engine.presets import VIDEO_MASTER_DEFAULT
from workers.thumbnail import run as _thumbnail_generate_worker


def handle(method, path, handler, qs, cid, vid, body):
    if method != "POST" or path != "/api/generate_thumbnail":
        return False, None

    import dashboard

    if not vid:
        handler._send(400, {"error": "Kein Video ausgewählt"})
        return True, None

    # Versuche das Skript zu lesen
    full_script = ""
    try:
        plan = json.load(open(v_plan(cid, vid)))
        full_script = " ".join(s.get("text", "") for s in plan["scenes"])
    except Exception:
        pass

    # Falls kein Skript da ist, nimm die Idee aus meta.json
    if not full_script.strip():
        meta = dashboard.load_v_meta(cid, vid)
        full_script = meta.get("idea", "")
        # Falls auch Idee leer, nimm ausgewählten Titel
        if not full_script.strip():
            full_script = meta.get("selected_title", "")

    if not full_script.strip():
        handler._send(400, {"error": "Kein Skript, Thema oder Titel vorhanden"})
        return True, None

    mode = dashboard.get_video_mode(cid, vid)
    try:
        master_style = (open(ch_vid_master(cid)).read().strip() if mode == "video"
                         else dashboard.read_master(cid)) or VIDEO_MASTER_DEFAULT
    except OSError:
        master_style = VIDEO_MASTER_DEFAULT
    chosen_title = dashboard.load_v_meta(cid, vid).get("selected_title", "")

    # Off the request thread — used to run inline (30-60s KIE submit+poll+download),
    # freezing the browser. Client polls /api/thumbnail_status. Same running-flag-set-
    # under-lock-before-thread-exists race guard as /api/plan.
    key = (cid, vid)
    with dashboard._THUMB_JOBS_LOCK:
        if dashboard.THUMB_JOBS.get(key, {}).get("running"):
            handler._send(200, {"ok": True, "already_running": True})
            return True, None
        dashboard.THUMB_JOBS[key] = {"running": True, "step": "Startet …", "error": None,
                                      "done": False, "file": None, "prompt": None, "ts": time.time()}
    threading.Thread(target=_thumbnail_generate_worker,
                      args=(cid, vid, full_script, master_style, chosen_title), daemon=True).start()
    handler._send(200, {"ok": True, "already_running": False})
    return True, None
