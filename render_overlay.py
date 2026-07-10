#!/usr/bin/env python3
"""Standalone helper: renders a text overlay (caption / callout / chapter title) as a
transparent PNG using Pillow. Runs inside .venv_whisper (an isolated venv shared with
whisper_transcribe.py) and is invoked via subprocess -- needed because the ffmpeg build
on this machine has no freetype/fontconfig compiled in, so its `drawtext` filter is
unavailable. The PNG this script produces gets composited onto a Ken-Burns clip with
ffmpeg's `overlay`/`fade` filters instead, both of which are in every standard build.

Phase E (Title-Cards): a separate CLI mode that renders a full-frame OPAQUE PNG (not a
transparent overlay) -- used as the input still for kind='title_card' scenes in the
renderer. Same venv / same subprocess pattern as overlays.

Usage (overlay modes):
  python3 render_overlay.py <out_path.png> <width> <height> <style> <text_b64>
    style:    "caption" | "callout" | "chapter"
    text_b64: base64-encoded UTF-8 text (sidesteps shell-escaping arbitrary punctuation)

Usage (title-card mode):
  python3 render_overlay.py <out_path.png> <width> <height> title_card <text_b64> [phase]
    phase: optional, one of "OPENING" | "RISING_ACTION" | "CLIMAX" | "RESOLUTION"
           (selects the underline accent color; "unknown"/missing = black)

Usage (word-caption batch mode, Cinematic-Mix Juli 2026, Schritt 3):
  python3 render_overlay.py <out_dir> <width> <height> word_caption_batch <words_json_b64>
    words_json_b64: base64-encoded JSON list of plain-text words (one PNG per list
                     entry, ONE subprocess call for the whole scene instead of one per
                     word -- same batching rationale as counter_anim below).
    Writes <out_dir>/word_0000.png ... word_{N-1:04d}.png.
"""
import sys
import os
import re
import json
import base64
from PIL import Image, ImageDraw, ImageFont, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
_CUSTOM_FONT_DIR = os.path.join(HERE, "assets", "fonts")

FONT_BOLD_CANDIDATES = []
if os.path.isdir(_CUSTOM_FONT_DIR):
    # Projekt-eigene Fonts (falls hinterlegt) haben Vorrang vor der System-Arial --
    # gilt für ALLE Overlay-Stile hier (caption/callout/chapter/counter/word_caption),
    # nicht nur die neuen Wort-Captions. Ein Font-Drop-in hebt die Optik überall.
    FONT_BOLD_CANDIDATES += sorted(
        os.path.join(_CUSTOM_FONT_DIR, fn) for fn in os.listdir(_CUSTOM_FONT_DIR)
        if fn.lower().endswith((".ttf", ".otf"))
    )
FONT_BOLD_CANDIDATES += [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]


def _font(size):
    for path in FONT_BOLD_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def _wrap(draw, text, font, max_width, max_lines):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if not cur or draw.textlength(trial, font=font) <= max_width:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip() + " …"
    return lines


def _draw_lines(draw, lines, font, width, top, line_h, fill, stroke_width, stroke_fill):
    y = top
    for line in lines:
        w = draw.textlength(line, font=font)
        draw.text(((width - w) / 2, y), line, font=font, fill=fill,
                   stroke_width=stroke_width, stroke_fill=stroke_fill)
        y += line_h


