"""routes/charsheets.py — Prefix /api/charsheets, /charsheets/,
/api/upload_charref, /api/gen_charsheet, /api/charsheet_update,
/api/charsheet_delete.

Vierzehnte Route-Gruppe aus dem dashboard.py-Handler (Refactor Phase 4,
Teil 14). Charakter-Referenz-Verwaltung: Liste/Ausliefern von Charsheet-
Dateien (reines Datei-I/O) + Upload/KI-Generierung/Update/Löschen. Ein
synchroner externer API-Call (`gen_charsheet` -> KIE, `analyze_char_image`
+ `upload_image_public`), aber kein Worker-Thread.

engine.prompts (analyze_char_image, gen_charsheet) und engine.imagegen
(upload_image_public) sind mypy-strict ohne dashboard.py-Abhängigkeit --
Top-Level-Import sicher. ch_sheets kommt aus core.paths.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import traceback

from core.paths import ch_sheets
from engine.imagegen import upload_image_public
from engine.prompts import analyze_char_image, gen_charsheet


def handle(method, path, handler, qs, cid, vid, body):
    if method == "GET" and path == "/api/charsheets":
        # vid aus Query-String: /api/charsheets?channel=...&vid=...
        vid_param = (qs.get("vid", [None]) or [None])[0]
        sheet_dir = ch_sheets(cid, vid_param) if vid_param else ch_sheets(cid)
        sheets = []
        try:
            files = os.listdir(sheet_dir)
        except OSError:
            files = []
        for f in sorted(files):
            if f.endswith(".json"):
                try:
                    meta = json.load(open(os.path.join(sheet_dir, f)))
                    img = os.path.join(sheet_dir, f.replace(".json", ".png"))
                    meta["has_image"] = os.path.exists(img)
                    sheets.append(meta)
                except Exception:
                    pass
        handler._send(200, {"sheets": sheets})
        return True, None

    if method == "GET" and path.startswith("/charsheets/"):
        # Same: vid-aware lookup with channel-pool fallback
        vid_param = (qs.get("vid", [None]) or [None])[0]
        sheet_dir = ch_sheets(cid, vid_param) if vid_param else ch_sheets(cid)
        fp = os.path.join(sheet_dir, os.path.basename(path))
        if os.path.exists(fp):
            b = open(fp, "rb").read()
            handler._send(200, b, "image/jpeg" if b[:2] == b"\xff\xd8" else "image/png")
        else:
            handler._send(404, {"error": "not found"})
        return True, None

    if method != "POST":
        return False, None

    if path == "/api/upload_charref":
        d = body or {}
        name = d.get("name", "Charakter").strip()
        img_b64 = d.get("image", "")
        mime = d.get("mime", "image/png")
        upload_vid = d.get("vid")  # July 2026: charsheets are now per-video
        if not name:
            handler._send(400, {"error": "name fehlt"})
            return True, None
        if not img_b64:
            handler._send(400, {"error": "image fehlt"})
            return True, None
        # B-1 Fix: validate=False tolerates fehlendes Padding (häufigster Browser-Bug),
        # try/except fängt den Rest. Ohne den Fix crasht die HTTP-Verbindung mit
        # leerem 500er-Body und das Frontend zeigt einen Silent-Fail.
        try:
            img_bytes = base64.b64decode(img_b64, validate=False)
        except Exception as e:
            handler._send(400, {"error": f"image ist kein gültiges Base64: {e}"})
            return True, None
        if not img_bytes:
            handler._send(400, {"error": "image dekodiert zu leer"})
            return True, None
        safe = re.sub(r"[^\w\-]", "_", name.lower()) or "character"
        os.makedirs(ch_sheets(cid, upload_vid), exist_ok=True)  # B-1: Auto-Mkdir (frische Kanäle)
        img_path = os.path.join(ch_sheets(cid, upload_vid), f"{safe}.png")
        meta_path = os.path.join(ch_sheets(cid, upload_vid), f"{safe}.json")
        try:
            open(img_path, "wb").write(img_bytes)
        except OSError as e:
            handler._send(500, {"error": f"Schreiben fehlgeschlagen: {e}"})
            return True, None
        try:
            desc = analyze_char_image(img_bytes, mime)
        except Exception as e:
            desc = ""
            print(f"  [Char] Analyse-Fehler: {e}", flush=True)
        # Öffentliche URL erzeugen: das Frontend erwartet `uri` (set_char_ref +
        # _applyCharRef), und die style_ref_url wird als KIE-Bildreferenz benutzt —
        # muss also public-http sein (set_char_ref lehnt Nicht-http-URLs ab).
        # Ohne diesen Upload bekam das Frontend `undefined` → Referenz wurde nie
        # gesetzt (Kern von Bug B-1: Upload "tat nichts").
        try:
            public_uri = upload_image_public(img_path)
        except Exception as e:
            handler._send(502, {"error": f"Public-Upload fehlgeschlagen: {e}"})
            return True, None
        json.dump({"name": name, "description": desc, "safe": safe, "mime": "image/png", "uri": public_uri},
                  open(meta_path, "w"), ensure_ascii=False)
        handler._send(200, {"ok": True, "name": name, "safe": safe, "description": desc, "uri": public_uri})
        return True, None

    if path == "/api/gen_charsheet":
        d = body or {}
        name = d.get("name", "").strip()
        desc = d.get("description", "").strip()
        vid_param = d.get("vid") or None
        if not name or not desc:
            handler._send(400, {"error": "name und description erforderlich"})
            return True, None
        # Juli 2026 (User-Report "doppelte Charsheets pro Charakter"): ein bestehender
        # Charakter (z.B. auto-generiert als char_01) hat schon ein "safe". Wurde bisher
        # IMMER neu aus dem Namen abgeleitet ("Protagonist (You)" -> "protagonist__you_"),
        # erzeugte "Neu generieren" für einen bereits vorhandenen Charakter eine ZWEITE,
        # unabhängige Datei statt die bestehende zu überschreiben — zwei KIE-Aufrufe
        # sehen nie gleich aus (kein Seed). Jetzt: das Frontend schickt das vorhandene
        # "safe" mit (siehe genCharSheet() in dashboard.html); nur wenn keins mitkommt
        # (wirklich neuer, manuell angelegter Charakter) wird aus dem Namen abgeleitet.
        safe = (d.get("safe") or "").strip()
        safe = re.sub(r"[^\w\-]", "_", safe.lower()) if safe else re.sub(r"[^\w\-]", "_", name.lower())
        sheet_dir = ch_sheets(cid, vid_param)
        try:
            img_bytes = gen_charsheet(cid, name, desc, vid=vid_param)
            open(os.path.join(sheet_dir, f"{safe}.png"), "wb").write(img_bytes)
            json.dump({"name": name, "description": desc, "safe": safe, "mime": "image/jpg"},
                      open(os.path.join(sheet_dir, f"{safe}.json"), "w"), ensure_ascii=False)
            handler._send(200, {"ok": True, "name": name, "safe": safe})
        except Exception as e:
            traceback.print_exc()
            handler._send(500, {"error": str(e)})
        return True, None

    if path == "/api/charsheet_update":
        d = body or {}
        safe = re.sub(r"[^\w\-]", "_", (d.get("safe") or "").lower())
        desc = d.get("description", "").strip()
        vid_param = d.get("vid") or None
        if not safe or not desc:
            handler._send(400, {"error": "safe und description erforderlich"})
            return True, None
        sheet_dir = ch_sheets(cid, vid_param)
        meta_path = os.path.join(sheet_dir, f"{safe}.json")
        if not os.path.exists(meta_path):
            handler._send(404, {"error": "Charakter existiert nicht in diesem Video"})
            return True, None
        try:
            meta = json.load(open(meta_path))
            meta["description"] = desc
            meta["updated_at"] = time.time()
            json.dump(meta, open(meta_path, "w"), ensure_ascii=False, indent=1)
            handler._send(200, {"ok": True})
        except Exception as e:
            handler._send(500, {"error": str(e)})
        return True, None

    if path == "/api/charsheet_delete":
        d = body or {}
        safe = re.sub(r"[^\w\-]", "_", (d.get("safe") or "").lower())
        vid_param = d.get("vid") or None
        if not safe:
            handler._send(400, {"error": "safe erforderlich"})
            return True, None
        sheet_dir = ch_sheets(cid, vid_param)
        removed = []
        for ext in (".json", ".png"):
            fp = os.path.join(sheet_dir, f"{safe}{ext}")
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                    removed.append(fp)
                except OSError:
                    pass
        handler._send(200, {"ok": True, "removed": removed})
        return True, None

    return False, None
