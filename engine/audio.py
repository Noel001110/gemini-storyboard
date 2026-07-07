"""engine.audio — Audio-Pipeline (Musik + SFX + Voice-Mixing).

Enthält (Phase M.4, 2026-07-07):
    Konstanten:
        SOUND_ASSETS_DIR, MUSIC_BED_FILE, SFX_FILES
    Funktionen:
        _build_sfx_events             — Regelbasierte SFX-Timing-Events
        _duck_music_under_voice       — sidechaincompress Ducking
        _place_sfx                    — SFX auf Narration setzen
        _phase_modulate_music         — Phase-Volume-Envelope (Phase G)
        _build_final_audio            — Orchestrator: Phase → Duck → SFX → loudnorm

BLEIBEN in dashboard.py (Phase M.6 Orchestrator):
    _render_worker                   — Ruft _build_final_audio + _mux_audio auf

Externe Abhängigkeiten (lazy importiert):
    engine_elevenlabs.PHASE_VOLUME  — Phase→Volume-Mapping
    engine.render._transition_for_scene — SFX-Family-Pick (gleiche Logik wie Video)
"""

from __future__ import annotations

import os
import shutil
import subprocess


# ── Constants ────────────────────────────────────────────────────────────────

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOUND_ASSETS_DIR = os.path.join(HERE, "assets")
MUSIC_BED_FILE = os.path.join(SOUND_ASSETS_DIR, "music", "neutral_bed.mp3")
SFX_FILES = {
    "whoosh": os.path.join(SOUND_ASSETS_DIR, "sfx", "whoosh_01.wav"),
    "impact": os.path.join(SOUND_ASSETS_DIR, "sfx", "impact_01.wav"),
    "riser":  os.path.join(SOUND_ASSETS_DIR, "sfx", "riser_01.wav"),
}


# ── Public API ────────────────────────────────────────────────────────────────

__all__ = [
    "SOUND_ASSETS_DIR", "MUSIC_BED_FILE", "SFX_FILES",
    "_build_sfx_events", "_duck_music_under_voice", "_place_sfx",
    "_phase_modulate_music", "_build_final_audio",
]


# ── SFX-Events ───────────────────────────────────────────────────────────────

def _build_sfx_events(scenes: list) -> list:
    """Rule-based SFX timing — no LLM call.

    - At a real sequence/scene change (this scene is the anchor of a sequence whose
      immediately preceding scene belongs to a DIFFERENT sequence or none): SFX is
      whatever _transition_for_scene picked for the matching visual crossfade — a
      "fade"-family transition gets no SFX, "wipe"/"smooth" get "whoosh". Visual and
      audio transition are therefore always in sync by construction.
    - 'riser' fires at every 'punchy' scene.
    - 'impact' additionally fires at a 'punchy' scene that is NOT already a crossfade
      transition point — genuine hard cut on a dramatic beat gets a sharp accent.

    Timing prefers `start_aligned` (Phase 3, Whisper word-timestamps) over estimated
    `start` when present.
    """
    # Lazy-import to break engine.render ↔ engine.audio cycle
    from engine.render import _transition_for_scene

    def scene_start(s):
        return s["start_aligned"] if s.get("start_aligned") is not None else s.get("start", 0)

    events = []
    for idx, s in enumerate(scenes):
        if idx == 0:
            continue
        prev = scenes[idx - 1]
        is_transition = s.get("seq_id") is not None and s.get("seq_pos", 0) == 0 and prev.get("seq_id") != s.get("seq_id")
        if is_transition:
            _transition_type, sfx, _duration = _transition_for_scene(s, idx)
            if sfx:
                events.append({"start": scene_start(s), "sfx": sfx})
        if s.get("pacing") == "punchy":
            events.append({"start": scene_start(s), "sfx": "riser"})
            if not is_transition:
                events.append({"start": scene_start(s), "sfx": "impact"})
    return events


# ── Ducking ──────────────────────────────────────────────────────────────────

