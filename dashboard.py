#!/usr/bin/env python3
"""Localhost-Dashboard für die Storyboard-Bildgenerierung.
Nur Python-Standardlib. Start: python3 dashboard.py [--port 8000]
"""
import os, re, sys, json, time, base64, zipfile, io, threading, concurrent.futures, collections
import urllib.request, urllib.error, subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import shutil
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
KIE_KEY_FILE  = os.path.expanduser("~/.kie_key")
CHANNELS_DIR  = os.path.join(HERE, "channels")
CHANNELS_FILE = os.path.join(CHANNELS_DIR, "channels.json")

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

def _start_job_cleanup_daemon():
    def loop():
        while True:
            time.sleep(1800)  # 30 Minuten
            try:
                _cleanup_stale_jobs()
            except Exception as e:
                print(f"  [Cleanup] Fehler: {e}", flush=True)
    threading.Thread(target=loop, daemon=True).start()

# ── Per-channel path helpers (channel = brand/style, holds N videos) ──────────
def ch_dir(cid):        return os.path.join(CHANNELS_DIR, cid)
def ch_master(cid):     return os.path.join(ch_dir(cid), "master_prompt.txt")
def ch_vid_master(cid): return os.path.join(ch_dir(cid), "video_master_prompt.txt")
def ch_sheets(cid):     return os.path.join(ch_dir(cid), "charsheets")
def ch_videos_file(cid):return os.path.join(ch_dir(cid), "videos.json")
# ElevenLabs voiceover persistence (Phase 1) — one voice_id and one settings block per
# channel, applied to every video unless the video carries its own override later
# (Phase 1 keeps it channel-scoped only).
def ch_voice_id(cid):       return os.path.join(ch_dir(cid), "voice_id.txt")
def ch_voice_settings(cid): return os.path.join(ch_dir(cid), "voice_settings.json")
def get_channel_char_ref(cid: str) -> str:
    p = os.path.join(ch_dir(cid), "char_ref_url.txt")
    try:    return open(p).read().strip()
    except: return ""

# ── Per-video path helpers (one video = one script/plan/generated set) ────────
def v_dir(cid, vid):     return os.path.join(ch_dir(cid), "videos", vid)
def v_out(cid, vid):     return os.path.join(v_dir(cid, vid), "generated")
def v_plan(cid, vid):    return os.path.join(v_out(cid, vid), "plan.json")
def v_uploads(cid, vid): return os.path.join(v_dir(cid, vid), "uploads")
def v_audio(cid, vid):   return os.path.join(v_uploads(cid, vid), "audio_meta.json")
def v_meta(cid, vid):    return os.path.join(v_dir(cid, vid), "meta.json")  # titles, thumbnail prompt
# Deliberately separate from v_out()/generated/ — the render worker rmtree()s this
# directory after a successful render, and that must NEVER be able to reach the folder
# holding the actual generated images/videos.
def v_render_tmp(cid, vid): return os.path.join(v_dir(cid, vid), "render_tmp")

def load_v_meta(cid, vid):
    try:    return json.load(open(v_meta(cid, vid)))
    except: return {"titles": [], "selected_title": "", "thumbnail_prompt": ""}

def save_v_meta(cid, vid, meta):
    json.dump(meta, open(v_meta(cid, vid), "w"), ensure_ascii=False, indent=1)

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
    json.dump(videos, open(ch_videos_file(cid), "w"), ensure_ascii=False, indent=1)

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

IMAGE_MASTER_DEFAULT = """\
CHARACTER VISUAL (used as reference for every image):
Minimalist 2D stick figure — round circle head, straight line limbs, no facial features drawn.
Pure black thin strokes on pure white #FFFFFF background.
No color, no shading, no fill. Flat line art only.

SCENE STYLE:
Each image shows the stick figure in a pose or action that visually represents the scene moment.
Keep proportions consistent. Simple bold lines. White background always empty.
"""

VIDEO_MASTER_DEFAULT = """\
CHARACTER (repeat verbatim in every video prompt):
Simple 2D stick figure — round head, straight limbs, thin black lines on pure white background.

STORY ARC:
[Describe the overall narrative: who is the protagonist, what is their journey,
what are the key emotional beats from beginning to end. 3-5 sentences.]

VISUAL STYLE RULES (always apply):
2D flat line art animation. Pure white #FFFFFF background throughout.
No photorealism. No color. No mouth movement. No lip sync.
Camera: Ken Burns zoom or slow pan matching scene emotion.
"""

# ── Channel list ──────────────────────────────────────────────────────────────
def load_channels():
    try:    return json.load(open(CHANNELS_FILE))
    except: return [{"id": "default", "name": "Kanal 1"}]

def save_channels(chs):
    os.makedirs(CHANNELS_DIR, exist_ok=True)
    json.dump(chs, open(CHANNELS_FILE, "w"), ensure_ascii=False, indent=1)

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
                    json.dump(meta, open(am, "w"))
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

# KIE.ai — image generation
KIE_API      = "https://api.kie.ai/api/v1/jobs"
KIE_MODEL    = "nano-banana-2"
# KIE.ai — text + audio (OpenAI-compatible)
KIE_CHAT_URL = "https://api.kie.ai/gemini-2.5-flash/v1/chat/completions"
# KIE.ai — native Gemini format (contents/parts), used for gemini-3-5-flash which
# supports thinkingConfig.thinkingLevel — helps counteract "lazy"/generic output on
# later items in a batch. Verified working 2026-07-02 against the real API.
GEMINI_NATIVE_URL = "https://api.kie.ai/gemini/v1/models/{model}:generateContent"

# ElevenLabs — Phase 1 voiceover with word-timestamps (single source of truth for scene
# timing when this source is used). Falls back to the existing Gemini-transcription path
# (Option A) for user-uploaded audio. Whisper-alignment in _render_worker keeps the
# timing correction loop unchanged for both paths.
ELEVENLABS_API           = "https://api.elevenlabs.io/v1"
ELEVENLABS_DEFAULT_MODEL = "eleven_multilingual_v2"
ELEVENLABS_KEY_FILE      = os.path.expanduser("~/.elevenlabs_key")

# Shared transcription status (thread-safe via GIL for simple dict ops)
TX_STATUS = {"step": 0, "total": 4, "msg": "Bereit", "running": False, "error": ""}

def tx(step, msg):
    TX_STATUS["step"] = step
    TX_STATUS["msg"] = msg
    print(f"  [TX {step}/{TX_STATUS['total']}] {msg}", flush=True)

def kie_key():
    return open(KIE_KEY_FILE).read().strip()

# ── ElevenLabs voiceover (Phase 1) ────────────────────────────────────────────
# voice_id.txt is plain text (one line) for easy editing; voice_settings.json carries
# the structured controls the API expects in voice_settings. Both are channel-scoped and
# optional — load_voice_settings() falls back to env/defaults when either is missing so
# the user can ship a non-empty ~/.elevenlabs_key without ever touching a settings file.
ELEVENLABS_VOICE_SETTINGS_DEFAULT = {
    "voice_id": "",
    "model_id": ELEVENLABS_DEFAULT_MODEL,
    "stability": 0.5,
    "similarity_boost": 0.75,
    "style": 0.0,
    "use_speaker_boost": True,
    # "output_format" lives outside voice_settings in the ElevenLabs API body, but
    # recording it here makes the persisted snapshot round-trip-able — the generate call
    # reads it explicitly.
    "output_format": "mp3_44100_128",
}

def elevenlabs_key() -> str:
    """Returns the ElevenLabs API key from ~/.elevenlabs_key. Raises with a clear
    message when missing so the user can self-service the setup (matches the kie_key()
    style: simple KeyError-style fail, no implicit config fallback)."""
    p = ELEVENLABS_KEY_FILE
    if not os.path.exists(p):
        raise RuntimeError(
            f"ElevenLabs-Key fehlt: {p} — bitte `echo \"$ELEVENLABS_API_KEY\" > {p} && "
            f"chmod 600 {p}` einmalig ausführen."
        )
    return open(p).read().strip()

def _resolve_voice_id(cid: str, override: str = "") -> str:
    """Resolution order: explicit override → channels/<cid>/voice_id.txt →
    ELEVENLABS_VOICE_DEFAULT env. Empty string means 'no voice configured'."""
    if override:
        return override.strip()
    p = ch_voice_id(cid)
    if os.path.exists(p):
        v = open(p).read().strip()
        if v:
            return v
    return os.environ.get("ELEVENLABS_VOICE_DEFAULT", "").strip()

def load_voice_settings(cid: str, override_voice_id: str = "") -> dict:
    """Returns the merged settings for `cid`. Caller may pass `override_voice_id` from
    the HTTP request to make a per-call choice (used by /api/voiceover_preview to test
    a voice that isn't yet the channel default). All other knobs come from
    channels/<cid>/voice_settings.json when present, else ELEVENLABS_VOICE_SETTINGS_DEFAULT.
    The returned dict always carries every key — never partially populated — so the
    ElevenLabs API call can splat it directly into the request body."""
    s = dict(ELEVENLABS_VOICE_SETTINGS_DEFAULT)
    sp = ch_voice_settings(cid)
    if os.path.exists(sp):
        try:
            saved = json.load(open(sp))
            if isinstance(saved, dict):
                s.update({k: v for k, v in saved.items() if v != "" or k == "voice_id"})
        except Exception as e:
            print(f"  [ElevenLabs] voice_settings.json unlesbar ({e}) — nutze Defaults", flush=True)
    s["voice_id"] = _resolve_voice_id(cid, override_voice_id)
    return s

def save_voice_settings(cid: str, settings: dict) -> None:
    """Persists channels/<cid>/voice_settings.json + voice_id.txt. Drops unknown keys,
    coerces bools, clamps floats to [0,1] where the API expects it."""
    clean = dict(ELEVENLABS_VOICE_SETTINGS_DEFAULT)
    clean.update({k: settings[k] for k in settings if k in clean and k != "voice_id"})
    # numeric clamp for the four sliders
    for k in ("stability", "similarity_boost", "style"):
        try:    clean[k] = max(0.0, min(1.0, float(clean[k])))
        except Exception: clean[k] = ELEVENLABS_VOICE_SETTINGS_DEFAULT[k]
    clean["use_speaker_boost"] = bool(settings.get("use_speaker_boost", True))
    if settings.get("model_id"):
        clean["model_id"] = str(settings["model_id"])
    # separate voice_id.txt (so it can be `cat`-ed and edited without JSON parsing)
    vid = str(settings.get("voice_id", "")).strip()
    if vid:
        with open(ch_voice_id(cid), "w") as f:
            f.write(vid + "\n")
    json.dump(clean, open(ch_voice_settings(cid), "w"), ensure_ascii=False, indent=1)

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
                        thinking_level="high"):
    """KIE.ai native Gemini format (gemini-3-5-flash) — supports thinkingConfig for
    more consistent reasoning on later items in a batch. `messages` uses the same
    [{"role","content"}] shape as post_kie_text() for drop-in compatibility;
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
               "maxOutputTokens": 8192}
    if json_mode:
        gen_cfg["responseMimeType"] = "application/json"
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
        print(f"  [Gemini3.5] Fehler, ein Retry: {e}", flush=True)
        return _do_call()

# ---------- Master-Prompt ----------
def read_master(cid="default"):
    try:    return open(ch_master(cid), encoding="utf-8").read().strip()
    except: return ""

def write_master(cid, txt):
    open(ch_master(cid), "w", encoding="utf-8").write(txt.strip() + "\n")

# ---------- Skript-Generator (Simplicissimus-Stil) ----------

SCRIPT_SYSTEM = """\
You are a documentary script writer. Your style matches Simplicissimus — the German YouTube channel known for narrative-documentary storytelling with investigative tension.

STORYTELLING SCHEMA (always use this structure):
1. HOOK: Open with ONE concrete person, moment, or place. Short, punchy sentences. No explanations yet — just tension.
2. BUILD-UP: Introduce the central phenomenon through the specific case. Alternate facts with atmosphere.
3. ESCALATION: Each new chapter raises the stakes. End chapters with tension, not closure — micro-cliffhangers.
4. BROADER PATTERN: Zoom out from the specific case to the systemic/global picture.
5. HUMAN COST: Ground the abstract in real human consequences. One concrete example.
6. CLOSING: Leave the audience with an uncomfortable truth, an open question, or a realization that goes beyond the story.

STYLE RULES:
- Sentence rhythm: short punchy sentence. Then a slightly longer analytical one. Then short again.
- Never open a chapter with a fact. Open with a scene, a person, or a rhetorical question.
- Voiceover pace: ~150 words per minute. Each chapter: 80–150 words. Total: 8–14 chapters.
- Tone: calm, analytical, journalistic — not sensational, not preachy.
- Transitions between chapters must create forward momentum, not summarize what just happened.
- Output clean voiceover text only. No stage directions. No brackets. No parentheses.
- Chapter titles as ## headings. Blank line between paragraphs.
- The output must NOT be word-for-word identical to the input — it must be freshly written in this style.
"""

def generate_script(raw_input: str, lang: str) -> str:
    lang_instr = (
        "Write the script in German (natural spoken German, not formal)."
        if lang == "de"
        else "Write the script in English (clear, neutral international English)."
    )
    user_msg = (
        f"{lang_instr}\n\n"
        f"Here is the raw input — a transcript, rough notes, or video ideas. "
        f"Rewrite it as a polished documentary voiceover script following the schema above. "
        f"Keep all key facts and arguments, but rephrase everything freshly:\n\n"
        f"{raw_input}"
    )
    msgs = [
        {"role": "system", "content": SCRIPT_SYSTEM},
        {"role": "user", "content": user_msg},
    ]
    return post_kie_text(msgs, temp=0.8)


# ---------- Title generator (viral/clickbait, research-backed formulas) ----------
# Formulas per 2026 CTR research: curiosity gap + loss-aversion/FOMO + a concrete
# number or fact + an emotional hook, 55-60 chars so it doesn't truncate on mobile.
# "Exaggerate the tension, not the outcome" — titles must stay factually accurate to
# the script, no fabricated claims.

TITLE_SYSTEM = """\
You are a YouTube title strategist. You write titles using proven high-CTR formulas,
but you NEVER misrepresent what the video actually contains — you exaggerate the
TENSION and stakes already present in the script, never invent a claim the script
doesn't support. Misleading clickbait is not acceptable; a strong honest hook is.

