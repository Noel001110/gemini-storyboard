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
os.makedirs(OUT_DIR, exist_ok=True)

IMG_MODEL = "gemini-3-pro-image"
TEXT_MODEL = "gemini-2.5-flash"
API = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"

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
        "- locations: array of {name: string, beats: [indices]} — distinct settings in order of first appearance\n"
        "- characters: array of string — recurring named people\n"
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

def visual_prompts(scenes, master, accent):
    beats = [s["text"] for s in scenes]

    # Stage 1 — story structure analysis
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
        "- Describe: subject + action + composition + emotion + flat colors to use.\n"
        "- Primary accent color is " + accent + "; you may suggest 1–2 additional flat colors per scene.\n"
        "- SENSITIVE content (violence / death / abuse / trafficking / child suffering): tasteful symbolism only — never graphic.\n"
        "- Do NOT describe art style (that is in the master prompt). Only describe scene content.\n\n"
        "ART STYLE context (stick-figure ASDF Movie style — for reference only):\n" + master[:500] + "\n\n"
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

# ---------- Bildgenerierung ----------
def gen_image(scene_prompt, master, out_path, size, accent="#2563EB"):
    acc = (f"\n\nFLAT COLOR PALETTE: primary accent is {accent}. You may use up to 2 additional flat colors "
           "if the scene calls for it. All colors must be flat fills — no gradients, no shading. "
           "Everything else is pure black (#000000) on clean white (#FFFFFF).")
    body = {
        "contents": [{"parts": [{"text": scene_prompt + acc + "\n\n" + master}]}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": "16:9", "imageSize": size},
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
            accent = d.get("accent", "#2563EB")
            text = clean_script(d.get("script", ""))
            if not text:
                return self._send(200, {"scenes": []})
            scenes = segment(text, wpm, sec)
            prompts = visual_prompts(scenes, read_master(), accent)
            for s, pr in zip(scenes, prompts):
                s["prompt"] = pr
                s["file"] = None
                s["status"] = "geplant"
                s["t"] = fmt_t(s["start"])
            out = {"scenes": scenes, "wpm": wpm, "sec": sec}
            json.dump(out, open(PLAN_FILE, "w"), ensure_ascii=False, indent=1)
            return self._send(200, out)
        if p == "/api/generate_one":
            i = int(d["i"]); prompt = d.get("prompt", "")
            size = d.get("size", "2K"); accent = d.get("accent", "#2563EB")
            fn = f"{i:03d}.png"
            res = gen_image(prompt, read_master(), os.path.join(OUT_DIR, fn), size, accent)
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
