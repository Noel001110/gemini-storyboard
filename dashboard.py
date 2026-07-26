#!/usr/bin/env python3
"""Localhost-Dashboard für die Storyboard-Bildgenerierung.
Nur Python-Standardlib. Start: python3 dashboard.py [--port 8010]
"""
import os, re, sys, json, time, base64, zipfile, io, threading, concurrent.futures
import urllib.request, urllib.error, subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import shutil
from urllib.parse import urlparse, parse_qs

# ── Shorts/Upload/Control-Erweiterung: Prefix-Dispatch (siehe routes/__init__.py) ──
from routes import dispatch, mount

# ── Refactor Phase 1: core/paths.py ist jetzt die Quelle für HERE/CHANNELS_DIR/
# CHANNELS_FILE + alle reinen Pfad-Helfer (ch_dir/v_out/...). Re-Export hier hält
# jeden bestehenden Call-Site (`dashboard.HERE`, `dashboard.v_out(...)`, die lazy
# `import dashboard` in shorts/api.py etc.) unverändert lauffähig.
from core.paths import (  # noqa: F401
    HERE, CHANNELS_DIR, CHANNELS_FILE, SHORTS_EXPORT_DIR,
    ch_dir, ch_master, ch_vid_master, ch_sheets, ch_videos_file,
    ch_voice_id, ch_voice_settings, ch_youtube_playlist_id,
    v_dir, v_out, v_plan, v_uploads, v_audio, v_meta, v_script, v_render_tmp,
    shorts_export_dir,
)

# ── Phase 2.1 (Schwachstellenbericht #6/#7/#36/#60/#68): Atomare Schreibvorgänge ──
# Schreibvorgänge auf channels.json, plan.json, videos.json, audio_meta.json u.a.
# müssen atomar sein — ein Crash mitten im Write darf die Datei nicht zerstören.
# Standard-Pattern:
#   1. tmp-Datei in GLEICHEM Verzeichnis (gleiches Filesystem → atomic rename)
#   2. fsync() — Inhalt ist auf Disk bevor wir umbenennen
#   3. os.replace() — atomar (POSIX garantiert), ersetzt Ziel in einem Schritt
# Für Listen/Dicts: indent=1 macht die Dateien lesbar, ohne indent für kompakte Saves.

def _atomic_write_json(path: str, data, ensure_ascii: bool = False, indent=None) -> None:
    """Atomar JSON schreiben: tmp-Datei → fsync → os.replace.

    Garantiert dass `path` immer entweder die alte oder die neue vollständige Version
    enthält — nie eine halbe Datei. Vermeidet Korruption bei Crash/Disk-Full/Power-Loss.

    Atomare Garantie nur innerhalb des gleichen Dateisystems (gleiche Partition) —
    os.replace ist nur dann atomar, wenn tmp-Datei und Ziel-Pfad auf derselben
    Partition liegen. Wir wählen tmp-Pfad daher explizit neben der Zieldatei.
    """
    path = str(path)
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    # Eindeutiger tmp-Name (verhindert Kollision bei parallelen Writes)
    fd, tmp_path = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=ensure_ascii, indent=indent)
            f.flush()
            os.fsync(f.fileno())  # erzwinge Write-to-Disk vor dem rename
        os.replace(tmp_path, path)  # atomar auf POSIX-Dateisystemen
    except BaseException:
        # Bei jedem Fehler (Crash, Disk-Full, Permission-Denied): tmp-Datei aufräumen
        try:    os.unlink(tmp_path)
        except OSError: pass
        raise

import tempfile  # für _atomic_write_json (Phase 2.1)
import signal   # für SIGTERM-Handler (Phase 2.1 #68)

# Graceful Shutdown: alle offenen Schreibvorgänge abschließen + Jobs stoppen.
# Schwachstelle #68: ohne SIGTERM-Handler werden Hintergrund-Worker bei Container-Stop
# mitten im Render abgebrochen — plan.json kann korrupt sein.
_SHUTDOWN_IN_PROGRESS = False
_SERVER_REF = None  # von main() gesetzt, sobald der ThreadingHTTPServer existiert
_SHUTDOWN_GRACE_PERIOD_S = 8.0

def _request_stop_all_running_jobs() -> list:
    """Setzt stop_requested=True auf jedem laufenden BATCH_/RENDER_/PRODUCE_JOBS-Eintrag
    -- exakt derselbe kooperative Abbruch-Mechanismus wie der bestehende "Stoppen"-Button
    (siehe stop_requested-Checks in _batch_generate_worker/_render_worker/_produce_worker).
    Gibt die betroffenen (kind, key)-Paare zurück, damit der Aufrufer auf ihr Ende warten kann."""
    affected = []
    with _BATCH_JOBS_LOCK:
        for key, entry in BATCH_JOBS.items():
            if entry.get("running"):
                entry["stop_requested"] = True
                affected.append(("batch", key))
    with _RENDER_JOBS_LOCK:
        for key, entry in RENDER_JOBS.items():
            if entry.get("running"):
                entry["stop_requested"] = True
                affected.append(("render", key))
    with _PRODUCE_JOBS_LOCK:
        for key, entry in PRODUCE_JOBS.items():
            if entry.get("running"):
                entry["stop_requested"] = True
                affected.append(("produce", key))
    return affected

def _shutdown_worker() -> None:
    """Läuft in einem eigenen Thread (NIE im Signal-Handler selbst, sonst deadlockt
    srv.shutdown() -- es muss aus einem ANDEREN Thread als dem serve_forever()-Loop
    aufgerufen werden). Gibt laufenden Batch-/Render-/Produce-Jobs eine begrenzte
    Gnadenfrist, ihren stop_requested-Checkpoint zu erreichen, bevor der Prozess
    endet -- die Worker-Threads sind daemon=True und würden beim Prozessende sonst
    ohne jede Rücksicht mitten in ihrer aktuellen Iteration abgewürgt."""
    affected = _request_stop_all_running_jobs()
    if affected:
        _log("INFO", "shutdown_waiting_for_jobs", count=len(affected))
        deadline = time.time() + _SHUTDOWN_GRACE_PERIOD_S
        registries = {"batch": (BATCH_JOBS, _BATCH_JOBS_LOCK),
                      "render": (RENDER_JOBS, _RENDER_JOBS_LOCK),
                      "produce": (PRODUCE_JOBS, _PRODUCE_JOBS_LOCK)}
        still_running = affected
        while time.time() < deadline:
            still_running = []
            for kind, key in affected:
                jobs, lock = registries[kind]
                with lock:
                    if jobs.get(key, {}).get("running"):
                        still_running.append((kind, key))
            if not still_running:
                break
            time.sleep(0.3)
        else:
            _log("WARN", "shutdown_grace_period_exceeded", still_running=len(still_running))
    if _SERVER_REF is not None:
        _SERVER_REF.shutdown()

def _graceful_shutdown(signum, frame):
    """SIGTERM/SIGINT-Handler: setzt das Flag (für /api/health), stößt einen
    begrenzten kooperativen Stop laufender Jobs an und beendet danach wirklich den
    Server -- vorher setzte dieser Handler NUR das Flag, srv.serve_forever() lief
    unbegrenzt weiter (empirisch verifiziert: SIGTERM/Ctrl-C beendete den Prozess
    NIE). Der eigentliche Stop läuft in einem eigenen Thread, siehe _shutdown_worker().
    """
    global _SHUTDOWN_IN_PROGRESS
    if _SHUTDOWN_IN_PROGRESS:  # Doppelte Signale ignorieren
        return
    _SHUTDOWN_IN_PROGRESS = True
    _log("INFO", "shutdown_signal", signal=signum)
    threading.Thread(target=_shutdown_worker, daemon=True).start()

try:
    signal.signal(signal.SIGTERM, _graceful_shutdown)  # Container-Stop
    signal.signal(signal.SIGINT, _graceful_shutdown)   # Ctrl-C
except ValueError as e:
    # signal.signal() crasht wenn nicht im Main-Thread (z.B. wenn das dashboard-Modul
    # von einem HTTP-Handler-Thread re-importiert wird über zirkuläre Imports). In dem
    # Fall ist der Handler in einem Worker-Thread sowieso nutzlos — wir setzen nur
    # _SHUTDOWN_IN_PROGRESS = False (default) und loggen.
    _SHUTDOWN_IN_PROGRESS = False   # Ctrl-C

# ── Phase J: Engine-Refactor — engine_*.py modules ─────────────────────────────
# ElevenLabs-Integration (Phase 1) + Phase-Engine-Constants (Phasen B-H-I) leben in
# engine_elevenlabs.py. Wildcard-Import für backward-compat: alle bisher direkt
# referenzierten Namen (elevenlabs_key, _elevenlabs_persist_and_schedule, etc.)
# bleiben global erreichbar.
from engine_elevenlabs import *  # noqa: F401,F403

# ── Phase M.2: Szenen-Segmentierung + Sequenz-Ketten nach engine/scenes.py ─────
# Re-Export für Rückwärtskompatibilität. Aufrufer wie `_batch_generate_worker`
# referenzieren weiterhin `dashboard._resolve_chain_refs` etc. — die Wildcard
# hier hält den alten Code lauffähig, ohne dass ich 200+ Zeilen patchen muss.
from engine.scenes import (  # noqa: F401,F403
    MAX_SCENE_SEC, PACING_TARGET_SEC, NORMAL_HARD_CAP_SEC, PACING_WARN_THRESHOLD,
    PacingProfile, PACING_PROFILES,
    ACCENT_PAUSE_THRESHOLD_SEC, ACCENT_MIN_SCENE_DUR_SEC,
    split_units, segment_by_pacing, _renumber_seq_pos, _apply_visual_sequences_direct,
    _wait_for_chain_scene, _resolve_chain_refs,
    _wait_for_entity_anchor_scene, _resolve_entity_ref, _find_charsheet_png,
    _is_accent_eligible, _compute_accent_t,
)

# ── Phase M.3: Visuelle Render-Pipeline nach engine/render.py ──────────────────
# Re-Export für Rückwärtskompatibilität. Aufrufer wie _render_worker und die API-
# Handler referenzieren weiterhin `dashboard._render_clip`, `dashboard.RENDER_FPS`, etc.
from engine.render import (  # noqa: F401,F403
    RENDER_FPS, RENDER_WIDTH, RENDER_HEIGHT, RENDER_SUPERSAMPLE_WIDTH,
    RenderTarget, RENDER_TARGETS,
    MOTION_LIBRARY, _PACING_MOTION_CANDIDATES, _PHASE_MOTION_CANDIDATES, TRANSITION_LIBRARY,
    _probe_video_encoder, _apply_sync_invariant,
    _build_motion, _normalize_motion, _motion_for_scene, _overlay_specs_for_scene,
    _render_clip, _assemble_clips, _mux_audio, _render_selfcheck,
    _transition_for_scene, _transition_after_hook, _has_transition_before,
    _clip_duration_sec, _crossfade_clips,
    render_text_overlay_png, render_title_card_png_via_venv,
)

# ── Phase M.4: Audio-Pipeline nach engine/audio.py ─────────────────────────────
# Re-Export für Rückwärtskompatibilität. Aufrufer wie _render_worker und die
# _build_final_audio-Aufruf-Stelle referenzieren weiterhin `dashboard._build_sfx_events`,
# `dashboard.MUSIC_BED_FILE`, etc.
from engine.audio import (  # noqa: F401,F403
    SOUND_ASSETS_DIR, MUSIC_BED_FILE, SFX_FILES,
    MUSIC_BEDS, PHASE_TO_TIER,
    _build_sfx_events, _duck_music_under_voice, _place_sfx,
    _phase_modulate_music, _build_music_track, _build_final_audio,
)

# ── Phase M.5: Prompt-Komposition + Char-Sheet-Pipeline nach engine/prompts.py ──
# Re-Export für Rückwärtskompatibilität. _build_image_prompt wird z.B. in
# _batch_generate_worker und _veo_job_worker aufgerufen.
from engine.prompts import (  # noqa: F401,F403
    IMAGE_PROMPT_CHUNK_SIZE, IMAGE_PROMPT_MIN_LEN,
    SCRIPT_SYSTEM, TITLE_SYSTEM, THUMBNAIL_PROMPT_SYSTEM,
    HOOK_PROMPT_ADDITION,
    _build_image_prompt, _build_video_prompt,
    load_char_refs, analyze_char_image, gen_charsheet,
    _anonymized_words, _validate_image_prompt_entry,
    _image_prompt_chunk, _image_prompt_single_retry,
    visual_prompts,
    generate_script, generate_titles,
    make_thumbnail_prompt, gen_thumbnail_image,
)

# ── Phase Q + 38: Stil-Presets nach engine/presets.py ─────────────────────────
# Re-Export. IMAGE_MASTER_DEFAULT ist jetzt = PRESET_MASTERS[DEFAULT_PRESET]
# (= "flat_cartoon_doc"), nicht mehr der karge Stick-Figure-Platzhalter.
# Bestehende Kanäle behalten ihren master_prompt.txt — keine Migration nötig.
from engine.presets import (  # noqa: F401,F403
    PRESET_MASTERS, PRESET_DESCRIPTIONS, DEFAULT_PRESET,
    IMAGE_MASTER_DEFAULT, VIDEO_MASTER_DEFAULT,
)

# ── Evaluation Juli 2026, Änderung 1: Bild-Provider nach engine/imagegen.py ────
# Re-Export. Der KIE-Client lebte vorher hier im Monolithen; engine/prompts.py und
# engine/scenes.py griffen deshalb zirkulär auf dashboard.py zurück. Verhalten
# unverändert -- reines Verschieben, kein neues Feature (siehe engine/imagegen.py
# Modul-Docstring für die volle Begründung).
from engine.imagegen import (  # noqa: F401,F403
    KIE_KEY_FILE, KIE_API, VALID_IMAGE_MODELS, kie_key,
    KIE_SUBMIT_RATE_LIMIT, KIE_SUBMIT_RATE_WINDOW,
    KIE_FAILURE_WINDOW_S, KIE_FAILURES_THRESHOLD, KIE_CIRCUIT_OPEN_DURATION_S,
    _kie_rate_limit_wait, _kie_circuit_status, _kie_record_failure,
    _kie_record_success, _kie_retry_with_backoff, _kie_submit_image,
    KIE_UPLOAD_URL, _multipart_upload, get_public_charsheet_url, upload_image_public,
    _CHARSHEET_UPLOAD_CACHE, _CHARSHEET_UPLOAD_LOCK,
    generate_image,
)

# ── Background job tracking ───────────────────────────────────────────────────
# {job_id: {status:"running"|"done"|"error", progress:0-100, file, source_url, error}}
JOBS: dict = {}

# Guards against duplicate generation for the same scene — e.g. two browser tabs both
# running "Alle Bilder generieren", or a double-click before the button disables.
# {(cid, vid, scene_i): job_id} — only present while that scene's job is still running.
ACTIVE_SCENE_JOBS: dict = {}
_ACTIVE_SCENE_JOBS_LOCK = threading.Lock()

# Guards every read-modify-write of a plan.json file. With concurrent scene generation
# (multiple scenes finishing at nearly the same moment) two threads doing bare
# "read plan.json -> modify one scene -> write plan.json" without a lock can race: thread
# B reads its snapshot before thread A's write lands, then B's write overwrites A's
# update with a stale copy that doesn't have A's scene marked done — the image is
# correctly generated and downloaded to disk, but the plan.json entry for it silently
# reverts to "not done", making that scene look skipped/never generated. One process-wide
# lock is enough here since each read+write is a few milliseconds against a small file.
_PLAN_WRITE_LOCK = threading.Lock()

# GLOBAL cap on concurrent KIE image generations, regardless of source. Per KIE's actual
# documented limits: up to 20 new task submissions per 10s, generally supporting 100+
# concurrently RUNNING tasks account-wide. 8 is a comfortable margin under both — fast
# enough to meaningfully parallelize "Alle Bilder generieren" (was previously fully
# sequential, one scene at a time, which was far more conservative than KIE actually
# requires) while leaving headroom for individual clicks and thumbnails on top.
MAX_CONCURRENT_IMAGE_GENS = 8
IMAGE_GEN_SEMAPHORE = threading.Semaphore(MAX_CONCURRENT_IMAGE_GENS)

# "Alle Bilder generieren" runs server-side now, not driven by the browser tab — it
# survives page reloads and tab closes, and multiple browser sessions just observe the
# same run instead of each starting their own (which was the actual root cause of the
# repeated duplicate-generation bugs: the old client-driven loop died on every reload,
# and a second click/tab created a second independent loop with its own stale scene list).
# {(cid, vid): {"running": bool, "stop_requested": bool, "done": int, "total": int,
#               "current_i": list[int] (scene indices currently in flight), "error": str|None}}
BATCH_JOBS: dict = {}
_BATCH_JOBS_LOCK = threading.Lock()

# Script -> plan (analysis + prompt-chunk generation) also runs server-side for the same
# reason as BATCH_JOBS above: it used to be a single blocking HTTP request, so closing
# the tab mid-generation looked like nothing happened, and re-clicking "Plan erstellen"
# started a second, fully independent LLM run on the same script — observed live
# duplicating every analysis + chunk call for a 167-scene script.
# {(cid, vid): {"running": bool, "step": str, "error": str|None, "done": bool}}
PLAN_JOBS: dict = {}
_PLAN_JOBS_LOCK = threading.Lock()

# Thumbnail generation — same server-side-job pattern as PLAN_JOBS above. Used to be a
# fully synchronous HTTP request (make_thumbnail_prompt + gen_thumbnail_image incl. KIE
# submit+poll+download, 30-60s inline), which froze the browser with a spinner and no
# progress. Now a worker thread does the work while the client polls /api/thumbnail_status.
# {(cid, vid): {"running": bool, "step": str, "error": str|None, "done": bool,
#               "file": str|None, "prompt": str|None, "ts": float}}
THUMB_JOBS: dict = {}
_THUMB_JOBS_LOCK = threading.Lock()

# Auto-rendering (images -> Ken Burns clips -> concat -> audio mux -> final.mp4) —
# same server-side-job pattern as BATCH_JOBS/PLAN_JOBS above, for the same reason:
# survives reloads, a second start call is refused while one is already running.
# {(cid, vid): {"running": bool, "stop_requested": bool, "stage": str, "done": int,
#               "total": int, "error": str|None, "file": str|None}}
RENDER_JOBS: dict = {}
_RENDER_JOBS_LOCK = threading.Lock()

# ElevenLabs voiceover job tracking — same pattern as BATCH/RENDER/PLAN/PRODUCE above,
# keyed by (cid, vid) because the voiceover's output (audio_meta.json + word timestamps)
# is per-video. The actual long-running work (_elevenlabs_persist_and_schedule) reuses
# the existing _produce_worker orchestrator after persisting audio + meta, so this dict
# only carries the ElevenLabs-specific request/response state (settings used, chars,
# task_id, resume flag) for the polling channel. After dispatch the orchestrator's
# PRODUCE_JOBS becomes the authoritative progress source.
# {(cid, vid): {"running": bool, "stage": str, "error": str|None,
#               "voiceover_source": "elevenlabs", "voiceover_task_id": str|None,
#               "voiceover_chars": int|None, "ts": float, "resume": bool}}
VOICE_JOBS: dict = {}
_VOICE_JOBS_LOCK = threading.Lock()

# Every one of the job dicts above is only ever ADDED to, never proactively pruned —
# a long-lived server process accumulates one entry per image/veo/batch/plan/render job
# forever. JOBS is the worst offender (one entry per scene per click, unlike the other
# four which are capped at one entry per (cid,vid)). _cleanup_stale_jobs() runs on a
# 30-minute daemon and removes only entries that are BOTH finished AND older than
# MAX_AGE_JOBS_HOURS — an entry still running must never be deleted, or the client's
# polling loop would silently orphan (poll a job_id the server has forgotten about).
MAX_AGE_JOBS_HOURS = 2.0

# Refactor Phase 1: die _log()-Implementierung (Phase 3.4, #40 — strukturiertes
# JSON-Logging per LOG_JSON=1, sonst menschenlesbares key=value-Format) lebt jetzt
# in core/logging.py, damit auch engine/routes/workers sie ohne `import dashboard`
# erreichen können. Re-Export hält die 3 bestehenden _log(...)-Call-Sites unverändert.
from core.logging import log_event as _log  # noqa: F401

# Phase 3.4: Health-Endpoint braucht Server-Uptime und Git-Commit (für Monitoring)
_START_TIME = time.time()
def _get_git_commit() -> str:
    """Bestimmt den aktuellen Git-Commit-Hash (für /health-Endpoint-Version-Feld).
    Gibt '' zurück wenn nicht in einem Git-Repo oder git nicht verfügbar."""
    try:
        import subprocess as _sp
        return _sp.check_output(["git", "rev-parse", "HEAD"],
                              cwd=os.path.dirname(os.path.abspath(__file__)),
                              stderr=_sp.DEVNULL, text=True).strip()
    except Exception:
        return ""
_CURRENT_GIT_COMMIT = _get_git_commit()

def _cleanup_stale_jobs(max_age_hours: float = MAX_AGE_JOBS_HOURS):
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    # JOBS has no "running" bool — its schema is {"status": "running"|"done"|"error", ...}.
    with _ACTIVE_SCENE_JOBS_LOCK:
        for job_id in list(JOBS.keys()):
            entry = JOBS[job_id]
            if entry.get("status") == "running":
                continue
            if entry.get("ts") and entry["ts"] < cutoff:
                del JOBS[job_id]
                removed += 1
    for d, lock in ((BATCH_JOBS, _BATCH_JOBS_LOCK), (PLAN_JOBS, _PLAN_JOBS_LOCK),
                    (THUMB_JOBS, _THUMB_JOBS_LOCK),
                    (RENDER_JOBS, _RENDER_JOBS_LOCK), (PRODUCE_JOBS, _PRODUCE_JOBS_LOCK),
                    (VOICE_JOBS, _VOICE_JOBS_LOCK)):
        with lock:
            for key in list(d.keys()):
                entry = d[key]
                if entry.get("running"):
                    continue
                if entry.get("ts") and entry["ts"] < cutoff:
                    del d[key]
                    removed += 1
    if removed:
        print(f"  [Cleanup] {removed} veraltete Job-Einträge entfernt (>{max_age_hours}h, abgeschlossen)", flush=True)


