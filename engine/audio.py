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
      6. Loudnorm auf TARGET_LUFS (-30, Underscore-Pegel) — kohärente Lautstärke zwischen
         Stufen, VOR dem Ducking. Die finale Gesamt-Loudness (Voice+Musik+SFX) wird erst
         ganz am Ende in _place_sfx auf FINAL_TARGET_LUFS (-14, YouTube-Streaming-Ziel)
         normalisiert (Cinematic-Mix-Fix, Juli 2026 — siehe TARGET_LUFS-Docstring).
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

# Cinematic-Mix Juli 2026: SFX_LIBRARY erschließt die 104 Sound-Dateien in
# assets/Sounds/, aber NUR aus "Free Cinematic SFX/" (Inferno/Firestarter Free) — das
# ist die exakt gleiche Quelle, aus der die 4 ursprünglichen assets/sfx/*.wav-Dateien
# stammen (siehe assets/CREDITS.txt: Flame Sound EULA, kommerziell nutzbar). Das
# zweite Paket in assets/Sounds/ ("#99S006 Cinematic Sound Effects" / Generdyn) hat
# laut CREDITS.txt UNGEKLÄRTEN Lizenzstatus und wird deshalb bewusst NICHT genutzt.
#
# Pro Kategorie mehrere Varianten (Rotation via _select_sfx_variant, deterministisch
# über einen Seed wie scene["i"] — kein Zufall, ARCHITECTURE §13/§15.1). Erster Eintrag
# je Kategorie ist immer die bereits vorhandene, kuratierte assets/sfx/*.wav-Datei —
# das hält SFX_FILES (unten) rückwärtskompatibel für Aufrufer, die den alten Single-
# Datei-Zugriff nutzen.
_FREE_SFX_DIR = os.path.join(SOUND_ASSETS_DIR, "Sounds", "Free Cinematic SFX")
_INFERNO_DIR = os.path.join(_FREE_SFX_DIR, "Inferno Free")
_FIRESTARTER_DIR = os.path.join(_FREE_SFX_DIR, "Firestarter Free")

SFX_LIBRARY: dict[str, list[str]] = {
    "whoosh": [
        os.path.join(SFX_DIR, "whoosh_01.wav"),  # = "Transition - Whoosh Hit 02.wav"
        os.path.join(_INFERNO_DIR, "Transition - Passby 06.wav"),
        os.path.join(_INFERNO_DIR, "Transition - Smooth 04.wav"),
        os.path.join(_FIRESTARTER_DIR, "Impact_WhooshHit01.wav"),
        os.path.join(_FIRESTARTER_DIR, "Impact_WhooshHit04.wav"),
        os.path.join(_FIRESTARTER_DIR, "Impact_WhooshHit06.wav"),
    ],
    "impact": [
        os.path.join(SFX_DIR, "impact_01.wav"),  # = "Impact - Big Bang.wav"
        os.path.join(_INFERNO_DIR, "Impact - Metallic Collision.wav"),
        os.path.join(_FIRESTARTER_DIR, "Impact_Avalanche.wav"),
        os.path.join(_FIRESTARTER_DIR, "Impact_GetDown.wav"),
        os.path.join(_FIRESTARTER_DIR, "Impact_Mechanical.wav"),
        os.path.join(_FIRESTARTER_DIR, "Impact_OhMyGod.wav"),
        os.path.join(_FIRESTARTER_DIR, "Impact_Smacker.wav"),
    ],
    "riser": [
        os.path.join(SFX_DIR, "riser_01.wav"),  # = "Tension - Riser 02.wav"
        os.path.join(_INFERNO_DIR, "Tension - Riser 12.wav"),
    ],
    "swell": [
        os.path.join(SFX_DIR, "swell_01.wav"),  # Pixabay (rson201) — unverändert
    ],
    "braam": [
        os.path.join(_INFERNO_DIR, "Braam - Standpoint.wav"),
        os.path.join(_INFERNO_DIR, "Braam - This One Is Massive.wav"),
        os.path.join(_INFERNO_DIR, "Braam - Up And Under.wav"),
        os.path.join(_FIRESTARTER_DIR, "Braam_NoImpact_Dissonance.wav"),
        os.path.join(_FIRESTARTER_DIR, "Braam_NoImpact_Ominous.wav"),
    ],
    "boom": [
        os.path.join(_INFERNO_DIR, "Boom - Big Reveal.wav"),
        os.path.join(_INFERNO_DIR, "Boom - Alarming.wav"),
        os.path.join(_INFERNO_DIR, "Boom - Cavernous B.wav"),
        os.path.join(_INFERNO_DIR, "Boom - Earth Core Ripper.wav"),
        os.path.join(_INFERNO_DIR, "Boom - Morphing.wav"),
        os.path.join(_FIRESTARTER_DIR, "Boom_Heavy_Transition.wav"),
    ],
    "downshifter": [
        os.path.join(_INFERNO_DIR, "Downshifter - Slowdown.wav"),
    ],
}

