"""routes/job_status.py — Prefix /api/generate_all_status, /api/render_status,
/api/produce_status, /api/plan_status, /api/thumbnail_status,
/api/voiceover_status, /api/transcribe_status.

Vierte Route-Gruppe aus dem dashboard.py-Handler (Refactor Phase 4, Teil 4).
Reine Read-Only-Statusabfragen auf die noch nicht migrierten globalen
Job-Dicts+Locks in dashboard.py (BATCH_JOBS/RENDER_JOBS/PRODUCE_JOBS/
PLAN_JOBS/THUMB_JOBS/VOICE_JOBS/TX_STATUS, siehe CLAUDE.md "JobRegistry —
wann einsetzen" fürs bewusste Nicht-Migrieren dieser Dicts) -- kein
Worker-Thread wird hier gestartet, kein externer API-Call, gleiche
Risikoklasse wie Teil 1-3.

Achtung Präfix-Überschneidung: "/api/plan_status" ist Präfix von
"/api/plan_status_reset" (bleibt bewusst in dashboard.py, gehört zur
Plan-Generierung). dispatch() matched trotzdem korrekt, weil handle() unten
den Pfad exakt vergleicht -- bei "/api/plan_status_reset" liefert das (False,
None) zurück und dashboard.py's do_POST-Kette greift wie bisher.

"/api/transcribe_status" existiert im alten Handler doppelt (GET UND POST,
identischer Body) -- hier wird nur die GET-Variante gezogen; die (bereits vor
diesem Refactor redundante) POST-Variante bleibt unverändert in dashboard.py.

Import von `dashboard` passiert lazy INNERHALB von handle() (siehe CLAUDE.md
"Layering/Import-Richtung").
"""
from __future__ import annotations


def handle(method, path, handler, qs, cid, vid, body):
    if method != "GET":
        return False, None

    if path == "/api/generate_all_status":
        import dashboard
        with dashboard._BATCH_JOBS_LOCK:
            state = dashboard.BATCH_JOBS.get((cid, vid))
        handler._send(200, state or {"running": False})
        return True, None

    if path == "/api/render_status":
        import dashboard
        target = qs.get("target", ["longform"])[0]
        with dashboard._RENDER_JOBS_LOCK:
            state = dashboard.RENDER_JOBS.get((cid, vid, target))
        handler._send(200, state or {"running": False})
        return True, None

    if path == "/api/produce_status":
        import dashboard
        with dashboard._PRODUCE_JOBS_LOCK:
            state = dashboard.PRODUCE_JOBS.get((cid, vid))
        handler._send(200, state or {"running": False})
        return True, None

    if path == "/api/plan_status":
        import dashboard
        with dashboard._PLAN_JOBS_LOCK:
            state = dashboard.PLAN_JOBS.get((cid, vid))
        handler._send(200, state or {"running": False})
        return True, None

    if path == "/api/thumbnail_status":
        import dashboard
        with dashboard._THUMB_JOBS_LOCK:
            state = dashboard.THUMB_JOBS.get((cid, vid))
        handler._send(200, state or {"running": False})
        return True, None

    if path == "/api/voiceover_status":
        import dashboard
        with dashboard._VOICE_JOBS_LOCK:
            state = dashboard.VOICE_JOBS.get((cid, vid))
        handler._send(200, state or {"running": False})
        return True, None

    if path == "/api/transcribe_status":
        import dashboard
        handler._send(200, dict(dashboard.TX_STATUS))
        return True, None

    return False, None
