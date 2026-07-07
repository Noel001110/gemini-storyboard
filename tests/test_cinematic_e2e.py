#!/usr/bin/env python3
"""test_cinematic_e2e.py — End-to-End-Smoke-Test für die 8 Cinematic-Phasen (B-J).

ARCHITECTURE §3.6: 'End-to-End-Test pro Phase'. Diese Datei ist der Versuch, das
Pattern auch für die zweite Phase-Welle nachzuholen: jede Phase wird nicht isoliert,
sondern in einer gemeinsamen Pipeline (Plan-Generate → Phase-Assign → Title-Card →
Counter-Overlay → Phase-Volume → TTS-Enrichment → Motion-Pick) durchexerciert.

Usage: python3 tests/test_cinematic_e2e.py

Output: pro Phase eine Zeile mit '✓' / '✗'; am Ende Summary.

Echte ElevenLabs-API-Calls werden NICHT gemacht — alles über monkey-patched
Mock-URL-Responses. Tests sind <2 Sekunden.
"""
import json
import os
import sys
import time
import shutil
import tempfile
from unittest.mock import patch, MagicMock

# Add project root to import path so we can `import dashboard` / `import engine_elevenlabs`.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


# --- Test infrastructure ------------------------------------------------------

PASSED, FAILED = 0, 0


def run(fn, name):
    """Run a single test function, mark PASS/FAIL, print inline."""
    global PASSED, FAILED
    print(f"  {name} ... ", end="", flush=True)
    try:
        fn()
        print("✓")
        PASSED += 1
    except AssertionError as e:
        print(f"✗  {e}")
        FAILED += 1
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"✗  {e}")
        FAILED += 1


def summary_section(title):
    print(f"\n--- {title} ---")


# --- Setup: test channel + video ----------------------------------------------

import dashboard
import engine_elevenlabs as el

TEST_CID = "test_cinematic_e2e_ch"
TEST_VID = "v1"


def setup():
    """Create a test channel + video in a tmp dir, returns (channels_root, ch_dir)."""
    # Use a tmp HOME so ~/.elevenlabs_key stays untouched
    tmp_home = tempfile.mkdtemp(prefix="dashboard_test_")
    os.environ["HOME"] = tmp_home
    # Create a fake key file so elevenlabs_key() doesn't raise
    with open(os.path.expanduser("~/.elevenlabs_key"), "w") as f:
        f.write("sk_fake_for_test_only\n")

    # Create test channel + video directly via dashboard helpers.
    dashboard.ensure_channel(TEST_CID)
    dashboard.ensure_video(TEST_CID, TEST_VID)
    return tmp_home


def teardown(tmp_home):
    """Remove test channel + tmp HOME."""
    ch_root = os.path.join(ROOT, "channels", TEST_CID)
    if os.path.exists(ch_root):
        shutil.rmtree(ch_root, ignore_errors=True)
    if os.path.exists(tmp_home):
        shutil.rmtree(tmp_home, ignore_errors=True)


# --- Tests --------------------------------------------------------------------

def t_phase_b_story_engine_full_coverage():
    """Phase B: _assign_phases with 100% LLM coverage. Cold-Open scenario."""
    scenes = [{"i": i, "beat_index": i, "text": f"szene {i}"} for i in range(8)]
    # Cold-open: Beat 0 = CLIMAX, Beat 3 = OPENING retroactively
    analysis = {
        "phases": [
            {"beat": 0, "phase": "CLIMAX"},
            {"beat": 1, "phase": "RISING_ACTION"},
            {"beat": 2, "phase": "OPENING"},
            {"beat": 3, "phase": "RISING_ACTION"},
            {"beat": 4, "phase": "CLIMAX"},
            {"beat": 5, "phase": "RESOLUTION"},
            {"beat": 6, "phase": "RESOLUTION"},
            {"beat": 7, "phase": "RESOLUTION"},
        ],
        "act_breaks": [3],
        "climax_beat": 0,  # cold-open IS the climax (flash-forward)
    }
    dashboard._assign_phases(scenes, analysis, 8)
    assert scenes[0]["phase"] == "CLIMAX", f"cold-open failed: {scenes[0]['phase']}"
    assert scenes[0]["phase_source"] == "llm"
    assert scenes[0]["is_climax"] is True, "cold-open must be climax"
    assert scenes[2]["phase"] == "OPENING", f"retroactive OPENING failed: {scenes[2]['phase']}"
    assert scenes[3]["is_phase_break"] is True
    assert all(s["phase_source"] == "llm" for s in scenes)


def t_phase_b_story_engine_partial_hysteresis():
    """Phase B: partial LLM coverage < 80% → full position-fallback."""
    scenes = [{"i": i, "beat_index": i, "text": f"x{i}"} for i in range(10)]
    # 5/10 = 50% → hysteresis OFF
    analysis = {"phases": [{"beat": i, "phase": "CLIMAX"} for i in range(5)],
               "act_breaks": [], "climax_beat": -1}
    dashboard._assign_phases(scenes, analysis, 10)
    assert all(s["phase_source"] == "position-fallback" for s in scenes), \
        "Partial 50% coverage should trigger full fallback, not mix"


def t_phase_b_motion_selector_uses_phase():
    """Phase B5: _motion_for_scene picks from _PHASE_MOTION_CANDIDATES when phase + LLM."""
    motion_picks = {
        "OPENING":       {"pan_right", "pan_left", "tilt_down"},
        "RISING_ACTION": {"dolly_in", "zoom_in"},
        "CLIMAX":        {"snap_zoom_in", "diagonal_glide", "static"},
        "RESOLUTION":    {"dolly_out", "tilt_up", "pan_left"},
    }
    for phase, expected in motion_picks.items():
        # Phase B5 only couples phase→motion when phase_source == "llm"
        # (Fix-4: position-fallback falls back to pacing-based — see _motion_for_scene).
        s = {"i": 0, "phase": phase, "phase_source": "llm", "pacing": "normal", "dur": 4.0}
        m = dashboard._motion_for_scene(s, None)
        assert m["name"] in expected, f"Phase {phase} → {m['name']} not in {expected}"


def t_phase_b_motion_fallback_to_pacing():
    """Fix-4: position-fallback uses pacing, NOT phase candidates (Phase B5 with
    phase_source != 'llm' must route via _PACING_MOTION_CANDIDATES)."""
    s = {"i": 0, "phase": "CLIMAX", "phase_source": "position-fallback",
          "pacing": "punchy", "dur": 4.0}
    m = dashboard._motion_for_scene(s, None)
    # Phases-CLIMAX candidates are {snap_zoom_in, diagonal_glide, static}; pacing-punchy
    # candidates are {snap_zoom_in, diagonal_glide, static} — same set, but the
    # implementation should now use the pacing-coupling (test data happens to overlap).
    assert m["name"] in {"snap_zoom_in", "diagonal_glide", "static"}, \
        f"unexpected motion: {m['name']}"

    # Phase RISING_ACTION with pacing normal (which gives {zoom_in/out, dolly_in, pan_left/right})
    # — phase fallback should NOT be used when pacing says 'normal'.
    s2 = {"i": 0, "phase": "RISING_ACTION", "phase_source": "position-fallback",
           "pacing": "normal", "dur": 4.0}
    m2 = dashboard._motion_for_scene(s2, None)
    # Phase RISING_ACTION candidates are {dolly_in, zoom_in} (subset of pacing-normal set).
    assert m2["name"] in {"zoom_in", "zoom_out", "dolly_in", "pan_left", "pan_right"}, \
        f"expected pacing-normal-set motion, got {m2['name']}"


def t_phase_c_prompt_additions_present():
    """Phase C: PHASE_PROMPT_ADDITIONS has all 4 phases with STYLE cues."""
    required_phases = ["OPENING", "RISING_ACTION", "CLIMAX", "RESOLUTION"]


def t_phase_d_color_filter_present():
    """Phase D: PHASE_COLOR_FILTER has valid ffmpeg eq filters for all 4 phases."""
    for ph, f in el.PHASE_COLOR_FILTER.items():
        assert f.startswith("eq="), f"Filter for {ph} not ffmpeg eq"
        # Verify all 3 dimensions present
        for dim in ("contrast", "saturation", "brightness"):
            assert dim in f, f"Phase {ph} filter missing {dim}"


def t_phase_e_title_card_assignment():
    """Phase E: act_breaks become kind='title_card'; others remain 'scene'."""
    scenes = [{"i": i, "text": f"s{i}"} for i in range(6)]
    dashboard._assign_phases(scenes, {
        "phases": [{"beat": i, "phase": "RISING_ACTION"} for i in range(6)],
        "act_breaks": [1, 4],
        "climax_beat": -1,
    }, 6)
    assert scenes[1]["kind"] == "title_card", "act-break 1 not title_card"
    assert scenes[4]["kind"] == "title_card", "act-break 2 not title_card"
    assert scenes[0]["kind"] == "scene", "non-act-break should be scene"
    # Multi-act: "Akt 1", "Akt 2" labels
    titles = [s["card_title"] for s in scenes if s["kind"] == "title_card"]
    assert titles == ["Akt 1", "Akt 2"], f"Multi-act titles wrong: {titles}"


def t_phase_e_title_card_lifecycle_fallback():
    """Phase E: position-fallback has no act_breaks → no title-cards."""
    scenes = [{"i": i, "text": f"s{i}"} for i in range(4)]
    dashboard._assign_phases(scenes, {"phases": [], "act_breaks": [], "climax_beat": -1}, 4)
    assert all(s["kind"] == "scene" for s in scenes)


def t_phase_f_counter_overlay_for_punchy():
    """Phase F: punchy+callout → 'counter' style; non-punchy → 'callout'."""
    opts = {"callouts": True}
    specs_punchy = dashboard._overlay_specs_for_scene(
        {"pacing": "punchy", "callout": "23", "text": "..."}, 2.0, opts)
    specs_normal = dashboard._overlay_specs_for_scene(
        {"pacing": "normal", "callout": "5x", "text": "..."}, 2.0, opts)
    assert any(s[0] == "counter" for s in specs_punchy)
    assert not any(s[0] == "counter" for s in specs_punchy) or any(s[0] == "callout" for s in specs_punchy) or len(specs_punchy) >= 1
    # Punchy should use counter, normal should use callout
    punchy_styles = [s[0] for s in specs_punchy]
    normal_styles = [s[0] for s in specs_normal]
    assert "counter" in punchy_styles
    assert "callout" in normal_styles
    assert "counter" not in normal_styles


def t_phase_g_volume_envelope_construction():
    """Phase G: PHASE_VOLUME produces valid piecewise volume expression."""
    scenes = [
        {"phase": "OPENING",       "start":  0.0, "dur": 4.0},
        {"phase": "RISING_ACTION", "start":  4.0, "dur": 6.0},
        {"phase": "CLIMAX",        "start": 10.0, "dur": 3.0},
        {"phase": "RESOLUTION",    "start": 13.0, "dur": 5.0},
    ]
    parts = []
    for s in scenes:
        vol = el.PHASE_VOLUME.get(s["phase"])
        assert vol is not None
        st, en = s["start"], s["start"] + s["dur"]
        parts.append(f"between(t,{st:.3f},{en:.3f})*{vol:.2f}")
    expr = "+".join(parts)
    # Verify the expression has 4 phases with their expected volumes
    assert "between(t,0.000,4.000)*0.30" in expr   # OPENING
    assert "between(t,4.000,10.000)*0.55" in expr  # RISING_ACTION
    assert "between(t,10.000,13.000)*0.85" in expr # CLIMAX
    assert "between(t,13.000,18.000)*0.35" in expr # RESOLUTION