# Rückwärtskompatibel: Single-Datei-Zugriff (dashboard.py-Identity-Check,
# tests/test_cinematic_e2e.py::t_...) — jeweils die erste (kuratierte) Variante.
SFX_FILES = {cat: variants[0] for cat, variants in SFX_LIBRARY.items()}

# Dichte-Deckel (Schritt 2.4): "große" Akzent-SFX (braam/boom/impact/downshifter)
# brauchen Abstand, sonst verlieren sie ihre Wirkung — Profi-Regel: Akzente wirken nur
# selten gesetzt.
SFX_BIG_CATEGORIES = {"braam", "boom", "impact", "downshifter"}
SFX_DENSITY_MIN_GAP_SEC = 6.0

# Riser-Dateien in der Bibliothek sind bis zu ~20s lang (mit langem Reverb-Tail — normal
# für professionelle Cinematic-SFX-Packs). Für den Zweck hier ("kurzer Spannungs-Anlauf,
# Peak landet auf dem Cut") ist das zu lang: bei einem 20s-Anlauf würde der Riser über
# mehrere unbeteiligte Sätze/Szenen hinweg unmotiviert klingen. Sowohl das Anlauf-Timing
# als auch die tatsächliche Wiedergabelänge (in _place_sfx per atrim) werden auf diesen
# Wert gedeckelt. Braam/Boom/Impact/Downshifter bekommen KEINEN Deckel — ihr langes
# Ausklingen unter der Stimme ist beabsichtigte Charakteristik dieser Sound-Kategorie.
RISER_RUNUP_CAP_SEC = 3.0

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

# Cinematic-Mix Juli 2026: Musik ist Underscore, nicht Co-Lead. -16 LUFS lag praktisch
# auf Voice-Level (das war das "viel zu laut"-Feedback) -- Film-Mix-Standard für
# Hintergrundmusik unter Dialog ist -18 bis -22 dB relativ, hier als eigenständiger
# LUFS-Zielwert für die Musik-Kette VOR dem Ducking ausgedrückt.
TARGET_LUFS = -30               # Musik-Underscore-Pegel (vor Sidechain-Ducking)
TARGET_TRUE_PEAK_DB = -1.5
TARGET_LRA = 11

# Finale Loudness des kompletten Mixes (Voice+Musik+SFX) -- YouTube-Zielwert, nicht der
# Broadcast-Wert von -24: Streaming-Plattformen normalisieren selbst gegen -14 LUFS.
FINAL_TARGET_LUFS = -14
FINAL_TARGET_TRUE_PEAK_DB = -1.5
FINAL_TARGET_LRA = 11

# SFX-Pegel relativ zur (bereits geduckten) Stimme -- kategorieabhängig, damit große
# Akzente (Braam/Boom/Impact) hörbar bleiben, Übergangs-Whooshes aber nicht die Stimme
# überdecken. Werte sind linear-Faktoren für ffmpeg `volume=`.
SFX_VOLUME_BY_CATEGORY = {
    "whoosh": 0.25,       # -12 dB -- Übergangs-Akzent, dezent
    "impact": 0.35,       # -9 dB  -- harter Cut-Akzent
    "braam":  0.35,       # -9 dB  -- Phasen-Eintritt CLIMAX
    "boom":   0.35,       # -9 dB  -- Phasen-Grenze
    "riser":  0.20,       # -14 dB -- liegt länger unter der Stimme, darf leiser sein
    "swell":  0.20,       # -14 dB -- Klimax-Anlauf, wie riser
    "downshifter": 0.30,  # -10.5 dB -- Spannungsabbau CLIMAX->RESOLUTION
}
SFX_DEFAULT_VOLUME = 0.25


