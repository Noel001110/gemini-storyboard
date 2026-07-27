#!/usr/bin/env python3
"""Localhost-Dashboard für die Storyboard-Bildgenerierung.
Nur Python-Standardlib. Start: python3 dashboard.py [--port 8010]
"""
import os, re, sys, json, time, base64, threading
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
# _batch_generate_worker aufgerufen.
from engine.prompts import (  # noqa: F401,F403
    IMAGE_PROMPT_CHUNK_SIZE, IMAGE_PROMPT_MIN_LEN,
    SCRIPT_SYSTEM, TITLE_SYSTEM, THUMBNAIL_PROMPT_SYSTEM,
    HOOK_PROMPT_ADDITION,
    _build_image_prompt,
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
# a long-lived server process accumulates one entry per image/batch/plan/render job
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

def _image_job_worker_inner(job_id: str, task_id: str, out_path: str, plan_path: str, scene_i: int,
                             skip_upscale: bool = False):
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
            if not skip_upscale:
                from engine.upscale import upscale_image_local_safe
                upscale_image_local_safe(out_path)
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


# ---------- Auto-Rendering (reines FFmpeg — kein MoviePy/Remotion/Node) ----------
# Nimmt die bereits generierten Standbilder (generated/NNN.jpg) und schneidet sie
# mit Ken-Burns-Bewegung zu einem fertigen Video mit durchgehendem Voiceover
# zusammen (T2V/I2V-Videoerzeugung wurde 2026-07-26 entfernt, ungenutzt).


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
    import routes.render
    import routes.produce
    import routes.voiceover
    import routes.misc
    import routes.charsheets
    import routes.images
    import routes.audio
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
    mount("/api/rewrite_script_retention", routes.script_gen)
    mount("/api/generate_thumbnail", routes.thumbnail)
    mount("/api/plan", routes.plan)
    mount("/api/generate_all_start", routes.batch)
    mount("/api/generate_all_stop", routes.batch)
    mount("/api/render_start", routes.render)
    mount("/api/render_stop", routes.render)
    mount("/api/produce_start", routes.produce)
    mount("/api/produce_stop", routes.produce)
    mount("/api/voiceover_file", routes.voiceover)
    mount("/api/voiceover_delete", routes.voiceover)
    mount("/api/voiceover_preview", routes.voiceover)
    mount("/api/voiceover_generate", routes.voiceover)
    mount("/api/job_status", routes.misc)
    mount("/health", routes.misc)
    mount("/api/health", routes.misc)
    mount("/api/measure_wpm", routes.misc)
    mount("/api/download", routes.misc)
    mount("/generated/", routes.misc)
    mount("/api/charsheets", routes.charsheets)
    mount("/charsheets/", routes.charsheets)
    mount("/api/upload_charref", routes.charsheets)
    mount("/api/gen_charsheet", routes.charsheets)
    mount("/api/charsheet_update", routes.charsheets)
    mount("/api/charsheet_delete", routes.charsheets)
    mount("/api/generate_one", routes.images)
    mount("/api/upload_audio", routes.audio)
    mount("/api/transcribe", routes.audio)
    mount("/api/shorts/", shorts.api)
    mount("/api/youtube/", youtube.api)
    mount("/api/control/", control.api)
    import tiktok.api
    mount("/tiktok/oauth/", tiktok.api)
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