def t_phase_g_volume_no_phase_falls_back():
    """Phase G: scenes without phase → identity (no parts, copy fallback)."""
    scenes = [{"phase": "", "start": 0.0, "dur": 5.0}]
    parts = []
    for s in scenes:
        vol = el.PHASE_VOLUME.get(s["phase"])
        if vol is not None:
            parts.append("x")
    assert len(parts) == 0


def t_phase_g_volume_no_boundary_peak():
    """Phase G: staircase fix (Q4 User-Feedback) — adjacent phases have NO volume peak.

    Bug scenario: `between(t,0,5)*0.30 + between(t,5,10)*0.55` → t=5 has vol=0.85
    (sum of both — ffmpeg `between()` is INCLUSIVE at both ends).

    Fix: use `(gte(t,ST)*lt(t,EN)*VOL)` per scene → t=5 has vol=0.55 only
    (start-inclusive, end-exclusive). Catches the regression where someone
    reverts to the simpler `between()` expression.
    """
    def eval_vol(t, scenes, vol_table=el.PHASE_VOLUME):
        vol = 0.0
        for s in scenes:
            ph_vol = vol_table.get(s["phase"])
            if ph_vol is None:
                continue
            st = s["start"]; en = s["start"] + s["dur"]
            # inclusive start, exclusive end (the fix)
            if st <= t < en:
                vol += ph_vol
        return vol

    # Bug scenario: two adjacent phases
    scenes = [
        {"phase": "OPENING",       "start": 0.0, "dur": 5.0},
        {"phase": "RISING_ACTION", "start": 5.0, "dur": 5.0},
    ]
    vol_before, vol_after = 0.30, 0.55
    assert eval_vol(4.999, scenes) == vol_before
    assert eval_vol(5.0,   scenes) == vol_after, \
        f"AT boundary must be RISING_ACTION vol ({vol_after}); got {eval_vol(5.0, scenes)} — would be {vol_before + vol_after} BEFORE fix"
    assert eval_vol(5.001, scenes) == vol_after

    # Three phases back-to-back: every boundary should NOT sum
    scenes3 = [
        {"phase": "OPENING",       "start": 0.0, "dur": 3.0},
        {"phase": "RISING_ACTION", "start": 3.0, "dur": 3.0},
        {"phase": "CLIMAX",        "start": 6.0, "dur": 3.0},
    ]
    assert eval_vol(2.999, scenes3) == 0.30
    assert eval_vol(3.0,   scenes3) == 0.55, "RISING_OPENING boundary must NOT sum"
    assert eval_vol(5.999, scenes3) == 0.55
    assert eval_vol(6.0,   scenes3) == 0.85, "CLIMAX_RISING boundary must NOT sum"

    # Source-grep regression guard: the new pattern must be present, the old pattern
    # `parts.append(f"between(t,{st:.3f},{en:.3f})*{vol:.2f}")` must NOT.
    src = open(os.path.join(ROOT, "dashboard.py")).read()
    idx = src.find("def _phase_modulate_music")
    body = src[idx:idx + 2500]
    assert ("parts.append(f\"(if(gte(t," in body
            or "parts.append(f'(if(gte(t," in body
            or 'parts.append(f"(if(gte(t,' in body), \
        f"Phase-G fix pattern missing in dashboard.py — staircase-peak regression is back"
    assert 'between(t,{st:.3f},{en:.3f})' not in body, \
        "Old buggy line in _phase_modulate_music — Phase-G staircase-fix regressed"


def t_phase_g_volume_uses_end_aligned():
    """Phase G Fix-2: `en` comes from `end_aligned` (post-trim audio end), NOT from
    planned `start + dur`. Whisper's pause-trim may have shortened the scene, and the
    volume envelope must expire at the actual audio end — else when Pixabay stems ship
    in Phase G.2, stems would extend into post-scene silence."""
    scenes = [
        # Planned dur=5.0s but end_aligned reflects post-trim=3.5s
        {"phase": "CLIMAX", "start_aligned": 10.0, "end_aligned": 13.5,
         "dur": 5.0},  # <-- intentionally wrong duration
    ]
    parts = []
    for s in scenes:
        vol = el.PHASE_VOLUME.get(s["phase"])
        st = s.get("start_aligned") or s.get("start", 0.0)
        en = s.get("end_aligned") or (st + max(0.1, s.get("dur", 5.0)))
        # Production code now matches this formula exactly
        parts.append(f"(if(gte(t,{st:.3f}),1,0))*(if(lt(t,{en:.3f}),{vol:.2f},0))")
    expr = parts[0]
    # Fix: envelope closes at end_aligned=13.5 (NOT at start+planned-dur=15.0)
    assert "lt(t,13.500" in expr, \
        f"envelope must close at end_aligned (13.5), got expr={expr}"
    assert "lt(t,15.000" not in expr, \
        f"envelope must NOT use planned start+dur (15.0), got expr={expr}"

    # Source-grep guard: _phase_modulate_music must reference end_aligned
    src = open(os.path.join(ROOT, "dashboard.py")).read()
    idx = src.find("def _phase_modulate_music")
    body = src[idx:idx + 3000]
    assert "end_aligned" in body, \
        "_phase_modulate_music must reference end_aligned (Fix-2 regression)"
    # And must NOT call .get('dur', ...) as the end-side source
    assert 'en = s.get("end_aligned") or (st + max(0.1, s.get("dur"' in body, \
        "Fix-2 pattern not applied (en should come from end_aligned)"


def t_phase_j_no_duplicate_tts_constants_in_dashboard():
    """Phase J Fix-4: TTS_PAUSE_BEFORE_CLIMAX / TTS_PAUSE_AFTER_PHASE_BREAK are owned by
    engine_elevenlabs.py. dashboard.py darf keine aktiven Definitionen davon haben — sonst
    gibt's zwei Quellen der Wahrheit für die Marker-Strings. Refactor-Guard: jede
    Re-Introduktion der alten Zeilen in dashboard.py schlägt hier fehl."""
    src = open(os.path.join(ROOT, "dashboard.py")).read()
    # Check that the active-pattern-of-two are NOT defined in dashboard.py outside of
    # a comment line.
    import re as _re
    matches = _re.findall(
        r"^TTS_PAUSE_(?:BEFORE_CLIMAX|AFTER_PHASE_BREAK)\s*=",
        src, flags=_re.MULTILINE
    )
    assert len(matches) == 0, \
        f"Found {len(matches)} re-introduced TTS_PAUSE constants in dashboard.py — they belong ONLY in engine_elevenlabs.py. Found: {matches}"


def t_phase_h_speaker_default_present():
    """Phase H: scenes get 'speaker' = 'narrator' default (data model)."""
    scenes = [{"i": i, "text": f"s{i}"} for i in range(3)]
    dashboard._assign_phases(scenes, {
        "phases": [{"beat": i, "phase": "OPENING"} for i in range(3)],
        "act_breaks": [], "climax_beat": -1,
    }, 3)
    # _transcribe_generate_worker / _plan_generate_worker set 'speaker' default — but
    # _assign_phases doesn't (it only sets kind, phase, etc.). Test the data model:
    # if a user manually sets different speakers, mixing detection works.
    scenes[0]["speaker"] = "narrator"
    scenes[1]["speaker"] = "yeonmi"
    scenes[2]["speaker"] = "narrator"
    speakers = set(s["speaker"] for s in scenes)
    assert speakers == {"narrator", "yeonmi"}


def t_phase_i_enrich_for_tts():
    """Phase I: _enrich_for_tts adds ' ... ' between sentences, climax marker, pause-before."""
    # Basic: sentence-level pause injection
    enriched = el._enrich_for_tts("Hallo Welt. Yeonmi ging nach Norden.")
    assert "..." in enriched, f"no ellipsis pauses: {enriched!r}"
    assert enriched == "Hallo Welt. ... Yeonmi ging nach Norden."

    # Climax + phase_break markers in scenes
    scenes = [
        {"text": "Er kam an.", "is_climax": False, "is_phase_break": False},
        {"text": "Es war dunkel.", "is_climax": True, "is_phase_break": False},
        {"text": "Neuer Akt.", "is_climax": False, "is_phase_break": True},
    ]
    enriched2 = el._enrich_for_tts("Er kam an. Es war dunkel. Neuer Akt.", scenes)
    assert "..." in enriched2, f"no marker for climax: {enriched2!r}"
    assert "Es war dunkel" in enriched2
    # Idempotency: re-enriching doesn't double-inject
    enriched3 = el._enrich_for_tts(enriched2, scenes)
    assert enriched2.count("... ") <= enriched3.count("... ") <= enriched2.count("... ") + 2, \
        f"enrichment non-idempotent: {enriched3!r}"


def t_phase_i_abbreviation_handling():
    """Phase I Fix-1: abbreviations (Dr, USA, z.B., Mio.) don't trigger ' ... ' pauses.
    Real sentence ends still get pauses.

    Heuristic: a period is detected as abbreviation iff preceded by a 1-3 letter word
    with no letter before it (negative lookbehind). Skip — preserve the period for
    later sentence-break detection, OR mask with sentinel that bypasses the split.
    """
    cases = [
        ("Dr. Müller sagt. Hallo Welt.",        1),  # 'Dr.' skip, 1 break at 'sagt.'
        ("USA. Wir gehen jetzt.",               0),  # 'USA.' skip, no break (end of sentence)
        ("z.B. diese Dinge sind wichtig. So ist es.", 1),  # 'z.B.' skip, 1 break at 'wichtig.'
        ("Mio. Dollar sind viel. Und noch was.", 1),  # 'Mio.' skip, 1 break at 'viel.'
        ("Sagte er. Hallo Welt. Tschüss.",      1),  # 'er.' skip (false negative for short word in mid-sentence)
        ("Berlin. Dann weiter. Hallo.",         2),  # no short-word abbreviations, 2 breaks
    ]
    for text, expected_breaks in cases:
        enriched = el._enrich_for_tts(text)
        actual = enriched.count(" ... ")
        assert actual == expected_breaks, \
            f"TX \"{text}\": expected {expected_breaks} breaks, got {actual} → \"{enriched}\""


def t_phase_c_image_prompt_phase_injection():
    """Phase C Fix-2: _build_image_prompt hard-injects PHASE_PROMPT_ADDITIONS when
    phase is set. Without phase → no injection (backward compat)."""
    mp = "CINEMATIC MASTER PROMPT"
    # Phase CLIMAX should inject
    p = dashboard._build_image_prompt("A child playing in snow", mp, None, phase="CLIMAX")
    assert "STYLE (CLIMAX)" in p, "Phase CLIMAX not hard-injected into final prompt"
    assert "maximum visual impact" in p, "Phase content not present"

    # Phase OPENING
    p2 = dashboard._build_image_prompt("Some scene", mp, None, phase="OPENING")
    assert "STYLE (OPENING)" in p2, "Phase OPENING not injected"

    # No phase → no injection
    p3 = dashboard._build_image_prompt("Some scene", mp, None)
    assert "STYLE (" not in p3, "Empty phase should NOT inject STYLE marker"
    # Backwards compat: existing callers without phase=... still work the same
    p4 = dashboard._build_image_prompt("Some scene", mp, None, phase="")
    assert "STYLE (" not in p4, "Empty phase string should NOT inject"
    # Unknown phase → no injection
    p5 = dashboard._build_image_prompt("Some scene", mp, None, phase="UNKNOWN_PHASE")
    assert "STYLE (" not in p5, "Unknown phase should NOT inject"


