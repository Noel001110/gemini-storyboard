"""engine.prompts — Bild-Prompt-Komposition + Character-Sheet-Pipeline.

Enthält (Phase M.5, 2026-07-07):
    Funktionen:
        _phase_prompt_addition       — Phase-Style-Lookup
        _build_image_prompt          — Bild-Prompt zusammensetzen (Scene + Char-Refs + Phase + Master)
        _build_video_prompt          — Video-Prompt zusammensetzen (Veo)
        load_char_refs               — Char-Sheet-Metadaten aus Dateien laden
        analyze_char_image           — LLM-Aufruf: Character-Design-Spec aus Bild
        gen_charsheet                — 5-Pose-Sheet via Bildmodell generieren

NICHT hier (Phase M.6 Orchestrator, dann Q/38):
    IMAGE_MASTER_DEFAULT, VIDEO_MASTER_DEFAULT  — bleiben in dashboard.py bis Phase Q
                                                (dann ersetzt durch PRESET_MASTERS)
    visual_prompts, _image_prompt_chunk, etc.   — LLM-Pipeline, bleibt dashboard.py
    PHASE_PROMPT_ADDITIONS                      — lebt schon in engine_elevenlabs.py
                                                (Lazy-Import hier)

Externe Abhängigkeiten (lazy importiert):
    engine_elevenlabs.PHASE_PROMPT_ADDITIONS  — Phase→Style-Mapping (Quelle der Wahrheit)
"""

from __future__ import annotations

import base64
import json
import os
import re


# ── Public API ────────────────────────────────────────────────────────────────

__all__ = [
    "_phase_prompt_addition",
    "_build_image_prompt", "_build_video_prompt",
    "load_char_refs", "analyze_char_image", "gen_charsheet",
]


# ── Phase-Style-Lookup ───────────────────────────────────────────────────────

def _phase_prompt_addition(phase: str) -> str:
    """Inline lookup of phase-specific prompt cues. Kept as thin wrapper for clarity
    at call sites — actual logic is `PHASE_PROMPT_ADDITIONS.get(phase, "")`.

    Used inside `_build_image_prompt` — a HARD injection of the phase STYLE into the
    final KIE-bound prompt — making Phase C a real constraint (vs. a soft hint that
    the LLM might forget).
    """
    from engine_elevenlabs import PHASE_PROMPT_ADDITIONS  # lazy: avoid cycle
    return PHASE_PROMPT_ADDITIONS.get(phase, "")


# ── Prompt-Komposition ───────────────────────────────────────────────────────

def _build_image_prompt(scene_prompt, master, char_refs, phase=""):
    """Compose the final image-generation prompt: scene text + character refs (if any)
    + PHASE_PROMPT_ADDITIONS hard-injection (Phase C, Juli 2026) + master prompt.
    The phase cue is hard-injected (not just hinted to the LLM) to make Phase C a
    real constraint instead of an LLM-soft-compliance thing.
    """
    char_hint = ""
    if char_refs:
        for cr in char_refs:
            desc = cr.get("description", ""); name = cr.get("name", "Figur")
            if desc:
                char_hint += (f"\n\nCHARACTER DESIGN for '{name}': {desc}"
                              f"\nApply this exact design in whatever pose this scene requires.")
    phase_hint = ""
    if phase:
        phase_cue = _phase_prompt_addition(phase)
        if phase_cue:
            phase_hint = f"\n\nSTYLE ({phase}): {phase_cue}"
    return scene_prompt + char_hint + phase_hint + "\n\n" + master


def _build_video_prompt(scene_prompt: str, vid_master: str) -> str:
    """Append the literal master prompt to the scene action description.
    Veo only ever sees the final submitted string — it has no access to the
    dashboard's master prompt field, so the style must be embedded here every
    time, not just hinted to the LLM that writes the scene description.
    """
    return scene_prompt.strip() + "\n\nVISUAL STYLE (apply exactly):\n" + vid_master.strip()


# ── Character-Sheets ─────────────────────────────────────────────────────────

def load_char_refs(cid="default"):
    """Load character-sheet metadata from JSON files in the channel's charsheets dir."""
    # Lazy-import to avoid cycle: ch_sheets is in dashboard.py
    from dashboard import ch_sheets
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
    # Lazy-import: post_kie_text is in dashboard.py
    from dashboard import post_kie_text
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
    """Generate a character reference sheet image (5 poses) and return the bytes."""
    # Lazy-imports: ch_sheets + gen_image live in dashboard.py
    from dashboard import ch_sheets, gen_image
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
    # Pre-33.2 cleanup: backslash inside an f-string expression part is illegal on
    # Python 3.11/3.12 (PEP 701, allowed only in 3.13+). Extracting the regex
    # sanitizer value to its own line keeps the server startable on 3.11/3.12.
    tmp_name = re.sub(r"[^\w]", "_", name)
    tmp = os.path.join(ch_sheets(cid), f"_tmp_{tmp_name}.jpg")
    res = gen_image(prompt, "", tmp)
    if res["ok"]:
        data = open(tmp, "rb").read()
        try: os.unlink(tmp)
        except: pass
        return data
    raise RuntimeError(f"Character sheet generation failed: {res.get('error')}")