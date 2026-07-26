"""routes/audio.py — Prefix /api/upload_audio, /api/transcribe (exakt, NICHT
/api/transcribe_status).

Sechzehnte Route-Gruppe aus dem dashboard.py-Handler (Refactor Phase 4,
Teil 16 -- letzte Gruppe, danach ist Phase 4 abgeschlossen). Audio-Upload
(reines Datei-I/O) + Whisper-Transkription (synchroner Aufruf von
_transcribe_generate_worker, kein Worker-Thread -- die Route blockt bis die
Transkription fertig ist, gleiche Risikoklasse wie Teil 5/6/12).

Präfix-Falle (wie in Teil 8/13 dokumentiert): "/api/transcribe" ist ein
String-Präfix von "/api/transcribe_status". Die POST-Variante von
/api/transcribe_status (ein bereits vor diesem Refactor redundantes Duplikat
der GET-Variante) lebt deshalb NICHT hier, sondern in routes/job_status.py
(gleiche Präfix-Familie wie die GET-Variante, dort seit Teil 4).

Die noch nicht extrahierten dashboard.py-Helfer (ensure_video,
_atomic_write_json, _transcribe_generate_worker, _PLAN_WRITE_LOCK,
TX_STATUS) bleiben lazy importiert.
"""
from __future__ import annotations

import base64
import json
import os
import traceback

from core.paths import v_audio, v_plan, v_uploads


def handle(method, path, handler, qs, cid, vid, body):
    if method != "POST":
        return False, None

    if path == "/api/upload_audio":
        import dashboard
        d = body or {}
        try:
            raw = base64.b64decode(d["data"])
        except Exception:
            handler._send(400, {"error": "Ungültige Base64-Daten"})
            return True, None
        if not vid:
            handler._send(400, {"error": "Kein Video ausgewählt"})
            return True, None
        dashboard.ensure_video(cid, vid)
        ext = (d.get("name", "audio.bin").rsplit(".", 1)[-1].lower()) or "bin"
        local_path = os.path.join(v_uploads(cid, vid), f"voiceover.{ext}")
        open(local_path, "wb").write(raw)
        json.dump({"path": local_path, "mime": d.get("mime", "audio/mpeg"), "name": d.get("name", "")},
                  open(v_audio(cid, vid), "w"))
        # A fresh recording invalidates any previously trimmed audio and word-
        # alignment computed against the OLD file -- both would silently produce
        # wrong timing/cuts if left in place (the pause-trim + start_aligned/
        # end_aligned re-derive automatically at the next render, see _render_worker).
        trimmed_path = os.path.join(v_uploads(cid, vid), "voiceover_trimmed.wav")
        if os.path.exists(trimmed_path):
            os.remove(trimmed_path)
        try:
            with dashboard._PLAN_WRITE_LOCK:
                plan = json.load(open(v_plan(cid, vid)))
                for s in plan.get("scenes", []):
                    s.pop("start_aligned", None)
                    s.pop("end_aligned", None)
                dashboard._atomic_write_json(v_plan(cid, vid), plan, ensure_ascii=False, indent=1)
        except Exception:
            pass
        print(f"  [Audio] {os.path.basename(local_path)} ({len(raw)//1024} KB)", flush=True)
        handler._send(200, {"ok": True, "size": len(raw), "name": d.get("name", "")})
        return True, None

    if path == "/api/transcribe":
        import dashboard
        d = body or {}
        sec = float(d.get("sec", 4))
        if not vid:
            handler._send(400, {"error": "Kein Video ausgewählt"})
            return True, None
        if not os.path.exists(v_audio(cid, vid)):
            handler._send(400, {"error": "Keine Audio-Datei hochgeladen."})
            return True, None
        dashboard.TX_STATUS["running"] = True
        dashboard.TX_STATUS["error"] = ""
        try:
            out = dashboard._transcribe_generate_worker(cid, vid, sec)
        except Exception as e:
            traceback.print_exc()
            dashboard.TX_STATUS["running"] = False
            dashboard.TX_STATUS["error"] = str(e)
            handler._send(500, {"error": f"Transkription fehlgeschlagen: {e}"})
            return True, None
        dashboard.TX_STATUS["running"] = False
        handler._send(200, out)
        return True, None

    return False, None