# ── Public API ────────────────────────────────────────────────────────────────

__all__ = [
    "SOUND_ASSETS_DIR", "MUSIC_BED_FILE", "SFX_FILES", "SFX_LIBRARY",
    "SFX_BIG_CATEGORIES", "SFX_DENSITY_MIN_GAP_SEC",
    "MUSIC_BEDS", "PHASE_TO_TIER",
    "TARGET_LUFS", "FINAL_TARGET_LUFS", "SFX_VOLUME_BY_CATEGORY", "SFX_DEFAULT_VOLUME",
    "_select_sfx_variant", "_sfx_duration_sec", "_apply_sfx_density_cap",
    "_phase_boundary_sfx_events",
    "_build_sfx_events", "_duck_music_under_voice", "_place_sfx",
    "_phase_modulate_music", "_build_music_track", "_build_final_audio",
]


# ── SFX-Auswahl + Dauer-Messung ──────────────────────────────────────────────

_SFX_DURATION_CACHE: dict[str, float] = {}


def _sfx_duration_sec(path: str) -> float:
    """ffprobe-Dauer einer SFX-Datei, gecacht (Dateien werden pro Render mehrfach
    wiederverwendet — jede zusätzliche ffprobe-Sekunde wäre reine Verschwendung)."""
    if path in _SFX_DURATION_CACHE:
        return _SFX_DURATION_CACHE[path]
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                               "-of", "csv=p=0", path], capture_output=True, text=True, timeout=15)
        dur = float(out.stdout.strip())
    except Exception:
        dur = 2.0  # konservativer Fallback (typische Riser-Länge)
    _SFX_DURATION_CACHE[path] = dur
    return dur


def _select_sfx_variant(category: str, seed: int) -> str | None:
    """Deterministische Varianten-Auswahl innerhalb einer SFX-Kategorie (rotiert über
    `seed % len(variants)` — gleiches Muster wie _select_bed_for_block/_motion_for_scene,
    kein Zufall). Fällt auf SFX_FILES[category] zurück, falls SFX_LIBRARY die Kategorie
    nicht kennt oder keine ihrer Dateien existiert."""
    variants = [p for p in SFX_LIBRARY.get(category, []) if os.path.exists(p)]
    if variants:
        return variants[seed % len(variants)]
    fallback = SFX_FILES.get(category)
    return fallback if fallback and os.path.exists(fallback) else None


# ── SFX-Events ───────────────────────────────────────────────────────────────

def _phase_boundary_sfx_events(scenes: list) -> list:
    """Vertont Story-Phasen-Grenzen (Schritt 2.3): Eintritt in CLIMAX bekommt einen
    Braam GENAU auf dem Schnitt plus einen Riser-Anlauf davor (Peak des Riser-Anlaufs
    landet auf dem Braam); der Ausstieg aus CLIMAX (→ RESOLUTION) bekommt einen
    Downshifter — das klassische "Spannung ablassen"-Trailer-Werkzeug. Andere
    Phasenwechsel bleiben unvertont (die visuelle Transition + ggf. punchy-SFX reichen).

    Nutzt dieselbe Blockgruppierung wie die Musik-Segmentkette (_group_into_blocks) —
    Musik-Blockgrenze und SFX-Akzent sitzen dadurch garantiert auf demselben Zeitpunkt.
    """
    events = []
    blocks = _group_into_blocks(scenes)
    for i in range(1, len(blocks)):
        prev_tier, cur_tier = blocks[i - 1]["tier"], blocks[i]["tier"]
        if cur_tier == prev_tier:
            continue
        start = blocks[i]["start"]
        if cur_tier == "climax" and prev_tier != "climax":
            riser_path = _select_sfx_variant("riser", i)
            riser_dur = min(_sfx_duration_sec(riser_path), RISER_RUNUP_CAP_SEC) if riser_path else RISER_RUNUP_CAP_SEC
            events.append({"start": max(0.0, start - riser_dur), "sfx": "riser", "seed": i,
                            "priority": 1, "big": False})
            events.append({"start": start, "sfx": "braam", "seed": i,
                            "priority": 2, "big": True})
        elif prev_tier == "climax" and cur_tier != "climax":
            events.append({"start": start, "sfx": "downshifter", "seed": i,
                            "priority": 2, "big": True})
    return events


