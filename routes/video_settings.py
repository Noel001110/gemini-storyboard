"""routes/video_settings.py — Prefix /api/presets, /api/char_ref, /api/get_mode,
/api/set_mode, /api/master, /api/vid_master, /api/image_model, /api/overlay_opts,
/api/style_ref.

Zweite Route-Gruppe aus dem dashboard.py-Handler (Refactor Phase 4, Teil 2).
Reine Settings-Persistenz pro Channel/Video (master_prompt.txt, mode.json,
image_model, overlay_opts, style_ref_url.txt) -- kein Worker-Thread, kein
externer API-Call, gleiche Risikoklasse wie routes/channels.py (Teil 1).

Import von `dashboard` passiert lazy INNERHALB von handle() (siehe CLAUDE.md
"Layering/Import-Richtung") -- die Helfer (read_master/write_master/
get_video_mode/set_video_mode/get_video_image_model/set_video_image_model/
get_video_overlay_opts/set_video_overlay_opts/get_channel_style_ref(s)) sind
noch nicht aus dashboard.py extrahiert. VIDEO_MASTER_DEFAULT/VALID_IMAGE_MODELS
kommen direkt aus engine/ (dort bereits definiert, kein Zyklus-Risiko).
"""
from __future__ import annotations

from core.paths import ch_vid_master
from engine.imagegen import VALID_IMAGE_MODELS
from engine.presets import DEFAULT_PRESET, PRESET_DESCRIPTIONS, PRESET_MASTERS, VIDEO_MASTER_DEFAULT


def handle(method, path, handler, qs, cid, vid, body):
    if method == "GET" and path == "/api/presets":
        handler._send(200, {
            "presets": [
                {"id": pid, "description": PRESET_DESCRIPTIONS[pid]}
                for pid in PRESET_MASTERS
            ],
            "default": DEFAULT_PRESET,
        })
        return True, None

    if method == "GET" and path == "/api/char_ref":
        import dashboard
        # Audit Juli 2026 (Bereich 3): style_ref_url.txt kann jetzt mehrzeilig sein
        # (bis zu 3 Refs) -- über get_channel_style_ref() lesen statt raw, sonst
        # würde diese Legacy-Route eine gejointe Mehrzeilen-URL zurückgeben.
        handler._send(200, {"url": dashboard.get_channel_style_ref(cid)})
        return True, None

    if method == "GET" and path == "/api/get_mode":
        import dashboard
        handler._send(200, {"mode": dashboard.get_video_mode(cid, vid)})
        return True, None

    if method == "POST" and path == "/api/set_mode":
        import dashboard
        d = body or {}
        mode = d.get("mode", "image")
        if mode not in ("image", "video"):
            mode = "image"
        dashboard.set_video_mode(cid, vid, mode)
        handler._send(200, {"ok": True, "mode": mode})
        return True, None

    if method == "GET" and path == "/api/vid_master":
        try:
            txt = open(ch_vid_master(cid)).read()
        except OSError:
            txt = VIDEO_MASTER_DEFAULT
        handler._send(200, {"master": txt})
        return True, None

    if method == "POST" and path == "/api/vid_master":
        d = body or {}
        txt = d.get("master", "").strip()
        open(ch_vid_master(cid), "w").write(txt)
        handler._send(200, {"ok": True})
        return True, None

    if method == "GET" and path == "/api/master":
        import dashboard
        handler._send(200, {"master": dashboard.read_master(cid)})
        return True, None

    if method == "POST" and path == "/api/master":
        import dashboard
        d = body or {}
        dashboard.write_master(cid, d.get("master", ""))
        handler._send(200, {"ok": True})
        return True, None

    if method == "GET" and path == "/api/image_model":
        import dashboard
        handler._send(200, {
            "model": dashboard.get_video_image_model(cid, vid),
            "options": list(VALID_IMAGE_MODELS),
        })
        return True, None

    if method == "POST" and path == "/api/image_model":
        import dashboard
        if not vid:
            handler._send(400, {"error": "Kein Video ausgewählt"})
            return True, None
        d = body or {}
        dashboard.set_video_image_model(cid, vid, d.get("model", "nano-banana-2"))
        handler._send(200, {"ok": True})
        return True, None

    if method == "GET" and path == "/api/style_ref":
        import dashboard
        # Channel-level reference image(s). The frontend (openChannelSettings,
        # loadStyleRefStatus) calls /api/style_ref — the file is owned by the
        # channel (channels/<cid>/style_ref.png + .txt), not the video. Audit
        # Juli 2026 (Bereich 3): bis zu 3 Refs -- "urls" ist die Liste (Quelle der
        # Wahrheit), "url" bleibt für alte Frontend-Versionen als erster Eintrag.
        urls = dashboard.get_channel_style_refs(cid)
        handler._send(200, {"urls": urls, "url": urls[0] if urls else ""})
        return True, None

    if method == "GET" and path == "/api/overlay_opts":
        import dashboard
        handler._send(200, dashboard.get_video_overlay_opts(cid, vid))
        return True, None

    if method == "POST" and path == "/api/overlay_opts":
        import dashboard
        if not vid:
            handler._send(400, {"error": "Kein Video ausgewählt"})
            return True, None
        d = body or {}
        dashboard.set_video_overlay_opts(cid, vid, d.get("opts", {}))
        handler._send(200, {"ok": True})
        return True, None

    return False, None
