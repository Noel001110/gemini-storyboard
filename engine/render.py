"""engine.render — Visuelle Render-Pipeline (ffmpeg-basiert).

Enthält (Phase M.3, 2026-07-07):
    Konstanten:
        RENDER_FPS, RENDER_WIDTH, RENDER_HEIGHT, RENDER_SUPERSAMPLE_WIDTH
        WHISPER_VENV_PY, OVERLAY_SCRIPT
    Bibliotheken:
        MOTION_LIBRARY, _PACING_MOTION_CANDIDATES, _PHASE_MOTION_CANDIDATES,
        TRANSITION_LIBRARY
    Funktionen (visueller Render-Pfad):
        _probe_video_encoder         — Hardware-Encoder-Detect (h264_videotoolbox)
        _apply_sync_invariant        — Frame-exakte Sync (Architektur-kritisch)
        _build_motion                — Motion-Rezept skalieren
        _normalize_motion            — Altes Schema → neues Schema (Rückwärtskompat.)
        _motion_for_scene            — Regelbasierte Motion-Auswahl
        _overlay_specs_for_scene     — Overlay-Specs (caption/callout/counter/chapter)
        _render_clip                 — Ken-Burns-Clip rendern (Kernfunktion)
        _assemble_clips              — Concat-Demuxer
        _render_selfcheck            — Post-Render ffprobe-Checks
        _transition_for_scene        — Übergangsfamilie wählen
        _has_transition_before       — Schnittpunkt-Regel
        _clip_duration_sec           — ffprobe-Helper
        _crossfade_clips             — xfade-Filter
        render_text_overlay_png      — PNG-Overlay via .venv_whisper (Subprocess)
        render_title_card_png_via_venv — PNG Title-Card via .venv_whisper

    PNG-Helper wurden aus historischen Gründen aus dashboard.py herausgezogen, weil
    sie thematisch zum Render-Pfad gehören und engine.render self-contained sein
    soll.

NICHT hier (Phase M.4 — engine/audio.py):
    _mux_audio, _phase_modulate_music, _duck_music_under_voice, _place_sfx,
    _build_sfx_events, _build_final_audio, SFX_FILES, SOUND_ASSETS_DIR

BLEIBT in dashboard.py (Phase M.6 — Orchestrator):
    _render_worker                — Orchestrator (Koordiniert Render+Audio+Persistenz)
    _render_clip-Aufrufer

Externe Abhängigkeiten (lazy importiert, um Zyklen zu vermeiden):
    engine_elevenlabs.PHASE_COLOR_FILTER  — Phase-Color-Grading-Mapping
"""

from __future__ import annotations

import base64
import json
import os
import subprocess


# ── Constants ────────────────────────────────────────────────────────────────

RENDER_FPS = 30
RENDER_WIDTH = 1920
RENDER_HEIGHT = 1080
RENDER_SUPERSAMPLE_WIDTH = 3840  # scale-up before zoompan — without this, zoompan's
# per-frame rounding to whole pixels is visible as jitter on a slow zoom. 4K is enough
# to make that invisible at the zoom intensities used here (capped well under 1.2x)
# while costing only ~1/4 the memory/CPU of 8K supersampling.

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WHISPER_VENV_PY = os.path.join(HERE, ".venv_whisper", "bin", "python3")
OVERLAY_SCRIPT = os.path.join(HERE, "render_overlay.py")

_VIDEO_ENCODER = None  # cache for _probe_video_encoder()


# ── Public API ────────────────────────────────────────────────────────────────

__all__ = [
    "RENDER_FPS", "RENDER_WIDTH", "RENDER_HEIGHT", "RENDER_SUPERSAMPLE_WIDTH",
    "WHISPER_VENV_PY", "OVERLAY_SCRIPT",
    "MOTION_LIBRARY", "_PACING_MOTION_CANDIDATES", "_PHASE_MOTION_CANDIDATES",
    "TRANSITION_LIBRARY",
    "_probe_video_encoder", "_apply_sync_invariant",
    "_build_motion", "_normalize_motion", "_motion_for_scene",
    "_overlay_specs_for_scene",
    "_render_clip", "_assemble_clips", "_mux_audio", "_render_selfcheck",
    "_transition_for_scene", "_transition_after_hook", "_has_transition_before",
    "_clip_duration_sec", "_crossfade_clips", "_render_word_caption_sequence",
    "render_text_overlay_png", "render_title_card_png_via_venv",
]


# ── Motion-Vokabular ──────────────────────────────────────────────────────────
# Jeder Eintrag ist EIN generalisierter Zoom+Fokuspunkt-Verlauf: z0->z1 (Skalierung) und
# focus0->focus1 (welcher Bildpunkt zentriert wird), beide über dieselbe Smoothstep-Kurve
# interpoliert wie das bisherige Ken-Burns-Easing. Ein reiner Pan/Tilt ist einfach z0==z1
# (keine Skalierung) mit einem wandernden Fokuspunkt -- kein neuer ffmpeg-Filter, nur eine
# Verallgemeinerung des bereits vorhandenen zoompan-Ausdrucks. Pan/Tilt/Dolly/Diagonal
# brauchen alle einen LEICHTEN Zoom-Puffer (>1.0) über den ganzen Verlauf, sonst würde der
# Crop-Ausschnitt beim Wandern des Fokuspunkts über den Bildrand hinauslaufen.

