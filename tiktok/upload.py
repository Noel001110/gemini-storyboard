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
        # TikTok has a single caption field ('title') up to 2200 chars. 
        # We concatenate title, description and hashtags.
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
        
        # truncate to safe limit
        tiktok_caption = tiktok_caption[:2150]

        # 1. Initialize upload (Inbox API flow for Sandbox & Public Accounts)
        init_req = urllib.request.Request(
            "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/",
            data=json.dumps({
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": file_size,
                    "chunk_size": file_size,
                    "total_chunk_count": 1
                }
            }).encode("utf-8"),
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
        db.queue_update(entry["id"], attempts=entry.get("attempts", 0) + 1, error=str(e))
        print(f"  [TikTok Upload] Queue #{entry['id']}: Fehler: {e}", flush=True)