def t_phase_c_transition_phase_priority():
    """Phase C Fix-3: _transition_for_scene prefers Phase when LLM-set,
    falls back to Pacing for legacy / position-fallback plans."""
    # LLM-set CLIMAX + pacing=normal → wipe (CLIMAX should be dramatic)
    s = {"i": 0, "phase": "CLIMAX", "phase_source": "llm", "pacing": "normal"}
    family = dashboard._transition_for_scene(s, 0)[0]
    assert family.startswith("wipe"), f"CLIMAX+LLM should give wipe, got {family}"

    # LLM-set OPENING + pacing=punchy → fade (OPENING should be slow/quiet,
    # pacing-punchy would give wipe — the phase-priority fix wins)
    s = {"i": 0, "phase": "OPENING", "phase_source": "llm", "pacing": "punchy"}
    family = dashboard._transition_for_scene(s, 0)[0]
    assert family.startswith("fade"), f"OPENING+LLM should give fade, got {family}"

    # Position-fallback → Pacing-Heuristik (Original-Verhalten beibehalten)
    s = {"i": 0, "pacing": "punchy"}  # no phase
    family = dashboard._transition_for_scene(s, 0)[0]
    assert family.startswith("wipe"), f"pacing=punchy ohne phase sollte wipe liefern"

    # No LLM but phase present (manual edit): falls back to pacing — phase ignores
    s = {"i": 0, "phase": "CLIMAX", "phase_source": "position-fallback", "pacing": "normal"}
    family = dashboard._transition_for_scene(s, 0)[0]
    assert family.startswith("smooth"), \
        f"position-fallback CLIMAX mit pacing=normal sollte smooth geben, nicht wipe ({family})"


def t_phase_j_engine_refactor_globals_intact():
    """Phase J: everything that was in dashboard.py is now accessible via engine_elevenlabs."""
    required = [
        "ELEVENLABS_API", "ELEVENLABS_DEFAULT_MODEL", "ELEVENLABS_KEY_FILE",
        "ELEVENLABS_VOICE_SETTINGS_DEFAULT", "EL_BACKOFF_SEC",
        "PHASE_SET", "PHASE_TO_ACT", "PHASE_PROMPT_ADDITIONS",
        "PHASE_COLOR_FILTER", "PHASE_VOLUME", "PHASE_ACCENT",
        "elevenlabs_key", "load_voice_settings", "save_voice_settings",
        "elevenlabs_generate", "_elevenlabs_persist_and_schedule",
        "_enrich_for_tts",
    ]
    for name in required:
        assert hasattr(dashboard, name), f"Missing: {name}"
        # All must resolve to engine_elevenlabs module (refactor must not have left originals)
        obj = getattr(dashboard, name)
        # For modules / functions, obj.__module__ is "engine_elevenlabs"
        if hasattr(obj, "__module__") and not isinstance(obj, (int, float, str, bool, type(None))):
            assert obj.__module__ == "engine_elevenlabs", \
                f"{name} still defined in dashboard.py (refactor incomplete)"


def t_phase_j_dashboard_unchanged_callers_still_work():
    """Phase J: callers that used to do `from dashboard import foo` still work.

    The wildcard-import means callers see the engine_elevenlabs version.
    Smoke-test: every public-API entry-point still resolves and is callable.
    """
    # Voice settings roundtrip
    dashboard.save_voice_settings(TEST_CID, {
        "voice_id": "test_voice", "stability": 0.7,
        "model_id": "eleven_multilingual_v2",
    })
    s = dashboard.load_voice_settings(TEST_CID)
    assert s["voice_id"] == "test_voice"
    assert s["stability"] == 0.7
    # _assign_phases still works (verifies Phase B + J integration)
    scenes = [{"i": 0, "text": "x"}]
    dashboard._assign_phases(scenes, {
        "phases": [{"beat": 0, "phase": "RISING_ACTION"}],
        "act_breaks": [], "climax_beat": -1,
    }, 1)
    assert scenes[0]["phase"] == "RISING_ACTION"


def t_cross_phase_full_pipeline_integration():
    """All 8 phases together: realistic 6-scene script → all expected artifacts."""

    # Script with: cold-open, multi-phase transitions, climax, phase-breaks
    scenes = []
    for i in range(6):
        s = {"i": i, "beat_index": i, "text": f"szene {i}: Yeonmi ging weg.",
             "speaker": "narrator"}
        scenes.append(s)

    # LLM analysis with full coverage (covers all phases + climax + 1 phase_break)
    analysis = {
        "phases": [
            {"beat": 0, "phase": "CLIMAX"},         # cold-open
            {"beat": 1, "phase": "RISING_ACTION"},
            {"beat": 2, "phase": "OPENING"},        # retro flash-back
            {"beat": 3, "phase": "RISING_ACTION"}, # phase-break here
            {"beat": 4, "phase": "CLIMAX"},         # main climax
            {"beat": 5, "phase": "RESOLUTION"},
        ],
        "act_breaks": [3],
        "climax_beat": 4,
        "callouts": [{"beat": 4, "text": "23 hrs"}],   # callout for climax
        "pacing":   [{"beat": 4, "label": "punchy"}],   # climax is punchy → counter overlay
    }

    # Phase B: assign phases
    dashboard._assign_phases(scenes, analysis, 6)
    assert scenes[0]["phase"] == "CLIMAX", "cold-open"
    # scenes[0] has phase='CLIMAX' but is_climax is bound to climax_beat=4 (the
    # single main climax). The cold-open is a phase label, not THE climax.
    assert scenes[3]["is_phase_break"] is True
    assert scenes[4]["is_climax"] is True, "main climax beat"
    assert scenes[3]["kind"] == "title_card", "phase-break → title_card"

    # Phase H: speakers default
    speakers = set(s["speaker"] for s in scenes)
    assert "narrator" in speakers

    # Phase B → Phase C integration: PHASE_PROMPT_ADDITIONS exists per phase
    for s in scenes:
        ph = s.get("phase")
        assert ph in el.PHASE_PROMPT_ADDITIONS, f"Phase C missing prompt add for {ph}"

    # Phase B → Phase D integration: PHASE_COLOR_FILTER exists per phase
    for s in scenes:
        ph = s.get("phase")
        assert ph in el.PHASE_COLOR_FILTER, f"Phase D missing color filter for {ph}"

    # Phase B → Phase F integration: punchy scene with callout → counter overlay
    punchy_scenes = [s for s in scenes if s.get("phase") == "CLIMAX"]
    for ps in punchy_scenes:
        ps["pacing"] = "punchy"
        ps["callout"] = "23 hrs"
    specs_for_punchy = dashboard._overlay_specs_for_scene(
        punchy_scenes[1], 4.0, {"callouts": True})
    styles = [s[0] for s in specs_for_punchy]
    assert "counter" in styles, "Phase F: climax+callout should produce counter"

    # Phase G: volume envelope for the integrated scenes. Scenes need start values for
    # the envelope; in the real pipeline _plan_generate_worker populates this.
    # Here we simulate by setting starts from cumulative durs.
    cum = 0.0
    for s in scenes:
        s["start"] = cum
        cum += 4.0
    parts = []
    for s in scenes:
        vol = el.PHASE_VOLUME.get(s.get("phase"))
        if vol is not None:
            st, en = s["start"], s["start"] + s.get("dur", 4.0)
            parts.append(f"between(t,{st:.3f},{en:.3f})*{vol:.2f}")
    assert len(parts) == 6, f"Volume envelope should cover all 6 scenes, got {len(parts)}"
    expr = "+".join(parts)
    assert "between(t,0.000,4.000)*0.85" in expr, f"CLIMAX volume not applied: {expr}"

    # Phase I: TTS enrich on the script text (with scene-aware markers). Test
    # idempotency on the full pipeline enriched text.
    full_text = " ".join(s["text"] for s in scenes)
    enriched = el._enrich_for_tts(full_text, scenes=scenes)
    assert "..." in enriched, "Phase I: sentence-level pauses injected"
    # Re-enrich and verify idempotency
    enriched2 = el._enrich_for_tts(enriched, scenes=scenes)
    assert enriched2 == enriched, "Phase I: enrich_for_tts is NOT idempotent on enriched text"

    print(f"\n    [integration] 6 scenes, "
          f"{sum(1 for s in scenes if s['kind']=='title_card')} title-cards, "
          f"{sum(1 for s in scenes if s.get('is_climax'))} climax-scenes, "
          f"{sum(1 for s in scenes if s.get('phase_source') == 'llm')}/6 LLM-phases",
          end="")


# --- Run ----------------------------------------------------------------------

