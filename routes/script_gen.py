"""routes/script_gen.py — Prefix /api/generate_script, /api/generate_titles,
/api/rewrite_script_retention.

Sechste Route-Gruppe aus dem dashboard.py-Handler (Refactor Phase 4, Teil 6);
/api/rewrite_script_retention kam am 2026-07-27 dazu (Long-Form-Retention-
Framework-Review, siehe engine/prompts.py Docstring bei
rewrite_script_for_retention). Alle drei Routen machen einen synchronen
LLM-Call (Gemini, via engine/prompts.py) und liefern das Ergebnis direkt in
der HTTP-Response zurück -- kein Worker-Thread, kein Job-Status-Polling
(anders als z.B. /api/plan oder /api/generate_thumbnail, die wegen ihrer
Laufzeit als Background-Jobs laufen; Skript-/Titel-/Rewrite-Generierung ist
kurz genug für eine synchrone Response).

engine/prompts.py ist mypy-strict und hat keine dashboard.py-Abhängigkeit --
Top-Level-Import hier ist sicher, kein lazy `import dashboard` für die
LLM-Calls nötig. Für Video-Meta (load_v_meta/save_v_meta) und den Plan-Read
bleibt `import dashboard` lazy, da diese Helfer noch nicht aus dashboard.py
extrahiert sind. /api/rewrite_script_retention braucht wie /api/generate_script
kein cid/vid -- reiner Text-Transform, kein dashboard-Import nötig.
"""
from __future__ import annotations

import json
import traceback

from core.paths import v_plan
from engine.prompts import generate_script, generate_titles, rewrite_script_for_retention


def handle(method, path, handler, qs, cid, vid, body):
    if method != "POST":
        return False, None

    if path == "/api/generate_script":
        d = body or {}
        raw = d.get("text", "").strip()
        lang = d.get("lang", "en")
        if not raw:
            handler._send(400, {"error": "Kein Text eingegeben."})
            return True, None
        try:
            print(f"  [Script] {lang.upper()} ({len(raw)} Zeichen) …", flush=True)
            handler._send(200, {"ok": True, "script": generate_script(raw, lang)})
        except Exception as e:
            traceback.print_exc()
            handler._send(500, {"error": str(e)})
        return True, None

    if path == "/api/rewrite_script_retention":
        d = body or {}
        raw = d.get("text", "").strip()
        lang = d.get("lang", "en")
        if not raw:
            handler._send(400, {"error": "Kein Skript-Text eingegeben."})
            return True, None
        try:
            print(f"  [RetentionRewrite] {lang.upper()} ({len(raw)} Zeichen) …", flush=True)
            handler._send(200, {"ok": True, "script": rewrite_script_for_retention(raw, lang)})
        except Exception as e:
            traceback.print_exc()
            handler._send(500, {"error": str(e)})
        return True, None

    if path == "/api/generate_titles":
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

        if not full_script.strip():
            handler._send(400, {"error": "Kein Skript und kein Thema vorhanden"})
            return True, None

        print("  [Title] Generiere Titel-Optionen …", flush=True)
        titles = generate_titles(full_script, n=5)
        if not titles:
            handler._send(500, {"error": "Titel-Generierung fehlgeschlagen"})
            return True, None
        meta = dashboard.load_v_meta(cid, vid)
        meta["titles"] = titles
        dashboard.save_v_meta(cid, vid, meta)
        handler._send(200, {"ok": True, "titles": titles})
        return True, None

    return False, None
