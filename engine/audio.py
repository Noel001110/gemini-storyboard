"""engine.audio — Audio-Pipeline (Musik + SFX + Voice-Mixing).

Enthält (Phase M.4 + K, 2026-07-07):
    Konstanten:
        SOUND_ASSETS_DIR, MUSIC_BED_FILE (Fallback), SFX_FILES, MUSIC_BEDS
    Funktionen:
        _build_sfx_events             — Regelbasierte SFX-Timing-Events
        _duck_music_under_voice       — sidechaincompress Ducking
        _place_sfx                    — SFX auf Narration setzen
        _phase_modulate_music         — Phase-Volume-Envelope (Phase G)
        _build_music_track            — Segment-Kette über Phasen-Blöcke (Phase K.3)
        _build_final_audio            — Orchestrator: Music-Track → Duck → SFX → loudnorm

BLEIBEN in dashboard.py (Phase M.6 Orchestrator):
    _render_worker                   — Ruft _build_final_audio + _mux_audio auf

Externe Abhängigkeiten (lazy importiert):
    engine_elevenlabs.PHASE_VOLUME  — Phase→Volume-Mapping
    engine.render._transition_for_scene — SFX-Family-Pick (gleiche Logik wie Video)

Phase K Besonderheiten (CINEMATIC_UPGRADE_PLAN.md §3 + §4.1):
    MUSIC_BEDS ist ein dict[tier_name] -> [file_path, ...]. Mehrere Betten pro Stufe
    verhindern Monotonie in langen Videos. Bett-Auswahl deterministisch via
    block_index % len(candidates) — analog _motion_for_scene.

    Die Segment-Kette erzeugt _music_track.mp3:
      1. Blockgruppierung: zusammenhängende Szenen mit gleicher Phase→Stufe
      2. Bett-Auswahl pro Block
      3. atrim auf Blockdauer (Crossfade-Kompensation)
      4. acrossfade mit qsin-Kurve (3s, glatter als tri)
      5. HighPass auf 80Hz (gleicher Bass-Headroom für alle Betten)
      6. Loudnorm auf -16 LUFS (Broadcast-Standard) — kohärente Lautstärke zwischen Stufen
    Fallback-Kette: Tier leer → neutral_bed.mp3 → gar keine Musik.
"""

from __future__ import annotations

import os
import shutil
import subprocess


# ── Constants ────────────────────────────────────────────────────────────────

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOUND_ASSETS_DIR = os.path.join(HERE, "assets")
MUSIC_DIR = os.path.join(SOUND_ASSETS_DIR, "music")
MUSIC_BED_FILE = os.path.join(MUSIC_DIR, "neutral_bed.mp3")  # Phase K Fallback
SFX_DIR = os.path.join(SOUND_ASSETS_DIR, "sfx")
SFX_FILES = {
    "whoosh": os.path.join(SFX_DIR, "whoosh_01.wav"),
    "impact": os.path.join(SFX_DIR, "impact_01.wav"),
    "riser":  os.path.join(SFX_DIR, "riser_01.wav"),
    "swell":  os.path.join(SFX_DIR, "swell_01.wav"),  # Phase K — neu (Klimax-Anlauf)
}

