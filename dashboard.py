#!/usr/bin/env python3
"""Localhost-Dashboard für die Storyboard-Bildgenerierung.
Nur Python-Standardlib. Start: python3 dashboard.py [--port 8000]
"""
import os, re, sys, json, time, base64, zipfile, io, threading
import urllib.request, urllib.error
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

# ── Per-channel path helpers (channel = brand/style, holds N videos) ──────────
def ch_dir(cid):        return os.path.join(CHANNELS_DIR, cid)
def ch_master(cid):     return os.path.join(ch_dir(cid), "master_prompt.txt")
def ch_vid_master(cid): return os.path.join(ch_dir(cid), "video_master_prompt.txt")
def ch_sheets(cid):     return os.path.join(ch_dir(cid), "charsheets")
def ch_videos_file(cid):return os.path.join(ch_dir(cid), "videos.json")

# ── Per-video path helpers (one video = one script/plan/generated set) ────────
def v_dir(cid, vid):     return os.path.join(ch_dir(cid), "videos", vid)
def v_out(cid, vid):     return os.path.join(v_dir(cid, vid), "generated")
def v_plan(cid, vid):    return os.path.join(v_out(cid, vid), "plan.json")
def v_uploads(cid, vid): return os.path.join(v_dir(cid, vid), "uploads")
def v_audio(cid, vid):   return os.path.join(v_uploads(cid, vid), "audio_meta.json")

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

# Shared transcription status (thread-safe via GIL for simple dict ops)
TX_STATUS = {"step": 0, "total": 4, "msg": "Bereit", "running": False, "error": ""}

def tx(step, msg):
    TX_STATUS["step"] = step
    TX_STATUS["msg"] = msg
    print(f"  [TX {step}/{TX_STATUS['total']}] {msg}", flush=True)

def kie_key():
    return open(KIE_KEY_FILE).read().strip()

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

    gen_cfg = {"temperature": temp, "thinkingConfig": {"thinkingLevel": thinking_level}}
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
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=hdrs)
    with urllib.request.urlopen(req, timeout=240) as r:
        resp = json.load(r)
    return resp["candidates"][0]["content"]["parts"][0]["text"]

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

def segment(text, wpm, sec_per_img):
    target = max(3, round(wpm / 60.0 * sec_per_img))
    units = split_units(text)
    segs, cur = [], []
    for u in units:
        cur.append(u)
        if len(" ".join(cur).split()) >= target:
            segs.append(" ".join(cur)); cur = []
    if cur:
        if segs and len(" ".join(cur).split()) < target * 0.5:
            segs[-1] += " " + " ".join(cur)
        else:
            segs.append(" ".join(cur))
    # Zeiten schätzen
    scenes, t = [], 0.0
    for i, seg in enumerate(segs):
        dur = len(seg.split()) / (wpm / 60.0)
        scenes.append({"i": i, "start": round(t, 1), "dur": round(dur, 1), "text": seg})
        t += dur
    return scenes

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
        '  "callbacks": [{"from_beat": N, "to_beat": M, "shared_element": string}]\n'
        "}\n\n"
        'Rule: set "anonymize": true for every real, identifiable named person (public '
        "figures, named victims/individuals) — these get depicted later only as a "
        "silhouette or symbolic stand-in, never named or shown photorealistically.\n\n"
        "BEATS:\n" + json.dumps(beats, ensure_ascii=False)
    )
    for attempt in (1, 2):
        try:
            txt = post_kie_text([{"role": "user", "content": instr}], json_mode=True, temp=0.2)
            return json.loads(txt)
        except Exception as e:
            print(f"Analyse-Fehler (Versuch {attempt}):", e)
    return {}

IMAGE_PROMPT_CHUNK_SIZE = 9    # scenes per LLM call — same reasoning as video: avoid context degradation
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

def _validate_image_prompt_entry(entry: dict) -> bool:
    ip = (entry.get("image_prompt") or "").strip()
    if len(ip) < IMAGE_PROMPT_MIN_LEN:
        return False
    entity = (entry.get("concrete_entity") or "").strip().lower()
    if entity and entity not in ("none", "n/a", "-"):
        words = [w for w in re.findall(r"[a-zA-Z]{4,}", entity) if w not in ("char", "loc", "sym")]
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

