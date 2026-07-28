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


def process_one(entry: dict) -> None:
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
        # erlaubten Optionen (siehe _query_creator_info/_build_init_body).
        creator_info = _query_creator_info(access_token)
        init_body = _build_init_body(tiktok_caption, file_size,
                                      creator_info.get("privacy_level_options", []))

        # 1. Initialize upload (Direct Post -- postet wirklich, kein Entwurf)
        init_req = urllib.request.Request(
            "https://open.tiktokapis.com/v2/post/publish/video/init/",
            data=json.dumps(init_body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8"
            },
            method="POST"
        )

        with urllib.request.urlopen(init_req) as resp:
            init_res = json.loads(resp.read().decode("utf-8"))
            
        if init_res.get("error", {}).get("code") != "ok":
            raise ValueError(f"Init Error: {init_res}")
            
        upload_url = init_res["data"]["upload_url"]
        publish_id = init_res["data"]["publish_id"]
        
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
