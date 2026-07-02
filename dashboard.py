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

# ── Per-channel path helpers ──────────────────────────────────────────────────
def ch_dir(cid):        return os.path.join(CHANNELS_DIR, cid)
def ch_master(cid):     return os.path.join(ch_dir(cid), "master_prompt.txt")
def ch_vid_master(cid): return os.path.join(ch_dir(cid), "video_master_prompt.txt")
def ch_out(cid):        return os.path.join(ch_dir(cid), "generated")
def ch_plan(cid):       return os.path.join(ch_out(cid), "plan.json")
def ch_sheets(cid):     return os.path.join(ch_dir(cid), "charsheets")
def ch_uploads(cid):    return os.path.join(ch_dir(cid), "uploads")
def ch_audio(cid):      return os.path.join(ch_uploads(cid), "audio_meta.json")
def ch_mode(cid):       return os.path.join(ch_dir(cid), "mode.txt")  # "image" | "video"

def get_mode(cid) -> str:
    try: return open(ch_mode(cid)).read().strip()
    except: return "image"

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

def ensure_channel(cid):
    for d in [ch_out(cid), ch_sheets(cid), ch_uploads(cid)]:
        os.makedirs(d, exist_ok=True)

# ── Channel list ──────────────────────────────────────────────────────────────
def load_channels():
    try:    return json.load(open(CHANNELS_FILE))
    except: return [{"id": "default", "name": "Kanal 1"}]

def save_channels(chs):
    os.makedirs(CHANNELS_DIR, exist_ok=True)
    json.dump(chs, open(CHANNELS_FILE, "w"), ensure_ascii=False, indent=1)

# ── First-run migration: move flat files → channels/default/ ─────────────────
def init_channels():
    os.makedirs(CHANNELS_DIR, exist_ok=True)
    if not os.path.exists(CHANNELS_FILE):
        save_channels([{"id": "default", "name": "Kanal 1"}])
        ensure_channel("default")
        # copy old master_prompt.txt
        old_master = os.path.join(HERE, "master_prompt.txt")
        if os.path.exists(old_master) and not os.path.exists(ch_master("default")):
            shutil.copy2(old_master, ch_master("default"))
        # copy old generated/
        old_gen = os.path.join(HERE, "generated")
        if os.path.exists(old_gen):
            for f in os.listdir(old_gen):
                dst = os.path.join(ch_out("default"), f)
                if not os.path.exists(dst):
                    try: shutil.copy2(os.path.join(old_gen, f), dst)
                    except: pass
        # copy old charsheets/
        old_cs = os.path.join(HERE, "charsheets")
        if os.path.exists(old_cs):
            for f in os.listdir(old_cs):
                dst = os.path.join(ch_sheets("default"), f)
                if not os.path.exists(dst):
                    try: shutil.copy2(os.path.join(old_cs, f), dst)
                    except: pass
    else:
        for ch in load_channels():
            ensure_channel(ch["id"])

init_channels()

# KIE.ai — image generation
KIE_API      = "https://api.kie.ai/api/v1/jobs"
KIE_MODEL    = "nano-banana-2"
# KIE.ai — text + audio (OpenAI-compatible)
KIE_CHAT_URL = "https://api.kie.ai/gemini-2.5-flash/v1/chat/completions"

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
    """Stage 1 — map recurring locations, characters, props and callbacks across the whole script."""
    instr = (
        "Analyze this narration script (JSON array of text beats, 0-indexed). Return a JSON object with:\n"
        "- locations: array of {name: string, beats: [indices], props: [1-3 minimal background props as short strings]} "
        "— distinct settings in order of first appearance\n"
        "- characters: array of {name: string, description: string, beats: [indices]} "
        "— recurring named people with a brief visual description (clothing, hair, posture)\n"
        "- props: array of string — important recurring objects or symbols\n"
        "- arc: string — 3-word emotional arc (e.g. 'despair → hope → triumph')\n"
        "- callbacks: array of {at: index, references: index} — beats that visually reference an earlier beat\n\n"
        "BEATS:\n" + json.dumps(beats, ensure_ascii=False)
    )
    try:
        txt = post_kie_text([{"role": "user", "content": instr}], json_mode=True, temp=0.2)
        return json.loads(txt)
    except Exception as e:
        print("Analyse-Fehler:", e)
        return {}