def visual_prompts(scenes, master, analysis=None):
    """Generate all still-image prompts, chunked (not all-in-one) with forced intermediate
    reasoning fields and a validation+retry pass — same structure as video_prompts_batch(),
    adapted for stills (no story-phase/camera-move logic, explicit character-consistency
    field instead since there's no chain-extend anchor between separate images).

    Returns list of prompt strings, one per scene, same order as scenes. Style (master
    prompt) is NOT included here — it's appended separately in _build_image_prompt().
    """
    beats = [s["text"] for s in scenes]
    total = len(beats)
    if total == 0:
        return []

    if analysis is None:
        print(f"  [Plan] Analysiere {total} Beats …", flush=True)
        analysis = analyze_script(beats)
    analysis_ctx = json.dumps(analysis, ensure_ascii=False, indent=1) if analysis else "{}"

    prompts: list[str] = []
    chunks = [beats[i:i+IMAGE_PROMPT_CHUNK_SIZE] for i in range(0, total, IMAGE_PROMPT_CHUNK_SIZE)]
    offset = 0
    for ci, chunk in enumerate(chunks):
        print(f"  [Plan] Bild-Chunk {ci+1}/{len(chunks)} ({len(chunk)} Szenen) …", flush=True)
        try:
            entries = _image_prompt_chunk(chunk, offset, total, analysis_ctx)
        except Exception as e:
            print(f"  [Plan] Bild-Chunk-Fehler: {e} — Fallback für diesen Chunk", flush=True)
            entries = [{"image_prompt": f"Scene illustrating: {t[:80]}. Simple, clear composition.",
                        "concrete_entity": ""} for t in chunk]

        for j, entry in enumerate(entries):
            beat_i = offset + j
            if not _validate_image_prompt_entry(entry):
                print(f"  [Plan] Szene {beat_i} zu kurz/generisch — Einzel-Retry …", flush=True)
                entry = _image_prompt_single_retry(beats[beat_i], beat_i, total, analysis_ctx)
            prompts.append(str(entry.get("image_prompt") or f"Scene illustrating: {beats[beat_i][:80]}."))
        offset += len(chunk)

    return prompts

def story_phase(i: int, total: int) -> str:
    return (
        "OPENING"        if i < total * 0.15 else
        "RISING ACTION"  if i < total * 0.50 else
        "CLIMAX"         if i < total * 0.75 else
        "RESOLUTION"
    )

VIDEO_PROMPT_CHUNK_SIZE = 9   # scenes per LLM call — keeps analysis focus from degrading over long batches
VIDEO_PROMPT_MIN_LEN    = 280  # chars — forces setting/light/camera to be spelled out, not just a mood word

_VIDEO_PROMPT_FEWSHOT = """\
EXAMPLE — TOO SHORT / MISSES THE CONTENT (do not do this):
Line: "Reports suggested that people around him were monitored before his murder."
Bad video_prompt: "Dark ominous scene, surveillance concept, cinematic lighting"
→ Wrong: doesn't say WHO was monitored, doesn't show the surveillance mechanism, loses the actual fact in the line.

EXAMPLE — CORRECT:
Line: "Reports suggested that people around him were monitored before his murder."
core_statement: "The target's inner circle was surveilled before his death."
concrete_entity: "char_target (anonymized), sym_surveillance_device"
Good video_prompt: "An empty chair in a press room, a phone resting on the floor beside it,
a faint red glow pulsing from the phone screen suggesting active surveillance, dim somber
lighting from a single overhead source, nobody visible in frame, close-up composition
emphasizing absence and unease"
→ Why better: translates "inner circle monitored" into a concrete object (glowing phone =
surveillance symbol), AND is long enough to define setting/light/framing — not just a mood word.\
"""

