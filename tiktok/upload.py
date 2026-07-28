"""TikTok Upload Module"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import store.db as db
from tiktok.oauth import refresh_if_needed

# Juli 2026 (User-Entscheidung): TikTok-Auto-Upload abgeschaltet -- Nutzer plant Posts
# künftig manuell über die TikTok-App auf dem Laptop. Zentraler Schalter statt Löschen
# (CLAUDE.md-Konvention: hidden-not-deleted). process_one() prüft dies zuerst als
# Absicherung; youtube/upload.py:worker_loop() und youtube/api.py:queue_add() prüfen es
# zusätzlich VOR dem Dispatch/Queueing, damit die Queue nicht mit nie verarbeiteten
# TikTok-Einträgen vollläuft. Wieder auf True stellen, um die Pipeline zu reaktivieren.
AUTO_UPLOAD_ENABLED = False


def _query_creator_info(access_token: str) -> dict:
    """POST .../creator_info/query/ — liefert u.a. privacy_level_options, die
    für DIESEN Account/App-Status tatsächlich erlaubten Werte (unaudited nur
    SELF_ONLY). TikToks Doku verlangt, dass der gesendete privacy_level aus
    genau dieser Liste stammt -- frisch pro Post abgefragt statt hart codiert,
    damit der Code nach einem bestandenen Audit ohne Änderung weiterläuft."""
    req = urllib.request.Request(
        "https://open.tiktokapis.com/v2/post/publish/creator_info/query/",
        data=b"{}",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        res = json.loads(resp.read().decode("utf-8"))
    if res.get("error", {}).get("code") != "ok":
        raise ValueError(f"creator_info/query Error: {res}")
    return res["data"]


def _build_caption(entry: dict) -> str:
    """TikTok hat ein einziges Caption-Feld ('title', bis 2200 Zeichen) --
    Titel, Beschreibung und Hashtags werden zusammengeführt. Reine Funktion,
    isoliert testbar ohne Netzwerk/DB."""
    tiktok_caption = entry["title"] or "Tiktok Short"
    if entry["description"]:
        tiktok_caption += f"\n\n{entry['description']}"
    if entry["tags"]:
        try:
            tags = json.loads(entry["tags"])
            if tags:
                hashtags = " ".join([f"#{t.replace(' ', '')}" for t in tags])
                tiktok_caption += f"\n\n{hashtags}"
        except Exception:
            pass
    return tiktok_caption[:2150]


def _build_init_body(title: str, file_size: int, privacy_level_options: list) -> dict:
    """Request-Body für /v2/post/publish/video/init/ (Direct Post). Reine
    Funktion, isoliert testbar ohne Netzwerk.

    privacy_level MUSS laut TikToks Doku einer der von creator_info/query
    tatsächlich für diesen Account/App-Status erlaubten Werte sein (unaudited
    typischerweise nur SELF_ONLY) -- nicht hart codiert, damit der Code nach
    einem bestandenen Audit ohne Änderung öffentlich posten kann. Erster Wert
    aus der Liste als Default; SELF_ONLY als Fallback falls die Liste leer/
    unerwartet ist (sicherster Default statt eines Fehlers).

    is_aigc=True (empfohlen, nicht optional weggelassen): TikToks Content-
    Sharing-Guidelines verlangen explizite Kennzeichnung von KI-generiertem
    Content -- bei dieser Pipeline ist buchstäblich alles KI-generiert."""
    privacy_level = privacy_level_options[0] if privacy_level_options else "SELF_ONLY"
    return {
        "post_info": {
            "title": title,
            "privacy_level": privacy_level,
            "is_aigc": True,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": file_size,
            "chunk_size": file_size,
            "total_chunk_count": 1,
        },
    }


def _init_direct_post(access_token: str, title: str, file_size: int,
                       privacy_level_options: list) -> dict:
    """POST .../video/init/ (Direct Post). Real getestet (Juli 2026, kein
    Doku-Fund): TikToks privacy_level_options filtert NICHT nach App-Audit-
    Status -- listet, was der ACCOUNT theoretisch erlaubt (z.B.
    PUBLIC_TO_EVERYONE), nicht was ein unaudited App tatsächlich posten darf.
    Der eigentliche Fehler kommt erst beim Init-Call selbst: HTTP 403
    unaudited_client_can_only_post_to_private_accounts. Statt hart zu
    scheitern: einmaliger Retry mit privacy_level=SELF_ONLY erzwungen. Sobald
    der Audit besteht, akzeptiert TikTok den ersten Versuch direkt -- keine
    Codeänderung nötig, das ist der ganze Sinn von _build_init_body()'s
    "erste verfügbare Option zuerst"-Logik."""
    def _try(privacy_level: str) -> dict:
        body = _build_init_body(title, file_size, [privacy_level])
        req = urllib.request.Request(
            "https://open.tiktokapis.com/v2/post/publish/video/init/",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))

    privacy_level = privacy_level_options[0] if privacy_level_options else "SELF_ONLY"
    try:
        res = _try(privacy_level)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        if (e.code == 403 and "unaudited_client_can_only_post_to_private_accounts" in err_body
                and privacy_level != "SELF_ONLY"):
            print("  [TikTok Upload] App unaudited -- Retry mit privacy_level=SELF_ONLY", flush=True)
            res = _try("SELF_ONLY")
        else:
            raise ValueError(f"Init Error: HTTP {e.code}: {err_body}")
    if res.get("error", {}).get("code") != "ok":
        raise ValueError(f"Init Error: {res}")
    return res["data"]


def process_one(entry: dict) -> None:
    if not AUTO_UPLOAD_ENABLED:
        return
    if entry["status"] not in ("queued", "uploading"):
        return

    # Check scheduling
    if time.time() < entry["scheduled_at"]:
        return

    cid = entry["cid"]
    access_token = refresh_if_needed(cid)
    
    file_path = entry["file_path"]
    if not os.path.exists(file_path):
        db.queue_update(entry["id"], status="failed", error="Datei nicht gefunden")
        return

    file_size = os.path.getsize(file_path)
    
    db.queue_update(entry["id"], status="uploading")

    try:
        tiktok_caption = _build_caption(entry)

        # Direct Post statt Inbox/Draft (Juli 2026, User-Wunsch "wie bei
        # YouTube, 0 Gedanken machen müssen") -- Inbox postete nur als Entwurf,
        # der Nutzer musste in der TikTok-App manuell antippen. Direct Post
        # verlangt privacy_level aus den für DIESEN Account tatsächlich
        # erlaubten Optionen; _init_direct_post() fällt zusätzlich auf
        # SELF_ONLY zurück, falls der App-Audit-Status das erzwingt (siehe
        # dessen Docstring).
        creator_info = _query_creator_info(access_token)
        init_data = _init_direct_post(access_token, tiktok_caption, file_size,
                                       creator_info.get("privacy_level_options", []))

        upload_url = init_data["upload_url"]
        publish_id = init_data["publish_id"]

        # 2. Upload file
        with open(file_path, "rb") as f:
            upload_req = urllib.request.Request(
                upload_url,
                data=f.read(),
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Range": f"bytes 0-{file_size-1}/{file_size}"
                },
                method="PUT"
            )
            with urllib.request.urlopen(upload_req) as resp:
                resp.read() # read response
                
        # 3. Mark as uploaded
        db.queue_update(entry["id"], status="uploaded", youtube_video_id=f"tiktok_{publish_id}")
        print(f"  [TikTok Upload] Queue #{entry['id']} erfolgreich: {publish_id}")
        
    except Exception as e:
        # status="failed" statt nur error/attempts zu schreiben (Fix Duplikat-
        # Uploads, Juli 2026): TikTok hat kein Resumable-Upload-Äquivalent zu
        # YouTube (jeder Init-Call fordert eine neue publish_id an) -- ein
        # Eintrag, der auf "uploading" hängen bleibt, wurde vom 5s-Poll-Loop
        # (queue_pending()) immer wieder aufgegriffen und postete denselben
        # Clip bei jedem Fehlschlag NACH erfolgreichem TikTok-Empfang erneut.
        # Ein automatischer Retry ist hier nie sicher -- manueller Retry über
        # die UI (nutzt den deduplizierenden queue_add()) bleibt möglich.
        db.queue_update(entry["id"], status="failed",
                         attempts=entry.get("attempts", 0) + 1, error=str(e))
        print(f"  [TikTok Upload] Queue #{entry['id']}: Fehler: {e}", flush=True)