# Phase K.2 — MUSIC_BEDS: tier_name → Liste von Bett-Pfaden. Mehrere pro Stufe
# verhindern Monotonie; Auswahl via block_index % len(candidates).
# Reihenfolge der Pfade pro Tier = empfohlene Reihenfolge, deterministisch.
MUSIC_BEDS: dict[str, list[str]] = {
    "calm": [
        # Primär (längere, ruhigere Betten für OPENING/RESOLUTION)
        os.path.join(MUSIC_DIR, "bed_calm_02.mp3"),                                # 116s
        os.path.join(MUSIC_DIR, "bed_calm_05.mp3"),                                # 141s
        os.path.join(MUSIC_DIR, "bed_calm_03.mp3"),                                # 166s
        os.path.join(MUSIC_DIR, "leberch-calm-background-375199.mp3"),               # 96s
        os.path.join(MUSIC_DIR, "lnplusmusic-soft-calm-background-music-416544.mp3"),# 325s
        os.path.join(MUSIC_DIR, "bed_calm_01_short.mp3"),                          # 90s (atrim von 62min-Fass)
    ],
    "tension": [
        os.path.join(MUSIC_DIR, "bed_tension_02.mp3"),                              # 87s
        os.path.join(MUSIC_DIR, "atlasaudio-suspense-tension-511877.mp3"),           # 102s
        os.path.join(MUSIC_DIR, "leberch-tension-510483.mp3"),                       # 96s
        os.path.join(MUSIC_DIR, "bed_tension_04.mp3"),                             # 285s
        os.path.join(MUSIC_DIR, "bed_tension_05.mp3"),                             # 265s
    ],
    "climax": [
        os.path.join(MUSIC_DIR, "bed_climax_01.mp3"),                              # 133s
        os.path.join(MUSIC_DIR, "bed_climax_05.mp3"),                              # 187s
        os.path.join(MUSIC_DIR, "bed_climax_04.mp3"),                              # 186s
        os.path.join(MUSIC_DIR, "sound_for_you-epic-jungle-drums-dramatic-epic-jungle-percussion-468532.mp3"),  # 66s
        os.path.join(MUSIC_DIR, "thefealdoproject-the-untouched-secret-revealed-15651.mp3"),  # 261s
    ],
}

# Phase→Tier-Mapping (Plan §3 Phase K.2)
PHASE_TO_TIER: dict[str, str] = {
    "OPENING":       "calm",
    "RISING_ACTION": "tension",
    "CLIMAX":        "climax",
    "RESOLUTION":    "calm",
}

# Phase K.3 — Crossfade-Parameter
CROSSFADE_DURATION_SEC = 3.0  # Plan §4.1 empfiehlt 3-4s bei verschiedenen Stilen
CROSSFADE_CURVE = "qsin"       # Plan-Vorschlag: glatter als tri
HIGHPASS_HZ = 80                # Plan: gleicher Bass-Headroom für alle Betten
TARGET_LUFS = -16               # Broadcast-Standard (Streaming)
TARGET_TRUE_PEAK_DB = -1.5
TARGET_LRA = 11


# ── Public API ────────────────────────────────────────────────────────────────