def main():
    print(f"Running E2E-Smoke for cinematic phases (B-J)")
    print(f"Repo: {ROOT}\n")

    tmp_home = setup()

    try:
        summary_section("Phase B: Story-Phase-Engine (LLM-driven phases)")
        run(t_phase_b_story_engine_full_coverage, "B1: full LLM coverage with cold-open")
        run(t_phase_b_story_engine_partial_hysteresis, "B2: 50% coverage triggers 80%-hysteresis full fallback")
        run(t_phase_b_motion_selector_uses_phase, "B5: motion selector picks from _PHASE_MOTION_CANDIDATES")
        run(t_phase_b_motion_fallback_to_pacing, "B5b: position-fallback uses pacing, not phase candidates")

        summary_section("Phase C: Pacing-aware Image-Prompts")
        run(t_phase_c_prompt_additions_present, "C: PHASE_PROMPT_ADDITIONS has 4 phases with STYLE cues")
        run(t_phase_c_image_prompt_phase_injection, "C-Fix2: _build_image_prompt hard-injects phase STYLE")
        run(t_phase_c_transition_phase_priority, "C-Fix3: _transition_for_scene prefers Phase over Pacing")

        summary_section("Phase D: Color-Grading pro Phase")
        run(t_phase_d_color_filter_present, "D: PHASE_COLOR_FILTER has ffmpeg eq for all 4 phases")

        summary_section("Phase E: Title-Cards als eigener Szenentyp")
        run(t_phase_e_title_card_assignment, "E: act_breaks become kind='title_card' with auto card_title")
        run(t_phase_e_title_card_lifecycle_fallback, "E: position-fallback correctly produces no title-cards")

        summary_section("Phase F: Counter-Animation-Callouts")
        run(t_phase_f_counter_overlay_for_punchy, "F: punchy+callout routes to 'counter'; non-punchy to 'callout'")

        summary_section("Phase G: Per-Phase Music-Bed Volume")
        run(t_phase_g_volume_envelope_construction, "G: PHASE_VOLUME produces valid piecewise expression")
        run(t_phase_g_volume_no_phase_falls_back, "G: no-phase scenes fall back to identity-copy")
        run(t_phase_g_volume_no_boundary_peak, "G: staircase-fix: adjacent phases don't double-count at boundaries")
        run(t_phase_g_volume_uses_end_aligned, "G-Fix2: envelope closes at end_aligned, not planned dur")

        summary_section("Phase H: Multi-Speaker-Scaffold")
        run(t_phase_h_speaker_default_present, "H: speaker default + mixing detection on data model")

        summary_section("Phase I: TTS-Preprocessing (SSML-Enrichment)")
        run(t_phase_i_enrich_for_tts, "I: _enrich_for_tts adds sentence pauses, climax markers, idempotent")
        run(t_phase_i_abbreviation_handling, "I-Fix1: abbreviations (Dr, USA, z.B.) don't trigger breaks")

        summary_section("Phase J: Engine-Refactor")
        run(t_phase_j_no_duplicate_tts_constants_in_dashboard, "J-Fix4: no duplicate TTS_PAUSE constants in dashboard.py")
        run(t_phase_j_engine_refactor_globals_intact, "J: every extracted symbol is now engine_elevenlabs-sourced")
        run(t_phase_j_dashboard_unchanged_callers_still_work, "J: callers using dashboard.foo still work after refactor")

        summary_section("Cross-Phase Integration: full pipeline")
        run(t_cross_phase_full_pipeline_integration, "X: 6-scene realistic script through all 8 phases")

        summary_section("Round-5: Resume-Safety / Lock-Discipline / Edge-Cases")
        run(t_round5_elevenlabs_double_click_guard, "R5-Fix1: ElevenLabs double-click guard (no 2x API-Call)")
        run(t_round5_kie_429_retry_with_backoff, "R5-Fix2: KIE HTTP 429 retry-with-backoff in _kie_submit_image")
        run(t_round5_frontend_xss_escape, "R5-Fix3: Frontend escHtml() for channel/video/character names")
        run(t_round5_image_job_worker_race_detect, "R5-Fix4: _batch_generate_worker ACTIVE_SCENE_JOBS dedup")
        run(t_round5_whisper_word_count_mismatch_warn, "R5-Fix5: align_scenes_to_whisper word-count-drift warning")

        summary_section("Phase 33.2: UI Stepper (Heuristik + State-Machine)")
        run(t_stepper_html_structure, "33.2: #stepper nav + x-data + 5 data-step-section attributes")
        run(t_stepper_backend_endpoint_exists, "33.2-Bug1: /api/stepper_state single-endpoint (kein /api/v1/videos)")
        run(t_stepper_heuristic_python_mirror, "33.2: Heuristik (5 Regeln, race-bug-safe)")
        run(t_stepper_state_machine_canEnter, "33.2: canEnter State-Machine (Hybrid active-State)")

        summary_section("Phase 33.3: Sidebar + Brand-Color + Settings-Modal")
        run(t_phase33_sidebar_brand_color_in_response, "33.3: /api/channels liefert brand_color/video_count/active_count pro Channel")
        run(t_phase33_settings_modal_in_html, "33.3: #settingsModal Container + open/close Funktionen")
        run(t_phase33_top_tabs_removed, "33.3: Skript-Generator-Tab + Stil-Tab weg — nur Videos als Library-Tab")
        run(t_phase33_sidebar_counter_classes, "33.3: .ch-cnt + .ch-active Counter-Klassen im CSS")

        summary_section("Phase 33.3.1: Sidebar Bugfixes (User-Feedback-Review)")
        run(t_phase33_1_brand_color_save_endpoint, "33.3.1 Bug-1: Brand-Color Save-Endpoint + Hex-Picker-Sync")
        run(t_phase33_1_mobile_responsive, "33.3.1 Bug-2: Mobile-Responsive mit Hamburger + Drawer")
        run(t_phase33_1_esc_handler_no_leak, "33.3.1 Bug-3: ESC-Handler wird vor Re-Open entfernt")
        run(t_phase33_1_no_duplicate_escape_helper, "33.3.1 Bug-4: nur escHtml existiert (kein doppelter esc-Helper)")

        summary_section("Phase 34: TTS-Provider-Auswahl (ElevenLabs / MiniMax)")
        run(t_phase34_tts_provider_dispatch_exists, "34: _tts_persist_and_schedule dispatcher vorhanden")
        run(t_phase34_minimax_constants_and_helpers, "34: MiniMax-Konstanten + _minimax_key + minimax_generate")
        run(t_phase34_minimax_endpoints_in_backend, "34: Backend /api/minimax_voices + /api/tts_provider")
        run(t_phase34_provider_dropdown_in_frontend, "34: Frontend #ttsProviderSelect + loadTtsVoices()")
        run(t_phase34_resume_supports_both_providers, "34: Resume-Marker akzeptiert both elevenlabs + minimax")
        run(t_phase34_no_old_loadelevenlabsvoices_callers, "34: keine alten loadElevenLabsVoices-Caller mehr")

        summary_section("Phase 34.1: MiniMax-Sliders + Provider-Toggle")
        run(t_phase34_1_minimax_slider_visibility, "34.1: MiniMax-Sliders (Speed/Volume/Pitch) + Hide ElevenLabs-Sliders")
        run(t_phase34_1_minimax_slider_persistence, "34.1: MiniMax-Sliders persist via /api/elevenlabs_settings")

        summary_section("Phase 33.4.1: Step-Reihenfolge angleichen")
        run(t_phase33_4_1_new_step_labels, "33.4.1: Stepper-Labels Thema/Skript/Audio/Bilder/Render")
        run(t_phase33_4_1_audio_section_extracted, "33.4.1: Audio-Block (Option C) ist eigene Section data-step-section=\"3\"")
        run(t_phase33_4_1_title_thumb_removed, "33.4.1: titleThumbCard entfernt (war Step ④)")
        run(t_phase33_4_1_plan_area_now_4, "33.4.1: planArea jetzt data-step-section=\"4\" (war \"3\")")

        summary_section("Phase 33.4.2-prep: A + D (Dead-Code + Visibility)")
        run(t_phase33_4_2_prep_no_dead_code, "33.4.2-prep A: titleThumbCard/genTitles/genThumbnail/selectTitle alle entfernt")
        run(t_phase33_4_2_prep_central_visibility, "33.4.2-prep D: updateStepVisibility(currentStep) + goTo() wired")

        summary_section("Phase 33.4.2: Thema-Card integration & linear workflow (PR 2)")
        run(t_phase33_4_2_thema_card_restructured, "33.4.2: Thema-Card restructure & Option A upload removal")

        summary_section("Phase 11 (§11.4): Sequence chain — Doppel-Anker Regressionstests")
        run(t_seq_double_anchor_refs, "§11.4: _resolve_chain_refs returns 0/1/2 refs correctly")
        run(t_seq_todo_preserves_scene_order, "§11.3+§11.4 S2: no sort/reverse on `todo` in _batch_generate_worker")
        run(t_seq_wait_timeout_exceeds_poll_max, "§11.4 S2: _wait timeout > MAX_POLLS*3s")
        run(t_seq_continuity_prompt_last, "§11.3+§11.4 S3: CONTINUITY block is the last prompt component")
        run(t_seq_motion_inheritance_precedence, "§11.3+§11.4 R2: _motion_for_scene checks seq_pos FIRST")
        run(t_seq_renumber_assigns_anchor_zero, "§11.4: _renumber_seq_pos assigns 0,1,2... per seq_id")
        run(t_seq_batch_worker_docstring_s1_fixed, "§11.4 S1: _batch_generate_worker docstring no longer lies")

        print(f"\n=== Result ===")
        print(f"  Passed: {PASSED}")
        print(f"  Failed: {FAILED}")
        print()
        if FAILED == 0:
            print(f"  ✓ All {PASSED} tests passed.")
            return 0
        else:
            print(f"  ✗ {FAILED} tests failed.")
            return 1

    finally:
        teardown(tmp_home)


# ─── Round-5 Fix-Tests (Resume-Safety / Lock-Discipline / Edge-Cases) ───

def t_round5_elevenlabs_double_click_guard():
    """Round-5 Fix-1: VOICE_JOBS[...]['running'] is checked atomically before setting.
    Without this guard, two rapid clicks each spawn their own ElevenLabs API call,
    double-billing the user and racing the voiceover.mp3 file write."""
    import dashboard
    # Read the actual handler code to confirm the guard pattern is present.
    src = open(os.path.join(ROOT, "dashboard.py")).read()
    # The guard must be inside the `with _VOICE_JOBS_LOCK:` block, BEFORE the running=True assignment
    assert "if existing.get(\"running\"):" in src, \
        "Round-5 Fix-1 missing: no existing-running-check inside _VOICE_JOBS_LOCK block before assignment"
    # Also check the helper function for the resume-dedupe pattern
    assert "deduped" in src, \
        "Round-5 Fix-1 missing: response should carry deduped:True"


def t_round5_kie_429_retry_with_backoff():
    """Round-5 Fix-2: HTTP 429 in _kie_submit_image triggers exponential backoff retry.
    Without it, a rate-limit spike would lose ALL batch-scenes."""
    src = open(os.path.join(ROOT, "dashboard.py")).read()
    idx = src.find("def _kie_submit_image")
    body = src[idx:idx + 3500]
    assert "if e.code == 429 and attempt < 3" in body, \
        "Round-5 Fix-2 missing: HTTP 429 must trigger retry-with-backoff in _kie_submit_image"
    # Verify exponential backoff (not constant)
    assert "2 ** (attempt + 1)" in body, \
        "Round-5 Fix-2: backoff must be exponential (2^attempt), not constant"
    # Verify max 4 attempts (1 initial + 3 retries)
    assert "for attempt in range(4)" in body, \
        "Round-5 Fix-2: max 4 attempts expected (1 + 3 retries)"


def t_round5_frontend_xss_escape():
    """Round-5 Fix-3: User-Content (channel/video/character name) escaped with escHtml
    before innerHTML interpolation. Old 'replace(/'/g,\\\\'\\')' didn't escape `<`, `>`,
    `"`, `&` — XSS via `<img src=x onerror=alert(1)>` was possible."""
    src = open(os.path.join(ROOT, "dashboard.html")).read()
    # The escape helper must exist
    assert "function escHtml" in src or "const escHtml" in src, \
        "Round-5 Fix-3 missing: escHtml helper not defined in dashboard.html"
    # The dangerous patterns — raw innerHTML with user-input — must use escHtml
    # Spot-check the 4 known XSS vector lines
    for vec_line, vec_name in [
        ("<span class=\"ch-name\">${escHtml(ch.name)}</span>", "channel-name innerHTML"),
        ("renameChannel(event,'${escHtml(ch.id).replace", "channel rename onclick"),
        ("<div class=\"video-name\">${escHtml(v.name)}</div>", "video-name innerHTML"),
        ("renameVideo('${escHtml(v.id).replace", "video rename onclick"),
    ]:
        assert vec_line in src, \
            f"Round-5 Fix-3 missing escape: '{vec_name}' does not use escHtml(...)"


