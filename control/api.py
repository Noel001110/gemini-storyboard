"""control/api.py — Prefix /api/control/, gemountet in dashboard.py main().

Status-Aggregation für control.html: Warteschlange, Quota, OAuth-Verbindungsstatus
pro Kanal. Kanal-/video-übergreifend, deshalb bewusst getrennt von den pro-Video-
Endpoints in dashboard.py/youtube/api.py.

Import von `dashboard` passiert absichtlich erst INNERHALB von handle() (lazy) --
siehe shorts/api.py-Docstring für die Begründung (sys.modules-Fix in dashboard.py).
"""
from __future__ import annotations

import store.db as db
from youtube import quota
from youtube.oauth import client_configured


def handle(method, path, handler, qs, cid, vid, body):
    if method == "GET" and path == "/api/control/queue":
        status = (qs.get("status") or [None])[0]
        handler._send(200, {"queue": db.queue_list(status)})
        return True, None

    if method == "GET" and path == "/api/control/video_publish_status":
        # Nur für DIESES eine Video (cid+vid) -- Quelle für dashboard.htmls
        # Publish-Übersichtskarte am Ende des Schritt-Flows. Nutzt dieselbe
        # db.queue_list() wie die globale Control-Queue, nur gefiltert.
        rows = [r for r in db.queue_list() if r["cid"] == cid and r["vid"] == vid]
        entries = [{
            "id": r["id"], "render_target": r["render_target"], "title": r["title"],
            "status": r["status"], "scheduled_at": r["scheduled_at"],
            "youtube_video_id": r["youtube_video_id"], "error": r["error"],
        } for r in rows]
        handler._send(200, {"entries": entries})
        return True, None

    if method == "GET" and path == "/api/control/quota":
        handler._send(200, {
            "remaining": quota.uploads_remaining_today(),
            "limit": quota.DAILY_UPLOAD_LIMIT,
            "oauth_client_configured": client_configured(),
        })
        return True, None

    if method == "GET" and path == "/api/control/channels":
        import dashboard
        out = []
        for ch in dashboard.load_channels():
            tokens = db.get_tokens(ch["id"])
            playlist_id = ""
            try:
                playlist_id = open(dashboard.ch_youtube_playlist_id(ch["id"])).read().strip()
            except OSError:
                pass
            out.append({
                "id": ch["id"],
                "name": ch.get("name", ch["id"]),
                "connected": bool(tokens),
                "youtube_channel_title": (tokens or {}).get("youtube_channel_title"),
                "playlist_id": playlist_id,
            })
        handler._send(200, {"channels": out, "oauth_client_configured": client_configured()})
        return True, None

    if method == "POST" and path == "/api/control/set_playlist":
        import dashboard
        d = body or {}
        target_cid = d.get("cid")
        if not target_cid:
            handler._send(400, {"error": "cid fehlt"})
            return True, None
        playlist_id = str(d.get("playlist_id", "")).strip()
        with open(dashboard.ch_youtube_playlist_id(target_cid), "w") as f:
            f.write(playlist_id)
        handler._send(200, {"ok": True})
        return True, None

    if method == "POST" and path == "/api/control/queue_update":
        d = body or {}
        qid = d.get("id")
        if qid is None:
            handler._send(400, {"error": "id fehlt"})
            return True, None
        fields: dict = {}
        if "scheduled_at" in d:
            try:
                scheduled_at = float(d["scheduled_at"])
            except (TypeError, ValueError):
                handler._send(400, {"error": "Ungültiger scheduled_at-Wert."})
                return True, None
            from youtube.upload import assert_schedulable
            try:
                assert_schedulable(scheduled_at)
            except ValueError as e:
                handler._send(400, {"error": str(e)})
                return True, None
            fields["scheduled_at"] = scheduled_at
            # Ein manuell verschobener Termin gilt wieder als frisch planbar -- falls der
            # Eintrag zuvor als 'failed' (zu naher Termin, siehe youtube/upload.py) markiert
            # war, reaktiviert eine neue, gültige Zeit ihn automatisch für den Worker.
            entry = db.queue_get(qid)
            if entry and entry["status"] == "failed":
                fields["status"] = "queued"
        for key in ("description", "category_id"):
            if key in d:
                fields[key] = str(d[key]).strip()
        if "tags" in d:
            import json
            fields["tags"] = json.dumps([str(t).strip() for t in (d["tags"] or []) if str(t).strip()])
        db.queue_update(qid, **fields)
        handler._send(200, {"ok": True})
        return True, None

    if method == "POST" and path == "/api/control/queue_cancel":
        d = body or {}
        qid = d.get("id")
        if qid is None:
            handler._send(400, {"error": "id fehlt"})
            return True, None
        db.queue_cancel(qid)
        handler._send(200, {"ok": True})
        return True, None

    return False, None