MOTION_LIBRARY = {
    # Reiner Zoom: Fokuspunkt FEST, nur die Zoomstufe ändert sich. Feinschliff Runde 2
    # (Juli 2026): 1.12 -> 1.3 -- User-Vorgabe 120-140% GENAUSO für Zoom wie für Pan/Tilt.
    "zoom_in":        {"z0": 1.0,  "z1": 1.3,  "focus0": (0.5, 0.45),  "focus1": (0.5, 0.45)},
    "zoom_out":       {"z0": 1.3,  "z1": 1.0,  "focus0": (0.5, 0.45),  "focus1": (0.5, 0.45)},
    # Reiner Slide/Pan: Zoomstufe FEST bei 1.3 (Runde 1). Feinschliff Runde 2: die
    # Fokus-Wanderung war mit ±14% um die Mitte (0.64<->0.36) bei DIESEM Zoom-Level zu
    # groß -- derselbe absolute Weg wirkt im kleineren, gezoomten Ausschnitt schneller
    # als vorher bei 1.08. User-Feedback: "ganz langsam übers Bild wandeln", "nur leicht
    # zentriert aus der Mitte raus/rein". Wanderung auf ±8% (0.58<->0.42) reduziert --
    # spürbar langsamer UND bleibt immer nah der Mitte (nie nah am Bildrand).
    "pan_left":       {"z0": 1.3,  "z1": 1.3,  "focus0": (0.58, 0.48), "focus1": (0.42, 0.48)},
    "pan_right":      {"z0": 1.3,  "z1": 1.3,  "focus0": (0.42, 0.48), "focus1": (0.58, 0.48)},
    "tilt_up":        {"z0": 1.3,  "z1": 1.3,  "focus0": (0.5, 0.58),  "focus1": (0.5, 0.42)},
    "tilt_down":      {"z0": 1.3,  "z1": 1.3,  "focus0": (0.5, 0.42),  "focus1": (0.5, 0.58)},
    # Hook/Punchy-Spezialeffekt (Runde 1) -- bewusst kurz + energisch, NICHT Ziel dieses
    # Feedbacks, bleibt bei 1.16. Fokus fest -- reiner Zoom.
    "snap_zoom_in":   {"z0": 1.0,  "z1": 1.16, "focus0": (0.5, 0.45),  "focus1": (0.5, 0.45)},
    # Feinschliff Runde 2: "static" ist ab jetzt NUR noch der technische Fallback für
    # Szenen mit dur<1.2s (zu kurz für sichtbare Bewegung ohne Ruckeln) -- taucht in
    # KEINER stilistischen Auswahlliste mehr auf ("jede Szene braucht einen Effekt").
    "static":         {"z0": 1.02, "z1": 1.02, "focus0": (0.5, 0.5),   "focus1": (0.5, 0.5)},
}

# Schritt 4.3: Hook-Intensität 1.2 -> 1.0 (kein zusätzlicher Verstärkungsfaktor mehr --
# snap_zoom_in ist mit dem gesenkten z1=1.16 schon energisch genug für den Hook-Beat).
HOOK_MOTION_INTENSITY = 1.0

# Schritt 4.2: Gegenrichtung der jeweils VORHERIGEN Szene -- wird in
# _pick_motion_avoiding_reversal gemieden, damit zwei aufeinanderfolgende Szenen nicht
# als Ping-Pong wirken (pan_left direkt nach pan_right etc.). Bewegungen ohne
# Richtungs-Gegenstück (static/snap_zoom_in) haben keinen Eintrag.
_OPPOSITE_MOTION = {
    "pan_left": "pan_right", "pan_right": "pan_left",
    "tilt_up": "tilt_down", "tilt_down": "tilt_up",
    "zoom_in": "zoom_out", "zoom_out": "zoom_in",
}

# Schritt 4.1: regelbasierte Motion-Kandidaten aus dem Bild-Prompt-Text -- kein LLM-
# Call, reines Keyword-Matching auf dem Shot-Vokabular, das analyze_script bereits in
# jeden Prompt schreibt ("close-up", "wide shot", "top-down", ...). Reihenfolge ist
# Priorität: Dokument/Screen zuerst (Lesbarkeit schlägt Intimitäts-Zoom -- ein
# schwenkender Zoom über Bildschirmtext macht ihn unlesbar), dann Close-up, Wide,
# zuletzt der generische Portrait-Fallback.
_SHOT_HINT_RULES = [
    (("top-down", "document", "screen", "monitor", "touchscreen", "report", "paper"),
     ["tilt_down"]),
    (("close-up", "close up", "tight shot", "extreme close-up"),
     ["zoom_in"]),
    (("wide shot", "wide-angle", "wide angle", "establishing shot", "aerial", "bird's-eye"),
     ["pan_left", "pan_right"]),
    (("medium shot", "stands", "standing", "portrait"),
     ["zoom_in"]),
]

# Auswahl-Kandidaten nach `pacing` — vorbereitet für `phase` (Story-Phase-Engine):
# wenn scene.get("phase") künftig gesetzt ist, wird das bevorzugt, sonst fällt die
# Auswahl auf pacing zurück. Kein Zufall (Resume-Determinismus, ARCHITECTURE §13/§15.1)
# — Auswahl über scene["i"] % len(candidates).
_PACING_MOTION_CANDIDATES = {
    "calm":   ["pan_left", "pan_right", "tilt_up", "tilt_down", "zoom_out"],
    "normal": ["zoom_in", "zoom_out", "tilt_up", "pan_left", "pan_right"],
    "punchy": ["snap_zoom_in", "zoom_in"],
}

_PHASE_MOTION_CANDIDATES = {
    "OPENING":       ["pan_right", "pan_left", "tilt_down"],
    "RISING_ACTION": ["zoom_in", "tilt_down"],
    "CLIMAX":        ["snap_zoom_in", "zoom_in"],
    "RESOLUTION":    ["zoom_out", "tilt_up", "pan_left"],
}


# ── Übergangs-Bibliothek ──────────────────────────────────────────────────────
# Kuratierte Übergangs-Bibliothek: ffmpegs xfade-Filter bringt bereits 58 fertige
# Übergangstypen mit (kein neues Paket, keine eigene Easing-Formel nötig für diesen
# Teil). Mehrere Richtungsvarianten pro Familie (damit ein längeres Video nicht monoton
# wirkt); welche Familie greift, ist regelbasiert aus pacing/phase abgeleitet.
#   - "calm"   -> sanftes Dissolve/Fade, kein SFX, LANGSAMER (0.8s "linger")
#   - "punchy" -> energischer Wipe, mit Whoosh, SCHNELLER (0.3s "snappy")
#   - sonst    -> neutraler Smooth-Übergang, unveränderte 0.5s
# Feinschliff Runde 2: wipe/smooth von 2 auf 4 Typen erweitert (zusätzlich up/down) --
# mehr Varietät ohne den Charakter der Familie zu ändern. fade bleibt bei 2 (fade/
# dissolve sind die einzigen sanften, zur "calm"-Familie passenden xfade-Varianten).
TRANSITION_LIBRARY = {
    "fade":   {"types": ["fade", "dissolve"],
               "sfx": None,     "duration": 0.8},
    "wipe":   {"types": ["wipeleft", "wiperight", "wipeup", "wipedown"],
               "sfx": "whoosh", "duration": 0.3},
    "smooth": {"types": ["smoothleft", "smoothright", "smoothup", "smoothdown"],
               "sfx": "whoosh", "duration": 0.5},
}


# ── Encoder-Detect (cached) ──────────────────────────────────────────────────

