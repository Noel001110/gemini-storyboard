#!/usr/bin/env python3
"""Gemini image generator for the Yeonmi storyboard.
Usage: python3 gen.py <out.png> "<scene prompt>"
Sends MASTER STYLE BLOCK + scene, with anchor.png as a style reference,
to gemini-3-pro-image at 16:9.
"""
import sys, os, json, base64, urllib.request

KEY = open(os.path.expanduser("~/.gemini_key")).read().strip()
MODEL = os.environ.get("GEN_MODEL", "gemini-3-pro-image")
HERE = os.path.dirname(os.path.abspath(__file__))
ANCHOR = os.path.join(HERE, "anchor.png") if os.environ.get("USE_REF") else ""

MASTER = (
 "STYLE (identical for every frame in this series — drawn by one and the same illustrator, same hand, "
 "same tools, same day): a professional hand-drawn black-ink CONCEPT STORYBOARD SKETCH. "
 "TECHNIQUE: confident continuous-line / loose-scribble ink drawing — crisp, sharp, high-contrast "
 "black strokes with clean edges; energetic and gestural but always readable. Shading ONLY as sparse, "
 "loose, hand-drawn parallel hatching where truly needed — never solid black fills, never gradients, "
 "never soft pencil smudging. "
 "BACKGROUND: a single solid, perfectly clean pure-white field (#FFFFFF). The empty space is true blank "
 "white — no paper texture, no canvas, no grain, no noise, no dust, no smudges, no gray washes, no "
 "atmospheric haze, no gradient, no vignette, nothing rendered in the negative space. "
 "FIGURES: humans drawn with loose but correct proportions (NEVER stick figures, NEVER chibi), faces "
 "with expressive but minimal features, natural gesture-drawing body language. "
 "COLOR: strictly black ink on white PLUS at most ONE single flat accent color per frame — warm red "
 "(#E4322B) by default, or teal (#2BB6C4) only for water, ice or cold — used as one small pointed "
 "highlight, never filling areas. "
 "COMPOSITION: wide 16:9, ONE clear focal idea per frame, lots of generous empty white negative space "
 "around the subject, subject centered and fully inside the frame, nothing important cropped. Any "
 "hand-lettered text must be short, neat and correctly spelled. "
 "RECURRING CHARACTER Yeonmi (must look identical in every frame she appears): a slim 13-year-old "
 "North Korean girl, shoulder-length black hair in a low ponytail with a few loose strands, round soft "
 "face, large expressive eyes, a thin worn coat over simple trousers, slightly hunched cold posture. "
 "DO NOT: no whiteboard, no frame or border, no paper/canvas/photographed surface, no gray or colored "
 "background, no blur, no soft focus, no 3D, no cinematic lighting, no photorealism, no anime, no manga, "
 "no Disney, no cartoon rendering, no thick uniform outlines, no sterile corporate vector look, no busy "
 "or detailed background scenery, no extra limbs, no broken anatomy, no garbled text, no watermark, no signature."
)

def b64(path):
    return base64.b64encode(open(path, "rb").read()).decode()

def main():
    out, scene = sys.argv[1], sys.argv[2]
    parts = [{"text": scene + " " + MASTER}]
    if os.path.exists(ANCHOR):
        parts.append({"inline_data": {"mime_type": "image/png", "data": b64(ANCHOR)}})
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": "16:9"},
        },
    }
    size = os.environ.get("IMG_SIZE")  # z.B. 4K / 2K / 1K (nur gemini-3-pro-image)
    if size:
        body["generationConfig"]["imageConfig"]["imageSize"] = size
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    import time
    resp = None
    for attempt in range(6):
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"x-goog-api-key": KEY, "Content-Type": "application/json"},
        )
        try:
            resp = json.load(urllib.request.urlopen(req, timeout=180))
            break
        except urllib.error.HTTPError as e:
            code = e.code
            if code in (429, 500, 503):
                wait = min(60, 5 * (2 ** attempt))
                print(f"HTTP {code}, retry in {wait}s (attempt {attempt+1}/6)")
                time.sleep(wait); continue
            print("HTTP", code, e.read().decode()[:500]); sys.exit(1)
    if resp is None:
        print("RATE LIMIT — aufgegeben nach 6 Versuchen"); sys.exit(3)
    for p in resp.get("candidates", [{}])[0].get("content", {}).get("parts", []):
        d = p.get("inline_data") or p.get("inlineData")
        if d:
            open(out, "wb").write(base64.b64decode(d["data"]))
            print("OK", out, os.path.getsize(out) // 1024, "KB"); return
    print("KEIN BILD. Antwort:", json.dumps(resp)[:600]); sys.exit(2)

if __name__ == "__main__":
    main()
