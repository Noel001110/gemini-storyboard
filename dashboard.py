#!/usr/bin/env python3
"""Localhost-Dashboard für die Storyboard-Bildgenerierung.
Nur Python-Standardlib. Start: python3 dashboard.py [--port 8000]
"""
import os, re, sys, json, time, base64, zipfile, io, threading
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
KIE_KEY_FILE = os.path.expanduser("~/.kie_key")
MASTER_FILE = os.path.join(HERE, "master_prompt.txt")
OUT_DIR = os.path.join(HERE, "generated")
PLAN_FILE = os.path.join(OUT_DIR, "plan.json")
UPLOAD_DIR    = os.path.join(HERE, "uploads")
AUDIO_META_FILE = os.path.join(UPLOAD_DIR, "audio_meta.json")
CHARSHEET_DIR = os.path.join(HERE, "charsheets")
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(CHARSHEET_DIR, exist_ok=True)

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
    hdrs = {"Authorization": f"Bearer {kie_key()}", "Content-Type": "application/json"}
    req = urllib.request.Request(KIE_CHAT_URL, data=json.dumps(body).encode(), headers=hdrs)
    with urllib.request.urlopen(req, timeout=240) as r:
        resp = json.load(r)
    return resp["choices"][0]["message"]["content"]

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
        "ART STYLE context (stick-figure ASDF Movie style — for reference only):\n" + master[:400] + "\n\n"
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
    tmp = os.path.join(CHARSHEET_DIR, f"_tmp_{re.sub(r'[^\\w]','_',name)}.jpg")
    res = gen_image(prompt, "", tmp)
    if res["ok"]:
        data = open(tmp, "rb").read()
        try: os.unlink(tmp)
        except: pass
        return data
    raise RuntimeError(f"Character sheet generation failed: {res.get('error')}")


# ---------- Bildgenerierung via KIE.ai ----------

def gen_image(scene_prompt, master, out_path, char_refs=None):
    char_hint = ""
    if char_refs:
        for cr in char_refs:
            desc = cr.get("description", "")
            name = cr.get("name", "Figur")
            if desc:
                char_hint += (
                    f"\n\nCHARACTER DESIGN for '{name}': {desc}"
                    f"\nApply this exact design in whatever pose this scene requires."
                )
    color_rules = (
        "\n\nCOLOR RULES: flat colors on objects/props only — no gradients, no shading. "
        "Stick figure interiors = white. Background = pure white (#FFFFFF). "
        "Semantic color choices (grass=green, fire=orange, water=blue, etc.). "
        "Max 3–4 flat colors per frame besides black and white."
    )
    full_prompt = scene_prompt + char_hint + color_rules + "\n\n" + master

    hdrs = {"Authorization": f"Bearer {kie_key()}", "Content-Type": "application/json"}
    body = {
        "model": KIE_MODEL,
        "input": {
            "prompt": full_prompt,
            "aspect_ratio": "16:9",
            "resolution": "2K",
            "output_format": "jpg",
        },
    }

    # 1. Task erstellen
    try:
        req = urllib.request.Request(f"{KIE_API}/createTask", data=json.dumps(body).encode(), headers=hdrs)
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"KIE HTTP {e.code}: {e.read().decode()[:200]}"}

    if resp.get("code") != 200:
        return {"ok": False, "error": f"KIE: {resp.get('msg', str(resp))}"}

    task_id = resp["data"]["taskId"]
    print(f"  [KIE] Task {task_id} gestartet …", flush=True)

    # 2. Pollen bis fertig (max ~4 min)
    poll_url = f"{KIE_API}/recordInfo?taskId={task_id}"
    poll_hdrs = {"Authorization": f"Bearer {kie_key()}"}
    for _ in range(80):
        time.sleep(3)
        try:
            with urllib.request.urlopen(urllib.request.Request(poll_url, headers=poll_hdrs), timeout=15) as r2:
                info = json.load(r2).get("data", {})
        except Exception as e:
            print(f"  [KIE] Poll-Fehler: {e}", flush=True); continue

        state = info.get("state", "")
        print(f"  [KIE] {state} ({info.get('progress', 0)}%)", flush=True)

        if state == "success":
            try:
                urls = json.loads(info.get("resultJson", "{}")).get("resultUrls", [])
            except Exception:
                urls = []
            if not urls:
                return {"ok": False, "error": "KIE: kein Bild in resultUrls"}
            with urllib.request.urlopen(urls[0], timeout=60) as img_r:
                open(out_path, "wb").write(img_r.read())
            return {"ok": True, "file": os.path.basename(out_path), "ts": int(time.time())}

        if state == "fail":
            return {"ok": False, "error": f"KIE fehlgeschlagen: {info.get('failMsg', 'unbekannt')}"}

    return {"ok": False, "error": "KIE Timeout (>4 min)"}



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
            # Build index→timestamp map from plan
            ts_map = {}
            try:
                plan = json.load(open(PLAN_FILE))
                for s in plan.get("scenes", []):
                    t = s.get("t", "").replace(":", "-")
                    ts_map[f"{s['i']:03d}.jpg"] = f"{t}.jpg"
                    ts_map[f"{s['i']:03d}.png"] = f"{t}.png"
            except Exception:
                pass
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                for f in sorted(os.listdir(OUT_DIR)):
                    if f.endswith(".png") or f.endswith(".jpg"):
                        arcname = ts_map.get(f, f)
                        z.write(os.path.join(OUT_DIR, f), arcname)
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
            meta = {"name": name, "description": desc, "safe": safe, "mime": "image/png"}
            json.dump(meta, open(meta_path, "w"), ensure_ascii=False)
            print(f"  [Char] Fertig. Beschreibung: {desc[:80]}", flush=True)
            return self._send(200, {"ok": True, "name": name, "safe": safe, "description": desc})

        if p == "/api/gen_charsheet":
            name = d.get("name", "").strip()
            desc = d.get("description", "").strip()
            if not name or not desc:
                return self._send(400, {"error": "name und description erforderlich"})
            safe = re.sub(r"[^\w\-]", "_", name.lower())
            img_path  = os.path.join(CHARSHEET_DIR, f"{safe}.png")
            meta_path = os.path.join(CHARSHEET_DIR, f"{safe}.json")
            try:
                print(f"  [Char] Generiere Character-Sheet für '{name}' via KIE …", flush=True)
                img_bytes = gen_charsheet(name, desc)
                open(img_path, "wb").write(img_bytes)
                meta = {"name": name, "description": desc, "safe": safe, "mime": "image/jpg"}
                json.dump(meta, open(meta_path, "w"), ensure_ascii=False)
                print(f"  [Char] Fertig.", flush=True)
                return self._send(200, {"ok": True, "name": name, "safe": safe})
            except Exception as e:
                import traceback; traceback.print_exc()
                return self._send(500, {"error": str(e)})

        if p == "/api/generate_one":
            i = int(d["i"]); prompt = d.get("prompt", "")
            fn = f"{i:03d}.jpg"
            char_refs = load_char_refs()
            res = gen_image(prompt, read_master(), os.path.join(OUT_DIR, fn), char_refs)
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
    if not os.path.exists(KIE_KEY_FILE):
        print("WARN: ~/.kie_key fehlt — alle KI-Funktionen werden scheitern.")
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"Dashboard läuft: http://localhost:{port}  (Strg+C zum Beenden)")
    srv.serve_forever()

if __name__ == "__main__":
    main()