def _probe_video_encoder() -> tuple:
    """Returns (encoder_name, extra_ffmpeg_args). Checked once, cached — h264_videotoolbox
    (Apple Silicon hardware encoder) is roughly 4x faster than libx264 and only lightly
    loads the CPU, important since the Python server runs alongside during a render. It
    needs an explicit quality flag or the default output is visibly soft. Falls back to
    libx264 if videotoolbox isn't available on this machine.
    """
    global _VIDEO_ENCODER
    if _VIDEO_ENCODER is not None:
        return _VIDEO_ENCODER
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                              capture_output=True, text=True, timeout=10)
        if "h264_videotoolbox" in out.stdout:
            _VIDEO_ENCODER = ("h264_videotoolbox", ["-q:v", "65"])
        else:
            _VIDEO_ENCODER = ("libx264", ["-preset", "medium", "-crf", "20"])
    except Exception:
        _VIDEO_ENCODER = ("libx264", ["-preset", "medium", "-crf", "20"])
    print(f"  [Render] Video-Encoder: {_VIDEO_ENCODER[0]}", flush=True)
    return _VIDEO_ENCODER


def _apply_sync_invariant(scenes: list, audio_duration: float, fps: int) -> list:
    """Two sequential steps — the second does NOT replace the first, both always run:

    1) Linear normalization (seconds, float): scale every scene's `dur` so their sum
       matches the real audio_duration exactly. Spreads the usually-small WPM-estimate
       error invisibly across all scenes.
    2) Integer-frame rounding (frames, int): converts the now-normalized durations to
       exact frame counts, with the LAST scene absorbing the rounding remainder, so
       sum(frames) == round(audio_duration * fps) EXACTLY. This is what actually
       prevents the tail-clipping/drift bug class (MoneyPrinterTurbo issue #985).

    Returns a list of per-scene frame counts, same order/length as `scenes`.

    Juli 2026 Fix (Präzisions-Boost, nur wenn ALLE Szenen `start_aligned` haben): die
    alte `scene_dur` nahm nur `end_aligned - start_aligned` (reine Sprechzeit OHNE die
    Pause danach). `total_dur` war dadurch systematisch kleiner als `audio_duration`
    (Summe aller Zwischenpausen fehlte), also `factor > 1.0` — JEDE Szene wurde um
    diesen Faktor gestreckt, auch wenn ihre eigene Pause winzig war. Der Schnittpunkt
    vor Szene N verschob sich damit von der tatsächlichen Startzeit des N-ten Worts weg,
    akkumuliert über die ganze Szenenliste (der eigentliche Sync-Drift, den der User
    beobachtet hat — nicht der bereits gefixte Chunk-Offset).

    Fix: wenn ALLE Szenen aligned sind, ist die Dauer einer Szene die Zeit bis zum
    NÄCHSTEN `start_aligned` (schließt die Pause danach ein), letzte Szene bis zum
    Audio-Ende. Die Summe telescopiert dann zu `audio_duration - scenes[0].start_aligned`
    — bei einer typischen, sehr kurzen Anfangs-Stille also `factor ≈ 1.0`. Die
    kumulierte Position vor Szene N entspricht dann fast exakt `scenes[N].start_aligned`
    — der Schnitt sitzt auf dem Wort, nicht proportional verschoben.

    Fällt zurück auf die alte (sprechzeit-basierte) Berechnung, wenn auch nur eine Szene
    kein `start_aligned` hat (z.B. Whisper-Teilabdeckung) — dort ist "welche Pause gehört
    zu welcher Szene" nicht zuverlässig bekannt, das Risiko einer Fehl-Zuordnung wäre
    größer als der Gewinn.
    """
    def scene_dur_speech_only(s):
        sa, ea = s.get("start_aligned"), s.get("end_aligned")
        if sa is not None and ea is not None and ea > sa:
            return ea - sa
        return s.get("dur", 0)

    all_aligned = bool(scenes) and all(s.get("start_aligned") is not None for s in scenes)
    if all_aligned:
        starts = [s["start_aligned"] for s in scenes]
        durations = []
        for i in range(len(scenes)):
            if i + 1 < len(scenes):
                durations.append(max(0.1, starts[i + 1] - starts[i]))
            else:
                durations.append(max(0.1, audio_duration - starts[i]))
    else:
        durations = [scene_dur_speech_only(s) for s in scenes]

    total_dur = sum(durations) or 1.0
    factor = audio_duration / total_dur
    normalized = [max(0.1, d * factor) for d in durations]

    audio_frames = round(audio_duration * fps)
    frames = [round(d * fps) for d in normalized]
    if frames:
        frames[-1] += audio_frames - sum(frames)
        frames[-1] = max(1, frames[-1])
    return frames


# ── Motion (Auswahl + Skalierung + Normalisierung) ───────────────────────────

def _build_motion(name: str, intensity_scale: float = 1.0) -> dict:
    """Scales a MOTION_LIBRARY recipe's movement AROUND ITS OWN MIDPOINT — intensity_scale
    == 1.0 reproduces the base recipe exactly, <1 dampens (short scene, subtle), >1
    amplifies (long scene, fuller movement) — without changing which direction it moves in."""
    base = MOTION_LIBRARY[name]
    z_mid = (base["z0"] + base["z1"]) / 2
    fx_mid = (base["focus0"][0] + base["focus1"][0]) / 2
    fy_mid = (base["focus0"][1] + base["focus1"][1]) / 2
    z0 = z_mid + (base["z0"] - z_mid) * intensity_scale
    z1 = z_mid + (base["z1"] - z_mid) * intensity_scale
    fx0 = fx_mid + (base["focus0"][0] - fx_mid) * intensity_scale
    fy0 = fy_mid + (base["focus0"][1] - fy_mid) * intensity_scale
    fx1 = fx_mid + (base["focus1"][0] - fx_mid) * intensity_scale
    fy1 = fy_mid + (base["focus1"][1] - fy_mid) * intensity_scale
    return {"name": name, "z0": round(z0, 4), "z1": round(z1, 4),
            "focus0": [round(fx0, 4), round(fy0, 4)], "focus1": [round(fx1, 4), round(fy1, 4)]}


def _normalize_motion(motion: dict) -> dict:
    """Accepts either the pre-existing {"type","z_end","focus"} shape (zoom_in/zoom_out/
    static only, from before the motion vocabulary existed) or the current {"name","z0",
    "z1","focus0","focus1"} shape, and always returns the current shape. Old plans with
    already-rendered `scene["motion"]` keep working without a migration step (ARCHITECTURE
    §11 rule: additive, no schema-version bump).
    """
    if "z0" in motion:
        return motion
    z_end = motion.get("z_end", 1.02)
    mtype = motion.get("type", "static")
    focus = motion.get("focus", [0.5, 0.5])
    z0, z1 = (z_end, 1.0) if mtype == "zoom_out" else (1.0, z_end) if mtype == "zoom_in" else (z_end, z_end)
    return {"name": mtype, "z0": z0, "z1": z1, "focus0": focus, "focus1": focus}


