"""routes/misc.py — Prefix /api/job_status, /health, /api/health,
/api/measure_wpm, /api/download, /generated/.

Dreizehnte Route-Gruppe aus dem dashboard.py-Handler (Refactor Phase 4,
Teil 13). Reine GET-Routen: Job-Status-Lookup auf das globale JOBS-Dict
(unabhängig von den bereits in routes/job_status.py gebündelten
BATCH_JOBS/RENDER_JOBS/etc.), der Health-Check, die WPM-Messung, der
ZIP-Download aller generierten Bilder, und das Ausliefern generierter
Bild-/Video-Dateien aus dem Output-Verzeichnis. Kein Worker-Thread, kein
externer API-Call.

"/" und "/control" (statische HTML-Seiten) bleiben BEWUSST in dashboard.py --
"/" ist ein String-Präfix von JEDEM Pfad, ein mount("/", ...) müsste also
IMMER als letzter Eintrag in main()'s Mount-Reihenfolge stehen und dürfte nie
etwas nach sich haben (jeder künftige Mount danach wäre totes Gewicht). Für
zwei Zeilen ist das fragile Invariante nicht wert; sie bleiben als letzte
Prüfung im alten do_GET-Zweig.

Die noch nicht extrahierten dashboard.py-Globals (JOBS, BATCH_JOBS,
RENDER_JOBS, _START_TIME, _SHUTDOWN_IN_PROGRESS, _CURRENT_GIT_COMMIT,
_measure_channel_wpm) bleiben lazy importiert.
"""
from __future__ import annotations

import io
import json
import os
import time
import zipfile

from core.paths import v_out, v_plan


def handle(method, path, handler, qs, cid, vid, body):
    if method != "GET":
        return False, None

    if path == "/api/job_status":
        import dashboard
        job_id = qs.get("job_id", [""])[0]
        handler._send(200, dashboard.JOBS.get(job_id, {"status": "unknown"}))
        return True, None

    if path in ("/health", "/api/health"):
        import dashboard
        uptime_sec = time.time() - dashboard._START_TIME
        active_jobs = sum(1 for v in dashboard.JOBS.values() if v.get("status") == "running")
        with dashboard._BATCH_JOBS_LOCK:
            active_batches = sum(1 for v in dashboard.BATCH_JOBS.values() if v and v.get("running"))
        with dashboard._RENDER_JOBS_LOCK:
            active_renders = sum(1 for v in dashboard.RENDER_JOBS.values() if v and v.get("running"))
        handler._send(200, {
            "status": "ok" if not dashboard._SHUTDOWN_IN_PROGRESS else "shutting_down",
            "uptime_sec": round(uptime_sec, 1),
            "active_jobs": active_jobs,
            "active_batches": active_batches,
            "active_renders": active_renders,
            "version": "main/" + (dashboard._CURRENT_GIT_COMMIT[:7] if dashboard._CURRENT_GIT_COMMIT else "unknown"),
        })
        return True, None

    if path == "/api/measure_wpm":
        import dashboard
        # Struktur-/Schnitt-Review Juli 2026: gibt die reale, aus bereits fertigen
        # Videos DIESES Kanals gemessene Sprechrate zurück (statt der festen 150/160-
        # Annahme) -- Frontend nutzt das als informierten Default fürs #wpm-Feld.
        measured = dashboard._measure_channel_wpm(cid, fallback=None)
        handler._send(200, {
            "wpm": round(measured, 1) if measured is not None else None,
            "measured": measured is not None,
        })
        return True, None

    if path == "/api/download":
        ts_map = {}
        try:
            plan = json.load(open(v_plan(cid, vid)))
            for s in plan.get("scenes", []):
                t = s.get("t", "").replace(":", "-")
                ts_map[f"{s['i']:03d}.jpg"] = f"{t}.jpg"
                ts_map[f"{s['i']:03d}.png"] = f"{t}.png"
        except Exception:
            pass
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for f in sorted(os.listdir(v_out(cid, vid))):
                if f.endswith(".png") or f.endswith(".jpg"):
                    z.write(os.path.join(v_out(cid, vid), f), ts_map.get(f, f))
        handler._send(200, buf.getvalue(), "application/zip")
        return True, None

    if path.startswith("/generated/"):
        fp = os.path.join(v_out(cid, vid), os.path.basename(path))
        if os.path.exists(fp):
            b = open(fp, "rb").read()
            name = os.path.basename(fp)
            if name.endswith(".mp4"):
                handler._send(200, b, "video/mp4")
            else:
                handler._send(200, b, "image/jpeg" if b[:2] == b"\xff\xd8" else "image/png")
        else:
            handler._send(404, {"error": "not found"})
        return True, None

    return False, None