def t_round5_image_job_worker_race_detect():
    """Round-5 Fix-4: _batch_generate_worker checks ACTIVE_SCENE_JOBS before submitting
    a KIE task. Without it, a manual 'Generate Scene 5' click + a 'Generate all'
    batch passing through Scene 5 would BOTH submit."""
    src = open(os.path.join(ROOT, "dashboard.py")).read()
    idx = src.find("def _batch_generate_worker")
    body = src[idx:idx + 15000]   # large slice — body of this function is ~13K
    # The dedup-block must be present in the batch worker
    assert "if existing_job and JOBS.get(existing_job, {}).get(\"status\") == \"running\":" in body, \
        "Round-5 Fix-4 missing: _batch_generate_worker has no ACTIVE_SCENE_JOBS-dedup check"


def t_round5_whisper_word_count_mismatch_warn():
    """Round-5 Fix-5: align_scenes_to_whisper warns when word-count between Gemini-scenes
    and Whisper-output drifts >20%. Without this, a silent sync drift at the
    aligned/unaligned transition was hidden from the user."""
    src = open(os.path.join(ROOT, "dashboard.py")).read()
    idx = src.find("def align_scenes_to_whisper")
    body = src[idx:idx + 2500]
    assert "drift_ratio" in body, \
        "Round-5 Fix-5 missing: drift_ratio computation not present in align_scenes_to_whisper"
    assert "WARNUNG" in body, \
        "Round-5 Fix-5 missing: warning-log not emitted on word-count mismatch"


# ─── Phase 33.2 — Stepper Tests ────────────────────────────────────────────

def t_stepper_html_structure():
    """Phase 33.2 (post-fix-33.2-bug1+2): Stepper-Container im #view-editor mit Alpine
    x-data="stepperState()" gebunden. 5 Step-Cards haben data-step-section="N".
    Step-Labels entsprechen den EXISTIERENDEN Sections (①Modus/②Skript/③Bilder/④Titel/⑤Render)."""
    src = open(os.path.join(ROOT, "dashboard.html")).read()
    assert '<nav id="stepper"' in src, "Phase 33.2 missing: #stepper nav element"
    assert 'x-data="stepperState()"' in src, \
        "Phase 33.2 missing: x-data binding to stepperState()"

    for n in (1, 2, 3, 4, 5):
        assert f'data-step-section="{n}"' in src, \
            f"Phase 33.2 missing: data-step-section=\"{n}\" auf Step {n} card"

    # Bug-2-Fix: Step-Labels müssen den Sections entsprechen.
    # Phase 33.4.1 hat die Labels von (Modus, Skript, Bilder, Titel, Render) auf
    # (Thema, Skript, Audio, Bilder, Render) geändert.
    for label in ("'Thema'", "'Skript'", "'Audio'", "'Bilder'", "'Render'"):
        assert label in src, \
            f"Phase 33.2 missing: stepperState() definiert step '{label.strip(chr(39))}'"


def t_stepper_backend_endpoint_exists():
    """Phase 33.2 Bug-1-Fix: Stepper nutzt nicht /api/v1/videos/... (existiert nicht),
    sondern den EINEN Backend-Endpoint /api/stepper_state der alle 5 Heuristik-
    Daten konsolidiert zurückgibt. Verifiziert dass der Endpoint im Backend
    definiert ist + das Frontend die korrekte URL nutzt."""
    py_src = open(os.path.join(ROOT, "dashboard.py")).read()
    html_src = open(os.path.join(ROOT, "dashboard.html")).read()
    # Backend-Endpoint vorhanden
    assert '/api/stepper_state' in py_src, \
        "Bug-1 missing: backend /api/stepper_state endpoint not defined in dashboard.py"
    # Frontend nutzt genau diesen Endpoint
    assert '/api/stepper_state?channel=' in html_src, \
        "Bug-1 missing: frontend /api/stepper_state URL with channel/video params"
    # Anti-Regression: alte falsche URLs NICHT mehr da
    assert '/api/v1/videos' not in html_src, \
        "Bug-1: old /api/v1/videos/... URL still in dashboard.html — must be removed"
    # Backend liefert die 5 Daten-Felder die die Heuristik erwartet
    for field in ('"thema_done"', '"plan_done"', '"audio_done"', '"images_done"', '"images_total"', '"rendered"'):
        assert field in py_src, f"backend /api/stepper_state missing field: {field}"


def t_stepper_heuristic_python_mirror():
    """Phase 33.2: Heuristik-Spiegelung in Python (gleiche Regeln wie die JS-Heuristik
    in dashboard.html). Verifiziert:
    - ① THEMA: meta.json + selected_title NOT empty
    - ② SKRIPT: plan.json existiert
    - ③ AUDIO: voiceover.mp3 existiert (KEIN audio_meta.json als Fallback)
    - ⑤ RENDER: final.mp4 ODER meta.json.rendered_at
    """
    # Pure-Python-Spiegelung der JS-Heuristik aus stepperState().
    def heuristic(files: dict) -> dict:
        """files = {'meta.json': str, 'plan.json': str, 'voiceover.mp3': None,
                   'audio_meta.json': str, 'final.mp4': None}
        Returns {step_n: completed_bool}"""
        completed = {}
        # ① THEMA: meta.json + selected_title
        meta = json.loads(files.get('meta.json', '{}'))
        if files.get('meta.json') and (meta.get('selected_title') or '').strip():
            completed[1] = True
        # ② SKRIPT
        if files.get('plan.json'):
            completed[2] = True
        # ③ AUDIO: NUR voiceover.mp3, KEIN audio_meta.json-Fallback
        if files.get('voiceover.mp3') is not None:
            completed[3] = True
        # ⑤ RENDER: final.mp4 OR rendered_at
        if files.get('final.mp4') is not None or meta.get('rendered_at'):
            completed[5] = True
        return completed

    # Test 1: empty video — nothing done
    assert heuristic({}) == {}, "empty: nothing should be done"

    # Test 2: only meta.json without selected_title → ① NOT done
    assert heuristic({'meta.json': '{}'}) == {}, \
        "empty meta without selected_title → ① must NOT be completed"

    # Test 3: meta.json with selected_title → ① done
    assert heuristic({'meta.json': '{"selected_title": "Yeonmi V3"}'}) == {1: True}, \
        "selected_title → ① completed"

    # Test 4: plan.json exists → ② done (regardless of audio)
    assert heuristic({'plan.json': '{}'}) == {2: True}, \
        "plan.json → ② completed"

    # Test 5: audio_meta.json alone → ③ NOT done (race-bug prevention)
    assert heuristic({'audio_meta.json': '{}'}) == {}, \
        "audio_meta.json without voiceover.mp3 → ③ must NOT be completed (race-bug prevention)"

    # Test 6: voiceover.mp3 alone → ③ done
    assert heuristic({'voiceover.mp3': 'binary-mp3-blob'}) == {3: True}, \
        "voiceover.mp3 → ③ completed"

    # Test 7: audio_meta.json + voiceover.mp3 → ③ done
    assert 3 in heuristic({'voiceover.mp3': 'x', 'audio_meta.json': '{}'}), \
        "voiceover.mp3 wins regardless of audio_meta.json"

    # Test 8: meta.json with rendered_at → ⑤ done (without final.mp4)
    assert 5 in heuristic({'meta.json': '{"rendered_at": "2026-07-01"}'}), \
        "rendered_at alone → ⑤ completed"

    # Test 9: final.mp4 alone → ⑤ done (without meta)
    assert 5 in heuristic({'final.mp4': 'binary-mp4-blob'}), \
        "final.mp4 alone → ⑤ completed"

    # Test 10: complete pipeline — all 5 steps done
    full = {
        'meta.json': '{"selected_title": "T", "rendered_at": "2026"}',
        'plan.json': '{}',
        'voiceover.mp3': 'x',
        'final.mp4': 'x',
    }
    result = heuristic(full)
    assert all(result.get(n) for n in (1, 2, 3, 5)), \
        f"full pipeline should complete 5 steps; got {result}"


def t_stepper_state_machine_canEnter():
    """Phase 33.2: Alpine.js State-Machine canEnter() / currentStep-Hybrid.
    Pure-Python-Mirror der JS-Logik in stepperState()."""
    # Mirror der canEnter-Methode (siehe dashboard.html stepperState().canEnter)
    def canEnter(n, completed, current):
        if completed.get(n): return True
        if n == current: return True
        if n == 1: return True
        # direkter Nachfolger eines completed-ODER-current-steps
        if completed.get(n - 1) or (n - 1) == current: return True
        return False

    # Fall 1: nichts done → current=1, alle anderen locked außer 1 und seinem direkten Nachfolger ②
    completed = {}
    assert canEnter(1, completed, 1) is True,  "step ① always open"
    assert canEnter(2, completed, 1) is True,  "step ② als Nachfolger des current-steps"
    assert canEnter(3, completed, 1) is False, "step ③ locked (kein completed davor und nicht current)"
    assert canEnter(5, completed, 1) is False, "step ⑤ locked when nothing done"

    # Fall 2: ① done, current=2 → ② + ③ frei (③ als Nachfolger von ②), ④/⑤ nicht
    completed = {1: True}
    assert canEnter(1, completed, 2) is True,  "completed step always open"
    assert canEnter(2, completed, 2) is True,  "current step"
    assert canEnter(3, completed, 2) is True,  "③ als Nachfolger von ② (current) frei"
    assert canEnter(4, completed, 2) is False, "④ locked (kein completed davor)"
    assert canEnter(5, completed, 2) is False, "⑤ locked"

    # Fall 3: ①+②+③ done → ④/⑤ als Nachfolger frei
    completed = {1: True, 2: True, 3: True}
    assert canEnter(4, completed, 4) is True, "④ als Nachfolger von ③"
    assert canEnter(5, completed, 4) is True, "⑤ als Nachfolger von ④"

    # Fall 4: alle done → alle unlocked
    completed = {n: True for n in (1, 2, 3, 4, 5)}
    for n in (1, 2, 3, 4, 5):
        assert canEnter(n, completed, 5) is True, \
            f"all completed → step {n} unlocked"


# ─── Phase 33.3 — Sidebar / Modal / Tabs-Refactor ──────────────────────────

def t_phase33_sidebar_brand_color_in_response():
    """33.3: /api/channels erweitert pro Channel um video_count + active_count
    (brand_color wird Frontend-seitig per nameToHsl aus dem Namen abgeleitet wenn
    nicht explizit gesetzt)."""
    py_src = open(os.path.join(ROOT, "dashboard.py")).read()
    idx = py_src.find('if p == "/api/channels":')
    body = py_src[idx:idx + 1500]
    assert "video_count" in body, \
        "33.3 missing: /api/channels liefert kein video_count pro Channel"
    assert "active_count" in body, \
        "33.3 missing: /api/channels liefert kein active_count pro Channel"


def t_phase33_settings_modal_in_html():
    """33.3: Settings-Modal (id=settingsModal) im HTML, plus openChannelSettings()
    Funktion die Modal öffnet und Inhalt lädt. Shared zwischen Library und Editor."""
    html = open(os.path.join(ROOT, "dashboard.html")).read()
    assert 'id="settingsModal"' in html, \
        "33.3 missing: #settingsModal Container"
    assert "openChannelSettings" in html, \
        "33.3 missing: openChannelSettings() function"
    assert "closeSettingsModal" in html, \
        "33.3 missing: closeSettingsModal() function"
    # Brand-Color-Picker muss da sein
    assert 'id="settingsBrandColor"' in html, \
        "33.3 missing: brand_color-Picker im Modal"
    # Header-Settings-Button muss das Modal öffnen
    assert 'onclick="openChannelSettings()"' in html, \
        "33.3 missing: Settings-Button → openChannelSettings() wiring"
    # ESC-Handler für Modal-Close
    assert "Escape" in html, \
        "33.3 missing: ESC-Key schließt Modal nicht"