def render_caption(width, height, text):
    """Bottom-anchored subtitle: white bold text on a translucent box, wraps to fit
    within safe margins. Shown for the scene's full on-screen duration."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font_size = round(height * 0.042)
    font = _font(font_size)
    lines = _wrap(draw, text, font, width * 0.82, max_lines=3)
    line_h = font_size * 1.3
    block_h = line_h * len(lines) + font_size * 0.8
    box_top = height - block_h - height * 0.06
    draw.rectangle([0, box_top, width, height], fill=(0, 0, 0, 130))
    _draw_lines(draw, lines, font, width, box_top + font_size * 0.4, line_h,
                fill=(255, 255, 255, 255), stroke_width=max(1, font_size // 18),
                stroke_fill=(0, 0, 0, 200))
    return img


def render_callout(width, height, text):
    """Big, punchy upper-frame number/stat, no box -- meant to pop briefly (~1-1.5s)."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font_size = round(height * 0.11)
    font = _font(font_size)
    lines = _wrap(draw, text, font, width * 0.85, max_lines=2)
    line_h = font_size * 1.15
    _draw_lines(draw, lines, font, width, height * 0.14, line_h,
                fill=(255, 214, 64, 255), stroke_width=max(2, font_size // 14),
                stroke_fill=(0, 0, 0, 230))
    return img


def render_counter(width, height, text):
    """Phase F: even bigger, centered single number for 'this is the stat' punchy moments.
    No wrap (single-line numbers only — analysis enforces short callouts, max ~6 chars).
    White text with thick black stroke + red letter-fill for high contrast — meant to
    dominate the frame for ~1s (Phase-6 punchy cut)."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Even bigger font than callout — counter is THE focal moment
    font_size = round(height * 0.22)
    font = _font(font_size)
    # Single-line render — callout is already constrained to ~6 chars by analyze_script prompt.
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    cx = (width - text_w) / 2
    cy = (height - text_h) / 2
    # Red letter-fill with thicker black stroke — the red signals 'important number'.
    draw.text((cx, cy), text, font=font,
              fill=(220, 38, 38, 255),
              stroke_width=max(4, font_size // 12),
              stroke_fill=(0, 0, 0, 255))
    return img


# ── Cinematic-Mix Juli 2026: CapCut-Stil 1-Wort-Captions (Schritt 3) ──────────

_ACCENT_WORD_RE = re.compile(r"[\d$%]")  # Zahlen/$/% -> Akzentfarbe statt Weiß
WORD_CAPTION_ACCENT = (255, 214, 64, 255)  # #FFD640, gleiche Akzentfarbe wie render_callout
WORD_CAPTION_WHITE = (255, 255, 255, 255)


def render_word_caption(width, height, text):
    """Ein einzelnes Wort, groß, fett, ohne Box -- der CapCut/Hormozi-Stil. Zahlen/$/%
    bekommen die Akzentfarbe (Gelb), sonst Weiß. Kräftiger schwarzer Stroke + weicher
    (gaußscher) Schlagschatten für Lesbarkeit auf jedem Hintergrund, INSTANT (kein
    Fade -- das Timing/Pop-Gefühl entsteht rein aus dem harten Wort-Wechsel, siehe
    _render_word_caption_sequence in engine/render.py)."""
    font_size = round(height * 0.075)
    font = _font(font_size)
    tmp_draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    max_w = width * 0.85
    bbox = tmp_draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    if tw > max_w:
        font_size = max(round(font_size * max_w / tw), round(height * 0.03))
        font = _font(font_size)
        bbox = tmp_draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    cx = (width - tw) / 2 - bbox[0]
    cy = height * 0.72 - th / 2 - bbox[1]  # unteres Drittel, zentriert

    fill = WORD_CAPTION_ACCENT if _ACCENT_WORD_RE.search(text) else WORD_CAPTION_WHITE

    # Weicher Schlagschatten: eigene Ebene, geblurrt, dann unter den Haupttext gelegt --
    # ein reiner stroke_fill (wie bei den anderen Stilen) ist ein HARTER Rand, hier
    # zusätzlich ein weicher Tiefenschatten für bessere Lesbarkeit auf hellen Fotos.
    shadow_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_layer)
    shadow_offset = max(2, font_size // 16)
    shadow_draw.text((cx + shadow_offset, cy + shadow_offset * 1.4), text, font=font,
                      fill=(0, 0, 0, 190))
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=max(2, font_size // 14)))

    img = Image.alpha_composite(Image.new("RGBA", (width, height), (0, 0, 0, 0)), shadow_layer)
    draw = ImageDraw.Draw(img)
    draw.text((cx, cy), text, font=font, fill=fill,
               stroke_width=max(3, font_size // 10), stroke_fill=(0, 0, 0, 255))
    return img


def render_blank(width, height, text=""):
    """Rein transparentes PNG -- für die Lücke vor dem ersten Wort einer Wort-Caption-
    Sequenz (engine/render.py::_render_word_caption_sequence). Bewusst NICHT über
    render_caption("") wiederverwendet: render_caption zeichnet seine halbtransparente
    Bauchbinden-Box UNABHÄNGIG davon ob der Text leer ist -- das wäre hier ein
    sichtbarer schwarzer Balken ohne Text, nicht "nichts"."""
    return Image.new("RGBA", (width, height), (0, 0, 0, 0))


def render_word_caption_batch(out_dir, width, height, words):
    """Rendert EINE PNG pro Wort in `words` (word_0000.png, word_0001.png, ...) über
    EINEN Subprocess-Aufruf -- die eigentliche Kosten-Optimierung (Python-Start +
    Pillow-Import passiert einmal pro SZENE, nicht einmal pro WORT). Gleiches Batching-
    Prinzip wie render_counter_anim_sequence weiter unten."""
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for i, w in enumerate(words):
        path = os.path.join(out_dir, f"word_{i:04d}.png")
        render_word_caption(width, height, w).save(path)
        paths.append(path)
    return paths


def render_chapter(width, height, text):
    """Chapter-title card: centered, smaller than a callout, no box -- a brief scene-
    setting label rather than a shouted number. Shown at a sequence's first scene."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font_size = round(height * 0.06)
    font = _font(font_size)
    lines = _wrap(draw, text, font, width * 0.7, max_lines=2)
    line_h = font_size * 1.25
    block_h = line_h * len(lines)
    _draw_lines(draw, lines, font, width, (height - block_h) / 2, line_h,
                fill=(255, 255, 255, 255), stroke_width=max(1, font_size // 16),
                stroke_fill=(0, 0, 0, 220))
    return img


# ── Phase N: Animierte Daten-Overlays (Count-Up) ──────────────────────────────

def _smoothstep(t: float) -> float:
    """3t²-2t³ easing curve, matching _render_clip in dashboard.py."""
    t = max(0.0, min(1.0, t))
    return 3 * t * t - 2 * t * t * t


def render_counter_anim_frame(width, height, from_val, to_val, t, fmt, label=""):
    """Phase N: Ein einzelnes Frame einer Count-Up-Sequenz.

    t: 0.0..1.0 (Position in der Animation, smoothstep-interpoliert)
    from_val/to_val: Zahlenwerte für Count-Up (z.B. 0 → 3.2)
    fmt: Format-String (z.B. "3,2 Mio." → ".1f")
    label: Optionaler Untertitel (z.B. "verhungert 1994–1998")

    Phase N.1: identische Optik zum statischen counter (rote Letter-Fill, schwarzer Stroke)
    damit die Animation als „Variante des gleichen Stils" lesbar bleibt.
    """
    val = from_val + (to_val - from_val) * _smoothstep(t)
    text = fmt.format(val)

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Counter-Look: große Schrift, mittig, rot mit schwarzem Stroke (wie render_counter)
    font_size = round(height * 0.22)
    font = _font(font_size)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    cx = (width - text_w) / 2
    # Position leicht oberhalb Mitte, damit Platz für Label bleibt
    cy = (height - text_h) / 2 - (round(height * 0.06) if label else 0)
    draw.text((cx, cy), text, font=font,
              fill=(220, 38, 38, 255),
              stroke_width=max(4, font_size // 12),
              stroke_fill=(0, 0, 0, 255))

    if label:
        label_font_size = round(height * 0.035)
        label_font = _font(label_font_size)
        label_bbox = draw.textbbox((0, 0), label, font=label_font)
        label_w = label_bbox[2] - label_bbox[0]
        label_x = (width - label_w) / 2
        label_y = cy + text_h + round(height * 0.02)
        draw.text((label_x, label_y), label, font=label_font,
                  fill=(255, 255, 255, 255),
                  stroke_width=2,
                  stroke_fill=(0, 0, 0, 220))
    return img


def render_counter_anim_sequence(width, height, from_val, to_val, n_frames, fmt, label, out_dir):
    """Phase N.1: rendert N Frames als PNG-Sequenz (ov_0000.png, ov_0001.png, ...).

    ffmpeg liest sie später mit `-framerate 30 -i ov_%04d.png` (Plan §4.3 N.2).
    """
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for i in range(n_frames):
        t = i / max(1, n_frames - 1) if n_frames > 1 else 1.0
        path = os.path.join(out_dir, f"ov_{i:04d}.png")
        render_counter_anim_frame(width, height, from_val, to_val, t, fmt, label).save(path)
        paths.append(path)
    return paths


RENDERERS = {"caption": render_caption, "callout": render_callout,
              "chapter": render_chapter, "counter": render_counter,
              "word_caption": render_word_caption, "blank": render_blank}

# Phase E title-card accent colors — same key fingerprint as PHASE_COLOR_FILTER in
# dashboard.py so a "CLIMAX" scene's title-card underline matches the warm red the
# color-grading filter pushes the rest of that scene toward.
PHASE_ACCENT = {
    "OPENING":       "#888",
    "RISING_ACTION": "#1e6bd6",
    "CLIMAX":        "#c13838",
    "RESOLUTION":    "#1f8a4a",
}


def render_title_card(width, height, text, phase=""):
    """Phase E: full-frame opaque title-card PNG. Replaces the LLM-generated narrative
    still for scenes with kind='title_card' (act-breaks). The downstream _render_clip
    pipeline (zoompan + color-grading + overlays) still applies on top — just like
    regular images, the title text remains readable through the slow-pan motion because
    the text is centered + large + the phase accent underline is a thick bar."""
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    font_size = round(height * 0.12)
    font = _font(font_size)
    lines = _wrap(draw, text, font, width * 0.85, max_lines=2)
    line_h = font_size * 1.25
    block_h = line_h * len(lines)
    accent = PHASE_ACCENT.get(phase, "#000")
    # Each line centered, with optional underline accent below the last line
    y = (height - block_h) / 2
    line_y_positions = []
    for line in lines:
        w = draw.textlength(line, font=font)
        draw.text(((width - w) / 2, y), line, font=font, fill="black",
                   stroke_width=max(1, font_size // 18), stroke_fill=(255, 255, 255))
        line_y_positions.append((y + font_size, (width - w) / 2, w))
        y += line_h
    last_y, last_x, last_w = line_y_positions[-1]
    underline_y = last_y + round(height * 0.04)
    pad = round(width * 0.04)
    draw.line([(max(0, last_x - pad), underline_y),
               (min(width, last_x + last_w + pad), underline_y)],
              fill=accent, width=max(2, round(height * 0.006)))
    return img


def main():
    if len(sys.argv) < 6:
        print("usage: render_overlay.py <out.png> <width> <height> <style> <text_b64>", file=sys.stderr)
        sys.exit(1)
    out_path, width, height, style, text_b64 = sys.argv[1:6]
    width, height = int(width), int(height)
    text = base64.b64decode(text_b64).decode("utf-8")
    phase = ""
    if style == "title_card":
        # title_card can take an optional 7th arg = phase (for accent color)
        phase = sys.argv[6] if len(sys.argv) > 6 else ""
        render_title_card(width, height, text, phase).save(out_path)
        return
    if style == "counter_anim":
        # Phase N: Counter-Anim-Sequenz-Modus
        # argv: out_path, width, height, "counter_anim", fmt_b64,
        #        from_val, to_val, n_frames, label_b64
        if len(sys.argv) < 10:
            print("usage: render_overlay.py <out_dir> <width> <height> counter_anim "
                  "<fmt_b64> <from_val> <to_val> <n_frames> <label_b64>",
                  file=sys.stderr)
            sys.exit(1)
        fmt = base64.b64decode(sys.argv[6]).decode("utf-8")
        from_val = float(sys.argv[7])
        to_val = float(sys.argv[8])
        n_frames = int(sys.argv[9])
        label_b64 = sys.argv[10]
        label = base64.b64decode(label_b64).decode("utf-8") if label_b64 else ""
        render_counter_anim_sequence(width, height, from_val, to_val, n_frames, fmt, label, out_path)
        return
    if style == "word_caption_batch":
        # Schritt 3: argv = out_dir, width, height, "word_caption_batch", words_json_b64
        words = json.loads(base64.b64decode(text_b64).decode("utf-8"))
        render_word_caption_batch(out_path, width, height, words)
        return
    fn = RENDERERS.get(style)
    if not fn:
        print(f"unknown style: {style}", file=sys.stderr)
        sys.exit(1)
    fn(width, height, text).save(out_path)


if __name__ == "__main__":
    main()