def _validate_video_prompt_entry(entry: dict) -> bool:
    vp = (entry.get("video_prompt") or "").strip()
    if len(vp) < VIDEO_PROMPT_MIN_LEN:
        return False
    entity = (entry.get("concrete_entity") or "").strip().lower()
    if entity and entity not in ("none", "n/a", "-"):
        # crude heuristic: does at least one meaningful word from concrete_entity show up in the prompt?
        words = [w for w in re.findall(r"[a-zA-Z]{4,}", entity) if w not in ("char", "loc", "sym")]
        if words and not any(w.lower() in vp.lower() for w in words):
            return False
    return True

def _video_prompt_chunk(chunk_beats: list, chunk_offset: int, total: int,
                         analysis_ctx: str, vid_master: str, prev_prompts: list,
                         has_char_ref: bool) -> list:
    """One LLM call for a small chunk of scenes (8-10), forcing intermediate reasoning
    fields per scene before the final video_prompt text. Returns list of entry dicts."""
    numbered = "\n".join(
        f"{chunk_offset+i+1}. [{story_phase(chunk_offset+i, total)}] {t}"
        for i, t in enumerate(chunk_beats)
    )
    ref_note = (
        "A character reference image will be supplied to the video model — "
        "focus prompts on ACTION and MOVEMENT, not appearance."
        if has_char_ref else
        "No reference image — describe character appearance briefly in each prompt as defined in CHARACTER CONTEXT."
    )
    prev_ctx = (
        "PREVIOUSLY GENERATED PROMPTS (last 2, for visual continuity — reuse the same "
        "visual representation for any recurring entity shown here):\n" +
        "\n".join(f"  • {p[:160]}" for p in prev_prompts[-2:])
        if prev_prompts else ""
    )

    instr = f"""\
You are a video scene director for a documentary/factual video. You receive a structural
ANALYSIS of the full script and a CHUNK of consecutive narrator lines. Work through each
line in order using the forced fields below — do not skip straight to the final prompt text.

ANALYSIS (entities, locations, symbols, emotional arc, callbacks — extracted from the FULL script):
{analysis_ctx}

{prev_ctx}

CHARACTER & STYLE CONTEXT (visual style only — do not repeat this in video_prompt, style is applied separately):
{vid_master.strip()}

REFERENCE IMAGE: {ref_note}

{_VIDEO_PROMPT_FEWSHOT}

For EACH line in the chunk below, produce an object with ALL of these fields, in order:
{{
  "scene": N,
  "core_statement": "What is this line actually claiming/showing? One sentence.",
  "concrete_entity": "The EXACT entity id from ANALYSIS (locations/characters/recurring_symbols)
                       relevant here. If none fits, name the new concrete thing from the line
                       itself (person/place/object/technology). Abstract metaphor ONLY if the
                       line truly has no concrete referent.",
  "callback_check": "Does ANALYSIS.callbacks say this scene references an earlier one? If yes,
                      name the recurring element that MUST appear in video_prompt. Else 'none'.",
  "shot_framing": "Derive shot size ONLY from the story phase tag:
                    OPENING -> wide/establishing shot
                    RISING ACTION -> medium shot, light tension cues in frame
                    CLIMAX -> close-up, high intensity, tight framing
                    RESOLUTION -> wide/distanced, calmer composition",
  "video_prompt": "The final image text. MUST visibly include concrete_entity AND the
                    callback_check element (if not 'none'). MUST implement shot_framing.
                    NO style text here — style is appended separately after this call.
                    Must explicitly name: (1) the concrete main subject, (2) the setting/
                    location, (3) a lighting mood, (4) the camera angle/shot size.
                    A prompt that only describes a vague mood without these four elements
                    is invalid. Minimum {VIDEO_PROMPT_MIN_LEN} characters."
}}

HARD RULE: if a line names a concrete person, place, or technology, video_prompt MUST show
exactly that — check this yourself against your own concrete_entity field before writing it.

NARRATOR LINES IN THIS CHUNK:
{numbered}

Return a JSON array of {len(chunk_beats)} objects, one per line above, in the same order.
"""
    # gemini-3-5-flash + thinkingLevel=high: counteracts the "gets lazy/generic on
    # later batch items" behavior seen with 2.5-flash on long scripts.
    txt = post_gemini_native([{"role": "user", "content": instr}], json_mode=True, temp=0.45)
    arr = json.loads(txt)
    if isinstance(arr, dict):
        for v in arr.values():
            if isinstance(v, list) and len(v) == len(chunk_beats):
                arr = v; break
    if not isinstance(arr, list) or len(arr) != len(chunk_beats):
        raise ValueError(f"unexpected chunk response shape ({type(arr)}, len={len(arr) if isinstance(arr,list) else '?'})")
    return arr

