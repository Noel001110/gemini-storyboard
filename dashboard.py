#!/usr/bin/env python3
"""Localhost-Dashboard für die Storyboard-Bildgenerierung.
Nur Python-Standardlib. Start: python3 dashboard.py [--port 8000]
"""
import os, re, sys, json, time, base64, zipfile, io, threading
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
KEYFILE = os.path.expanduser("~/.gemini_key")
MASTER_FILE = os.path.join(HERE, "master_prompt.txt")
OUT_DIR = os.path.join(HERE, "generated")
PLAN_FILE = os.path.join(OUT_DIR, "plan.json")
UPLOAD_DIR    = os.path.join(HERE, "uploads")
AUDIO_META_FILE = os.path.join(UPLOAD_DIR, "audio_meta.json")
CHARSHEET_DIR = os.path.join(HERE, "charsheets")
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(CHARSHEET_DIR, exist_ok=True)

IMG_MODEL   = "gemini-3-pro-image"
TEXT_MODEL  = "gemini-2.5-flash"
AUDIO_MODEL = "gemini-2.5-flash"
API = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
FILES_UPLOAD = "https://generativelanguage.googleapis.com/upload/v1beta/files?uploadType=resumable"
FILES_API    = "https://generativelanguage.googleapis.com/v1beta/{name}"

# Shared transcription status (thread-safe via GIL for simple dict ops)
TX_STATUS = {"step": 0, "total": 4, "msg": "Bereit", "running": False, "error": ""}

def tx(step, msg):
    TX_STATUS["step"] = step
    TX_STATUS["msg"] = msg
    print(f"  [TX {step}/{TX_STATUS['total']}] {msg}", flush=True)

def key():
    return open(KEYFILE).read().strip()