def _apply_sfx_density_cap(events: list, min_gap_sec: float = SFX_DENSITY_MIN_GAP_SEC) -> list:
    """Schritt 2.4: "große" Akzent-SFX (SFX_BIG_CATEGORIES) brauchen mindestens
    `min_gap_sec` Abstand zueinander, sonst verlieren sie ihre Wirkung. Bei Konflikt
    gewinnt das Ereignis mit höherer `priority` (Phasen-Grenze > punchy-Szene); "kleine"
    Kategorien (whoosh/riser/swell) sind vom Deckel ausgenommen — die dürfen dicht sitzen,
    weil sie ohnehin leise/kurz sind (siehe SFX_VOLUME_BY_CATEGORY).
    """
    kept, kept_big_starts = [], []
    for ev in sorted(events, key=lambda e: (-e.get("priority", 0), e["start"])):
        if ev.get("big"):
            if any(abs(ev["start"] - t) < min_gap_sec for t in kept_big_starts):
                continue
            kept_big_starts.append(ev["start"])
        kept.append(ev)
    return sorted(kept, key=lambda e: e["start"])


def _build_sfx_events(scenes: list) -> list:
    """Rule-based SFX timing — no LLM call.

    - At a real sequence/scene change (this scene is the anchor of a sequence whose
      immediately preceding scene belongs to a DIFFERENT sequence or none): SFX is
      whatever _transition_for_scene picked for the matching visual crossfade — a
      "fade"-family transition gets no SFX, "wipe"/"smooth" get "whoosh". Visual and
      audio transition are therefore always in sync by construction.
    - 'riser' fires at every 'punchy' scene, timed so its PEAK (= its end) lands ON the
      cut — a riser builds tension TOWARD a moment, so it must START EARLIER, not at the
      cut itself (Sounddesign-Theorie, Schritt 2.2 — this was backwards before).
    - 'impact' additionally fires at a 'punchy' scene that is NOT already a crossfade
      transition point — genuine hard cut on a dramatic beat gets a sharp accent.
    - Story-Phasen-Grenzen (CLIMAX-Eintritt/Ausstieg) bekommen zusätzliche Braam/
      Downshifter-Akzente (_phase_boundary_sfx_events).
    - Alle "großen" Akzente laufen durch den Dichte-Deckel (_apply_sfx_density_cap),
      damit sie selten und damit wirksam bleiben.

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
        seed = s.get("i", idx)
        is_transition = s.get("seq_id") is not None and s.get("seq_pos", 0) == 0 and prev.get("seq_id") != s.get("seq_id")
        if is_transition:
            _transition_type, sfx, _duration = _transition_for_scene(s, idx)
            if sfx:
                events.append({"start": scene_start(s), "sfx": sfx, "seed": seed,
                                "priority": 0, "big": False})
        if s.get("pacing") == "punchy":
            riser_path = _select_sfx_variant("riser", seed)
            riser_dur = min(_sfx_duration_sec(riser_path), RISER_RUNUP_CAP_SEC) if riser_path else RISER_RUNUP_CAP_SEC
            events.append({"start": max(0.0, scene_start(s) - riser_dur), "sfx": "riser",
                            "seed": seed, "priority": 1, "big": False})
            if not is_transition:
                events.append({"start": scene_start(s), "sfx": "impact", "seed": seed,
                                "priority": 1, "big": True})

    events += _phase_boundary_sfx_events(scenes)
    return _apply_sfx_density_cap(events)


# ── Ducking ──────────────────────────────────────────────────────────────────

def _duck_music_under_voice(voice_path: str, music_path: str, out_path: str) -> None:
    """Music bed ducked under the voiceover via sidechaincompress — volume drops
    automatically whenever the voice is present, rises back up in gaps. `-stream_loop -1`
    on the music input loops the bed file for the whole video; `amix=duration=first`
    then trims the result to the voice track's exact length.

    `normalize=0` on amix is critical: ffmpeg's amix defaults to `normalize=1`, which
    divides EVERY input's gain by the input count (here: halves the voice too, silently
    undoing all careful level-setting upstream). Without it, the voice comes out at
    roughly half its real level -- the actual root cause of the whole mix "sounding too
    quiet/muddy", not just the music.

    attack=5/release=300 (vs. the old 50/500) makes the duck grab faster and let go
    faster -- since the music bed is already mixed to -30 LUFS underscore level (see
    TARGET_LUFS), the duck only needs a light additional dip, not a slow fade.
    """
    cmd = ["ffmpeg", "-y", "-i", voice_path, "-stream_loop", "-1", "-i", music_path,
           "-filter_complex",
           "[0:a]asplit=2[voice][sc];"
           "[1:a][sc]sidechaincompress=threshold=0.02:ratio=10:attack=5:release=300[ducked];"
           "[voice][ducked]amix=inputs=2:duration=first:normalize=0[a]",
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

    Per-category volume (SFX_VOLUME_BY_CATEGORY) instead of one flat 0.7 (-3 dB, far
    too loud relative to the voice) -- transitions stay dezent, accents stay punchy.
    `normalize=0` on amix for the same reason as in _duck_music_under_voice: without it
    the narration (already carrying the ducked music) gets divided down by the input
    count too. The final `loudnorm` is parameterized to FINAL_TARGET_LUFS (YouTube
    streaming target) instead of ffmpeg's un-parameterized default (-24 LUFS broadcast).
    """
    inputs = ["-i", narration_path]
    filter_parts, labels = [], []
    for ev in sfx_events:
        sfx_path = _select_sfx_variant(ev["sfx"], ev.get("seed", 0))
        if not sfx_path or not os.path.exists(sfx_path):
            continue
        inputs += ["-i", sfx_path]
        delay_ms = max(0, round(ev["start"] * 1000))
        vol = SFX_VOLUME_BY_CATEGORY.get(ev["sfx"], SFX_DEFAULT_VOLUME)
        label = f"s{len(labels)}"
        # Riser-Dateien haben oft einen ~20s-Reverb-Tail (siehe RISER_RUNUP_CAP_SEC-
        # Docstring) -- ohne atrim würde ein Riser weit über den beabsichtigten
        # Spannungs-Anlauf hinaus unmotiviert weiterlaufen. Andere Kategorien (Braam/
        # Boom/Impact/Downshifter) dürfen voll ausklingen -- das ist ihre Charakteristik.
        trim = f"atrim=0:{RISER_RUNUP_CAP_SEC}," if ev["sfx"] == "riser" else ""
        filter_parts.append(f"[{len(labels)+1}:a]{trim}adelay={delay_ms}|{delay_ms},volume={vol}[{label}]")
        labels.append(label)

    loudnorm = f"loudnorm=I={FINAL_TARGET_LUFS}:TP={FINAL_TARGET_TRUE_PEAK_DB}:LRA={FINAL_TARGET_LRA}"
    if not labels:
        # Nothing to place — just loudnorm-normalize the narration/music mix alone.
        cmd = ["ffmpeg", "-y", "-i", narration_path, "-af", loudnorm, out_path]
    else:
        mix_inputs = "[0:a]" + "".join(f"[{l}]" for l in labels)
        filter_complex = (";".join(filter_parts) +
                           f";{mix_inputs}amix=inputs={len(labels)+1}:duration=first:normalize=0,{loudnorm}[a]")
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
            f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TRUE_PEAK_DB}:LRA={TARGET_LRA}"
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