def _duck_music_under_voice(voice_path: str, music_path: str, out_path: str) -> None:
    """Music bed ducked under the voiceover via sidechaincompress — volume drops
    automatically whenever the voice is present, rises back up in gaps. `-stream_loop -1`
    on the music input loops the bed file for the whole video; `amix=duration=first`
    then trims the result to the voice track's exact length.
    """
    cmd = ["ffmpeg", "-y", "-i", voice_path, "-stream_loop", "-1", "-i", music_path,
           "-filter_complex",
           "[0:a]asplit=2[voice][sc];"
           "[1:a][sc]sidechaincompress=threshold=0.02:ratio=10:attack=50:release=500[ducked];"
           "[voice][ducked]amix=inputs=2:duration=first[a]",
           "-map", "[a]", out_path]
    result = subprocess.run(cmd, capture_output=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg Ducking fehlgeschlagen: {result.stderr.decode(errors='replace')[-300:]}")


# ── SFX-Placement ────────────────────────────────────────────────────────────

def _place_sfx(narration_path: str, sfx_events: list, out_path: str) -> None:
    """Layers SFX files onto the (already ducked) narration track at specific
    timestamps via adelay, then amix + loudnorm for one consistent final loudness.
    Silently skips any event whose asset file doesn't exist — a missing single SFX
    file must not fail the whole render.
    """
    inputs = ["-i", narration_path]
    filter_parts, labels = [], []
    for ev in sfx_events:
        sfx_path = SFX_FILES.get(ev["sfx"])
        if not sfx_path or not os.path.exists(sfx_path):
            continue
        inputs += ["-i", sfx_path]
        delay_ms = max(0, round(ev["start"] * 1000))
        label = f"s{len(labels)}"
        filter_parts.append(f"[{len(labels)+1}:a]adelay={delay_ms}|{delay_ms},volume=0.7[{label}]")
        labels.append(label)

    if not labels:
        # Nothing to place — just loudnorm-normalize the narration/music mix alone.
        cmd = ["ffmpeg", "-y", "-i", narration_path, "-af", "loudnorm", out_path]
    else:
        mix_inputs = "[0:a]" + "".join(f"[{l}]" for l in labels)
        filter_complex = ";".join(filter_parts) + f";{mix_inputs}amix=inputs={len(labels)+1}:duration=first,loudnorm[a]"
        cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", filter_complex, "-map", "[a]", out_path]
    result = subprocess.run(cmd, capture_output=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg SFX-Platzierung fehlgeschlagen: {result.stderr.decode(errors='replace')[-300:]}")


# ── Phase-Volume-Envelope ────────────────────────────────────────────────────

def _phase_modulate_music(music_path: str, scenes: list, out_path: str) -> None:
    """Phase G: pre-modulate music volume per scene's phase BEFORE sidechaincompress.

    Expression per scene: `(if(gte(t,ST),1,0))*(if(lt(t,EN),VOL,0))` — inclusive-start,
    exclusive-end semantics, avoids the staircase peak at scene boundaries that
    `between(t,a,b)*vol` introduces (ffmpeg's `between` is inclusive at BOTH ends).

    End-of-interval EN prefers `end_aligned` (post-trim audio end) over the planned
    `start_aligned + dur`. Whisper's pause-trim may have shortened the scene.

    Falls back to a plain copy if no scene has a phase or ffmpeg returns non-zero.
    """
    from engine_elevenlabs import PHASE_VOLUME  # lazy import, see module docstring

    parts = []
    for s in scenes:
        ph = s.get("phase", "")
        if ph not in PHASE_VOLUME:
            continue
        vol = PHASE_VOLUME[ph]
        st = s.get("start_aligned") or s.get("start", 0.0)
        en = s.get("end_aligned") or (st + max(0.1, s.get("dur", 5.0)))
        # Exclusive-end inclusive-start interval: 1 only when st <= t < en.
        # ffmpeg syntax: gte(a,b)=1 if a>=b, lt(a,b)=1 if a<b. AND-multiplied.
        parts.append(f"(if(gte(t,{st:.3f}),1,0))*(if(lt(t,{en:.3f}),{vol:.2f},0))")
    if not parts:
        shutil.copy(music_path, out_path)
        return
    expr = "+".join(parts)
    cmd = ["ffmpeg", "-y", "-i", music_path, "-af", f"volume='{expr}'", out_path]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120, text=True)
        if result.returncode != 0:
            print(f"  [Audio] phase-modulate failed ({result.stderr[-200:]}) — fallback ohne Modulation", flush=True)
            shutil.copy(music_path, out_path)
    except Exception as e:
        print(f"  [Audio] phase-modulate exception ({e}) — fallback ohne Modulation", flush=True)
        shutil.copy(music_path, out_path)


# ── Final-Audio-Orchestrator ─────────────────────────────────────────────────

def _build_final_audio(voice_path: str, scenes: list, render_dir: str) -> str:
    """Builds the final audio track for muxing: voiceover + ducked music bed + rule-
    based SFX, loudnorm-normalized. Falls back to the raw voiceover unchanged if the
    music bed asset is missing.

    Phase G: between loading the music bed and sidechaincompressing it, runs a per-
    phase volume envelope (PHASE_VOLUME table on the music input). With the current
    neutral_bed.mp3 the effect is audible but subtle — Phase K will replace it with
    tiered Pixabay stems for full differentiation.
    """
    if not os.path.exists(MUSIC_BED_FILE):
        print("  [Render] Kein Musikbett gefunden (assets/music/neutral_bed.mp3) — "
              "rendere ohne Sound-Design, nur Voiceover.", flush=True)
        return voice_path
    try:
        # Phase G: phase-modulate the music bed before ducking
        phase_modulated_path = os.path.join(render_dir, "_phase_modulated.mp3")
        _phase_modulate_music(MUSIC_BED_FILE, scenes, phase_modulated_path)
        ducked_path = os.path.join(render_dir, "_ducked.mp3")
        _duck_music_under_voice(voice_path, phase_modulated_path, ducked_path)
        sfx_events = _build_sfx_events(scenes)
        final_audio_path = os.path.join(render_dir, "_final_audio.mp3")
        _place_sfx(ducked_path, sfx_events, final_audio_path)
        print(f"  [Render] Sound-Design: Phase-G-modulierter Musikbett gedückt + {len(sfx_events)} SFX-Ereignisse platziert", flush=True)
        return final_audio_path
    except Exception as e:
        print(f"  [Render] Sound-Design fehlgeschlagen ({e}) — falle zurück auf reines Voiceover.", flush=True)
        return voice_path