def post_gemini(model, body):
    req = urllib.request.Request(
        API.format(model), data=json.dumps(body).encode(),
        headers={"x-goog-api-key": key(), "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=240))

# ---------- Master-Prompt ----------
def read_master():
    try:
        return open(MASTER_FILE, encoding="utf-8").read().strip()
    except FileNotFoundError:
        return ""

def write_master(txt):
    open(MASTER_FILE, "w", encoding="utf-8").write(txt.strip() + "\n")

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
    body = {
        "contents": [{"parts": [{"text": instr}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    }
    try:
        r = post_gemini(TEXT_MODEL, body)
        txt = r["candidates"][0]["content"]["parts"][0]["text"]
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
        "ART STYLE context (stick-figure ASDF Movie style — for reference only):\n" + master[:400] + "\n\n"
        "BEATS:\n" + json.dumps(beats, ensure_ascii=False) + "\n\n"
        "Return a JSON array of strings, one per beat, same order and same length as BEATS."
    )
    body = {
        "contents": [{"parts": [{"text": instr}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {"type": "ARRAY", "items": {"type": "STRING"}},
            "temperature": 0.7,
        },
    }
    try:
        r = post_gemini(TEXT_MODEL, body)
        txt = r["candidates"][0]["content"]["parts"][0]["text"]
        arr = json.loads(txt)
        if isinstance(arr, list) and len(arr) == len(beats):
            return [str(x) for x in arr]
    except Exception as e:
        print("Planner-Fehler:", e)
    return beats  # Fallback

# ---------- Character sheets ----------

def charsheet_path(name):
    safe = re.sub(r"[^\w\-]", "_", name.lower())
    return os.path.join(CHARSHEET_DIR, safe)

def load_char_refs():
    """Return list of {name, uri, mime} for all sheets that have a Gemini Files URI."""
    refs = []
    for f in os.listdir(CHARSHEET_DIR):
        if f.endswith(".json"):
            try:
                meta = json.load(open(os.path.join(CHARSHEET_DIR, f)))
                if meta.get("uri"):
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
    body = {
        "contents": [{"parts": [
            {"text": instr},
            {"inlineData": {"mimeType": mime, "data": base64.b64encode(img_bytes).decode()}}
        ]}],
        "generationConfig": {"temperature": 0.2},
    }
    r = post_gemini(TEXT_MODEL, body)
    return r["candidates"][0]["content"]["parts"][0]["text"].strip()


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
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": "16:9"},
        },
    }
    for attempt in range(4):
        try:
            r = post_gemini(IMG_MODEL, body)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503):
                time.sleep(min(30, 4 * 2 ** attempt)); continue
            raise
        cand = (r.get("candidates") or [{}])[0]
        for p in cand.get("content", {}).get("parts", []):
            d = p.get("inlineData") or p.get("inline_data")
            if d:
                return base64.b64decode(d["data"])
        time.sleep(3)
    raise RuntimeError("Character sheet generation failed")


# ---------- Bildgenerierung ----------
IMG_SIZE = "2K"

def gen_image(scene_prompt, master, out_path, char_refs=None):
    acc = (
        "\n\nCOLOR RULES: use flat colors on objects/props only (no gradients, no shading). "
        "Stick figure interiors = white. Background = white (#FFFFFF). "
        "Choose colors that are semantically fitting (grass=green, fire=orange, etc.). "
        "Max 3–4 flat colors per frame besides black and white."
    )
    parts = []
    if char_refs:
        for cr in char_refs:
            desc_hint = f"\n   Design spec: {cr['description']}" if cr.get("description") else ""
            parts.append({"text": (
                f"━━ CHARACTER DESIGN REFERENCE for '{cr['name']}' ━━\n"
                f"The image below is a DESIGN STYLE GUIDE — not a pose template.{desc_hint}\n\n"
                f"EXTRACT from this image: line thickness, head circle size, body proportions, "
                f"clothing outline style, eye dots, mouth curve style.\n"
                f"APPLY that design to draw '{cr['name']}' in whatever pose the scene below calls for.\n\n"
                f"STRICTLY FORBIDDEN: do NOT copy the sideways angle, the walking pose, "
                f"the specific leg/arm position, or any composition from this reference image. "
                f"The reference shows design only — every scene gets its own pose and framing.\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )})
            parts.append({"fileData": {"mimeType": cr.get("mime", "image/png"), "fileUri": cr["uri"]}})
    parts.append({"text": scene_prompt + acc + "\n\n" + master})
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": "16:9", "imageSize": IMG_SIZE},
        },
    }
    for attempt in range(5):
        try:
            r = post_gemini(IMG_MODEL, body)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503):
                time.sleep(min(40, 4 * 2 ** attempt)); continue
            return {"ok": False, "error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
        cand = (r.get("candidates") or [{}])[0]
        for p in cand.get("content", {}).get("parts", []):
            d = p.get("inlineData") or p.get("inline_data")
            if d:
                open(out_path, "wb").write(base64.b64decode(d["data"]))
                return {"ok": True}
        fr = cand.get("finishReason", "")
        if fr == "IMAGE_SAFETY":
            return {"ok": False, "error": "Safety-Filter blockiert — Prompt entschärfen"}
        if attempt < 4:
            time.sleep(3); continue
        return {"ok": False, "error": f"Kein Bild ({fr or 'leer'})"}
    return {"ok": False, "error": "Rate-Limit"}

# ---------- Audio → Gemini Files API ----------

def gemini_upload_file(local_path, mime_type):
    """Upload a file to the Gemini Files API using the resumable protocol. Returns the file URI."""
    file_size = os.path.getsize(local_path)
    display_name = os.path.basename(local_path)
    meta_body = json.dumps({"file": {"display_name": display_name}}).encode()

    # 1. Start upload session
    req = urllib.request.Request(FILES_UPLOAD, data=meta_body, method="POST")
    req.add_header("x-goog-api-key", key())
    req.add_header("X-Goog-Upload-Protocol", "resumable")
    req.add_header("X-Goog-Upload-Command", "start")
    req.add_header("X-Goog-Upload-Header-Content-Length", str(file_size))
    req.add_header("X-Goog-Upload-Header-Content-Type", mime_type)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        upload_url = resp.getheader("X-Goog-Upload-URL")

    # 2. Upload bytes
    with open(local_path, "rb") as f:
        data = f.read()
    upload_req = urllib.request.Request(upload_url, data=data, method="POST")
    upload_req.add_header("Content-Length", str(file_size))
    upload_req.add_header("X-Goog-Upload-Offset", "0")
    upload_req.add_header("X-Goog-Upload-Command", "upload, finalize")
    with urllib.request.urlopen(upload_req, timeout=180) as resp:
        result = json.load(resp)

    file_name = result["file"]["name"]
    file_uri  = result["file"]["uri"]

    # 3. Wait for ACTIVE state
    status_url = FILES_API.format(name=file_name)
    for _ in range(20):
        req2 = urllib.request.Request(status_url)
        req2.add_header("x-goog-api-key", key())
        with urllib.request.urlopen(req2, timeout=10) as r2:
            info = json.load(r2)
        if info.get("state") == "ACTIVE":
            return file_uri
        time.sleep(3)
    return file_uri  # proceed anyway


def transcribe_and_segment(local_path, mime_type, sec_per_img):
    """Transcribe audio inline (base64) and split into timed beats — no Files API needed."""
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
    )
    body = {
        "contents": [{
            "parts": [
                {"text": instr},
                {"inlineData": {"mimeType": mime_type, "data": audio_b64}}
            ]
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "start": {"type": "NUMBER"},
                        "text": {"type": "STRING"}
                    },
                    "required": ["start", "text"]
                }
            },
            "temperature": 0.1,
        }
    }
    r = post_gemini(AUDIO_MODEL, body)
    txt = r["candidates"][0]["content"]["parts"][0]["text"]
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
        p = self.path.split("?")[0]
        if p == "/":
            return self._send(200, open(os.path.join(HERE, "dashboard.html"), encoding="utf-8").read(), "text/html; charset=utf-8")
        if p == "/api/transcribe_status":
            return self._send(200, dict(TX_STATUS))
        if p == "/api/master":
            return self._send(200, {"master": read_master()})
        if p == "/api/plan":
            try:
                return self._send(200, json.load(open(PLAN_FILE)))
            except Exception:
                return self._send(200, {"scenes": []})
        if p == "/api/download":
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                for f in sorted(os.listdir(OUT_DIR)):
                    if f.endswith(".png"):
                        z.write(os.path.join(OUT_DIR, f), f)
            return self._send(200, buf.getvalue(), "application/zip")
        if p.startswith("/generated/"):
            fp = os.path.join(OUT_DIR, os.path.basename(p))
            if os.path.exists(fp):
                b = open(fp, "rb").read()
                ct = "image/jpeg" if b[:2] == b"\xff\xd8" else "image/png"
                return self._send(200, b, ct)
            return self._send(404, {"error": "not found"})
        if p == "/api/charsheets":
            sheets = []
            for f in sorted(os.listdir(CHARSHEET_DIR)):
                if f.endswith(".json"):
                    try:
                        meta = json.load(open(os.path.join(CHARSHEET_DIR, f)))
                        img_path = os.path.join(CHARSHEET_DIR, f.replace(".json", ".png"))
                        meta["has_image"] = os.path.exists(img_path)
                        sheets.append(meta)
                    except Exception:
                        pass
            return self._send(200, {"sheets": sheets})
        if p.startswith("/charsheets/"):
            fp = os.path.join(CHARSHEET_DIR, os.path.basename(p))
            if os.path.exists(fp):
                b = open(fp, "rb").read()
                ct = "image/jpeg" if b[:2] == b"\xff\xd8" else "image/png"
                return self._send(200, b, ct)
            return self._send(404, {"error": "not found"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0]
        try:
            d = self._read()
        except Exception:
            return self._send(400, {"error": "bad json"})
        if p == "/api/master":
            write_master(d.get("master", ""))
            return self._send(200, {"ok": True})
        if p == "/api/plan":
            wpm = float(d.get("wpm", 150)); sec = float(d.get("sec", 4))
            text = clean_script(d.get("script", ""))
            if not text:
                return self._send(200, {"scenes": []})
            scenes = segment(text, wpm, sec)
            beats = [s["text"] for s in scenes]
            analysis = analyze_script(beats)
            prompts = visual_prompts(scenes, read_master(), analysis)
            for s, pr in zip(scenes, prompts):
                s["prompt"] = pr
                s["file"] = None
                s["status"] = "geplant"
                s["t"] = fmt_t(s["start"])
            chars = analysis.get("characters", [])
            out = {"scenes": scenes, "wpm": wpm, "sec": sec, "characters": chars}
            json.dump(out, open(PLAN_FILE, "w"), ensure_ascii=False, indent=1)
            return self._send(200, out)
        if p == "/api/upload_audio":
            # {data: base64, mime: string, name: string}
            try:
                raw = base64.b64decode(d["data"])
            except Exception:
                return self._send(400, {"error": "Ungültige Base64-Daten"})
            ext = (d.get("name", "audio.bin").rsplit(".", 1)[-1].lower()) or "bin"
            local_path = os.path.join(UPLOAD_DIR, f"voiceover.{ext}")
            open(local_path, "wb").write(raw)
            json.dump({"path": local_path, "mime": d.get("mime", "audio/mpeg"), "name": d.get("name", "")},
                      open(AUDIO_META_FILE, "w"))
            print(f"  [Audio] Gespeichert: {os.path.basename(local_path)} ({len(raw)//1024} KB)", flush=True)
            return self._send(200, {"ok": True, "size": len(raw), "name": d.get("name", "")})

        if p == "/api/transcribe_status":
            return self._send(200, dict(TX_STATUS))

        if p == "/api/transcribe":
            sec = float(d.get("sec", 4))
            try:
                meta = json.load(open(AUDIO_META_FILE))
            except Exception:
                return self._send(400, {"error": "Keine Audio-Datei hochgeladen."})
            TX_STATUS["running"] = True; TX_STATUS["error"] = ""
            try:
                mb = os.path.getsize(meta["path"]) / 1024 / 1024
                tx(1, f"Sende Audio an Gemini ({mb:.1f} MB) …")
                beats = transcribe_and_segment(meta["path"], meta["mime"], sec)
                tx(2, f"{len(beats)} Szenen transkribiert — baue Szenen …")
            except Exception as e:
                import traceback; traceback.print_exc()
                TX_STATUS["running"] = False; TX_STATUS["error"] = str(e)
                return self._send(500, {"error": f"Transkription fehlgeschlagen: {e}"})
            scenes = []
            for i, b in enumerate(beats):
                dur = (beats[i+1]["start"] - b["start"]) if i+1 < len(beats) else sec
                scenes.append({
                    "i": i, "start": round(float(b["start"]), 1), "dur": round(float(dur), 1),
                    "text": b["text"], "t": fmt_t(float(b["start"])),
                    "file": None, "status": "geplant", "prompt": ""
                })
            tx(3, f"Analysiere Story-Struktur ({len(scenes)} Szenen) …")
            analysis = analyze_script([s["text"] for s in scenes])
            tx(4, f"Schreibe Bild-Prompts …")
            prompts = visual_prompts(scenes, read_master(), analysis)
            for s, pr in zip(scenes, prompts):
                s["prompt"] = pr
            tx(4, f"Fertig — {len(scenes)} Szenen bereit ✓")
            TX_STATUS["running"] = False
            chars = analysis.get("characters", [])
            out = {"scenes": scenes, "sec": sec, "source": "audio", "characters": chars}
            json.dump(out, open(PLAN_FILE, "w"), ensure_ascii=False, indent=1)
            return self._send(200, out)

        if p == "/api/upload_charref":
            # {name, image: base64, mime}
            name = d.get("name", "Charakter").strip()
            img_b64 = d.get("image", "")
            mime = d.get("mime", "image/png")
            if not img_b64:
                return self._send(400, {"error": "image fehlt"})
            img_bytes = base64.b64decode(img_b64)
            safe = re.sub(r"[^\w\-]", "_", name.lower())
            img_path  = os.path.join(CHARSHEET_DIR, f"{safe}.png")
            meta_path = os.path.join(CHARSHEET_DIR, f"{safe}.json")
            open(img_path, "wb").write(img_bytes)
            print(f"  [Char] Analysiere Referenzbild für '{name}' …", flush=True)
            try:
                desc = analyze_char_image(img_bytes, mime)
            except Exception as e:
                desc = ""
                print(f"  [Char] Analyse-Fehler (ignoriert): {e}", flush=True)
            print(f"  [Char] Upload zu Gemini Files …", flush=True)
            uri = gemini_upload_file(img_path, "image/png")
            meta = {"name": name, "description": desc, "safe": safe, "uri": uri, "mime": "image/png"}
            json.dump(meta, open(meta_path, "w"), ensure_ascii=False)
            print(f"  [Char] Fertig. Beschreibung: {desc[:80]}", flush=True)
            return self._send(200, {"ok": True, "name": name, "safe": safe, "uri": uri, "description": desc})

        if p == "/api/gen_charsheet":
            name = d.get("name", "").strip()
            desc = d.get("description", "").strip()
            if not name or not desc:
                return self._send(400, {"error": "name und description erforderlich"})
            safe = re.sub(r"[^\w\-]", "_", name.lower())
            img_path  = os.path.join(CHARSHEET_DIR, f"{safe}.png")
            meta_path = os.path.join(CHARSHEET_DIR, f"{safe}.json")
            try:
                print(f"  [Char] Generiere Character-Sheet für '{name}' …", flush=True)
                img_bytes = gen_charsheet(name, desc)
                open(img_path, "wb").write(img_bytes)
                print(f"  [Char] Lade hoch zu Gemini Files …", flush=True)
                uri = gemini_upload_file(img_path, "image/png")
                meta = {"name": name, "description": desc, "safe": safe, "uri": uri, "mime": "image/png"}
                json.dump(meta, open(meta_path, "w"), ensure_ascii=False)
                print(f"  [Char] Fertig: {uri}", flush=True)
                return self._send(200, {"ok": True, "name": name, "safe": safe, "uri": uri})
            except Exception as e:
                import traceback; traceback.print_exc()
                return self._send(500, {"error": str(e)})

        if p == "/api/generate_one":
            i = int(d["i"]); prompt = d.get("prompt", "")
            fn = f"{i:03d}.png"
            char_refs = load_char_refs()
            res = gen_image(prompt, read_master(), os.path.join(OUT_DIR, fn), char_refs)
            # plan.json aktualisieren
            try:
                plan = json.load(open(PLAN_FILE))
                for s in plan["scenes"]:
                    if s["i"] == i:
                        s["prompt"] = prompt
                        s["status"] = "fertig" if res["ok"] else "fehler"
                        s["file"] = fn if res["ok"] else None
                json.dump(plan, open(PLAN_FILE, "w"), ensure_ascii=False, indent=1)
            except Exception:
                pass
            if res["ok"]:
                return self._send(200, {"ok": True, "file": fn, "ts": int(time.time())})
            return self._send(200, {"ok": False, "error": res["error"]})
        return self._send(404, {"error": "not found"})

def main():
    port = 8000
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    if not os.path.exists(KEYFILE):
        print("WARN: ~/.gemini_key fehlt — Bildgenerierung wird scheitern.")
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"Dashboard läuft: http://localhost:{port}  (Strg+C zum Beenden)")
    srv.serve_forever()

if __name__ == "__main__":
    main()