def visual_prompts(scenes, master, analysis=None):
    beats = [s["text"] for s in scenes]

    # Stage 1 — story structure analysis (skip if already provided)
    if analysis is None:
        print(f"  [Plan] Analysiere {len(beats)} Beats …", flush=True)
        analysis = analyze_script(beats)
    analysis_ctx = json.dumps(analysis, ensure_ascii=False, indent=1) if analysis else ""

    # Stage 2 — generate per-beat image prompts with full continuity context
    print(f"  [Plan] Generiere Bild-Prompts …", flush=True)
    instr = (
        "You are a storyboard director for an ASDF Movie-style stick-figure animation.\n"
        "Turn this narration into ONE visual scene description per beat.\n\n"
    )
    if analysis_ctx:
        arc = analysis.get("arc", "")
        instr += (
            "STORY STRUCTURE — use this for visual continuity:\n" + analysis_ctx + "\n\n"
            "CONTINUITY RULES (strictly enforced):\n"
            "1. Beats sharing the same LOCATION: keep background props consistent (same horizon line, same door, same props).\n"
            "2. Character 'Yeonmi' looks identical every frame: circle head, bob-haircut side lines, trapezoid skirt.\n"
            "3. CALLBACK beats (see callbacks above): echo the earlier scene's framing or prop explicitly.\n"
            "4. Emotional arc is '" + arc + "' — let the color mood shift accordingly across the sequence.\n\n"
        )
    instr += (
        "PER-SCENE RULES:\n"
        "- Max ~40 words per description.\n"
        "- Describe: subject + action + composition + emotion.\n"
        "- COLOR: choose flat colors that feel semantically right for objects/props in this scene "
        "(e.g. grass=green, car=red, fire=orange). Use 2–4 flat colors max. "
        "Stick figure interiors stay white. Background stays white. "
        "Keep the same color for the same object across all scenes.\n"
        "- SENSITIVE content (violence / death / abuse / trafficking / child suffering): tasteful symbolism only — never graphic.\n"
        "- Do NOT describe art style (that is in the master prompt). Only describe scene content.\n\n"
        "CONTINUITY MARKERS (mandatory in your output):\n"
        "- First beat in a new location → begin prompt with: [NEW SCENE: location name | prop1, prop2, prop3]\n"
        "  Example: [NEW SCENE: kitchen | horizontal floor line, small square window upper-right]\n"
        "- Subsequent beats in the SAME location → begin prompt with: [SAME SCENE: location name | prop1, prop2]\n"
        "  This tells the image model to keep those exact background elements.\n"
        "- If a beat directly continues an action from the previous beat → append at end: [CONT ACTION]\n"
        "These markers are part of the prompt text and will be sent to the image model.\n\n"
        "ART STYLE context (stick-figure ASDF Movie style — follow exactly, no deviations):\n" + master + "\n\n"
        "BEATS:\n" + json.dumps(beats, ensure_ascii=False) + "\n\n"
        "Return a JSON array of strings, one per beat, same order and same length as BEATS."
    )
    try:
        txt = post_kie_text([{"role": "user", "content": instr}], json_mode=True, temp=0.7)
        arr = json.loads(txt)
        if isinstance(arr, list) and len(arr) == len(beats):
            return [str(x) for x in arr]
        # unwrap if nested (some models return {"prompts": [...]})
        for v in arr.values() if isinstance(arr, dict) else []:
            if isinstance(v, list) and len(v) == len(beats):
                return [str(x) for x in v]
    except Exception as e:
        print("Planner-Fehler:", e)
    return beats  # Fallback

def story_phase(i: int, total: int) -> str:
    return (
        "OPENING"        if i < total * 0.15 else
        "RISING ACTION"  if i < total * 0.50 else
        "CLIMAX"         if i < total * 0.75 else
        "RESOLUTION"
    )