__all__ = [
    "SOUND_ASSETS_DIR", "MUSIC_BED_FILE", "SFX_FILES",
    "MUSIC_BEDS", "PHASE_TO_TIER",
    "_build_sfx_events", "_duck_music_under_voice", "_place_sfx",
    "_phase_modulate_music", "_build_music_track", "_build_final_audio",
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


# ── Phase K.3 — Segment-Kette ────────────────────────────────────────────────

def _phase_to_tier(phase: str) -> str | None:
    """Map story phase to music tier. Returns None if phase unknown → caller falls
    back to neutral_bed for that block."""
    return PHASE_TO_TIER.get(phase)


def _group_into_blocks(scenes: list) -> list:
    """Zusammenhängende Szenen gleicher Phase→Tier zu Blöcken gruppieren.

    Jeder Block: {'tier', 'start', 'duration', 'scene_indices'}.
    'start' = erste Block-Szene start_aligned (oder start)
    'duration' = Blockdauer (letztes end_aligned - erstes start_aligned)
    """
    blocks = []
    if not scenes:
        return blocks

    def _start(s):
        return s.get("start_aligned") if s.get("start_aligned") is not None else s.get("start", 0.0)
    def _end(s):
        return s.get("end_aligned") or (_start(s) + max(0.1, s.get("dur", 5.0)))

    cur_tier = _phase_to_tier(scenes[0].get("phase", ""))
    cur_start = _start(scenes[0])
    cur_end = _end(scenes[0])
    cur_indices = [0]

    for idx in range(1, len(scenes)):
        tier = _phase_to_tier(scenes[idx].get("phase", ""))
        if tier == cur_tier:
            cur_end = _end(scenes[idx])
            cur_indices.append(idx)
        else:
            blocks.append({
                "tier": cur_tier,
                "start": cur_start,
                "duration": cur_end - cur_start,
                "scene_indices": cur_indices,
            })
            cur_tier = tier
            cur_start = _start(scenes[idx])
            cur_end = _end(scenes[idx])
            cur_indices = [idx]

    # Letzter Block
    blocks.append({
        "tier": cur_tier,
        "start": cur_start,
        "duration": cur_end - cur_start,
        "scene_indices": cur_indices,
    })
    return blocks


def _select_bed_for_block(block_idx: int, tier: str) -> str | None:
    """Deterministische Bett-Auswahl: tier-beds[block_idx % len]. Falls Tier leer oder
    keine Datei existiert → return None (Caller fällt auf neutral_bed zurück)."""
    beds = MUSIC_BEDS.get(tier, [])
    beds = [b for b in beds if os.path.exists(b)]
    if not beds:
        return None
    return beds[block_idx % len(beds)]


def _build_music_track(scenes: list, render_dir: str) -> str | None:
    """Phase K.3 — Musik-Spur als Segment-Kette pro zusammenhängendem Phasen-Block.

    Per Block:
      1. Bett-Auswahl (deterministisch via _select_bed_for_block)
      2. atrim auf Blockdauer + Crossfade-Overlap (außer letzter Block)
      3. HighPass + Loudnorm im Block
      4. Acrossfade zwischen Blöcken (qsin, 3s)

    Returns path to music track, or None if no usable music was found (in which
    case _build_final_audio falls back to neutral_bed or pure voiceover).

    Fallback-Kette:
      - MUSIC_BEDS[tier] leer / Datei fehlt → neutral_bed.mp3 für diesen Block
      - neutral_bed.mp3 fehlt auch → kein Block, gib None zurück
    """
    blocks = _group_into_blocks(scenes)
    if not blocks:
        return None

    # Wenn KEIN Block eine gültige Tier-Bett hat und neutral_bed fehlt → None
    any_bed_available = any(_select_bed_for_block(i, b["tier"]) is not None
                            for i, b in enumerate(blocks))
    if not any_bed_available and not os.path.exists(MUSIC_BED_FILE):
        print("  [Audio] Keine MUSIC_BEDS-Dateien und neutral_bed fehlt — keine Musik-Spur.", flush=True)
        return None

    n = len(blocks)
    inputs = []
    filter_parts = []
    last_label = None

    for i, block in enumerate(blocks):
        tier = block["tier"]
        bed = _select_bed_for_block(i, tier) or MUSIC_BED_FILE
        if not os.path.exists(bed):
            continue
        inputs += ["-stream_loop", "-1", "-i", bed]
        # Input-Index = Anzahl bisheriger -i Flags (4 Elemente pro Input: -stream_loop -1 -i <path>)
        in_idx = len(inputs) // 4 - 1

        # Blockdauer inkl. Crossfade-Overlap (außer letzter Block)
        block_dur = block["duration"]
        if i < n - 1:
            block_dur_with_overlap = block_dur + CROSSFADE_DURATION_SEC
        else:
            block_dur_with_overlap = block_dur

        # Output-Label für dieses Segment
        seg_label = f"b{i}"

        # Filter: atrim + HighPass + Loudnorm
        # - atrim mit asetpts setzt Block-Start auf 0 (für cleanes Crossfade)
        # - HighPass entfernt unterschiedliche Bass-Heads (gleicher Headroom)
        # - Loudnorm normiert Lautstärke aller Blöcke auf gleichen LUFS
        filter_parts.append(
            f"[{in_idx}:a]atrim=0:{block_dur_with_overlap:.3f},"
            f"asetpts=PTS-STARTPTS,"
            f"highpass=f={HIGHPASS_HZ},"
            f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TRUE_PEAK_DB}:LRA={TARGET_LRA},"
            f"volume=0.85"  # leichte Reserve, damit final loudnorm nicht clippt
            f"[{seg_label}]"
        )

        if last_label is None:
            last_label = seg_label
        else:
            # Crossfade mit qsin — gleichberechtigte Kurve (kein Pegel-Sprung in Mitte)
            crossfade_label = f"x{i}"
            filter_parts.append(
                f"[{last_label}][{seg_label}]acrossfade="
                f"d={CROSSFADE_DURATION_SEC}:c1={CROSSFADE_CURVE}:c2={CROSSFADE_CURVE}"
                f"[{crossfade_label}]"
            )
            last_label = crossfade_label

    if last_label is None:
        return None

    # Filtergraph zusammenbauen
    filter_complex = ";".join(filter_parts)
    out_path = os.path.join(render_dir, "_music_track.mp3")

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", f"[{last_label}]",
        "-c:a", "libmp3lame", "-q:a", "2",
        out_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=300, text=True)
        if result.returncode != 0:
            print(f"  [Audio] _build_music_track failed: {result.stderr[-300:]} — fallback neutral_bed", flush=True)
            if os.path.exists(MUSIC_BED_FILE):
                shutil.copy(MUSIC_BED_FILE, out_path)
                return out_path
            return None
        print(f"  [Audio] _build_music_track: {n} Block(s), acrossfade={CROSSFADE_DURATION_SEC}s {CROSSFADE_CURVE}, LUFS={TARGET_LUFS}", flush=True)
        return out_path
    except Exception as e:
        print(f"  [Audio] _build_music_track exception: {e} — fallback neutral_bed", flush=True)
        if os.path.exists(MUSIC_BED_FILE):
            shutil.copy(MUSIC_BED_FILE, out_path)
            return out_path
        return None


