"""routes/channels.py — Prefix /api/channels, /api/videos (Kanal-/Video-CRUD).

Erste Route-Gruppe, die aus dem dashboard.py-Handler ausgelagert wird (Refactor
Phase 4). Bewusst zuerst gewählt: reines CRUD auf channels.json/videos.json,
keine Worker-Threads, keine externen API-Calls — niedrigstes Risiko unter den
~77 Routen im alten Handler. Folgt exakt dem module.handle(method, path,
handler, qs, cid, vid, body) -> (handled, result)-Vertrag, den shorts/api.py,
youtube/api.py und control/api.py bereits produktiv nutzen (siehe
routes/__init__.py:mount/dispatch).

Import von `dashboard` passiert lazy INNERHALB von handle() (siehe CLAUDE.md
"Layering/Import-Richtung") -- die Helfer (load_channels/save_channels/
load_videos/save_videos/create_video/ensure_channel) sind noch nicht aus
dashboard.py extrahiert; ein Top-Level-Import würde einen Zyklus riskieren,
weil dashboard.py dieses Modul selbst importiert (main()).
"""
from __future__ import annotations

import os
import re
import shutil
import time

from core.paths import v_dir


def handle(method, path, handler, qs, cid, vid, body):
    if method == "GET" and path == "/api/channels":
        import dashboard
        chs = dashboard.load_channels()
        for ch in chs:
            vids = dashboard.load_videos(ch["id"])
            ch["video_count"] = len(vids)
            # Active-Count = Videos mit plan.json ODER voiceover.mp3 (Phase-B-Hint)
            ch["active_count"] = sum(
                1 for v in vids
                if os.path.exists(os.path.join(v_dir(ch["id"], v["id"]), "generated", "plan.json"))
                or os.path.exists(os.path.join(v_dir(ch["id"], v["id"]), "uploads", "voiceover.mp3"))
            )
        handler._send(200, {"channels": chs})
        return True, None

    if method == "GET" and path == "/api/videos":
        import dashboard
        handler._send(200, {"videos": dashboard.load_videos(cid)})
        return True, None

    if method == "POST" and path == "/api/videos":
        import dashboard
        d = body or {}
        name = d.get("name", "Neues Video").strip()
        entry = dashboard.create_video(cid, name)
        handler._send(200, {"ok": True, **entry})
        return True, None

    if method == "POST" and path == "/api/videos/delete":
        import dashboard
        videos = [v for v in dashboard.load_videos(cid) if v["id"] != vid]
        dashboard.save_videos(cid, videos)
        shutil.rmtree(v_dir(cid, vid), ignore_errors=True)
        handler._send(200, {"ok": True})
        return True, None

    if method == "POST" and path == "/api/videos/rename":
        import dashboard
        d = body or {}
        new_name = d.get("name", "").strip()
        videos = dashboard.load_videos(cid)
        for v in videos:
            if v["id"] == vid and new_name:
                v["name"] = new_name
        dashboard.save_videos(cid, videos)
        handler._send(200, {"ok": True})
        return True, None

    if method == "POST" and path == "/api/channels":
        import dashboard
        from core.paths import ch_master
        d = body or {}
        name = d.get("name", "Neuer Kanal").strip()
        safe = re.sub(r"[^\w]", "_", name.lower())[:30] or "kanal"
        chs = dashboard.load_channels()
        ids = {c["id"] for c in chs}
        cid_new = safe if safe not in ids else f"{safe}_{int(time.time()) % 10000}"
        chs.append({"id": cid_new, "name": name})
        dashboard.save_channels(chs)
        dashboard.ensure_channel(cid_new)
        # Phase 38: Stil-Preset-Auswahl. Falls 'preset' mitgegeben wird und gültig
        # ist, wird das entsprechende Master-Preset nach channels/<cid>/master_prompt.txt
        # geschrieben. Existierende master_prompt.txt wird NIE überschrieben (Q.4).
        preset_id = d.get("preset")
        dst = ch_master(cid_new)
        chosen_preset = None
        if not os.path.exists(dst):
            from engine.presets import PRESET_MASTERS, DEFAULT_PRESET
            chosen_preset = preset_id if preset_id in PRESET_MASTERS else DEFAULT_PRESET
            with open(dst, "w") as f:
                f.write(PRESET_MASTERS[chosen_preset])
        handler._send(200, {"ok": True, "id": cid_new, "name": name, "preset": chosen_preset})
        return True, None

    if method == "POST" and path == "/api/channels/delete":
        import dashboard
        chs = dashboard.load_channels()
        if len(chs) <= 1:
            handler._send(400, {"error": "Letzter Kanal kann nicht gelöscht werden."})
            return True, None
        dashboard.save_channels([c for c in chs if c["id"] != cid])
        handler._send(200, {"ok": True})
        return True, None

    if method == "POST" and path == "/api/channels/rename":
        import dashboard
        d = body or {}
        new_name = d.get("name", "").strip()
        chs = dashboard.load_channels()
        for c in chs:
            if c["id"] == cid:
                c["name"] = new_name
        dashboard.save_channels(chs)
        handler._send(200, {"ok": True})
        return True, None

    if method == "POST" and path == "/api/channels/brand_color":
        # Phase 33.3.1 Bug-1 — Brand-Color pro Channel persistieren.
        import dashboard
        d = body or {}
        color = (d.get("brand_color") or "").strip()
        # Validierung: 7-stellige #RRGGBB oder 4-stellige #RGB (input[type=color])
        if color and not re.fullmatch(r"#(?:[0-9a-fA-F]{3}){1,2}", color):
            handler._send(400, {"error": f"brand_color invalid: {color!r}"})
            return True, None
        chs = dashboard.load_channels()
        for c in chs:
            if c["id"] == cid:
                c["brand_color"] = color
        dashboard.save_channels(chs)
        handler._send(200, {"ok": True, "brand_color": color})
        return True, None

    return False, None