def _shot_hint_from_prompt(prompt: str) -> list | None:
    """Schritt 4.1: regelbasierte Motion-Kandidaten aus dem Bild-Prompt-Text (Keyword-
    Matching gegen _SHOT_HINT_RULES, kein LLM-Call). Motiviert die Kamerabewegung am
    tatsächlichen INHALT der Szene statt am reinen Szenenindex. None, wenn kein
    Keyword trifft — Aufrufer fällt dann auf Phase/Pacing zurück."""
    if not prompt:
        return None
    p = prompt.lower()
    for keywords, candidates in _SHOT_HINT_RULES:
        if any(kw in p for kw in keywords):
            return candidates
    return None


def _pick_motion_avoiding_reversal(candidates: list, seed: int, prev_scene: dict | None) -> str:
    """Schritt 4.2: wählt `candidates[seed % len(candidates)]`, überspringt diesen
    Kandidaten aber, wenn er die exakte Gegenrichtung der VORHERIGEN Szene ist
    (_OPPOSITE_MOTION) — verhindert den sichtbarsten "wirkt randomisiert"-Fall:
    pan_left direkt nach pan_right, etc. Bleibt deterministisch (gleiche Eingaben ->
    gleiche Ausgabe, ARCHITECTURE §13/§15.1) — probiert einfach die nächsten
    Kandidaten in der bereits deterministischen Rotation durch."""
    prev_motion = prev_scene.get("motion") if prev_scene else None
    prev_name = _normalize_motion(prev_motion).get("name") if prev_motion else None
    forbidden = _OPPOSITE_MOTION.get(prev_name) if prev_name else None
    n = len(candidates)
    for offset in range(n):
        name = candidates[(seed + offset) % n]
        if name != forbidden:
            return name
    return candidates[seed % n]  # alle Kandidaten sind die Gegenrichtung -- gibt es bei den aktuellen Listen nicht (>=2 Einträge, nicht alle dieselbe Achse), Sicherheitsnetz


def _motion_for_scene(scene: dict, prev_scene: dict) -> dict:
    """Rule-based motion recipe — no LLM call. Sequence continuations (seq_pos >= 1)
    inherit the previous scene's motion (continuity = one camera move per sequence).
    Otherwise, priority order (Schritt 4.1): Prompt-Shot-Hint > Phase-Kandidaten
    (LLM-driven) > Pacing-Fallback (position-based / legacy). Feinschliff Runde 2
    (User-Feedback): JEDE Szene bekommt einen echten Ken-Burns-Effekt -- die frühere
    "jede 3.-4. Szene bleibt static"-Regel ist entfernt, "static" ist nur noch der
    technische dur<1.2s-Fallback unten. Die finale Auswahl vermeidet zusätzlich die
    Gegenrichtung der Vorszene (Schritt 4.2, _pick_motion_avoiding_reversal).

    Phase L: Hook-Szenen (scene['is_hook'] = True) erzwingen snap_zoom_in — AUSSER die
    Szene ist Fortsetzung einer Sequenz (CINEMATIC_UPGRADE_PLAN.md §11.3 Schutzregel 2:
    Motion-Vererbung schlägt jede neue Motion-Regel). Hook gewinnt nur, wenn die Szene
    einen eigenen Look hat (= Anker einer Sequenz oder eigenständige Szene).
    """
    dur = scene.get("dur", 3.0)
    if dur < 1.2:
        return _build_motion("static", 1.0)

    is_seq_continuation = (
        scene.get("seq_id") is not None
        and scene.get("seq_pos", 0) >= 1
        and prev_scene
        and prev_scene.get("motion")
    )

    # Phase L Hook-Override (Schutzregel 2: Sequenz-Vererbung schlägt Hook)
    if scene.get("is_hook") and not is_seq_continuation:
        return _build_motion("snap_zoom_in", HOOK_MOTION_INTENSITY)

    if is_seq_continuation:
        name = _normalize_motion(prev_scene["motion"]).get("name", "zoom_in")
    else:
        i = scene.get("i", 0)
        prompt_hint = _shot_hint_from_prompt(scene.get("prompt", ""))
        if prompt_hint:
            candidates = prompt_hint
        else:
            phase = scene.get("phase")
            pacing = scene.get("pacing") if scene.get("pacing") in _PACING_MOTION_CANDIDATES else "normal"
            if phase and scene.get("phase_source") == "llm" and phase in _PHASE_MOTION_CANDIDATES:
                candidates = _PHASE_MOTION_CANDIDATES[phase]
            else:
                candidates = _PACING_MOTION_CANDIDATES[pacing]
        name = _pick_motion_avoiding_reversal(candidates, i, prev_scene)

    intensity_scale = min(0.5 + dur * 0.12, 1.4)
    return _build_motion(name, intensity_scale)


# ── Overlay-Specs ─────────────────────────────────────────────────────────────

def _overlay_specs_for_scene(scene: dict, clip_dur: float, overlay_opts: dict | None) -> list:
    """Decides which text overlays (if any) apply to this scene and their on-screen
    window. Returns a list of (style, text, t0, t1) tuples, evaluated in the order they
    should be layered (chapter title first/bottom-most, caption last/top-most is NOT
    required here since they occupy different screen regions and never overlap).

    Phase N: data_visual (additive, kein Ersatz für callout) — wenn analyze_script
    data_visual erkennt, wird ein animierter Counter-Overlay gerendert. Statischer callout
    bleibt Fallback, falls data_visual fehlt.
    """
    if not overlay_opts:
        return []
    specs = []
    if overlay_opts.get("chapters") and scene.get("seq_pos") == 0 and scene.get("seq_reason"):
        specs.append(("chapter", scene["seq_reason"], 0.0, min(2.0, clip_dur)))
    # Phase N: data_visual hat Vorrang vor statischem callout (Counter-Anim)
    if scene.get("data_visual") and scene["data_visual"].get("kind") == "counter":
        dv = scene["data_visual"]
        anim_dur = min(1.5, clip_dur - 0.2) if clip_dur > 0.5 else clip_dur
        if anim_dur > 0.3:
            specs.append(("counter_anim", dv, 0.1, 0.1 + anim_dur))
    elif overlay_opts.get("callouts") and scene.get("callout"):
        # Phase F: punchy + callout → switch to the dramatic "counter" style (big number,
        # centered, red letter-fill) instead of the standard callout.
        if scene.get("pacing") == "punchy":
            counter_t0 = min(0.1, clip_dur * 0.15)
            counter_t1 = min(1.2, clip_dur - 0.05) if clip_dur > 0.3 else clip_dur
            if counter_t1 > counter_t0:
                specs.append(("counter", scene["callout"], counter_t0, counter_t1))
        else:
            t0 = min(0.2, clip_dur * 0.1)
            t1 = min(1.6, clip_dur - 0.05) if clip_dur > 0.3 else clip_dur
            if t1 > t0:
                specs.append(("callout", scene["callout"], t0, t1))
    if overlay_opts.get("captions"):
        # Cinematic-Mix Juli 2026 (Schritt 3): CapCut-Stil 1-Wort-Captions, wenn die
        # Szene Wort-Slices hat (align_scenes_to_whisper, ElevenLabs/Whisper-Pfad nach
        # erfolgtem Alignment). Fallback auf die alte Voll-Text-Bauchbinde, wenn
        # `words` fehlt (geschätztes Timing, alte resumte Pläne ohne Re-Alignment).
        if scene.get("words"):
            specs.append(("word_caption_seq", scene["words"], 0.0, clip_dur))
        elif scene.get("text"):
            specs.append(("caption", scene["text"], 0.0, clip_dur))
    return specs