# ── Final-Audio-Orchestrator ─────────────────────────────────────────────────

def _build_final_audio(voice_path: str, scenes: list, render_dir: str) -> str:
    """Builds the final audio track for muxing: voiceover + (Phase K) tiered music
    track + rule-based SFX + loudnorm-normalized.

    Phase K replaces the single neutral_bed with a per-block tiered segment chain.
    Fallbacks: neutral_bed for missing tiers → reines Voiceover.
    """
    try:
        # Phase K.3 — Segment-Kette über Phasen-Blöcke (mit HighPass + Loudnorm + qsin)
        music_path = _build_music_track(scenes, render_dir)
        if music_path is None:
            # Kein Music-Track überhaupt möglich → reines Voiceover
            print("  [Render] Keine Musik-Spur möglich — rendere nur Voiceover.", flush=True)
            return voice_path

        # Phase G: phase-modulate the music track before ducking
        phase_modulated_path = os.path.join(render_dir, "_phase_modulated.mp3")
        _phase_modulate_music(music_path, scenes, phase_modulated_path)
        ducked_path = os.path.join(render_dir, "_ducked.mp3")
        _duck_music_under_voice(voice_path, phase_modulated_path, ducked_path)
        sfx_events = _build_sfx_events(scenes)
        final_audio_path = os.path.join(render_dir, "_final_audio.mp3")
        _place_sfx(ducked_path, sfx_events, final_audio_path)
        n_blocks = len(_group_into_blocks(scenes))
        print(f"  [Render] Sound-Design: {n_blocks}-Block Segment-Kette + Phase-G-Modulation + "
              f"Ducking + {len(sfx_events)} SFX-Ereignisse platziert", flush=True)
        return final_audio_path
    except Exception as e:
        print(f"  [Render] Sound-Design fehlgeschlagen ({e}) — falle zurück auf reines Voiceover.", flush=True)
        return voice_path