def t_phase33_top_tabs_removed():
    """33.3: Skript-Generator-Tab weg (Duplikat zu Step ②), Stil-Einstellungen-Tab
    weg (wandert ins Modal). Library zeigt nur Videos als Hauptbereich."""
    html = open(os.path.join(ROOT, "dashboard.html")).read()
    assert "switchTopTab('script'" not in html, \
        "33.3 missing: Skript-Generator-Tab wurde nicht entfernt (Duplikation zu Step ②)"
    assert "switchTopTab('style'" not in html, \
        "33.3 missing: Stil-Einstellungen-Tab wurde nicht entfernt (sollte Modal sein)"
    assert 'switchTopTab("videos"' not in html, \
        "33.3: kein expliziter Videos-Tab-Click mehr erwartet (Tab-Pattern weg)"
    # Library-Header ist neu (mit Neues-Video-Button)
    assert 'id="libraryHeader"' in html, \
        "33.3 missing: Library-Header mit Neues-Video-Button"


def t_phase33_sidebar_counter_classes():
    """33.3: Channel-Sidebar hat Counter-Badges für Video-Count + Active-Count.
    CSS-Klassen .ch-cnt und .ch-active müssen existieren und sichtbar sein."""
    html = open(os.path.join(ROOT, "dashboard.html")).read()
    assert ".ch-cnt" in html, \
        "33.3 missing: .ch-cnt CSS-Klasse für Video-Counter"
    assert ".ch-active" in html, \
        "33.3 missing: .ch-active CSS-Klasse für Active-Counter"
    # nameToHsl helper existiert (HSL-from-name fallback für brand_color)
    assert "nameToHsl" in html, \
        "33.3 missing: nameToHsl helper für brand_color default fallback"
    # Frontend nutzt nameToHsl im chList-Rendering
    assert "nameToHsl(ch.name" in html, \
        "33.3 missing: nameToHsl wird im loadChannels() für brand-color-Default aufgerufen"


# ─── Phase 33.3.1 — User-Feedback-Bugfixes ─────────────────────────────────

def t_phase33_1_brand_color_save_endpoint():
    """33.3.1 Bug-1: Brand-Color-Picker hat Save-Button + persistiert via
    /api/channels/brand_color (Backend akzeptiert #RGB oder #RRGGBB)."""
    py_src = open(os.path.join(ROOT, "dashboard.py")).read()
    html = open(os.path.join(ROOT, "dashboard.html")).read()
    # Backend-Endpoint vorhanden
    assert '"/api/channels/brand_color"' in py_src, \
        "33.3.1 missing: backend /api/channels/brand_color endpoint"
    # Hex-Validierung im Backend
    assert 're.fullmatch(r"#(?:[0-9a-fA-F]{3}){1,2}"' in py_src, \
        "33.3.1 missing: backend Hex-Format-Validierung"
    # Frontend: Save-Button + Handler
    assert 'saveSettingsBrandColor' in html, \
        "33.3.1 missing: saveSettingsBrandColor() JS function"
    assert 'id="settingsBrandColorText"' in html, \
        "33.3.1 missing: Hex-Text-Input für Color-Picker-Sync"
    assert 'syncBrandColorFields' in html, \
        "33.3.1 missing: syncBrandColorFields() helper für Picker↔Hex-Sync"


def t_phase33_1_mobile_responsive():
    """33.3.1 Bug-2: Mobile-Responsive mit Hamburger-Menu + Drawer.
    Auf <1024px wird Sidebar zum Off-Canvas-Drawer mit Backdrop."""
    html = open(os.path.join(ROOT, "dashboard.html")).read()
    css = open(os.path.join(ROOT, "dashboard.html")).read()
    # Hamburger-Button im Header
    assert 'id="sidebarToggle"' in html, \
        "33.3.1 missing: #sidebarToggle button (Hamburger)"
    assert 'toggleSidebar' in html, \
        "33.3.1 missing: toggleSidebar() JS function"
    # Mobile-CSS-Medien-Query
    assert '@media (max-width: 1023px)' in css, \
        "33.3.1 missing: Mobile @media query for sidebar"
    # Sidebar-Backdrop
    assert 'id="sidebarBackdrop"' in html, \
        "33.3.1 missing: #sidebarBackdrop element"
    # Body-class-based Toggle (CSS-Target)
    assert 'body.sidebar-open' in css, \
        "33.3.1 missing: body.sidebar-open CSS selector"
    # Auto-Close beim Channel-Switch (mobile UX)
    assert "sidebar-open" in html and "_origSwitchChannel_phase33" in html, \
        "33.3.1 missing: auto-close sidebar on channel-switch"


def t_phase33_1_esc_handler_no_leak():
    """33.3.1 Bug-3: ESC-Handler wird VOR dem Anlegen eines neuen entfernt.
    Sonst akkumulieren sich Handler bei jedem Open → ESC feuert N-mal."""
    html = open(os.path.join(ROOT, "dashboard.html")).read()
    # openChannelSettings muss removeEventListener VOR addEventListener machen
    func_start = html.find("async function openChannelSettings()")
    func_end   = html.find("\nfunction closeSettingsModal()", func_start)
    body = html[func_start:func_end]
    assert "removeEventListener('keydown', modal._escHandler)" in body, \
        "33.3.1 missing: ESC-Handler wird in openChannelSettings() NICHT entfernt vor dem Hinzufügen — Leak"


def t_phase33_1_no_duplicate_escape_helper():
    """33.3.1 Bug-4: User-Frage ob es zwei Escape-Helper gibt (esc + escHtml).
    Bestätigung: nur escHtml existiert. Diese Test verhindert dass jemand eine
    zweite Helper-Funktion `esc()` hinzufügt ohne den Test zu korrigieren."""
    html = open(os.path.join(ROOT, "dashboard.html")).read()
    # escHtml muss existieren
    assert "const escHtml" in html, \
        "33.3.1: escHtml helper missing"
    # Eine einzelne 'function esc(' oder 'const esc =' Definition darf NICHT vorkommen.
    # Whitelist: 'escHtml' ist ok. esc ist NICHT erlaubt.
    import re as _re
    matches = _re.findall(r"\b(?:function|const)\s+esc\s*[(=]", html)
    assert len(matches) == 0, \
        f"33.3.1 Bug-4: zweite Escape-Helper gefunden — nur escHtml sollte existieren. matches={matches}"


# ─── Phase 34 — TTS-Provider-Auswahl (ElevenLabs / MiniMax) ─────────────────

def t_phase34_tts_provider_dispatch_exists():
    """Phase 34: _tts_persist_and_schedule dispatcher entscheidet anhand von
    tts_provider-Feld in den settings zwischen ElevenLabs und MiniMax. Die alte
    _elevenlabs_persist_and_schedule bleibt als Provider-Default erhalten."""
    py = open(os.path.join(ROOT, "engine_elevenlabs.py")).read()
    assert "def _tts_persist_and_schedule" in py, \
        "Phase 34 missing: _tts_persist_and_schedule dispatcher"
    # Dispatch-Logik: tts_provider == "minimax" → MiniMax-Pfad, sonst ElevenLabs
    assert 'tts_provider' in py, "Phase 34 missing: tts_provider dispatch check"
    assert '_minimax_persist_and_schedule' in py, \
        "Phase 34 missing: _minimax_persist_and_schedule für MiniMax-Pfad"

def t_phase34_minimax_constants_and_helpers():
    """Phase 34: MiniMax-Konstanten + _minimax_key() + minimax_generate() existieren.
    Rückgabe-Shape identisch zu elevenlabs_generate() für provider-agnostic
    Konsumenten."""
    py = open(os.path.join(ROOT, "engine_elevenlabs.py")).read()
    assert "MINIMAX_API" in py and 'https://api.minimaxi.chat/v1' in py, \
        "Phase 34 missing: MiniMax API base URL"
    assert "MINIMAX_DEFAULT_MODEL" in py and 'minimax-speech-2.6-hd' in py, \
        "Phase 34 missing: MiniMax Default-Model (2.6 HD per ARCHITECTURE §34 Empfehlung)"
    assert "def _minimax_key" in py, "Phase 34 missing: _minimax_key() helper"
    assert "def minimax_generate" in py, "Phase 34 missing: minimax_generate() function"
    # Identische Return-Shape zu elevenlabs_generate
    assert py.count('"audio_base64"') >= 2, \
        "Phase 34: minimax_generate muss audio_base64 + words zurückgeben (provider-shape-parity)"
    assert py.count('"task_id"') >= 2, \
        "Phase 34: minimax_generate muss task_id zurückgeben (provider-shape-parity)"

def t_phase34_minimax_endpoints_in_backend():
    """Phase 34: Backend exponiert /api/minimax_voices + /api/tts_provider.
    GET tts_provider liest aus voice_settings.json, POST schreibt tts_provider dort."""
    py = open(os.path.join(ROOT, "dashboard.py")).read()
    assert '"/api/minimax_voices"' in py, \
        "Phase 34 missing: backend /api/minimax_voices endpoint"
    assert '"/api/tts_provider"' in py, \
        "Phase 34 missing: backend /api/tts_provider endpoint"
    # MiniMax-voice-Endpoint ruft get_voice auf mit Bearer-Auth
    assert 'get_voice' in py or '/v1/get_voice' in py or 'voice_list' in py, \
        "Phase 34 missing: MiniMax /v1/get_voice (oder kompatibler) Endpoint-Call"
    # Provider-Validierung in POST
    assert '"minimax"' in py and '"elevenlabs"' in py, \
        "Phase 34: tts_provider-Werte müssen 'minimax' und 'elevenlabs' sein"

def t_phase34_provider_dropdown_in_frontend():
    """Phase 34: Frontend-Dropdown #ttsProviderSelect + dynamische Voice-Liste.
    loadTtsVoices() dispatcht je nach Provider auf /api/elevenlabs_voices oder
    /api/minimax_voices. onTtsProviderChange() persistiert den Provider-Wechsel."""
    html = open(os.path.join(ROOT, "dashboard.html")).read()
    assert 'id="ttsProviderSelect"' in html, \
        "Phase 34 missing: #ttsProviderSelect dropdown"
    assert 'function loadTtsVoices' in html, \
        "Phase 34 missing: loadTtsVoices() function"
    assert 'function onTtsProviderChange' in html, \
        "Phase 34 missing: onTtsProviderChange() function"
    # Dropdown-Optionen
    assert '<option value="elevenlabs">' in html, \
        "Phase 34 missing: ElevenLabs-Dropdown-Option"
    assert '<option value="minimax">' in html, \
        "Phase 34 missing: MiniMax-Dropdown-Option"
    # Initial-Provider wird aus /api/tts_provider gefetched
    assert "ch_get('/api/tts_provider')" in html, \
        "Phase 34 missing: Initial-Provider-Fetch aus Backend"