# ── Render-Funktionen (ffmpeg) ───────────────────────────────────────────────

def _render_clip(img_path: str, scene: dict, out_path: str, fps: int = RENDER_FPS,
                  overlay_opts: dict | None = None) -> None:
    """Renders one scene's still image into a short Ken-Burns clip, optionally with text
    overlays composited on top. Resume-safe: skips if out_path exists and non-empty.

    Feinschliff Runde 2 (User-Feedback "Akt-Einspieler müssen raus"): `kind=="title_card"`
    wird hier NICHT mehr als Sonderfall behandelt -- solche Szenen rendern wie jede
    andere, mit ihrem bereits vorhandenen echten Bild (`scene["file"]`) statt einer live
    erzeugten weißen PIL-Titelkarte. `render_title_card_png_via_venv` bleibt im Modul
    erhalten (dormant, reaktivierbar), wird von hier aus nur nicht mehr aufgerufen.
    """
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return
    # Lazy-import to avoid cycles (engine_elevenlabs is a higher-level module)
    from engine_elevenlabs import PHASE_COLOR_FILTER

    motion = _normalize_motion(scene.get("motion") or {"type": "static", "z_end": 1.02, "focus": [0.5, 0.5]})
    frames = max(1, scene.get("_frames") or round(scene.get("dur", 3.0) * fps))
    z0, z1 = motion["z0"], motion["z1"]
    fx0, fy0 = motion["focus0"]
    fx1, fy1 = motion["focus1"]
    clip_dur = frames / fps

    # Smoothstep easing (3t²-2t³), built purely from the frame index `on`/`frames
    smoothstep = f"(3*pow(on/{frames},2)-2*pow(on/{frames},3))"
    # Phase O: Wort-Akzent-Puls (Plan §4.4) — gaußscher Zoom-Puls auf accent_t.
    # Nur wenn scene["accent_t"] gesetzt (vom _render_worker nach Whisper-Alignment).
    # amp=0.05 (+5% Zoom-Peak, unter 1.2x-Jitter-Grenze aus MOTION_LIBRARY),
    # sigma=0.06s (~0.2s sichtbare Puls-Breite bei 30fps).
    accent_t = scene.get("accent_t")
    if accent_t is not None and accent_t > 0 and clip_dur > 2.0:
        f_a = round(accent_t * fps)
        sigma = max(2, round(0.06 * fps))
        amp = 0.05
        # +amp*exp(-pow((on-f_a)/sigma,2)) — symmetrischer Gauß, glatt beidseitig
        z_expr = (f"{z0}+({z1}-{z0})*{smoothstep}"
                  f"+{amp}*exp(-pow((on-{f_a})/{sigma},2))")
    else:
        z_expr = f"{z0}+({z1}-{z0})*{smoothstep}"
    fx_expr = f"({fx0}+({fx1}-{fx0})*{smoothstep})"
    fy_expr = f"({fy0}+({fy1}-{fy0})*{smoothstep})"
    x_expr = f"(iw*{fx_expr})-(iw/zoom/2)"
    y_expr = f"(ih*{fy_expr})-(ih/zoom/2)"

    overlay_specs = _overlay_specs_for_scene(scene, clip_dur, overlay_opts)
    encoder, encoder_args = _probe_video_encoder()
    inputs = ["-loop", "1", "-i", img_path]

    # Phase D + Phase P: phase-aware color-grading applied AFTER zoompan.
    # Plan §0 + §3: colorbalance (Papier-Tönung) statt eq (für Tusche-Look effektiver),
    # + vignette für CLIMAX (dezent, PI/5-Bereich). Bei CLIMAX hängen wir den
    # Vignette-Filter mit Komma verkettet an colorbalance (ffmpeg-Filtergraph).
    color_filter = PHASE_COLOR_FILTER.get(scene.get("phase", ""), "")
    if scene.get("phase") == "CLIMAX":
        # Vignette nur für CLIMAX, dezent (PI/5 = ~36° Vignette-Winkel)
        color_filter = f"{color_filter},vignette=PI/5" if color_filter else "vignette=PI/5"
    eq_suffix  = f",{color_filter}" if color_filter else ""
    filter_parts = [
        f"[0:v]scale={RENDER_SUPERSAMPLE_WIDTH}:-2,"
        f"zoompan=z='{z_expr}':d={frames}:x='{x_expr}':y='{y_expr}':"
        f"s={RENDER_WIDTH}x{RENDER_HEIGHT}:fps={fps},setsar=1"
        f"{eq_suffix}[base]"
    ]
    overlay_pngs = []
    overlay_seq_dirs = []  # Phase N: temp dirs mit PNG-Sequenzen
    last_label = "base"
    try:
        for idx, (style, text, t0, t1) in enumerate(overlay_specs):
            png_path = f"{out_path}.ov{idx}.png"
            # Phase N: counter_anim nutzt PNG-Sequenz statt statisches PNG
            if style == "counter_anim":
                # text ist hier das data_visual-dict (siehe _overlay_specs_for_scene)
                dv = text
                seq_dir = f"{out_path}.ovseq{idx}"
                n_frames = max(2, int(round((t1 - t0) * fps)))
                from_val = float(dv.get("from", 0))
                to_val = float(dv.get("to", 0))
                fmt = str(dv.get("format", "{:.1f}"))
                label = str(dv.get("label", ""))
                _render_counter_anim_sequence(
                    seq_dir, RENDER_WIDTH, RENDER_HEIGHT,
                    from_val, to_val, n_frames, fmt, label,
                )
                overlay_seq_dirs.append(seq_dir)
                # ffmpeg-Input: PNG-Sequenz mit vorgegebener Framerate
                inputs += ["-framerate", str(fps), "-i", f"{seq_dir}/ov_%04d.png"]
                in_idx = idx + 1
                # Sequenz hat eigene Timing-Semantik: erstes Frame = Szene t0,
                # letztes Frame = Szene t1. ffmpeg wiederholt die Sequenz für
                # längere Szenen via -stream_loop, bzw. stoppt wenn clip_dur
                # überschritten — die Dauer hier ist bewusst = n_frames/fps.
                faded_label = f"ov{idx}f"
                # Kurzer Fade-in am Anfang (0.1s)
                fade_dur = 0.1
                filter_parts.append(
                    f"[{in_idx}:v]format=rgba,"
                    f"fade=t=in:st={t0}:d={fade_dur}:alpha=1"
                    f"[{faded_label}]"
                )
            elif style == "word_caption_seq":
                # Schritt 3: text ist hier scene["words"] (siehe _overlay_specs_for_scene).
                # EIN Sequenz-Input für die ganze Szene, unabhängig von der Wortzahl --
                # gleiches Input-Muster wie counter_anim, aber OHNE Fade: der CapCut-Pop
                # lebt vom harten, instant Wort-Wechsel (Plan-Vorgabe).
                words = text
                seq_dir = f"{out_path}.ovseq{idx}"
                _render_word_caption_sequence(seq_dir, RENDER_WIDTH, RENDER_HEIGHT,
                                               words, t1 - t0, fps)
                overlay_seq_dirs.append(seq_dir)
                inputs += ["-framerate", str(fps), "-i", f"{seq_dir}/seq_%04d.png"]
                in_idx = idx + 1
                faded_label = f"ov{idx}f"
                filter_parts.append(f"[{in_idx}:v]format=rgba[{faded_label}]")
            else:
                render_text_overlay_png(png_path, RENDER_WIDTH, RENDER_HEIGHT, style, text)
                overlay_pngs.append(png_path)
                inputs += ["-loop", "1", "-i", png_path]
                in_idx = idx + 1
                fade_dur = min(0.3, max(0.05, (t1 - t0) / 4))
                fade_out_st = max(t0, t1 - fade_dur)
                faded_label = f"ov{idx}f"
                filter_parts.append(
                    f"[{in_idx}:v]format=rgba,"
                    f"fade=t=in:st={t0}:d={fade_dur}:alpha=1,"
                    f"fade=t=out:st={fade_out_st}:d={fade_dur}:alpha=1[{faded_label}]"
                )
            is_last = idx == len(overlay_specs) - 1
            next_label = "outv" if is_last else f"comp{idx}"
            # overlay mit enable='between(t,t0,t1)' — gilt für beide Fälle
            # Bei counter_anim: Sequenz wird ohnehin nur t1-t0 lang gezeigt,
            # davor/nachher ist die PNG transparent → kein sichtbarer Effekt.
            filter_parts.append(
                f"[{last_label}][{faded_label}]overlay=enable='between(t,{t0},{t1})'[{next_label}]"
            )
            last_label = next_label
        map_label = last_label if overlay_specs else "base"

        cmd = [
            "ffmpeg", "-y", *inputs,
            "-t", str(clip_dur), "-r", str(fps),
            "-filter_complex", ";".join(filter_parts),
            "-map", f"[{map_label}]",
            "-c:v", encoder, *encoder_args,
            "-pix_fmt", "yuv420p", "-video_track_timescale", "90000",
            out_path,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg Clip-Render fehlgeschlagen ({os.path.basename(img_path)}): "
                                f"{result.stderr.decode(errors='replace')[-300:]}")
    finally:
        for p in overlay_pngs:
            try: os.remove(p)
            except: pass
        for d in overlay_seq_dirs:
            import shutil
            try: shutil.rmtree(d, ignore_errors=True)
            except: pass

