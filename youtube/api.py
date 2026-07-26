"""youtube/api.py — Prefix /api/youtube/, gemountet in dashboard.py main().

Phase 2: Packaging (Description/Tags/Kategorie, siehe youtube/metadata.py). Titel
selbst läuft weiterhin über die bestehenden /api/generate_titles + /api/select_title
(dashboard.py) -- hier wird nichts dupliziert.
Phase 3: oauth_start/oauth_callback (Authorization-Code-Flow, siehe youtube/oauth.py)
+ queue_add (Warteschlangen-Brücke aus dashboard.html, siehe store/db.py).

Import von `dashboard` passiert absichtlich erst INNERHALB von handle() (lazy) --
siehe shorts/api.py-Docstring für die Begründung (sys.modules-Fix in dashboard.py).
"""
from __future__ import annotations

import json
import os
import secrets
import time

import store.db as db
from youtube.metadata import generate_packaging, get_categories, generate_chapters, format_chapters_block

# Kurzlebiger In-Memory-State für den OAuth-CSRF-state-Parameter -- ausreichend für
# ein lokales Single-User-Tool (kein Multi-Tenant-Betrieb), kein DB-Eintrag nötig,
# der Flow beginnt und endet innerhalb desselben Prozess-Lebenszyklus.
_PENDING_STATES: dict = {}
_STATE_TTL_SEC = 600


def _new_state(cid: str) -> str:
    state = secrets.token_urlsafe(24)
    _PENDING_STATES[state] = (cid, time.time())
    for s, (_c, ts) in list(_PENDING_STATES.items()):
        if time.time() - ts > _STATE_TTL_SEC:
            _PENDING_STATES.pop(s, None)
    return state


def _pop_state(state: str) -> str | None:
    entry = _PENDING_STATES.pop(state, None)
    if not entry:
        return None
    cid, ts = entry
    if time.time() - ts > _STATE_TTL_SEC:
        return None
    return cid


