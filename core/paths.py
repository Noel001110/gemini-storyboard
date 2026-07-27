"""core/paths.py — reine Pfad-Helfer für channels/<cid>/videos/<vid>/... .

Ausschließlich Pfad-Konstruktion (os.path.join), keine I/O. Ursprünglich in
dashboard.py definiert; hierher verschoben (Refactor Phase 1), weil
shorts/api.py, youtube/upload.py und control/api.py sie bisher nur über ein
lazy `import dashboard` erreichen konnten (God-Modul-Kopplung, siehe
core/__init__.py). dashboard.py re-exportiert alles hier unverändert weiter
(`from core.paths import *`) -- kein bestehender Call-Site bricht.
"""
from __future__ import annotations

import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANNELS_DIR = os.path.join(HERE, "channels")
CHANNELS_FILE = os.path.join(CHANNELS_DIR, "channels.json")

# Juli 2026 (Nutzerwunsch "Shorts in einen extra Ordner, nach Video sortiert, zum
# Weiterleiten"): eigener Top-Level-Ordner (Geschwister von channels/), NICHT unter
# channels/<cid>/videos/<vid>/generated/ -- dort liegen die fertigen Shorts sonst
# vermischt mit 70+ Szenenbildern, render_tmp/ etc., unpraktisch zum schnellen Finden.
SHORTS_EXPORT_DIR = os.path.join(HERE, "shorts_export")


# ── Per-channel path helpers (channel = brand/style, holds N videos) ──────────
def ch_dir(cid: str) -> str:
    return os.path.join(CHANNELS_DIR, cid)


def ch_master(cid: str) -> str:
    return os.path.join(ch_dir(cid), "master_prompt.txt")


def ch_vid_master(cid: str) -> str:
    return os.path.join(ch_dir(cid), "video_master_prompt.txt")


def ch_sheets(cid: str, vid: str | None = None) -> str:
    # Juli 2026: charsheets sind jetzt pro Video (channels/<cid>/videos/<vid>/
    # charsheets/), damit unabhängige Videos sich nicht kontaminieren können
    # (z.B. Theranos-Skript sieht Jamal-Khashoggi-Charsheets). Ohne vid: Fallback
    # auf den alten Kanal-globalen Pool (Abwärtskompat mit alten Daten/Call-Sites).
    if vid:
        return os.path.join(v_dir(cid, vid), "charsheets")
    return os.path.join(ch_dir(cid), "charsheets")


def ch_videos_file(cid: str) -> str:
    return os.path.join(ch_dir(cid), "videos.json")


def ch_voice_id(cid: str) -> str:
    return os.path.join(ch_dir(cid), "voice_id.txt")


def ch_voice_settings(cid: str) -> str:
    return os.path.join(ch_dir(cid), "voice_settings.json")


def ch_youtube_playlist_id(cid: str) -> str:
    return os.path.join(ch_dir(cid), "youtube_playlist_id.txt")


# ── Per-video path helpers (one video = one script/plan/generated set) ────────
def v_dir(cid: str, vid: str) -> str:
    return os.path.join(ch_dir(cid), "videos", vid)


def v_out(cid: str, vid: str) -> str:
    return os.path.join(v_dir(cid, vid), "generated")


def v_plan(cid: str, vid: str) -> str:
    return os.path.join(v_out(cid, vid), "plan.json")


def v_analysis_cache(cid: str, vid: str) -> str:
    # Cache für analyze_script()'s Ergebnis, keyed by Skript-Text-Hash (siehe
    # workers/plan.py) -- analyze_script ist ein LLM-Call und daher nicht
    # deterministisch; ohne Cache konnte ein Re-Plan mit UNVERÄNDERTEM Text
    # trotzdem leicht andere Szenengrenzen erzeugen und dadurch bereits
    # gerenderte Bilder grundlos verwerfen (siehe _preserve_rendered_scenes).
    return os.path.join(v_out(cid, vid), "analysis_cache.json")


def v_uploads(cid: str, vid: str) -> str:
    return os.path.join(v_dir(cid, vid), "uploads")


def v_audio(cid: str, vid: str) -> str:
    return os.path.join(v_uploads(cid, vid), "audio_meta.json")


def v_meta(cid: str, vid: str) -> str:
    return os.path.join(v_dir(cid, vid), "meta.json")  # titles, thumbnail prompt


def v_script(cid: str, vid: str) -> str:
    return os.path.join(v_dir(cid, vid), "script.json")  # raw narration, survives sessions


def v_render_tmp(cid: str, vid: str) -> str:
    # Bewusst getrennt von v_out()/generated/ -- der Render-Worker löscht dieses
    # Verzeichnis nach erfolgreichem Render per rmtree() und darf dabei niemals
    # den Ordner mit den echten generierten Bildern/Videos erreichen können.
    return os.path.join(v_dir(cid, vid), "render_tmp")


def shorts_export_dir(cid: str, vid: str) -> str:
    return os.path.join(SHORTS_EXPORT_DIR, cid, vid)