def _cleanup_stale_render_tmp(max_age_hours: float = 2.0):
    """Schwäche #69: Render-Temp-Dirs können bei Crash/Disk-Full zurückbleiben.
    Beim Server-Start alte render_tmp/-Dirs aufräumen die älter als max_age_hours sind.
    """
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    freed_bytes = 0
    if not os.path.isdir(CHANNELS_DIR):
        return
    for cid in os.listdir(CHANNELS_DIR):
        ch_path = os.path.join(CHANNELS_DIR, cid, "videos")
        if not os.path.isdir(ch_path):
            continue
        for vid in os.listdir(ch_path):
            tmp_dir = os.path.join(ch_path, vid, "render_tmp")
            if not os.path.isdir(tmp_dir):
                continue
            try:
                _mtimes = [os.path.getmtime(os.path.join(tmp_dir, f)) for f in os.listdir(tmp_dir)]
                mtime = max(_mtimes) if _mtimes else os.path.getmtime(tmp_dir)
            except OSError:
                continue
            if mtime < cutoff:
                _size = 0
                for _f in os.listdir(tmp_dir):
                    _fp = os.path.join(tmp_dir, _f)
                    if os.path.isfile(_fp):
                        try:    _size += os.path.getsize(_fp)
                        except OSError: pass
                shutil.rmtree(tmp_dir, ignore_errors=True)
                removed += 1
                freed_bytes += _size
    if removed:
        print(f"  [Cleanup] {removed} alte render_tmp/ aufgeräumt ({freed_bytes//1024}KB freigegeben)", flush=True)


def _start_job_cleanup_daemon():
    def loop():
        while True:
            time.sleep(1800)  # 30 Minuten
            try:
                _cleanup_stale_jobs()
            except Exception as e:
                print(f"  [Cleanup] Fehler: {e}", flush=True)
    threading.Thread(target=loop, daemon=True).start()

# Reine Pfad-Helfer (ch_dir/ch_master/.../v_dir/v_out/.../shorts_export_dir) leben
# jetzt in core/paths.py und werden oben re-exportiert (Refactor Phase 1) -- hier
# bleiben nur die Funktionen mit echter I/O (Datei lesen/schreiben/kopieren).
def get_channel_style_refs(cid: str) -> list:
    """Style-Reference-Images: defines the global look (line weight, palette, render
    style) for image generation. Bis zu 3 Referenzbilder (Audit Juli 2026, Bereich 3
    "Multi-Style-References") -- gespeichert als newline-separierte Liste in
    style_ref_url.txt. Eine bestehende 1-Zeilen-Datei (Alt-Kanäle) ist automatisch
    eine 1-Element-Liste, keine Migration nötig."""
    p = os.path.join(ch_dir(cid), "style_ref_url.txt")
    try:
        lines = open(p).read().splitlines()
    except Exception:
        return []
    return [ln.strip() for ln in lines if ln.strip()]


def get_channel_style_ref(cid: str) -> str:
    # Abwärtskompat für Aufrufer, die nur EINEN Style-Ref brauchen (z.B. schnelle
    # Existenz-Checks). Neue Aufrufer sollten get_channel_style_refs() (Liste) nutzen.
    refs = get_channel_style_refs(cid)
    return refs[0] if refs else ""


def export_short_copy(cid: str, vid: str, src_path: str, out_name: str) -> str | None:
    """Kopiert einen fertigen Short nach shorts_export/<cid>/<vid>/<out_name> -- best
    effort, ein Fehlschlag hier darf den eigentlichen Render/Queue-Vorgang nicht
    kaputt machen (gleiches Prinzip wie die Best-effort-Schritte in youtube/upload.py).
    Gibt den Zielpfad zurück, oder None bei Fehler."""
    import shutil
    try:
        out_dir = shorts_export_dir(cid, vid)
        os.makedirs(out_dir, exist_ok=True)
        dst = os.path.join(out_dir, out_name)
        shutil.copy2(src_path, dst)
        return dst
    except Exception as e:
        print(f"  [ShortsExport] Kopieren fehlgeschlagen ({src_path} -> {out_name}): {e}", flush=True)
        return None

# Struktur-/Schnitt-Review (Juli 2026): SCRIPT_SYSTEM behauptet "~120-150 wpm", real
# gemessen an zwei fertigen Videos waren es 164-188 wpm (bis zu 24 wpm Unterschied
# ZWISCHEN Videos desselben Kanals) -- die Segmentierung plante mit einer falschen
# Sprechrate, wodurch die beabsichtigten calm/punchy-Dauerkontraste zur Mitte
# kollabierten (Root Cause des erratischen Schnitt-Rhythmus). Diese Funktion ersetzt
# die feste Annahme durch die tatsächlich gemessene Rate der zuletzt fertig gerenderten
# Videos DESSELBEN Kanals -- pro Kanal, nie global hartkodiert (Generalisierungs-Vorgabe).
DEFAULT_WPM_FALLBACK = 150.0  # nur genutzt, wenn der Kanal noch KEIN aligned Video hat


def _measure_channel_wpm(cid: str, fallback: float | None = DEFAULT_WPM_FALLBACK) -> float | None:
    """Liest alle vorhandenen plan.json-Dateien des Kanals, summiert Wörter und
    Whisper-ausgerichtete Dauer (start_aligned/end_aligned) über ALLE Videos hinweg,
    und gibt die reale Sprechrate zurück. Ein Video ohne Alignment (noch nie gerendert)
    trägt nichts bei. Kein Alignment im ganzen Kanal vorhanden -> `fallback`."""
    total_words = 0
    total_dur = 0.0
    try:
        videos_dir = os.path.join(ch_dir(cid), "videos")
        vids = os.listdir(videos_dir) if os.path.isdir(videos_dir) else []
    except OSError:
        vids = []
    for vid in vids:
        try:
            plan = json.load(open(v_plan(cid, vid)))
        except Exception:
            continue
        for s in plan.get("scenes", []):
            if s.get("start_aligned") is None or s.get("end_aligned") is None:
                continue
            dur = s["end_aligned"] - s["start_aligned"]
            if dur <= 0:
                continue
            total_words += len(str(s.get("text", "")).split())
            total_dur += dur
    if total_dur <= 0:
        return fallback
    return total_words / (total_dur / 60.0)


def load_v_meta(cid, vid):
    try:    return json.load(open(v_meta(cid, vid)))
    except: return {"titles": [], "selected_title": "", "thumbnail_prompt": ""}

def save_v_meta(cid, vid, meta):
    _atomic_write_json(v_meta(cid, vid), meta, ensure_ascii=False, indent=1)

def load_v_script(cid, vid):
    """Source-of-truth for the raw narration text per video. Created on first edit,
    survives browser-switches and machine-changes (unlike the localStorage fallback
    in the frontend). Returns {} if not yet persisted."""
    try:    return json.load(open(v_script(cid, vid)))
    except: return {}

def save_v_script(cid, vid, payload):
    # payload is the merged dict from the frontend: {text, language, preset, updatedAt}
    # We overwrite the whole file — it's tiny (<100KB even for hour-long scripts) and
    # the frontend is the only writer, so there's no read-modify-write race to worry
    # about (unlike plan.json which gets partial updates from workers).
    _atomic_write_json(v_script(cid, vid), payload, ensure_ascii=False, indent=1)

def get_video_image_model(cid: str, vid: str) -> str:
    """Image model choice (nano-banana-2 vs -lite) is per-VIDEO, not per-channel — a
    channel's style/character stays fixed, but different videos may want the cheaper
    lite model to save credits while others want full quality."""
    m = load_v_meta(cid, vid).get("image_model", "")
    return m if m in VALID_IMAGE_MODELS else "nano-banana-2"

def set_video_image_model(cid: str, vid: str, model: str):
    if model not in VALID_IMAGE_MODELS:
        model = "nano-banana-2"
    meta = load_v_meta(cid, vid)
    meta["image_model"] = model
    save_v_meta(cid, vid, meta)

OVERLAY_KEYS = ("captions", "callouts", "chapters")

def get_video_overlay_opts(cid: str, vid: str) -> dict:
    """Text-overlay toggles (Phase 4.4) are per-VIDEO, persisted like image_model —
    the render worker reads this directly (no need to pass it through every call site,
    including the one-button orchestrator's _render_worker call). Off by default: the
    plan explicitly marks this feature optional, so a video's look never changes
    without the user deliberately opting in."""
    saved = load_v_meta(cid, vid).get("overlay_opts", {})
    return {k: bool(saved.get(k, False)) for k in OVERLAY_KEYS}

def set_video_overlay_opts(cid: str, vid: str, opts: dict):
    meta = load_v_meta(cid, vid)
    meta["overlay_opts"] = {k: bool(opts.get(k, False)) for k in OVERLAY_KEYS}
    save_v_meta(cid, vid, meta)

def ensure_channel(cid):
    os.makedirs(ch_dir(cid), exist_ok=True)
    os.makedirs(ch_sheets(cid), exist_ok=True)

def ensure_video(cid, vid):
    os.makedirs(v_out(cid, vid), exist_ok=True)
    os.makedirs(v_uploads(cid, vid), exist_ok=True)

def load_videos(cid):
    try:    return json.load(open(ch_videos_file(cid)))
    except: return []

def save_videos(cid, videos):
    ensure_channel(cid)
    _atomic_write_json(ch_videos_file(cid), videos, ensure_ascii=False, indent=1)

def create_video(cid, name, mode="image"):
    videos = load_videos(cid)
    safe = re.sub(r"[^\w]", "_", name.lower())[:30] or "video"
    ids = {v["id"] for v in videos}
    vid = safe if safe not in ids else f"{safe}_{int(time.time())%10000}"
    entry = {"id": vid, "name": name, "mode": mode, "created_ts": int(time.time())}
    videos.append(entry)
    save_videos(cid, videos)
    ensure_video(cid, vid)
    return entry

def get_video_entry(cid, vid):
    for v in load_videos(cid):
        if v["id"] == vid: return v
    return None

def get_video_mode(cid, vid) -> str:
    v = get_video_entry(cid, vid)
    return (v or {}).get("mode", "image")

def set_video_mode(cid, vid, mode):
    videos = load_videos(cid)
    for v in videos:
        if v["id"] == vid: v["mode"] = mode
    save_videos(cid, videos)

# ── Channel list ──────────────────────────────────────────────────────────────
def load_channels():
    try:    return json.load(open(CHANNELS_FILE))
    except: return [{"id": "default", "name": "Kanal 1"}]

def save_channels(chs):
    os.makedirs(CHANNELS_DIR, exist_ok=True)
    _atomic_write_json(CHANNELS_FILE, chs, ensure_ascii=False, indent=1)

# ── First-run migration: move flat files → channels/default/ ─────────────────
def _legacy_mode(cid):
    p = os.path.join(ch_dir(cid), "mode.txt")
    try:
        m = open(p).read().strip()
        return m if m in ("image", "video") else "image"
    except: return "image"

def _migrate_legacy_video(cid):
    """One-time move: old single-plan channel layout (channels/<cid>/generated/plan.json)
    → channels/<cid>/videos/video_1/generated/plan.json. Preserves in-progress work."""
    if os.path.exists(ch_videos_file(cid)):
        return  # already migrated
    legacy_out     = os.path.join(ch_dir(cid), "generated")
    legacy_uploads = os.path.join(ch_dir(cid), "uploads")
    # Very first ever run (pre-channel layout): merge root generated/ into legacy_out
    if cid == "default":
        root_gen = os.path.join(HERE, "generated")
        if os.path.exists(root_gen) and not os.path.exists(legacy_out):
            shutil.copytree(root_gen, legacy_out)
        root_cs = os.path.join(HERE, "charsheets")
        if os.path.exists(root_cs):
            os.makedirs(ch_sheets(cid), exist_ok=True)
            for f in os.listdir(root_cs):
                dst = os.path.join(ch_sheets(cid), f)
                if not os.path.exists(dst):
                    try: shutil.copy2(os.path.join(root_cs, f), dst)
                    except: pass
        old_master = os.path.join(HERE, "master_prompt.txt")
        if os.path.exists(old_master) and not os.path.exists(ch_master(cid)):
            shutil.copy2(old_master, ch_master(cid))

    has_legacy = os.path.exists(os.path.join(legacy_out, "plan.json")) or os.path.exists(legacy_out)
    if has_legacy:
        entry = create_video(cid, "Video 1", mode=_legacy_mode(cid))
        vid = entry["id"]
        if os.path.exists(legacy_out):
            for f in os.listdir(legacy_out):
                shutil.move(os.path.join(legacy_out, f), os.path.join(v_out(cid, vid), f))
            shutil.rmtree(legacy_out, ignore_errors=True)
        if os.path.exists(legacy_uploads):
            for f in os.listdir(legacy_uploads):
                shutil.move(os.path.join(legacy_uploads, f), os.path.join(v_uploads(cid, vid), f))
            shutil.rmtree(legacy_uploads, ignore_errors=True)
        # fix audio_meta.json's stored absolute path to point at new location
        am = v_audio(cid, vid)
        if os.path.exists(am):
            try:
                meta = json.load(open(am))
                new_path = os.path.join(v_uploads(cid, vid), os.path.basename(meta.get("path", "")))
                if os.path.exists(new_path):
                    meta["path"] = new_path
                    _atomic_write_json(am, meta)
            except: pass
        print(f"  [Migrate] Kanal '{cid}': altes Layout → 'Video 1' ({vid})", flush=True)
    else:
        save_videos(cid, [])

def init_channels():
    os.makedirs(CHANNELS_DIR, exist_ok=True)
    if not os.path.exists(CHANNELS_FILE):
        save_channels([{"id": "default", "name": "Kanal 1"}])
    for ch in load_channels():
        ensure_channel(ch["id"])
        _migrate_legacy_video(ch["id"])

init_channels()
# Schwäche #69: räume alte render_tmp/ von gecrashten Renders
_cleanup_stale_render_tmp()

# KIE.ai — image generation: KIE_API, VALID_IMAGE_MODELS, kie_key() etc. jetzt in
# engine/imagegen.py (Evaluation Juli 2026, Änderung 1), re-exportiert oben.
KIE_MODEL    = "nano-banana-2"
# KIE.ai — text + audio (OpenAI-compatible)
KIE_CHAT_URL = "https://api.kie.ai/gemini-2.5-flash/v1/chat/completions"
# KIE.ai — native Gemini format (contents/parts), used for gemini-3-5-flash which
# supports thinkingConfig.thinkingLevel — helps counteract "lazy"/generic output on
# later items in a batch. Verified working 2026-07-02 against the real API.
GEMINI_NATIVE_URL = "https://api.kie.ai/gemini/v1/models/{model}:generateContent"

# ElevenLabs — moved to engine_elevenlabs.py (Phase J engine refactor). Wildcard-
# import weiter oben in dashboard.py macht die Symbole global verfügbar.

# Shared transcription status (thread-safe via GIL for simple dict ops)
TX_STATUS = {"step": 0, "total": 4, "msg": "Bereit", "running": False, "error": ""}

def tx(step, msg):
    TX_STATUS["step"] = step
    TX_STATUS["msg"] = msg
    print(f"  [TX {step}/{TX_STATUS['total']}] {msg}", flush=True)

def post_kie_text(messages, json_mode=False, temp=0.7):
    """KIE.ai OpenAI-compatible chat completions (Gemini 2.5 Flash)."""
    body = {"model": "gemini-2.5-flash", "messages": messages, "temperature": temp}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    hdrs = {
        "Authorization": f"Bearer {kie_key()}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Origin": "https://kie.ai",
        "Referer": "https://kie.ai/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8",
    }
    req = urllib.request.Request(KIE_CHAT_URL, data=json.dumps(body).encode(), headers=hdrs)
    with urllib.request.urlopen(req, timeout=240) as r:
        resp = json.load(r)
    return resp["choices"][0]["message"]["content"]

def post_gemini_native(messages, json_mode=False, temp=0.7, model="gemini-3-5-flash",
                        thinking_level="high", response_schema=None):
    """KIE.ai native Gemini format (gemini-3-5-flash) — supports thinkingConfig for
    more consistent reasoning on later items in a batch. `messages` uses the same
    [{"role","content"}] shape as post_kie_text() for drop-in compatibility;

    July 2026: default thinking_level switched from "high" to "low" for prompt-generation
    paths — high burns 3000+ reasoning tokens per call on long JSON-array outputs and
    frequently pushes past maxOutputTokens=8192 mid-response, breaking json.loads().
    Tests with response_schema + low thinking: 451-char output for 1 beat, parse OK,
    zero retries needed.

    response_schema: optional Gemini JSON Schema (passed through to responseSchema
    field). When provided, Gemini guarantees the output matches the schema exactly —
    no missing fields, no unescaped quotes, no truncation mid-value.
    role "system" is folded into the first user turn since Gemini has no system role
    in this endpoint's contents array."""
    contents = []
    system_txt = ""
    for m in messages:
        if m["role"] == "system":
            system_txt += m["content"] + "\n\n"
            continue
        role = "model" if m["role"] == "assistant" else "user"
        text = (system_txt + m["content"]) if (role == "user" and system_txt) else m["content"]
        if role == "user": system_txt = ""
        contents.append({"role": role, "parts": [{"text": text}]})

    gen_cfg = {"temperature": temp, "thinkingConfig": {"thinkingLevel": thinking_level},
               "maxOutputTokens": 16384}
    if json_mode:
        gen_cfg["responseMimeType"] = "application/json"
        if response_schema:
            gen_cfg["responseSchema"] = response_schema
    body = {"stream": False, "contents": contents, "generationConfig": gen_cfg}
    hdrs = {
        "Authorization": f"Bearer {kie_key()}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Origin": "https://kie.ai",
        "Referer": "https://kie.ai/",
        "Accept": "application/json, text/plain, */*",
    }
    url = GEMINI_NATIVE_URL.format(model=model)
    data = json.dumps(body).encode()

    def _do_call():
        req = urllib.request.Request(url, data=data, headers=hdrs)
        with urllib.request.urlopen(req, timeout=240) as r:
            resp = json.load(r)
        candidates = resp.get("candidates")
        if not candidates:
            block_reason = (resp.get("promptFeedback") or {}).get("blockReason")
            raise RuntimeError(f"Gemini: keine candidates in Antwort "
                                f"(blockReason={block_reason}, keys={list(resp.keys())})")
        cand = candidates[0]
        finish = cand.get("finishReason")
        parts = (cand.get("content") or {}).get("parts") or []
        if not parts:
            raise RuntimeError(f"Gemini: keine parts im Kandidaten (finishReason={finish})")
        if finish and finish not in ("STOP", "MAX_TOKENS"):
            raise RuntimeError(f"Gemini: finishReason={finish} (Safety/Recitation-Filter?)")
        return parts[0]["text"]

    try:
        return _do_call()
    except (RuntimeError, urllib.error.URLError, TimeoutError) as e:
        print(f"  [Gemini3.5] Fehler, ein Retry nach 2s Pause: {e}", flush=True)
        # July 2026 (User-Report: 15-Min-Prompt-Phase): immediate back-to-back retries on
        # `keine candidates in Antwort` triggered KIE.ai rate-limits harder. A short pause
        # lets the upstream recover before the next attempt.
        time.sleep(2)
        return _do_call()

# ---------- Charakter-Steckbrief für den Prompt-Text ----------
# Evaluation Juli 2026 (User-Report "Jake/Narrator sehen von Szene zu Szene anders aus,
# teils bricht der Stil ganz"): _build_image_prompt() KANN einen kanonischen Steckbrief
# plus die entscheidende Konfliktregel ("bei Widerspruch gewinnt das Referenzbild") in
# den Prompt schreiben — beide Aufrufer in der Bild-Generierung übergaben aber
# char_refs=None und gar kein entity, also blieb char_hint IMMER leer. Das Referenzbild
# ging zwar an KIE, aber ohne den Text der ihm Vorrang gibt. Da die Szenen-Prompts in
# 12er-Chunks geschrieben werden und der Prompt-Autor den Charakter in jedem Chunk neu
# erfindet ("grey crewneck" statt des roten T-Shirts im Charsheet), gewann regelmäßig
# der konkrete Szenentext gegen das Referenzbild.
#
# Zweite Hürde: _filter_char_refs_for_entity vergleicht `safe == entity[5:]`, also
# "01" gegen den Charsheet-Namen "narrator" — das matcht NIE. Deshalb lösen wir hier
# concrete_entity (char_01) über plan["characters"] auf den echten Charsheet-Eintrag
# auf und geben dessen eigenen `safe`-Key als entity zurück, womit der Filter greift.
def charsheet_refs_for_entity(plan: dict, cid: str, vid: str, entity: str):
    """(char_refs, entity_key) für _build_image_prompt. Leer, wenn kein Charsheet passt."""
    if not entity:
        return [], ""
    try:
        from engine.scenes import _find_charsheet_png
        png_path, _dbg = _find_charsheet_png(plan, cid, vid, entity)
        if not png_path:
            return [], ""
        meta = json.load(open(png_path.replace(".png", ".json")))
    except Exception:
        return [], ""
    safe = (meta.get("safe") or "").strip()
    desc = (meta.get("description") or "").strip()
    if not safe or not desc:
        return [], ""
    # Der Charsheet-Steckbrief ist die Wahrheit, nicht plan["characters"]: das PNG wurde
    # aus ihm erzeugt, plan["characters"] kann davon abweichen (z.B. fehlt dort das rote
    # T-Shirt des Narrators, das im Charsheet-Bild deutlich zu sehen ist).
    return [{"name": meta.get("name") or safe, "safe": safe, "description": desc}], safe


# Szenen ohne Charakter-Anker, die trotzdem Menschen/Körperteile zeigen (Hände, Füße,
# "he/she", ein namentlich genannter Charakter). concrete_entity ist dort ein Objekt
# ("expensive designer sneakers"), also stieg die Referenz-Auflösung aus und das Modell
# erfand Hautton und Strichführung frei — sichtbar an Szene 73 (weiße, glänzende Haut,
# kompletter Stilbruch). 9 von 131 Szenen des warnenden Videos fielen in diese Lücke.
_PEOPLE_IN_PROMPT_RE = re.compile(
    r"\b(hands?|feet|foot|arms?|legs?|fingers?|face|shoulders?|"
    r"man|woman|men|women|person|people|boy|girl|he|she|his|her|"
    r"narrator|figure|silhouette)\b", re.I)


def scene_depicts_people(scene: dict) -> bool:
    return bool(_PEOPLE_IN_PROMPT_RE.search(scene.get("prompt") or ""))


_CHAR_NAME_STOPWORDS = {"the", "a", "an", "you", "your"}