def _send_redirect(handler, location: str) -> None:
    handler.send_response(302)
    handler.send_header("Location", location)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def handle(method, path, handler, qs, cid, vid, body):
    import dashboard

    if method == "GET" and path == "/api/youtube/categories":
        handler._send(200, {"categories": get_categories(cid)})
        return True, None

    if method == "POST" and path == "/api/youtube/generate_packaging":
        if not vid:
            handler._send(400, {"error": "Kein Video ausgewählt"})
            return True, None
        full_script = ""
        plan = None
        try:
            plan = json.load(open(dashboard.v_plan(cid, vid)))
            full_script = " ".join(s.get("text", "") for s in plan["scenes"])
        except Exception:
            pass
        meta = dashboard.load_v_meta(cid, vid)
        if not full_script.strip():
            full_script = meta.get("idea", "")
        if not full_script.strip():
            handler._send(400, {"error": "Kein Skript und kein Thema vorhanden"})
            return True, None
        chosen_title = meta.get("selected_title") or (meta.get("titles") or [""])[0]
        if not chosen_title:
            handler._send(400, {"error": "Erst einen Titel auswählen (Schritt Titel)"})
            return True, None

        print(f"  [Packaging] Generiere Description/Tags/Kategorie …", flush=True)
        categories = get_categories(cid)
        try:
            packaging = generate_packaging(full_script, chosen_title, categories)
        except Exception as e:
            handler._send(500, {"error": f"Packaging-Generierung fehlgeschlagen: {e}"})
            return True, None

        description = packaging["description"]
        # Kapitel-Marken (YouTube-Klick-Marken im Player) -- braucht start_aligned pro
        # Szene (Longform muss vorher fertig gerendert sein, gleiche Voraussetzung wie
        # beim Klimax-Short). generate_chapters() gibt None zurück, wenn zu wenige
        # gültige Blöcke übrig bleiben (z.B. Longform noch nie gerendert) -- dann bleibt
        # die Description einfach ohne Kapitel-Block, kein Fehler.
        if plan is not None:
            try:
                chapters = generate_chapters(plan.get("scenes", []))
                if chapters:
                    description = f"{description}\n\n{format_chapters_block(chapters)}"
            except Exception as e:
                print(f"  [Packaging] Kapitel-Generierung übersprungen: {e}", flush=True)

        meta["description"] = description
        meta["tags"] = packaging["tags"]
        meta["category_id"] = packaging["category_id"]
        dashboard.save_v_meta(cid, vid, meta)
        handler._send(200, {"ok": True, **packaging, "description": description})
        return True, None

    if method == "POST" and path == "/api/youtube/save_packaging":
        if not vid:
            handler._send(400, {"error": "Kein Video ausgewählt"})
            return True, None
        meta = dashboard.load_v_meta(cid, vid)
        d = body or {}
        meta["description"] = str(d.get("description", "")).strip()
        meta["tags"] = [str(t).strip() for t in (d.get("tags") or []) if str(t).strip()]
        meta["category_id"] = str(d.get("category_id", "")).strip()
        dashboard.save_v_meta(cid, vid, meta)
        handler._send(200, {"ok": True})
        return True, None

    # ── Phase 3: OAuth (Authorization-Code-Flow) ──────────────────────────────
    if method == "GET" and path == "/api/youtube/oauth_start":
        from youtube.oauth import build_auth_url, client_configured
        if not client_configured():
            handler._send(400, {"error": "~/.youtube_oauth_client.json fehlt — OAuth-Client "
                                          "noch nicht eingerichtet."})
            return True, None
        state = _new_state(cid)
        _send_redirect(handler, build_auth_url(state))
        return True, None

    if method == "GET" and path == "/api/youtube/oauth_callback":
        from youtube.oauth import exchange_code
        code = (qs.get("code") or [""])[0]
        state = (qs.get("state") or [""])[0]
        oauth_error = (qs.get("error") or [""])[0]
        if oauth_error:
            handler._send(400, {"error": f"Google OAuth abgelehnt/fehlgeschlagen: {oauth_error}"})
            return True, None
        real_cid = _pop_state(state)
        if not real_cid:
            handler._send(400, {"error": "Ungültiger oder abgelaufener OAuth-state — bitte "
                                          "erneut über control.html verbinden."})
            return True, None
        try:
            result = exchange_code(real_cid, code)
        except Exception as e:
            handler._send(500, {"error": f"OAuth-Token-Tausch fehlgeschlagen: {e}"})
            return True, None
        handler._send(200,
            f"<html><body style='font-family:sans-serif;padding:40px'>"
            f"<h2>YouTube verbunden ✓</h2>"
            f"<p>Kanal: <b>{result['channel_title']}</b></p>"
            f"<p>Dieses Fenster kann geschlossen werden.</p>"
            f"</body></html>",
            "text/html; charset=utf-8")
        return True, None

    # ── Phase 3: Warteschlangen-Brücke aus dashboard.html ─────────────────────
    if method == "POST" and path == "/api/youtube/queue_add":
        if not vid:
            handler._send(400, {"error": "Kein Video ausgewählt"})
            return True, None
        d = body or {}
        render_target = d.get("render_target", "longform")
        file_name = "final.mp4" if render_target == "longform" else f"final_{render_target}.mp4"
        file_path = os.path.join(dashboard.v_out(cid, vid), file_name)
        if not os.path.exists(file_path):
            handler._send(400, {"error": f"{file_name} existiert noch nicht — erst rendern."})
            return True, None
        meta = dashboard.load_v_meta(cid, vid)
        title = meta.get("selected_title") or ""
        if not title:
            handler._send(400, {"error": "Erst einen Titel auswählen."})
            return True, None
        from youtube.upload import assert_schedulable, next_publish_slot
        # Juli 2026 (User-Wunsch "Longform startet am selben Tag wie Short 1, 20 Uhr"):
        # ohne expliziten scheduled_at (der manuelle "Zur Warteschlange hinzufügen"-
        # Button in ⑦ schickt seither keinen mehr) wird der Termin serverseitig über
        # den Anker-Tag dieses Videos berechnet -- day_offset=0, da Longform selbst der
        # Anker ODER an den bereits gesetzten Anker (z.B. von den Shorts) andockt.
        # Der manuelle Termin-Editor in Control (dashboard.html:3306) schickt weiterhin
        # einen expliziten Wert und ist von dieser Änderung nicht betroffen.
        if d.get("scheduled_at"):
            try:
                scheduled_at = float(d["scheduled_at"])
            except (TypeError, ValueError):
                handler._send(400, {"error": "Ungültiger scheduled_at-Wert."})
                return True, None
        else:
            scheduled_at = next_publish_slot(cid, vid, day_offset=0)
        try:
            assert_schedulable(scheduled_at)
        except ValueError as e:
            handler._send(400, {"error": str(e)})
            return True, None
        qid = db.queue_add(cid, vid, render_target, file_path, title, scheduled_at,
                            description=meta.get("description", ""),
                            tags=meta.get("tags", []),
                            category_id=meta.get("category_id"))
        handler._send(200, {"ok": True, "queue_id": qid})
        return True, None

    return False, None