FORMULAS TO DRAW FROM (mix, don't just pick one every time):
- Curiosity gap: hint at a shocking fact/connection without revealing it
- Number-based: "[Number] [Things] That [Concrete Result]"
- Loss-aversion / FOMO: what the viewer doesn't know yet, what they're missing
- Personal-authority: "[credible framing]. Here's what [it] means for you."

RULES:
- 55-60 characters total (titles longer than this get truncated on mobile — this is
  a hard constraint, not a suggestion)
- Every claim in the title must be directly supported by the script content given
- No emoji, no ALL CAPS spam, no exclamation-mark stacking
- Return options in the exact language the script is written in
"""

def generate_titles(full_script: str, n: int = 5) -> list:
    """Generate N candidate clickbait-but-honest titles from the full script."""
    user_msg = (
        f"Generate {n} distinct YouTube title options for this script, using the "
        f"formulas above. Return ONLY a JSON array of {n} strings, nothing else.\n\n"
        f"SCRIPT:\n{full_script.strip()[:6000]}"
    )
    try:
        txt = post_gemini_native([
            {"role": "system", "content": TITLE_SYSTEM},
            {"role": "user", "content": user_msg},
        ], json_mode=True, temp=0.9)
        arr = json.loads(txt)
        if isinstance(arr, dict):
            for v in arr.values():
                if isinstance(v, list): arr = v; break
        if isinstance(arr, list):
            return [str(t).strip() for t in arr][:n]
    except Exception as e:
        print(f"  [Title] Fehler: {e}", flush=True)
    return []


# ---------- Thumbnail generator ----------
# Research-backed rules (2026 CTR studies): one dominant subject, one message, one
# second to understand. Strong contrast (dark bg + light subject, or reverse).
# Expressive/exaggerated emotion — thumbnails with visible expression see 20-30%
# higher CTR. Max 3-5 words of on-image text (under 4 words = ~30% higher CTR than
# text-heavy designs). 2-3 colors max. 1280x720 (16:9), sharp focus, rule of thirds.

THUMBNAIL_PROMPT_SYSTEM = """\
You write a single image-generation prompt for a YouTube THUMBNAIL — this is a
fundamentally different job than a storyboard scene. A thumbnail must work as a tiny,
high-contrast image glanced at for under a second in a crowded feed. Apply these
non-negotiable rules:

1. ONE dominant subject only — the main character or the single most concrete symbol
   of the video's hook. No busy multi-element scenes.
2. STRONG CONTRAST — either a light subject on a dark background or a dark subject on
   a light background. Never a low-contrast, evenly-lit scene.
3. EXAGGERATED, READABLE EMOTION on the subject if it's a character — shock, alarm,
   intense focus, fear, urgency. Subtle/neutral expressions do not work for thumbnails.
4. RULE OF THIRDS — subject off-center, clear headroom, nothing important near the edges.
5. NO more than one small supporting prop/symbol tied directly to the video's hook.
6. Do not describe on-image text here — text is composited separately.
7. Keep the established character/art style exactly as given in STYLE CONTEXT, but push
   the POSE, EXPRESSION, and LIGHTING to thumbnail-appropriate extremes — a thumbnail
   is the most exaggerated, highest-contrast frame of the whole video, not a typical one.

Output ONE dense paragraph, 50-70 words. Start with the subject and its expression.
"""

def make_thumbnail_prompt(full_script: str, master_style: str) -> str:
    """Builds the single most attention-grabbing image prompt for this video's thumbnail,
    grounded in the actual hook/subject of the script (not a generic dramatic pose)."""
    user_msg = (
        f"STYLE CONTEXT (character/art style — follow exactly, push expression/lighting "
        f"to thumbnail extremes):\n{master_style.strip()}\n\n"
        f"FULL SCRIPT — identify the single most shocking/central hook and depict that:\n"
        f"{full_script.strip()[:4000]}\n\n"
        f"Write the thumbnail image prompt now."
    )
    try:
        return post_gemini_native([
            {"role": "system", "content": THUMBNAIL_PROMPT_SYSTEM},
            {"role": "user", "content": user_msg},
        ], temp=0.7).strip()
    except Exception as e:
        print(f"  [Thumbnail] Prompt-Fehler: {e}", flush=True)
        return "A single figure in a moment of shocked realization, strong dramatic lighting, high contrast."


def gen_thumbnail_image(prompt: str, master_style: str, out_path: str,
                         model: str = "nano-banana-2", ref_urls: list = None) -> dict:
    """Submits + polls + downloads a 16:9 thumbnail image. Reuses the same KIE image
    pipeline as scene generation, just with thumbnail-specific dimensions/prompt."""
    full_prompt = prompt.strip() + "\n\n" + master_style.strip()
    try:
        task_id = _kie_submit_image(full_prompt, model=model, ref_urls=ref_urls)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    print(f"  [Thumbnail] Task {task_id} läuft …", flush=True)
    poll_url  = f"{KIE_API}/recordInfo?taskId={task_id}"
    poll_hdrs = {"Authorization": f"Bearer {kie_key()}"}
    for poll_i in range(50):
        time.sleep(3)
        try:
            with urllib.request.urlopen(urllib.request.Request(poll_url, headers=poll_hdrs), timeout=15) as r2:
                info = json.load(r2).get("data", {})
        except Exception as e:
            print(f"  [Thumbnail] Poll-Fehler: {e}", flush=True); continue
        state = info.get("state", "")
        if state != "waiting" or poll_i % 5 == 0:
            print(f"  [Thumbnail] {state}", flush=True)
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
                return {"ok": False, "error": f"Download fehlgeschlagen: {e}"}
            return {"ok": True, "file": os.path.basename(out_path), "source_url": urls[0]}
        if state == "fail":
            return {"ok": False, "error": f"KIE fehlgeschlagen: {info.get('failMsg','unbekannt')}"}
    return {"ok": False, "error": "KIE Timeout (>150s)"}

# ---------- Skript -> Beats (inhaltlich, nach Zeit/Wort) ----------
def clean_script(s):
    s = re.sub(r"\(?\b\d{1,2}:\d{2}\b\)?", " ", s)   # Timestamps entfernen
    s = re.sub(r"\s+", " ", s).strip()
    return s

def split_units(text):
    # Sätze, lange Sätze zusätzlich an Kommas/Semikola
    out = []
    for sent in re.findall(r"[^.!?]+[.!?]?", text):
        sent = sent.strip()
        if not sent:
            continue
        if len(sent.split()) <= 22:
            out.append(sent)
        else:
            for part in re.split(r"(?<=[,;:])\s+", sent):
                if part.strip():
                    out.append(part.strip())
    return out

MAX_SCENE_SEC = 6.0          # hard cap — no scene may hold longer than this, regardless of pacing label
PACING_TARGET_SEC = {"calm": 5.0, "punchy": 1.1}   # "normal" target comes from the user's own sec-per-image input
NORMAL_HARD_CAP_SEC = 5.5     # Quick-Win Q (2026-07): cap the "normal" target at 5.5s.
                               # Previous value was 4.0s — too tight for narrative mid-form docs
                               # (~120 scenes per 8-min script). At sec_per_img=5.5 the same script
                               # yields ~87 scenes, ~25-33% fewer cuts, still below MAX_SCENE_SEC.
PACING_WARN_THRESHOLD = 0.30  # if >30% of units come back "punchy", the classifier likely over-fired on drama
                               # rather than found real reveals/cliffhangers — warn instead of silently
                               # exploding the scene (and KIE credit) count.

def segment_by_pacing(units: list, pacing: list, wpm: float, normal_sec: float,
                       sequences: list = None, callouts: list = None) -> list:
    """Groups atomic text units (from split_units) into scenes using a per-unit pacing
    label ("calm"/"normal"/"punchy") that analyze_script() already assigned in the SAME
    pass it used to read the emotional arc — so pacing can't drift from what the model
    already decided is the climax vs. the setup (two independent judgments of "how
    important is this moment" would eventually disagree with each other).

    calm beats can be grouped together and held up to MAX_SCENE_SEC; punchy beats are
    never merged with neighbors and get compressed to ~1s each (occasionally split into
    two rapid images for a "gut punch"); normal beats use the user's own sec-per-image
    dial. Missing/unparseable pacing data (e.g. analyze_script failed) defaults every
    unit to "normal", which reproduces the old fixed-interval behavior exactly — same
    resilience pattern as the rest of the pipeline's LLM-call fallbacks.

    `sequences` (optional): analyze_script()'s "visual_sequences" list, same index space
    as `pacing` (unit indices, NOT final scene indices — units get grouped/split below,
    so a unit's position and a scene's position are not 1:1 here, unlike the audio-
    transcription path where scenes already equal beats before analyze_script runs). A
    sequence boundary forces a scene cut exactly like a pacing-label change already does,
    so a "calm" merge can never silently span across two different sequences. seq_pos is
    NOT taken from the LLM's per-unit value — it's reassigned 0,1,2... per seq_id AFTER
    grouping, since one scene can now represent multiple original units (calm merge) or
    one unit can become two scenes (punchy split), so the raw per-unit position no longer
    lines up with the final scene count."""
    label_by_i = {p.get("beat"): p.get("label", "normal") for p in (pacing or []) if isinstance(p, dict)}
    seq_by_i = {}
    reason_by_sid = {}
    for seq in (sequences or []):
        if not isinstance(seq, dict):
            continue
        sid = seq.get("seq_id")
        if sid is not None and seq.get("reason"):
            reason_by_sid[sid] = seq["reason"]
        for beat_i in seq.get("beats", []) or []:
            if sid is not None:
                seq_by_i[beat_i] = sid
    callout_by_i = {c.get("beat"): c.get("text") for c in (callouts or [])
                    if isinstance(c, dict) and c.get("text")}
    targets = {"calm": PACING_TARGET_SEC["calm"],
               "normal": max(1.5, min(normal_sec, NORMAL_HARD_CAP_SEC)),
               "punchy": PACING_TARGET_SEC["punchy"]}
    hard_cap_words = max(3, round(MAX_SCENE_SEC * wpm / 60.0))

    n_punchy = sum(1 for i in range(len(units)) if label_by_i.get(i, "normal") == "punchy")
    if units and n_punchy / len(units) > PACING_WARN_THRESHOLD:
        print(f"  [Plan] WARNUNG: {n_punchy}/{len(units)} Einheiten als 'punchy' eingestuft "
              f"({n_punchy/len(units)*100:.0f}%) — ungewöhnlich hoch, ggf. Fehlklassifizierung", flush=True)

    segs, seg_seq_ids, seg_labels, seg_callouts = [], [], [], []
    cur, cur_label, cur_seq, cur_callout = [], None, None, None
    def flush():
        nonlocal cur, cur_seq, cur_callout
        if cur:
            segs.append(" ".join(cur))
            seg_seq_ids.append(cur_seq)
            seg_labels.append(cur_label)
            seg_callouts.append(cur_callout)
            cur = []
            cur_seq = None
            cur_callout = None

    for i, u in enumerate(units):
        label = label_by_i.get(i, "normal")
        if label not in targets:
            label = "normal"
        seq_id = seq_by_i.get(i)
        callout = callout_by_i.get(i)
        words = u.split()
        target_words = max(2, round(targets[label] * wpm / 60.0))

        if label == "punchy":
            # Never merged with neighbors. If the unit is long enough to naturally carry
            # two quick hits, split it into two punchy-length pieces instead of one
            # longer one — the "two rapid images" gut-punch effect. Falls back to
            # hard-cap-sized chunks instead if it's so long that even halving it would
            # still blow MAX_SCENE_SEC (rare, but a single scene must never exceed it).
            # Every resulting piece inherits this unit's seq_id/callout (if any) —
            # splitting a unit never breaks it out of its sequence or drops its callout.
            flush()
            if len(words) > hard_cap_words * 2:
                for j in range(0, len(words), hard_cap_words):
                    segs.append(" ".join(words[j:j+hard_cap_words])); seg_seq_ids.append(seq_id); seg_labels.append(label); seg_callouts.append(callout)
            elif len(words) > target_words * 1.8:
                mid = len(words) // 2
                segs.append(" ".join(words[:mid])); seg_seq_ids.append(seq_id); seg_labels.append(label); seg_callouts.append(callout)
                segs.append(" ".join(words[mid:])); seg_seq_ids.append(seq_id); seg_labels.append(label); seg_callouts.append(callout)
            else:
                segs.append(u); seg_seq_ids.append(seq_id); seg_labels.append(label); seg_callouts.append(callout)
            cur_label = None
            continue

        # A single unit can itself be longer than the hard cap (e.g. one long calm
        # sentence) — split it on its own before any merging logic runs, otherwise it
        # becomes one indivisible scene that blows past MAX_SCENE_SEC no matter what
        # target/label says (the exact bug class fixed once already for the old
        # fixed-interval segment(), reappearing here for the same underlying reason).
        if len(words) > hard_cap_words:
            flush()
            for j in range(0, len(words), hard_cap_words):
                segs.append(" ".join(words[j:j+hard_cap_words])); seg_seq_ids.append(seq_id); seg_labels.append(label); seg_callouts.append(callout)
            cur_label = None
            continue

        # calm/normal: keep grouping with the running buffer as long as the label AND
        # sequence membership match, and doing so doesn't blow the hard MAX_SCENE_SEC
        # cap; otherwise start fresh. A sequence boundary is treated exactly like a
        # label change — both force a cut.
        if cur and (cur_label != label or cur_seq != seq_id or len(cur) + len(words) > hard_cap_words):
            flush()
        cur.extend(words)
        cur_label = label
        cur_seq = seq_id
        if callout:
            cur_callout = callout
        if len(cur) >= target_words:
            flush()
            cur_label = None
    flush()

    scenes, t = [], 0.0
    for i, seg in enumerate(segs):
        dur = len(seg.split()) / (wpm / 60.0)
        scene = {"i": i, "start": round(t, 1), "dur": round(dur, 1), "text": seg,
                 "pacing": seg_labels[i] or "normal"}
        if seg_seq_ids[i] is not None:
            scene["seq_id"] = seg_seq_ids[i]
            reason = reason_by_sid.get(seg_seq_ids[i])
            if reason:
                scene["seq_reason"] = reason
        if seg_callouts[i]:
            scene["callout"] = seg_callouts[i]
        scenes.append(scene)
        t += dur

    _renumber_seq_pos(scenes)
    return scenes

def _renumber_seq_pos(scenes: list) -> None:
    """Assigns seq_pos 0,1,2... per seq_id in final scene order, in-place. Used by both
    segmentation paths: the manual-script path (where merging/splitting means the LLM's
    raw per-unit position no longer lines up with final scenes) and the audio-
    transcription path (where it's defensive — the LLM's "beats" list should already be
    in order, but trusting our own recount instead of the raw value costs nothing and
    guards against an out-of-order response)."""
    seq_counters = {}
    for scene in scenes:
        sid = scene.get("seq_id")
        if sid is None:
            continue
        scene["seq_pos"] = seq_counters.get(sid, 0)
        seq_counters[sid] = scene["seq_pos"] + 1

def _apply_visual_sequences_direct(scenes: list, sequences: list) -> None:
    """Audio-transcription path only: scenes are already 1:1 with the beats given to
    analyze_script() (no grouping/splitting happens for this path), so seq_id can be
    assigned by direct index instead of tracked through segmentation like the manual
    path needs (see segment_by_pacing's `sequences` handling)."""
    for seq in (sequences or []):
        if not isinstance(seq, dict):
            continue
        sid = seq.get("seq_id")
        if sid is None:
            continue
        reason = seq.get("reason")
        for beat_i in seq.get("beats", []) or []:
            if isinstance(beat_i, int) and 0 <= beat_i < len(scenes):
                scenes[beat_i]["seq_id"] = sid
                # Only needed on the anchor (seq_pos==0 after renumbering below), but
                # stored on every beat of the sequence since we don't know yet which one
                # will end up as the anchor — cheap, and unused fields are harmless.
                if reason:
                    scenes[beat_i]["seq_reason"] = reason
    _renumber_seq_pos(scenes)

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
        'string, "anonymize": bool, "first_appears_beat": N}],\n'
        '  "recurring_symbols": [{"id": "sym_01", "object": string, "meaning": string, '
        '"beats": [N, N]}],\n'
        '  "emotional_arc": {"opening": "ONE word", "midpoint": "ONE word", "resolution": "ONE word"},\n'
        '  "callbacks": [{"from_beat": N, "to_beat": M, "shared_element": string}],\n'
        '  "pacing": [{"beat": N, "label": "calm" | "normal" | "punchy"}],\n'
        '  "visual_sequences": [{"seq_id": N, "beats": [N, N, N], "reason": string, '
        '"camera": "slow push-in" | "pan" | "static series"}],\n'
        '  "callouts": [{"beat": N, "text": "short number/date/stat, max ~6 chars"}],\n'
        '  "phases": [{"beat": N, "phase": "OPENING" | "RISING_ACTION" | "CLIMAX" | "RESOLUTION"}],\n'
        '  "act_breaks": [N],\n'
        '  "climax_beat": N\n'
        "}\n\n"
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
        "BEATS:\n" + json.dumps(beats, ensure_ascii=False)
    )
    for attempt in (1, 2):
        try:
            txt = post_gemini_native([{"role": "user", "content": instr}], json_mode=True, temp=0.2)
            return json.loads(txt)
        except Exception as e:
            print(f"Analyse-Fehler (Versuch {attempt}):", e)
    return {}

IMAGE_PROMPT_CHUNK_SIZE = 20   # scenes per LLM call. Bigger chunk = fewer calls = the analysis
# JSON + few-shot examples + style context (repeated in full on EVERY chunk call) get sent
# far fewer times — that repeated overhead, not raw call count, is the real cost driver.
# 20 is a middle ground: cuts repeated-context cost ~55% vs. the earlier value of 9, while
# thinkingLevel=high keeps later-in-chunk quality from degrading like it did on 2.5-flash.
IMAGE_PROMPT_MIN_LEN    = 220  # chars — stills need less than video (no camera-move description) but still concrete

_IMAGE_PROMPT_FEWSHOT = """\
EXAMPLE — TOO SHORT / MISSES THE CONTENT (do not do this):
Line: "Reports suggested that people around him were monitored before his murder."
Bad image_prompt: "Dark ominous scene, surveillance concept"
→ Wrong: doesn't say WHO was monitored, doesn't show the surveillance mechanism, loses the actual fact.

EXAMPLE — CORRECT:
Line: "Reports suggested that people around him were monitored before his murder."
core_statement: "The target's inner circle was surveilled before his death."
concrete_entity: "char_target (anonymized), sym_surveillance_device"
Good image_prompt: "An empty chair in a press room, a phone resting on the floor beside it,
a faint glow on the phone screen suggesting active surveillance, dim somber lighting, nobody
visible in frame, composition emphasizing absence and unease"
→ Why better: translates "inner circle monitored" into a concrete object (glowing phone =
surveillance symbol), and is specific enough to define setting/light/focus — not just a mood word.\
"""

def _anonymized_words(analysis: dict) -> set:
    """Words belonging to characters marked anonymize=true in the Stage-1 analysis.
    These must NOT be required to appear literally in a prompt — the whole point of
    anonymize=true is that the person is depicted as a silhouette/symbol, never named."""
    words = set()
    for c in (analysis or {}).get("characters", []):
        if c.get("anonymize"):
            for field in (c.get("id", ""), c.get("name_or_role", "")):
                words.update(w.lower() for w in re.findall(r"[a-zA-Z]{4,}", field))
    return words

def _validate_image_prompt_entry(entry: dict, anonymized_words: set = frozenset()) -> bool:
    ip = (entry.get("image_prompt") or "").strip()
    if len(ip) < IMAGE_PROMPT_MIN_LEN:
        return False
    entity = (entry.get("concrete_entity") or "").strip().lower()
    if entity and entity not in ("none", "n/a", "-"):
        words = [w for w in re.findall(r"[a-zA-Z]{4,}", entity)
                 if w not in ("char", "loc", "sym", "anonymized") and w.lower() not in anonymized_words]
        if words and not any(w.lower() in ip.lower() for w in words):
            return False
    return True

def _image_prompt_chunk(chunk_beats: list, chunk_offset: int, total: int,
                         analysis_ctx: str) -> list:
    """One LLM call for a small chunk of scenes (still images — no story-phase/camera-move
    logic like video; instead forces explicit character-consistency notes, since stills have
    no 'last frame of previous clip' anchor to inherit continuity from)."""
    numbered = "\n".join(f"{chunk_offset+i+1}. {t}" for i, t in enumerate(chunk_beats))

    instr = f"""\
You are a storyboard director turning narration into single still images. You receive a
structural ANALYSIS of the full script and a CHUNK of consecutive narrator lines. Work
through each line using the forced fields below — do not skip straight to the final prompt.

ANALYSIS (entities, locations, symbols, emotional arc, callbacks — extracted from the FULL script):
{analysis_ctx}

{_IMAGE_PROMPT_FEWSHOT}

For EACH line in the chunk below, produce an object with ALL of these fields, in order:
{{
  "scene": N,
  "core_statement": "What is this line actually claiming/showing? One sentence.",
  "concrete_entity": "The EXACT entity id from ANALYSIS (locations/characters/recurring_symbols)
                       relevant here. If none fits, name the new concrete thing from the line
                       itself (person/place/object/technology). Abstract metaphor ONLY if the
                       line truly has no concrete referent.",
  "callback_check": "Does ANALYSIS.callbacks say this scene references an earlier one? If yes,
                      name the recurring element that MUST appear in image_prompt. Else 'none'.",
  "character_consistency": "Since this is a single still with no motion/continuity anchor from
                             a previous clip, restate exactly how the character(s) must look
                             (from ANALYSIS.characters visual_description) so every frame stays
                             identical — head shape, proportions, distinguishing features.",
  "image_prompt": "The final image text. MUST visibly include concrete_entity AND the
                    callback_check element (if not 'none'). MUST reflect character_consistency
                    exactly if a character appears. NO art-style words here (line weight, color
                    palette etc. — that's applied separately from the master prompt). Must
                    explicitly name: (1) the concrete main subject, (2) the setting/location,
                    (3) the composition/framing. A prompt that only describes a vague mood
                    without these three elements is invalid. Minimum {IMAGE_PROMPT_MIN_LEN} characters."
}}

HARD RULE: if a line names a concrete person, place, or technology, image_prompt MUST show
exactly that — check this yourself against your own concrete_entity field before writing it.

SENSITIVE content (violence/death/abuse/trafficking): tasteful symbolism only, never graphic.

NARRATOR LINES IN THIS CHUNK:
{numbered}

Return a JSON array of {len(chunk_beats)} objects, one per line above, in the same order.
"""
    txt = post_gemini_native([{"role": "user", "content": instr}], json_mode=True, temp=0.6)
    arr = json.loads(txt)
    if isinstance(arr, dict):
        for v in arr.values():
            if isinstance(v, list) and len(v) == len(chunk_beats):
                arr = v; break
    if not isinstance(arr, list) or len(arr) != len(chunk_beats):
        raise ValueError(f"unexpected chunk response shape ({type(arr)}, len={len(arr) if isinstance(arr,list) else '?'})")
    return arr

def _image_prompt_single_retry(beat_text: str, beat_i: int, total: int, analysis_ctx: str) -> dict:
    """Focused single-scene retry for entries that failed validation in the batch call."""
    try:
        result = _image_prompt_chunk([beat_text], beat_i, total, analysis_ctx)
        return result[0]
    except Exception as e:
        print(f"  [Plan] Bild-Einzel-Retry Szene {beat_i} fehlgeschlagen: {e}", flush=True)
        return {
            "scene": beat_i + 1, "concrete_entity": "",
            "image_prompt": f"Scene illustrating: {beat_text[:80]}. Simple, clear composition.",
        }

def visual_prompts(scenes, analysis=None):
    """Generate all still-image prompts, chunked (not all-in-one) with forced intermediate
    reasoning fields and a validation+retry pass — same structure as video_prompts_batch(),
    adapted for stills (no story-phase/camera-move logic, explicit character-consistency
    field instead since there's no chain-extend anchor between separate images).

    Returns list of {"prompt": str, "concrete_entity": str} dicts, one per scene, same
    order as scenes. concrete_entity is already computed per entry for validation
    purposes below — it used to be discarded after that; now it's returned too so
    callers can persist it onto the scene (used for conditional character-reference
    attachment, see _batch_generate_worker). Style (master prompt) is NOT included in
    the prompt text — it's appended separately in _build_image_prompt().
    """
    beats = [s["text"] for s in scenes]
    total = len(beats)
    if total == 0:
        return []

    if analysis is None:
        print(f"  [Plan] Analysiere {total} Beats …", flush=True)
        analysis = analyze_script(beats)
    analysis_ctx = json.dumps(analysis, ensure_ascii=False, indent=1) if analysis else "{}"
    anon_words = _anonymized_words(analysis)

    def _fetch_image_chunk(chunk, chunk_offset):
        """Try the chunk; on failure (incl. truncated/malformed JSON on large chunks),
        split in half and retry each half instead of giving up the whole chunk to the
        generic fallback — a truncation only costs half the chunk, not all of it."""
        try:
            return _image_prompt_chunk(chunk, chunk_offset, total, analysis_ctx)
        except Exception as e:
            if len(chunk) <= 1:
                print(f"  [Plan] Bild-Chunk-Fehler (Szene {chunk_offset}): {e} — Fallback", flush=True)
                return [{"image_prompt": f"Scene illustrating: {chunk[0][:80]}. Simple, clear composition.",
                         "concrete_entity": ""}]
            mid = len(chunk) // 2
            print(f"  [Plan] Bild-Chunk-Fehler: {e} — teile Chunk und wiederhole …", flush=True)
            left  = _fetch_image_chunk(chunk[:mid], chunk_offset)
            right = _fetch_image_chunk(chunk[mid:], chunk_offset + mid)
            return left + right

    prompts: list[str] = []
    chunks = [beats[i:i+IMAGE_PROMPT_CHUNK_SIZE] for i in range(0, total, IMAGE_PROMPT_CHUNK_SIZE)]
    offset = 0
    for ci, chunk in enumerate(chunks):
        print(f"  [Plan] Bild-Chunk {ci+1}/{len(chunks)} ({len(chunk)} Szenen) …", flush=True)
        entries = _fetch_image_chunk(chunk, offset)

        for j, entry in enumerate(entries):
            beat_i = offset + j
            if not _validate_image_prompt_entry(entry, anon_words):
                print(f"  [Plan] Szene {beat_i} zu kurz/generisch — Einzel-Retry …", flush=True)
                entry = _image_prompt_single_retry(beats[beat_i], beat_i, total, analysis_ctx)
            prompts.append({
                "prompt": str(entry.get("image_prompt") or f"Scene illustrating: {beats[beat_i][:80]}."),
                "concrete_entity": str(entry.get("concrete_entity") or ""),
            })
        offset += len(chunk)

    return prompts

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
PHASE_SET = {"OPENING", "RISING_ACTION", "CLIMAX", "RESOLUTION"}
PHASE_TO_ACT = {"OPENING": 0, "RISING_ACTION": 1, "CLIMAX": 2, "RESOLUTION": 3}
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
    print(f"  [Phase] {n_llm}/{total} LLM, {n_fb}/{total} fallback "
          f"(coverage={coverage*100:.0f}%, hysteresis={'ON' if use_llm else 'OFF'})", flush=True)

VIDEO_PROMPT_MIN_LEN    = 280  # chars — forces setting/light/camera to be spelled out, not just a mood word

# ---------- Character sheets ----------

def load_char_refs(cid="default"):
    refs = []
    for f in os.listdir(ch_sheets(cid)):
        if f.endswith(".json"):
            try:
                meta = json.load(open(os.path.join(ch_sheets(cid), f)))
                refs.append(meta)
            except Exception:
                pass
    return refs

def analyze_char_image(img_bytes, mime="image/png"):
    """Ask Gemini Vision to extract a text-only design description from a reference image."""
    instr = (
        "This image shows a character to be used as a visual design reference for a stick-figure animation. "
        "Write a precise CHARACTER DESIGN SPECIFICATION based on what you see. "
        "Describe ONLY the design elements: head shape and size relative to body, body proportions, "
        "line weight (thin/medium/thick), clothing details, eye style, mouth style, any distinguishing marks. "
        "Do NOT describe the pose, walking direction, or composition — only the visual design. "
        "Write as a concise spec (max 80 words) that could be used to draw this character consistently in any pose."
    )
    b64 = base64.b64encode(img_bytes).decode()
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": instr},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
    ]}]
    return post_kie_text(msgs, temp=0.2).strip()