def _character_text_match(scene_prompt: str, name_or_role: str) -> bool:
    """True wenn der Szenen-Prompt die Rolle/den Namen des Charakters erwähnt — z.B.
    "coworker" für name_or_role="Coworker", "protagonist" für "Protagonist (You)".
    Klammerzusätze ("(You)") und Stoppwörter werden entfernt, bevor verglichen wird:
    ein Substring-Match auf "Protagonist (You)" wörtlich würde nie treffen (kein Prompt
    schreibt das so), und ein Match auf das Stoppwort "you" allein würde auf fast jeden
    Prompt anschlagen."""
    name = re.sub(r"\([^)]*\)", "", name_or_role).strip().lower()
    words = [w for w in re.findall(r"[a-z]+", name) if w not in _CHAR_NAME_STOPWORDS]
    if not words:
        return False
    candidates = [" ".join(words)] if len(words) > 1 else []
    candidates.append(words[-1])
    return any(re.search(rf"\b{re.escape(c)}\b", scene_prompt, re.I) for c in candidates)


def nearest_character_entity(plan: dict, scene: dict) -> str:
    """Charakter für eine Szene ohne eigenen Charakter-Anker.

    Juli 2026 (User-Report, Bilder UI#72/#101 aus "The Raise Nobody Noticed": eine
    Szene über den Coworker bzw. den Protagonisten bekam die REFERENZ DES JEWEILS
    ANDEREN Charakters): reine zeitliche Nähe ("wer kam zuletzt vor") ignoriert den
    Szeneninhalt komplett — bei UI#101 ("...over the shoulder of the protagonist...")
    griff der alte Code den Coworker, weil der zufällig der zeitlich letzte Charakter
    war. Jetzt zuerst ein Textabgleich: nennt der Prompt einen bekannten Charakter beim
    Namen/bei der Rolle (siehe _character_text_match), gewinnt der — unabhängig von der
    Position im Skript. Nur wenn kein Name im Text vorkommt (z.B. eine reine
    Hand-/Fuß-Nahaufnahme ohne Rollen-Nennung), fällt es auf die alte Nähe-Heuristik
    zurück (erst rückwärts, sonst vorwärts)."""
    prompt = scene.get("prompt") or ""
    for ch in plan.get("characters") or []:
        cid = str(ch.get("id") or "")
        name = str(ch.get("name_or_role") or "")
        if cid.startswith("char_") and name and _character_text_match(prompt, name):
            return cid

    i = scene.get("i", 0)
    scenes = plan.get("scenes") or []
    chars = [s for s in scenes if str(s.get("concrete_entity", "")).startswith("char_")]
    before = [s for s in chars if s.get("i", -1) < i]
    if before:
        return str(max(before, key=lambda s: s["i"])["concrete_entity"])
    after = [s for s in chars if s.get("i", -1) > i]
    if after:
        return str(min(after, key=lambda s: s["i"])["concrete_entity"])
    return ""


# ---------- Master-Prompt ----------
def read_master(cid="default"):
    try:    return open(ch_master(cid), encoding="utf-8").read().strip()
    except: return ""

def write_master(cid, txt):
    open(ch_master(cid), "w", encoding="utf-8").write(txt.strip() + "\n")

# ---------- Skript -> Beats (inhaltlich, nach Zeit/Wort) ----------
def clean_script(s):
    s = re.sub(r"\(?\b\d{1,2}:\d{2}\b\)?", " ", s)   # Timestamps entfernen
    s = re.sub(r"\s+", " ", s).strip()
    return s

def fmt_t(s):
    return f"{int(s)//60}:{int(s)%60:02d}"

# ---------- Beats -> visuelle Prompts (2-stufig: Analyse + Prompts) ----------

def analyze_script(beats):
    """Stage 1 — read the ENTIRE script once and extract a structured entity map
    (locations, characters, recurring symbols, emotional arc, callbacks) that gets
    passed into every downstream prompt-generation call. Prevents scene-by-scene
    isolated interpretation."""
    instr = (
        "You are analyzing a complete video narration script (JSON array of text beats, "
        "0-indexed) for a visual-prompt generation pipeline. Read the ENTIRE script once "
        "before answering. Extract ONLY facts that actually appear or are clearly implied "
        "in the script — invent nothing.\n\n"
        "Return this exact JSON object:\n"
        "{\n"
        '  "locations": [{"id": "loc_01", "name": string, "description": string, '
        '"first_appears_beat": N}],\n'
        '  "characters": [{"id": "char_01", "name_or_role": string, "visual_description": '
        '"string (CRITICAL: You MUST extract the narrator and ALL mentioned people. If no physical description exists, you MUST invent a generic basic look, e.g. \'young man, casual clothes\')", "anonymize": bool, "first_appears_beat": N}],\n'
        '  "recurring_symbols": [{"id": "sym_01", "object": string, "meaning": string, '
        '"beats": [N, N]}],\n'
        '  "emotional_arc": {"opening": "ONE word", "midpoint": "ONE word", "resolution": "ONE word"},\n'
        '  "callbacks": [{"from_beat": N, "to_beat": M, "shared_element": string}],\n'
        '  "pacing": [{"beat": N, "label": "calm" | "normal" | "punchy"}],\n'
        '  "visual_sequences": [{"seq_id": N, "beats": [N, N, N], "reason": string, '
        '"camera": "slow push-in" | "pan" | "static series"}],\n'
        '  "callouts": [{"beat": N, "text": "short number/date/stat, max ~6 chars"}],\n'
        '  "data_visuals": [{"beat": N, "kind": "counter", "from": 0, "to": 3.2, '
        '"format": "3,2 Mio.", "label": "verhungert 1994-1998"}],\n'
        '  "phases": [{"beat": N, "phase": "OPENING" | "RISING_ACTION" | "CLIMAX" | "RESOLUTION"}],\n'
        '  "act_breaks": [N],\n'
        '  "climax_beat": N,\n'
        '  "hook": {"beat": N, "type": "quote" | "scene" | "thesis" | "none", '
        '"strength": "strong" | "weak"},\n'
        '  "throughline_question": "one-sentence question that drives the whole video, OR empty string"\n'
        "}\n\n"
        # Diese Regeln standen zuerst als "# ..."-Zeilen INNERHALB des JSON-Templates oben.
        # Das Modell ahmte das Muster nach und lieferte JSON MIT Kommentarzeilen zurück —
        # ungültig, json.loads scheiterte, characters blieb leer. Sie gehören als Prosa
        # HINTER den JSON-Block, nicht hinein.
        "CHARACTERS — hard rules:\n"
        "- CHARACTERS ARE MANDATORY. A script with any human presence NEVER returns an empty "
        "characters list.\n"
        '- Second-person scripts ("you", "your") DO have characters: the addressed person IS the '
        'protagonist. Emit them as char_01 with name_or_role "Protagonist (You)".\n'
        "- Unnamed roles are characters too (a coworker, the boss, an investor, a friend). Give "
        "each ONE stable id.\n"
        "- ONE id per person. Never create separate ids for the same person at different ages or "
        "moods (no char_protagonist_young plus char_protagonist_elderly).\n"
        # Juli 2026 (an echten Charsheets verifiziert): Die Beschreibung darf zeichenbar sein,
        # aber NICHT realistisch klingen. Mit "light grey button-down shirt with rolled sleeves,
        # athletic build" rendert das Bildmodell einen halb-realistischen, schattierten Mann und
        # ignoriert den Strichmännchen-Stil des Kanals komplett. Mit "short blonde hair, plain
        # light grey t-shirt" kommt exakt derselbe Charakter im korrekten Flat-Stil heraus.
        # Stoff-/Schnitt-/Körperbau-Details sind Realismus-Trigger — sie müssen raus.
        "- visual_description MUST be SHORT (max ~8 words) and purely about COLOUR: hair colour + "
        "hair style, and ONE simple garment + its colour. Nothing else.\n"
        "    GOOD: 'short black hair, plain green t-shirt'\n"
        "    GOOD: 'long red hair, simple blue dress'\n"
        "    BAD:  'a young professional who gradually becomes wealthy'  (nothing to draw)\n"
        "    BAD:  'athletic build, tailored button-down shirt with rolled sleeves'  (fabric, cut "
        "and physique detail force a realistic rendering and destroy the channel's art style)\n"
        "- NEVER mention: physique/build, fabric, tailoring, sleeves, accessories, age ranges.\n"
        "- Describe ONE canonical look per character — their default appearance. Do NOT describe "
        "how they change over the story; the pipeline draws one consistent design per character.\n"
        "- Characters MUST be VISUALLY DISTINGUISHABLE: no two characters may share the same hair "
        "colour AND the same clothing colour. Give each a different combination so a viewer tells "
        "them apart at a glance.\n\n"
        'Rule: set "anonymize": true for every real, identifiable named person (public '
        "figures, named victims/individuals) — these get depicted later only as a "
        "silhouette or symbolic stand-in, never named or shown photorealistically.\n\n"
        "PACING — provide exactly one entry per beat (0-indexed, same count as BEATS), "
        "judged by its narrative WEIGHT WITHIN THE ARC you just identified above, not the "
        "sentence in isolation:\n"
        '- "calm": background/context/setup — the viewer needs time to absorb it, this '
        "beat can hold on screen for 4-6 seconds.\n"
        '- "normal": default pacing, neither calm setup nor a dramatic spike.\n'
        '- "punchy": emotional peaks, reveals, shocking numbers, or cliffhangers — moments '
        "that should be slammed through fast, under 1.5 seconds, sometimes even meriting "
        "two rapid consecutive images for a 'gut punch'. A beat sitting near the "
        "emotional_arc's midpoint/resolution should be MORE likely punchy even if its "
        "literal wording sounds mild — judge by position in the story, not just word choice.\n\n"
        "VISUAL_SEQUENCES — group beats into a sequence ONLY when ≥2 CONSECUTIVE beats "
        "describe the SAME concrete location/subject continuously, as if it were one "
        "unbroken shot (e.g. a scene that lingers on the same room/object/person across "
        "several sentences). When in doubt, do NOT form a sequence — independent single "
        "images are the safe default; most beats belong to no sequence at all. 'beats' "
        "are 0-indexed positions into the SAME BEATS array given below (identical index "
        "space to PACING above), listed in order.\n\n"
        "CALLOUTS — ONLY if a beat states a concrete, specific number/date/statistic "
        "explicitly in its own text (e.g. a year, a count, a percentage, an age) — never "
        "invent one, never paraphrase a vague amount into a fake-precise number. Omit a "
        "beat entirely if nothing concrete fits; most beats will have no callout at all. "
        "Keep 'text' extremely short — the exact figure only (e.g. \"1969\", \"3.2M\", "
        "\"47%\"), no surrounding words.\n\n"
        "DATA_VISUALS (Phase N) — animated count-up overlay for statistics the script "
        "states literally. STRICT RULE: only use data_visuals when the beat text contains "
        "a concrete number that should be highlighted visually (e.g. '3,2 Millionen Menschen "
        "verhungerten' → counter from=0 to=3.2 format='3,2 Mio.' label='verhungert'). "
        "NEVER invent numbers — if the script says 'viele' or 'tausende' without precise "
        "figures, OMIT data_visual entirely. The number must appear literally in the "
        "beat text — paraphrase or inference is forbidden. Optional schema:\n"
        "- kind: 'counter' (only counter implemented in Phase N.1; 'bar'/'timeline' planned)\n"
        "- from: starting value (usually 0)\n"
        "- to: ending value (the concrete number from the beat text)\n"
        "- format: Python f-string-style format for the displayed value (e.g. '3,2 Mio.' "
        "  becomes '{:.1f} Mio.')\n"
        "- label: optional subtitle below the counter (max ~40 chars)\n\n"
        "DRAMATURGY (Story-Phase-Engine, Phase 3):\n"
        "Assign a STORY-PHASE to every beat — one of exactly four values: "
        "\"OPENING\", \"RISING_ACTION\", \"CLIMAX\", \"RESOLUTION\". These reflect the "
        "**actual narrative arc, NOT position** — a flash-forward cold-open at position 0 "
        "legitimately belongs to CLIMAX or RESOLUTION; a calm epilogue that wraps the "
        "story belongs to RESOLUTION even if it's the last beat. Use the emotional_arc "
        "you just identified as the primary signal.\n"
        "- phases: array with exactly one entry per beat, 0-indexed, SAME index space as "
        "pacing above. Same count as BEATS.\n"
        "- act_breaks: list of beat indices where the dramatic situation changes "
        "irreversibly (inciting incident, midpoint reversal, climax into resolution). "
        "Typical 3-act structure: 2 breaks. Up to 4 for complex narratives. Empty list "
        "is valid for single-act scripts. Beats listed here should ALSO appear at the "
        "boundary between two different phases in 'phases'.\n"
        "- climax_beat: the SINGLE beat index of the highest-tension moment — where the "
        "protagonist confronts the decisive turn. -1 if the script has no clear climax "
        "(purely informational scripts).\n\n"
        "HOOK (Phase L) — the cold-open moment that should grab the viewer in 0:00–0:05:\n"
        '- hook.beat: index of the beat that opens the video, 0..2 for a cold-open, '
        "or the same as the first beat with no clear hook if it starts with context/definition. "
        "-1 if no hook is identifiable at all.\n"
        '- hook.type: what kind of hook — "quote" (a striking statement/number), "scene" '
        '(a vivid concrete situation), "thesis" (a claim or proposition), or "none" '
        "(the opening is purely contextual/definitional, no cold-open).\n"
        '- hook.strength: "strong" if it would make the viewer stop scrolling (a person, '
        "scene, number, or claim that hits immediately), \"weak\" if it tries but doesn't "
        'land, or "none" if type is "none".\n'
        '- throughline_question: ONE-SENTENCE question (max 200 chars) that the entire '
        "video answers — phrased in a way the viewer would recognize and want to know the "
        'answer to. EMPTY STRING IS VALID if the script has no question (e.g. purely '
        "informational/encyclopedic scripts). NIEMALS eine Frage erfinden, die das Skript "
        "nicht trägt — wenn das Skript keine Frage stellt, leerer String.\n\n"
        "BEATS:\n" + json.dumps(beats, ensure_ascii=False)
    )
    result = {}
    for attempt in (1, 2):
        try:
            # thinking_level="low" ist hier NICHT Sparsamkeit, sondern Korrektheit.
            # Juli 2026 (User-Report "es wurden keine Charaktere aus dem Skript generiert"):
            # Dieser Call lief mit dem "high"-Default — wovor post_gemini_native's eigener
            # Docstring warnt: "high burns 3000+ reasoning tokens per call on long JSON-array
            # outputs and frequently pushes past maxOutputTokens mid-response, breaking
            # json.loads()". Genau das passierte: Die Antwort enthält pro Beat je einen
            # pacing- UND einen phases-Eintrag, bei 144 Beats also ~288 Objekte. Zusammen mit
            # den Reasoning-Tokens riss sie das Limit, wurde MITTEN IM STRING abgeschnitten
            # ("Unterminated string"), json.loads scheiterte — und übrig blieb ein leeres
            # result, also eine leere characters-Liste. Der Fehler sah wie ein
            # Verständnisproblem des Modells aus, war aber schlicht eine gekappte Antwort.
            # Er trifft LANGE Skripte, unabhängig vom Inhalt.
            txt = post_gemini_native([{"role": "user", "content": instr}], json_mode=True,
                                      temp=0.2, thinking_level="low")
            result = json.loads(txt)
        except Exception as e:
            print(f"Analyse-Fehler (Versuch {attempt}):", e)
            continue
        # Juli 2026 (User-Report: "Elizabeth Holmes" ungefiltert im Bild-Prompt +
        # inkonsistente concrete_entity-IDs quer über das Video): eine leere
        # characters-Liste bei einem NICHT-trivialen Skript ist kein harmloser
        # Rand-, sondern ein Qualitätsfehler — ohne sie hat jeder nachfolgende
        # Chunk-Aufruf (visual_prompts) keinen gemeinsamen Anker mehr und erfindet
        # pro Chunk eine eigene, inkonsistente Entity-ID für dieselbe Person UND die
        # "anonymize real named person"-Regel greift nie (leere Liste = niemand zum
        # Anonymisieren). Bei leerem Ergebnis + genug Beats für ein "echtes" Skript:
        # einmal erneut versuchen, bevor wir das Risiko eingehen.
        if not result.get("characters") and len(beats) >= 5 and attempt == 1:
            print(f"  [Analyse] characters-Liste leer bei {len(beats)} Beats — "
                  f"wiederhole einmal (Versuch {attempt})", flush=True)
            continue
        return result
    if not result.get("characters"):
        print(f"  [Analyse] WARNUNG: characters-Liste bleibt leer nach Retry "
              f"({len(beats)} Beats) — Charakter-Konsistenz/Anonymisierung könnte "
              f"in dieser Generierung nicht greifen.", flush=True)
    return result


def story_phase(i: int, total: int) -> str:
    # Underscore form throughout the project — matches analyze_script prompt and
    # _PHASE_MOTION_CANDIDATES keys. The legacy "RISING ACTION" (with space) was used
    # by the position-only heuristic before Phase 3; a single source of truth now.
    return (
        "OPENING"        if i < total * 0.15 else
        "RISING_ACTION"  if i < total * 0.50 else
        "CLIMAX"         if i < total * 0.75 else
        "RESOLUTION"
    )

# Story-Phase-Engine (Phase 3, Juli 2026): LLM-driven phase assignment with 80%-coverage
# hysteresis. Single source of truth = s["phase"]; s["phase_source"] is a debug-grip field
# that lets you grep `"phase_source": "position-fallback"` in any plan.json to find which
# scenes fell back to position-based. Hysterese: partial-LLM-coverage is treated as
# schema-drift → full fallback instead of mixing reliable fallback with uncertain LLM data.
# PHASE_SET / PHASE_TO_ACT / PHASE_PROMPT_ADDITIONS / PHASE_COLOR_FILTER / PHASE_VOLUME /
# PHASE_ACCENT moved to engine_elevenlabs.py (Phase J engine refactor).
# narration carries the moment), CLIMAX gets the loudest (cinematic swell). These
# values are multiplies on the music input BEFORE sidechaincompress ducks under the
# voice — sidechaincompress then takes what each phase gave it. With only the single
# neutral_bed.mp3 asset currently available, the per-phase effect is audible but not
# dramatic; it'll become meaningful once Pixabay stems (drums/bass/pads) get added
# later — drop them in, the volume envelope stays the same.
PHASE_VOLUME = {
    "OPENING":       0.30,
    "RISING_ACTION": 0.55,
    "CLIMAX":        0.85,
    "RESOLUTION":    0.35,
}

# Phase I: TTS-Preprocessing (SSML-Enrichment). ElevenLabs accepts a curated SSML subset:
# <break time="500ms"/> for natural pauses, and our text-based emphasis markers (the
# `<emphasis>` SSML tag is NOT in ElevenLabs' supported set — they treat it as
# literal text — so we use variations of punctuation + capitalisation to nudge the
# voice without breaking compat). TwelveLabs' speech engine reacts to:
#   - THREE-DOT "..." — natural short pause between phrases
#   - SINGLE-LINE BREAK (newline) — slightly stronger pause / scene-change hint
# TTS_PAUSE_BEFORE_CLIMAX, TTS_PAUSE_AFTER_PHASE_BREAK — siehe engine_elevenlabs.py
# (Single Source of Truth seit Phase-J-Refactor). Die Duplikate unten sind Dead Code;
# aus dem Dashboard hier entfernt, die einzigen Quellen sind jetzt die __all__-Imports
# aus engine_elevenlabs. Belassen mit historischem Hinweis.
#   - DOUBLE-LINE BREAK — full paragraph pause
#   - EXCLAMATION "!" + ALL-CAPS word — emphasis on the word (probability, not guarantee)
# The enricher is conservative — it only ADDS markers, never removes existing text.
TTS_PAUSE_MARKERS = {
    ".": ".",      # explicit sentence end (no marker, TTS treats as normal)
    "!": "!",      # emphasis on the preceding word
    "?": "?",
    ";": ".",      # semicolon as soft period
    ",": ",",
}
# TTS_PAUSE_AFTER_SENTENCE / TTS_PAUSE_BEFORE_CLIMAX / TTS_PAUSE_AFTER_PHASE_BREAK
# waren hier als Dead-Code-Definitionen (keinerlei Referenz im Codebase). Sie leben
# jetzt NUR in engine_elevenlabs.py als __all__-Exports. Diese Notiz dokumentiert
# die Migration; die ursprüngliche Definition wurde 2026-07 entfernt (Phase-J-clean-up).
# AUF KEINEN FALL neue Definitionen hier hinzufügen — siehe engine_elevenlabs.py.
PHASE_COVERAGE_THRESHOLD = 0.8  # <80% LLM coverage → full fallback (no mixing)