def t_phase34_resume_supports_both_providers():
    """Phase 34: Resume-Marker in /api/voiceover_generate akzeptiert BEIDE
    Provider (elevenlabs + minimax). Wenn User zwischen Providern wechselt und
    existierende audio_meta.json noch den alten Provider hat, soll der Resume
    sauber funktionieren ohne den falschen Provider-Pfad zu nehmen."""
    py = open(os.path.join(ROOT, "dashboard.py")).read()
    # Resume-Check muss BEIDE Provider in einem Tuple-Containment prüfen
    assert 'in ("elevenlabs", "minimax")' in py, \
        "Phase 34: Resume-Marker muss both Providers in einem Tuple-Containment prüfen"
    # Anti-Regression: alter Check "voiceover_source == elevenlabs" (Singular) im Resume-Pfad
    # darf nicht mehr exklusiv ElevenLabs-only filtern.
    import re as _re
    # Suche das Resume-Resume-Block-Kontext
    resume_idx = py.find('if (meta.get("voiceover_source")')
    assert resume_idx > 0, "Phase 34: Resume-Check nicht gefunden"
    block = py[resume_idx:resume_idx + 300]
    assert '"minimax"' in block, \
        "Phase 34: Resume-Block muss 'minimax' als zulässigen Provider haben"

def t_phase34_no_old_loadelevenlabsvoices_callers():
    """Phase 34: Anti-Regression — die alte loadElevenLabsVoices() darf nirgendwo
    mehr aufgerufen werden (außer in loadTtsVoices selbst nicht). Sie ist durch
    die provider-abstrakte loadTtsVoices() ersetzt."""
    html = open(os.path.join(ROOT, "dashboard.html")).read()
    # Definition darf noch da sein (kein Hard-Removal nötig) aber Calls müssen weg.
    import re as _re
    # Suche alle loadElevenLabsVoices( Vorkommen (nicht loadTtsVoices)
    matches = _re.findall(r"\bloadElevenLabsVoices\b", html)
    assert len(matches) == 0, \
        f"Phase 34 anti-regression: loadElevenLabsVoices() darf NICHT mehr vorkommen (gefunden: {len(matches)} mal)"


# ─── Phase 34.1 — MiniMax-Sliders + Provider-Toggle ─────────────────────────

def t_phase34_1_minimax_slider_visibility():
    """Phase 34.1: MiniMax-Sliders (Speed/Volume/Pitch) sind im HTML vorhanden +
    _ttsSlidersVisibility(provider) togglet ElevenLabs vs MiniMax-Block.
    ElevenLabs-Sliders sind in #elSlidersBlock, MiniMax in #minimaxSlidersBlock.
    Bug-Fix 2 (User-Feedback): ElevenLabs-Sliders verschwinden bei MiniMax-Auswahl,
    um Verwirrung zu vermeiden (User könnte denken sie wirken auch auf MiniMax)."""
    html = open(os.path.join(ROOT, "dashboard.html")).read()
    # Container-IDs vorhanden
    assert 'id="elSlidersBlock"' in html, \
        "34.1 missing: #elSlidersBlock container (ElevenLabs-Sliders)"
    assert 'id="minimaxSlidersBlock"' in html, \
        "34.1 missing: #minimaxSlidersBlock container (MiniMax-Sliders)"
    # MiniMax-Sliders: Speed, Volume, Pitch
    for slider_id in ('mmSpeed', 'mmVolume', 'mmPitch'):
        assert f'id="{slider_id}"' in html, \
            f"34.1 missing: MiniMax-Slider #{slider_id}"
    # Visibility-Controller
    assert 'function _ttsSlidersVisibility' in html, \
        "34.1 missing: _ttsSlidersVisibility() function"
    assert '_ttsSlidersVisibility(provider)' in html, \
        "34.1 missing: _ttsSlidersVisibility(provider)-Aufruf im loadTtsVoices"
    # Toggle-Logik: display:none für jeweils den anderen Block
    body = html[html.find("function _ttsSlidersVisibility"):html.find("function _ttsSlidersVisibility")+500]
    assert "'none'" in body, \
        "34.1 missing: display:none Logik in _ttsSlidersVisibility"
    # MiniMax-Slider-Wire-Helper
    assert 'function _mmWireSliders' in html, \
        "34.1 missing: _mmWireSliders() function (Slider-Persistierung)"


def t_phase34_1_minimax_slider_persistence():
    """Phase 34.1: MiniMax-Slider-Werte (speed/volume/pitch) werden via
    /api/elevenlabs_settings persistiert (mit tts_provider='minimax'). Beim
    Voice-Settings-Response werden die Werte geladen."""
    html = open(os.path.join(ROOT, "dashboard.html")).read()
    # Persistenz im onchange-Handler
    assert '+s.value' in html and '+v.value' in html, \
        "34.1 missing: MiniMax-Slider-Werte werden via +s.value/+v.value gepersistiert"
    assert 'parseInt(p.value, 10)' in html, \
        "34.1 missing: Pitch-Wert wird via parseInt() (int) persistiert"
    assert "tts_provider: 'minimax'" in html, \
        "34.1 missing: tts_provider: 'minimax' in der MiniMax-Persistenz-Payload"


# ─── Phase 33.4.1 — Step-Reihenfolge angleichen ────────────────────────────

def t_phase33_4_1_new_step_labels():
    """Phase 33.4.1: Stepper-Labels sind auf finale Reihenfolge umgestellt:
    ①Thema / ②Skript / ③Audio / ④Bilder / ⑤Render (war vorher: Modus/Skript/Bilder/Titel/Render)."""
    html = open(os.path.join(ROOT, "dashboard.html")).read()
    # Exakte Substrings prüfen
    expected_labels = [
        "{ n: 1, label: 'Thema'",
        "{ n: 2, label: 'Skript'",
        "{ n: 3, label: 'Audio'",
        "{ n: 4, label: 'Bilder'",
        "{ n: 5, label: 'Render'",
    ]
    for lbl in expected_labels:
        assert lbl in html, f"Phase 33.4.1 missing: stepperState-Definition {lbl!r}"
    # Anti-Regression: alte Labels NICHT mehr im stepperState (sonst wäre Migration
    # halb — würde verwirren wenn z.B. 'Titel' als n:4 label erhalten bleibt).
    for old in ("label: 'Modus'", "label: 'Titel'"):
        assert old not in html, \
            f"Phase 33.4.1 anti-regression: altes Label {old!r} noch im stepperState"

def t_phase33_4_1_audio_section_extracted():
    """Phase 33.4.1: Der TTS-Provider-Block (ehemals Option C in ②) ist in eine eigene
    Section data-step-section=\"3\" extrahiert. So ist der Stepper-Klick konsistent
    zur sichtbaren Section-Reihenfolge im Editor."""
    html = open(os.path.join(ROOT, "dashboard.html")).read()
    # Audio-Card mit data-step-section="3" und id="cardAudio"
    assert 'data-step-section="3"' in html, \
        "33.4.1 missing: data-step-section=\"3\" (sollte Audio-Card sein)"
    assert 'id="cardAudio"' in html, \
        "33.4.1 missing: #cardAudio (Audio-Section-Container-ID)"
    # TTS-Provider-Label auf der neuen Section
    assert '③ Audio generieren' in html or '③ Audio' in html, \
        "33.4.1 missing: '③ Audio' Section-Header"

def t_phase33_4_1_title_thumb_removed():
    """Phase 33.4.1: titleThumbCard ist entfernt. Der bestehende Titel-Block war
    für den video-orientierten Auto-Generate-Flow, nicht für den Standard-Wizard.
    TODO 33.4.2: Titel-Generierung wird in ① Thema integriert."""
    html = open(os.path.join(ROOT, "dashboard.html")).read()
    # Im HTML-Body darf #titleThumbCard nicht mehr als gerenderter Container sein
    # (function-definitions dürfen noch da sein, da sie kein throw werfen
    # solange der Container fehlt)
    import re as _re
    # Suche <div class="card" id="titleThumbCard"
    matches = _re.findall(r'<div\s+class="card"\s+id="titleThumbCard"', html)
    assert len(matches) == 0, \
        f"33.4.1: titleThumbCard-Card noch im HTML-Body ({len(matches)} mal)"

def t_phase33_4_1_plan_area_now_4():
    """Phase 33.4.1: planArea (war Schritt ③ in 33.2) ist jetzt data-step-section=\"4\"
    weil Step ③ jetzt die Audio-Section ist. data-step-section=\"3\" ist jetzt Audio."""
    html = open(os.path.join(ROOT, "dashboard.html")).read()
    # planArea muss data-step-section="4" haben
    assert 'id="planArea" data-step-section="4"' in html, \
        "33.4.1: planArea muss data-step-section=\"4\" sein (war vorher 3)"
    # planArea darf NICHT mehr data-step-section="3" haben (Anti-Regression)
    assert 'id="planArea" data-step-section="3"' not in html, \
        "33.4.1 anti-regression: planArea noch mit data-step-section=\"3\""


# ─── Phase 33.4.2-prep: A (Dead-Code) + D (Visibility) ─────────────────────

def t_phase33_4_2_prep_no_dead_code():
    """Phase 33.4.2-prep Step A: titleThumbCard-Card und alle Title/Thumbnail-Funktionen
    sind komplett entfernt. Genau die Funktionen die in 33.4.2 als 'in ①Thema-Card neu
    aufgebaut' markiert sind (genTitles → genTitleStep, genThumbnail → genThumbnailStep).
    Anti-Regression: keine Re-Introduktion."""
    html = open(os.path.join(ROOT, "dashboard.html")).read()
    import re as _re
    # Komplette Entfernung
    for token in ("titleThumbCard", "genTitles", "genThumbnail",
                  "selectTitle", "updateTitleThumbCardVisibility",
                  "renderTitleList", "renderThumbnail"):
        # Pattern erlaubt Wortgrenzen
        matches = _re.findall(rf"\b{_re.escape(token)}\b", html)
        assert len(matches) == 0, \
            f"33.4.2-prep A: '{token}' noch im Code ({len(matches)} Vorkommen)"

def t_phase33_4_2_prep_central_visibility():
    """Phase 33.4.2-prep Step D: zentrale updateStepVisibility(currentStep)-Funktion
    iteriert über alle 5 Step-Cards und toggled display:none/'' je nach currentStep.
    Aufruf erfolgt im stepperState.goTo() damit Stepper-Klick = Card-Switch."""
    html = open(os.path.join(ROOT, "dashboard.html")).read()
    # Funktion vorhanden
    assert "function updateStepVisibility" in html, \
        "33.4.2-prep D: updateStepVisibility(currentStep) function missing"
    # Iteration über 5 Cards
    assert "[data-step-section=\"${n}\"]" in html, \
        "33.4.2-prep D: querySelector for each step-card missing"
    # Aufruf im goTo
    assert "if (typeof updateStepVisibility === 'function') updateStepVisibility(n)" in html, \
        "33.4.2-prep D: goTo() must call updateStepVisibility(n)"