def gen_charsheet(cid, name, description):
    """Generate a character reference sheet image and return the bytes."""
    prompt = (
        f"CHARACTER REFERENCE SHEET — '{name}'.\n"
        f"Draw this stick figure in 5 different poses, arranged in a single horizontal row on a white background. "
        f"Label each pose below it with its name.\n\n"
        f"Poses: 1-Neutral (standing still) · 2-Happy (arms up, curved smile) · "
        f"3-Sad (shoulders drooping, frown) · 4-Shocked (arms spread wide, circle mouth) · "
        f"5-Walking (one leg forward)\n\n"
        f"Character design: {description}\n\n"
        f"All 5 poses MUST share identical proportions, head size, and identifying features. "
        f"White background (#FFFFFF), black ink only, medium-weight lines, no shading. "
        f"Label each pose clearly below in small neat text."
    )
    tmp = os.path.join(ch_sheets(cid), f"_tmp_{re.sub(r'[^\\w]','_',name)}.jpg")
    res = gen_image(prompt, "", tmp)
    if res["ok"]:
        data = open(tmp, "rb").read()
        try: os.unlink(tmp)
        except: pass
        return data
    raise RuntimeError(f"Character sheet generation failed: {res.get('error')}")


# ---------- Bildgenerierung via KIE.ai ----------

def _build_image_prompt(scene_prompt, master, char_refs):
    char_hint = ""
    if char_refs:
        for cr in char_refs:
            desc = cr.get("description", ""); name = cr.get("name", "Figur")
            if desc:
                char_hint += (f"\n\nCHARACTER DESIGN for '{name}': {desc}"
                              f"\nApply this exact design in whatever pose this scene requires.")
    return scene_prompt + char_hint + "\n\n" + master


def _build_video_prompt(scene_prompt: str, vid_master: str) -> str:
    """Append the literal master prompt to the scene action description.
    Veo only ever sees the final submitted string — it has no access to the
    dashboard's master prompt field, so the style must be embedded here every
    time, not just hinted to the LLM that writes the scene description."""
    return scene_prompt.strip() + "\n\nVISUAL STYLE (apply exactly):\n" + vid_master.strip()


VALID_IMAGE_MODELS = ("nano-banana-2", "nano-banana-2-lite")

# KIE's real documented limit is 20 submissions per 10s account-wide. Concurrent batch
# dispatch (MAX_CONCURRENT_IMAGE_GENS) has no natural pacing of its own — if several
# scenes finish or fail in quick succession, the freed slots all resubmit near-instantly,
# which live testing showed can burst past 20/10s and trigger KIE's "call frequency too
# high" error for a whole cascade of scenes at once. This tracks recent submission
# timestamps process-wide and makes every submitter wait its turn, capped comfortably
# under the real ceiling.
_KIE_SUBMIT_TIMES = collections.deque()
_KIE_SUBMIT_LOCK = threading.Lock()
KIE_SUBMIT_RATE_LIMIT = 12
KIE_SUBMIT_RATE_WINDOW = 10.0

def _kie_rate_limit_wait():
    while True:
        with _KIE_SUBMIT_LOCK:
            now = time.time()
            while _KIE_SUBMIT_TIMES and now - _KIE_SUBMIT_TIMES[0] > KIE_SUBMIT_RATE_WINDOW:
                _KIE_SUBMIT_TIMES.popleft()
            if len(_KIE_SUBMIT_TIMES) < KIE_SUBMIT_RATE_LIMIT:
                _KIE_SUBMIT_TIMES.append(now)
                return
            wait_for = KIE_SUBMIT_RATE_WINDOW - (now - _KIE_SUBMIT_TIMES[0]) + 0.05
        time.sleep(max(wait_for, 0.05))

def _kie_submit_image(full_prompt: str, model: str = "nano-banana-2", ref_urls: list = None) -> str:
    """Submit image task to KIE, return task_id.

    ref_urls: reference image URL(s) for visual consistency. IMPORTANT — the correct
    field per KIE's actual docs is "image_input" for nano-banana-2 (up to 14 images) and
    "image_urls" for nano-banana-2-lite (up to 10). Using the wrong field name silently
    does nothing — KIE accepts the request (200 OK) but the reference has zero effect on
    the output, which is exactly the bug this fixes (verified empirically: submitting
    "image_urls" against nano-banana-2 produced a result with no resemblance at all to
    the reference image)."""
    if model not in VALID_IMAGE_MODELS:
        model = "nano-banana-2"
    hdrs = {"Authorization": f"Bearer {kie_key()}", "Content-Type": "application/json"}
    input_body = {
        "prompt": full_prompt, "aspect_ratio": "16:9",
        "resolution": "2K", "output_format": "jpg",
    }
    if ref_urls:
        ref_field = "image_input" if model == "nano-banana-2" else "image_urls"
        input_body[ref_field] = ref_urls[:14 if model == "nano-banana-2" else 10]
    body = {"model": model, "input": input_body}
    req_data = json.dumps(body).encode()

    last_err = None
    for attempt in range(4):
        _kie_rate_limit_wait()
        req = urllib.request.Request(f"{KIE_API}/createTask", data=req_data, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"KIE HTTP {e.code}: {e.read().decode()[:200]}")
        if resp.get("code") == 200:
            return resp["data"]["taskId"]
        msg = resp.get("msg", str(resp))
        last_err = msg
        # "Your call frequency is too high" is transient and self-inflicted by our own
        # burst — worth a short backoff + retry instead of giving up the whole scene.
        # Anything else (e.g. insufficient credits) is not transient, fail immediately.
        if "frequency" in msg.lower() and attempt < 3:
            print(f"  [KIE] Rate-Limit getroffen, warte {2*(attempt+1)}s und versuche erneut …", flush=True)
            time.sleep(2 * (attempt + 1))
            continue
        raise RuntimeError(f"KIE: {msg}")
    raise RuntimeError(f"KIE: {last_err}")


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
            json.dump(plan, open(plan_path, "w"), ensure_ascii=False, indent=1)
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
                    json.dump(plan, open(plan_path, "w"), ensure_ascii=False, indent=1)
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


def _wait_for_chain_scene(plan_path: str, seq_id, target_pos: int, timeout: float = 170.0) -> dict:
    """Blocks until the scene at (seq_id, target_pos) in plan.json has a source_url, has
    failed, or the timeout elapses. Necessary because the batch worker dispatches up to
    MAX_CONCURRENT_IMAGE_GENS scenes at once (ThreadPoolExecutor, not the strictly
    sequential loop this feature was originally designed against) — an anchor (seq_pos 0)
    and its first continuation (seq_pos 1) can land in the SAME concurrent batch, so the
    continuation must not read plan.json before the anchor's image actually finished
    uploading. Returns the scene dict (possibly without source_url if it failed or timed
    out) rather than raising — the caller falls back to no chain ref in that case."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            plan = json.load(open(plan_path))
            match = next((s for s in plan["scenes"]
                          if s.get("seq_id") == seq_id and s.get("seq_pos") == target_pos), None)
            if match and (match.get("source_url") or match.get("status") == "fehler"):
                return match
        except Exception:
            pass
        time.sleep(2)
    try:
        plan = json.load(open(plan_path))
        return next((s for s in plan["scenes"]
                     if s.get("seq_id") == seq_id and s.get("seq_pos") == target_pos), {})
    except Exception:
        return {}


def _resolve_chain_refs(plan_path: str, scene: dict) -> tuple:
    """Returns (ref_urls, debug_info) for a scene that's part of a visual sequence
    (seq_id/seq_pos set by segment_by_pacing or _apply_visual_sequences_direct). seq_pos
    0 is the anchor shot — no chain reference needed, just the normal channel character
    reference. seq_pos >= 1 references BOTH the sequence's anchor image AND its immediate
    predecessor (deduplicated when they're the same image, i.e. seq_pos == 1) — a single
    fixed foundation image is what keeps nano-banana-2 visually consistent; chaining only
    off the immediate predecessor would accumulate drift with every new generation."""
    if scene.get("seq_id") is None or scene.get("seq_pos", 0) == 0:
        return [], {}
    seq_id, pos = scene["seq_id"], scene["seq_pos"]
    anchor = _wait_for_chain_scene(plan_path, seq_id, 0)
    prev = anchor if pos - 1 == 0 else _wait_for_chain_scene(plan_path, seq_id, pos - 1)
    refs, debug = [], {}
    if anchor.get("source_url"):
        refs.append(anchor["source_url"]); debug["chain_anchor_file"] = anchor.get("file")
    if prev.get("source_url") and prev.get("file") != anchor.get("file"):
        refs.append(prev["source_url"]); debug["chain_prev_file"] = prev.get("file")
    return refs, debug


def _batch_generate_worker(cid: str, vid: str):
    """Runs 'Alle Bilder generieren' entirely server-side — survives page reloads and
    tab closes. Dispatches up to MAX_CONCURRENT_IMAGE_GENS scenes at once (KIE's real
    limits support 100+ concurrent tasks, see IMAGE_GEN_SEMAPHORE) instead of one at a
    time. Image scenes have no ordering dependency on each other — each is built fresh
    from the channel's fixed master prompt + character reference/description, never from
    another scene's generated output — so dispatch order doesn't matter for correctness.
    (Sequential, order-dependent continuity — e.g. a Veo clip extending the previous
    scene's last frame — is a completely separate, still fully-sequential per-click code
    path around MAX_CHAIN_LENGTH; this function never touches it.)"""
    key = (cid, vid)
    plan_path = v_plan(cid, vid)
    try:
        plan = json.load(open(plan_path))
    except Exception as e:
        with _BATCH_JOBS_LOCK:
            BATCH_JOBS[key] = {"running": False, "stop_requested": False, "done": 0,
                                "total": 0, "current_i": [], "error": f"Plan lesen: {e}", "ts": time.time()}
        return

    scenes = plan["scenes"]
    total = len(scenes)
    todo = [s for s in scenes if not s.get("file")]
    with _BATCH_JOBS_LOCK:
        BATCH_JOBS[key] = {"running": True, "stop_requested": False,
                            "done": total - len(todo), "total": total,
                            "current_i": [], "error": None}
    print(f"  [BatchGen] {cid}/{vid}: {len(todo)} von {total} Szenen offen "
          f"(bis zu {MAX_CONCURRENT_IMAGE_GENS} parallel)", flush=True)

    master = read_master(cid)
    char_refs = load_char_refs(cid)
    image_model = get_video_image_model(cid, vid)
    char_ref_url = get_channel_char_ref(cid)

    def process_scene(scene):
        i = scene["i"]
        with _BATCH_JOBS_LOCK:
            if BATCH_JOBS[key]["stop_requested"]:
                # Stop halts NEW dispatches only — scenes already in flight when Stop
                # was pressed keep running to completion (KIE tasks can't be cancelled
                # mid-flight, and killing the poll loop would just orphan the task).
                return
            BATCH_JOBS[key]["current_i"].append(i)
        try:
            # Re-read the plan fresh in case a manual single-scene click already filled
            # this scene in while other scenes were being worked on.
            try:
                fresh_plan = json.load(open(plan_path))
                fresh_scene = next((s for s in fresh_plan["scenes"] if s["i"] == i), None)
                if fresh_scene and fresh_scene.get("file"):
                    return
            except Exception:
                pass

            scene_key = (cid, vid, i)
            fn = f"{i:03d}.jpg"
            out_path = os.path.join(v_out(cid, vid), fn)

            with _ACTIVE_SCENE_JOBS_LOCK:
                existing_job_id = ACTIVE_SCENE_JOBS.get(scene_key)
                already_running = bool(existing_job_id and JOBS.get(existing_job_id, {}).get("status") == "running")

            if already_running:
                # A manual click is already generating this exact scene — poll for it to
                # finish instead of submitting a second KIE task for the same scene.
                while JOBS.get(existing_job_id, {}).get("status") == "running":
                    time.sleep(2)
            else:
                # Chain-refs resolution can BLOCK (waiting on a sibling sequence scene,
                # see _wait_for_chain_scene) — deliberately done here, outside any lock,
                # so a waiting scene never holds _ACTIVE_SCENE_JOBS_LOCK/_BATCH_JOBS_LOCK
                # and doesn't block unrelated scenes from registering/checking in.
                chain_refs, chain_debug = _resolve_chain_refs(plan_path, scene)
                # Conditional character reference (not blindly attached to every scene):
                # only when this scene's chosen concrete_entity actually IS a character
                # from the analysis — pure landscape/symbol scenes skip it, saving KIE
                # tokens and avoiding mis-conditioning a scene with no character in it.
                entity = str(scene.get("concrete_entity", ""))
                use_char_ref = bool(char_ref_url) and entity.startswith("char_") and \
                    any(c.get("id") == entity for c in plan.get("characters", []))
                refs = chain_refs + ([char_ref_url] if use_char_ref else [])
                full_prompt = _build_image_prompt(scene.get("prompt", ""), master, char_refs)
                if scene.get("seq_id") is not None and scene.get("seq_pos", 0) >= 1:
                    # Positive constraints only — negated instructions ("do NOT redesign")
                    # are weighted weaker by instruction-following image models and can
                    # even be misread as a focus cue ("pink elephant effect").
                    full_prompt += (
                        "\n\nCONTINUITY (STRICT): This is a continuation of the exact same "
                        "shot as the reference image(s). You MUST perfectly match the "
                        "identity, outfit, and background environment shown in the "
                        "references. Change ONLY the camera angle/framing or the specific "
                        "action described above.")
                print(f"  [BatchGen] Szene {i}: char_ref {'angehängt' if use_char_ref else 'NICHT angehängt'} "
                      f"(concrete_entity={entity!r}), Ketten-Refs: {len(chain_refs)}", flush=True)

                # Global cap shared with individual clicks (see IMAGE_GEN_SEMAPHORE) —
                # bounds how many scenes (from here or elsewhere) are ever in flight with
                # KIE at once, regardless of how many scenes this batch tries to dispatch.
                IMAGE_GEN_SEMAPHORE.acquire()
                try:
                    task_id = _kie_submit_image(full_prompt, model=image_model, ref_urls=refs or None)
                except Exception as e:
                    IMAGE_GEN_SEMAPHORE.release()
                    err_text = str(e).lower()
                    retried = False
                    if refs and "credit" not in err_text and "balance" not in err_text and "frequency" not in err_text:
                        # A chain/character reference URL may have expired (KIE's public
                        # temp-hosting isn't permanent) — re-upload the local files fresh
                        # and retry once before giving up the scene over a stale URL.
                        print(f"  [BatchGen] Szene {i}: Submit mit Referenzen fehlgeschlagen ({e}) "
                              f"— lade Referenzen neu hoch und versuche erneut …", flush=True)
                        try:
                            fresh_refs = []
                            for ref_file in (chain_debug.get("chain_anchor_file"), chain_debug.get("chain_prev_file")):
                                if ref_file:
                                    local_path = os.path.join(v_out(cid, vid), ref_file)
                                    if os.path.exists(local_path):
                                        fresh_refs.append(upload_image_public(local_path))
                            if use_char_ref:
                                fresh_refs.append(char_ref_url)
                            IMAGE_GEN_SEMAPHORE.acquire()
                            try:
                                task_id = _kie_submit_image(full_prompt, model=image_model, ref_urls=fresh_refs or None)
                                retried = True
                            except Exception as e2:
                                IMAGE_GEN_SEMAPHORE.release()
                                print(f"  [BatchGen] Szene {i} Submit-Fehler (nach Referenz-Retry): {e2}", flush=True)
                        except Exception as e2:
                            print(f"  [BatchGen] Szene {i}: Referenz-Neu-Upload fehlgeschlagen: {e2}", flush=True)
                    if not retried:
                        print(f"  [BatchGen] Szene {i} Submit-Fehler: {e}", flush=True)
                        _mark_scene_error(plan_path, i)
                        if "credit" in err_text or "balance" in err_text:
                            # Not a per-scene problem — the account is out of KIE credits,
                            # so every remaining scene would fail identically. Stop
                            # dispatching NEW scenes immediately instead of burning
                            # through the rest of the queue with the same fatal error.
                            with _BATCH_JOBS_LOCK:
                                BATCH_JOBS[key]["stop_requested"] = True
                                BATCH_JOBS[key]["error"] = str(e)
                        return
                job_id = f"{cid}_{vid}_{i}_{int(time.time())}"
                JOBS[job_id] = {"status": "running", "progress": 0, "file": None,
                                "source_url": None, "ts": None, "error": None}
                with _ACTIVE_SCENE_JOBS_LOCK:
                    ACTIVE_SCENE_JOBS[scene_key] = job_id
                # Mark "läuft" in plan.json so the individual scene tiles show "Wird
                # generiert …" while the batch is running, not just for scenes started
                # via a manual single-scene click (that already did this on its own).
                # Also persist the chain/char-ref debug fields (Review-Auflage: sichtbar
                # nachvollziehbar, welche Szenen ohne Charakter-Referenz liefen).
                with _PLAN_WRITE_LOCK:
                    try:
                        p2 = json.load(open(plan_path))
                        for s in p2["scenes"]:
                            if s["i"] == i:
                                s["status"] = "läuft"
                                s["char_ref_applied"] = use_char_ref
                                if chain_debug.get("chain_anchor_file"):
                                    s["chain_anchor_file"] = chain_debug["chain_anchor_file"]
                                if chain_debug.get("chain_prev_file"):
                                    s["chain_prev_file"] = chain_debug["chain_prev_file"]
                        json.dump(p2, open(plan_path, "w"), ensure_ascii=False, indent=1)
                    except: pass
                try:
                    _image_job_worker_inner(job_id, task_id, out_path, plan_path, i)
                finally:
                    IMAGE_GEN_SEMAPHORE.release()
                    with _ACTIVE_SCENE_JOBS_LOCK:
                        if ACTIVE_SCENE_JOBS.get(scene_key) == job_id:
                            del ACTIVE_SCENE_JOBS[scene_key]
        finally:
            with _BATCH_JOBS_LOCK:
                BATCH_JOBS[key]["done"] += 1
                if i in BATCH_JOBS[key]["current_i"]:
                    BATCH_JOBS[key]["current_i"].remove(i)

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_IMAGE_GENS) as pool:
        futures = []
        for scene in todo:
            with _BATCH_JOBS_LOCK:
                if BATCH_JOBS[key]["stop_requested"]:
                    break
            futures.append(pool.submit(process_scene, scene))
        for f in futures:
            f.result()

    with _BATCH_JOBS_LOCK:
        stopped = BATCH_JOBS[key]["stop_requested"]
        BATCH_JOBS[key]["running"] = False
        BATCH_JOBS[key]["current_i"] = []
        BATCH_JOBS[key]["ts"] = time.time()
    print(f"  [BatchGen] {cid}/{vid}: {'gestoppt' if stopped else 'fertig'}", flush=True)


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
            json.dump(plan, open(plan_path, "w"), ensure_ascii=False, indent=1)
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

RENDER_FPS = 30
RENDER_WIDTH = 1920
RENDER_HEIGHT = 1080
RENDER_SUPERSAMPLE_WIDTH = 3840  # scale-up before zoompan — without this, zoompan's
# per-frame rounding to whole pixels is visible as jitter on a slow zoom. 4K is enough
# to make that invisible at the zoom intensities used here (capped well under 1.2x)
# while costing only ~1/4 the memory/CPU of 8K supersampling.

_VIDEO_ENCODER = None
def _probe_video_encoder() -> tuple:
    """Returns (encoder_name, extra_ffmpeg_args). Checked once, cached — h264_videotoolbox
    (Apple Silicon hardware encoder) is roughly 4x faster than libx264 and only lightly
    loads the CPU, important since the Python server runs alongside during a render. It
    needs an explicit quality flag or the default output is visibly soft. Falls back to
    libx264 if videotoolbox isn't available on this machine."""
    global _VIDEO_ENCODER
    if _VIDEO_ENCODER is not None:
        return _VIDEO_ENCODER
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                              capture_output=True, text=True, timeout=10)
        if "h264_videotoolbox" in out.stdout:
            _VIDEO_ENCODER = ("h264_videotoolbox", ["-q:v", "65"])
        else:
            _VIDEO_ENCODER = ("libx264", ["-preset", "medium", "-crf", "20"])
    except Exception:
        _VIDEO_ENCODER = ("libx264", ["-preset", "medium", "-crf", "20"])
    print(f"  [Render] Video-Encoder: {_VIDEO_ENCODER[0]}", flush=True)
    return _VIDEO_ENCODER