def video_prompts_batch(scenes: list, vid_master: str, has_char_ref: bool = False) -> list:
    """Single LLM call to generate all T2V video prompts with full story context.

    Returns list of prompt strings, one per scene, same order as scenes.
    """
    beats = [s["text"] for s in scenes]
    total = len(beats)
    phase_label = lambda i: story_phase(i, total)
    numbered_beats = "\n".join(
        f"{i+1}. [{phase_label(i)}] {t}" for i, t in enumerate(beats)
    )

    ref_note = (
        "A character reference image will be supplied to the video model — "
        "focus prompts on ACTION and MOVEMENT, not appearance."
        if has_char_ref else
        "No reference image — describe character appearance briefly in each prompt as defined in CHARACTER CONTEXT."
    )

    instr = f"""\
You are a video scene director for a documentary/factual video. Read the ENTIRE script below
first and identify what it is actually ABOUT — the real people, organizations, places,
technologies, and events it names — before writing a single prompt.

STEP 1 — UNDERSTAND THE SUBJECT (do this silently before writing prompts):
Read all narrator lines below as one continuous text. Identify:
- the central subject/topic (what is this documentary actually about?)
- recurring concrete entities: named people, organizations, places, objects, technologies
- which entity is being referenced at each point in the timeline

STEP 2 — GROUND EVERY PROMPT IN THE ACTUAL SUBJECT, NOT A GENERIC INTERPRETATION:
This is the most important rule. Do NOT default to abstract metaphor (a figure stumbling,
spinning, pointing at a box) when the line names something concrete. If a line mentions a
specific person, place, organization, or technology, show THAT thing — or a clear visual
stand-in for it (e.g. a phone screen with a suspicious message, a world map with marked
locations, a building exterior, a document/code on a screen, a person matching a described
role at a desk). Only fall back to abstract gesture/symbol when a line is pure commentary
with no concrete referent (e.g. "That sounds simple. Even convincing.").

STEP 3 — VISUAL CONSISTENCY FOR RECURRING ENTITIES:
Once you depict an entity (a place, an organization, an object), reuse the SAME visual
representation every time that entity reappears later in the script. This builds a visual
language the viewer learns to recognize, instead of a new random image each time.

CHARACTER & STYLE CONTEXT (visual style only — follow exactly, do not deviate):
{vid_master.strip()[:3000]}

REFERENCE IMAGE: {ref_note}

RULES FOR EACH PROMPT:
1. SUBJECT FIRST — what concrete thing from STEP 1/2 is this line actually about? Show it.
2. ACTION/CAMERA MOVEMENT — what happens or moves in the frame. No speaking, no lip sync.
3. CAMERA — one deliberate move: zoom-in=revelation | zoom-out=scale | pan-right=progress | pan-left=past | static=tension
4. CONTINUITY — reuse visual anchors per STEP 3 when the same entity recurs.
5. STYLE — apply only what's defined in CHARACTER & STYLE CONTEXT above.
6. LENGTH — 55–80 words per prompt. Dense, specific. No preamble, no meta-commentary.

FULL NARRATOR SCRIPT (read completely before writing any prompt):
{numbered_beats}

Return a JSON array of {total} strings — one prompt per line, same order as the script above.
"""
    print(f"  [VidPrompt] Batch-Generierung: {total} Szenen in einem Call …", flush=True)
    try:
        txt = post_kie_text([{"role": "user", "content": instr}], json_mode=True, temp=0.45)
        arr = json.loads(txt)
        # Handle direct list or wrapped {"prompts": [...]} etc.
        if isinstance(arr, list) and len(arr) == total:
            return [str(x) for x in arr]
        for v in (arr.values() if isinstance(arr, dict) else []):
            if isinstance(v, list) and len(v) == total:
                return [str(x) for x in v]
        print("  [VidPrompt] Unerwartetes Format, nutze Fallback", flush=True)
    except Exception as e:
        print(f"  [VidPrompt] Batch-Fehler: {e}", flush=True)
    # Fallback: simple per-scene description from the text (style is appended at
    # submission time regardless, see _build_video_prompt)
    return [
        f"Scene illustrating: {s['text'][:80]}. "
        "Character center frame, deliberate gesture matching the scene energy. Camera slow zoom-in."
        for s in scenes
    ]

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