def _assign_phases(scenes: list, analysis: dict, total: int) -> None:
    """Phase 3: assign each scene a STORY-PHASE, preferring LLM data when available.

    LLM data (analysis["phases"]) wins when ≥80% of beats have entries — coverage above
    this threshold means the LLM understood the script well enough to trust. Below it, we
    fall back to position-based phase for ALL scenes (no half-trust mixing). Single source
    of truth = scenes[].phase; scenes[].phase_source = "llm" | "position-fallback".
    """
    raw_phases = (analysis or {}).get("phases") or []
    llm_phases = {p.get("beat"): p.get("phase")
                  for p in raw_phases
                  if p.get("phase") in PHASE_SET}
    act_breaks = set((analysis or {}).get("act_breaks") or [])
    climax_beat = (analysis or {}).get("climax_beat", -1)
    coverage = len(llm_phases) / max(1, total)
    use_llm = coverage >= PHASE_COVERAGE_THRESHOLD

    n_llm = n_fb = 0
    for s in scenes:
        beat = s.get("beat_index", s["i"])
        if use_llm and beat in llm_phases:
            s["phase"] = llm_phases[beat]
            s["phase_source"] = "llm"
            s["is_phase_break"] = (beat in act_breaks)
            s["is_climax"] = (beat == climax_beat)
            s["act_index"] = PHASE_TO_ACT[s["phase"]]
            n_llm += 1
        else:
            s["phase"] = story_phase(s["i"], total)
            s["phase_source"] = "position-fallback"
            s["is_phase_break"] = False
            s["is_climax"] = False
            s["act_index"] = min(3, (s["i"] * 4 // max(1, total)))
            n_fb += 1
    # Phase E: classify each scene as 'scene' (default) or 'title_card' (if it's an
    # act-break). Title-cards are rendered as a separate PIL-generated still instead of
    # going through _build_image_prompt + KIE — they're full-screen title text, not
    # narrative imagery. The auto-derived card_title can be overridden by the user by
    # writing to s["card_title"] in the frontend.
    phase_break_sorted = sorted((s for s in scenes if s.get("is_phase_break")),
                                 key=lambda x: x["i"])
    for idx, s in enumerate(phase_break_sorted, start=1):
        s["kind"] = "title_card"
        s["act_index_visual"] = idx   # which act_break in chronological order (1-based)
        if not s.get("card_title"):
            s["card_title"] = f"Akt {idx}" if len(phase_break_sorted) > 1 else "Neuer Akt"
    for s in scenes:
        if "kind" not in s:
            s["kind"] = "scene"
    print(f"  [Phase] {n_llm}/{total} LLM, {n_fb}/{total} fallback "
          f"(coverage={coverage*100:.0f}%, hysteresis={'ON' if use_llm else 'OFF'}), "
          f"{len(phase_break_sorted)} title-card(s)", flush=True)


# VALID_IMAGE_MODELS, Rate-Limit/Circuit-Breaker (_kie_rate_limit_wait etc.) und
# _kie_submit_image jetzt in engine/imagegen.py (Evaluation Juli 2026, Änderung 1),
# re-exportiert oben — Verhalten unverändert, reines Verschieben.

IMAGE_JOB_MAX_POLLS = 50  # 50 * 3s = 150s. 90s (30 polls) turned out too aggressive in
# practice — live runs showed several legitimate generations still succeeding well past
# 90s, causing unnecessary timeouts that then had to be retried (wasting a credit each
# time). 150s is a middle ground between the original 4-minute cap (way too long to block
# a batch) and 90s (too short, false-positive timeouts on normal but slower generations).

def _image_job_worker(job_id: str, task_id: str, out_path: str, plan_path: str, scene_i: int,
                       scene_key: tuple = None):
    """Background thread: polls KIE task, downloads result, updates plan. Only reached
    via /api/generate_one, which already acquired IMAGE_GEN_SEMAPHORE before submitting —
    release it here once this scene's generation is fully done, one way or another."""
    try:
        _image_job_worker_inner(job_id, task_id, out_path, plan_path, scene_i)
    finally:
        IMAGE_GEN_SEMAPHORE.release()
        if scene_key is not None:
            with _ACTIVE_SCENE_JOBS_LOCK:
                if ACTIVE_SCENE_JOBS.get(scene_key) == job_id:
                    del ACTIVE_SCENE_JOBS[scene_key]

def _mark_scene_error(plan_path: str, scene_i: int):
    """Persist a failed/timed-out generation into plan.json too, not just the in-memory
    JOBS dict — otherwise a scene a browser reloaded away from stays stuck showing
    'läuft' forever, since nothing on reload can tell it actually failed."""
    with _PLAN_WRITE_LOCK:
        try:
            plan = json.load(open(plan_path))
            for s in plan["scenes"]:
                if s["i"] == scene_i and s.get("status") == "läuft":
                    s["status"] = "fehler"
            _atomic_write_json(plan_path, plan, ensure_ascii=False, indent=1)
        except: pass

def _image_job_worker_inner(job_id: str, task_id: str, out_path: str, plan_path: str, scene_i: int):
    poll_url  = f"{KIE_API}/recordInfo?taskId={task_id}"
    poll_hdrs = {"Authorization": f"Bearer {kie_key()}"}
    print(f"  [KIE] Job {job_id} / task {task_id} läuft …", flush=True)
    for poll_i in range(IMAGE_JOB_MAX_POLLS):
        time.sleep(3)
        try:
            with urllib.request.urlopen(urllib.request.Request(poll_url, headers=poll_hdrs), timeout=15) as r:
                info = json.load(r).get("data", {})
        except Exception as e:
            print(f"  [KIE] Poll-Fehler: {e}", flush=True); continue
        state    = info.get("state", "")
        progress = int(info.get("progress", 0))
        JOBS[job_id]["progress"] = progress
        # Only log every 5th poll while still waiting, to avoid flooding the log —
        # state changes (success/fail) always print below regardless.
        if state != "waiting" or poll_i % 5 == 0:
            print(f"  [KIE] {job_id} {state} {progress}%", flush=True)
        if state == "success":
            try:    urls = json.loads(info.get("resultJson", "{}")).get("resultUrls", [])
            except: urls = []
            if not urls:
                JOBS[job_id] = {"status": "error", "progress": 0, "error": "Kein Bild in resultUrls", "ts": time.time()}
                _mark_scene_error(plan_path, scene_i)
                return
            try:
                dl_req = urllib.request.Request(urls[0],
                    headers={"Referer": "https://kie.ai/", "User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(dl_req, timeout=60) as img_r:
                    open(out_path, "wb").write(img_r.read())
            except Exception as e:
                JOBS[job_id] = {"status": "error", "progress": 0, "error": f"Bild-Download fehlgeschlagen: {e}", "ts": time.time()}
                _mark_scene_error(plan_path, scene_i)
                return
            fn = os.path.basename(out_path)
            JOBS[job_id] = {"status": "done", "progress": 100,
                            "file": fn, "source_url": urls[0], "ts": int(time.time()), "error": None}
            with _PLAN_WRITE_LOCK:
                try:
                    plan = json.load(open(plan_path))
                    for s in plan["scenes"]:
                        if s["i"] == scene_i:
                            s["status"] = "fertig"; s["file"] = fn
                            s["source_url"] = urls[0]; s["source_url_ts"] = int(time.time())
                    _atomic_write_json(plan_path, plan, ensure_ascii=False, indent=1)
                except: pass
            return
        if state == "fail":
            JOBS[job_id] = {"status": "error", "progress": 0,
                            "error": f"KIE fehlgeschlagen: {info.get('failMsg','unbekannt')}", "ts": time.time()}
            _mark_scene_error(plan_path, scene_i)
            return
    print(f"  [KIE] {job_id} Timeout nach {IMAGE_JOB_MAX_POLLS*3}s — gebe auf", flush=True)
    JOBS[job_id] = {"status": "error", "progress": 0, "error": f"KIE Timeout (>{IMAGE_JOB_MAX_POLLS*3}s)", "ts": time.time()}
    _mark_scene_error(plan_path, scene_i)


# Refactor Phase 3: nach workers/batch.py verschoben (lazy `import dashboard` für
# die verbliebenen God-Modul-Helfer, siehe workers/__init__.py). Re-Export hält den
# bestehenden threading.Thread(target=_batch_generate_worker, ...)-Call-Site sowie
# den direkten Aufruf in _produce_worker unverändert lauffähig.
from workers.batch import run as _batch_generate_worker  # noqa: F401


def _veo_job_worker(job_id: str, task_id: str, scene: dict,
                    out_path: str, plan_path: str, cid: str, vid: str, video_prompt: str, chain_len: int = 0):
    """Background thread: polls Veo, downloads video, mixes audio, updates plan."""
    print(f"  [Veo] Worker {job_id} / task {task_id} gestartet", flush=True)
    JOBS[job_id] = {"status": "running", "progress": 20, "file": None, "error": None}

    poll = poll_veo(task_id, timeout=600)
    if not poll["ok"]:
        JOBS[job_id] = {"status": "error", "progress": 0, "error": poll["error"], "ts": time.time()}
        return

    JOBS[job_id]["progress"] = 80
    video_url = poll["video_url"]
    i = scene["i"]
    fn_silent = os.path.join(v_out(cid, vid), f"{i:03d}_veo_silent.mp4")
    fn_final  = os.path.basename(out_path)

    # Download
    try:
        dl_req = urllib.request.Request(video_url,
            headers={"Referer": "https://kie.ai/", "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(dl_req, timeout=120) as vr:
            open(fn_silent, "wb").write(vr.read())
    except Exception as e:
        JOBS[job_id] = {"status": "error", "progress": 0, "error": f"Download: {e}", "ts": time.time()}
        return

    # Audio mix
    has_audio = False
    try:
        audio_meta = json.load(open(v_audio(cid, vid)))
        audio_path = audio_meta.get("path", "")
        if os.path.exists(audio_path):
            import subprocess
            start = float(scene.get("start", 0))
            dur   = float(scene.get("dur", 8))
            result = subprocess.run([
                "ffmpeg", "-y",
                "-i", fn_silent,
                "-ss", str(start), "-t", str(dur), "-i", audio_path,
                "-map", "0:v", "-map", "1:a",
                "-c:v", "copy", "-c:a", "aac", "-shortest", out_path
            ], capture_output=True, timeout=60)
            if result.returncode == 0:
                os.remove(fn_silent)
                has_audio = True
    except Exception as e:
        print(f"  [Veo] Audio-Mix übersprungen: {e}", flush=True)
    if not has_audio:
        os.replace(fn_silent, out_path)

    # Update plan
    with _PLAN_WRITE_LOCK:
        try:
            plan = json.load(open(plan_path))
            for s in plan["scenes"]:
                if s["i"] == i:
                    s["video_file"] = fn_final
                    s["video_prompt"] = video_prompt
                    s["veo_task_id"] = task_id
                    s["chain_len"] = chain_len
            _atomic_write_json(plan_path, plan, ensure_ascii=False, indent=1)
        except Exception as e:
            print(f"  [Veo] Plan-Update-Fehler: {e}", flush=True)

    JOBS[job_id] = {"status": "done", "progress": 100,
                    "file": fn_final, "video_prompt": video_prompt,
                    "ts": int(time.time()), "error": None}
    print(f"  [Veo] Szene {i} fertig → {fn_final} (audio={'ja' if has_audio else 'nein'})", flush=True)


# ---------- Auto-Rendering (reines FFmpeg — kein MoviePy/Remotion/Node) ----------
# Bild-Modus only: nimmt die bereits generierten Standbilder (generated/NNN.jpg) und
# schneidet sie mit Ken-Burns-Bewegung zu einem fertigen Video mit durchgehendem
# Voiceover zusammen. Bewegtbild-Erzeugung (Veo/Grok) bleibt komplett unangetastet —
# dieser Renderer arbeitet ausschließlich auf bereits fertigen Standbildern.


# Refactor Phase 3: nach workers/render.py verschoben (lazy `import dashboard` für
# die verbliebenen God-Modul-Helfer, siehe workers/__init__.py). Re-Export hält den
# bestehenden threading.Thread(target=_render_worker, ...)-Call-Site sowie den
# direkten Aufruf in _produce_worker unverändert lauffähig.
from workers.render import run as _render_worker  # noqa: F401
def _preserve_rendered_scenes(prev_scenes: dict, scenes: list) -> int:
    """Carries file/status/source_url/source_url_ts over from a previous plan's scenes
    into a freshly-built `scenes` list, matched by normalized scene TEXT (not index —
    indices shift whenever the scene count changes between the old and new plan, but
    the text of an unchanged scene doesn't).

    Juli 2026 Fix (Audit A5, "Voiceover-Neugenerierung verwaist gerenderte Bilder"):
    this logic originally only existed inline in _plan_generate_worker (the manual-
    script path). _transcribe_generate_worker (the ElevenLabs-voiceover path) rebuilds
    plan.json from scratch on every voiceover regenerate/resume and had NO equivalent —
    every scene's `file`/`source_url` got reset to None even though the actual images on
    disk were correctly left untouched (see the `is_elevenlabs` branch above that skips
    deleting files). The plan.json → disk link was the only thing that broke; extracting
    this into a shared helper lets both workers use the identical, already-proven
    text-matching heuristic instead of drifting into two slightly different behaviors.

    Mutates `scenes` in place. Returns how many scenes were preserved.
    """
    def _norm_text(t):
        return " ".join((t or "").lower().split())
    preserved = 0
    if not prev_scenes:
        return preserved
    new_by_text = {}
    for s in scenes:
        nt = _norm_text(s.get("text", ""))
        if nt:
            new_by_text.setdefault(nt, []).append(s)
    for _i, prev in prev_scenes.items():
        nt = _norm_text(prev.get("text", ""))
        candidates = new_by_text.get(nt, [])
        if not candidates:
            continue
        ns = candidates.pop(0)
        ns["file"] = prev.get("file")
        ns["status"] = prev.get("status", "fertig")
        ns["source_url"] = prev.get("source_url")
        ns["source_url_ts"] = prev.get("source_url_ts")
        preserved += 1
    return preserved


# Refactor Phase 3: nach workers/plan.py verschoben (lazy `import dashboard` für die
# verbliebenen God-Modul-Helfer, siehe workers/__init__.py). Re-Export hält den
# bestehenden threading.Thread(target=_plan_generate_worker, ...)-Call-Site unverändert.
from workers.plan import run as _plan_generate_worker  # noqa: F401


# ---------- Phase 4.5: Ein-Knopf-Orchestrator ----------
# Kein neuer fachlicher Baustein -- verkettet nur die drei bereits einzeln getesteten
# Jobs (Plan/Transkription -> Bilder -> Rendern) hintereinander in einem einzigen
# Hintergrund-Thread, exakt dasselbe Server-seitige Job-Muster wie BATCH_JOBS/
# RENDER_JOBS/PLAN_JOBS. Jede Etappe ruft dieselbe Worker-Funktion auf wie ihr eigener
# bestehender Einzel-Button -- kein Zusatzrisiko, keine neue fachliche Logik.
# {(cid, vid): {"running": bool, "stage": str, "stop_requested": bool, "error": str|None,
#               "file": str|None}}
PRODUCE_JOBS: dict = {}
_PRODUCE_JOBS_LOCK = threading.Lock()


# Refactor Phase 3: nach workers/produce.py verschoben (lazy `import dashboard` für
# die verbliebenen God-Modul-Helfer, siehe workers/__init__.py). Re-Export hält den
# bestehenden threading.Thread(target=_produce_worker, ...)-Call-Site unverändert.
# PRODUCE_JOBS/_PRODUCE_JOBS_LOCK bleiben hier -- sie werden auch von
# _request_stop_all_running_jobs/_cleanup_stale_jobs sowie den /api/produce_*-Routen
# direkt gebraucht, nicht nur vom Worker selbst.
from workers.produce import run as _produce_worker  # noqa: F401


def gen_image(scene_prompt, master, out_path, char_refs=None):
    """Synchronous image generation — used only for charsheets.

    July 2026 (User-Report: "charsheets sehen für unterschiedliche Kanäle anders aus"):
    We extract image_data_url from each char_ref and pass them as ref_urls to
    _kie_submit_image so KIE actually sees the style reference. Before this fix,
    char_refs were only used as TEXT in the prompt (via _build_image_prompt → filter),
    but the visual style-anchor image never reached KIE. KIE rendered charsheets in a
    generic style (or stick figures if the prompt asked for them).
    """
    full_prompt = _build_image_prompt(scene_prompt, master, char_refs)
    # Extract image URLs from char_refs for KIE's image_input field. Keep only
    # entries that have a real data-URL or http(s) URL.
    ref_urls = None
    if char_refs:
        urls = []
        for cr in char_refs:
            url = cr.get("image_data_url") if isinstance(cr, dict) else None
            if url and isinstance(url, str) and url.startswith(("data:image/", "http://", "https://")):
                urls.append(url)
        if urls:
            ref_urls = urls
    try:
        task_id = _kie_submit_image(full_prompt, ref_urls=ref_urls)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    poll_url  = f"{KIE_API}/recordInfo?taskId={task_id}"
    poll_hdrs = {"Authorization": f"Bearer {kie_key()}"}
    for _ in range(80):
        time.sleep(3)
        try:
            with urllib.request.urlopen(urllib.request.Request(poll_url, headers=poll_hdrs), timeout=15) as r2:
                info = json.load(r2).get("data", {})
        except Exception as e:
            print(f"  [KIE] Poll-Fehler: {e}", flush=True); continue
        state = info.get("state", "")
        if state == "success":
            try:    urls = json.loads(info.get("resultJson", "{}")).get("resultUrls", [])
            except: urls = []
            if not urls: return {"ok": False, "error": "KIE: kein Bild in resultUrls"}
            try:
                dl_req = urllib.request.Request(urls[0],
                    headers={"Referer": "https://kie.ai/", "User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(dl_req, timeout=60) as img_r:
                    open(out_path, "wb").write(img_r.read())
            except Exception as e:
                return {"ok": False, "error": f"Bild-Download fehlgeschlagen: {e}"}
            return {"ok": True, "file": os.path.basename(out_path), "ts": int(time.time()), "source_url": urls[0]}
        if state == "fail":
            return {"ok": False, "error": f"KIE fehlgeschlagen: {info.get('failMsg','unbekannt')}"}
    return {"ok": False, "error": "KIE Timeout (>4 min)"}



# _multipart_upload, get_public_charsheet_url, upload_image_public, KIE_UPLOAD_URL
# jetzt in engine/imagegen.py (Evaluation Juli 2026, Änderung 1+2), re-exportiert oben.


# T2V-Mindest-Promptlänge. HINWEIS: Der gesamte T2V-Pfad (make_t2v_prompt +
# /api/preview_t2v_prompt + /api/generate_t2v + UI-Mode) soll laut Plan-Phase 39
# entfernt werden ("T2V raus / I2V rein"). Bis dahin verhindert diese Konstante
# den NameError (war nie definiert, Regression-Fund 2026-07).
VIDEO_PROMPT_MIN_LEN = 150

T2V_PROMPT_SYSTEM = """\
You are a video scene director. Your job: take ONE narrator line and write a precise AI video generation prompt that makes that line VISIBLE — no more, no less.

RULE #1 — VISUAL LITERALISM:
The narrator's words are your blueprint. Every key concept must become something physically visible:
- "I made a mistake" → character stumbles, drops something, freezes
- "most people give up" → character walks to edge, stops mid-step, shoulders drop
- "suddenly everything changed" → character reacts with whole body, environment shifts
- "here is the secret" → character leans forward, points to something small in frame
- "this took me 5 years" → character stands while tally marks accumulate beside them

RULE #2 — STRUCTURE:
1. CONCEPT: what does the character physically DO to show this idea?
2. ACTION: precise body movement, gesture, pose — no speaking
3. PROPS: simple objects or abstract symbols that support the concept
4. CAMERA: one deliberate move → zoom-in: revelation | zoom-out: scale | pan-right: progress | pan-left: past | static: tension
5. STYLE: use exactly the style from the CHARACTER CONTEXT — do not add or change anything

OUTPUT: ONE dense paragraph, 55-80 words. Start with the action. Do not mention the narration text.\
"""

def make_t2v_prompt(scene_text: str, scene_i: int, total_scenes: int,
                    video_master: str, prev_scene_prompts: list,
                    full_script: str = "") -> str:
    """Generate a T2V scene prompt that visually illustrates exactly what the narrator says,
    grounded in the actual subject of the script (not a generic abstract interpretation)."""
    story_phase = (
        "opening"         if scene_i < total_scenes * 0.15 else
        "rising action"   if scene_i < total_scenes * 0.50 else
        "climax"          if scene_i < total_scenes * 0.75 else
        "resolution"
    )

    prev_context = ""
    if prev_scene_prompts:
        recent = prev_scene_prompts[-2:]
        prev_context = (
            "VISUAL CONTINUITY — previous scenes showed:\n" +
            "\n".join(f"  • {p[:100]}" for p in recent) +
            "\nReuse the same visual representation for any recurring entity shown above."
        )

    script_context = (
        f"FULL SCRIPT (read this first to understand the real subject — people, places, "
        f"organizations, technologies named in it):\n{full_script.strip()[:4000]}\n\n"
        if full_script else ""
    )

    user_msg = (
        f"{script_context}"
        f"NARRATOR SAYS RIGHT NOW (scene {scene_i+1}/{total_scenes}, story phase: {story_phase}):\n"
        f'"{scene_text}"\n\n'
        f"CHARACTER & STYLE CONTEXT (style only):\n{video_master.strip()[:3000]}\n\n"
        + (f"{prev_context}\n\n" if prev_context else "") +
        "Write the video prompt for THIS line. If it names a concrete person, place, "
        "organization, or technology, show that thing (or a clear visual stand-in) — do not "
        "default to an abstract gesture unless the line truly has no concrete referent.\n\n"
        f"The prompt must be at least {VIDEO_PROMPT_MIN_LEN} characters and explicitly name: "
        "(1) the concrete main subject, (2) the setting/location, (3) a lighting mood, "
        "(4) the camera angle/shot size. A vague mood description without these four "
        "elements is not acceptable."
    )
    def _call():
        result = post_gemini_native([
            {"role": "system", "content": T2V_PROMPT_SYSTEM},
            {"role": "user",   "content": user_msg},
        ], temp=0.40)
        return result.strip()
    try:
        result = _call()
        if len(result) < VIDEO_PROMPT_MIN_LEN:
            print(f"  [T2V] Prompt zu kurz ({len(result)} Zeichen) — ein Retry …", flush=True)
            retry = _call()
            if len(retry) > len(result):
                result = retry
        return result
    except Exception:
        return (
            f"Scene illustrating: {scene_text[:80]}. "
            f"Camera slow zoom-in, {story_phase} pacing."
        )

VEO_API = "https://api.kie.ai/api/v1/veo"
MAX_CHAIN_LENGTH = 4  # max consecutive extends before forcing a fresh anchor shot

def gen_veo(video_prompt: str, image_urls: list | None = None,
            generation_type: str = "TEXT_2_VIDEO",
            model: str = "veo3_lite",
            resolution: str = "1080p",
            duration: int = 8) -> dict:
    """Submit Veo 3.1 job. Returns {ok, task_id} or {ok:False, error}."""
    hdrs = {"Authorization": f"Bearer {kie_key()}", "Content-Type": "application/json"}
    body = {
        "prompt":           video_prompt,
        "model":            model,
        "generationType":   generation_type,
        "aspect_ratio":     "16:9",
        "resolution":       resolution,
        "enableTranslation": True,
    }
    # REFERENCE_2_VIDEO always uses duration=8 (only supported value)
    if generation_type != "REFERENCE_2_VIDEO":
        body["duration"] = max(4, min(8, duration))
    if image_urls:
        body["imageUrls"] = image_urls
    try:
        req = urllib.request.Request(f"{VEO_API}/generate",
                                     data=json.dumps(body).encode(), headers=hdrs)
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"Veo HTTP {e.code}: {e.read().decode()[:300]}"}
    if resp.get("code") != 200:
        return {"ok": False, "error": f"Veo: {resp.get('msg', str(resp))}"}
    return {"ok": True, "task_id": resp["data"]["taskId"]}

def extend_veo(task_id: str, prompt: str) -> dict:
    """Extend an existing Veo task with a new prompt — continues from the last frame.
    Only works on 720p (non-finalized 1080p) source tasks. Returns {ok, task_id}."""
    hdrs = {"Authorization": f"Bearer {kie_key()}", "Content-Type": "application/json"}
    body = {"taskId": task_id, "prompt": prompt}
    try:
        req = urllib.request.Request(f"{VEO_API}/extend",
                                     data=json.dumps(body).encode(), headers=hdrs)
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"Veo Extend HTTP {e.code}: {e.read().decode()[:300]}"}
    if resp.get("code") != 200:
        return {"ok": False, "error": f"Veo Extend: {resp.get('msg', str(resp))}"}
    return {"ok": True, "task_id": resp["data"]["taskId"]}

def poll_veo(task_id: str, timeout: int = 600) -> dict:
    """Poll Veo until done.
    successFlag=1 → 1080p ready. originUrls stable for 2 polls → use 720p, don't wait longer.
    """
    hdrs = {"Authorization": f"Bearer {kie_key()}"}
    deadline = time.time() + timeout
    last_origin_url = None
    origin_stable_count = 0
    while time.time() < deadline:
        time.sleep(10)
        try:
            req = urllib.request.Request(
                f"{VEO_API}/record-info?taskId={task_id}", headers=hdrs)
            with urllib.request.urlopen(req, timeout=15) as r:
                info = json.load(r)
            code = info.get("code")
            data = info.get("data") or {}
            resp = data.get("response") or {}
            flag = data.get("successFlag")
            origin = (resp.get("originUrls") or [None])[0]
            result = (resp.get("resultUrls") or [None])[0]
            has_audio = bool((resp.get("hasAudioList") or [False])[0])
            elapsed = int(timeout - (deadline - time.time()))
            print(f"  [Veo] {elapsed}s flag={flag} result={bool(result)} origin={bool(origin)}", flush=True)
            if data.get("errorCode"):
                return {"ok": False, "error": f"Veo: {data.get('errorMessage','unbekannt')}"}
            # 1080p fertig
            if code == 200 and flag == 1:
                url = result or origin
                if url:
                    return {"ok": True, "video_url": url, "has_audio": has_audio}
            # originUrl stabil → 720p-Video fertig, nicht weiter auf 1080p warten
            if origin:
                if origin == last_origin_url:
                    origin_stable_count += 1
                else:
                    origin_stable_count = 1
                    last_origin_url = origin
                if origin_stable_count >= 2:
                    print(f"  [Veo] originUrl stabil → nutze 720p", flush=True)
                    return {"ok": True, "video_url": origin, "has_audio": has_audio}
            else:
                origin_stable_count = 0
        except Exception as e:
            print(f"  [Veo] poll error: {e}", flush=True)
    # Timeout: if we have originUrl (base quality), return it rather than failing
    if last_origin_url:
        print("  [Veo] 1080p timeout — using originUrl (720p)", flush=True)
        return {"ok": True, "video_url": last_origin_url, "has_audio": False}
    return {"ok": False, "error": "Veo Timeout — kein Video erhalten"}
    return {"ok": False, "error": "Timeout"}


def make_video_prompt(scene_text: str, char_desc: str) -> str:
    """Generate an image-to-video animation prompt for the grok image-to-video model.
    Uses the character master prompt for style context, not hardcoded style strings."""
    try:
        user_msg = (
            f"CHARACTER CONTEXT:\n{char_desc}\n\n"
            f"NARRATOR LINE:\n\"{scene_text}\"\n\n"
            "Write a short (30-50 word) animation prompt describing how the character in the image "
            "should MOVE to illustrate this line. Focus only on motion, gesture, and camera — "
            "not on describing the character's appearance (that's already in the image)."
        )
        return post_kie_text([{"role": "user", "content": user_msg}], temp=0.5).strip()
    except Exception:
        return f"Animate character to illustrate: {scene_text[:80]}. Smooth deliberate movement."


def gen_video(scene_url: str, video_prompt: str, duration: int = 6, style_ref_url: str = "") -> dict:
    """Submit KIE image-to-video job and return {ok, file_url, error}."""
    ref_url = style_ref_url if style_ref_url else scene_url
    prompt = video_prompt
    hdrs = {"Authorization": f"Bearer {kie_key()}", "Content-Type": "application/json"}
    body = {
        "model": "grok-imagine/image-to-video",
        "input": {
            "prompt":       prompt,
            "image_urls":   [ref_url],
            "duration":     duration,
            "resolution":   "720p",
            "aspect_ratio": "16:9",
            "mode":         "normal",
        },
    }
    try:
        req = urllib.request.Request(f"{KIE_API}/createTask",
                                     data=json.dumps(body).encode(), headers=hdrs)
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"KIE HTTP {e.code}: {e.read().decode()[:200]}"}

    if resp.get("code") != 200:
        return {"ok": False, "error": f"KIE: {resp.get('msg', str(resp))}"}

    task_id = resp["data"]["taskId"]
    print(f"  [Video] Task {task_id} gestartet …", flush=True)

    poll_url  = f"{KIE_API}/recordInfo?taskId={task_id}"
    poll_hdrs = {"Authorization": f"Bearer {kie_key()}"}
    for _ in range(120):  # max ~6 min
        time.sleep(3)
        try:
            with urllib.request.urlopen(urllib.request.Request(poll_url, headers=poll_hdrs), timeout=15) as r2:
                info = json.load(r2).get("data", {})
        except Exception as e:
            print(f"  [Video] Poll-Fehler: {e}", flush=True); continue

        state = info.get("state", "")
        print(f"  [Video] {state} ({info.get('progress', 0)}%)", flush=True)

        if state == "success":
            try:
                video_urls = json.loads(info.get("resultJson", "{}")).get("resultUrls", [])
            except Exception:
                video_urls = []
            if not video_urls:
                return {"ok": False, "error": "KIE: kein Video in resultUrls"}
            return {"ok": True, "video_url": video_urls[0]}

        if state == "fail":
            return {"ok": False, "error": f"KIE fehlgeschlagen: {info.get('failMsg', 'unbekannt')}"}

    return {"ok": False, "error": "KIE Video Timeout (>6 min)"}


# ---------- Phase 3: lokale Wort-Timestamps via faster-whisper ----------
# Läuft in einer isolierten venv (.venv_whisper/), per subprocess aufgerufen --
# genau wie ffmpeg ein externes Binary ist, bleibt dashboard.py selbst
# stdlib-only. Ersetzt den ursprünglich geplanten ElevenLabs-Scribe-Weg (über
# KIE live getestet, Task blieb dauerhaft auf "waiting" haengen, siehe
# ARCHITECTURE.md Abschnitt 16). Gemini (oben, transcribe_and_segment) bleibt
# für die grobe Story-Segmentierung zuständig -- es liefert nur keine
# verlässlichen Wort-Zeitstempel, dafür ist dieser Pfad da.
WHISPER_VENV_PY = os.path.join(HERE, ".venv_whisper", "bin", "python3")
WHISPER_SCRIPT = os.path.join(HERE, "whisper_transcribe.py")


def transcribe_words_whisper(audio_path, language=None):
    """Lokale Wort-Timestamp-Transkription. Gibt
    {"text","language","language_probability","words":[{"word","start","end"}]} zurück."""
    if not os.path.exists(WHISPER_VENV_PY):
        raise RuntimeError(
            "Whisper-venv fehlt (.venv_whisper/) -- einmalig einrichten: "
            "python3 -m venv .venv_whisper && ./.venv_whisper/bin/pip install faster-whisper"
        )
    args = [WHISPER_VENV_PY, WHISPER_SCRIPT, audio_path, language or "auto"]
    result = subprocess.run(args, capture_output=True, text=True, timeout=900)
    if result.returncode != 0:
        raise RuntimeError(f"Whisper-Transkription fehlgeschlagen: {result.stderr[-2000:]}")
    data = json.loads(result.stdout)
    if "error" in data:
        raise RuntimeError(data["error"])
    return data


def align_scenes_to_whisper(scenes: list, whisper_words: list) -> None:
    """Sets `start_aligned`/`end_aligned` on each scene in place, by sequentially
    consuming `len(scene["text"].split())` words per scene from the Whisper word list.

    Gemini's per-scene `text` and Whisper's word list are both transcriptions of the
    SAME audio in the SAME order, so word-count-based advancement is enough — no fuzzy
    text matching needed. This also tolerates the two engines disagreeing on individual
    words (e.g. Whisper hearing "Wortszeitstempel" where Gemini heard "Wort-Zeitstempel")
    since only the word COUNT is used to advance the pointer, never the text itself.

    Round-5 Fix-5: word-count mismatch is a real failure mode — Whisper hears
    Füllwörter / Halluzinationen / Halluzinationen am Anfang ('[music]'), Gemini
    optimiert sie weg. Wenn `wi >= n` vor der letzten Scene erreicht wird, kriegen
    der Rest KEIN `start_aligned`/`end_aligned` und fallen auf geschätzte `dur`-Werte
    zurück → stille Sync-Drift am Übergang aligned ↔ unaligned. Erkennen + loggen
    wenn Summe drastisch von n abweicht (User-Signal: „Audio ist wahrscheinlich
    off-tone, plan.json prüfen")."""
    wi = 0
    n = len(whisper_words)
    total_scene_words = sum(len(s.get("text", "").split()) for s in scenes)
    # Detect mismatch upfront — >20% gap means one engine inflated/dropped words vs. the
    # other. The user can then re-record the audio or live with partial alignment.
    if n > 0 and total_scene_words > 0:
        drift_ratio = abs(n - total_scene_words) / max(n, total_scene_words)
        if drift_ratio > 0.20:
            print(f"  [Whisper] WARNUNG: word-count mismatch — Gemini-Scenes={total_scene_words}, "
                  f"Whisper-Words={n} (Δ={drift_ratio*100:.0f}%). Letzte {sum(1 for s in scenes if s.get('start_aligned') is None)} "
                  f"Szenen bekommen kein aligned-Start → Sync-Drift wahrscheinlich.", flush=True)
    for s in scenes:
        words_in_scene = len(s.get("text", "").split())
        if words_in_scene == 0 or wi >= n:
            continue
        start_idx = wi
        end_idx = min(wi + words_in_scene, n) - 1
        s["start_aligned"] = whisper_words[start_idx]["start"]
        s["end_aligned"] = whisper_words[end_idx]["end"]
        # Cinematic-Mix Juli 2026 (Schritt 3, 1-Wort-Captions): Wort-Slices scene-
        # relativ ablegen (Offset zu start_aligned, NICHT zu dur/frames -- gleiche
        # Konvention wie Phase O's accent_t, das ebenfalls direkt gegen start_aligned/
        # end_aligned rechnet ohne Neuskalierung auf den gerundeten Frame-Takt). Der
        # Renderer clippt/rundet beim Overlay-Fenster ohnehin defensiv auf clip_dur.
        s["words"] = [
            {"word": whisper_words[k]["word"],
             "start": whisper_words[k]["start"] - s["start_aligned"],
             "end": whisper_words[k]["end"] - s["start_aligned"]}
            for k in range(start_idx, end_idx + 1)
        ]
        wi = end_idx + 1


# ---------- Pausen-Kürzung (auf Wunsch des Nutzers, nach Phase 3) ----------
# Nutzt genau die Whisper-Wort-Zeitstempel, die Phase 3 ohnehin schon berechnet -- die
# Lücke zwischen Wort N Ende und Wort N+1 Start IST die Sprechpause. Nur der Teil einer
# Pause, der über MAX_PAUSE_SEC hinausgeht, wird herausgeschnitten -- ein kurzer,
# natürlicher Atem-Abstand bleibt erhalten, nur die toten, langen Stellen (z.B. 2-3s
# zwischen Sätzen in einem 8-Minuten-Voiceover) verschwinden.
MAX_PAUSE_SEC = 0.3


def _strip_pause_tokens(words: list) -> list:
    """Entfernt Wort-Timestamp-Einträge, deren Text NUR aus Punkten besteht (".", "..",
    "...", "…") — genau die Marker, die `_enrich_for_tts` einfügt.

    Juli 2026 Fix (verifiziert an echten Daten): `_enrich_for_tts` (engine_elevenlabs.py)
    fügt vor dem ElevenLabs-Call "..."-Pausen-Marker in den Text ein, um der TTS eine
    natürlichere Betonung/Atempause zu geben — an einem echten Theranos-Skript maß ich
    919 Roh-Wörter → 1000 "Wörter" nach Enrichment (81 zusätzliche "..."-Tokens).
    ElevenLabs liefert für jeden dieser Marker einen eigenen Zeitstempel zurück, der
    Server persistiert die komplette Wortliste inkl. dieser Tokens in
    voiceover_word_timestamps.

    `align_scenes_to_whisper` zählt aber `len(scene["text"].split())` Wörter aus dem
    ROH-Skript (ohne "...") und konsumiert die Wort-Timestamp-Liste sequenziell in
    dieser Zählung — jedes ungezählte "..."-Token verschiebt den Lesekopf um eins,
    OHNE dass eine Szene dafür "verantwortlich" ist. Bei 81 Tokens über ein 8-Minuten-
    Voiceover verteilt lief der Zähler am Ende leer (`wi >= n`), bevor die letzten
    Szenen ihr `start_aligned` bekamen → geschätztes Timing statt echtem → Schnitt
    driftet. Diese Funktion entfernt die Phantom-Tokens VOR dem Alignment, sodass die
    Wortzahl wieder mit dem Roh-Skript übereinstimmt.

    Bewusst NUR reine Punkt-Tokens, nicht "jedes Token ohne alphanumerisches Zeichen" —
    ein breiterer Filter würde auch eigenständige Satzzeichen-Wörter treffen, die schon
    im ROH-Skript als eigenes `.split()`-Token stehen (z.B. ein freistehendes "—" oder
    "/", an echten Skripten beobachtet: "Silicon Valley — and..." zählt "—" als eigenes
    Wort). Die würden dann auf BEIDEN Seiten (Wortliste UND Szenentext) mitgezählt und
    blieben im Gleichgewicht — sie rauszufiltern hätte genau das Off-by-one-Problem
    reproduziert, das dieser Fix beheben soll, nur seltener. "..." dagegen kommt NIE aus
    dem Original-Skript, sondern ausschließlich aus dem Enrichment — daher der enge,
    literale Filter statt eines allgemeinen "keine Buchstaben"-Musters. Whisper-
    Transkripte enthalten "..."-Tokens nie → dort ein No-Op.
    """
    return [w for w in words if not re.fullmatch(r"\.+|…+", w.get("word", "").strip())]


# Sicherheitsabstand vor dem nächsten Wort. Der Schnitt endete früher EXAKT auf dessen
# start-Zeitstempel — und der kommt vom Aligner, nicht aus einer Stille-Messung. Liegt er
# auch nur Millisekunden zu spät (Plosive wie "p"/"t"/"k" starten mit einer stimmlosen
# Verschlussphase, die der Aligner gern verschluckt), wird der Wortanlaut weggeschnitten.
# Das ist der "als wären sie abgeschnitten"-Effekt, den der User über das ganze Voiceover
# hört: bei 124 Schnitten reicht eine kleine systematische Ungenauigkeit.
PAUSE_GUARD_SEC = 0.04


def _compute_pause_trims(words: list, max_pause: float = MAX_PAUSE_SEC) -> list:
    """Returns [(trim_start, trim_end), ...] -- the EXCESS portion of every gap between
    consecutive words that's longer than max_pause.

    Der Schnitt hört PAUSE_GUARD_SEC VOR dem nächsten Wort auf, nicht exakt auf dessen
    Start — siehe Konstante. Ein Intervall, das dadurch auf <= 0 schrumpft, wird gar nicht
    geschnitten (die Pause ist dann ohnehin kaum länger als erlaubt)."""
    trims = []
    for i in range(len(words) - 1):
        gap_start = words[i]["end"]
        gap_end = words[i + 1]["start"]
        if gap_end - gap_start > max_pause:
            cut_start = gap_start + max_pause
            cut_end = gap_end - PAUSE_GUARD_SEC
            if cut_end - cut_start > 0.01:      # unter 10ms lohnt der Schnitt nicht
                trims.append((cut_start, cut_end))
    return trims


def _trim_audio_pauses(audio_path: str, trims: list, out_path: str) -> None:
    """Cuts the given (start,end) intervals out of audio_path via ffmpeg's atrim+concat,
    producing out_path with those silent stretches removed. Lossless WAV intermediate --
    this is consumed immediately by the render pipeline, not user-facing, so no reason
    to re-encode voice audio through a lossy codec twice."""
    if not trims:
        shutil.copy(audio_path, out_path)
        return
    audio_duration = _clip_duration_sec(audio_path)
    keep_intervals, cursor = [], 0.0
    for (a, b) in trims:
        if a > cursor:
            keep_intervals.append((cursor, a))
        cursor = b
    if cursor < audio_duration:
        keep_intervals.append((cursor, audio_duration))

    # Mikro-Blenden an jeder Schnittkante (8ms). Ohne sie stößt `concat` zwei Wellenform-
    # Punkte mit beliebig unterschiedlicher Amplitude/Phase hart aneinander — jeder Sprung
    # ist ein hörbarer Klick. Bei 124 Schnitten über ein 8-Minuten-Voiceover ergibt das
    # genau die "Sound-Laggs", die der User berichtet.
    # 8ms sind als Lautstärke-Änderung unhörbar, beseitigen den Sprung aber vollständig.
    # DAUER bleibt unverändert (afade skaliert nicht, es dämpft nur) — kritisch, weil die
    # Wort-Timeline über _adjust_words_for_trims aus derselben trims-Liste berechnet wird.
    EDGE_FADE = 0.008
    filter_parts, labels = [], []
    for idx, (s, e) in enumerate(keep_intervals):
        label = f"a{idx}"
        seg_dur = max(0.0, e - s)
        fade = min(EDGE_FADE, seg_dur / 4) if seg_dur > 0 else 0.0
        chain = f"[0:a]atrim=start={s}:end={e},asetpts=PTS-STARTPTS"
        if fade > 0:
            chain += (f",afade=t=in:st=0:d={fade:.4f}"
                      f",afade=t=out:st={max(0.0, seg_dur - fade):.4f}:d={fade:.4f}")
        filter_parts.append(f"{chain}[{label}]")
        labels.append(f"[{label}]")
    filter_complex = ";".join(filter_parts) + f";{''.join(labels)}concat=n={len(labels)}:v=0:a=1[outa]"
    cmd = ["ffmpeg", "-y", "-i", audio_path, "-filter_complex", filter_complex, "-map", "[outa]", out_path]
    result = subprocess.run(cmd, capture_output=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg Pausen-Trimmen fehlgeschlagen: {result.stderr.decode(errors='replace')[-300:]}")


def _adjust_words_for_trims(words: list, trims: list) -> list:
    """Re-expresses each word's start/end on the TRIMMED audio's timeline -- every
    timestamp loses the cumulative duration of every trim interval that ends at or
    before it. A trim interval never falls strictly inside a word's own [start,end]
    (trims only exist inside inter-word silence), so one cumulative offset per word
    is exact for both its start and end."""
    adjusted = []
    for w in words:
        cum = sum(b - a for (a, b) in trims if b <= w["start"])
        adjusted.append({"word": w["word"], "start": w["start"] - cum, "end": w["end"] - cum})
    return adjusted


# ---------- Phase 4.4: Text-Overlays (Untertitel/Callouts/Kapitel-Titel) ----------
# Dieselbe isolierte venv wie Whisper oben, jetzt auch für Pillow -- der installierte
# ffmpeg-Build hat kein freetype/fontconfig kompiliert (`drawtext` daher nicht
# verfügbar; eine Neuinstallation mit ffmpeg-full hätte 47 neue Abhängigkeiten und ein
# Risiko für die bereits getestete Encoder-/Sync-Pipeline bedeutet). Stattdessen: Text
# wird als transparentes PNG per Pillow gerendert, dann per ffmpegs overlay/fade-Filter
# aufs Ken-Burns-Bild gelegt -- beide Filter sind in jedem Standard-Build enthalten.

def transcribe_and_segment(local_path, mime_type, sec_per_img):
    """Transcribe audio via KIE.ai Gemini 2.5 Flash (inline base64 data URI)."""
    with open(local_path, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode()
    instr = (
        f"This is a voice-over narration audio file. Do the following:\n"
        f"1. Transcribe the spoken content verbatim.\n"
        f"2. Segment the transcription into visual beats where each beat covers roughly "
        f"{sec_per_img:.0f} seconds of audio. Group words that belong together visually "
        f"(same topic / same scene). Beats may be slightly shorter or longer for semantic coherence.\n"
        f"3. For each beat provide:\n"
        f"   - start: start time in seconds (float, based on actual audio timing)\n"
        f"   - text: exact spoken words of that beat\n"
        f"Return ONLY a JSON array: [{{'start': 0.0, 'text': '...'}}] — no markdown, no explanation."
    )
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": instr},
        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{audio_b64}"}},
    ]}]
    txt = post_kie_text(msgs, json_mode=True, temp=0.1)
    # strip possible markdown code fences
    txt = re.sub(r"```[a-z]*\n?", "", txt).strip()
    return json.loads(txt)


def _elevenlabs_words_to_beats(words: list, sec: float, audio_duration: float) -> list:
    """Aggregates ElevenLabs word timestamps into scene-level beats [{start, text}]
    using simple time-windowing. Each beat covers ~`sec` seconds of audio; the very
    last beat absorbs whatever's left. This is deliberately less sophisticated than
    segment_by_pacing() on the manual-script side — ElevenLabs path is inherently
    aligned, the LLM-pacing segmentation is a Story-Phase-Engine task (deferred)."""
    if not words or audio_duration <= 0:
        return []
    beats = []
    n = max(1, int(round(audio_duration / sec)))
    for i in range(n):
        w_start = i * sec
        w_end   = (i + 1) * sec if i < n - 1 else audio_duration
        in_window = [w for w in words if w["start"] < w_end and w["end"] > w_start]
        text = " ".join(w["word"] for w in in_window).strip()
        beats.append({"start": round(w_start, 2), "text": text})
    return beats

def _transcribe_generate_worker(cid: str, vid: str, sec: float) -> dict:
    """Audio -> Plan, extracted out of the /api/transcribe HTTP handler so it's a
    standalone function like every other long-running action in this project
    (_plan_generate_worker, _batch_generate_worker, _render_worker) -- lets the
    one-button orchestrator (_produce_worker) call this exact same logic instead of
    duplicating it. Raises on failure; caller decides how to report that (HTTP error
    response vs. PRODUCE_JOBS error field).

    Phase 1 (ElevenLabs): when audio_meta.json carries voiceover_source='elevenlabs'
    + voiceover_word_timestamps, this worker SKIPS the Gemini-transcription call and
    builds scenes from the pre-captured timestamps — the single architectural win of
    using ElevenLabs instead of relying on an LLM to segment the audio. Falls back to
    the original Gemini path for any other (user-uploaded) audio.
    """
    meta = json.load(open(v_audio(cid, vid)))
    is_elevenlabs = (meta.get("voiceover_source") == "elevenlabs"
                     and bool(meta.get("voiceover_word_timestamps")))

    # Clear old generated files ONLY for the Gemini path (real re-transcribe). For the
    # ElevenLabs path we NEVER delete images — they're the user's renders and must
    # survive every ElevenLabs/plan re-run. This is the July 2026 bug-fix: previously
    # the ElevenLabs path deleted images unconditionally, which wiped a full 73-scene
    # render because the user re-triggered ElevenLabs after images finished.
    #
    # Juli 2026 Fix (Audit A5): the file-deletion fix above only solved half the
    # problem. This function ALWAYS rebuilds plan.json from scratch below (every scene
    # gets file=None/status="geplant") — even though the files on disk correctly
    # survive, the JSON's *pointer* to them was destroyed every time a voiceover got
    # regenerated. `19_year_old_fooled_the_world` (from the user's report) is exactly
    # this: images physically present, plan.json showing none of them. Same
    # text-matching heuristic + helper as _plan_generate_worker (_preserve_rendered_scenes),
    # so both plan-rebuilding paths now behave identically. Only applies on the
    # ElevenLabs path — the Gemini path just deleted the files above, so there's
    # nothing valid left to preserve a pointer to.
    prev_scenes = {}
    if is_elevenlabs:
        try:
            prev_plan = json.load(open(v_plan(cid, vid)))
            for ps in prev_plan.get("scenes", []):
                if ps.get("file") and ps.get("status") == "fertig":
                    prev_scenes[ps["i"]] = ps
        except Exception:
            pass
    if not is_elevenlabs:
        out_dir = v_out(cid, vid)
        for f in os.listdir(out_dir):
            if f.endswith((".jpg", ".png", ".mp4")):
                try:
                    os.remove(os.path.join(out_dir, f))
                    print(f"  [Transcribe] Gelösche alte Datei: {f}", flush=True)
                except: pass
    else:
        keep = [f for f in os.listdir(v_out(cid, vid))
                 if f.endswith((".jpg", ".png", ".mp4"))]
        print(f"  [Transcribe] ElevenLabs-Pfad: {len(keep)} Bilder/Videos bleiben "
              f"unangetastet ({len(prev_scenes)} Szene(n) im alten Plan als 'fertig' markiert).", flush=True)

    if is_elevenlabs:
        words = meta["voiceover_word_timestamps"]
        audio_duration = max((w["end"] for w in words), default=0.0)
        beats = _elevenlabs_words_to_beats(words, sec, audio_duration)
        tx(1, f"ElevenLabs: {len(words)} Wörter, {audio_duration:.1f}s Audio …")
        tx(2, f"{len(beats)} Szenen via Zeitfenster (à {sec}s) …")
    else:
        mb = os.path.getsize(meta["path"]) / 1024 / 1024
        tx(1, f"Sende Audio an KIE ({mb:.1f} MB) …")
        beats = transcribe_and_segment(meta["path"], meta["mime"], sec)
        tx(2, f"{len(beats)} Szenen transkribiert — baue Szenen …")

    scenes = []
    for i, b in enumerate(beats):
        dur = (beats[i+1]["start"] - b["start"]) if i+1 < len(beats) else sec
        scenes.append({"i": i, "start": round(float(b["start"]), 1), "dur": round(float(dur), 1),
                       "text": b["text"], "t": fmt_t(float(b["start"])),
                       "file": None, "status": "geplant", "prompt": ""})

    preserved = _preserve_rendered_scenes(prev_scenes, scenes)
    if preserved:
        print(f"  [Transcribe] {preserved} bereits gerenderte Szene(n) erhalten "
              f"(gleicher Text, file+status aus altem Plan übernommen).", flush=True)

    # Whisper-Alignment passiert bewusst NICHT hier, sondern erst in _render_worker
    # (Stage "timing") -- dort liegt so oder so schon das hochgeladene Voice-over vor,
    # UNABHÄNGIG davon ob dieser Plan hier (Audio-Transkription) oder der manuelle
    # Skript-Pfad (_plan_generate_worker) die Szenen-Texte geliefert hat. Ein einziger
    # Alignment-Punkt statt zwei, siehe ARCHITECTURE.md Abschnitt 16.5.
    tx(3, f"Analysiere Story-Struktur ({len(scenes)} Szenen) …")
    analysis = analyze_script([s["text"] for s in scenes])
    # This path's scenes are already 1:1 with the beats just analyzed (no
    # grouping/splitting like the manual-script path) — direct index assignment.
    _apply_visual_sequences_direct(scenes, analysis.get("visual_sequences", []))
    pacing_by_beat = {p.get("beat"): p.get("label") for p in analysis.get("pacing", [])
                      if isinstance(p, dict) and p.get("label") in ("calm", "normal", "punchy")}
    callout_by_beat = {c.get("beat"): c.get("text") for c in analysis.get("callouts", [])
                       if isinstance(c, dict) and c.get("text")}
    for s in scenes:
        s["pacing"] = pacing_by_beat.get(s["i"], "normal")
        if s["i"] in callout_by_beat:
            s["callout"] = callout_by_beat[s["i"]]

    tx(4, "Schreibe Bild-Prompts …")
    prompts = visual_prompts(scenes, analysis)
    prompt_error_scenes = []
    for s, pr in zip(scenes, prompts):
        s["prompt"] = pr["prompt"]; s["concrete_entity"] = pr["concrete_entity"]
        s["secondary_entity"] = pr.get("secondary_entity", "")
        s["prompt_error"] = pr.get("prompt_error", False)
        if s["prompt_error"]:
            prompt_error_scenes.append(s["i"])
    if prompt_error_scenes:
        print(f"  [Plan] WARNUNG: {len(prompt_error_scenes)} Szene(n) mit fehlgeschlagener "
              f"Prompt-Generierung (prompt_error): {prompt_error_scenes} — Prompt-Text vor "
              f"Bild-Generierung manuell prüfen/überschreiben.", flush=True)
    # video_prompt stays empty — only generated on demand per scene, see /api/plan comment
    for s in scenes:
        s["video_prompt"] = ""
    _assign_phases(scenes, analysis, len(scenes))
    # Phase H: derive `speaker` per scene. ⚠ SCAFFOLD ONLY ⚠ — was zur Verfügung steht:
    #   - s["speaker"] default "narrator" (Datenmodell ist da, in plan.json persistiert)
    #   - Detection + Log-Warnung wenn mehrere Speaker erkannt
    #   - Phase-H.2 (pro-Speaker ElevenLabs-Call + ffmpeg-concat) ist NICHT gebaut
    # Konkret: alle Szenen werden aktuell mit dem CHANNEL-DEFAULT-VOICE generiert, egal
    # was s["speaker"] sagt. Das Datenmodell erlaubt User-Manual-Edit in plan.json,
    # aber die Pipeline ignoriert es.
    # Wer das Feature „Multi-Speaker" öffentlich bewirbt, bewirbt etwas das nicht da ist.
    # Phase H bleibt als „Scaffold pass-through deaktiviert" dokumentiert bis H.2 kommt.
    SPEAKER_DEFAULT = "narrator"
    speaker_set = set()
    for s in scenes:
        # Future: derive from character matching in analyze_script. For now, set all to
        # the channel default so the data model is in place; multi-speaker override is
        # a manual edit + a future Phase H worker.
        if "speaker" not in s:
            s["speaker"] = SPEAKER_DEFAULT
        speaker_set.add(s["speaker"])
    if len(speaker_set) > 1:
        # mixed-speaker scripts: future enhancement — for now, surface the gap honestly.
        print(f"  [Phase H] WARNUNG: {len(speaker_set)} distinct speakers erkannt "
              f"({sorted(speaker_set)}). Aktueller ElevenLabs-Pfad generiert alle "
              f"Szenen mit dem Channel-Default-Voice. Multi-Speaker-Pipeline ist ein "
              f"follow-up (Plan §H.2). Edit s['speaker'] in plan.json manuell wenn "
              f"du jetzt verschiedene Stimmen willst.", flush=True)

    tx(4, f"Fertig — {len(scenes)} Szenen bereit ✓")

    out = {
        "scenes": scenes,
        "sec": sec,
        "source": "elevenlabs" if is_elevenlabs else "audio",
        "voiceover_source": meta.get("voiceover_source", ""),
        "voiceover_task_id": meta.get("voiceover_task_id"),
        "voiceover_word_timestamps": meta.get("voiceover_word_timestamps") if is_elevenlabs else None,
        "characters": analysis.get("characters", []),
    }
    _atomic_write_json(v_plan(cid, vid), out, ensure_ascii=False, indent=1)
    return out


# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Das Dashboard ist eine einzige HTML-Datei mit dem gesamten JS inline. Ohne
        # Cache-Header darf der Browser sie beliebig lange wiederverwenden — dann läuft
        # nach einem Bugfix weiter der ALTE Code, und der Fix sieht aus als wirke er
        # nicht. Bei einem lokalen Dev-Dashboard ist Caching ohnehin wertlos.
        if ctype.startswith("text/html"):
            self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        # HEAD-Requests dürfen laut HTTP-Spec keinen Body senden — sonst
        # kann der Browser den Body nicht zuverlässig vom Content-Length abgrenzen
        # und verschiedene Clients (curl, Python urllib, manche Browser) zeigen
        # dann merkwürdiges Verhalten.
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_HEAD(self):
        # BaseHTTPRequestHandler leitet HEAD-Requests nicht automatisch auf do_GET
        # weiter — wir mappen manuell damit /api/voiceover_file per HEAD (für
        # Player-Refresh ohne Body-Download) abrufbar ist.
        self.do_GET()

    def do_GET(self):
        p = urlparse(self.path).path
        qs = parse_qs(urlparse(self.path).query)
        cid = qs.get("channel", ["default"])[0]
        vid = qs.get("video", [""])[0]
        # Shorts/Upload/Control-Erweiterung: erster Prefix-Treffer (shorts./youtube./
        # control.-api.py) sendet selbst per handler._send(...) und liefert (True, None);
        # ohne Treffer läuft die bestehende Kette unten unverändert weiter.
        handled, _ = dispatch("GET", p, self, qs, cid, vid, None)
        if handled:
            return
        if p == "/":
            return self._send(200, open(os.path.join(HERE, "dashboard.html"), encoding="utf-8").read(), "text/html; charset=utf-8")
        # Shorts/Upload/Control-Erweiterung: eigene, kleine statische Seite -- NICHT ins
        # bestehende dashboard.html gemischt, da kanal-/video-übergreifend statt pro-Video.
        if p == "/control":
            return self._send(200, open(os.path.join(HERE, "control.html"), encoding="utf-8").read(), "text/html; charset=utf-8")
        # Phase 38: Stil-Presets abfragen (für UI-Dropdown)
        if p == "/api/job_status":
            job_id = qs.get("job_id", [""])[0]
            return self._send(200, JOBS.get(job_id, {"status": "unknown"}))
        # Phase 3.4 (Schwachstellenbericht #38): Health-Endpoint für Docker/LB-Monitoring
        if p == "/health" or p == "/api/health":
            uptime_sec = time.time() - _START_TIME
            active_jobs = sum(1 for v in JOBS.values() if v.get("status") == "running")
            with _BATCH_JOBS_LOCK:
                active_batches = sum(1 for v in BATCH_JOBS.values() if v and v.get("running"))
            with _RENDER_JOBS_LOCK:
                active_renders = sum(1 for v in RENDER_JOBS.values() if v and v.get("running"))
            return self._send(200, {
                "status": "ok" if not _SHUTDOWN_IN_PROGRESS else "shutting_down",
                "uptime_sec": round(uptime_sec, 1),
                "active_jobs": active_jobs,
                "active_batches": active_batches,
                "active_renders": active_renders,
                "version": "main/" + (_CURRENT_GIT_COMMIT[:7] if _CURRENT_GIT_COMMIT else "unknown"),
            })
        if p == "/api/measure_wpm":
            # Struktur-/Schnitt-Review Juli 2026: gibt die reale, aus bereits fertigen
            # Videos DIESES Kanals gemessene Sprechrate zurück (statt der festen 150/160-
            # Annahme) -- Frontend nutzt das als informierten Default fürs #wpm-Feld.
            measured = _measure_channel_wpm(cid, fallback=None)
            return self._send(200, {
                "wpm": round(measured, 1) if measured is not None else None,
                "measured": measured is not None,
            })
        # ElevenLabs voiceover endpoints (Phase 1) --------------------------------
        if p == "/api/voiceover_file":
            # Juli 2026 (User-Report: "generiertes Voiceover wird im Frontend nicht
            # angezeigt"): bis jetzt hatte der Server keinen Endpunkt der die
            # voiceover.mp3 aus uploads/ ausliefert — der Python http.server
            # antwortet 404 auf alles was nicht explizit gemappt ist, deshalb konnte
            # der Browser das Audio nicht abspielen obwohl die Datei auf Disk lag.
            # Diese Route streamt die MP3 mit korrektem Content-Type.
            if not vid:
                return self._send(400, {"error": "Kein Video ausgewählt"})
            audio_path = os.path.join(v_uploads(cid, vid), "voiceover.mp3")
            if not os.path.exists(audio_path):
                return self._send(404, {"error": "Kein Voiceover vorhanden"})
            try:
                with open(audio_path, "rb") as f:
                    data = f.read()
                # send_response + send_header + end_headers + body — explizit weil
                # _send() nur JSON serialisiert
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Accept-Ranges", "bytes")
                # Cache-Bust: bei jedem GET andere URL, damit nach Re-Generate
                # der Browser nicht den alten Player-State behält.
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)
                return
            except Exception as e:
                return self._send(500, {"error": f"Audio-Serve fehlgeschlagen: {e}"})

        if p == "/api/download":
            ts_map = {}
            try:
                plan = json.load(open(v_plan(cid, vid)))
                for s in plan.get("scenes", []):
                    t = s.get("t", "").replace(":", "-")
                    ts_map[f"{s['i']:03d}.jpg"] = f"{t}.jpg"
                    ts_map[f"{s['i']:03d}.png"] = f"{t}.png"
            except: pass
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                for f in sorted(os.listdir(v_out(cid, vid))):
                    if f.endswith(".png") or f.endswith(".jpg"):
                        z.write(os.path.join(v_out(cid, vid), f), ts_map.get(f, f))
            return self._send(200, buf.getvalue(), "application/zip")
        if p.startswith("/generated/"):
            fp = os.path.join(v_out(cid, vid), os.path.basename(p))
            if os.path.exists(fp):
                b = open(fp, "rb").read()
                name = os.path.basename(fp)
                if name.endswith(".mp4"):
                    return self._send(200, b, "video/mp4")
                return self._send(200, b, "image/jpeg" if b[:2] == b"\xff\xd8" else "image/png")
            return self._send(404, {"error": "not found"})
        if p == "/api/charsheets":
            # vid aus Query-String: /api/charsheets?channel=...&vid=...
            qs = parse_qs(urlparse(self.path).query)
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
                    except: pass
            return self._send(200, {"sheets": sheets})
        if p.startswith("/charsheets/"):
            # Same: vid-aware lookup with channel-pool fallback
            qs = parse_qs(urlparse(self.path).query)
            vid_param = (qs.get("vid", [None]) or [None])[0]
            sheet_dir = ch_sheets(cid, vid_param) if vid_param else ch_sheets(cid)
            fp = os.path.join(sheet_dir, os.path.basename(p))
            if os.path.exists(fp):
                b = open(fp, "rb").read()
                return self._send(200, b, "image/jpeg" if b[:2] == b"\xff\xd8" else "image/png")
            return self._send(404, {"error": "not found"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        p = urlparse(self.path).path
        # Auch im POST vid aus URL-Query parsen — der Browser sendet es in der URL,
        # nicht im Body. do_GET macht das bereits korrekt.
        _qs = parse_qs(urlparse(self.path).query)
        try:    d = self._read()
        except: return self._send(400, {"error": "bad json"})
        cid = d.get("cid", d.get("channel", _qs.get("channel", ["default"])[0]))
        vid = d.get("vid", d.get("video", _qs.get("video", [""])[0]))

        # Shorts/Upload/Control-Erweiterung: siehe Kommentar in do_GET oben.
        handled, _ = dispatch("POST", p, self, _qs, cid, vid, d)
        if handled:
            return

        # ── Generate T2V scene ────────────────────────────────────────────────
        # ── Preview T2V prompt without generating video ────────────────────────
        if p == "/api/preview_t2v_prompt":
            i = int(d["i"])
            try:
                plan = json.load(open(v_plan(cid, vid)))
                scene = next((s for s in plan["scenes"] if s["i"] == i), None)
            except Exception as e:
                return self._send(500, {"error": f"Plan lesen: {e}"})
            if not scene:
                return self._send(404, {"error": f"Szene {i} nicht gefunden"})
            try: vid_master = open(ch_vid_master(cid)).read().strip()
            except: vid_master = VIDEO_MASTER_DEFAULT
            total = len(plan["scenes"])
            prev_prompts = [s.get("video_prompt","") for s in plan["scenes"] if s["i"] < i and s.get("video_prompt")]
            full_script = " ".join(s.get("text","") for s in plan["scenes"])
            prompt = make_t2v_prompt(scene.get("text",""), i, total, vid_master, prev_prompts, full_script)
            return self._send(200, {"ok": True, "prompt": prompt})

        if p == "/api/generate_t2v":
            if not vid: return self._send(400, {"error": "Kein Video ausgewählt"})
            i = int(d["i"])
            try:
                plan = json.load(open(v_plan(cid, vid)))
                scene = next((s for s in plan["scenes"] if s["i"] == i), None)
            except Exception as e:
                return self._send(500, {"error": f"Plan lesen: {e}"})
            if not scene:
                return self._send(404, {"error": f"Szene {i} nicht gefunden"})

            # Load video master + all scene narrations for context
            try:    vid_master = open(ch_vid_master(cid)).read().strip()
            except: vid_master = VIDEO_MASTER_DEFAULT

            total = len(plan["scenes"])
            # Collect previously generated T2V prompts for continuity
            prev_prompts = [
                s.get("video_prompt", "")
                for s in plan["scenes"]
                if s["i"] < i and s.get("video_prompt")
            ]

            # Use custom prompt from frontend if user edited it, else auto-generate
            custom_prompt = (d.get("custom_prompt") or "").strip()
            if custom_prompt:
                video_prompt = custom_prompt
                print(f"  [Veo] Nutze manuellen Prompt für Szene {i}", flush=True)
            else:
                print(f"  [Veo] Generiere Prompt für Szene {i} …", flush=True)
                full_script = " ".join(s.get("text","") for s in plan["scenes"])
                video_prompt = make_t2v_prompt(
                    scene.get("text", ""), i, total, vid_master, prev_prompts, full_script
                )
            print(f"  [Veo] Prompt: {video_prompt[:120]} …", flush=True)

            # Chain-Extend: continue from previous scene's last frame for true visual continuity,
            # if same story phase and chain isn't too long yet (avoids quality drift).
            prev_scene = next((s for s in plan["scenes"] if s["i"] == i - 1), None)
            can_extend = bool(
                prev_scene
                and prev_scene.get("veo_task_id")
                and prev_scene.get("video_file")
                and story_phase(i - 1, total) == story_phase(i, total)
                and prev_scene.get("chain_len", 0) < MAX_CHAIN_LENGTH
            )

            veo_prompt = _build_video_prompt(video_prompt, vid_master)

            if can_extend:
                print(f"  [Veo] EXTEND von Szene {i-1} (chain_len={prev_scene.get('chain_len',0)+1})", flush=True)
                res = extend_veo(prev_scene["veo_task_id"], veo_prompt)
                chain_len = prev_scene.get("chain_len", 0) + 1
                duration_veo = 8
            else:
                # Load char ref or scene image for REFERENCE_2_VIDEO (fresh anchor shot)
                # Audit Juli 2026 (Bereich 3): über get_channel_style_ref() lesen, sonst
                # gibt eine mehrzeilige style_ref_url.txt (2-3 Refs) hier eine gejointe
                # Mehrzeilen-URL zurück statt einer einzelnen gültigen URL.
                style_ref_url = get_channel_style_ref(cid)
                scene_img_url = scene.get("source_url", "")
                ref_url = style_ref_url or scene_img_url

                requested_dur = int(d.get("duration", 8))
                if ref_url:
                    gen_type = "REFERENCE_2_VIDEO"
                    image_urls = [ref_url]
                    duration_veo = 8  # REFERENCE_2_VIDEO only supports 8s
                else:
                    gen_type = "TEXT_2_VIDEO"
                    image_urls = []
                    duration_veo = max(4, min(8, requested_dur))

                print(f"  [Veo] {gen_type} ref={'ja' if ref_url else 'nein'} dur={duration_veo}s", flush=True)
                # 720p only — 1080p tasks can't be extended, and origin-stable detection already
                # returns 720p quickly without waiting for the 1080p upscale.
                res = gen_veo(veo_prompt, image_urls=image_urls or None,
                              generation_type=gen_type, model="veo3_lite",
                              resolution="720p", duration=duration_veo)
                chain_len = 0

            if not res["ok"]:
                return self._send(500, {"error": res["error"]})

            task_id = res["task_id"]
            job_id  = f"veo_{cid}_{vid}_{i}_{int(time.time())}"
            out_path = os.path.join(v_out(cid, vid), f"{i:03d}.mp4")
            JOBS[job_id] = {"status": "running", "progress": 15, "file": None, "error": None}
            print(f"  [Veo] Task {task_id} → Job {job_id}", flush=True)

            t = threading.Thread(
                target=_veo_job_worker,
                args=(job_id, task_id, scene, out_path, v_plan(cid, vid), cid, vid, video_prompt, chain_len),
                daemon=True
            )
            t.start()
            return self._send(200, {"ok": True, "job_id": job_id, "video_prompt": video_prompt,
                                     "chained": can_extend})

        # ── Audio upload ──────────────────────────────────────────────────────
        if p == "/api/upload_audio":
            try:    raw = base64.b64decode(d["data"])
            except: return self._send(400, {"error": "Ungültige Base64-Daten"})
            if not vid: return self._send(400, {"error": "Kein Video ausgewählt"})
            ensure_video(cid, vid)
            ext = (d.get("name", "audio.bin").rsplit(".", 1)[-1].lower()) or "bin"
            local_path = os.path.join(v_uploads(cid, vid), f"voiceover.{ext}")
            open(local_path, "wb").write(raw)
            json.dump({"path": local_path, "mime": d.get("mime", "audio/mpeg"), "name": d.get("name", "")},
                      open(v_audio(cid, vid), "w"))
            # A fresh recording invalidates any previously trimmed audio and word-
            # alignment computed against the OLD file -- both would silently produce
            # wrong timing/cuts if left in place (the pause-trim + start_aligned/
            # end_aligned re-derive automatically at the next render, see _render_worker).
            trimmed_path = os.path.join(v_uploads(cid, vid), "voiceover_trimmed.wav")
            if os.path.exists(trimmed_path):
                os.remove(trimmed_path)
            try:
                with _PLAN_WRITE_LOCK:
                    plan = json.load(open(v_plan(cid, vid)))
                    for s in plan.get("scenes", []):
                        s.pop("start_aligned", None)
                        s.pop("end_aligned", None)
                    _atomic_write_json(v_plan(cid, vid), plan, ensure_ascii=False, indent=1)
            except Exception:
                pass
            print(f"  [Audio] {os.path.basename(local_path)} ({len(raw)//1024} KB)", flush=True)
            return self._send(200, {"ok": True, "size": len(raw), "name": d.get("name", "")})

        if p == "/api/transcribe_status":
            return self._send(200, dict(TX_STATUS))

        # ── ElevenLabs voiceover (Phase 1) ────────────────────────────────────────
        # Order matters here: settings endpoints and the preview need to be checked
        # before /api/voiceover_generate so they don't fall through. Preview is its own
        # route because it returns raw audio bytes, NOT JSON — _send() handles bytes via
        # the `else` branch (dict/list/str only trigger the json/str->encode path).
        if p == "/api/voiceover_delete":
            # User-Aktion "Voiceover löschen" — entfernt MP3 + audio_meta.json.
            # Der nächste /api/voiceover_generate läuft dann als echter Fresh-Call
            # (statt Resume-Pfad) weil audio_meta nicht mehr existiert.
            if not vid:
                return self._send(400, {"error": "Kein Video ausgewählt"})
            uploads_dir = v_uploads(cid, vid)
            for fn in ("voiceover.mp3", "voiceover_trimmed.wav", "audio_meta.json"):
                p = os.path.join(uploads_dir, fn)
                if os.path.exists(p):
                    try: os.remove(p)
                    except: pass
            return self._send(200, {"ok": True})

        if p == "/api/voiceover_preview":
            text = (d.get("text") or "Hallo Welt, das ist ein Stimm-Sample.").strip()[:500]
            settings = {k: d.get(k) for k in (
                "voice_id", "model_id", "stability", "similarity_boost",
                "style", "speed", "use_speaker_boost", "output_format") if d.get(k) is not None}
            # Juli 2026 Fix: `settings` wurde bisher gebaut und dann bis auf voice_id
            # komplett verworfen — "Voice testen" spielte immer die zuletzt GESPEICHERTEN
            # Slider-Werte vor, nie die, die der Nutzer im Moment gerade zieht (Preview
            # sollte genau das Gegenteil sein: ein Vorhören VOR dem Speichern). Jetzt: wie
            # in _tts_persist_and_schedule werden die persistierten Settings geladen und
            # dann mit den mitgeschickten Werten überschrieben.
            final_settings = load_voice_settings(cid, override_voice_id=(
                settings.get("voice_id") if settings.get("voice_id") else ""))
            for k, v in settings.items():
                if k in final_settings:
                    final_settings[k] = v
            try:
                raw = elevenlabs_generate(text, final_settings)
                audio_b64 = raw["audio_base64"]
            except Exception as e:
                return self._send(500, {"error": f"ElevenLabs Preview fehlgeschlagen: {e}"})
            audio_bytes = base64.b64decode(audio_b64)
            return self._send(200, audio_bytes, "audio/mpeg")

        if p == "/api/voiceover_generate":
            if not vid:
                return self._send(400, {"error": "Kein Video ausgewählt."})
            text = (d.get("text") or "").strip()
            if not text:
                return self._send(400, {"error": "Kein Skript-Text für ElevenLabs — bitte erst Skript in ② eintippen."})
            # Phase I: enrich text with TTS-friendly pause/emphasis markers. We don't have
            # access to scene boundaries yet (those come from plan.json which is built
            # AFTER this call) — but sentence-level "..." insertion runs on the raw text
            # without needing scenes. Scene-based enrichment (climax / phase-break markers)
            # is a no-op in the first generation; will activate once plan.json exists and
            # a regenerate is triggered.
            text = _enrich_for_tts(text, scenes=None)
            # Optional sec override for downstream scene-pacing (defaults to NORMAL_HARD_CAP_SEC).
            sec = d.get("sec")
            settings = {k: d.get(k) for k in (
                "voice_id", "model_id", "stability", "similarity_boost",
                "style", "speed", "use_speaker_boost", "output_format", "sec") if d.get(k) is not None}
            # Resume-Marker: if audio_meta.json + plan.json beide schon vorhanden und
            # voiceover_source == "elevenlabs", KEIN API-Call. Idempotent wie User-Feedback
            # Punkt 3 verlangt.
            meta_path = v_audio(cid, vid)
            plan_p = v_plan(cid, vid)
            try:
                meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
            except Exception:
                meta = {}
            # Juli 2026 Fix: die vorherige Fassung dieser Bedingung war
            # "A and B if meta else False and C and D" — Pythons Operator-Vorrang liest
            # das als "(A and B) if meta else (False and C and D)", NICHT als
            # "meta and A and B and C and D" wie die Intention nahelegt. Solange meta
            # nicht-leer war, wurden C (word_timestamps vorhanden) und D (plan.json
            # existiert) dadurch NIE geprüft — ein Resume wurde schon gemeldet wenn nur
            # audio_meta.json + der Audio-Pfad existierten, auch ohne brauchbare
            # Timestamps oder überhaupt einen Plan.
            if (bool(meta)
                and meta.get("voiceover_source") == "elevenlabs"
                and os.path.exists(meta.get("path", ""))
                and meta.get("voiceover_word_timestamps")
                and os.path.exists(plan_p)):
                src = meta.get("voiceover_source")
                with _VOICE_JOBS_LOCK:
                    VOICE_JOBS[(cid, vid)] = {
                        "running": False, "stage": "fertig (resume)",
                        "error": None, "voiceover_source": src,
                        "voiceover_task_id": meta.get("voiceover_task_id"),
                        "voiceover_chars": meta.get("voiceover_chars"),
                        "ts": time.time(), "resume": True,
                    }
                return self._send(200, {
                    "ok": True, "task_id": meta.get("voiceover_task_id"),
                    "resume": True,
                    "n_words": len(meta.get("voiceover_word_timestamps") or []),
                    "chars": meta.get("voiceover_chars"),
                })
            # Mark running BEFORE the call so a fast frontend polling loop sees a
            # state — most calls will be running for several seconds.
            # Atomic-Pre-Job-Lock (Round-5 Fix-1): verify no existing job runs. Without
            # this guard, two rapid clicks would each set running=True, both submit
            # ElevenLabs-Calls, double-bill the user, and race-write voiceover.mp3.
            with _VOICE_JOBS_LOCK:
                existing = VOICE_JOBS.get((cid, vid), {})
                # Der Dedupe-Riegel darf nicht ewig halten. Stirbt der Request-Thread,
                # ohne running=False zu setzen (Client-Disconnect beim Reload, Laptop-
                # Zuklappen mitten im Call), bleibt das Flag stehen — und _cleanup_stale_jobs
                # fasst laufende Jobs bewusst NICHT an. Ergebnis: jeder weitere Klick auf
                # "Voiceover generieren" landet hier, bekommt ok+deduped zurück und tut
                # nichts. Im Frontend sieht das aus, als reagiere der Button gar nicht mehr,
                # und nur ein Server-Neustart half. Ein Job, der älter ist als der harte
                # ElevenLabs-Deckel (EL_HARD_DEADLINE_SEC=300s) plus Puffer, KANN nicht mehr
                # echt laufen — den überschreiben wir statt zu blockieren.
                VOICE_JOB_STALE_SEC = 900
                age = time.time() - (existing.get("ts") or 0)
                if existing.get("running") and age <= VOICE_JOB_STALE_SEC:
                    print(f"  [ElevenLabs] Job für {cid}/{vid} bereits in Arbeit — dupliziere nicht", flush=True)
                    return self._send(200, {
                        "ok": True, "task_id": existing.get("voiceover_task_id"),
                        "deduped": True,
                        "chars": existing.get("voiceover_chars"),
                    })
                if existing.get("running"):
                    print(f"  [ElevenLabs] Verwaister Job für {cid}/{vid} (running seit "
                          f"{age/60:.1f} min, älter als {VOICE_JOB_STALE_SEC/60:.0f} min) — "
                          f"Thread ist tot, Sperre wird gelöst.", flush=True)
                VOICE_JOBS[(cid, vid)] = {
                    "running": True, "stage": "elevenlabs-generate",
                    "error": None, "voiceover_source": "elevenlabs",
                    "voiceover_task_id": None, "voiceover_chars": None,
                    "ts": time.time(), "resume": False,
                }
            try:
                result = _tts_persist_and_schedule(cid, vid, text,
                    settings=settings if sec is None else {**settings})
                return self._send(200, result)
            except Exception as e:
                import traceback; traceback.print_exc()
                with _VOICE_JOBS_LOCK:
                    VOICE_JOBS[(cid, vid)] = {
                        "running": False, "stage": "error",
                        "error": str(e), "voiceover_source": "elevenlabs",
                        "ts": time.time(), "resume": False,
                    }
                return self._send(500, {"error": f"ElevenLabs fehlgeschlagen: {e}"})

        # ── Transcribe ────────────────────────────────────────────────────────
        if p == "/api/transcribe":
            sec = float(d.get("sec", 4))
            if not vid: return self._send(400, {"error": "Kein Video ausgewählt"})
            if not os.path.exists(v_audio(cid, vid)):
                return self._send(400, {"error": "Keine Audio-Datei hochgeladen."})
            TX_STATUS["running"] = True; TX_STATUS["error"] = ""
            try:
                out = _transcribe_generate_worker(cid, vid, sec)
            except Exception as e:
                import traceback; traceback.print_exc()
                TX_STATUS["running"] = False; TX_STATUS["error"] = str(e)
                return self._send(500, {"error": f"Transkription fehlgeschlagen: {e}"})
            TX_STATUS["running"] = False
            return self._send(200, out)

        # ── Character refs ────────────────────────────────────────────────────
        if p == "/api/upload_charref":
            name = d.get("name", "Charakter").strip()
            img_b64 = d.get("image", "")
            mime = d.get("mime", "image/png")
            vid = d.get("vid")  # July 2026: charsheets are now per-video
            if not name:
                return self._send(400, {"error": "name fehlt"})
            if not img_b64:
                return self._send(400, {"error": "image fehlt"})
            # B-1 Fix: validate=False tolerates fehlendes Padding (häufigster Browser-Bug),
            # try/except fängt den Rest. Ohne den Fix crasht die HTTP-Verbindung mit
            # leerem 500er-Body und das Frontend zeigt einen Silent-Fail.
            try:
                img_bytes = base64.b64decode(img_b64, validate=False)
            except Exception as e:
                return self._send(400, {"error": f"image ist kein gültiges Base64: {e}"})
            if not img_bytes:
                return self._send(400, {"error": "image dekodiert zu leer"})
            safe = re.sub(r"[^\w\-]", "_", name.lower()) or "character"
            os.makedirs(ch_sheets(cid, vid), exist_ok=True)  # B-1: Auto-Mkdir (frische Kanäle)
            img_path  = os.path.join(ch_sheets(cid, vid), f"{safe}.png")
            meta_path = os.path.join(ch_sheets(cid, vid), f"{safe}.json")
            try:
                open(img_path, "wb").write(img_bytes)
            except OSError as e:
                return self._send(500, {"error": f"Schreiben fehlgeschlagen: {e}"})
            try:    desc = analyze_char_image(img_bytes, mime)
            except Exception as e:
                desc = ""; print(f"  [Char] Analyse-Fehler: {e}", flush=True)
            # Öffentliche URL erzeugen: das Frontend erwartet `uri` (set_char_ref +
            # _applyCharRef), und die style_ref_url wird als KIE-Bildreferenz benutzt —
            # muss also public-http sein (set_char_ref lehnt Nicht-http-URLs ab).
            # Ohne diesen Upload bekam das Frontend `undefined` → Referenz wurde nie
            # gesetzt (Kern von Bug B-1: Upload "tat nichts").
            try:
                public_uri = upload_image_public(img_path)
            except Exception as e:
                return self._send(502, {"error": f"Public-Upload fehlgeschlagen: {e}"})
            json.dump({"name": name, "description": desc, "safe": safe, "mime": "image/png", "uri": public_uri},
                      open(meta_path, "w"), ensure_ascii=False)
            return self._send(200, {"ok": True, "name": name, "safe": safe, "description": desc, "uri": public_uri})

        if p == "/api/gen_charsheet":
            name = d.get("name", "").strip(); desc = d.get("description", "").strip()
            vid_param = d.get("vid") or None
            if not name or not desc: return self._send(400, {"error": "name und description erforderlich"})
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
            tmp  = os.path.join(sheet_dir, f"_tmp_{safe}.jpg")
            try:
                img_bytes = gen_charsheet(cid, name, desc, vid=vid_param)
                open(os.path.join(sheet_dir, f"{safe}.png"), "wb").write(img_bytes)
                json.dump({"name": name, "description": desc, "safe": safe, "mime": "image/jpg"},
                          open(os.path.join(sheet_dir, f"{safe}.json"), "w"), ensure_ascii=False)
                return self._send(200, {"ok": True, "name": name, "safe": safe})
            except Exception as e:
                import traceback; traceback.print_exc()
                return self._send(500, {"error": str(e)})

        # ── Char-Sheet: Beschreibung aktualisieren ─────────────────────────────
        if p == "/api/charsheet_update":
            safe = re.sub(r"[^\w\-]", "_", (d.get("safe") or "").lower())
            desc = d.get("description", "").strip()
            vid_param = d.get("vid") or None
            if not safe or not desc: return self._send(400, {"error": "safe und description erforderlich"})
            sheet_dir = ch_sheets(cid, vid_param)
            meta_path = os.path.join(sheet_dir, f"{safe}.json")
            if not os.path.exists(meta_path):
                return self._send(404, {"error": "Charakter existiert nicht in diesem Video"})
            try:
                meta = json.load(open(meta_path))
                meta["description"] = desc
                meta["updated_at"] = time.time()
                json.dump(meta, open(meta_path, "w"), ensure_ascii=False, indent=1)
                return self._send(200, {"ok": True})
            except Exception as e:
                return self._send(500, {"error": str(e)})

        # ── Char-Sheet: löschen ────────────────────────────────────────────────
        if p == "/api/charsheet_delete":
            safe = re.sub(r"[^\w\-]", "_", (d.get("safe") or "").lower())
            vid_param = d.get("vid") or None
            if not safe: return self._send(400, {"error": "safe erforderlich"})
            sheet_dir = ch_sheets(cid, vid_param)
            removed = []
            for ext in (".json", ".png"):
                fp = os.path.join(sheet_dir, f"{safe}{ext}")
                if os.path.exists(fp):
                    try:
                        os.remove(fp)
                        removed.append(fp)
                    except: pass
            return self._send(200, {"ok": True, "removed": removed})

        # ── Generate one image (async) ────────────────────────────────────────
        # ── Auto-rendering (Ken Burns clips -> concat -> audio mux) ─────────────
        if p == "/api/render_start":
            if not vid: return self._send(400, {"error": "Kein Video ausgewählt"})
            force = bool(d.get("force", False))
            target = d.get("target", "longform")
            if target not in RENDER_TARGETS:
                return self._send(400, {"error": f"Unbekanntes render target: {target}"})
            key = (cid, vid, target)
            with _RENDER_JOBS_LOCK:
                if RENDER_JOBS.get(key, {}).get("running"):
                    return self._send(200, {"ok": True, "already_running": True})
                # Juli 2026 Fix (Audit 4.2): _apply_sync_invariant stretches whatever
                # scenes DO have a generated file across the FULL audio duration — a
                # render with e.g. 20/96 rendered scenes silently produces a full-length
                # video where each of those 20 images is shown ~5x longer than intended,
                # no warning anywhere. Surface the partial-render fact BEFORE starting so
                # the frontend can confirm() with the user; `force: true` skips this once
                # confirmed (same pattern as /api/generate_all_start's `force`).
                if not force:
                    try:
                        plan_for_check = json.load(open(v_plan(cid, vid)))
                        total_scenes = len(plan_for_check.get("scenes", []))
                        rendered_scenes = sum(1 for s in plan_for_check.get("scenes", []) if s.get("file"))
                    except Exception:
                        total_scenes = rendered_scenes = 0
                    if total_scenes and rendered_scenes < total_scenes:
                        return self._send(200, {
                            "ok": False, "partial": True,
                            "rendered": rendered_scenes, "total": total_scenes,
                        })
                # Same atomic "set running=True before the thread exists" fix as
                # generate_all_start above — avoids two rapid start calls each
                # spinning up their own render worker on the same video.
                # started_ts: für die ETA-Berechnung im Frontend (Fortschrittsbalken).
                # stage() unten aktualisiert den Job-Dict nur per .update(), started_ts
                # bleibt also über die gesamte Render-Laufzeit erhalten.
                RENDER_JOBS[key] = {"running": True, "stop_requested": False, "stage": "startet",
                                     "done": 0, "total": 0, "error": None, "file": None,
                                     "started_ts": time.time()}
            threading.Thread(target=_render_worker, args=(cid, vid, target), daemon=True).start()
            return self._send(200, {"ok": True, "already_running": False})

        if p == "/api/render_stop":
            target = d.get("target", "longform")
            key = (cid, vid, target)
            with _RENDER_JOBS_LOCK:
                if key in RENDER_JOBS:
                    RENDER_JOBS[key]["stop_requested"] = True
            return self._send(200, {"ok": True})

        if p == "/api/produce_start":
            if not vid: return self._send(400, {"error": "Kein Video ausgewählt"})
            key = (cid, vid)
            text = d.get("text", ""); wpm = float(d.get("wpm", 130)); sec = float(d.get("sec", 4))
            with _PRODUCE_JOBS_LOCK:
                if PRODUCE_JOBS.get(key, {}).get("running"):
                    return self._send(200, {"ok": True, "already_running": True})
                # Same atomic "set running=True before the thread exists" fix as
                # generate_all_start/render_start above.
                PRODUCE_JOBS[key] = {"running": True, "stage": "startet", "stop_requested": False,
                                      "error": None, "file": None}
            threading.Thread(target=_produce_worker, args=(cid, vid, text, wpm, sec), daemon=True).start()
            return self._send(200, {"ok": True, "already_running": False})

        if p == "/api/produce_stop":
            key = (cid, vid)
            with _PRODUCE_JOBS_LOCK:
                if key in PRODUCE_JOBS:
                    PRODUCE_JOBS[key]["stop_requested"] = True
            # Also forward the stop into whichever sub-job is CURRENTLY running --
            # _produce_worker only checks its own stop_requested BETWEEN stages, so a
            # stage already in flight (image batch or render) needs its own flag set
            # too, otherwise "Stop" would silently wait for that stage to finish first.
            with _BATCH_JOBS_LOCK:
                if BATCH_JOBS.get(key, {}).get("running"):
                    BATCH_JOBS[key]["stop_requested"] = True
            # _produce_worker rendert intern immer (cid, vid, "longform") -- siehe dort.
            with _RENDER_JOBS_LOCK:
                render_key = (cid, vid, "longform")
                if RENDER_JOBS.get(render_key, {}).get("running"):
                    RENDER_JOBS[render_key]["stop_requested"] = True
            return self._send(200, {"ok": True})

        if p == "/api/generate_one":
            if not vid: return self._send(400, {"error": "Kein Video ausgewählt"})
            i = int(d["i"]); prompt = d.get("prompt", "")
            scene_key = (cid, vid, i)
            with _ACTIVE_SCENE_JOBS_LOCK:
                existing_job_id = ACTIVE_SCENE_JOBS.get(scene_key)
                if existing_job_id and JOBS.get(existing_job_id, {}).get("status") == "running":
                    # Already generating this exact scene (double-click, second tab, or
                    # "Alle generieren" running twice) — hand back the SAME job instead
                    # of paying for and starting a second KIE task.
                    print(f"  [KIE] Szene {i} bereits in Arbeit ({existing_job_id}) — dupliziere nicht", flush=True)
                    return self._send(200, {"ok": True, "job_id": existing_job_id, "deduped": True})
            fn = f"{i:03d}.jpg"
            out_path = os.path.join(v_out(cid, vid), fn)
            # Phase C: read phase from plan.json so the image prompt gets the
            # PHASE_PROMPT_ADDITIONS hard-injection when available. plan.json is the
            # single source of truth for scene state.
            # Juli 2026 Fix (NameError-Edge): scene_for_phase MUSS vor dem try existieren —
            # vorher warf ein fehlschlagender json.load() eine NameError auf der
            # anschließenden scene_for_phase.get()-Zeile, weil die except-Klausel nur
            # scene_phase zurücksetzte, nie scene_for_phase selbst.
            scene_for_phase = {}
            try:
                plan = json.load(open(v_plan(cid, vid)))
                scene_for_phase = (plan.get("scenes") or [{}])[i] if i < len(plan.get("scenes", [])) else {}
                scene_phase = scene_for_phase.get("phase", "")
            except Exception:
                plan = {}
                scene_phase = ""
            entity = str(scene_for_phase.get("concrete_entity", ""))
            # Fix 1/4 auch hier (siehe _batch_generate_worker): dieser Pfad übergab
            # ebenfalls char_refs=None und kein entity — ausgerechnet der Klick, mit dem
            # man ein misslungenes Bild korrigiert, schickte den Steckbrief nie mit.
            prompt_entity = entity
            if (not entity.startswith("char_")) and scene_for_phase \
                    and scene_depicts_people(scene_for_phase):
                prompt_entity = nearest_character_entity(plan, scene_for_phase) or entity
            char_refs, entity_key = charsheet_refs_for_entity(plan, cid, vid, prompt_entity)
            # Vorgezogen (war vorher erst nach dem Semaphore-Acquire weiter unten): wird
            # jetzt schon hier für has_style_refs gebraucht (siehe _build_image_prompt).
            style_ref_urls = get_channel_style_refs(cid)
            full_prompt = _build_image_prompt(prompt, read_master(cid), char_refs,
                                              phase=scene_phase, entity=entity_key,
                                              has_style_refs=bool(style_ref_urls))
            # Juli 2026 Fix (Audit A2 "generate_one hat den Fallback-Fix nicht"): dieser
            # Pfad hatte bisher eine eigene, abgespeckte Inline-Logik, die NUR source_url
            # kannte — ausgerechnet der manuelle "Neu generieren"-Klick (mit dem man
            # schlechte Bilder korrigiert) hatte dadurch NIE den 3-Stufen-Fallback
            # (Charsheet-PNG, Name-Match, lokale Bilddatei) aus _resolve_entity_ref, den
            # der Batch-Worker seit Juli 2026 hat. Jetzt identische Logik, nur mit
            # wait=False (kein paralleler Sibling-Dispatch bei einem Einzelklick, also
            # kein Grund auf eine Anchor-Szene zu warten).
            entity_refs, entity_debug = _resolve_entity_ref(v_plan(cid, vid), scene_for_phase, wait=False)
            if entity_debug.get("is_local"):
                entity_refs = [get_public_charsheet_url(ref) for ref in entity_refs]

            # A2, gleiche Logik wie im Batch-Worker (_batch_generate_worker) — zweiter
            # Charakter in derselben Szene bekommt seine eigene Referenz mit angehängt.
            secondary_entity = str(scene_for_phase.get("secondary_entity", "") or "")
            if secondary_entity and secondary_entity != prompt_entity:
                sec_png, _sec_dbg = _find_charsheet_png(plan, cid, vid, secondary_entity)
                if sec_png:
                    entity_refs = entity_refs + [get_public_charsheet_url(sec_png)]

            if entity_refs:
                if scene_for_phase.get("seq_id") is not None and scene_for_phase.get("seq_pos", 0) >= 1:
                    full_prompt += (
                        "\n\nCONTINUITY (STRICT): This is a continuation of the exact same "
                        "shot as the reference image(s). You MUST perfectly match the "
                        "identity, outfit, and background environment shown in the "
                        "references. Change ONLY the camera angle/framing or the specific "
                        "action described above.")
                else:
                    full_prompt += (
                        "\n\nCHARACTER CONTINUITY: The reference image shows this exact character. "
                        "Preserve their FACIAL identity first — same eyes, nose, face shape — then "
                        "hair color/style, then outfit and build. Keep hands correct (five fingers). "
                        "Vary only pose, framing and expression to fit this scene's action above.")
            # Global concurrency cap — blocks here if MAX_CONCURRENT_IMAGE_GENS are already
            # in flight from ANY source (batch or other individual clicks), instead of
            # firing this KIE submission immediately alongside all the others. Released by
            # _image_job_worker once this scene's generation fully finishes.
            IMAGE_GEN_SEMAPHORE.acquire()
            # Juli 2026 (User-Report: "sobald kein Mensch im Prompt ist, denkt er sich
            # was aus"): Referenzbild jetzt an JEDE Szene (nicht mehr nur char_-Entities)
            # — es ist ein reiner STIL-Anker (siehe Master-Prompt), kein erzwungenes
            # "diese Person muss hier stehen" mehr, daher unproblematisch auch für
            # Landschafts-/Symbol-Szenen. Nur wenn schon ein spezifischeres, bereits
            # generiertes Bild desselben Charakters existiert (entity_refs), gilt NUR das
            # — nie beide Referenzbilder gleichzeitig (siehe Farb-Inkonsistenz-Fix in
            # _batch_generate_worker). Keine Charsheet-Text/Bild-Injection mehr (alte,
            # kanalweite Charsheets aus einem anderen Video überstimmten sonst die
            # Szenenbeschreibung).
            chain_refs, chain_debug = _resolve_chain_refs(v_plan(cid, vid), scene_for_phase)
            # D1 (Evaluation Juli 2026, Fund 1): Style-Ref(s) IMMER anhängen, nicht mehr
            # nur wenn kein Chain-/Entity-Anchor existiert -- sonst verliert die
            # Einzelklick-Generierung genau wie der Batch-Worker den Grafik-Stil-Anker bei
            # Charakter-Szenen. Reihenfolge: Identität zuerst, Stil zuletzt. Audit Juli
            # 2026 (Bereich 3): bis zu 3 Style-Refs statt nur einem.
            use_style_ref = bool(style_ref_urls)
            refs = chain_refs + entity_refs + (style_ref_urls if use_style_ref else [])
            try:
                task_id = _kie_submit_image(full_prompt, model=get_video_image_model(cid, vid),
                                             ref_urls=refs or None)
            except Exception as e:
                IMAGE_GEN_SEMAPHORE.release()
                return self._send(500, {"error": str(e)})
            job_id = f"{cid}_{vid}_{i}_{int(time.time())}"
            JOBS[job_id] = {"status": "running", "progress": 0, "file": None,
                            "source_url": None, "ts": None, "error": None}
            with _ACTIVE_SCENE_JOBS_LOCK:
                ACTIVE_SCENE_JOBS[scene_key] = job_id
            # Mark scene as running in plan
            with _PLAN_WRITE_LOCK:
                try:
                    plan = json.load(open(v_plan(cid, vid)))
                    for s in plan["scenes"]:
                        if s["i"] == i:
                            if prompt: s["prompt"] = prompt
                            s["status"] = "läuft"
                    _atomic_write_json(v_plan(cid, vid), plan, ensure_ascii=False, indent=1)
                except: pass
            threading.Thread(
                target=_image_job_worker,
                args=(job_id, task_id, out_path, v_plan(cid, vid), i, scene_key),
                daemon=True
            ).start()
            return self._send(200, {"ok": True, "job_id": job_id})

        # ── Generate video from image ─────────────────────────────────────────
        if p == "/api/generate_video":
            if not vid: return self._send(400, {"error": "Kein Video ausgewählt"})
            i = int(d["i"])
            # Match video duration to scene length (clamped to KIE's 6-30s range)
            scene_dur = float(d.get("scene_dur", d.get("duration", 6)))
            duration = max(6, min(30, round(scene_dur)))
            # Load scene from plan
            try:
                plan = json.load(open(v_plan(cid, vid)))
                scene = next((s for s in plan["scenes"] if s["i"] == i), None)
            except Exception as e:
                return self._send(500, {"error": f"Plan lesen fehlgeschlagen: {e}"})
            if not scene:
                return self._send(404, {"error": f"Szene {i} nicht gefunden"})
            # Load canonical character reference URL (if set). Audit Juli 2026
            # (Bereich 3): über get_channel_style_ref() lesen (Mehrzeilen-sicher).
            style_ref_url = get_channel_style_ref(cid)

            # Scene image as fallback reference (upload if no CDN url yet)
            source_url = scene.get("source_url", "")
            url_age = int(time.time()) - scene.get("source_url_ts", 0)
            if not style_ref_url and (not source_url or url_age > 72000):
                local_file = scene.get("file")
                if not local_file:
                    return self._send(400, {"error": "Kein Bild und kein Character-Ref — zuerst ein Bild generieren."})
                local_path = os.path.join(v_out(cid, vid), local_file)
                if not os.path.exists(local_path):
                    return self._send(400, {"error": f"Bild-Datei nicht gefunden: {local_file}"})
                print(f"  [Video] Lade Szenen-Bild hoch …", flush=True)
                try:
                    source_url = upload_image_public(local_path)
                    with _PLAN_WRITE_LOCK:
                        plan = json.load(open(v_plan(cid, vid)))
                        for s in plan["scenes"]:
                            if s["i"] == i:
                                s["source_url"] = source_url
                                s["source_url_ts"] = int(time.time())
                        _atomic_write_json(v_plan(cid, vid), plan, ensure_ascii=False, indent=1)
                except Exception as e:
                    return self._send(500, {"error": f"Bild-Upload fehlgeschlagen: {e}"})

            # Load character description from master prompt for context
            char_desc = ""
            try:
                char_desc = open(ch_master(cid)).read().strip()[:300]
            except: pass

            # Generate detailed video scene prompt from narration
            print(f"  [Video] Generiere Video-Prompt für Szene {i} …", flush=True)
            video_prompt = make_video_prompt(scene.get("text", ""), char_desc)
            print(f"  [Video] Prompt: {video_prompt[:120]} …", flush=True)

            using_ref = "style-ref" if style_ref_url else "scene-img"
            print(f"  [Video] Referenz: {using_ref}", flush=True)
            res = gen_video(source_url, video_prompt, duration, style_ref_url)
            if not res["ok"]:
                return self._send(500, {"error": res["error"]})
            # Download mp4
            video_url = res["video_url"]
            fn_silent = f"{i:03d}_silent.mp4"
            fn        = f"{i:03d}.mp4"
            silent_path = os.path.join(v_out(cid, vid), fn_silent)
            out_path    = os.path.join(v_out(cid, vid), fn)
            try:
                dl_req = urllib.request.Request(video_url,
                    headers={"Referer": "https://kie.ai/", "User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(dl_req, timeout=120) as vr:
                    open(silent_path, "wb").write(vr.read())
            except Exception as e:
                return self._send(500, {"error": f"Video-Download fehlgeschlagen: {e}"})
            # Mix in voiceover segment if available
            has_audio = False
            try:
                audio_meta = json.load(open(v_audio(cid, vid)))
                audio_path = audio_meta.get("path", "")
                if os.path.exists(audio_path):
                    start = float(scene.get("start", 0))
                    dur   = float(scene.get("dur", duration))
                    import subprocess
                    result = subprocess.run([
                        "ffmpeg", "-y",
                        "-i", silent_path,
                        "-ss", str(start), "-t", str(dur), "-i", audio_path,
                        "-map", "0:v", "-map", "1:a",
                        "-c:v", "copy", "-c:a", "aac", "-shortest",
                        out_path
                    ], capture_output=True, timeout=60)
                    if result.returncode == 0:
                        os.remove(silent_path)
                        has_audio = True
                        print(f"  [Video] Audio gemischt (start={start}s dur={dur}s)", flush=True)
                    else:
                        print(f"  [Video] ffmpeg Fehler: {result.stderr.decode()[-200:]}", flush=True)
            except Exception as e:
                print(f"  [Video] Audio-Mix übersprungen: {e}", flush=True)
            if not has_audio:
                # No audio — just rename silent to final
                os.replace(silent_path, out_path)
            # Update plan (re-read fresh under the lock — don't reuse the `plan` object
            # read earlier in this request, which may now be stale)
            with _PLAN_WRITE_LOCK:
                try:
                    plan = json.load(open(v_plan(cid, vid)))
                    for s in plan["scenes"]:
                        if s["i"] == i:
                            s["video_file"] = fn
                            s["video_prompt"] = video_prompt
                    _atomic_write_json(v_plan(cid, vid), plan, ensure_ascii=False, indent=1)
                except: pass
            print(f"  [Video] Szene {i} fertig → {fn} (audio={'ja' if has_audio else 'nein'})", flush=True)
            return self._send(200, {"ok": True, "file": fn, "video_prompt": video_prompt, "ts": int(time.time())})

        # ── Set canonical character reference URL ─────────────────────────────
        # Juli 2026 Fix: dashboard.html ruft '/api/set_style_ref' auf (der Endpoint
        # wurde intern längst zu einem reinen Stil-Anker umgebaut, siehe
        # get_channel_style_ref()/style_ref_url.txt), aber die Route hieß noch
        # '/api/set_char_ref' — der Button im Stil-Tab lief seit dem Umbau ins Leere
        # (404). Beide Namen akzeptieren statt umzubenennen, damit nichts anderes
        # bricht, das noch den alten Namen aufruft.
        if p in ("/api/set_char_ref", "/api/set_style_ref"):
            # Audit Juli 2026 (Bereich 3, Multi-Style-Ref): akzeptiert jetzt entweder
            # ein einzelnes "url" (Legacy, 1 Slot) ODER eine Liste "urls" (bis zu 3
            # Slots) -- das Frontend schickt bei jeder Änderung (Add/Remove/Edit
            # eines Slots) die komplette aktuelle Liste, der Server überschreibt
            # style_ref_url.txt komplett. Keine Index-Patch-Semantik nötig, das hält
            # die Datei immer konsistent mit dem, was das Frontend gerade anzeigt.
            urls = d.get("urls")
            if urls is None:
                single = d.get("url", "").strip()
                urls = [single] if single else []
            urls = [str(u).strip() for u in urls if str(u).strip()][:3]
            for u in urls:
                if not u.startswith("http"):
                    return self._send(400, {"error": f"Ungültige URL: {u}"})
            ref_path = os.path.join(ch_dir(cid), "style_ref_url.txt")
            if not urls:
                if os.path.exists(ref_path): os.remove(ref_path)
                return self._send(200, {"ok": True, "url": "", "urls": []})
            open(ref_path, "w").write("\n".join(urls) + "\n")
            return self._send(200, {"ok": True, "url": urls[0], "urls": urls})

        # ── Generate + upload canonical character reference image ──────────────
        # Gleicher Alias-Grund wie bei set_char_ref/set_style_ref oben.
        if p in ("/api/gen_char_ref", "/api/gen_style_ref"):
            # Audit Juli 2026 (Bereich 3, Multi-Style-Ref): optionales "index" (0-2)
            # ersetzt genau diesen Slot; ohne index wird ein neuer Slot angehängt
            # (max. 3 -- kein KIE-Credit verbrennen, wenn eh kein Platz ist).
            existing_refs = get_channel_style_refs(cid)
            slot_index = d.get("index")
            if slot_index is not None:
                try: slot_index = int(slot_index)
                except Exception: slot_index = None
            if slot_index is None and len(existing_refs) >= 3:
                return self._send(400, {"error": "Maximal 3 Style-Referenzen — erst eine entfernen."})
            master = ""
            try: master = open(ch_master(cid)).read().strip()
            except: pass
            # Neutral standing-pose prompt — deliberately does NOT hardcode any style
            # words (background, line-weight, shading) here. That used to say "pure
            # white background, no shading", which directly contradicted whatever
            # style the channel's actual master prompt describes (e.g. Ink Explainer's
            # "never a white background, flat cel-shading") — the master prompt alone
            # must own all style decisions, this only specifies the pose.
            char_prompt = (
                f"Full body, neutral standing pose, facing forward, arms at sides, "
                f"plain simple setting.\n\n{master}"
            )
            try:
                task_id = _kie_submit_image(char_prompt)
            except Exception as e:
                return self._send(500, {"error": f"Bild-Generierung fehlgeschlagen: {e}"})
            # Poll until done
            for _ in range(60):
                time.sleep(5)
                try:
                    hdrs = {"Authorization": f"Bearer {kie_key()}"}
                    req = urllib.request.Request(f"{KIE_API}/recordInfo?taskId={task_id}", headers=hdrs)
                    with urllib.request.urlopen(req, timeout=15) as r:
                        info = json.load(r)["data"]
                    if info.get("state") == "success":
                        result_json = json.loads(info.get("resultJson", "{}"))
                        cdn_url = result_json.get("resultUrls", [""])[0]
                        if cdn_url:
                            # Upload to public host for KIE I2V access
                            dl_req = urllib.request.Request(cdn_url,
                                headers={"Referer": "https://kie.ai/", "User-Agent": "Mozilla/5.0"})
                            with urllib.request.urlopen(dl_req, timeout=60) as vr:
                                img_data = vr.read()
                            # Save locally -- erster Slot behält den Legacy-Namen
                            # "style_ref.png" (gen_charsheet's lokaler Fallback-Pfad
                            # erwartet genau diesen Namen), weitere Slots timestamped.
                            ref_fname = "style_ref.png" if not existing_refs else f"style_ref_{int(time.time())}.png"
                            ref_path = os.path.join(ch_dir(cid), ref_fname)
                            open(ref_path, "wb").write(img_data)
                            pub_url = upload_image_public(ref_path)
                            if slot_index is not None and 0 <= slot_index < len(existing_refs):
                                existing_refs[slot_index] = pub_url
                            else:
                                existing_refs.append(pub_url)
                            existing_refs = existing_refs[:3]
                            open(os.path.join(ch_dir(cid), "style_ref_url.txt"), "w").write(
                                "\n".join(existing_refs) + "\n")
                            return self._send(200, {"ok": True, "url": pub_url, "urls": existing_refs})
                    elif info.get("state") == "fail":
                        return self._send(500, {"error": f"KIE fail: {info.get('failMsg')}"})
                except Exception as e:
                    print(f"  [CharRef] Poll error: {e}", flush=True)
            return self._send(500, {"error": "Timeout beim Generieren des Character-Refs"})

        return self._send(404, {"error": "not found"})

def main():
    # 8000 kollidiert auf diesem Rechner mit Docker Desktop (com.docker.backend hört
    # auf *:8000 per IPv6, während dieser Server nur IPv4/127.0.0.1 bindet). Löst der
    # Browser "localhost" zu ::1 auf (macOS bevorzugt das oft), landet die Anfrage bei
    # Docker statt hier und liefert ein nacktes 404 — sah aus wie ein Absturz, war aber
    # nur der falsche Port. 8010 ist auf dieser Maschine kollisionsfrei.
    port = 8010
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    if not os.path.exists(KIE_KEY_FILE):
        print("WARN: ~/.kie_key fehlt — alle KI-Funktionen werden scheitern.")
    # Shorts/Upload/Control-Erweiterung: eine Zeile auskommentieren schaltet das
    # gesamte Gebiet ab, ohne den Rest anzufassen (siehe routes/__init__.py).
    import shorts.api, youtube.api, control.api
    import routes.channels
    import routes.video_settings
    import routes.video_meta
    import routes.job_status
    import routes.voice
    import routes.script_gen
    import routes.thumbnail
    import routes.plan
    import routes.batch
    import store.db as store_db
    # Refactor Phase 4 (Teil 1-5): Route-Gruppen aus dem dashboard.py-Handler
    # ausgelagert. Reihenfolge unkritisch -- Präfixe überschneiden sich nicht
    # mit shorts/youtube/control.
    mount("/api/channels", routes.channels)
    mount("/api/videos", routes.channels)
    mount("/api/presets", routes.video_settings)
    mount("/api/char_ref", routes.video_settings)
    mount("/api/get_mode", routes.video_settings)
    mount("/api/set_mode", routes.video_settings)
    mount("/api/vid_master", routes.video_settings)
    mount("/api/master", routes.video_settings)
    mount("/api/image_model", routes.video_settings)
    mount("/api/style_ref", routes.video_settings)
    mount("/api/overlay_opts", routes.video_settings)
    mount("/api/video_meta", routes.video_meta)
    mount("/api/stepper_state", routes.video_meta)
    mount("/api/script", routes.video_meta)
    mount("/api/select_title", routes.video_meta)
    mount("/api/save_script", routes.video_meta)
    mount("/api/save_idea", routes.video_meta)
    mount("/api/generate_all_status", routes.job_status)
    mount("/api/render_status", routes.job_status)
    mount("/api/produce_status", routes.job_status)
    mount("/api/plan_status", routes.job_status)
    mount("/api/thumbnail_status", routes.job_status)
    mount("/api/voiceover_status", routes.job_status)
    mount("/api/transcribe_status", routes.job_status)
    mount("/api/elevenlabs_voices", routes.voice)
    mount("/api/tts_provider", routes.voice)
    mount("/api/elevenlabs_settings", routes.voice)
    mount("/api/generate_script", routes.script_gen)
    mount("/api/generate_titles", routes.script_gen)
    mount("/api/generate_thumbnail", routes.thumbnail)
    mount("/api/plan", routes.plan)
    mount("/api/generate_all_start", routes.batch)
    mount("/api/generate_all_stop", routes.batch)
    mount("/api/shorts/", shorts.api)
    mount("/api/youtube/", youtube.api)
    mount("/api/control/", control.api)
    # Refactor Phase 0: rotierendes Backup VOR init_db(), sonst würde die frisch
    # geöffnete Connection mitkopiert statt des Vor-Start-Stands.
    store_db.backup_db()
    store_db.init_db()
    # Upload-Worker läuft nur, wenn der Nutzer je einen Google-Cloud-OAuth-Client
    # eingerichtet hat -- ohne diese Datei gibt es serverseitig ohnehin kein Kanal-
    # Token, gegen das der Worker etwas hochladen könnte (siehe youtube/oauth.py).
    from youtube.oauth import client_configured
    if client_configured():
        from youtube.upload import worker_loop
        threading.Thread(target=worker_loop, daemon=True).start()
        print("  [Upload] Worker-Thread gestartet (~/.youtube_oauth_client.json gefunden).")
    else:
        print("  [Upload] ~/.youtube_oauth_client.json fehlt — Upload-Feature bleibt inaktiv "
              "(Shorts/Packaging funktionieren unabhängig davon).")
    _start_job_cleanup_daemon()
    global _SERVER_REF
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    _SERVER_REF = srv  # muss VOR serve_forever() gesetzt sein, siehe _shutdown_worker()
    print(f"Dashboard läuft: http://localhost:{port}  (Strg+C zum Beenden)")
    srv.serve_forever()
    srv.server_close()
    _log("INFO", "shutdown_complete")

if __name__ == "__main__":
    # `python3 dashboard.py` lädt dieses Modul als "__main__", nicht als "dashboard" --
    # ein `import dashboard` aus shorts/api.py (o.ä.) würde sonst eine ZWEITE, komplett
    # unabhängige Kopie mit eigenem RENDER_JOBS/RENDER_TARGETS/... importieren, die nie
    # mit der tatsächlich laufenden Handler-Klasse H in Verbindung steht (leerer
    # Render-Status trotz laufendem Render war das beobachtbare Symptom). Registriert
    # das bereits geladene __main__-Modul zusätzlich unter dem Namen "dashboard", BEVOR
    # main() die Shorts/YouTube/Control-Module mountet -- ihr lazy `import dashboard`
    # findet dadurch dieselbe Modul-Instanz statt eine neue auszuführen.
    sys.modules.setdefault("dashboard", sys.modules[__name__])
    main()