def _apply_sync_invariant(scenes: list, audio_duration: float, fps: int) -> list:
    """Two sequential steps — the second does NOT replace the first, both always run:

    1) Linear normalization (seconds, float): scale every scene's `dur` so their sum
       matches the real audio_duration exactly. Spreads the usually-small WPM-estimate
       error invisibly across all scenes.
    2) Integer-frame rounding (frames, int): converts the now-normalized durations to
       exact frame counts, with the LAST scene absorbing the rounding remainder, so
       sum(frames) == round(audio_duration * fps) EXACTLY. This is what actually
       prevents the tail-clipping/drift bug class (MoneyPrinterTurbo issue #985: a bare
       `if video_duration >= audio_duration` float comparison breaks on minor FFmpeg
       rounding and cuts off early) — no code downstream ever compares durations as
       floats again, only these integer frame counts are treated as ground truth.

    Returns a list of per-scene frame counts, same order/length as `scenes`.

    Prefers `start_aligned`/`end_aligned` (Phase 3, Whisper word-timestamps) over the
    estimated `dur` when present on a scene — same two-step math either way, just fed
    with a more accurate per-scene duration where available."""
    def scene_dur(s):
        sa, ea = s.get("start_aligned"), s.get("end_aligned")
        if sa is not None and ea is not None and ea > sa:
            return ea - sa
        return s.get("dur", 0)

    total_dur = sum(scene_dur(s) for s in scenes) or 1.0
    factor = audio_duration / total_dur
    normalized = [max(0.1, scene_dur(s) * factor) for s in scenes]

    audio_frames = round(audio_duration * fps)
    frames = [round(d * fps) for d in normalized]
    if frames:
        frames[-1] += audio_frames - sum(frames)
        frames[-1] = max(1, frames[-1])
    return frames


# ---------- Motion-Vokabular (erweitert von reinem Ken-Burns-Zoom) ----------
# Jeder Eintrag ist EIN generalisierter Zoom+Fokuspunkt-Verlauf: z0->z1 (Skalierung) und
# focus0->focus1 (welcher Bildpunkt zentriert wird), beide über dieselbe Smoothstep-Kurve
# interpoliert wie das bisherige Ken-Burns-Easing. Ein reiner Pan/Tilt ist einfach z0==z1
# (keine Skalierung) mit einem wandernden Fokuspunkt -- kein neuer ffmpeg-Filter, nur eine
# Verallgemeinerung des bereits vorhandenen zoompan-Ausdrucks. Pan/Tilt/Dolly/Diagonal
# brauchen alle einen LEICHTEN Zoom-Puffer (>1.0) über den ganzen Verlauf, sonst würde der
# Crop-Ausschnitt beim Wandern des Fokuspunkts über den Bildrand hinauslaufen.
MOTION_LIBRARY = {
    "zoom_in":        {"z0": 1.0,  "z1": 1.12, "focus0": (0.5, 0.45),  "focus1": (0.5, 0.45)},
    "zoom_out":       {"z0": 1.12, "z1": 1.0,  "focus0": (0.5, 0.45),  "focus1": (0.5, 0.45)},
    "pan_left":       {"z0": 1.08, "z1": 1.08, "focus0": (0.64, 0.48), "focus1": (0.36, 0.48)},
    "pan_right":      {"z0": 1.08, "z1": 1.08, "focus0": (0.36, 0.48), "focus1": (0.64, 0.48)},
    "tilt_up":        {"z0": 1.08, "z1": 1.08, "focus0": (0.5, 0.64),  "focus1": (0.5, 0.36)},
    "tilt_down":      {"z0": 1.08, "z1": 1.08, "focus0": (0.5, 0.36),  "focus1": (0.5, 0.64)},
    "dolly_in":       {"z0": 1.0,  "z1": 1.08, "focus0": (0.42, 0.46), "focus1": (0.58, 0.46)},
    "dolly_out":      {"z0": 1.08, "z1": 1.0,  "focus0": (0.58, 0.46), "focus1": (0.42, 0.46)},
    "diagonal_glide": {"z0": 1.04, "z1": 1.1,  "focus0": (0.4, 0.4),   "focus1": (0.6, 0.55)},
    "snap_zoom_in":   {"z0": 1.0,  "z1": 1.25, "focus0": (0.5, 0.45),  "focus1": (0.5, 0.45)},
    "static":         {"z0": 1.02, "z1": 1.02, "focus0": (0.5, 0.5),   "focus1": (0.5, 0.5)},
}

# Auswahl-Kandidaten nach `pacing` (heute verfügbar) -- vorbereitet für `phase` (Story-
# Phase-Engine, noch nicht gebaut): wenn scene.get("phase") künftig gesetzt ist, wird das
# bevorzugt, sonst fällt die Auswahl auf pacing zurück. Kein Zufall (Resume-Determinismus,
# siehe ARCHITECTURE §13/§15.1) -- Auswahl über scene["i"] % len(candidates).
_PACING_MOTION_CANDIDATES = {
    "calm":   ["pan_left", "pan_right", "tilt_up", "tilt_down", "dolly_out"],
    "normal": ["zoom_in", "zoom_out", "dolly_in", "pan_left", "pan_right"],
    "punchy": ["snap_zoom_in", "diagonal_glide", "static"],
}
_PHASE_MOTION_CANDIDATES = {
    "OPENING":       ["pan_right", "pan_left", "tilt_down"],
    "RISING_ACTION": ["dolly_in", "zoom_in"],
    "CLIMAX":        ["snap_zoom_in", "diagonal_glide", "static"],
    "RESOLUTION":    ["dolly_out", "tilt_up", "pan_left"],
}


def _build_motion(name: str, intensity_scale: float = 1.0) -> dict:
    """Scales a MOTION_LIBRARY recipe's movement AROUND ITS OWN MIDPOINT — intensity_scale
    == 1.0 reproduces the base recipe exactly, <1 dampens (short scene, subtle), >1
    amplifies (long scene, fuller movement) — without changing which direction it moves in."""
    base = MOTION_LIBRARY[name]
    z_mid = (base["z0"] + base["z1"]) / 2
    fx_mid = (base["focus0"][0] + base["focus1"][0]) / 2
    fy_mid = (base["focus0"][1] + base["focus1"][1]) / 2
    z0 = z_mid + (base["z0"] - z_mid) * intensity_scale
    z1 = z_mid + (base["z1"] - z_mid) * intensity_scale
    fx0 = fx_mid + (base["focus0"][0] - fx_mid) * intensity_scale
    fy0 = fy_mid + (base["focus0"][1] - fy_mid) * intensity_scale
    fx1 = fx_mid + (base["focus1"][0] - fx_mid) * intensity_scale
    fy1 = fy_mid + (base["focus1"][1] - fy_mid) * intensity_scale
    return {"name": name, "z0": round(z0, 4), "z1": round(z1, 4),
            "focus0": [round(fx0, 4), round(fy0, 4)], "focus1": [round(fx1, 4), round(fy1, 4)]}


def _normalize_motion(motion: dict) -> dict:
    """Accepts either the pre-existing {"type","z_end","focus"} shape (zoom_in/zoom_out/
    static only, from before the motion vocabulary existed) or the current {"name","z0",
    "z1","focus0","focus1"} shape, and always returns the current shape. Old plans with
    already-rendered `scene["motion"]` keep working without a migration step (ARCHITECTURE
    §11 rule: additive, no schema-version bump)."""
    if "z0" in motion:
        return motion
    z_end = motion.get("z_end", 1.02)
    mtype = motion.get("type", "static")
    focus = motion.get("focus", [0.5, 0.5])
    z0, z1 = (z_end, 1.0) if mtype == "zoom_out" else (1.0, z_end) if mtype == "zoom_in" else (z_end, z_end)
    return {"name": mtype, "z0": z0, "z1": z1, "focus0": focus, "focus1": focus}


def _motion_for_scene(scene: dict, prev_scene: dict) -> dict:
    """Rule-based motion recipe — no LLM call, no external easing/animation library. Very
    short scenes stay nearly static, since a moving camera on a sub-1.2s cut reads as
    jitter, not cinematic movement. Sequence continuations (seq_pos >= 1) keep the SAME
    motion as their sequence's previous scene, so several images belonging to one visual
    sequence read as one continuous camera move instead of independent shots each
    'breathing' on their own. Intensity scales with duration — a long scene gets a fuller
    movement, a short one stays subtle."""
    dur = scene.get("dur", 3.0)
    if dur < 1.2:
        return _build_motion("static", 1.0)

    if (scene.get("seq_id") is not None and scene.get("seq_pos", 0) >= 1
            and prev_scene and prev_scene.get("motion")):
        name = _normalize_motion(prev_scene["motion"]).get("name", "zoom_in")
    else:
        phase = scene.get("phase")
        pacing = scene.get("pacing") if scene.get("pacing") in _PACING_MOTION_CANDIDATES else "normal"
        candidates = _PHASE_MOTION_CANDIDATES.get(phase) or _PACING_MOTION_CANDIDATES[pacing]
        name = candidates[scene.get("i", 0) % len(candidates)]

    intensity_scale = min(0.5 + dur * 0.12, 1.4)
    return _build_motion(name, intensity_scale)


def _overlay_specs_for_scene(scene: dict, clip_dur: float, overlay_opts: dict) -> list:
    """Decides which text overlays (if any) apply to this scene and their on-screen
    window. Returns a list of (style, text, t0, t1) tuples, evaluated in the order they
    should be layered (chapter title first/bottom-most, caption last/top-most is NOT
    required here since they occupy different screen regions and never overlap)."""
    if not overlay_opts:
        return []
    specs = []
    if overlay_opts.get("chapters") and scene.get("seq_pos") == 0 and scene.get("seq_reason"):
        specs.append(("chapter", scene["seq_reason"], 0.0, min(2.0, clip_dur)))
    if overlay_opts.get("callouts") and scene.get("callout"):
        t0 = min(0.2, clip_dur * 0.1)
        t1 = min(1.6, clip_dur - 0.05) if clip_dur > 0.3 else clip_dur
        if t1 > t0:
            specs.append(("callout", scene["callout"], t0, t1))
    if overlay_opts.get("captions") and scene.get("text"):
        specs.append(("caption", scene["text"], 0.0, clip_dur))
    return specs


def _render_clip(img_path: str, scene: dict, out_path: str, fps: int = RENDER_FPS,
                  overlay_opts: dict = None) -> None:
    """Renders one scene's still image into a short Ken-Burns clip, optionally with text
    overlays (captions/callouts/chapter titles, Phase 4.4) composited on top. Resume-safe:
    skips entirely if out_path already exists and is non-empty — same pattern as
    _batch_generate_worker's `todo = [s for s in scenes if not s.get("file")]`, so a
    crashed/restarted render only redoes the clips actually missing, not everything."""
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return
    motion = _normalize_motion(scene.get("motion") or {"type": "static", "z_end": 1.02, "focus": [0.5, 0.5]})
    frames = max(1, scene.get("_frames") or round(scene.get("dur", 3.0) * fps))
    z0, z1 = motion["z0"], motion["z1"]
    fx0, fy0 = motion["focus0"]
    fx1, fy1 = motion["focus1"]
    clip_dur = frames / fps

    # Smoothstep easing (3t²-2t³), built purely from the frame index `on`/`frames` — NOT
    # from zoompan's internal `zoom` variable (which holds the clamped previous-frame
    # value and would accumulate rounding drift). This alone fixes the mechanical
    # "robot camera" look of a plain linear zoom expression, at zero extra runtime cost.
    # The SAME easing curve now also interpolates the focus point (fx0,fy0)->(fx1,fy1) --
    # a pure pan/tilt is simply z0==z1 with a moving focus point, so this one expression
    # family covers the whole motion vocabulary (Ken Burns zoom, pan, tilt, dolly,
    # diagonal glide), not a separate code path per motion type.
    smoothstep = f"(3*pow(on/{frames},2)-2*pow(on/{frames},3))"
    z_expr = f"{z0}+({z1}-{z0})*{smoothstep}"
    fx_expr = f"({fx0}+({fx1}-{fx0})*{smoothstep})"
    fy_expr = f"({fy0}+({fy1}-{fy0})*{smoothstep})"
    x_expr = f"(iw*{fx_expr})-(iw/zoom/2)"
    y_expr = f"(ih*{fy_expr})-(ih/zoom/2)"

    overlay_specs = _overlay_specs_for_scene(scene, clip_dur, overlay_opts)
    encoder, encoder_args = _probe_video_encoder()
    inputs = ["-loop", "1", "-i", img_path]
    filter_parts = [
        f"[0:v]scale={RENDER_SUPERSAMPLE_WIDTH}:-2,"
        f"zoompan=z='{z_expr}':d={frames}:x='{x_expr}':y='{y_expr}':"
        f"s={RENDER_WIDTH}x{RENDER_HEIGHT}:fps={fps},setsar=1[base]"
    ]
    overlay_pngs = []
    last_label = "base"
    try:
        for idx, (style, text, t0, t1) in enumerate(overlay_specs):
            png_path = f"{out_path}.ov{idx}.png"
            render_text_overlay_png(png_path, RENDER_WIDTH, RENDER_HEIGHT, style, text)
            overlay_pngs.append(png_path)
            inputs += ["-loop", "1", "-i", png_path]
            in_idx = idx + 1
            fade_dur = min(0.3, max(0.05, (t1 - t0) / 4))
            fade_out_st = max(t0, t1 - fade_dur)
            faded_label = f"ov{idx}f"
            filter_parts.append(
                f"[{in_idx}:v]format=rgba,"
                f"fade=t=in:st={t0}:d={fade_dur}:alpha=1,"
                f"fade=t=out:st={fade_out_st}:d={fade_dur}:alpha=1[{faded_label}]"
            )
            is_last = idx == len(overlay_specs) - 1
            next_label = "outv" if is_last else f"comp{idx}"
            filter_parts.append(
                f"[{last_label}][{faded_label}]overlay=enable='between(t,{t0},{t1})'[{next_label}]"
            )
            last_label = next_label
        map_label = last_label if overlay_specs else "base"

        cmd = [
            "ffmpeg", "-y", *inputs,
            "-t", str(clip_dur), "-r", str(fps),
            "-filter_complex", ";".join(filter_parts),
            "-map", f"[{map_label}]",
            "-c:v", encoder, *encoder_args,
            "-pix_fmt", "yuv420p", "-video_track_timescale", "90000",
            out_path,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg Clip-Render fehlgeschlagen ({os.path.basename(img_path)}): "
                                f"{result.stderr.decode(errors='replace')[-300:]}")
    finally:
        for p in overlay_pngs:
            try: os.remove(p)
            except: pass


