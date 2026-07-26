"""routes/video_meta.py — Prefix /api/video_meta, /api/stepper_state,
/api/script, /api/select_title, /api/save_script, /api/save_idea.

Dritte Route-Gruppe aus dem dashboard.py-Handler (Refactor Phase 4, Teil 3).
Reine Meta-/Skript-Persistenz pro Video (meta.json, script.json) + ein
konsolidierter Read-Only-Aggregat-Endpoint (stepper_state) -- kein
Worker-Thread, kein externer API-Call, gleiche Risikoklasse wie
routes/channels.py und routes/video_settings.py (Teil 1+2).

Import von `dashboard` passiert lazy INNERHALB von handle() (siehe CLAUDE.md
"Layering/Import-Richtung") -- die Helfer (load_v_meta/save_v_meta/
load_v_script/save_v_script) sind noch nicht aus dashboard.py extrahiert.
"""
from __future__ import annotations

import json
import os
import re
import time

from core.paths import v_out, v_plan, v_uploads


def handle(method, path, handler, qs, cid, vid, body):
    if method == "GET" and path == "/api/video_meta":
        import dashboard
        meta = dashboard.load_v_meta(cid, vid)
        meta["has_thumbnail"] = os.path.exists(os.path.join(v_out(cid, vid), "thumbnail.jpg"))
        handler._send(200, meta)
        return True, None

    if method == "GET" and path == "/api/stepper_state":
        import dashboard
        # Konsolidierter Endpunkt: alle 5 Heuristik-Bedingungen in EINEM Round-Trip.
        # Vorher: Frontend machte 5 separate fetches → race-anfällig, langsam, viele
        # 404s wenn das Backend-Routing anders benannt ist. Mit diesem Endpunkt hat
        # das Frontend genau eine Quelle der Wahrheit für "wie weit ist das Video?".
        try:
            meta = dashboard.load_v_meta(cid, vid)
        except Exception:
            meta = {}
        plan_path = v_plan(cid, vid)
        audio_path = os.path.join(v_uploads(cid, vid), "voiceover.mp3")
        out_dir = v_out(cid, vid)
        # Image count: plan.json scenes total + Anzahl generierter *NNN.jpg im out/.
        try:
            plan = json.load(open(plan_path)) if os.path.exists(plan_path) else {}
        except Exception:
            plan = {}
        total_scenes = len(plan.get("scenes") or [])
        try:
            generated_files = [f for f in os.listdir(out_dir) if re.match(r"^\d{3}\.jpg$", f)]
            generated_count = len(generated_files)
        except Exception:
            generated_count = 0
        rendered = bool(meta.get("rendered_at")) or \
            os.path.exists(os.path.join(v_out(cid, vid), "final.mp4"))
        handler._send(200, {
            # ① THEMA: meta.json + selected_title nicht leer (siehe 33.2-Heuristik)
            "thema_done": bool((meta.get("selected_title") or "").strip()),
            # ② SKRIPT
            "plan_done":   os.path.exists(plan_path),
            # ③ AUDIO: NUR voiceover.mp3, kein audio_meta.json-Fallback (Race-Bug-Safe)
            "audio_done":  os.path.exists(audio_path),
            # ④ BILDER: counter (N / M), kein binärer done-Threshold
            "images_done": generated_count,
            "images_total": total_scenes,
            # ⑤ RENDER
            "rendered":    rendered,
            # raw meta für UI-Sidebars (nicht für Stepper selbst, aber das Backend
            # hat's geladen — Übertragung vermeidet zweiten Fetch)
            "meta":        meta,
        })
        return True, None

    if method == "GET" and path == "/api/script":
        import dashboard
        if not vid:
            handler._send(200, {"text": None})
            return True, None
        data = dashboard.load_v_script(cid, vid)
        handler._send(200, data or {"text": None})
        return True, None

    if method == "POST" and path == "/api/select_title":
        import dashboard
        if not vid:
            handler._send(400, {"error": "Kein Video ausgewählt"})
            return True, None
        d = body or {}
        meta = dashboard.load_v_meta(cid, vid)
        meta["selected_title"] = d.get("title", "").strip()
        dashboard.save_v_meta(cid, vid, meta)
        handler._send(200, {"ok": True})
        return True, None

    if method == "POST" and path == "/api/save_script":
        import dashboard
        # The frontend had a localStorage workaround that worked for a single browser
        # on a single machine but lost the script the moment Noel opened the dashboard
        # on the Mac after writing it on the laptop. script.json fixes that — written
        # debounced (every ~2.5s while typing), read once on video load, never blocks.
        if not vid:
            handler._send(400, {"error": "Kein Video ausgewählt"})
            return True, None
        d = body or {}
        text = d.get("text", "")
        if not isinstance(text, str):
            handler._send(400, {"error": "text muss String sein"})
            return True, None
        # Hard cap — protects against accidental paste of a 500-page document into
        # a single script.json. ~500KB is enough for ~5h narration at ~150wpm.
        if len(text) > 500_000:
            handler._send(413, {"error": "Skript zu lang (>500k Zeichen)"})
            return True, None
        payload = {
            "text": text,
            "language": d.get("language", "de"),
            "preset": d.get("preset", "flat_cartoon_doc"),
            "updatedAt": int(time.time()),
        }
        try:
            dashboard.save_v_script(cid, vid, payload)
        except Exception as e:
            handler._send(500, {"error": f"Schreiben fehlgeschlagen: {e}"})
            return True, None
        handler._send(200, {"ok": True, "savedAt": payload["updatedAt"]})
        return True, None

    if method == "POST" and path == "/api/save_idea":
        import dashboard
        if not vid:
            handler._send(400, {"error": "Kein Video ausgewählt"})
            return True, None
        d = body or {}
        meta = dashboard.load_v_meta(cid, vid)
        meta["idea"] = d.get("idea", "").strip()
        dashboard.save_v_meta(cid, vid, meta)
        handler._send(200, {"ok": True})
        return True, None

    return False, None
