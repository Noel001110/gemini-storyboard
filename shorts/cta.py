"""shorts/cta.py — stille CTA-Endkarte für die Parts-Pipeline (shorts/api.py).

Bewusst NICHT als synthetisches Scene-Objekt in short_plan.json (würde die Whisper-
Alignment/Sync-Invariante-Pipeline in _render_worker für eine stille, textlastige Karte
extra verbiegen müssen) -- stattdessen ein reiner Nachbearbeitungsschritt NACH dem
fertigen, normal gemuxten Part-Render. User-Entscheidung: stille Text-Karte (kein TTS),
generischer Text ohne echten Link zum Longform-Video.
"""
from __future__ import annotations

import os
import subprocess

from PIL import Image, ImageDraw, ImageFont

from engine.render import _probe_video_encoder

DEFAULT_CTA_TEXT = "Watch the full video on YouTube!"
DEFAULT_CTA_DURATION_SEC = 3.0

# Eigener, kleinerer Font-Renderer statt engine.render.render_title_card_png_via_venv:
# dessen render_title_card() (render_overlay.py) ist für kurze 2-4-Wort-Akt-Titel
# ausgelegt (font_size = 12% der Höhe, max_lines=2) -- ein voller CTA-Satz wie
# "Watch the full video on YouTube!" wurde damit nach 2 Wörtern mit "…" abgeschnitten
# (empirisch getestet). Pillow ist auch im Haupt-Environment vorhanden (kein .venv_whisper-
# Subprocess nötig), deshalb hier ein eigener, kleinerer/mehrzeiliger Renderer.
_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]


def _font(size: int):
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def _wrap(draw, text: str, font, max_width: float) -> list:
    words = text.split()
    lines: list = []
    cur = ""
    for w in words:
        trial = (cur + " " + w).strip()
        if not cur or draw.textlength(trial, font=font) <= max_width:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _render_cta_card(width: int, height: int, text: str) -> Image.Image:
    """Vollbild-Textkarte, klein/mehrzeilig genug für einen ganzen CTA-Satz (anders als
    render_title_card, siehe Modul-Kommentar oben)."""
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    font_size = round(height * 0.055)
    font = _font(font_size)
    lines = _wrap(draw, text, font, width * 0.8)
    line_h = font_size * 1.3
    block_h = line_h * len(lines)
    y = (height - block_h) / 2
    for line in lines:
        w = draw.textlength(line, font=font)
        draw.text(((width - w) / 2, y), line, font=font, fill="black")
        y += line_h
    return img


def build_cta_clip(out_path: str, width: int, height: int, fps: int,
                    text: str = DEFAULT_CTA_TEXT,
                    duration: float = DEFAULT_CTA_DURATION_SEC) -> None:
    """Baut einen stillen, `duration` Sekunden langen Clip mit einer Vollbild-Textkarte
    + stillem Mono-Audio (anullsrc, 44100Hz -- passend zum Mono-Voiceover, siehe
    append_cta-Docstring), gleiches Encoder-Setup wie _render_clip."""
    png_path = f"{out_path}.png"
    _render_cta_card(width, height, text).save(png_path)
    try:
        encoder, encoder_args = _probe_video_encoder()
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", png_path,
            "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=44100",
            "-t", str(duration), "-r", str(fps),
            "-c:v", encoder, *encoder_args,
            "-c:a", "aac", "-shortest",
            "-pix_fmt", "yuv420p", "-video_track_timescale", "90000",
            out_path,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f"CTA-Clip-Render fehlgeschlagen: "
                                f"{result.stderr.decode(errors='replace')[-300:]}")
    finally:
        try: os.remove(png_path)
        except OSError: pass


def append_cta(base_video_path: str, cta_clip_path: str, out_path: str) -> None:
    """Hängt den CTA-Clip ans Ende des fertig gemuxten Part-Videos. Re-encoded (nicht
    `-c copy` wie _assemble_clips) -- der CTA-Clip und base_video_path durchliefen
    unterschiedliche ffmpeg-Aufrufe (build_cta_clip vs. _render_worker/_mux_audio),
    ein reiner Stream-Copy-Concat wäre bei kleinsten Parameter-Abweichungen (Timestamps,
    SPS/PPS) fragil. Re-Encode kostet hier nur wenige Sekunden (Part ist ohnehin kurz)
    und garantiert ein sauberes, durchgehendes Ergebnis."""
    tmp_dir = os.path.dirname(out_path) or "."
    concat_list_path = os.path.join(tmp_dir, "_cta_concat_list.txt")
    try:
        with open(concat_list_path, "w") as f:
            f.write(f"file '{os.path.abspath(base_video_path)}'\n")
            f.write(f"file '{os.path.abspath(cta_clip_path)}'\n")
        encoder, encoder_args = _probe_video_encoder()
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path,
               "-c:v", encoder, *encoder_args, "-c:a", "aac", "-b:a", "192k",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path]
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"CTA-Anhängen fehlgeschlagen: "
                                f"{result.stderr.decode(errors='replace')[-300:]}")
    finally:
        try: os.remove(concat_list_path)
        except OSError: pass