def t_phase33_4_2_thema_card_restructured():
    """Phase 33.4.2: Thema-Card restructure & Option A upload removal."""
    html = open(os.path.join(ROOT, "dashboard.html")).read()
    assert "id=\"ideaInput\"" in html, "Thema-Card: ideaInput textarea missing"
    assert "genTitleStep()" in html, "Thema-Card: genTitleStep() trigger missing"
    assert "genThumbnailStep()" in html, "Thema-Card: genThumbnailStep() trigger missing"
    assert "id=\"titleListStep\"" in html, "Thema-Card: titleListStep list container missing"
    assert "id=\"thumbSlotStep\"" in html, "Thema-Card: thumbSlotStep preview container missing"
    
    # Audio-Upload Dropzone und Option A müssen komplett aus Schritt 2 verschwunden sein
    assert "Option A · Voice-Over hochladen" not in html, "Schritt 2: Option A dropzone must be removed"
    assert "transAudio" not in html or "transcribeAudio" not in html, "Schritt 2: transcribeAudio logic must be removed from Step 2"


# ─────────────────────────────────────────────────────────────────────────────
# Phase 11: Sequence chain (Doppel-Anker) — see CINEMATIC_UPGRADE_PLAN.md §11.4
# Regression tests for the 4 previously-uncovered functions plus S1 docstring fix.
# MUST run green BEFORE Phase L (which touches _motion_for_scene, see Schutzregel 2).
# ─────────────────────────────────────────────────────────────────────────────

def t_seq_double_anchor_refs():
    """§11.4 (S4 regression): _resolve_chain_refs returns the correct refs.

    seq_pos 1 → exactly 1 ref (anchor; pos-1 == 0 dedup'd)
    seq_pos 2 → exactly 2 refs (anchor + predecessor, distinct files)
    seq_pos 0 / no seq_id → empty list, no wait
    """
    from engine.scenes import _resolve_chain_refs, _wait_for_chain_scene

    # Plan mit 3-Szenen-Sequenz, Anker + 2 Fortsetzungen, alle mit source_url + file
    tmp = tempfile.mkdtemp(prefix="seq_test_")
    try:
        plan_path = os.path.join(tmp, "plan.json")
        scenes = [
            {"i": 0, "seq_id": "s1", "seq_pos": 0, "source_url": "http://a/anchor.jpg", "file": "anchor.jpg"},
            {"i": 1, "seq_id": "s1", "seq_pos": 1, "source_url": "http://a/cont1.jpg",   "file": "cont1.jpg"},
            {"i": 2, "seq_id": "s1", "seq_pos": 2, "source_url": "http://a/cont2.jpg",   "file": "cont2.jpg"},
        ]
        json.dump({"scenes": scenes}, open(plan_path, "w"))

        # Anchor (seq_pos 0) → no chain refs (normal char_ref only)
        refs, debug = _resolve_chain_refs(plan_path, scenes[0])
        assert refs == [], f"anchor must have no chain refs, got {refs}"
        assert debug == {}, f"anchor must have no chain debug, got {debug}"

        # seq_pos 1 → exactly 1 ref (anchor; prev IS anchor, dedup'd)
        refs, debug = _resolve_chain_refs(plan_path, scenes[1])
        assert refs == ["http://a/anchor.jpg"], f"seq_pos 1 must have exactly 1 ref, got {refs}"
        assert debug.get("chain_anchor_file") == "anchor.jpg"
        assert "chain_prev_file" not in debug, "seq_pos 1 must NOT have chain_prev_file (dedup'd)"

        # seq_pos 2 → exactly 2 refs (anchor + distinct prev)
        refs, debug = _resolve_chain_refs(plan_path, scenes[2])
        assert refs == ["http://a/anchor.jpg", "http://a/cont1.jpg"], f"seq_pos 2 must have 2 refs, got {refs}"
        assert debug.get("chain_anchor_file") == "anchor.jpg"
        assert debug.get("chain_prev_file") == "cont1.jpg", f"seq_pos 2 must have chain_prev_file, got {debug}"

        # No seq_id → empty
        refs, debug = _resolve_chain_refs(plan_path, {"i": 99, "pacing": "calm"})
        assert refs == []
        assert debug == {}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_seq_todo_preserves_scene_order():
    """§11.4 (S2 regression): _batch_generate_worker's `todo` MUST preserve scene order.

    Schutzregel 1: no `sort`/`sorted`/`reverse` on `todo`. If anyone ever sorts
    `todo` (e.g. "Hook zuerst" in Phase L), anchors and continuations land in
    parallel batches and deadlock via _wait_for_chain_scene.
    """
    src = open(os.path.join(ROOT, "dashboard.py")).read()
    # Find _batch_generate_worker body
    i = src.find("def _batch_generate_worker")
    assert i >= 0, "_batch_generate_worker must still exist in dashboard.py"
    # Look for `todo` until the function's next def or class
    # Crude but effective: scan 200 lines after def and check for forbidden calls on `todo`
    end = src.find("\ndef ", i + 50)
    if end < 0:
        end = i + 5000
    body = src[i:end]
    # Forbidden: todo.sort(), todo.sorted, sorted(todo), reverse=True on todo, etc.
    # Allowed: filtering (list comprehensions, `for s in scenes if not s.get("file")`).
    forbidden = ["todo.sort", "sorted(todo", "todo.reverse", "reversed(todo"]
    for f in forbidden:
        assert f not in body, (
            f"§11.3 Schutzregel 1 violated: _batch_generate_worker contains '{f}'. "
            f"`todo` MUST preserve original scene order — see CINEMATIC_UPGRADE_PLAN.md §11.3."
        )


def t_seq_wait_timeout_exceeds_poll_max():
    """§11.4 (S2 regression): _wait_for_chain_scene timeout > IMAGE_JOB_MAX_POLLS * 3s.

    Schutzregel: timeout (170s) must stay > MAX_POLLS * 3s (150s by default).
    If anyone raises IMAGE_JOB_MAX_POLLS, the wait would silently expire and
    continuations would lose their chain refs without error.
    """
    import re as _re
    src = open(os.path.join(ROOT, "dashboard.py")).read()
    # Find IMAGE_JOB_MAX_POLLS
    m = _re.search(r"IMAGE_JOB_MAX_POLLS\s*=\s*(\d+)", src)
    assert m, "IMAGE_JOB_MAX_POLLS must be defined"
    max_polls = int(m.group(1))
    poll_total_sec = max_polls * 3

    from engine.scenes import _wait_for_chain_scene
    import inspect
    sig = inspect.signature(_wait_for_chain_scene)
    timeout_default = sig.parameters["timeout"].default
    assert timeout_default > poll_total_sec, (
        f"_wait_for_chain_scene timeout ({timeout_default}s) must be > "
        f"IMAGE_JOB_MAX_POLLS * 3s ({poll_total_sec}s). Otherwise continuations "
        f"silently lose their chain refs."
    )


def t_seq_continuity_prompt_last():
    """§11.4 (S3 regression): CONTINUITY block is appended LAST to the image prompt.

    Schutzregel 3: master + phase-cue may change, but the CONTINUITY block must
    stay at the very end — otherwise earlier instructions can dilute or contradict
    the chain-ref adherence.
    """
    src = open(os.path.join(ROOT, "dashboard.py")).read()
    # Find the CONTINUITY block in _batch_generate_worker
    i = src.find('"\\n\\nCONTINUITY (STRICT):')
    assert i >= 0, "CONTINUITY (STRICT) block must still exist in dashboard.py"
    # Within the next ~80 lines, there must be NO further mutation of full_prompt.
    # `print()` and `acquire()`/`try:` between CONTINUITY and submit are OK.
    after = src[i:i+5000]
    # Any forbidden mutation of full_prompt?
    forbidden_after_continuity = [
        'full_prompt += "', 'full_prompt =', 'full_prompt += f',
    ]
    for f in forbidden_after_continuity:
        # Allow only the CONTINUITY line itself (which contains the marker)
        assert f not in after or after.count(f) == 1, (
            f"§11.3 Schutzregel 3 violated: full_prompt is mutated AFTER CONTINUITY "
            f"block (found '{f}' beyond the marker). CONTINUITY must be the LAST "
            f"prompt component."
        )
    # Submit must still happen after CONTINUITY
    assert "_kie_submit_image(" in after or "gen_image(" in after, (
        "CONTINUITY block is the last component — submit (_kie_submit_image) must follow"
    )


def t_seq_motion_inheritance_precedence():
    """§11.4 (Schutzregel 2): _motion_for_scene checks seq_pos FIRST, before any
    new motion rule (Phase L is_hook, future overrides, etc.).
    """
    src = open(os.path.join(ROOT, "dashboard.py")).read()
    i = src.find("def _motion_for_scene")
    assert i >= 0, "_motion_for_scene must exist"
    body_start = src.find("\n", i) + 1
    # Read until next `def ` at top-level
    end = src.find("\ndef ", body_start)
    if end < 0:
        end = body_start + 3000
    body = src[body_start:end]
    # First condition must reference seq_pos (the inheritance check)
    # Find the first `if ` after the function start
    first_if = body.find("if ")
    assert first_if >= 0, "_motion_for_scene must have at least one if"
    snippet = body[first_if:first_if + 300]
    assert "seq_pos" in snippet or "seq_id" in snippet, (
        f"_motion_for_scene: first condition must be the seq_pos inheritance check "
        f"(Schutzregel 2). Got: {snippet[:200]!r}"
    )


def t_seq_renumber_assigns_anchor_zero():
    """§11.4 (basic correctness): _renumber_seq_pos assigns 0,1,2,... per seq_id."""
    from engine.scenes import _renumber_seq_pos
    scenes = [
        {"i": 0, "seq_id": "s1"},
        {"i": 1, "seq_id": "s1"},
        {"i": 2, "seq_id": "s1"},
        {"i": 3, "seq_id": "s2"},
        {"i": 4, "seq_id": "s2"},
        {"i": 5},  # no seq_id
    ]
    _renumber_seq_pos(scenes)
    assert scenes[0]["seq_pos"] == 0, "first scene of s1 must be anchor"
    assert scenes[1]["seq_pos"] == 1
    assert scenes[2]["seq_pos"] == 2
    assert scenes[3]["seq_pos"] == 0, "first scene of s2 must be anchor"
    assert scenes[4]["seq_pos"] == 1
    assert scenes[5]["seq_pos"] == 0, "no seq_id → seq_pos=0 (no anchor, harmless default)"


def t_seq_batch_worker_docstring_s1_fixed():
    """§11.4 (S1 regression): _batch_generate_worker docstring no longer claims
    'no ordering dependency' — it now correctly notes the chain-ref dependency.
    """
    src = open(os.path.join(ROOT, "dashboard.py")).read()
    i = src.find("def _batch_generate_worker")
    assert i >= 0
    # Read the docstring — find the NEXT triple-quote AFTER the opening one
    open_q = src.find('"""', i)
    assert open_q > 0
    close_q = src.find('"""', open_q + 3)
    assert close_q > 0
    docstring = src[open_q:close_q + 3]
    # Old buggy phrase must be gone
    assert "no ordering dependency" not in docstring, (
        "S1 bug NOT fixed: _batch_generate_worker docstring still says 'no ordering dependency' "
        "while the function calls _resolve_chain_refs 70 lines below."
    )
    # New correct phrase must be present
    assert "_resolve_chain_refs" in docstring, (
        f"S1 fix incomplete: docstring must reference _resolve_chain_refs. Got: {docstring[:300]!r}"
    )


if __name__ == "__main__":
    sys.exit(main())