def _assemble_clips(clip_paths: list, out_path: str) -> None:
    """concat-demuxer, hard cuts only (V1 decision — crossfades are later polish, need
    a full filter_complex re-encode instead of a lossless -c copy). Requires all clips
    to share codec/resolution/fps/timescale, which _render_clip's fixed recipe guarantees."""
    list_path = out_path + ".txt"
    with open(list_path, "w") as f:
        for p in clip_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")
    try:
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", out_path]
        result = subprocess.run(cmd, capture_output=True, timeout=180)
    finally:
        os.remove(list_path)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg Zusammenschnitt fehlgeschlagen: {result.stderr.decode(errors='replace')[-300:]}")


def _mux_audio(silent_path: str, audio_path: str, out_path: str) -> None:
    """Final mux: one continuous voiceover track over the assembled silent video.
    -af apad pads the audio with a small buffer if it's a touch shorter than the video —
    a safety net ON TOP of the integer-frame sync invariant (_apply_sync_invariant), not
    a replacement for it; if there's still a residual mismatch, the audio gets padded
    rather than the video getting truncated. -movflags +faststart moves the MP4 metadata
    to the front so the <video> preview can start playing before the whole file has
    downloaded — without it the browser waits for the complete file first."""
    cmd = ["ffmpeg", "-y", "-i", silent_path, "-i", audio_path,
           "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
           "-af", "apad=pad_dur=0.3", "-shortest", "-movflags", "+faststart", out_path]
    result = subprocess.run(cmd, capture_output=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg Audio-Mux fehlgeschlagen: {result.stderr.decode(errors='replace')[-300:]}")


def _render_selfcheck(final_path: str, expected_audio_duration: float) -> dict:
    """Post-render ffprobe checks, same philosophy as _validate_image_prompt_entry: a
    silent success that's actually broken (truncated video, no audio track) must not be
    reported as done. One level higher up — on the finished video instead of a prompt."""
    checks = {"duration_ok": False, "audio_ok": False, "frames_ok": False}
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                               "-of", "csv=p=0", final_path], capture_output=True, text=True, timeout=15)
        video_dur = float(out.stdout.strip())
        checks["duration_ok"] = abs(video_dur - expected_audio_duration) < 0.5
    except Exception:
        pass
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                               "-show_entries", "stream=codec_type", "-of", "csv=p=0", final_path],
                              capture_output=True, text=True, timeout=15)
        checks["audio_ok"] = "audio" in out.stdout
    except Exception:
        pass
    checks["frames_ok"] = os.path.exists(final_path) and os.path.getsize(final_path) > 0
    return checks


# ---------- Übergänge (Phase 4) — Crossfade NUR an echten Sequenz-/Szenenwechseln, ----
# alles andere bleibt harter Schnitt über den verlustfreien concat-Demuxer.
TRANSITION_DURATION_SEC = 0.5  # Fallback/Default, siehe TRANSITION_LIBRARY für die pro-Familie-Dauer

# Kuratierte Übergangs-Bibliothek: ffmpegs xfade-Filter bringt bereits 58 fertige
# Übergangstypen mit (kein neues Paket, keine eigene Easing-Formel nötig für diesen
# Teil) — "die Bibliothek" ist also bereits da, es fehlte nur eine Auswahlregel statt
# immer denselben Typ ("fade") zu nehmen. Pro Familie zwei Richtungsvarianten, damit
# ein längeres Video nicht monoton wirkt; welche Familie greift, ist regelbasiert aus
# bereits vorhandenen Szenendaten (pacing) abgeleitet, kein LLM-Call, kein Zufall
# (Zufall würde Resume-Läufe nach einem Reload inkonsistent machen).
#   - "calm"   -> sanftes Dissolve/Fade, kein SFX, LANGSAMER (0.8s "linger" statt der
#                 alten festen 0.5s) — eine ruhige Szene darf sich Zeit lassen
#   - "punchy" -> energischer Wipe, mit Whoosh, SCHNELLER (0.3s "snappy") — ein Wipe,
#                 der 0.8s dauert, wirkt behäbig statt hart
#   - sonst    -> neutraler Smooth-Übergang, unveränderte 0.5s — der Standardfall
# Vorbereitet für die künftige Story-Phase-Engine: sobald scene["phase"] existiert, kann
# die Dauer stattdessen pro Phase variieren (OPENING kurz, RESOLUTION lang) — bis dahin
# ist `pacing` der verfügbare, sinnvolle Proxy dafür.
TRANSITION_LIBRARY = {
    "fade":   {"types": ["fade", "dissolve"],           "sfx": None,      "duration": 0.8},
    "wipe":   {"types": ["wipeleft", "wiperight"],      "sfx": "whoosh",  "duration": 0.3},
    "smooth": {"types": ["smoothleft", "smoothright"],  "sfx": "whoosh",  "duration": 0.5},
}


def _transition_for_scene(scene: dict, idx: int) -> tuple:
    """Wählt Übergangstyp + passendes SFX (oder None) + Übergangsdauer für den Schnitt
    VOR `scene`. Richtung (links/rechts) alterniert über den Szenenindex — dasselbe
    deterministische Muster wie die Zoom-Richtung in _motion_for_scene, damit ein
    erneuter Render (Resume nach Reload) exakt denselben Übergang produziert, kein
    Zufalls-Rauschen. Gibt (transition_type, sfx_or_None, duration_sec) zurück."""
    family = "fade" if scene.get("pacing") == "calm" else \
             "wipe" if scene.get("pacing") == "punchy" else "smooth"
    lib = TRANSITION_LIBRARY[family]
    transition_type = lib["types"][scene.get("i", idx) % len(lib["types"])]
    return transition_type, lib["sfx"], lib["duration"]


def _has_transition_before(scenes: list, idx: int) -> bool:
    """Identische Regel wie das Whoosh-SFX-Ereignis (_build_sfx_events) — bewusst so:
    Bild-Übergang und Whoosh-Sound müssen auf demselben Schnitt sitzen. True, wenn diese
    Szene der Anker (seq_pos==0) einer Sequenz ist UND die unmittelbar vorherige Szene
    einer anderen (oder gar keiner) Sequenz angehört — ein echter visueller Wechsel,
    nicht nur die erste Szene im Video."""
    if idx == 0:
        return False
    s, prev = scenes[idx], scenes[idx - 1]
    return s.get("seq_id") is not None and s.get("seq_pos", 0) == 0 and prev.get("seq_id") != s.get("seq_id")


def _clip_duration_sec(path: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                           "-of", "csv=p=0", path], capture_output=True, text=True, timeout=15)
    return float(out.stdout.strip())


