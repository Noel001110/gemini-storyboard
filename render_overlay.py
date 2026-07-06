#!/usr/bin/env python3
"""Standalone helper: renders a text overlay (caption / callout / chapter title) as a
transparent PNG using Pillow. Runs inside .venv_whisper (an isolated venv shared with
whisper_transcribe.py) and is invoked via subprocess -- needed because the ffmpeg build
on this machine has no freetype/fontconfig compiled in, so its `drawtext` filter is
unavailable. The PNG this script produces gets composited onto a Ken-Burns clip with
ffmpeg's `overlay`/`fade` filters instead, both of which are in every standard build.

Usage: python3 render_overlay.py <out_path.png> <width> <height> <style> <text_b64>
  style:    "caption" | "callout" | "chapter"
  text_b64: base64-encoded UTF-8 text (sidesteps shell-escaping arbitrary punctuation)
"""
import sys
import os
import base64
from PIL import Image, ImageDraw, ImageFont

FONT_BOLD_CANDIDATES = [
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


RENDERERS = {"caption": render_caption, "callout": render_callout, "chapter": render_chapter}


def main():
    if len(sys.argv) < 6:
        print("usage: render_overlay.py <out.png> <width> <height> <style> <text_b64>", file=sys.stderr)
        sys.exit(1)
    out_path, width, height, style, text_b64 = sys.argv[1:6]
    width, height = int(width), int(height)
    text = base64.b64decode(text_b64).decode("utf-8")
    fn = RENDERERS.get(style)
    if not fn:
        print(f"unknown style: {style}", file=sys.stderr)
        sys.exit(1)
    fn(width, height, text).save(out_path)


if __name__ == "__main__":
    main()