def gen_charsheet(name, description):
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
    tmp = os.path.join(ch_sheets("default"), f"_tmp_{re.sub(r'[^\\w]','_',name)}.jpg")
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
                    out_path: str, plan_path: str, cid: str, video_prompt: str, chain_len: int = 0):
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
    fn_silent = os.path.join(ch_out(cid), f"{i:03d}_veo_silent.mp4")
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
        audio_meta = json.load(open(ch_audio(cid)))
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
        "default to an abstract gesture unless the line truly has no concrete referent."
    )
    try:
        result = post_kie_text([
            {"role": "system", "content": T2V_PROMPT_SYSTEM},
            {"role": "user",   "content": user_msg},
        ], temp=0.40)
        return result.strip()
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
        if p == "/":
            return self._send(200, open(os.path.join(HERE, "dashboard.html"), encoding="utf-8").read(), "text/html; charset=utf-8")
        if p == "/api/channels":
            return self._send(200, {"channels": load_channels()})
        if p == "/api/char_ref":
            ref_path = os.path.join(ch_dir(cid), "char_ref_url.txt")
            url = open(ref_path).read().strip() if os.path.exists(ref_path) else ""
            return self._send(200, {"url": url})
        if p == "/api/get_mode":
            return self._send(200, {"mode": get_mode(cid)})
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
            try:    return self._send(200, json.load(open(ch_plan(cid))))
            except: return self._send(200, {"scenes": []})
        if p == "/api/download":
            ts_map = {}
            try:
                plan = json.load(open(ch_plan(cid)))
                for s in plan.get("scenes", []):
                    t = s.get("t", "").replace(":", "-")
                    ts_map[f"{s['i']:03d}.jpg"] = f"{t}.jpg"
                    ts_map[f"{s['i']:03d}.png"] = f"{t}.png"
            except: pass
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                for f in sorted(os.listdir(ch_out(cid))):
                    if f.endswith(".png") or f.endswith(".jpg"):
                        z.write(os.path.join(ch_out(cid), f), ts_map.get(f, f))
            return self._send(200, buf.getvalue(), "application/zip")
        if p.startswith("/generated/"):
            fp = os.path.join(ch_out(cid), os.path.basename(p))
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
            # Clear old generated files when loading new script
            out = ch_out(cid)
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
            json.dump(out, open(ch_plan(cid), "w"), ensure_ascii=False, indent=1)
            return self._send(200, out)

        # ── Mode toggle ───────────────────────────────────────────────────────
        if p == "/api/set_mode":
            mode = d.get("mode", "image")
            if mode not in ("image", "video"): mode = "image"
            open(ch_mode(cid), "w").write(mode)
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
                plan = json.load(open(ch_plan(cid)))
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
                plan = json.load(open(ch_plan(cid)))
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
            job_id  = f"veo_{cid}_{i}_{int(time.time())}"
            out_path = os.path.join(ch_out(cid), f"{i:03d}.mp4")
            JOBS[job_id] = {"status": "running", "progress": 15, "file": None, "error": None}
            print(f"  [Veo] Task {task_id} → Job {job_id}", flush=True)

            t = threading.Thread(
                target=_veo_job_worker,
                args=(job_id, task_id, scene, out_path, ch_plan(cid), cid, video_prompt, chain_len),
                daemon=True
            )
            t.start()
            return self._send(200, {"ok": True, "job_id": job_id, "video_prompt": video_prompt,
                                     "chained": can_extend})

        # ── Audio upload ──────────────────────────────────────────────────────
        if p == "/api/upload_audio":
            try:    raw = base64.b64decode(d["data"])
            except: return self._send(400, {"error": "Ungültige Base64-Daten"})
            ext = (d.get("name", "audio.bin").rsplit(".", 1)[-1].lower()) or "bin"
            local_path = os.path.join(ch_uploads(cid), f"voiceover.{ext}")
            open(local_path, "wb").write(raw)
            json.dump({"path": local_path, "mime": d.get("mime", "audio/mpeg"), "name": d.get("name", "")},
                      open(ch_audio(cid), "w"))
            print(f"  [Audio] {os.path.basename(local_path)} ({len(raw)//1024} KB)", flush=True)
            return self._send(200, {"ok": True, "size": len(raw), "name": d.get("name", "")})

        if p == "/api/transcribe_status":
            return self._send(200, dict(TX_STATUS))

        # ── Transcribe ────────────────────────────────────────────────────────
        if p == "/api/transcribe":
            sec = float(d.get("sec", 4))
            try:    meta = json.load(open(ch_audio(cid)))
            except: return self._send(400, {"error": "Keine Audio-Datei hochgeladen."})
            TX_STATUS["running"] = True; TX_STATUS["error"] = ""
            # Clear old generated files when transcribing new audio
            out = ch_out(cid)
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
            json.dump(out, open(ch_plan(cid), "w"), ensure_ascii=False, indent=1)
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
                img_bytes = gen_charsheet(name, desc)
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
            out_path = os.path.join(ch_out(cid), fn)
            full_prompt = _build_image_prompt(prompt, read_master(cid), load_char_refs(cid))
            try:
                task_id = _kie_submit_image(full_prompt)
            except Exception as e:
                return self._send(500, {"error": str(e)})
            job_id = f"{cid}_{i}_{int(time.time())}"
            JOBS[job_id] = {"status": "running", "progress": 0, "file": None,
                            "source_url": None, "ts": None, "error": None}
            # Mark scene as running in plan
            try:
                plan = json.load(open(ch_plan(cid)))
                for s in plan["scenes"]:
                    if s["i"] == i:
                        s["prompt"] = prompt; s["status"] = "läuft"
                json.dump(plan, open(ch_plan(cid), "w"), ensure_ascii=False, indent=1)
            except: pass
            threading.Thread(
                target=_image_job_worker,
                args=(job_id, task_id, out_path, ch_plan(cid), i),
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
                plan = json.load(open(ch_plan(cid)))
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
                local_path = os.path.join(ch_out(cid), local_file)
                if not os.path.exists(local_path):
                    return self._send(400, {"error": f"Bild-Datei nicht gefunden: {local_file}"})
                print(f"  [Video] Lade Szenen-Bild hoch …", flush=True)
                try:
                    source_url = upload_image_public(local_path)
                    for s in plan["scenes"]:
                        if s["i"] == i:
                            s["source_url"] = source_url
                            s["source_url_ts"] = int(time.time())
                    json.dump(plan, open(ch_plan(cid), "w"), ensure_ascii=False, indent=1)
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
            silent_path = os.path.join(ch_out(cid), fn_silent)
            out_path    = os.path.join(ch_out(cid), fn)
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
                audio_meta = json.load(open(ch_audio(cid)))
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
                json.dump(plan, open(ch_plan(cid), "w"), ensure_ascii=False, indent=1)
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