def _crossfade_clips(clip_a: str, clip_b: str, out_path: str, duration: float, transition_type: str = "fade") -> None:
    """Merges two already-rendered clips into one with a crossfade at the boundary.
    `clip_a` MUST have been rendered with `duration` extra seconds of Ken-Burns motion
    tacked onto its planned length beforehand (see _render_worker's transition_frames
    compensation) — the crossfade consumes exactly that overlap, so the merged clip's
    total duration still equals the ORIGINAL uncompensated sum of both scenes' planned
    durations. That's what keeps the frame-exact audio sync invariant (_apply_sync_
    invariant) intact even though a crossfade inherently overlaps two clips in time.
    `transition_type` is any of ffmpeg xfade's ~58 built-in names (see TRANSITION_LIBRARY
    for the curated subset actually selected from) — picking a different name costs
    nothing extra, it's the same filter."""
    dur_a = _clip_duration_sec(clip_a)
    offset = max(0.0, dur_a - duration)
    encoder, encoder_args = _probe_video_encoder()
    cmd = ["ffmpeg", "-y", "-i", clip_a, "-i", clip_b,
           "-filter_complex", f"[0:v][1:v]xfade=transition={transition_type}:duration={duration}:offset={offset}[v]",
           "-map", "[v]", "-c:v", encoder, *encoder_args,
           "-pix_fmt", "yuv420p", "-video_track_timescale", "90000", out_path]
    result = subprocess.run(cmd, capture_output=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg Übergang fehlgeschlagen: {result.stderr.decode(errors='replace')[-300:]}")


# ---------- Sound-Design (Phase 2.5) — Musikbett + SFX, alles reines FFmpeg ----------
# Kein Live-API-Abruf, kein neues Paket: ein einmal kuratierter, lokaler Asset-Pool unter
# assets/ (projektweit, nicht pro Kanal — Sounds sind stiluniversell). Fehlen die Dateien
# (z.B. frischer Checkout ohne echte Assets), fällt der Renderer sauber auf reines
# Voiceover zurück (Phase-2-Verhalten) statt zu crashen — siehe _build_final_audio.
SOUND_ASSETS_DIR = os.path.join(HERE, "assets")
MUSIC_BED_FILE = os.path.join(SOUND_ASSETS_DIR, "music", "neutral_bed.mp3")
SFX_FILES = {
    "whoosh": os.path.join(SOUND_ASSETS_DIR, "sfx", "whoosh_01.wav"),
    "impact": os.path.join(SOUND_ASSETS_DIR, "sfx", "impact_01.wav"),
    "riser":  os.path.join(SOUND_ASSETS_DIR, "sfx", "riser_01.wav"),
}


def _build_sfx_events(scenes: list) -> list:
    """Rule-based SFX timing — no LLM call.
    - At a real sequence/scene change (this scene is the anchor, seq_pos==0, of a
      sequence whose immediately preceding scene belongs to a DIFFERENT sequence or
      none): the SFX is whatever _transition_for_scene picked for the matching visual
      crossfade (same selection call as _render_worker uses for the video side) — a
      "fade"-family transition gets no SFX (a whoosh would fight a slow, calm dissolve),
      "wipe"/"smooth" get "whoosh". Visual and audio transition are therefore always in
      sync by construction, never two independently-computed choices that could drift.
    - 'riser' fires at every scene pacing labeled 'punchy' — the closest available proxy
      for "strongest emotional_arc transitions" from the plan, since this codebase's
      scene model has no explicit chapter markers to key off of.
    - 'impact' (Phase 4.2) additionally fires at a 'punchy' scene that is NOT already a
      crossfade transition point — a genuine hard cut on a dramatic beat gets a sharp,
      percussive accent landing exactly on the cut, which only became possible frame-
      accurately once Phase 3 (Whisper word-timestamps) existed. A transition point
      already has a soft video crossfade + whoosh; stacking an impact hit on top of that
      would clash, hence the exclusion.
    Timing prefers `start_aligned` (Phase 3, Whisper word-timestamps) over the estimated
    `start` when present — same placement rule, just landing on the real word boundary
    instead of an approximate one."""
    def scene_start(s):
        return s["start_aligned"] if s.get("start_aligned") is not None else s.get("start", 0)

    events = []
    for idx, s in enumerate(scenes):
        if idx == 0:
            continue
        prev = scenes[idx - 1]
        is_transition = s.get("seq_id") is not None and s.get("seq_pos", 0) == 0 and prev.get("seq_id") != s.get("seq_id")
        if is_transition:
            _transition_type, sfx, _duration = _transition_for_scene(s, idx)
            if sfx:
                events.append({"start": scene_start(s), "sfx": sfx})
        if s.get("pacing") == "punchy":
            events.append({"start": scene_start(s), "sfx": "riser"})
            if not is_transition:
                events.append({"start": scene_start(s), "sfx": "impact"})
    return events


def _duck_music_under_voice(voice_path: str, music_path: str, out_path: str) -> None:
    """Music bed ducked under the voiceover via sidechaincompress — volume drops
    automatically whenever the voice is present, rises back up in gaps. `-stream_loop -1`
    on the music input loops the (typically much shorter) bed file for the whole video;
    `amix=duration=first` then trims the result to the voice track's exact length."""
    cmd = ["ffmpeg", "-y", "-i", voice_path, "-stream_loop", "-1", "-i", music_path,
           "-filter_complex",
           "[0:a]asplit=2[voice][sc];"
           "[1:a][sc]sidechaincompress=threshold=0.02:ratio=10:attack=50:release=500[ducked];"
           "[voice][ducked]amix=inputs=2:duration=first[a]",
           "-map", "[a]", out_path]
    result = subprocess.run(cmd, capture_output=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg Ducking fehlgeschlagen: {result.stderr.decode(errors='replace')[-300:]}")


def _place_sfx(narration_path: str, sfx_events: list, out_path: str) -> None:
    """Layers SFX files onto the (already ducked) narration track at specific
    timestamps via adelay, then amix + loudnorm for one consistent final loudness.
    Silently skips any event whose asset file doesn't exist — a missing single SFX
    file must not fail the whole render."""
    inputs = ["-i", narration_path]
    filter_parts, labels = [], []
    for ev in sfx_events:
        sfx_path = SFX_FILES.get(ev["sfx"])
        if not sfx_path or not os.path.exists(sfx_path):
            continue
        inputs += ["-i", sfx_path]
        delay_ms = max(0, round(ev["start"] * 1000))
        label = f"s{len(labels)}"
        filter_parts.append(f"[{len(labels)+1}:a]adelay={delay_ms}|{delay_ms},volume=0.7[{label}]")
        labels.append(label)

    if not labels:
        # Nothing to place — just loudnorm-normalize the narration/music mix alone.
        cmd = ["ffmpeg", "-y", "-i", narration_path, "-af", "loudnorm", out_path]
    else:
        mix_inputs = "[0:a]" + "".join(f"[{l}]" for l in labels)
        filter_complex = ";".join(filter_parts) + f";{mix_inputs}amix=inputs={len(labels)+1}:duration=first,loudnorm[a]"
        cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", filter_complex, "-map", "[a]", out_path]
    result = subprocess.run(cmd, capture_output=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg SFX-Platzierung fehlgeschlagen: {result.stderr.decode(errors='replace')[-300:]}")


def _build_final_audio(voice_path: str, scenes: list, render_dir: str) -> str:
    """Builds the final audio track for muxing: voiceover + ducked music bed + rule-
    based SFX, loudnorm-normalized. Falls back to the raw voiceover unchanged if the
    music bed asset is missing (e.g. a fresh checkout before real assets are added) —
    same resilience pattern as everywhere else in this codebase: missing optional data
    degrades gracefully instead of failing the whole render."""
    if not os.path.exists(MUSIC_BED_FILE):
        print("  [Render] Kein Musikbett gefunden (assets/music/neutral_bed.mp3) — "
              "rendere ohne Sound-Design, nur Voiceover.", flush=True)
        return voice_path
    try:
        ducked_path = os.path.join(render_dir, "_ducked.mp3")
        _duck_music_under_voice(voice_path, MUSIC_BED_FILE, ducked_path)
        sfx_events = _build_sfx_events(scenes)
        final_audio_path = os.path.join(render_dir, "_final_audio.mp3")
        _place_sfx(ducked_path, sfx_events, final_audio_path)
        print(f"  [Render] Sound-Design: Musikbett gedückt + {len(sfx_events)} SFX-Ereignisse platziert", flush=True)
        return final_audio_path
    except Exception as e:
        print(f"  [Render] Sound-Design fehlgeschlagen ({e}) — falle zurück auf reines Voiceover.", flush=True)
        return voice_path


def _render_worker(cid: str, vid: str):
    """Orchestrates: prepare (sync invariant) -> motion -> clips (one Ken Burns clip per
    scene, resume-safe) -> assemble (concat) -> audio (mux) -> review (ffprobe checks).
    Sequential — one ffmpeg process at a time, no thread pool over rendering (that would
    oversubscribe the CPU well beyond what the images' 8-way concurrency already does)."""
    key = (cid, vid)
    plan_path = v_plan(cid, vid)
    render_dir = v_render_tmp(cid, vid)
    os.makedirs(render_dir, exist_ok=True)

    def stage(name, done=0, total=0):
        with _RENDER_JOBS_LOCK:
            if key in RENDER_JOBS:
                RENDER_JOBS[key].update(stage=name, done=done, total=total)

    def stop_requested():
        with _RENDER_JOBS_LOCK:
            return RENDER_JOBS.get(key, {}).get("stop_requested", False)

    try:
        try:
            plan = json.load(open(plan_path))
        except Exception as e:
            raise RuntimeError(f"Plan lesen: {e}")

        scenes = [s for s in plan["scenes"] if s.get("file")]
        if not scenes:
            raise RuntimeError("Keine generierten Bilder vorhanden — erst Bilder generieren.")

        stage("prepare")
        try:
            audio_meta = json.load(open(v_audio(cid, vid)))
            audio_path = audio_meta.get("path", "")
        except Exception:
            audio_path = ""
        if not audio_path or not os.path.exists(audio_path):
            raise RuntimeError("Kein hochgeladenes Voice-over gefunden — Rendern braucht eine durchgehende Audiospur.")

        # Whisper-Alignment + Pausen-Kürzung laufen HIER, nicht im /api/transcribe-
        # Handler -- an dieser Stelle liegt IMMER ein hochgeladenes Voice-over vor,
        # unabhängig davon, ob die Szenen-Texte ursprünglich aus der Audio-
        # Transkription (Option A) oder dem manuellen Skript-Pfad (Option B, geschätzte
        # WPM-Timeline) stammen. Ein einziger Punkt statt zwei getrennter, damit BEIDE
        # Pfade dieselbe frame-genaue Timing-Qualität bekommen (ARCHITECTURE.md 16.5).
        # Die gekürzte Audiospur wird NEBEN dem Original abgelegt (v_uploads, nicht
        # render_tmp/, das nach jedem Render gelöscht wird) -- ihre Existenz ist damit
        # selbst der Resume-Marker: schon getrimmt + Szenen schon ausgerichtet heißt
        # kein erneuter Whisper-Lauf bei einem Wiederholungs-Render.
        trimmed_audio_path = os.path.join(v_uploads(cid, vid), "voiceover_trimmed.wav")
        needs_alignment = any(s.get("start_aligned") is None for s in scenes)
        # ElevenLabs fast-path: when audio_meta.json carries word timestamps (Phase 1),
        # we still need pause-trim, but the SLOW step (Whisper transcription) is replaced
        # by the provider-side timestamps we already captured at generate-time. The same
        # _compute_pause_trims / _adjust_words_for_trims / align_scenes_to_whisper chain
        # then runs unchanged — both code paths produce identical scene timing.
        elevenlabs_words = audio_meta.get("voiceover_word_timestamps") if audio_meta else None
        if needs_alignment or not os.path.exists(trimmed_audio_path):
            stage("timing")
            try:
                if isinstance(elevenlabs_words, list) and elevenlabs_words:
                    whisper_words = [{"word": w["word"], "start": w["start"], "end": w["end"]}
                                     for w in elevenlabs_words]
                    whisper_lang = "elevenlabs"
                    whisper_prob = 1.0
                else:
                    whisper_result = transcribe_words_whisper(audio_path)
                    whisper_words = whisper_result["words"]
                    whisper_lang  = whisper_result.get("language", "unknown")
                    whisper_prob  = whisper_result.get("language_probability", 0.0)
                trims = _compute_pause_trims(whisper_words)
                _trim_audio_pauses(audio_path, trims, trimmed_audio_path)
                adjusted_words = _adjust_words_for_trims(whisper_words, trims)
                align_scenes_to_whisper(scenes, adjusted_words)
                trimmed_total = sum(b - a for a, b in trims)
                source_tag = "ElevenLabs" if isinstance(elevenlabs_words, list) and elevenlabs_words else "Whisper"
                print(f"  [{source_tag}] {len(whisper_words)} Wörter ausgerichtet, "
                      f"{len(trims)} Pause(n) auf {MAX_PAUSE_SEC}s gekürzt (-{trimmed_total:.1f}s) "
                      f"(Sprache: {whisper_lang}, p={whisper_prob})",
                      flush=True)
            except Exception as e:
                # Graceful degradation: scenes keep their estimated start/dur, the
                # sync invariant and SFX timing simply fall back to those (both
                # already check start_aligned/end_aligned for None before using them),
                # and the raw (untrimmed) upload is used for muxing below.
                print(f"  [Whisper] Übersprungen, geschätzte Timeline bleibt aktiv: {e}", flush=True)

        # Everything downstream (sync invariant, sound design, final mux) uses the
        # pause-trimmed audio whenever it exists — it's the same recording, just
        # shorter, so nothing about "which audio is the real one" changes for the
        # viewer, only that dead air is gone.
        if os.path.exists(trimmed_audio_path) and os.path.getsize(trimmed_audio_path) > 0:
            audio_path = trimmed_audio_path

        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "csv=p=0", audio_path], capture_output=True, text=True, timeout=15)
        audio_duration = float(probe.stdout.strip())

        frames = _apply_sync_invariant(scenes, audio_duration, RENDER_FPS)
        for s, f in zip(scenes, frames):
            s["_frames"] = f

        stage("motion")
        for idx, s in enumerate(scenes):
            if not s.get("motion"):
                s["motion"] = _motion_for_scene(s, scenes[idx - 1] if idx > 0 else None)

        # Transition points (same rule as the whoosh SFX event) get their PRECEDING
        # scene's clip rendered with extra frames — exactly THIS transition's own
        # duration (varies per pacing family, see TRANSITION_LIBRARY: calm lingers at
        # 0.8s, punchy snaps at 0.3s) — tacked on. The crossfade then consumes exactly
        # that overlap, so the merged clip's total duration still equals the original,
        # uncompensated sum of both scenes' planned durations, keeping the frame-exact
        # sync invariant intact regardless of which duration was used.
        transition_at = [idx for idx in range(len(scenes)) if _has_transition_before(scenes, idx)]
        for idx in transition_at:
            prev = scenes[idx - 1]
            _ttype, _sfx, t_duration = _transition_for_scene(scenes[idx], idx)
            transition_frames = round(t_duration * RENDER_FPS)
            prev["_frames"] = (prev.get("_frames") or round(prev["dur"] * RENDER_FPS)) + transition_frames

        overlay_opts = get_video_overlay_opts(cid, vid)
        stage("clips", 0, len(scenes))
        clip_paths = []
        for idx, s in enumerate(scenes):
            if stop_requested():
                raise RuntimeError("Abgebrochen (Stop angefordert)")
            clip_path = os.path.join(render_dir, f"{s['i']:03d}.mp4")
            img_path = os.path.join(v_out(cid, vid), s["file"])
            _render_clip(img_path, s, clip_path, fps=RENDER_FPS, overlay_opts=overlay_opts)
            clip_paths.append(clip_path)
            s["clip_file"] = os.path.basename(clip_path)
            stage("clips", idx + 1, len(scenes))

        # Merge in crossfades ONLY at the identified transition points — every other
        # cut stays a hard cut via the lossless concat demuxer below. Each merge
        # replaces the last entry of the growing output list (which may itself already
        # be a merge from a previous transition), so back-to-back transitions chain
        # correctly instead of referencing an already-consumed clip.
        stage("transitions", 0, len(transition_at))
        merged_paths = []
        transitions_done = 0
        for idx, s in enumerate(scenes):
            path = clip_paths[idx]
            if idx in transition_at and merged_paths:
                transition_type, _sfx, t_duration = _transition_for_scene(s, idx)
                s["transition_type"] = transition_type  # sichtbar in plan.json, zum Nachvollziehen
                merged_path = os.path.join(render_dir, f"xfade_{s['i']:03d}.mp4")
                _crossfade_clips(merged_paths[-1], path, merged_path, t_duration, transition_type)
                merged_paths[-1] = merged_path
                transitions_done += 1
                stage("transitions", transitions_done, len(transition_at))
            else:
                merged_paths.append(path)
        clip_paths = merged_paths

        stage("assemble")
        silent_path = os.path.join(render_dir, "silent.mp4")
        _assemble_clips(clip_paths, silent_path)

        stage("audio")
        final_path = os.path.join(v_out(cid, vid), "final.mp4")
        # Sound-design layer (Phase 2.5) — ducked music bed + rule-based SFX, or just the
        # raw voiceover unchanged if no music asset is present (see _build_final_audio).
        mixed_audio_path = _build_final_audio(audio_path, scenes, render_dir)
        _mux_audio(silent_path, mixed_audio_path, final_path)

        stage("review")
        checks = _render_selfcheck(final_path, audio_duration)
        if not all(checks.values()):
            raise RuntimeError(f"Selbstprüfung fehlgeschlagen: {checks}")

        with _PLAN_WRITE_LOCK:
            fresh_plan = json.load(open(plan_path))
            by_i = {s["i"]: s for s in fresh_plan["scenes"]}
            for s in scenes:
                if s["i"] in by_i:
                    by_i[s["i"]]["motion"] = s["motion"]
                    by_i[s["i"]]["clip_file"] = s["clip_file"]
                    if "transition_type" in s:
                        by_i[s["i"]]["transition_type"] = s["transition_type"]
                    if s.get("start_aligned") is not None:
                        by_i[s["i"]]["start_aligned"] = s["start_aligned"]
                        by_i[s["i"]]["end_aligned"] = s["end_aligned"]
            fresh_plan["audio_duration"] = audio_duration
            fresh_plan["render"] = {"file": "final.mp4", "ts": int(time.time()), "checks": checks}
            json.dump(fresh_plan, open(plan_path, "w"), ensure_ascii=False, indent=1)

        # Only delete render_tmp after mux + selfcheck succeeded, and only this
        # directory — v_render_tmp() is deliberately separate from v_out()/generated/,
        # never the same path, so this can never touch the actual generated images.
        shutil.rmtree(render_dir, ignore_errors=True)

        with _RENDER_JOBS_LOCK:
            RENDER_JOBS[key] = {"running": False, "stop_requested": False, "stage": "fertig",
                                 "done": len(scenes), "total": len(scenes), "error": None, "file": "final.mp4",
                                 "ts": time.time()}
        print(f"  [Render] {cid}/{vid}: fertig → final.mp4", flush=True)
    except Exception as e:
        import traceback; traceback.print_exc()
        with _RENDER_JOBS_LOCK:
            prev = RENDER_JOBS.get(key, {})
            RENDER_JOBS[key] = {"running": False, "stop_requested": False, "stage": "error",
                                 "done": prev.get("done", 0), "total": prev.get("total", 0),
                                 "error": str(e), "file": None, "ts": time.time()}
        print(f"  [Render] {cid}/{vid}: Fehler: {e}", flush=True)


def _plan_generate_worker(cid: str, vid: str, text: str, wpm: float, sec: float):
    """Runs script -> scenes -> analysis -> image prompts server-side, the same reason
    as _batch_generate_worker: this used to be one blocking HTTP request, so closing the
    tab mid-run looked like nothing happened and re-clicking started a second, fully
    independent LLM pass over the same script."""
    key = (cid, vid)
    try:
        ensure_video(cid, vid)
        out = v_out(cid, vid)
        for f in os.listdir(out):
            if f.endswith((".jpg", ".png", ".mp4")):
                try:
                    os.remove(os.path.join(out, f))
                    print(f"  [Plan] Gelösche alte Datei: {f}", flush=True)
                except: pass

        with _PLAN_JOBS_LOCK:
            PLAN_JOBS[key]["step"] = "Zerlege Skript in Einheiten …"
        units = split_units(text)

        # Analysis runs on the raw atomic units (not pre-grouped scenes) BEFORE
        # segmentation now, because it also assigns the per-unit pacing label (calm/
        # normal/punchy) used to decide scene cuts below — the same LLM pass that reads
        # the emotional arc decides pacing, so cuts land on "a complete thought" instead
        # of a mechanical time interval, and pacing can't drift from what the model
        # already decided is the climax vs. the setup.
        with _PLAN_JOBS_LOCK:
            PLAN_JOBS[key]["step"] = f"Analysiere {len(units)} Einheiten (Story-Bogen + Pacing) …"
        analysis = analyze_script(units)

        with _PLAN_JOBS_LOCK:
            PLAN_JOBS[key]["step"] = "Gruppiere Szenen nach Pacing …"
        scenes = segment_by_pacing(units, analysis.get("pacing", []), wpm, sec,
                                    sequences=analysis.get("visual_sequences", []),
                                    callouts=analysis.get("callouts", []))

        with _PLAN_JOBS_LOCK:
            PLAN_JOBS[key]["step"] = f"Schreibe Bild-Prompts für {len(scenes)} Szenen …"
        prompts = visual_prompts(scenes, analysis)

        for s, pr in zip(scenes, prompts):
            s["prompt"] = pr["prompt"]; s["concrete_entity"] = pr["concrete_entity"]; s["file"] = None
            s["status"] = "geplant"; s["t"] = fmt_t(s["start"])
            s["video_prompt"] = ""
        _assign_phases(scenes, analysis, len(scenes))
        out_data = {"scenes": scenes, "wpm": wpm, "sec": sec, "characters": analysis.get("characters", [])}
        json.dump(out_data, open(v_plan(cid, vid), "w"), ensure_ascii=False, indent=1)

        with _PLAN_JOBS_LOCK:
            PLAN_JOBS[key] = {"running": False, "step": "Fertig", "error": None, "done": True, "ts": time.time()}
        print(f"  [Plan] {cid}/{vid}: fertig, {len(scenes)} Szenen", flush=True)
    except Exception as e:
        import traceback; traceback.print_exc()
        with _PLAN_JOBS_LOCK:
            PLAN_JOBS[key] = {"running": False, "step": "Fehler", "error": str(e), "done": False, "ts": time.time()}


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


def _produce_worker(cid: str, vid: str, text: str = "", wpm: float = 130.0, sec: float = 4.0):
    """Plan (falls nötig) -> Alle Bilder generieren -> Rendern, nacheinander. Bevorzugt
    einen bereits bestehenden Plan; sonst ein hochgeladenes Voice-over (Option A); sonst
    das übergebene `text` (Option B, manueller Skript-Pfad). Bricht bei Fehler in einer
    Etappe sofort ab, der Etappen-Name + Fehlergrund landen in PRODUCE_JOBS."""
    key = (cid, vid)

    def set_stage(stage):
        with _PRODUCE_JOBS_LOCK:
            if key in PRODUCE_JOBS:
                PRODUCE_JOBS[key]["stage"] = stage

    def stop_requested():
        with _PRODUCE_JOBS_LOCK:
            return PRODUCE_JOBS.get(key, {}).get("stop_requested", False)

    def fail(stage, msg):
        with _PRODUCE_JOBS_LOCK:
            PRODUCE_JOBS[key] = {"running": False, "stage": stage, "stop_requested": False,
                                  "error": msg, "file": None, "ts": time.time()}
        print(f"  [Produce] {cid}/{vid}: Fehler in Etappe '{stage}': {msg}", flush=True)

    try:
        plan_path = v_plan(cid, vid)
        try:
            has_plan = bool(json.load(open(plan_path)).get("scenes"))
        except Exception:
            has_plan = False

        if not has_plan:
            if stop_requested():
                return fail("plan", "Abgebrochen (Stop angefordert)")
            set_stage("plan")
            if os.path.exists(v_audio(cid, vid)):
                try:
                    _transcribe_generate_worker(cid, vid, sec)
                except Exception as e:
                    return fail("plan", f"Transkription fehlgeschlagen: {e}")
            elif text.strip():
                with _PLAN_JOBS_LOCK:
                    PLAN_JOBS[key] = {"running": True, "step": "Startet …", "error": None, "done": False}
                _plan_generate_worker(cid, vid, text, wpm, sec)
                with _PLAN_JOBS_LOCK:
                    plan_err = PLAN_JOBS.get(key, {}).get("error")
                if plan_err:
                    return fail("plan", plan_err)
            else:
                return fail("plan", "Kein Voice-over hochgeladen und kein Skript eingegeben.")

        if stop_requested():
            return fail("images", "Abgebrochen (Stop angefordert)")
        set_stage("images")
        with _BATCH_JOBS_LOCK:
            if BATCH_JOBS.get(key, {}).get("running"):
                return fail("images", "Bild-Generierung läuft bereits für dieses Video.")
        _batch_generate_worker(cid, vid)
        with _BATCH_JOBS_LOCK:
            batch_err = BATCH_JOBS.get(key, {}).get("error")
        if batch_err:
            return fail("images", batch_err)

        if stop_requested():
            return fail("render", "Abgebrochen (Stop angefordert)")
        set_stage("render")
        with _RENDER_JOBS_LOCK:
            RENDER_JOBS[key] = {"running": True, "stop_requested": False, "stage": "startet",
                                 "done": 0, "total": 0, "error": None, "file": None}
        _render_worker(cid, vid)
        with _RENDER_JOBS_LOCK:
            render_state = dict(RENDER_JOBS.get(key, {}))
        if render_state.get("error"):
            return fail("render", render_state["error"])

        with _PRODUCE_JOBS_LOCK:
            PRODUCE_JOBS[key] = {"running": False, "stage": "fertig", "stop_requested": False,
                                  "error": None, "file": render_state.get("file"), "ts": time.time()}
        print(f"  [Produce] {cid}/{vid}: fertig → {render_state.get('file')}", flush=True)
    except Exception as e:
        import traceback; traceback.print_exc()
        with _PRODUCE_JOBS_LOCK:
            cur_stage = PRODUCE_JOBS.get(key, {}).get("stage", "unbekannt")
        fail(cur_stage, str(e))


def gen_image(scene_prompt, master, out_path, char_refs=None):
    """Synchronous image generation — used only for charsheets."""
    full_prompt = _build_image_prompt(scene_prompt, master, char_refs)
    try:
        task_id = _kie_submit_image(full_prompt)
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



def _multipart_upload(url: str, field: str, filename: str, data: bytes, mime: str, extra_fields: dict = None) -> str:
    """Generic multipart/form-data upload, returns response body."""
    boundary = b"----upload-" + str(int(time.time())).encode()
    body = b""
    if extra_fields:
        for k, v in extra_fields.items():
            body += b"--" + boundary + b"\r\n"
            body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    body += (b"--" + boundary + b"\r\n"
             + f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode()
             + f"Content-Type: {mime}\r\n\r\n".encode()
             + data + b"\r\n--" + boundary + b"--\r\n")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary.decode()}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode().strip()


def upload_image_public(local_path: str) -> str:
    """Upload local image to a public host, return URL. Tries litterbox → tmpfiles."""
    with open(local_path, "rb") as f:
        data = f.read()
    ext   = os.path.splitext(local_path)[1].lower() or ".png"
    mime  = "image/png" if ext == ".png" else "image/jpeg"
    fname = "image" + ext

    # Try litterbox.catbox.moe (72h temp, reliable)
    try:
        url = _multipart_upload(
            "https://litterbox.catbox.moe/resources/internals/api.php",
            "fileToUpload", fname, data, mime,
            extra_fields={"reqtype": "fileupload", "time": "72h"}
        )
        if url.startswith("http"):
            print(f"  [Upload] litterbox → {url}", flush=True)
            return url
    except Exception as e:
        print(f"  [Upload] litterbox fehlgeschlagen: {e} — versuche tmpfiles …", flush=True)

    # Fallback: tmpfiles.org
    resp = _multipart_upload("https://tmpfiles.org/api/v1/upload", "file", fname, data, mime)
    try:
        j = json.loads(resp)
        url = j["data"]["url"].replace("tmpfiles.org/", "tmpfiles.org/dl/")
        print(f"  [Upload] tmpfiles → {url}", flush=True)
        return url
    except Exception:
        raise ValueError(f"Upload fehlgeschlagen: {resp}")


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