def _assemble_clips(clip_paths: list, out_path: str) -> None:
    """concat-demuxer, hard cuts only (V1 decision — crossfades are later polish, need
    a full filter_complex re-encode instead of a lossless -c copy). Requires all clips
    to share codec/resolution/fps/timescale, which _render_clip's fixed recipe guarantees.
    """
    list_path = out_path + ".txt"
    with open(list_path, "w") as f:
        for p in clip_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")
    try:
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", out_path]
        result = subprocess.run(cmd, capture_output=True, timeout=180)
    finally:
        os.remove(list_path)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg Zusammenschnitt fehlgeschlagen: {result.stderr.decode(errors='replace')[-300:]}")


def _mux_audio(silent_path: str, audio_path: str, out_path: str) -> None:
    """Final mux: one continuous voiceover track over the assembled silent video.
    -af apad pads the audio with a small buffer if it's a touch shorter than the video —
    a safety net ON TOP of the integer-frame sync invariant (_apply_sync_invariant), not
    a replacement for it; if there's still a residual mismatch, the audio gets padded
    rather than the video getting truncated. -movflags +faststart moves the MP4 metadata
    to the front so the <video> preview can start playing before the whole file has
    downloaded — without it the browser waits for the complete file first."""
    cmd = ["ffmpeg", "-y", "-i", silent_path, "-i", audio_path,
           "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
           "-af", "apad=pad_dur=0.3", "-shortest", "-movflags", "+faststart", out_path]
    result = subprocess.run(cmd, capture_output=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg Audio-Mux fehlgeschlagen: {result.stderr.decode(errors='replace')[-300:]}")


def _render_selfcheck(final_path: str, expected_audio_duration: float) -> dict:
    """Post-render ffprobe checks: a silent success that's actually broken (truncated
    video, no audio track) must not be reported as done.
    """
    checks = {"duration_ok": False, "audio_ok": False, "frames_ok": False}
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                               "-of", "csv=p=0", final_path], capture_output=True, text=True, timeout=15)
        video_dur = float(out.stdout.strip())
        checks["duration_ok"] = abs(video_dur - expected_audio_duration) < 0.5
    except Exception:
        pass
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                               "-show_entries", "stream=codec_type", "-of", "csv=p=0", final_path],
                              capture_output=True, text=True, timeout=15)
        checks["audio_ok"] = "audio" in out.stdout
    except Exception:
        pass
    checks["frames_ok"] = os.path.exists(final_path) and os.path.getsize(final_path) > 0
    return checks


