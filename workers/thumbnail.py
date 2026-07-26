"""workers/thumbnail.py — Thumbnail-Generierung (aus dashboard.py extrahiert,
Refactor Phase 3). Siehe workers/__init__.py für die lazy-import-Konvention.
"""
from __future__ import annotations

import os
import time
import traceback

from core.paths import v_out
from engine.prompts import (
    composite_thumbnail_text,
    gen_thumbnail_image,
    make_thumbnail_prompt,
    make_thumbnail_text,
)


def run(cid: str, vid: str, full_script: str, master_style: str, chosen_title: str = "") -> None:
    """Läuft im eigenen Thread (siehe dashboard.py-Route, die diesen Worker startet).
    Client pollt /api/thumbnail_status. Die schwere Operation (gen_thumbnail_image)
    macht KIE submit+poll+download und kann 30-60s dauern.

    Juli 2026 (User-Report "Thumbnails sehen schlecht aus, brauchen fetten Text +
    3D-Tiefeneffekt wie [Referenz-Kanal]"): make_thumbnail_text() + composite_thumbnail_text()
    existierten schon in engine/prompts.py, wurden aber nirgends aufgerufen -- das fertige
    KI-Bild ging bisher OHNE jeden Text direkt raus. Jetzt Pflichtschritt nach dem Bild."""
    import dashboard  # lazy, siehe workers/__init__.py

    key = (cid, vid)
    try:
        with dashboard._THUMB_JOBS_LOCK:
            dashboard.THUMB_JOBS[key]["step"] = "Generiere Prompt …"
        print("  [Thumbnail] Generiere Prompt …", flush=True)
        prompt = make_thumbnail_prompt(full_script, master_style)
        print(f"  [Thumbnail] Prompt: {prompt[:120]} …", flush=True)
        with dashboard._THUMB_JOBS_LOCK:
            dashboard.THUMB_JOBS[key]["step"] = "Warte auf Bild-Slot …"
        dashboard.IMAGE_GEN_SEMAPHORE.acquire()
        print("  [Thumbnail] Semaphore erhalten, submitte an KIE …", flush=True)
        with dashboard._THUMB_JOBS_LOCK:
            dashboard.THUMB_JOBS[key]["step"] = "Erzeuge Bild bei KIE …"
        style_ref_urls = dashboard.get_channel_style_refs(cid)
        try:
            res = gen_thumbnail_image(prompt, master_style, os.path.join(v_out(cid, vid), "thumbnail.jpg"),
                                       model=dashboard.get_video_image_model(cid, vid),
                                       ref_urls=style_ref_urls or None)
        finally:
            dashboard.IMAGE_GEN_SEMAPHORE.release()
        if not res["ok"]:
            print(f"  [Thumbnail] Fehler: {res['error']}", flush=True)
            with dashboard._THUMB_JOBS_LOCK:
                dashboard.THUMB_JOBS[key] = {"running": False, "step": "Fehler", "error": res["error"],
                                              "done": False, "file": None, "prompt": None, "ts": time.time()}
            return
        print(f"  [Thumbnail] Bild fertig → {res['file']}", flush=True)

        thumb_text = ""
        with dashboard._THUMB_JOBS_LOCK:
            dashboard.THUMB_JOBS[key]["step"] = "Komponiere Text …"
        try:
            thumb_text = make_thumbnail_text(full_script, chosen_title)
            composite_thumbnail_text(os.path.join(v_out(cid, vid), "thumbnail.jpg"), thumb_text)
            print(f"  [Thumbnail] Text komponiert: \"{thumb_text}\"", flush=True)
        except Exception as e:
            # Text-Compositing ist ein Nice-to-have obendrauf -- ein Fehler hier darf das
            # bereits fertige, gültige KI-Bild nicht verwerfen (graceful degradation).
            traceback.print_exc()
            print(f"  [Thumbnail] Text-Compositing fehlgeschlagen (Bild bleibt ohne Text): {e}", flush=True)

        meta = dashboard.load_v_meta(cid, vid)
        meta["thumbnail_prompt"] = prompt
        meta["thumbnail_text"] = thumb_text
        dashboard.save_v_meta(cid, vid, meta)
        with dashboard._THUMB_JOBS_LOCK:
            dashboard.THUMB_JOBS[key] = {"running": False, "step": "Fertig", "error": None, "done": True,
                                          "file": res["file"], "prompt": prompt, "ts": time.time()}
    except Exception as e:
        traceback.print_exc()
        with dashboard._THUMB_JOBS_LOCK:
            dashboard.THUMB_JOBS[key] = {"running": False, "step": "Fehler", "error": str(e),
                                          "done": False, "file": None, "prompt": None, "ts": time.time()}