def _video_prompt_single_retry(beat_text: str, beat_i: int, total: int,
                                analysis_ctx: str, vid_master: str, prev_prompts: list,
                                has_char_ref: bool) -> dict:
    """Focused single-scene retry for entries that failed validation in the batch call —
    smaller call, less room for the model to get 'lazy'."""
    try:
        result = _video_prompt_chunk([beat_text], beat_i, total, analysis_ctx, vid_master,
                                      prev_prompts, has_char_ref)
        return result[0]
    except Exception as e:
        print(f"  [VidPrompt] Einzel-Retry Szene {beat_i} fehlgeschlagen: {e}", flush=True)
        return {
            "scene": beat_i + 1, "concrete_entity": "",
            "video_prompt": f"Scene illustrating: {beat_text[:80]}. "
                             "Character center frame, deliberate gesture matching the scene energy. Camera slow zoom-in.",
        }

def video_prompts_batch(scenes: list, vid_master: str, has_char_ref: bool = False) -> list:
    """Generate all T2V video prompts, chunked (not all-in-one) to avoid context
    degradation over long scripts, with forced intermediate reasoning fields and a
    validation+retry pass for entries that come back too short/generic.

    Returns list of prompt strings, one per scene, same order as scenes.
    """
    beats = [s["text"] for s in scenes]
    total = len(beats)
    if total == 0:
        return []

    print(f"  [VidPrompt] Analysiere {total} Szenen …", flush=True)
    analysis = analyze_script(beats)
    analysis_ctx = json.dumps(analysis, ensure_ascii=False, indent=1) if analysis else "{}"

    prompts: list[str] = []
    prev_prompts: list[str] = []
    chunks = [beats[i:i+VIDEO_PROMPT_CHUNK_SIZE] for i in range(0, total, VIDEO_PROMPT_CHUNK_SIZE)]
    offset = 0
    for ci, chunk in enumerate(chunks):
        print(f"  [VidPrompt] Chunk {ci+1}/{len(chunks)} ({len(chunk)} Szenen) …", flush=True)
        try:
            entries = _video_prompt_chunk(chunk, offset, total, analysis_ctx, vid_master,
                                           prev_prompts, has_char_ref)
        except Exception as e:
            print(f"  [VidPrompt] Chunk-Fehler: {e} — Fallback für diesen Chunk", flush=True)
            entries = [{"video_prompt": f"Scene illustrating: {t[:80]}. "
                        "Character center frame, deliberate gesture matching the scene energy. Camera slow zoom-in.",
                        "concrete_entity": ""} for t in chunk]

        for j, entry in enumerate(entries):
            beat_i = offset + j
            if not _validate_video_prompt_entry(entry):
                print(f"  [VidPrompt] Szene {beat_i} zu kurz/generisch — Einzel-Retry …", flush=True)
                entry = _video_prompt_single_retry(beats[beat_i], beat_i, total, analysis_ctx,
                                                    vid_master, prev_prompts, has_char_ref)
            vp = str(entry.get("video_prompt") or f"Scene illustrating: {beats[beat_i][:80]}.")
            prompts.append(vp)
            prev_prompts.append(vp)
        offset += len(chunk)

    return prompts

# ---------- Character sheets ----------

def charsheet_path(name):
    safe = re.sub(r"[^\w\-]", "_", name.lower())
    return os.path.join(CHARSHEET_DIR, safe)

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