# ── Transition ────────────────────────────────────────────────────────────────

def _transition_for_scene(scene: dict, transition_seq_idx: int) -> tuple:
    """Wählt Übergangstyp + passendes SFX (oder None) + Übergangsdauer für den Schnitt
    VOR `scene`.

    `transition_seq_idx` ist NICHT der Szenenindex, sondern die laufende Nummer
    INNERHALB der tatsächlichen Übergangs-Punkte (Position in der `transition_at`-
    Liste des Callers: 0, 1, 2, …). Feinschliff Runde 2 (User-Feedback "Schnittmuster
    wiederholt sich"): vorher hing die Sub-Typ-Rotation am ROHEN Szenenindex
    (`scene["i"] % len(types)`) -- da Übergänge nur an seltenen, unregelmäßig
    verteilten Sequenzgrenzen feuern, konnten mehrere davon zufällig dieselbe
    Index-Parität teilen und so denselben Sub-Typ mehrmals in Folge auslösen. Über
    `transition_seq_idx` alterniert die Sub-Typ-Folge jetzt garantiert strikt über die
    gesamte Video-Laufzeit, unabhängig von den absoluten Szenenindizes.

    Family-Pick-Lookup:
    - scene.phase_source == "llm" mit phase → CLIMAX=wipe, OPENING/RESOLUTION=fade,
      RISING_ACTION=smooth (Phase hat Vorrang vor Pacing).
    - sonst: Pacing-Heuristik.

    Phase L: Wenn die Szene VOR scene[idx] (also scene[idx-1]) eine Hook-Szene war,
    erzwingen wir einen hard cut (kein weicher Fade aus dem Hook raus — der Hook-Beat
    muss wie ein Schlag sitzen). Wir wissen hier nur die aktuelle Szene; der Caller
    übergibt die scenes-Liste via prev_is_hook-Logik im Render-Worker.
    Aktuell: keine API-Änderung — Hook-Übergang wird über die Library hart gesteuert
    via xfade mit duration=0, was effektiv ein hartes Schneiden ist.
    """
    phase = scene.get("phase", "")
    phase_source = scene.get("phase_source", "")
    pacing = scene.get("pacing", "normal")
    if phase and phase_source == "llm":
        if phase == "CLIMAX":
            family = "wipe"
        elif phase in ("OPENING", "RESOLUTION"):
            family = "fade"
        else:
            family = "smooth"
    else:
        family = "fade"  if pacing == "calm"  else \
                 "wipe"  if pacing == "punchy" else \
                 "smooth"
    lib = TRANSITION_LIBRARY[family]
    transition_type = lib["types"][transition_seq_idx % len(lib["types"])]
    return transition_type, lib["sfx"], lib["duration"]


def _transition_after_hook(prev_scene: dict) -> tuple:
    """Phase L: Übergang NACH einer Hook-Szene → immer hard cut (kurz, kein Fade).

    Wird vom Render-Worker aufgerufen statt _transition_for_scene, wenn die
    vorherige Szene is_hook war. Hard cut = xfade mit duration=0 (effektiv Schneiden).
    """
    return ("fade", None, 0.0)  # 0.0s = instant cut, kein SFX (Hook-Szene hat schon Aufmerksamkeit)


def _has_transition_before(scenes: list, idx: int) -> bool:
    """Identische Regel wie das Whoosh-SFX-Ereignis — Bild-Übergang und Whoosh-Sound
    müssen auf demselben Schnitt sitzen. True wenn diese Szene der Anker einer Sequenz
    ist UND die unmittelbar vorherige Szene einer anderen Sequenz angehört."""
    if idx == 0:
        return False
    s, prev = scenes[idx], scenes[idx - 1]
    return s.get("seq_id") is not None and s.get("seq_pos", 0) == 0 and prev.get("seq_id") != s.get("seq_id")


def _clip_duration_sec(path: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                           "-of", "csv=p=0", path], capture_output=True, text=True, timeout=15)
    return float(out.stdout.strip())