def gen_veo(video_prompt: str, image_urls: list = None,
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


def gen_video(scene_url: str, video_prompt: str, duration: int = 6, char_ref_url: str = "") -> dict:
    """Submit KIE image-to-video job and return {ok, file_url, error}."""
    ref_url = char_ref_url if char_ref_url else scene_url
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
    since only the word COUNT is used to advance the pointer, never the text itself."""
    wi = 0
    n = len(whisper_words)
    for s in scenes:
        words_in_scene = len(s.get("text", "").split())
        if words_in_scene == 0 or wi >= n:
            continue
        start_idx = wi
        end_idx = min(wi + words_in_scene, n) - 1
        s["start_aligned"] = whisper_words[start_idx]["start"]
        s["end_aligned"] = whisper_words[end_idx]["end"]
        wi = end_idx + 1


# ---------- Pausen-Kürzung (auf Wunsch des Nutzers, nach Phase 3) ----------
# Nutzt genau die Whisper-Wort-Zeitstempel, die Phase 3 ohnehin schon berechnet -- die
# Lücke zwischen Wort N Ende und Wort N+1 Start IST die Sprechpause. Nur der Teil einer
# Pause, der über MAX_PAUSE_SEC hinausgeht, wird herausgeschnitten -- ein kurzer,
# natürlicher Atem-Abstand bleibt erhalten, nur die toten, langen Stellen (z.B. 2-3s
# zwischen Sätzen in einem 8-Minuten-Voiceover) verschwinden.
MAX_PAUSE_SEC = 0.3


def _compute_pause_trims(words: list, max_pause: float = MAX_PAUSE_SEC) -> list:
    """Returns [(trim_start, trim_end), ...] -- the EXCESS portion of every gap between
    consecutive words that's longer than max_pause. Each interval lies entirely inside
    a silent gap, never overlapping an actual spoken word, so cutting it out can never
    clip speech."""
    trims = []
    for i in range(len(words) - 1):
        gap_start = words[i]["end"]
        gap_end = words[i + 1]["start"]
        if gap_end - gap_start > max_pause:
            trims.append((gap_start + max_pause, gap_end))
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

    filter_parts, labels = [], []
    for idx, (s, e) in enumerate(keep_intervals):
        label = f"a{idx}"
        filter_parts.append(f"[0:a]atrim=start={s}:end={e},asetpts=PTS-STARTPTS[{label}]")
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
OVERLAY_SCRIPT = os.path.join(HERE, "render_overlay.py")


def render_text_overlay_png(out_path: str, width: int, height: int, style: str, text: str) -> None:
    """style: 'caption' | 'callout' | 'chapter'. Text wird base64-kodiert übergeben,
    damit beliebige Satzzeichen/Unicode nicht per Shell-Escaping durchgereicht werden müssen."""
    if not os.path.exists(WHISPER_VENV_PY):
        raise RuntimeError(
            "Helfer-venv fehlt (.venv_whisper/) -- einmalig einrichten: "
            "python3 -m venv .venv_whisper && ./.venv_whisper/bin/pip install faster-whisper Pillow"
        )
    text_b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    args = [WHISPER_VENV_PY, OVERLAY_SCRIPT, out_path, str(width), str(height), style, text_b64]
    # 90s, not 30s: normally finishes in well under a second (confirmed: 60-130ms in
    # isolation), but a real render shares the machine with concurrent KIE image
    # polling/downloads and ffmpeg encoding -- observed one real run where disk/CPU
    # contention from those pushed a single venv-python cold start past 30s. This is a
    # transient-contention margin, not a sign the script itself is slow.
    result = subprocess.run(args, capture_output=True, text=True, timeout=90)
    if result.returncode != 0:
        raise RuntimeError(f"Overlay-Rendering fehlgeschlagen: {result.stderr[-500:]}")


# ── ElevenLabs voiceover (Phase 1) ────────────────────────────────────────────
# ElevenLabs is the only service in this stack that returns word timestamps DIRECTLY
# from the generator, eliminating the Whisper-pass we still need for user-uploaded
# audio. The /v1/text-to-speech/{voice_id}/with-timestamps endpoint gives back both
# base64-encoded audio and a per-word `alignment.words` array with start/end floats —
# making this the determinstic, provider-side "single source of truth" for scene
# timing (instead of an LLM hallucinating beats like KIE.transcribe_and_segment does).
#
# Retry policy: 429/5xx → backoff 5s, 10s, 20s (max 3 attempts). Anything else
# (incl. auth 401, validation 422, schema-drift 200-but-no-alignment) raises immediately
# — we do NOT silently fall back to Whisper. The user must see the error and decide
# (ARCHITECTURE.md §6.1: "stillschweigende Fallbacks sind verboten").
EL_BACKOFF_SEC = [5, 10, 20]

def _elevenlabs_call_with_retry(url: str, body: dict, headers: dict) -> dict:
    """POST → parse JSON, retrying 429/5xx with backoff. All other failures raise."""
    last_err = None
    for attempt, wait in enumerate([0] + EL_BACKOFF_SEC):
        if wait:
            print(f"  [ElevenLabs] Retry {attempt}/{len(EL_BACKOFF_SEC)} in {wait}s …", flush=True)
            time.sleep(wait)
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
            headers={**headers, "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            # Read response body for the error message — ElevenLabs returns JSON for
            # 422 validation errors with a `detail` field that's actually useful.
            try:    err_body = json.loads(e.read() or b"{}")
            except: err_body = {}
            detail = (err_body.get("detail") if isinstance(err_body, dict) else None) or e.reason
            last_err = RuntimeError(f"ElevenLabs HTTP {e.code}: {detail}")
            if e.code not in (408, 425, 429) and e.code < 500:
                # 4xx other than 408/425/429 — not retriable (auth/validation/schema errors)
                raise last_err
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = RuntimeError(f"ElevenLabs Netzwerkfehler: {e}")
        except Exception as e:
            last_err = RuntimeError(f"ElevenLabs Fehler: {e}")
    # All retries exhausted
    raise last_err or RuntimeError("ElevenLabs: retries exhausted")

def elevenlabs_generate(text: str, settings: dict) -> dict:
    """Generates TTS from `text` using merged channel settings. Returns the parsed
    ElevenLabs response (audio_base64, alignment.words, task_id-ish id).

    Raises RuntimeError on any failure — including the 'partial response' case where
    ElevenLabs returned audio but no alignment.words, since that's the single thing
    this whole feature exists to deliver and we cannot degrade to Whisper silently."""
    voice_id = settings.get("voice_id") or ""
    if not voice_id:
        raise RuntimeError("Keine voice_id konfiguriert: ~/.elevenlabs_key oder channels/<cid>/voice_id.txt befüllen.")
    key = elevenlabs_key()
    url = f"{ELEVENLABS_API}/text-to-speech/{voice_id}/with-timestamps"
    body = {
        "text": text,
        "model_id": settings.get("model_id") or ELEVENLABS_DEFAULT_MODEL,
        "voice_settings": {
            "stability": float(settings.get("stability", 0.5)),
            "similarity_boost": float(settings.get("similarity_boost", 0.75)),
            "style": float(settings.get("style", 0.0)),
            "use_speaker_boost": bool(settings.get("use_speaker_boost", True)),
        },
        "output_format": settings.get("output_format") or "mp3_44100_128",
    }
    headers = {"xi-api-key": key, "Accept": "application/json"}
    resp = _elevenlabs_call_with_retry(url, body, headers)

    # ElevenLabs returns 200 even for some failure modes — guard the response shape
    # explicitly (cf. user-feedback point 2 / Test 6: partial response must raise, not
    # silently fall back to Whisper).
    if not isinstance(resp, dict):
        raise RuntimeError(f"ElevenLabs-Antwort ist kein JSON-Objekt: {type(resp).__name__}")
    if not resp.get("audio_base64"):
        raise RuntimeError("ElevenLabs antwortete ohne audio_base64 — bitte erneut versuchen.")
    alignment = resp.get("alignment") or {}
    words = alignment.get("words") if isinstance(alignment, dict) else None
    if not isinstance(words, list) or not words:
        raise RuntimeError(
            "ElevenLabs antwortete ohne alignment.words — bitte erneut versuchen "
            "(Provider-Schema-Drift oder leerer Text?)."
        )
    # Normalize word list: [{word, start, end}, ...]
    norm = []
    for w in words:
        if not isinstance(w, dict):
            continue
        txt = (w.get("text") or w.get("word") or "").strip()
        if not txt:
            continue
        try:
            s = float(w.get("start", 0.0)); e = float(w.get("end", s + 0.01))
        except (TypeError, ValueError):
            continue
        norm.append({"word": txt, "start": max(0.0, s), "end": max(s + 0.01, e)})
    if not norm:
        raise RuntimeError("ElevenLabs-alignment.words enthielt keine verwertbaren Wörter.")
    return {
        "audio_base64": resp["audio_base64"],
        "words": norm,
        # ElevenLabs doesn't return an explicit task id here; synthesize one from the
        # voice_id + first/last word + utc so /api/voiceover_status has something stable
        # to display and logging/reproducibility is reasonable.
        "task_id": f"el_{voice_id[:8]}_{int(time.time())}",
    }

def _elevenlabs_persist_and_schedule(cid: str, vid: str, text: str,
                                     settings: dict | None = None,
                                     override_voice_id: str = "") -> dict:
    """Synchronous wrapper around elevenlabs_generate() that handles idempotent
    persistence of voiceover.mp3 + audio_meta.json (atomic write — see Test 7) and
    schedules _transcribe_generate_worker in the background to derive scene plan.json
    from the captured word timestamps. Falls into the existing orchestrator code path
    instead of being its own worker — same resume-safety, same stage tracking.

    Returns the persisted info so the HTTP handler can echo a status response without
    re-reading state. Raises with a clear message on every failure mode (no partial
    persistence)."""
    if not vid:
        raise RuntimeError("Kein Video ausgewählt.")
    ensure_video(cid, vid)

    # 1. Settings + idempotency check — pause-trimmed audio is also invalidated
    # whenever a fresh voiceover is requested, mirroring /api/upload_audio.
    final_settings = load_voice_settings(cid, override_voice_id=override_voice_id)
    if settings:
        for k, v in settings.items():
            if k in final_settings:
                final_settings[k] = v
    voiceover_mp3 = os.path.join(v_uploads(cid, vid), "voiceover.mp3")
    trimmed_path  = os.path.join(v_uploads(cid, vid), "voiceover_trimmed.wav")
    if os.path.exists(trimmed_path):
        try:    os.remove(trimmed_path)
        except Exception: pass

    # 2. ElevenLabs call — raises on partial response (user-feedback point 2 / Test 6)
    raw = elevenlabs_generate(text, final_settings)
    audio_bytes = base64.b64decode(raw["audio_base64"])
    words       = raw["words"]
    task_id     = raw["task_id"]
    char_count  = len(text)

    # 3. Atomic write — MP3 first, then meta. If meta fails we delete the MP3; we do
    # NOT want voiceover.mp3 without audio_meta.json lying around (Test 7).
    meta_written = False
    try:
        # write mp3 (overwrite if exists — fresh voiceover replaces old recording)
        with open(voiceover_mp3, "wb") as f:
            f.write(audio_bytes)
        # write meta (this is the canonical resume-marker: if meta is gone but mp3 exists,
        # the next call re-runs ElevenLabs and clobbers both — no half-state survives).
        meta = {
            "path": voiceover_mp3,
            "mime": "audio/mpeg",
            "name": "voiceover.mp3",
            "voiceover_source": "elevenlabs",
            "voiceover_task_id": task_id,
            "voiceover_chars": char_count,
            "voiceover_word_timestamps": words,
            "voiceover_settings_used": {
                "voice_id":         final_settings.get("voice_id", ""),
                "model_id":         final_settings.get("model_id", ELEVENLABS_DEFAULT_MODEL),
                "stability":        final_settings.get("stability"),
                "similarity_boost": final_settings.get("similarity_boost"),
                "style":            final_settings.get("style"),
                "use_speaker_boost": final_settings.get("use_speaker_boost"),
            },
        }
        json.dump(meta, open(v_audio(cid, vid), "w"), ensure_ascii=False, indent=1)
        meta_written = True
    finally:
        if not meta_written and os.path.exists(voiceover_mp3):
            try:    os.remove(voiceover_mp3)
            except Exception: pass

    # 4. Update plan.json's already-existing scenes by clearing old aligned timestamps.
    # _render_worker will redo alignment from the new word list next time it runs.
    with _PLAN_WRITE_LOCK:
        try:
            plan = json.load(open(v_plan(cid, vid)))
            for s in plan.get("scenes", []):
                s.pop("start_aligned", None)
                s.pop("end_aligned", None)
            json.dump(plan, open(v_plan(cid, vid), "w"), ensure_ascii=False, indent=1)
        except Exception:
            pass

    # 5. Schedule the existing _transcribe_generate_worker — that worker is now
    # voiceover_source-aware (Phase 1.D below) and will skip the Gemini-transcribe step
    # because meta["voiceover_source"] == "elevenlabs" + meta["voiceover_word_timestamps"]
    # are already populated.
    plan_thread_sec = float(settings.get("sec", 5.5)) if isinstance(settings, dict) else 5.5
    def _run():
        try:
            _transcribe_generate_worker(cid, vid, plan_thread_sec)
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"  [ElevenLabs→Plan] Fehler in _transcribe_generate_worker: {e}", flush=True)
    threading.Thread(target=_run, daemon=True).start()

    # 6. Mark the VOICE_JOBS entry as done — the actual stage progression is observed
    # via /api/plan_status + /api/produce_status (orchestrator), this dict only carries
    # the ElevenLabs-specific state.
    with _VOICE_JOBS_LOCK:
        VOICE_JOBS[(cid, vid)] = {
            "running": False,
            "stage": "fertig",
            "error": None,
            "voiceover_source": "elevenlabs",
            "voiceover_task_id": task_id,
            "voiceover_chars": char_count,
            "ts": time.time(),
            "resume": False,
        }
    print(f"  [ElevenLabs] {cid}/{vid}: voiceover.mp3 ({len(audio_bytes)//1024} KB, "
          f"{len(words)} Wörter, chars={char_count}) — Plan-Worker gestartet", flush=True)
    return {
        "ok": True,
        "task_id": task_id,
        "audio_kb": len(audio_bytes) // 1024,
        "n_words": len(words),
        "chars": char_count,
    }

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
    # Clear old generated files when transcribing new audio.
    out_dir = v_out(cid, vid)
    for f in os.listdir(out_dir):
        if f.endswith((".jpg", ".png", ".mp4")):
            try:
                os.remove(os.path.join(out_dir, f))
                print(f"  [Transcribe] Gelösche alte Datei: {f}", flush=True)
            except: pass

    is_elevenlabs = (meta.get("voiceover_source") == "elevenlabs"
                     and bool(meta.get("voiceover_word_timestamps")))

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
    for s, pr in zip(scenes, prompts):
        s["prompt"] = pr["prompt"]; s["concrete_entity"] = pr["concrete_entity"]
    # video_prompt stays empty — only generated on demand per scene, see /api/plan comment
    for s in scenes:
        s["video_prompt"] = ""
    _assign_phases(scenes, analysis, len(scenes))
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
    json.dump(out, open(v_plan(cid, vid), "w"), ensure_ascii=False, indent=1)
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
        self.end_headers()
        self.wfile.write(body)

    def _read(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        p = urlparse(self.path).path
        qs = parse_qs(urlparse(self.path).query)
        cid = qs.get("channel", ["default"])[0]
        vid = qs.get("video", [""])[0]
        if p == "/":
            return self._send(200, open(os.path.join(HERE, "dashboard.html"), encoding="utf-8").read(), "text/html; charset=utf-8")
        if p == "/api/channels":
            return self._send(200, {"channels": load_channels()})
        if p == "/api/videos":
            return self._send(200, {"videos": load_videos(cid)})
        if p == "/api/char_ref":
            ref_path = os.path.join(ch_dir(cid), "char_ref_url.txt")
            url = open(ref_path).read().strip() if os.path.exists(ref_path) else ""
            return self._send(200, {"url": url})
        if p == "/api/get_mode":
            return self._send(200, {"mode": get_video_mode(cid, vid)})
        if p == "/api/vid_master":
            try:    txt = open(ch_vid_master(cid)).read()
            except: txt = VIDEO_MASTER_DEFAULT
            return self._send(200, {"master": txt})
        if p == "/api/job_status":
            job_id = qs.get("job_id", [""])[0]
            return self._send(200, JOBS.get(job_id, {"status": "unknown"}))
        if p == "/api/generate_all_status":
            with _BATCH_JOBS_LOCK:
                state = BATCH_JOBS.get((cid, vid))
            return self._send(200, state or {"running": False})
        if p == "/api/render_status":
            with _RENDER_JOBS_LOCK:
                state = RENDER_JOBS.get((cid, vid))
            return self._send(200, state or {"running": False})
        if p == "/api/produce_status":
            with _PRODUCE_JOBS_LOCK:
                state = PRODUCE_JOBS.get((cid, vid))
            return self._send(200, state or {"running": False})
        if p == "/api/video_meta":
            meta = load_v_meta(cid, vid)
            meta["has_thumbnail"] = os.path.exists(os.path.join(v_out(cid, vid), "thumbnail.jpg"))
            return self._send(200, meta)
        if p == "/api/transcribe_status":
            return self._send(200, dict(TX_STATUS))
        # ElevenLabs voiceover endpoints (Phase 1) --------------------------------
        if p == "/api/voiceover_status":
            with _VOICE_JOBS_LOCK:
                state = VOICE_JOBS.get((cid, vid))
            return self._send(200, state or {"running": False})
        if p == "/api/elevenlabs_voices":
            # Lists library voices + any cloned voices the account owns (account-only;
            # no env fallback here — without a key the user just gets an empty list,
            # which the dropdown renders as "configure voice first").
            try:
                req = urllib.request.Request(f"{ELEVENLABS_API}/voices",
                    headers={"xi-api-key": elevenlabs_key(), "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.load(r)
                voices = data.get("voices", [])
                return self._send(200, {"voices": [{
                    "voice_id": v.get("voice_id"),
                    "name": v.get("name"),
                    "category": v.get("category"),
                    "preview_url": v.get("preview_url"),
                } for v in voices]})
            except Exception as e:
                return self._send(200, {"voices": [], "error": str(e)})
        if p == "/api/elevenlabs_settings":
            return self._send(200, load_voice_settings(cid))
        if p == "/api/master":
            return self._send(200, {"master": read_master(cid)})
        if p == "/api/image_model":
            return self._send(200, {"model": get_video_image_model(cid, vid), "options": list(VALID_IMAGE_MODELS)})
        if p == "/api/overlay_opts":
            return self._send(200, get_video_overlay_opts(cid, vid))
        if p == "/api/plan":
            try:    return self._send(200, json.load(open(v_plan(cid, vid))))
            except: return self._send(200, {"scenes": []})
        if p == "/api/plan_status":
            with _PLAN_JOBS_LOCK:
                state = PLAN_JOBS.get((cid, vid))
            return self._send(200, state or {"running": False})
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
            sheets = []
            for f in sorted(os.listdir(ch_sheets(cid))):
                if f.endswith(".json"):
                    try:
                        meta = json.load(open(os.path.join(ch_sheets(cid), f)))
                        img = os.path.join(ch_sheets(cid), f.replace(".json", ".png"))
                        meta["has_image"] = os.path.exists(img)
                        sheets.append(meta)
                    except: pass
            return self._send(200, {"sheets": sheets})
        if p.startswith("/charsheets/"):
            fp = os.path.join(ch_sheets(cid), os.path.basename(p))
            if os.path.exists(fp):
                b = open(fp, "rb").read()
                return self._send(200, b, "image/jpeg" if b[:2] == b"\xff\xd8" else "image/png")
            return self._send(404, {"error": "not found"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        p = urlparse(self.path).path
        try:    d = self._read()
        except: return self._send(400, {"error": "bad json"})
        cid = d.get("channel", "default")
        vid = d.get("video", "")

        # ── Video management (one video = one script/plan within a channel) ────
        if p == "/api/videos":
            name = d.get("name", "Neues Video").strip()
            entry = create_video(cid, name)
            return self._send(200, {"ok": True, **entry})
        if p == "/api/videos/delete":
            videos = [v for v in load_videos(cid) if v["id"] != vid]
            save_videos(cid, videos)
            shutil.rmtree(v_dir(cid, vid), ignore_errors=True)
            return self._send(200, {"ok": True})
        if p == "/api/videos/rename":
            new_name = d.get("name", "").strip()
            videos = load_videos(cid)
            for v in videos:
                if v["id"] == vid and new_name: v["name"] = new_name
            save_videos(cid, videos)
            return self._send(200, {"ok": True})

        # ── Channel management ────────────────────────────────────────────────
        if p == "/api/channels":
            name = d.get("name", "Neuer Kanal").strip()
            safe = re.sub(r"[^\w]", "_", name.lower())[:30] or "kanal"
            chs  = load_channels()
            ids  = {c["id"] for c in chs}
            cid_new = safe if safe not in ids else f"{safe}_{int(time.time())%10000}"
            chs.append({"id": cid_new, "name": name})
            save_channels(chs)
            ensure_channel(cid_new)
            # copy current channel's master as starting point
            src = ch_master(cid if cid in ids else "default")
            dst = ch_master(cid_new)
            if os.path.exists(src) and not os.path.exists(dst):
                shutil.copy2(src, dst)
            return self._send(200, {"ok": True, "id": cid_new, "name": name})
        if p == "/api/channels/delete":
            chs = load_channels()
            if len(chs) <= 1:
                return self._send(400, {"error": "Letzter Kanal kann nicht gelöscht werden."})
            save_channels([c for c in chs if c["id"] != cid])
            return self._send(200, {"ok": True})
        if p == "/api/channels/rename":
            new_name = d.get("name", "").strip()
            chs = load_channels()
            for c in chs:
                if c["id"] == cid: c["name"] = new_name
            save_channels(chs)
            return self._send(200, {"ok": True})

        # ── Master prompt ─────────────────────────────────────────────────────
        if p == "/api/master":
            write_master(cid, d.get("master", ""))
            return self._send(200, {"ok": True})
        if p == "/api/image_model":
            if not vid: return self._send(400, {"error": "Kein Video ausgewählt"})
            set_video_image_model(cid, vid, d.get("model", "nano-banana-2"))
            return self._send(200, {"ok": True})
        if p == "/api/overlay_opts":
            if not vid: return self._send(400, {"error": "Kein Video ausgewählt"})
            set_video_overlay_opts(cid, vid, d.get("opts", {}))
            return self._send(200, {"ok": True})

        # ── Script generator (global, no channel needed) ──────────────────────
        if p == "/api/generate_script":
            raw = d.get("text", "").strip()
            lang = d.get("lang", "de")
            if not raw:
                return self._send(400, {"error": "Kein Text eingegeben."})
            try:
                print(f"  [Script] {lang.upper()} ({len(raw)} Zeichen) …", flush=True)
                return self._send(200, {"ok": True, "script": generate_script(raw, lang)})
            except Exception as e:
                import traceback; traceback.print_exc()
                return self._send(500, {"error": str(e)})

        # ── Title generator ──────────────────────────────────────────────────
        if p == "/api/generate_titles":
            if not vid: return self._send(400, {"error": "Kein Video ausgewählt"})
            try:
                plan = json.load(open(v_plan(cid, vid)))
                full_script = " ".join(s.get("text", "") for s in plan["scenes"])
            except Exception as e:
                return self._send(500, {"error": f"Plan lesen: {e}"})
            if not full_script.strip():
                return self._send(400, {"error": "Kein Skript vorhanden"})
            print(f"  [Title] Generiere Titel-Optionen …", flush=True)
            titles = generate_titles(full_script, n=5)
            if not titles:
                return self._send(500, {"error": "Titel-Generierung fehlgeschlagen"})
            meta = load_v_meta(cid, vid)
            meta["titles"] = titles
            save_v_meta(cid, vid, meta)
            return self._send(200, {"ok": True, "titles": titles})

        if p == "/api/select_title":
            if not vid: return self._send(400, {"error": "Kein Video ausgewählt"})
            meta = load_v_meta(cid, vid)
            meta["selected_title"] = d.get("title", "").strip()
            save_v_meta(cid, vid, meta)
            return self._send(200, {"ok": True})

        # ── Thumbnail generator ───────────────────────────────────────────────
        if p == "/api/generate_thumbnail":
            if not vid: return self._send(400, {"error": "Kein Video ausgewählt"})
            try:
                plan = json.load(open(v_plan(cid, vid)))
                full_script = " ".join(s.get("text", "") for s in plan["scenes"])
            except Exception as e:
                return self._send(500, {"error": f"Plan lesen: {e}"})
            if not full_script.strip():
                return self._send(400, {"error": "Kein Skript vorhanden"})
            mode = get_video_mode(cid, vid)
            try:
                master_style = (open(ch_vid_master(cid)).read().strip() if mode == "video"
                                 else read_master(cid)) or VIDEO_MASTER_DEFAULT
            except: master_style = VIDEO_MASTER_DEFAULT
            print(f"  [Thumbnail] Generiere Prompt …", flush=True)
            prompt = make_thumbnail_prompt(full_script, master_style)
            print(f"  [Thumbnail] Prompt: {prompt[:120]} …", flush=True)
            IMAGE_GEN_SEMAPHORE.acquire()
            print(f"  [Thumbnail] Semaphore erhalten, submitte an KIE …", flush=True)
            char_ref_url = get_channel_char_ref(cid)
            try:
                res = gen_thumbnail_image(prompt, master_style, os.path.join(v_out(cid, vid), "thumbnail.jpg"),
                                           model=get_video_image_model(cid, vid),
                                           ref_urls=[char_ref_url] if char_ref_url else None)
            finally:
                IMAGE_GEN_SEMAPHORE.release()
            if not res["ok"]:
                print(f"  [Thumbnail] Fehler: {res['error']}", flush=True)
                return self._send(500, {"error": res["error"]})
            print(f"  [Thumbnail] Fertig → {res['file']}", flush=True)
            meta = load_v_meta(cid, vid)
            meta["thumbnail_prompt"] = prompt
            save_v_meta(cid, vid, meta)
            return self._send(200, {"ok": True, "file": res["file"], "prompt": prompt, "ts": int(time.time())})

        # ── Scene plan ────────────────────────────────────────────────────────
        if p == "/api/plan":
            wpm = float(d.get("wpm", 150)); sec = float(d.get("sec", 4))
            text = clean_script(d.get("script", ""))
            if not text: return self._send(200, {"scenes": []})
            if not vid: return self._send(400, {"error": "Kein Video ausgewählt"})
            key = (cid, vid)
            with _PLAN_JOBS_LOCK:
                if PLAN_JOBS.get(key, {}).get("running"):
                    return self._send(200, {"ok": True, "already_running": True})
                # Set running=True atomically with the check, before the worker thread
                # exists — same fix as the image batch job race (see generate_all_start).
                PLAN_JOBS[key] = {"running": True, "step": "Startet …", "error": None, "done": False}
            threading.Thread(target=_plan_generate_worker, args=(cid, vid, text, wpm, sec), daemon=True).start()
            return self._send(200, {"ok": True, "already_running": False})

        if p == "/api/plan_status_reset":
            with _PLAN_JOBS_LOCK:
                PLAN_JOBS.pop((cid, vid), None)
            return self._send(200, {"ok": True})

        # ── Mode toggle ───────────────────────────────────────────────────────
        if p == "/api/set_mode":
            mode = d.get("mode", "image")
            if mode not in ("image", "video"): mode = "image"
            set_video_mode(cid, vid, mode)
            return self._send(200, {"ok": True, "mode": mode})

        # ── Video master prompt ───────────────────────────────────────────────
        if p == "/api/vid_master":
            txt = d.get("master", "").strip()
            open(ch_vid_master(cid), "w").write(txt)
            return self._send(200, {"ok": True})

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
                char_ref_path = os.path.join(ch_dir(cid), "char_ref_url.txt")
                char_ref_url  = open(char_ref_path).read().strip() if os.path.exists(char_ref_path) else ""
                scene_img_url = scene.get("source_url", "")
                ref_url = char_ref_url or scene_img_url

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
                    json.dump(plan, open(v_plan(cid, vid), "w"), ensure_ascii=False, indent=1)
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
        if p == "/api/elevenlabs_settings":
            # Per-field defaults — _callers may POST just one slider, everything else
            # should keep its persisted value rather than silently regressing (an empty
            # string for use_speaker_boost would turn into False via bool()).
            save_voice_settings(cid, {
                "voice_id": d.get("voice_id", ""),
                "model_id": d.get("model_id") or ELEVENLABS_DEFAULT_MODEL,
                "stability": d.get("stability") if d.get("stability") is not None else ELEVENLABS_VOICE_SETTINGS_DEFAULT["stability"],
                "similarity_boost": d.get("similarity_boost") if d.get("similarity_boost") is not None else ELEVENLABS_VOICE_SETTINGS_DEFAULT["similarity_boost"],
                "style": d.get("style") if d.get("style") is not None else ELEVENLABS_VOICE_SETTINGS_DEFAULT["style"],
                # bool fields: explicit false string is OK, missing key means "leave alone"
                "use_speaker_boost": bool(d["use_speaker_boost"]) if "use_speaker_boost" in d else ELEVENLABS_VOICE_SETTINGS_DEFAULT["use_speaker_boost"],
                "output_format": d.get("output_format") or ELEVENLABS_VOICE_SETTINGS_DEFAULT["output_format"],
            })
            return self._send(200, load_voice_settings(cid))

        if p == "/api/voiceover_preview":
            text = (d.get("text") or "Hallo Welt, das ist ein Stimm-Sample.").strip()[:500]
            settings = {k: d.get(k) for k in (
                "voice_id", "model_id", "stability", "similarity_boost",
                "style", "use_speaker_boost", "output_format") if d.get(k) is not None}
            try:
                raw = elevenlabs_generate(text, load_voice_settings(cid, override_voice_id=(
                    settings.get("voice_id") if settings.get("voice_id") else "")))
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
            # Optional sec override for downstream scene-pacing (defaults to NORMAL_HARD_CAP_SEC).
            sec = d.get("sec")
            settings = {k: d.get(k) for k in (
                "voice_id", "model_id", "stability", "similarity_boost",
                "style", "use_speaker_boost", "output_format", "sec") if d.get(k) is not None}
            # Resume-Marker: if audio_meta.json + plan.json beide schon vorhanden und
            # voiceover_source == "elevenlabs", KEIN API-Call. Idempotent wie User-Feedback
            # Punkt 3 verlangt.
            meta_path = v_audio(cid, vid)
            plan_p = v_plan(cid, vid)
            try:
                meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
            except Exception:
                meta = {}
            if (meta.get("voiceover_source") == "elevenlabs"
                and os.path.exists(meta.get("path", "")) if meta else False
                and meta.get("voiceover_word_timestamps")
                and os.path.exists(plan_p)):
                with _VOICE_JOBS_LOCK:
                    VOICE_JOBS[(cid, vid)] = {
                        "running": False, "stage": "fertig (resume)",
                        "error": None, "voiceover_source": "elevenlabs",
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
            with _VOICE_JOBS_LOCK:
                VOICE_JOBS[(cid, vid)] = {
                    "running": True, "stage": "elevenlabs-generate",
                    "error": None, "voiceover_source": "elevenlabs",
                    "voiceover_task_id": None, "voiceover_chars": None,
                    "ts": time.time(), "resume": False,
                }
            try:
                result = _elevenlabs_persist_and_schedule(cid, vid, text,
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
            if not img_b64: return self._send(400, {"error": "image fehlt"})
            img_bytes = base64.b64decode(img_b64)
            safe = re.sub(r"[^\w\-]", "_", name.lower())
            img_path  = os.path.join(ch_sheets(cid), f"{safe}.png")
            meta_path = os.path.join(ch_sheets(cid), f"{safe}.json")
            open(img_path, "wb").write(img_bytes)
            try:    desc = analyze_char_image(img_bytes, mime)
            except Exception as e:
                desc = ""; print(f"  [Char] Analyse-Fehler: {e}", flush=True)
            json.dump({"name": name, "description": desc, "safe": safe, "mime": "image/png"},
                      open(meta_path, "w"), ensure_ascii=False)
            return self._send(200, {"ok": True, "name": name, "safe": safe, "description": desc})

        if p == "/api/gen_charsheet":
            name = d.get("name", "").strip(); desc = d.get("description", "").strip()
            if not name or not desc: return self._send(400, {"error": "name und description erforderlich"})
            safe = re.sub(r"[^\w\-]", "_", name.lower())
            tmp  = os.path.join(ch_sheets(cid), f"_tmp_{safe}.jpg")
            try:
                img_bytes = gen_charsheet(cid, name, desc)
                open(os.path.join(ch_sheets(cid), f"{safe}.png"), "wb").write(img_bytes)
                json.dump({"name": name, "description": desc, "safe": safe, "mime": "image/jpg"},
                          open(os.path.join(ch_sheets(cid), f"{safe}.json"), "w"), ensure_ascii=False)
                return self._send(200, {"ok": True, "name": name, "safe": safe})
            except Exception as e:
                import traceback; traceback.print_exc()
                return self._send(500, {"error": str(e)})

        # ── Generate one image (async) ────────────────────────────────────────
        # ── "Alle Bilder generieren" — runs server-side, survives reloads ──────
        if p == "/api/generate_all_start":
            if not vid: return self._send(400, {"error": "Kein Video ausgewählt"})
            key = (cid, vid)
            with _BATCH_JOBS_LOCK:
                if BATCH_JOBS.get(key, {}).get("running"):
                    return self._send(200, {"ok": True, "already_running": True})
                # Set running=True HERE, atomically with the check above, before the
                # worker thread even exists — not inside the thread itself. Setting it
                # later left a window where two rapid start calls (e.g. a user
                # double-clicking, or stop-then-immediately-start) could both see
                # "not running" and each spin up their own worker, causing multiple
                # concurrent generation loops hammering KIE in parallel.
                BATCH_JOBS[key] = {"running": True, "stop_requested": False, "done": 0,
                                    "total": 0, "current_i": [], "error": None}
            threading.Thread(target=_batch_generate_worker, args=(cid, vid), daemon=True).start()
            return self._send(200, {"ok": True, "already_running": False})

        if p == "/api/generate_all_stop":
            key = (cid, vid)
            with _BATCH_JOBS_LOCK:
                if key in BATCH_JOBS:
                    BATCH_JOBS[key]["stop_requested"] = True
            return self._send(200, {"ok": True})

        # ── Auto-rendering (Ken Burns clips -> concat -> audio mux) ─────────────
        if p == "/api/render_start":
            if not vid: return self._send(400, {"error": "Kein Video ausgewählt"})
            key = (cid, vid)
            with _RENDER_JOBS_LOCK:
                if RENDER_JOBS.get(key, {}).get("running"):
                    return self._send(200, {"ok": True, "already_running": True})
                # Same atomic "set running=True before the thread exists" fix as
                # generate_all_start above — avoids two rapid start calls each
                # spinning up their own render worker on the same video.
                RENDER_JOBS[key] = {"running": True, "stop_requested": False, "stage": "startet",
                                     "done": 0, "total": 0, "error": None, "file": None}
            threading.Thread(target=_render_worker, args=(cid, vid), daemon=True).start()
            return self._send(200, {"ok": True, "already_running": False})

        if p == "/api/render_stop":
            key = (cid, vid)
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
            with _RENDER_JOBS_LOCK:
                if RENDER_JOBS.get(key, {}).get("running"):
                    RENDER_JOBS[key]["stop_requested"] = True
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
            full_prompt = _build_image_prompt(prompt, read_master(cid), load_char_refs(cid))
            # Global concurrency cap — blocks here if MAX_CONCURRENT_IMAGE_GENS are already
            # in flight from ANY source (batch or other individual clicks), instead of
            # firing this KIE submission immediately alongside all the others. Released by
            # _image_job_worker once this scene's generation fully finishes.
            IMAGE_GEN_SEMAPHORE.acquire()
            char_ref_url = get_channel_char_ref(cid)
            try:
                task_id = _kie_submit_image(full_prompt, model=get_video_image_model(cid, vid),
                                             ref_urls=[char_ref_url] if char_ref_url else None)
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
                            s["prompt"] = prompt; s["status"] = "läuft"
                    json.dump(plan, open(v_plan(cid, vid), "w"), ensure_ascii=False, indent=1)
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
            # Load canonical character reference URL (if set)
            char_ref_path = os.path.join(ch_dir(cid), "char_ref_url.txt")
            char_ref_url = open(char_ref_path).read().strip() if os.path.exists(char_ref_path) else ""

            # Scene image as fallback reference (upload if no CDN url yet)
            source_url = scene.get("source_url", "")
            url_age = int(time.time()) - scene.get("source_url_ts", 0)
            if not char_ref_url and (not source_url or url_age > 72000):
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
                        json.dump(plan, open(v_plan(cid, vid), "w"), ensure_ascii=False, indent=1)
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

            using_ref = "char-ref" if char_ref_url else "scene-img"
            print(f"  [Video] Referenz: {using_ref}", flush=True)
            res = gen_video(source_url, video_prompt, duration, char_ref_url)
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
                    json.dump(plan, open(v_plan(cid, vid), "w"), ensure_ascii=False, indent=1)
                except: pass
            print(f"  [Video] Szene {i} fertig → {fn} (audio={'ja' if has_audio else 'nein'})", flush=True)
            return self._send(200, {"ok": True, "file": fn, "video_prompt": video_prompt, "ts": int(time.time())})

        # ── Set canonical character reference URL ─────────────────────────────
        if p == "/api/set_char_ref":
            url = d.get("url", "").strip()
            ref_path = os.path.join(ch_dir(cid), "char_ref_url.txt")
            if not url:
                # Clear: delete file
                if os.path.exists(ref_path): os.remove(ref_path)
                return self._send(200, {"ok": True, "url": ""})
            if not url.startswith("http"):
                return self._send(400, {"error": "Ungültige URL"})
            open(ref_path, "w").write(url)
            return self._send(200, {"ok": True, "url": url})

        # ── Generate + upload canonical character reference image ──────────────
        if p == "/api/gen_char_ref":
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
                            # Save locally
                            ref_path = os.path.join(ch_dir(cid), "char_ref.png")
                            open(ref_path, "wb").write(img_data)
                            pub_url = upload_image_public(ref_path)
                            open(os.path.join(ch_dir(cid), "char_ref_url.txt"), "w").write(pub_url)
                            return self._send(200, {"ok": True, "url": pub_url})
                    elif info.get("state") == "fail":
                        return self._send(500, {"error": f"KIE fail: {info.get('failMsg')}"})
                except Exception as e:
                    print(f"  [CharRef] Poll error: {e}", flush=True)
            return self._send(500, {"error": "Timeout beim Generieren des Character-Refs"})

        return self._send(404, {"error": "not found"})

def main():
    port = 8000
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    if not os.path.exists(KIE_KEY_FILE):
        print("WARN: ~/.kie_key fehlt — alle KI-Funktionen werden scheitern.")
    _start_job_cleanup_daemon()
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"Dashboard läuft: http://localhost:{port}  (Strg+C zum Beenden)")
    srv.serve_forever()

if __name__ == "__main__":
    main()