def _kie_submit_image(full_prompt: str) -> str:
    """Submit image task to KIE, return task_id."""
    hdrs = {"Authorization": f"Bearer {kie_key()}", "Content-Type": "application/json"}
    body = {"model": KIE_MODEL, "input": {
        "prompt": full_prompt, "aspect_ratio": "16:9",
        "resolution": "2K", "output_format": "jpg",
    }}
    req = urllib.request.Request(f"{KIE_API}/createTask", data=json.dumps(body).encode(), headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"KIE HTTP {e.code}: {e.read().decode()[:200]}")
    if resp.get("code") != 200:
        raise RuntimeError(f"KIE: {resp.get('msg', str(resp))}")
    return resp["data"]["taskId"]


def _image_job_worker(job_id: str, task_id: str, out_path: str, plan_path: str, scene_i: int):
    """Background thread: polls KIE task, downloads result, updates plan."""
    poll_url  = f"{KIE_API}/recordInfo?taskId={task_id}"
    poll_hdrs = {"Authorization": f"Bearer {kie_key()}"}
    print(f"  [KIE] Job {job_id} / task {task_id} läuft …", flush=True)
    for _ in range(80):
        time.sleep(3)
        try:
            with urllib.request.urlopen(urllib.request.Request(poll_url, headers=poll_hdrs), timeout=15) as r:
                info = json.load(r).get("data", {})
        except Exception as e:
            print(f"  [KIE] Poll-Fehler: {e}", flush=True); continue
        state    = info.get("state", "")
        progress = int(info.get("progress", 0))
        JOBS[job_id]["progress"] = progress
        print(f"  [KIE] {job_id} {state} {progress}%", flush=True)
        if state == "success":
            try:    urls = json.loads(info.get("resultJson", "{}")).get("resultUrls", [])
            except: urls = []
            if not urls:
                JOBS[job_id] = {"status": "error", "progress": 0, "error": "Kein Bild in resultUrls"}
                return
            with urllib.request.urlopen(urls[0], timeout=60) as img_r:
                open(out_path, "wb").write(img_r.read())
            fn = os.path.basename(out_path)
            JOBS[job_id] = {"status": "done", "progress": 100,
                            "file": fn, "source_url": urls[0], "ts": int(time.time()), "error": None}
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
                            "error": f"KIE fehlgeschlagen: {info.get('failMsg','unbekannt')}"}
            return
    JOBS[job_id] = {"status": "error", "progress": 0, "error": "KIE Timeout (>4 min)"}


def _veo_job_worker(job_id: str, task_id: str, scene: dict,
                    out_path: str, plan_path: str, cid: str, vid: str, video_prompt: str, chain_len: int = 0):
    """Background thread: polls Veo, downloads video, mixes audio, updates plan."""
    print(f"  [Veo] Worker {job_id} / task {task_id} gestartet", flush=True)
    JOBS[job_id] = {"status": "running", "progress": 20, "file": None, "error": None}

    poll = poll_veo(task_id, timeout=600)
    if not poll["ok"]:
        JOBS[job_id] = {"status": "error", "progress": 0, "error": poll["error"]}
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
        JOBS[job_id] = {"status": "error", "progress": 0, "error": f"Download: {e}"}
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
            with urllib.request.urlopen(urls[0], timeout=60) as img_r:
                open(out_path, "wb").write(img_r.read())
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

T2V_MODEL = "grok-imagine/text-to-video"  # confirmed working on KIE