def _crossfade_clips(clip_a: str, clip_b: str, out_path: str, duration: float, transition_type: str = "fade") -> None:
    """Merges two already-rendered clips into one with a crossfade at the boundary.
    `clip_a` MUST have been rendered with `duration` extra seconds of Ken-Burns motion
    tacked onto its planned length beforehand (see _render_worker's transition_frames
    compensation) — the crossfade consumes exactly that overlap.
    """
    dur_a = _clip_duration_sec(clip_a)
    offset = max(0.0, dur_a - duration)
    encoder, encoder_args = _probe_video_encoder()
    cmd = ["ffmpeg", "-y", "-i", clip_a, "-i", clip_b,
           "-filter_complex", f"[0:v][1:v]xfade=transition={transition_type}:duration={duration}:offset={offset}[v]",
           "-map", "[v]", "-c:v", encoder, *encoder_args,
           "-pix_fmt", "yuv420p", "-video_track_timescale", "90000", out_path]
    result = subprocess.run(cmd, capture_output=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg Übergang fehlgeschlagen: {result.stderr.decode(errors='replace')[-300:]}")


# ── PNG-Helper (via .venv_whisper Subprocess) ─────────────────────────────────

def _render_counter_anim_sequence(out_dir, width, height, from_val, to_val, n_frames, fmt, label):
    """Phase N.1: ruft render_overlay.py im counter_anim-Modus auf und erzeugt
    n_frames PNG-Dateien (ov_0000.png ... ov_{n-1:04d}.png).

    Sequenz wird vom ffmpeg-Schritt in _render_clip via
    `-framerate {fps} -i {out_dir}/ov_%04d.png` eingelesen.
    """
    fmt_b64 = base64.b64encode(fmt.encode("utf-8")).decode("ascii")
    label_b64 = base64.b64encode(label.encode("utf-8")).decode("ascii")
    args = [
        WHISPER_VENV_PY, OVERLAY_SCRIPT, out_dir,
        str(width), str(height), "counter_anim",
        fmt_b64,
        str(from_val), str(to_val), str(n_frames), label_b64,
    ]
    result = subprocess.run(args, capture_output=True, text=True, timeout=90)
    if result.returncode != 0:
        raise RuntimeError(f"Counter-Anim-Sequenz fehlgeschlagen: {result.stderr[-500:]}")


def _render_word_caption_sequence(out_dir: str, width: int, height: int, words: list,
                                   clip_dur: float, fps: int) -> None:
    """Cinematic-Mix Juli 2026 (Schritt 3): baut EINE durchgehende Frame-Sequenz für
    die 1-Wort-Captions einer Szene — ein einziger `-framerate {fps} -i seq_%04d.png`-
    Input in _render_clip, unabhängig davon wie viele Wörter die Szene hat (gleiches
    Muster wie counter_anim, aber hier codiert die Sequenz DISKRETE Sichtbarkeits-
    Fenster statt einer kontinuierlichen Animation).

    Ablauf:
      1. EIN Subprocess-Aufruf rendert die N Wort-PNGs (word_caption_batch) — die
         teure Python-Start+Pillow-Kosten fallen pro SZENE an, nicht pro Wort.
      2. Für jedes Ausgabe-Frame [0, round(clip_dur*fps)) wird bestimmt, welches Wort
         "aktiv" ist, und das passende PNG per Symlink unter seq_%04d.png eingehängt.
         Wort i ist sichtbar von seinem eigenen `start` bis zum `start` des nächsten
         Worts (oder bis clip_dur beim letzten Wort) — ABSICHTLICH lückenlos: "Jedes
         Wort steht, bis das nächste kommt" (ruhiger als Blinken, auch über kurze
         Pausen hinweg). Nur VOR dem ersten Wort (falls dessen `start` > 0) bleibt der
         Frame leer/transparent.
      3. Ein einziges vorgerendertes transparentes Blank-PNG deckt diese Lücke ab.

    Wörter, deren `start` bei/über `clip_dur` liegt (Rundungsrand am Szenenende),
    werden ignoriert — ihr Text wäre ohnehin nicht mehr sichtbar.
    """
    os.makedirs(out_dir, exist_ok=True)
    n_frames = max(1, round(clip_dur * fps))
    usable = [w for w in words if w.get("start", 0.0) < clip_dur]
    if not usable:
        # Keine Wörter in diesem Clip-Fenster -- ein einziges Blank-Frame reicht,
        # ffmpeg wiederholt das letzte Bild einer -framerate-Sequenz nicht automatisch,
        # also müssen wir trotzdem n_frames Kopien/Symlinks anlegen.
        blank_path = os.path.join(out_dir, "blank.png")
        _render_word_caption_blank(blank_path, width, height)
        for f in range(n_frames):
            os.symlink(blank_path, os.path.join(out_dir, f"seq_{f:04d}.png"))
        return

    words_b64 = base64.b64encode(json.dumps([w["word"] for w in usable]).encode("utf-8")).decode("ascii")
    args = [WHISPER_VENV_PY, OVERLAY_SCRIPT, out_dir, str(width), str(height),
            "word_caption_batch", words_b64]
    result = subprocess.run(args, capture_output=True, text=True, timeout=90)
    if result.returncode != 0:
        raise RuntimeError(f"Wort-Caption-Batch fehlgeschlagen: {result.stderr[-500:]}")

    first_start = max(0.0, usable[0].get("start", 0.0))
    blank_frames_before = min(n_frames, round(first_start * fps))
    if blank_frames_before > 0:
        blank_path = os.path.join(out_dir, "blank.png")
        _render_word_caption_blank(blank_path, width, height)
        for f in range(blank_frames_before):
            os.symlink(blank_path, os.path.join(out_dir, f"seq_{f:04d}.png"))

    for idx, w in enumerate(usable):
        word_png = os.path.join(out_dir, f"word_{idx:04d}.png")
        start_f = max(blank_frames_before, round(max(0.0, w.get("start", 0.0)) * fps))
        if idx + 1 < len(usable):
            end_f = min(n_frames, round(max(0.0, usable[idx + 1].get("start", 0.0)) * fps))
        else:
            end_f = n_frames
        for f in range(start_f, max(start_f + 1, end_f)):
            if f >= n_frames:
                break
            link_path = os.path.join(out_dir, f"seq_{f:04d}.png")
            if not os.path.exists(link_path):
                os.symlink(word_png, link_path)


def _render_word_caption_blank(out_path: str, width: int, height: int) -> None:
    """Ein einzelnes transparentes PNG für die Lücke vor dem ersten Wort einer Szene
    (z.B. eine Sequenz-Anker-Szene, deren Kamera-Bewegung schon läuft, während die
    Stimme noch aus der Vorszene nachklingt). Eigener `blank`-Style statt `caption`
    mit leerem Text -- render_caption zeichnet seine halbtransparente Box unabhängig
    vom Textinhalt, das wäre hier ein sichtbarer Balken ohne Text."""
    render_text_overlay_png(out_path, width, height, "blank", "")


def render_text_overlay_png(out_path: str, width: int, height: int, style: str, text: str) -> None:
    """style: 'caption' | 'callout' | 'chapter'. Text wird base64-kodiert übergeben,
    damit beliebige Satzzeichen/Unicode nicht per Shell-Escaping durchgereicht werden müssen.
    """
    if not os.path.exists(WHISPER_VENV_PY):
        raise RuntimeError(
            "Helfer-venv fehlt (.venv_whisper/) -- einmalig einrichten: "
            "python3 -m venv .venv_whisper && ./.venv_whisper/bin/pip install faster-whisper Pillow"
        )
    text_b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    args = [WHISPER_VENV_PY, OVERLAY_SCRIPT, out_path, str(width), str(height), style, text_b64]
    # 90s timeout — see dashboard.py Z. 3158 for rationale (transient contention margin).
    result = subprocess.run(args, capture_output=True, text=True, timeout=90)
    if result.returncode != 0:
        raise RuntimeError(f"Overlay-Rendering fehlgeschlagen: {result.stderr[-500:]}")


def render_title_card_png_via_venv(out_path: str, width: int, height: int,
                                   text: str, phase: str = "") -> None:
    """Phase E: full-frame OPAQUE title-card PNG via .venv_whisper (Pillow lives there)."""
    if not os.path.exists(WHISPER_VENV_PY):
        raise RuntimeError(
            "Helfer-venv fehlt (.venv_whisper/) — Pillow wird für title-card-Rendering benötigt. "
            "Einmalig einrichten: python3 -m venv .venv_whisper && "
            "./.venv_whisper/bin/pip install faster-whisper Pillow"
        )
    text_b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    args = [WHISPER_VENV_PY, OVERLAY_SCRIPT,
            out_path, str(width), str(height), "title_card", text_b64, phase]
    result = subprocess.run(args, capture_output=True, text=True, timeout=90)
    if result.returncode != 0:
        raise RuntimeError(f"Title-Card-Rendering fehlgeschlagen: {result.stderr[-500:]}")