def gen_t2v(video_prompt: str, duration: int = 6) -> dict:
    """Submit KIE text-to-video job. Returns {ok, task_id} or {ok:False, error}."""
    hdrs = {"Authorization": f"Bearer {kie_key()}", "Content-Type": "application/json"}
    body = {
        "model": T2V_MODEL,
        "input": {
            "prompt":       video_prompt,
            "duration":     max(6, min(30, duration)),
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
        return {"ok": False, "error": f"KIE HTTP {e.code}: {e.read().decode()[:300]}"}
    if resp.get("code") != 200:
        return {"ok": False, "error": f"KIE: {resp.get('msg', str(resp))}"}
    return {"ok": True, "task_id": resp["data"]["taskId"]}

def poll_kie_video(task_id: str, timeout: int = 600) -> dict:
    """Poll KIE until task succeeds or fails. Returns {ok, video_url} or {ok:False, error}."""
    hdrs = {"Authorization": f"Bearer {kie_key()}"}
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(8)
        try:
            req = urllib.request.Request(f"{KIE_API}/recordInfo?taskId={task_id}", headers=hdrs)
            with urllib.request.urlopen(req, timeout=15) as r:
                info = json.load(r)["data"]
            state = info.get("state", "")
            pct   = info.get("completePercent", 0) or 0
            print(f"  [T2V] {state} ({pct}%)", flush=True)
            if state == "success":
                result = json.loads(info.get("resultJson", "{}"))
                url = result.get("resultUrls", [""])[0]
                return {"ok": True, "video_url": url}
            elif state == "fail":
                return {"ok": False, "error": f"KIE fail: {info.get('failMsg', 'unknown')}"}
        except Exception as e:
            print(f"  [T2V] poll error: {e}", flush=True)
    return {"ok": False, "error": "Timeout"}


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
        if p == "/api/transcribe_status":
            return self._send(200, dict(TX_STATUS))
        if p == "/api/master":
            return self._send(200, {"master": read_master(cid)})
        if p == "/api/plan":
            try:    return self._send(200, json.load(open(v_plan(cid, vid))))
            except: return self._send(200, {"scenes": []})
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

        # ── Scene plan ────────────────────────────────────────────────────────
        if p == "/api/plan":
            wpm = float(d.get("wpm", 150)); sec = float(d.get("sec", 4))
            text = clean_script(d.get("script", ""))
            if not text: return self._send(200, {"scenes": []})
            if not vid: return self._send(400, {"error": "Kein Video ausgewählt"})
            ensure_video(cid, vid)
            # Clear old generated files when loading new script
            out = v_out(cid, vid)
            for f in os.listdir(out):
                if f.endswith((".jpg", ".png", ".mp4")):
                    try:
                        os.remove(os.path.join(out, f))
                        print(f"  [Plan] Gelösche alte Datei: {f}", flush=True)
                    except: pass
            scenes = segment(text, wpm, sec)
            analysis = analyze_script([s["text"] for s in scenes])
            prompts  = visual_prompts(scenes, read_master(cid), analysis)
            for s, pr in zip(scenes, prompts):
                s["prompt"] = pr; s["file"] = None
                s["status"] = "geplant"; s["t"] = fmt_t(s["start"])
            # Always generate video prompts so both modes are ready
            try: vid_master = open(ch_vid_master(cid)).read().strip()
            except: vid_master = VIDEO_MASTER_DEFAULT
            has_ref = os.path.exists(os.path.join(ch_dir(cid), "char_ref_url.txt"))
            vid_prompts = video_prompts_batch(scenes, vid_master, has_char_ref=has_ref)
            for s, vp in zip(scenes, vid_prompts):
                s["video_prompt"] = vp
                s["phase"] = story_phase(s["i"], len(scenes))
            out = {"scenes": scenes, "wpm": wpm, "sec": sec, "characters": analysis.get("characters", [])}
            json.dump(out, open(v_plan(cid, vid), "w"), ensure_ascii=False, indent=1)
            return self._send(200, out)

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
            print(f"  [Audio] {os.path.basename(local_path)} ({len(raw)//1024} KB)", flush=True)
            return self._send(200, {"ok": True, "size": len(raw), "name": d.get("name", "")})

        if p == "/api/transcribe_status":
            return self._send(200, dict(TX_STATUS))

        # ── Transcribe ────────────────────────────────────────────────────────
        if p == "/api/transcribe":
            sec = float(d.get("sec", 4))
            if not vid: return self._send(400, {"error": "Kein Video ausgewählt"})
            try:    meta = json.load(open(v_audio(cid, vid)))
            except: return self._send(400, {"error": "Keine Audio-Datei hochgeladen."})
            TX_STATUS["running"] = True; TX_STATUS["error"] = ""
            # Clear old generated files when transcribing new audio
            out = v_out(cid, vid)
            for f in os.listdir(out):
                if f.endswith((".jpg", ".png", ".mp4")):
                    try:
                        os.remove(os.path.join(out, f))
                        print(f"  [Transcribe] Gelösche alte Datei: {f}", flush=True)
                    except: pass
            try:
                mb = os.path.getsize(meta["path"]) / 1024 / 1024
                tx(1, f"Sende Audio an KIE ({mb:.1f} MB) …")
                beats = transcribe_and_segment(meta["path"], meta["mime"], sec)
                tx(2, f"{len(beats)} Szenen transkribiert — baue Szenen …")
            except Exception as e:
                import traceback; traceback.print_exc()
                TX_STATUS["running"] = False; TX_STATUS["error"] = str(e)
                return self._send(500, {"error": f"Transkription fehlgeschlagen: {e}"})
            scenes = []
            for i, b in enumerate(beats):
                dur = (beats[i+1]["start"] - b["start"]) if i+1 < len(beats) else sec
                scenes.append({"i": i, "start": round(float(b["start"]), 1), "dur": round(float(dur), 1),
                               "text": b["text"], "t": fmt_t(float(b["start"])),
                               "file": None, "status": "geplant", "prompt": ""})
            tx(3, f"Analysiere Story-Struktur ({len(scenes)} Szenen) …")
            analysis = analyze_script([s["text"] for s in scenes])
            tx(4, "Schreibe Bild-Prompts …")
            prompts = visual_prompts(scenes, read_master(cid), analysis)
            for s, pr in zip(scenes, prompts): s["prompt"] = pr
            tx(4, f"Schreibe Video-Prompts für {len(scenes)} Szenen …")
            try: vid_master = open(ch_vid_master(cid)).read().strip()
            except: vid_master = VIDEO_MASTER_DEFAULT
            has_ref = os.path.exists(os.path.join(ch_dir(cid), "char_ref_url.txt"))
            vid_prompts = video_prompts_batch(scenes, vid_master, has_char_ref=has_ref)
            for s, vp in zip(scenes, vid_prompts):
                s["video_prompt"] = vp
                s["phase"] = story_phase(s["i"], len(scenes))
            tx(4, f"Fertig — {len(scenes)} Szenen bereit ✓")
            TX_STATUS["running"] = False
            out = {"scenes": scenes, "sec": sec, "source": "audio", "characters": analysis.get("characters", [])}
            json.dump(out, open(v_plan(cid, vid), "w"), ensure_ascii=False, indent=1)
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
        if p == "/api/generate_one":
            i = int(d["i"]); prompt = d.get("prompt", "")
            fn = f"{i:03d}.jpg"
            out_path = os.path.join(v_out(cid, vid), fn)
            full_prompt = _build_image_prompt(prompt, read_master(cid), load_char_refs(cid))
            try:
                task_id = _kie_submit_image(full_prompt)
            except Exception as e:
                return self._send(500, {"error": str(e)})
            job_id = f"{cid}_{vid}_{i}_{int(time.time())}"
            JOBS[job_id] = {"status": "running", "progress": 0, "file": None,
                            "source_url": None, "ts": None, "error": None}
            # Mark scene as running in plan
            try:
                plan = json.load(open(v_plan(cid, vid)))
                for s in plan["scenes"]:
                    if s["i"] == i:
                        s["prompt"] = prompt; s["status"] = "läuft"
                json.dump(plan, open(v_plan(cid, vid), "w"), ensure_ascii=False, indent=1)
            except: pass
            threading.Thread(
                target=_image_job_worker,
                args=(job_id, task_id, out_path, v_plan(cid, vid), i),
                daemon=True
            ).start()
            return self._send(200, {"ok": True, "job_id": job_id})

        # ── Generate video from image ─────────────────────────────────────────
        if p == "/api/generate_video":
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
            # Update plan
            try:
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
            # Build a neutral standing-pose prompt
            char_prompt = (
                f"{master}\n"
                "Full body, neutral standing pose, facing forward, arms at sides. "
                "2D flat line art, thin black strokes, pure white background, no shading."
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
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"Dashboard läuft: http://localhost:{port}  (Strg+C zum Beenden)")
    srv.serve_forever()

if __name__ == "__main__":
    main()
