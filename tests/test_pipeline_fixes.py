#!/usr/bin/env python3
"""test_pipeline_fixes.py — Regressionstests für den Juli-2026-Produktionsreife-Pass.

Deckt die Fixes aus dem Audit "ElevenLabs-Sync + Char-Refs + Frontend/Backend-Kontrakt"
ab: Chunk-Offset-Drift (A1), save_voice_settings-Clamp/Whitelist (K2/K5), Resume-
Precedence-Bug (K6), Char-Ref-Fallback-Stufen (A2/A3), Preserve-Helper (A5) und die
Partial-Render-Warnung (4.2).

Usage: python3 tests/test_pipeline_fixes.py

Gleicher Hand-Rolled-Runner wie tests/test_cinematic_e2e.py (kein pytest-Discovery,
Funktionsnamen bewusst `t_*` statt `test_*`, siehe README/ARCHITECTURE-Konvention).
Echte ElevenLabs-Calls werden gemockt; ffmpeg/ffprobe laufen ECHT (harte Abhängigkeit
der Pipeline ohnehin), damit der Chunk-Offset-Test die reale Bug-Klasse abdeckt statt
sie wegzumocken.
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import shutil
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

PASSED, FAILED = 0, 0


def run(fn, name):
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


TEST_CID = "test_pipeline_fixes_ch"
TEST_VID = "v1"


def setup():
    tmp_home = tempfile.mkdtemp(prefix="pipeline_fixes_test_")
    os.environ["HOME"] = tmp_home
    with open(os.path.expanduser("~/.elevenlabs_key"), "w") as f:
        f.write("sk_fake_for_test_only\n")
    import dashboard
    dashboard.ensure_channel(TEST_CID)
    dashboard.ensure_video(TEST_CID, TEST_VID)
    return tmp_home


def teardown(tmp_home):
    ch_root = os.path.join(ROOT, "channels", TEST_CID)
    if os.path.exists(ch_root):
        shutil.rmtree(ch_root, ignore_errors=True)
    if os.path.exists(tmp_home):
        shutil.rmtree(tmp_home, ignore_errors=True)


def _make_silent_mp3(seconds: float) -> bytes:
    """Real ffmpeg-generated silent MP3 of exactly `seconds` duration — used to
    simulate an ElevenLabs chunk whose real audio is LONGER than its last word-end
    (the trailing-silence behavior that caused the sync drift)."""
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        path = f.name
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono",
             "-t", str(seconds), path],
            check=True, capture_output=True, timeout=30,
        )
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.remove(path)


# --- A1: Chunk-Offset misst echte ffprobe-Dauer statt letztes Wort-Ende --------

def t_a1_mp3_duration_measures_real_file():
    import engine_elevenlabs as el
    audio = _make_silent_mp3(2.5)
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(audio)
        path = f.name
    try:
        dur = el._mp3_duration_sec(path)
        assert abs(dur - 2.5) < 0.05, f"expected ~2.5s, got {dur}"
    finally:
        os.remove(path)


def t_a1_chunk_offset_uses_real_duration_not_last_word_end():
    """The actual regression: ElevenLabs chunk 1's last word ends at 2.0s, but the
    real MP3 is 2.5s long (0.5s trailing silence — exactly what caused the drift on
    the user's video). Chunk 2's first word must be offset by the REAL 2.5s, not the
    word-derived 2.0s."""
    import engine_elevenlabs as el

    chunk_audio = _make_silent_mp3(2.5)
    chunk_b64 = base64.b64encode(chunk_audio).decode("ascii")

    call_log = []

    def fake_call(url, body, headers):
        call_log.append(body["text"])
        # Every chunk: last word ends at 2.0s, real audio (chunk_b64) is 2.5s.
        resp = {
            "audio_base64": chunk_b64,
            "alignment": {"words": [
                {"text": "Hello", "start": 0.0, "end": 1.0},
                {"text": "world.", "start": 1.2, "end": 2.0},
            ]},
        }
        return resp, f"req_{len(call_log)}"

    # Force exactly 2 chunks with a tiny fake limit so the test doesn't need a
    # multi-thousand-char script.
    with patch.object(el, "_elevenlabs_call_with_retry", side_effect=fake_call), \
         patch.object(el, "_chunk_limit_for_model", return_value=20):
        text = "Sentence one is here. Sentence two follows now."
        settings = {"voice_id": "test_voice", "model_id": "eleven_multilingual_v2"}
        result = el.elevenlabs_generate(text, settings)

    assert len(call_log) >= 2, f"expected >=2 chunk calls, got {len(call_log)}"
    words = result["words"]
    # First chunk's words are unshifted.
    assert words[0]["start"] == 0.0
    assert words[1]["end"] == 2.0
    # Second chunk's first word MUST be offset by the REAL 2.5s duration of chunk 1,
    # not by 2.0s (the old, wrong "last word end" heuristic).
    third_word = words[2]
    assert abs(third_word["start"] - 2.5) < 0.01, (
        f"chunk-2 offset used last-word-end (2.0s) instead of real ffprobe duration "
        f"(2.5s) — got start={third_word['start']}"
    )


def t_a1_model_dependent_chunk_limit():
    import engine_elevenlabs as el
    assert el._chunk_limit_for_model("eleven_v3") == 4800
    assert el._chunk_limit_for_model("eleven_multilingual_v2") == 9500
    assert el._chunk_limit_for_model("eleven_flash_v2_5") == 28000
    # Unknown model → conservative v3-level fallback, not unbounded
    assert el._chunk_limit_for_model("some_future_model") == el.EL_CHUNK_CHAR_LIMIT


def t_a1_request_id_comes_from_header_not_fake():
    """previous_request_ids must be built from REAL request-id headers (ElevenLabs
    docs: stitching needs the actual ID from a completed response), not a locally
    fabricated string — the old code generated `el_{voice}_{idx}_{time}` which the
    API would silently ignore/reject."""
    import engine_elevenlabs as el
    chunk_audio = _make_silent_mp3(1.0)
    chunk_b64 = base64.b64encode(chunk_audio).decode("ascii")
    seen_previous_ids = []

    def fake_call(url, body, headers):
        if "previous_request_ids" in body:
            seen_previous_ids.append(body["previous_request_ids"])
        resp = {"audio_base64": chunk_b64,
                "alignment": {"words": [{"text": "hi", "start": 0.0, "end": 0.5}]}}
        return resp, "real-request-id-abc123"

    with patch.object(el, "_elevenlabs_call_with_retry", side_effect=fake_call), \
         patch.object(el, "_chunk_limit_for_model", return_value=20):
        settings = {"voice_id": "test_voice", "model_id": "eleven_multilingual_v2"}
        el.elevenlabs_generate("Sentence one here. Sentence two here too.", settings)

    assert seen_previous_ids, "expected at least one call with previous_request_ids"
    assert seen_previous_ids[0] == ["real-request-id-abc123"], (
        f"previous_request_ids must be the real header value, got {seen_previous_ids[0]}"
    )


def t_a1_v3_never_gets_previous_request_ids():
    import engine_elevenlabs as el
    chunk_audio = _make_silent_mp3(1.0)
    chunk_b64 = base64.b64encode(chunk_audio).decode("ascii")
    bodies = []

    def fake_call(url, body, headers):
        bodies.append(dict(body))
        resp = {"audio_base64": chunk_b64,
                "alignment": {"words": [{"text": "hi", "start": 0.0, "end": 0.5}]}}
        return resp, "req-1"

    with patch.object(el, "_elevenlabs_call_with_retry", side_effect=fake_call), \
         patch.object(el, "_chunk_limit_for_model", return_value=20):
        settings = {"voice_id": "test_voice", "model_id": "eleven_v3"}
        el.elevenlabs_generate("Sentence one here. Sentence two here too.", settings)

    assert len(bodies) >= 2
    assert "previous_request_ids" not in bodies[1], \
        "eleven_v3 must never receive previous_request_ids (API rejects it)"


# --- K2/K5: save_voice_settings Clamp + Whitelist ------------------------------

def t_k2_speed_clamped_to_official_range():
    import engine_elevenlabs as el
    el.save_voice_settings(TEST_CID, {"voice_id": "v", "speed": 1.3})
    loaded = el.load_voice_settings(TEST_CID)
    assert loaded["speed"] == 1.2, f"speed 1.3 must clamp to 1.2, got {loaded['speed']}"
    el.save_voice_settings(TEST_CID, {"voice_id": "v", "speed": 0.3})
    loaded = el.load_voice_settings(TEST_CID)
    assert loaded["speed"] == 0.7, f"speed 0.3 must clamp to 0.7, got {loaded['speed']}"


def t_k5_tts_provider_and_minimax_fields_persist():
    import engine_elevenlabs as el
    el.save_voice_settings(TEST_CID, {
        "voice_id": "v", "tts_provider": "minimax", "volume": 1.5, "pitch": -3,
    })
    loaded = el.load_voice_settings(TEST_CID)
    assert loaded.get("tts_provider") == "minimax", \
        f"tts_provider must survive save/load roundtrip, got {loaded.get('tts_provider')}"
    assert loaded.get("volume") == 1.5
    assert loaded.get("pitch") == -3


# --- A5: _preserve_rendered_scenes ---------------------------------------------

def t_a5_preserve_matches_by_normalized_text():
    import dashboard
    prev_scenes = {
        0: {"i": 0, "text": "Elizabeth walks into the room.", "file": "000.jpg",
            "status": "fertig", "source_url": "https://cdn/000.jpg"},
        5: {"i": 5, "text": "  ELIZABETH   walks into the room.  ", "file": "005.jpg",
            "status": "fertig", "source_url": "https://cdn/005.jpg"},
    }
    # New scenes: re-indexed (0 and 5 collapsed to 0 and 1 after a re-plan), but text
    # is unchanged for the first, changed for the second.
    new_scenes = [
        {"i": 0, "text": "Elizabeth walks into the room.", "file": None, "status": "geplant"},
        {"i": 1, "text": "A completely different sentence now.", "file": None, "status": "geplant"},
    ]
    preserved = dashboard._preserve_rendered_scenes(prev_scenes, new_scenes)
    assert preserved == 1, f"expected 1 preserved (case/whitespace-insensitive match), got {preserved}"
    assert new_scenes[0]["file"] == "000.jpg"
    assert new_scenes[0]["source_url"] == "https://cdn/000.jpg"
    assert new_scenes[1]["file"] is None, "unmatched scene must stay unpreserved"


def t_a5_preserve_empty_prev_is_noop():
    import dashboard
    new_scenes = [{"i": 0, "text": "x", "file": None, "status": "geplant"}]
    preserved = dashboard._preserve_rendered_scenes({}, new_scenes)
    assert preserved == 0
    assert new_scenes[0]["file"] is None


def t_a5_transcribe_worker_uses_preserve_helper():
    """Source-check (same convention as the Round-5 tests in test_cinematic_e2e.py):
    _transcribe_generate_worker must call the shared helper instead of unconditionally
    resetting file/status on every ElevenLabs re-run."""
    src = open(os.path.join(ROOT, "dashboard.py")).read()
    idx = src.find("def _transcribe_generate_worker")
    assert idx != -1
    body = src[idx:idx + 4500]
    assert "_preserve_rendered_scenes(prev_scenes, scenes)" in body, \
        "A5 fix missing: _transcribe_generate_worker must call _preserve_rendered_scenes"


# --- A2/A3: Char-Ref-Fallback ---------------------------------------------------

def t_a2_generate_one_uses_resolve_entity_ref():
    """Source-check: /api/generate_one must delegate to the shared fallback chain
    instead of its old source_url-only inline logic."""
    src = open(os.path.join(ROOT, "dashboard.py")).read()
    idx = src.find('if p == "/api/generate_one"')
    assert idx != -1
    body = src[idx:idx + 3000]
    assert "_resolve_entity_ref(v_plan(cid, vid), scene_for_phase, wait=False)" in body, \
        "A2 fix missing: generate_one must call _resolve_entity_ref with wait=False"


def t_a3_wait_for_entity_anchor_returns_immediately_when_wait_false():
    from engine.scenes import _wait_for_entity_anchor_scene
    plan = {"scenes": [{"i": 0, "concrete_entity": "char_01", "status": "geplant"}]}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(plan, f)
        path = f.name
    try:
        import time
        t0 = time.time()
        result = _wait_for_entity_anchor_scene(path, "char_01", 0, timeout=170.0, wait=False)
        elapsed = time.time() - t0
        assert elapsed < 1.0, f"wait=False must not block, took {elapsed}s"
        assert result.get("i") == 0
    finally:
        os.remove(path)


def t_a3_wait_for_entity_anchor_treats_rendered_file_as_final():
    """A scene with status='fertig' + a file but NO source_url (the recovery-race
    scenario) is a FINAL state — the poll loop must return immediately instead of
    blocking the full 170s timeout waiting for a source_url that will never appear."""
    from engine.scenes import _wait_for_entity_anchor_scene
    plan = {"scenes": [{"i": 0, "concrete_entity": "char_01", "status": "fertig",
                          "file": "000.jpg"}]}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(plan, f)
        path = f.name
    try:
        import time
        t0 = time.time()
        result = _wait_for_entity_anchor_scene(path, "char_01", 0, timeout=170.0, wait=True)
        elapsed = time.time() - t0
        assert elapsed < 5.0, f"rendered-but-no-source_url must be treated as final, took {elapsed}s"
        assert result.get("file") == "000.jpg"
    finally:
        os.remove(path)


def t_a3_resolve_entity_ref_stage_1b_local_file_fallback():
    """Stage 1b: anchor scene has a `file` on disk but no source_url → return the
    LOCAL file path directly (is_local=True) instead of falling through to the
    (unrelated) charsheet pool or returning no reference at all. Deliberately a raw
    path, not a base64 data-URL: KIE.ai blocks its own tempfile URLs as a reference,
    so the dashboard instead uploads this local path to catbox.moe and passes THAT
    URL along — a data-URL would defeat that upload step. Same shape as stage 2/3's
    charsheet-png fallback (also `is_local: True` + raw path), for consistency."""
    import dashboard
    from engine.scenes import _resolve_entity_ref

    out_dir = dashboard.v_out(TEST_CID, TEST_VID)
    os.makedirs(out_dir, exist_ok=True)
    img_path = os.path.join(out_dir, "000.jpg")
    with open(img_path, "wb") as f:
        f.write(b"\xff\xd8\xff\xfake-jpeg-bytes-for-test")

    plan_path = dashboard.v_plan(TEST_CID, TEST_VID)
    plan = {
        "scenes": [
            {"i": 0, "concrete_entity": "char_01", "status": "fertig", "file": "000.jpg"},
            {"i": 1, "concrete_entity": "char_01", "status": "geplant", "file": None},
        ],
        "characters": [],
    }
    with open(plan_path, "w") as f:
        json.dump(plan, f)

    scene = plan["scenes"][1]
    refs, debug = _resolve_entity_ref(plan_path, scene, wait=False)
    assert refs, "expected a local-file fallback reference, got none"
    assert refs[0] == img_path, f"unexpected ref shape: {refs[0][:60]}"
    assert debug.get("is_local") is True
    assert debug.get("source") == "anchor-scene-local-file"


# --- 4.2: Partial-Render-Warnung -----------------------------------------------

def t_42_render_start_warns_on_partial_scenes():
    """Source-check: /api/render_start must detect rendered < total scenes and return
    a partial-warning response instead of silently starting a stretched-out render."""
    src = open(os.path.join(ROOT, "dashboard.py")).read()
    idx = src.find('if p == "/api/render_start"')
    assert idx != -1
    body = src[idx:idx + 2200]
    assert '"partial": True' in body, "4.2 fix missing: no partial-render warning response"
    assert "rendered_scenes < total_scenes" in body, \
        "4.2 fix missing: no rendered-vs-total comparison"
    assert "not force" in body, "4.2 fix missing: force override to skip the warning"


# --- Voiceover/Plan-Entkopplung (Symbiose-Fix Juli 2026) -----------------------

def t_decouple_plan_has_usable_scenes_true_when_prompted():
    import engine_elevenlabs as el
    plan = {"scenes": [{"i": 0, "text": "x", "prompt": ""},
                        {"i": 1, "text": "y", "prompt": "A shot of something."}]}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(plan, f)
        path = f.name
    try:
        assert el._plan_has_usable_scenes(path) is True
    finally:
        os.remove(path)


def t_decouple_plan_has_usable_scenes_false_when_empty_or_missing():
    import engine_elevenlabs as el
    plan = {"scenes": [{"i": 0, "text": "x", "prompt": ""}, {"i": 1, "text": "y", "prompt": None}]}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(plan, f)
        path = f.name
    try:
        assert el._plan_has_usable_scenes(path) is False
    finally:
        os.remove(path)
    # Fehlende Datei -> ebenfalls False, kein Crash
    assert el._plan_has_usable_scenes("/nonexistent/plan.json") is False


def t_decouple_voiceover_skips_rebuild_when_plan_already_prompted():
    """Der eigentliche Regressionstest für den User-Report: ein Video mit bereits
    promptiertem Plan darf durch 'Voiceover generieren' NICHT neu segmentiert werden."""
    import dashboard
    import engine_elevenlabs as el

    cid, vid = TEST_CID, TEST_VID
    plan_path = dashboard.v_plan(cid, vid)
    good_plan = {
        "scenes": [{"i": 0, "text": "Elizabeth walks in.", "prompt": "A detailed shot.",
                    "file": None, "status": "geplant"}],
        "characters": [],
    }
    with open(plan_path, "w") as f:
        json.dump(good_plan, f)

    chunk_audio = _make_silent_mp3(1.0)
    chunk_b64 = base64.b64encode(chunk_audio).decode("ascii")

    def fake_generate(text, settings):
        return {"audio_base64": chunk_b64,
                "words": [{"word": "hi", "start": 0.0, "end": 0.5}],
                "task_id": "test-task", "n_chunks": 1}

    rebuild_calls = []
    def fake_transcribe_worker(cid_, vid_, sec):
        rebuild_calls.append((cid_, vid_))
        return {}

    with patch.object(el, "elevenlabs_generate", side_effect=fake_generate), \
         patch.object(dashboard, "_transcribe_generate_worker", fake_transcribe_worker):
        el._elevenlabs_persist_and_schedule(cid, vid, "Elizabeth walks in.",
                                             settings={"voice_id": "test_voice"})
        import time as _t; _t.sleep(0.3)  # falls doch ein Thread gestartet würde

    assert not rebuild_calls, (
        f"_transcribe_generate_worker wurde aufgerufen obwohl schon ein promptierter "
        f"Plan existierte — der Rebuild-Guard hat nicht gegriffen: {rebuild_calls}"
    )
    reread = json.load(open(plan_path))
    assert reread["scenes"][0]["prompt"] == "A detailed shot.", \
        "Plan wurde trotzdem verändert (Prompt ging verloren)"


def t_decouple_voiceover_still_rebuilds_when_no_plan_exists():
    """Voice-first-Workflow (noch kein Plan) muss weiterhin automatisch bauen —
    die Entkopplung darf nur den 'schon-guter-Plan-da'-Fall abschalten."""
    import dashboard
    import engine_elevenlabs as el

    cid, vid = TEST_CID, TEST_VID
    plan_path = dashboard.v_plan(cid, vid)
    with open(plan_path, "w") as f:
        json.dump({"scenes": []}, f)

    chunk_audio = _make_silent_mp3(1.0)
    chunk_b64 = base64.b64encode(chunk_audio).decode("ascii")

    def fake_generate(text, settings):
        return {"audio_base64": chunk_b64,
                "words": [{"word": "hi", "start": 0.0, "end": 0.5}],
                "task_id": "test-task", "n_chunks": 1}

    rebuild_calls = []
    def fake_transcribe_worker(cid_, vid_, sec):
        rebuild_calls.append((cid_, vid_))
        return {}

    with patch.object(el, "elevenlabs_generate", side_effect=fake_generate), \
         patch.object(dashboard, "_transcribe_generate_worker", fake_transcribe_worker):
        el._elevenlabs_persist_and_schedule(cid, vid, "Hallo Welt.",
                                             settings={"voice_id": "test_voice"})
        import time as _t; _t.sleep(0.3)

    assert rebuild_calls == [(cid, vid)], (
        f"Voice-first-Fall (kein Plan) muss weiterhin _transcribe_generate_worker "
        f"auslösen, aber: {rebuild_calls}"
    )


# --- Enrichment-Token-Strip (Symbiose-Fix Juli 2026) ----------------------------

def t_strip_pause_tokens_removes_ellipsis_keeps_real_words():
    """Nur reine Punkt-Tokens ("...", "…") fliegen raus — NICHT jedes Token ohne
    alphanumerisches Zeichen. Ein freistehendes "—" bleibt bewusst erhalten: es kommt
    aus dem ORIGINAL-Skript (nicht aus dem Enrichment) und wird dort auch von
    align_scenes_to_whisper als eigenes .split()-Wort mitgezählt — würde man es hier
    filtern, entstünde exakt das Off-by-one-Problem, das dieser Fix beheben soll,
    nur an einer anderen Stelle (siehe Docstring von _strip_pause_tokens)."""
    import dashboard
    words = [
        {"word": "Hello", "start": 0.0, "end": 0.5},
        {"word": "...", "start": 0.5, "end": 0.6},
        {"word": "world.", "start": 0.6, "end": 1.0},
        {"word": "—", "start": 1.0, "end": 1.1},
        {"word": "$9", "start": 1.1, "end": 1.3},
        {"word": "…", "start": 1.3, "end": 1.4},
    ]
    out = dashboard._strip_pause_tokens(words)
    assert [w["word"] for w in out] == ["Hello", "world.", "—", "$9"], \
        f"unerwartetes Ergebnis: {[w['word'] for w in out]}"


def t_strip_pause_tokens_fixes_alignment_word_count_mismatch():
    """Der eigentliche Regressionstest: OHNE Strip lief der Wortzeiger bei vielen
    '...'-Tokens leer, bevor die letzten Szenen ihr start_aligned bekamen. MIT Strip
    bekommen alle Szenen ein start_aligned, wenn die Wortzahl sonst exakt matcht."""
    import dashboard
    # 6 gesprochene Wörter, aber 3 zusätzliche '...'-Phantom-Tokens dazwischen —
    # simuliert das reale Verhältnis (Enrichment bläht die Liste auf).
    raw_words = [
        {"word": "One", "start": 0.0, "end": 0.2},
        {"word": "drop", "start": 0.2, "end": 0.4},
        {"word": "of", "start": 0.4, "end": 0.5},
        {"word": "blood.", "start": 0.5, "end": 0.8},
        {"word": "...", "start": 0.8, "end": 0.9},
        {"word": "Hundreds", "start": 0.9, "end": 1.2},
        {"word": "of", "start": 1.2, "end": 1.3},
        {"word": "tests.", "start": 1.3, "end": 1.6},
        {"word": "...", "start": 1.6, "end": 1.7},
    ]
    scenes = [
        {"i": 0, "text": "One drop of blood."},
        {"i": 1, "text": "Hundreds of tests."},
    ]
    stripped = dashboard._strip_pause_tokens(raw_words)
    dashboard.align_scenes_to_whisper(scenes, stripped)
    assert all(s.get("start_aligned") is not None for s in scenes), (
        f"ohne Strip hätten die '...'-Tokens den Wortzeiger verschoben: {scenes}"
    )
    assert scenes[1]["start_aligned"] == 0.9, \
        f"Szene 1 muss bei 'Hundreds' (0.9s) starten, nicht verschoben: {scenes[1]}"


def t_render_worker_calls_strip_before_alignment():
    """Source-check: der Alignment-Block in _render_worker muss _strip_pause_tokens
    VOR _compute_pause_trims/align_scenes_to_whisper aufrufen."""
    src = open(os.path.join(ROOT, "dashboard.py")).read()
    idx = src.find("trims = _compute_pause_trims(whisper_words)")
    assert idx != -1
    window = src[max(0, idx - 700):idx]
    assert "_strip_pause_tokens(whisper_words)" in window, \
        "Strip-Fix fehlt: _strip_pause_tokens muss vor _compute_pause_trims laufen"


# --- Sync-Invariant Präzisions-Fix (Symbiose-Fix Juli 2026) --------------------

def t_sync_invariant_cut_points_land_on_start_aligned():
    """Der eigentliche Regressionstest für den beobachteten Sync-Drift: wenn ALLE
    Szenen aligned sind, muss die kumulierte Frame-Position vor Szene N (also der
    Schnittpunkt) ~= scenes[N].start_aligned sein — nicht proportional verschoben
    durch eine ungleich verteilte Gesamt-Pausenzeit."""
    from engine.render import _apply_sync_invariant
    fps = 30
    # 3 Szenen mit einer sehr ungleich verteilten Pause zwischen Szene 1 und 2 (2s) —
    # genau der Fall, der die alte, sprechzeit-only Berechnung verzerrt hätte.
    scenes = [
        {"i": 0, "start_aligned": 0.0, "end_aligned": 2.0},
        {"i": 1, "start_aligned": 4.0, "end_aligned": 5.0},   # 2s Pause davor
        {"i": 2, "start_aligned": 5.5, "end_aligned": 7.0},
    ]
    audio_duration = 8.0
    frames = _apply_sync_invariant(scenes, audio_duration, fps)
    assert sum(frames) == round(audio_duration * fps), "Tail-Clipping-Invariante verletzt"

    cumulative = 0.0
    cut_points = []
    for f in frames[:-1]:
        cumulative += f / fps
        cut_points.append(cumulative)
    # Schnittpunkt vor Szene 1 muss nah an deren start_aligned (4.0s) liegen —
    # mit der ALTEN Berechnung (nur end-start, Pause ignoriert) läge er weit davor.
    assert abs(cut_points[0] - scenes[1]["start_aligned"]) < 0.05, (
        f"Schnitt vor Szene 1 sitzt bei {cut_points[0]:.2f}s, erwartet ~{scenes[1]['start_aligned']}s"
    )
    assert abs(cut_points[1] - scenes[2]["start_aligned"]) < 0.05, (
        f"Schnitt vor Szene 2 sitzt bei {cut_points[1]:.2f}s, erwartet ~{scenes[2]['start_aligned']}s"
    )


def t_sync_invariant_falls_back_when_not_all_aligned():
    """Fehlt auch nur einer Szene start_aligned, muss die alte (sprechzeit-basierte)
    Berechnung greifen — kein Umbau des Fallback-Pfads."""
    from engine.render import _apply_sync_invariant
    fps = 30
    scenes = [
        {"i": 0, "start_aligned": 0.0, "end_aligned": 2.0},
        {"i": 1, "dur": 3.0},  # kein start_aligned -> Fallback-Pfad
    ]
    frames = _apply_sync_invariant(scenes, 6.0, fps)
    assert sum(frames) == round(6.0 * fps)
    assert len(frames) == 2 and all(f > 0 for f in frames)


# --- Cinematic-Mix Juli 2026: Schritt 1 (Audio-Mix) -----------------------------

def t_mix_music_underscore_lufs_not_voice_level():
    """Musik muss auf Underscore-Pegel liegen (weit unter Voice), nicht auf
    Broadcast-Pegel wie vorher (-16 LUFS lag praktisch auf Voice-Höhe)."""
    import engine.audio as a
    assert a.TARGET_LUFS <= -25, f"TARGET_LUFS sollte Underscore-Pegel sein, war {a.TARGET_LUFS}"
    assert a.FINAL_TARGET_LUFS == -14, f"YouTube-Zielwert erwartet -14, war {a.FINAL_TARGET_LUFS}"


def t_mix_sfx_category_volumes_match_hierarchy():
    """Whoosh (Übergang) muss leiser sein als Impact/Braam/Boom (Akzent) -- die
    kategoriebasierten Pegel dürfen nicht wieder auf einen einzigen Flat-Wert
    zusammenfallen (das war der 'viel zu laut'-Bug: 0.7 pauschal)."""
    import engine.audio as a
    assert a.SFX_VOLUME_BY_CATEGORY["whoosh"] < a.SFX_VOLUME_BY_CATEGORY["impact"]
    assert all(v <= 0.35 for v in a.SFX_VOLUME_BY_CATEGORY.values()), \
        "kein SFX darf lauter als -9dB (0.35) relativ zur Stimme sein"


def t_mix_amix_calls_have_normalize_0():
    """ffmpeg's amix halbiert standardmäßig ALLE Inputs (normalize=1) -- ohne
    explizites normalize=0 würde die Stimme selbst leiser werden, nicht nur die
    Musik/SFX. Prüft den tatsächlich gebauten ffmpeg-Befehl (subprocess gemockt,
    kein echter ffmpeg-Lauf nötig für diesen reinen Kommandozeilen-Check)."""
    import engine.audio as a
    captured = []

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        class R: returncode = 0
        return R()

    with patch.object(a.subprocess, "run", side_effect=fake_run):
        a._duck_music_under_voice("/tmp/voice.mp3", "/tmp/music.mp3", "/tmp/out.mp3")
        a._place_sfx("/tmp/narr.mp3", [], "/tmp/out2.mp3")

    duck_cmd = " ".join(captured[0])
    assert "normalize=0" in duck_cmd, "Ducking-amix ohne normalize=0 -- Stimme würde mitgedämpft"
    sfx_cmd = " ".join(captured[1])
    assert f"I={a.FINAL_TARGET_LUFS}" in sfx_cmd, "finales loudnorm nutzt nicht FINAL_TARGET_LUFS"


# --- Cinematic-Mix Juli 2026: Schritt 2 (SFX-Bibliothek + Riser-Timing) ---------

def t_sfx_library_only_maps_license_cleared_files():
    """SFX_LIBRARY darf NUR aus 'Free Cinematic SFX/' (Flame Sound, EULA bestätigt)
    speisen -- das Generdyn-Pack ('#99S006') hat laut assets/CREDITS.txt ungeklärten
    Lizenzstatus und darf in keinem Pfad auftauchen."""
    import engine.audio as a
    for cat, paths in a.SFX_LIBRARY.items():
        for p in paths:
            assert "#99S006" not in p, f"Lizenz-ungeklärte Datei in SFX_LIBRARY[{cat}]: {p}"
            assert os.path.exists(p), f"SFX_LIBRARY[{cat}] verweist auf fehlende Datei: {p}"


def t_riser_runup_starts_before_cut_not_at_cut():
    """Ein Riser baut Spannung AUF einen Moment hin -- sein Ende (Peak) muss auf dem
    Cut sitzen, er muss also VOR dem Cut starten. Die alte Logik setzte ihn AM Cut
    (Peak mitten in der nächsten Szene, falsch herum)."""
    import engine.audio as a
    scenes = [
        {"i": 0, "start": 0.0, "dur": 10.0, "phase": "OPENING"},
        {"i": 1, "start": 10.0, "dur": 3.0, "phase": "OPENING", "pacing": "punchy"},
    ]
    events = a._build_sfx_events(scenes)
    riser_events = [e for e in events if e["sfx"] == "riser"]
    assert riser_events, "erwartet mindestens ein Riser-Event für die punchy Szene"
    assert riser_events[0]["start"] < 10.0, \
        f"Riser muss VOR dem Cut (t=10.0) starten, war: {riser_events[0]['start']}"


def t_riser_runup_capped_even_for_long_files():
    """Riser-Dateien haben teils ~20s Reverb-Tail -- der Anlauf davor darf trotzdem
    nicht länger als RISER_RUNUP_CAP_SEC sein, sonst klingt der Riser über mehrere
    unbeteiligte Sätze hinweg unmotiviert."""
    import engine.audio as a
    scenes = [
        {"i": 0, "start": 0.0, "dur": 100.0, "phase": "OPENING"},
        {"i": 1, "start": 100.0, "dur": 3.0, "phase": "OPENING", "pacing": "punchy"},
    ]
    events = a._build_sfx_events(scenes)
    riser_events = [e for e in events if e["sfx"] == "riser"]
    assert riser_events
    runup = 100.0 - riser_events[0]["start"]
    assert runup <= a.RISER_RUNUP_CAP_SEC + 0.01, \
        f"Riser-Anlauf sollte auf {a.RISER_RUNUP_CAP_SEC}s gedeckelt sein, war {runup}s"


def t_sfx_density_cap_drops_close_big_events():
    """Zwei 'große' SFX innerhalb von SFX_DENSITY_MIN_GAP_SEC dürfen nicht beide
    überleben -- Akzente wirken nur, wenn sie selten sind. Die höher priorisierte
    Phasen-Grenze (braam) muss die kollidierende punchy-Szene-Impact verdrängen."""
    import engine.audio as a
    scenes = [
        {"i": 0, "start": 0.0, "dur": 9.0, "phase": "RISING_ACTION"},
        {"i": 1, "start": 9.0, "dur": 3.0, "phase": "CLIMAX", "pacing": "punchy"},
    ]
    events = a._build_sfx_events(scenes)
    big_events_at_9 = [e for e in events if e.get("big") and abs(e["start"] - 9.0) < 0.01]
    assert len(big_events_at_9) == 1, \
        f"genau 1 großes SFX am Schnittpunkt erwartet (kein Stapeln), war: {big_events_at_9}"
    assert big_events_at_9[0]["sfx"] == "braam", \
        "Phasen-Grenze (braam) muss Vorrang vor der kollidierenden punchy-impact haben"


def t_phase_boundary_climax_entry_gets_braam_exit_gets_downshifter():
    """Story-Phasen-Grenzen bekommen Akzente: Eintritt in CLIMAX -> braam, Ausstieg
    aus CLIMAX -> downshifter. Andere Übergänge bleiben unvertont."""
    import engine.audio as a
    scenes = [
        {"i": 0, "start": 0.0, "dur": 5.0, "phase": "OPENING"},
        {"i": 1, "start": 5.0, "dur": 5.0, "phase": "RISING_ACTION"},
        {"i": 2, "start": 10.0, "dur": 5.0, "phase": "CLIMAX"},
        {"i": 3, "start": 15.0, "dur": 5.0, "phase": "RESOLUTION"},
    ]
    events = a._phase_boundary_sfx_events(scenes)
    by_start = {round(e["start"]): e["sfx"] for e in events if e["sfx"] in a.SFX_BIG_CATEGORIES}
    assert by_start.get(10) == "braam", f"CLIMAX-Eintritt (t=10) sollte braam sein, war: {by_start}"
    assert by_start.get(15) == "downshifter", f"CLIMAX-Ausstieg (t=15) sollte downshifter sein, war: {by_start}"
    assert 5 not in by_start, "OPENING->RISING_ACTION sollte KEINEN großen Akzent bekommen"


# --- Cinematic-Mix Juli 2026: Schritt 3 (1-Wort-Captions) -----------------------

def t_align_scenes_populates_scene_relative_word_slices():
    """align_scenes_to_whisper muss zusätzlich zu start_aligned/end_aligned auch
    `words` ablegen -- scene-relative Offsets (start_aligned als Nullpunkt), NICHT
    die absoluten Audio-Zeitstempel (die Overlay-Fenster in _render_clip sind
    Clip-relativ, t=0 am Clip-Anfang)."""
    from dashboard import align_scenes_to_whisper
    scenes = [
        {"i": 0, "text": "But that was"},
        {"i": 1, "text": "the problem."},
    ]
    words = [
        {"word": "But", "start": 10.0, "end": 10.2},
        {"word": "that", "start": 10.2, "end": 10.4},
        {"word": "was", "start": 10.4, "end": 10.6},
        {"word": "the", "start": 10.6, "end": 10.8},
        {"word": "problem.", "start": 10.8, "end": 11.2},
    ]
    align_scenes_to_whisper(scenes, words)
    assert scenes[0]["start_aligned"] == 10.0
    assert scenes[0]["words"][0]["word"] == "But"
    assert scenes[0]["words"][0]["start"] == 0.0, "erstes Wort einer Szene muss bei Offset 0.0 liegen"
    # Szene 1 startet bei absolutem 10.6 ("the"); ihr EIGENES erstes Wort ist also auch
    # bei Offset 0.0 -- der eigentliche Beweis für "scene-relativ" ist ihr ZWEITES Wort
    # ("problem.", absolut 10.8): 10.8 - 10.6 = 0.2, NICHT der absolute Zeitstempel 10.8.
    assert scenes[1]["start_aligned"] == 10.6
    assert scenes[1]["words"][0]["start"] == 0.0
    assert scenes[1]["words"][1]["word"] == "problem."
    assert abs(scenes[1]["words"][1]["start"] - 0.2) < 1e-9, \
        f"'problem.' sollte 0.2s relativ zu Szene 1's start_aligned (10.8-10.6) liegen, war: {scenes[1]['words'][1]['start']}"


def t_overlay_specs_prefers_word_caption_seq_over_full_caption():
    """Wenn die Szene Wort-Slices hat, muss das neue word_caption_seq greifen (CapCut-
    Stil) statt der alten Voll-Text-Bauchbinde. Fallback auf 'caption' bleibt bestehen,
    wenn `words` fehlt (Whisper-Teilabdeckung / alte resumte Pläne)."""
    from engine.render import _overlay_specs_for_scene
    scene_with_words = {"text": "Hello world", "words": [{"word": "Hello", "start": 0.0, "end": 0.3},
                                                            {"word": "world", "start": 0.3, "end": 0.6}]}
    specs = _overlay_specs_for_scene(scene_with_words, clip_dur=2.0, overlay_opts={"captions": True})
    assert any(s[0] == "word_caption_seq" for s in specs), "erwartet word_caption_seq bei vorhandenen words"
    assert not any(s[0] == "caption" for s in specs), "darf NICHT zusätzlich die alte Voll-Text-Caption zeigen"

    scene_no_words = {"text": "Hello world"}
    specs2 = _overlay_specs_for_scene(scene_no_words, clip_dur=2.0, overlay_opts={"captions": True})
    assert any(s[0] == "caption" for s in specs2), "Fallback auf alte Caption fehlt, wenn words fehlt"


def t_render_worker_needs_alignment_also_checks_words():
    """Source-check: ein VOR diesem Feature bereits gerendertes Video hat start_aligned
    schon gesetzt, aber kein `words` -- die Resume-Optimierung darf das Alignment dann
    NICHT überspringen, sonst bleiben alte Bauchbinden-Captions für immer aktiv."""
    src = open(os.path.join(ROOT, "dashboard.py")).read()
    idx = src.find("needs_alignment = any(")
    assert idx != -1, "needs_alignment-Zeile nicht gefunden"
    line = src[idx:idx + 200]
    assert 'not s.get("words")' in line, \
        f"needs_alignment muss auch fehlende words erkennen, Zeile war: {line[:120]}"


def t_render_worker_persists_words_to_plan_json():
    """Source-check: die plan.json-Schreib-Zurück-Stelle (nach dem Render) muss
    `words` mitpersistieren -- sonst würde needs_alignment bei JEDEM künftigen
    Resume-Render wieder True liefern und die Ausrichtung sinnlos wiederholen, obwohl
    sie schon einmal korrekt berechnet wurde."""
    src = open(os.path.join(ROOT, "dashboard.py")).read()
    idx = src.find('by_i[s["i"]]["start_aligned"] = s["start_aligned"]')
    assert idx != -1
    body = src[idx:idx + 700]
    assert 'by_i[s["i"]]["words"] = s["words"]' in body, \
        "plan.json-Persistenz vergisst words -- Resume würde Alignment endlos wiederholen"


def t_word_caption_sequence_covers_full_clip_with_correct_timing():
    """End-to-End-Check des Sequenz-Bauplans (echter .venv_whisper-Subprocess-Aufruf,
    kein Mock): N Frames insgesamt, Lücke vor dem ersten Wort ist blank, letztes Wort
    hält bis zum Clip-Ende (lückenlos, 'bis das nächste kommt')."""
    from engine.render import _render_word_caption_sequence
    tmp_dir = tempfile.mkdtemp(prefix="word_caption_seq_test_")
    try:
        words = [
            {"word": "Hello", "start": 0.2, "end": 0.4},
            {"word": "world", "start": 0.4, "end": 0.6},
        ]
        fps = 30
        clip_dur = 1.0
        _render_word_caption_sequence(tmp_dir, 640, 360, words, clip_dur, fps)
        n_frames = round(clip_dur * fps)
        seq_files = sorted(f for f in os.listdir(tmp_dir) if f.startswith("seq_"))
        assert len(seq_files) == n_frames, f"erwartet {n_frames} Sequenz-Frames, war {len(seq_files)}"
        # Vor Wort 0 (t<0.2s -> Frame < 6): blank
        first_target = os.path.realpath(os.path.join(tmp_dir, "seq_0000.png"))
        assert first_target.endswith("blank.png"), "Frame 0 (vor erstem Wort) muss blank sein"
        # Letztes Wort ("world", start=0.4 -> Frame 12) hält bis zum Clip-Ende (Frame 29)
        last_target = os.path.realpath(os.path.join(tmp_dir, f"seq_{n_frames - 1:04d}.png"))
        assert "word_0001" in last_target, \
            f"letztes Frame sollte noch das letzte Wort zeigen (lückenlos), war: {last_target}"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# --- Cinematic-Mix Juli 2026: Schritt 4 (motivierte Motion) ---------------------

def t_shot_hint_document_has_priority_over_closeup():
    """Ein Prompt, der SOWOHL 'top-down'/'document' ALS AUCH 'close-up' enthält, muss
    die Dokument-Kategorie bekommen (Lesbarkeit schlägt Intimitäts-Zoom -- ein
    schwenkender Zoom über Bildschirmtext macht ihn unlesbar). Feinschliff Runde 2:
    'static' ist aus der Dokument-Regel entfernt (jede Szene bekommt einen Effekt)."""
    from engine.render import _shot_hint_from_prompt
    hint = _shot_hint_from_prompt("A top-down close-up of a printed medical report")
    assert hint == ["tilt_down"], f"Dokument-Priorität erwartet, war: {hint}"


def t_shot_hint_wide_and_no_match():
    from engine.render import _shot_hint_from_prompt
    assert _shot_hint_from_prompt("A wide shot of the skyline at dusk") == ["pan_left", "pan_right"]
    assert _shot_hint_from_prompt("Just some random descriptive text") is None
    assert _shot_hint_from_prompt("") is None


def t_motion_avoids_direction_reversal_from_previous_scene():
    """Zwei aufeinanderfolgende unabhängige Szenen dürfen nicht in exakte
    Gegenrichtungen schwenken (pan_left direkt nach pan_right) -- das war der
    sichtbarste 'randomisiert'-Eindruck."""
    from engine.render import _pick_motion_avoiding_reversal, _build_motion
    prev = {"motion": _build_motion("pan_right", 1.0)}
    picked = _pick_motion_avoiding_reversal(["pan_left", "pan_right"], seed=0, prev_scene=prev)
    assert picked == "pan_right", f"Gegenrichtung zu pan_right (pan_left) hätte gemieden werden müssen, war: {picked}"


def t_motion_every_scene_gets_a_real_effect_never_stylistic_static():
    """Feinschliff Runde 2 (User-Feedback 'jede Szene braucht einen Effekt'): die frühere
    'jede 3./4. unabhängige Szene wird static'-Regel ist entfernt. 'static' darf nur noch
    über den technischen dur<1.2s-Fallback zurückkommen, nie über die stilistische
    Auswahl (Prompt-Hint/Phase/Pacing)."""
    from engine.render import _motion_for_scene
    for i in range(20):
        scene = {"i": i, "dur": 3.0, "pacing": "normal", "prompt": "Generic scene description"}
        m = _motion_for_scene(scene, None)
        assert m["name"] != "static", f"i={i}: static sollte nie mehr stilistisch gewählt werden"
    # Technischer Fallback bleibt: extrem kurze Szene ist weiterhin static.
    short_scene = {"i": 0, "dur": 0.8, "pacing": "normal"}
    m_short = _motion_for_scene(short_scene, None)
    assert m_short["name"] == "static", "dur<1.2s muss weiterhin auf static fallen (technischer Grund)"


def t_motion_hook_intensity_lowered_and_snap_zoom_capped():
    """Cinematic-Mix-Gegenstück zum aktualisierten Phase-L-Test in
    test_cinematic_e2e.py: snap_zoom_in-Basis-z1 <= 1.16 (vorher 1.25), Hook nutzt
    HOOK_MOTION_INTENSITY == 1.0 (vorher 1.2)."""
    from engine.render import MOTION_LIBRARY, HOOK_MOTION_INTENSITY
    assert MOTION_LIBRARY["snap_zoom_in"]["z1"] <= 1.16
    assert HOOK_MOTION_INTENSITY == 1.0


# --- Feinschliff Runde 2: Motion-Tempo, Übergangs-Varietät, Akt-Einspieler raus ----

def t_zoom_in_out_boosted_to_120_140_percent():
    """User-Feedback: Zoom (zoom_in/zoom_out) soll GENAUSO wie Pan/Tilt auf 120-140%
    angehoben werden -- vorher nur 1.12 (12%), viel zu subtil."""
    from engine.render import MOTION_LIBRARY
    zi, zo = MOTION_LIBRARY["zoom_in"], MOTION_LIBRARY["zoom_out"]
    assert 1.2 <= zi["z1"] <= 1.4, f"zoom_in z1 sollte 120-140% sein, war {zi['z1']}"
    assert 1.2 <= zo["z0"] <= 1.4, f"zoom_out z0 sollte 120-140% sein, war {zo['z0']}"
    assert tuple(zi["focus0"]) == tuple(zi["focus1"]), "zoom_in muss weiterhin reiner Zoom bleiben (Fokus fix)"


def t_pan_tilt_travel_shrunk_and_centered():
    """User-Feedback: Pan/Tilt ist bei 130% Zoom zu schnell -- die Fokus-Wanderung
    muss deutlich kleiner UND symmetrisch um die Bildmitte (0.5) zentriert sein, damit
    die Bewegung langsam wirkt und nie nah an den Bildrand kommt."""
    from engine.render import MOTION_LIBRARY
    for name, axis in (("pan_left", 0), ("pan_right", 0), ("tilt_up", 1), ("tilt_down", 1)):
        m = MOTION_LIBRARY[name]
        f0, f1 = m["focus0"][axis], m["focus1"][axis]
        travel = abs(f1 - f0)
        assert travel <= 0.2, f"{name}: Wanderung sollte klein sein (<=0.2), war {travel}"
        assert abs((f0 + f1) / 2 - 0.5) < 1e-6, f"{name}: muss um 0.5 zentriert sein, Mitte war {(f0+f1)/2}"


def t_static_absent_from_every_stylistic_candidate_pool():
    """'static' darf in KEINER stilistischen Auswahlliste mehr vorkommen (nur noch als
    technischer dur<1.2s-Fallback in _motion_for_scene selbst)."""
    from engine.render import _PACING_MOTION_CANDIDATES, _PHASE_MOTION_CANDIDATES, _SHOT_HINT_RULES
    for pacing, candidates in _PACING_MOTION_CANDIDATES.items():
        assert "static" not in candidates, f"static noch in _PACING_MOTION_CANDIDATES[{pacing}]"
    for phase, candidates in _PHASE_MOTION_CANDIDATES.items():
        assert "static" not in candidates, f"static noch in _PHASE_MOTION_CANDIDATES[{phase}]"
    for _keywords, candidates in _SHOT_HINT_RULES:
        assert "static" not in candidates, f"static noch in einer _SHOT_HINT_RULES-Kandidatenliste: {candidates}"


def t_transition_rotation_uses_occurrence_position_not_scene_index():
    """Der Sub-Typ (z.B. wipeleft/wiperight) muss über die laufende Nummer INNERHALB
    der Übergangs-Punkte rotieren, nicht über den rohen Szenenindex -- sonst können
    mehrere Übergänge zufällig dieselbe Index-Parität teilen und derselbe Typ feuert
    mehrmals in Folge (der eigentliche 'wiederholt sich'-Bug)."""
    from engine.render import _transition_for_scene, TRANSITION_LIBRARY

    # Gleiche Szene (gleicher roher Index i=5), aber unterschiedliche
    # transition_seq_idx -- muss trotzdem unterschiedliche Typen liefern.
    scene = {"i": 5, "phase_source": "llm", "phase": "RISING_ACTION"}  # -> "smooth"-Familie
    t0, _sfx0, _d0 = _transition_for_scene(scene, 0)
    t1, _sfx1, _d1 = _transition_for_scene(scene, 1)
    assert t0 != t1, f"transition_seq_idx 0 und 1 sollten unterschiedliche Typen liefern, beide waren {t0}"

    # Strikte Rotation über viele aufeinanderfolgende Positionen einer Familie: nie
    # zweimal derselbe Typ in Folge.
    types_seen = [_transition_for_scene(scene, k)[0] for k in range(8)]
    for a, b in zip(types_seen, types_seen[1:]):
        assert a != b, f"Sub-Typ wiederholt sich in Folge: {types_seen}"
    assert set(types_seen) == set(TRANSITION_LIBRARY["smooth"]["types"]), \
        f"sollte alle 4 smooth-Varianten durchrotieren, sah nur: {set(types_seen)}"


def t_transition_families_expanded_to_four_variants():
    """wipe/smooth sind auf 4 Sub-Typen erweitert (User-Feedback: mehr Varietät gegen
    Monotonie). fade bleibt bei 2 (einzige sanften xfade-Varianten für 'calm')."""
    from engine.render import TRANSITION_LIBRARY
    assert len(TRANSITION_LIBRARY["wipe"]["types"]) == 4
    assert len(TRANSITION_LIBRARY["smooth"]["types"]) == 4
    assert len(TRANSITION_LIBRARY["fade"]["types"]) == 2


def t_render_worker_transition_calls_use_position_lookup():
    """Source-check: _render_worker muss die Übergangs-Position (nicht den rohen
    Szenenindex) an _transition_for_scene übergeben -- an beiden Aufrufstellen
    (Frame-Kompensation + Crossfade-Merge)."""
    src = open(os.path.join(ROOT, "dashboard.py")).read()
    assert "transition_seq_idx_by_scene_idx" in src, \
        "Lookup-Dict für Übergangs-Positionen fehlt in dashboard.py"
    calls = [line for line in src.splitlines() if "_transition_for_scene(" in line]
    assert len(calls) >= 2
    for line in calls:
        assert "transition_seq_idx_by_scene_idx[idx]" in line, \
            f"_transition_for_scene-Aufruf nutzt nicht die Positions-Lookup: {line.strip()}"


def t_title_card_scenes_render_like_normal_scenes():
    """Source-check: _render_clip darf den 'kind==title_card'-Sonderfall nicht mehr
    behandeln (kein AUFRUF von render_title_card_png_via_venv mehr) -- ein erklärender
    Kommentar darf 'title_card' weiter beim Namen nennen."""
    src = open(os.path.join(ROOT, "engine", "render.py")).read()
    idx = src.find("def _render_clip(")
    assert idx != -1
    next_def = src.find("\ndef _assemble_clips(", idx)
    body = src[idx:next_def if next_def != -1 else idx + 6000]
    assert 'scene.get("kind") == "title_card"' not in body, \
        "_render_clip darf den title_card-Sonderfall nicht mehr ABFRAGEN"
    assert "render_title_card_png_via_venv(" not in body, \
        "_render_clip darf render_title_card_png_via_venv nicht mehr aufrufen"
    # Funktion bleibt im Modul erhalten (dormant, reaktivierbar), nur der Aufruf fehlt.
    from engine.render import render_title_card_png_via_venv
    assert callable(render_title_card_png_via_venv)


# --- Nachjustierung Juli 2026: reine Sprecherspur + saubere Ken-Burns-Motion ----

def t_no_sound_design_layer_render_worker_uses_raw_voice():
    """Source-check: der User will künftig NUR die Sprecherspur im Render, Musik/SFX
    legt er selbst extern drüber. _render_worker darf _build_final_audio nicht mehr
    AUFRUFEN (ein erklärender Kommentar darf die Funktion weiter beim Namen nennen)
    -- _mux_audio muss direkt mit audio_path gefüttert werden."""
    src = open(os.path.join(ROOT, "dashboard.py")).read()
    idx = src.find('final_path = os.path.join(v_out(cid, vid), "final.mp4")')
    assert idx != -1
    body = src[idx:idx + 900]
    assert "_build_final_audio(" not in body, \
        "_render_worker darf die Sound-Design-Kette nicht mehr AUFRUFEN"
    assert '_mux_audio(silent_path, audio_path, final_path)' in body, \
        "_mux_audio muss direkt die rohe (pausen-gekürzte) Sprecherspur bekommen"


def t_motion_library_has_no_combined_zoom_and_pan_recipes():
    """Kein MOTION_LIBRARY-Rezept darf GLEICHZEITIG die Zoomstufe (z0 != z1) UND den
    Fokuspunkt (focus0 != focus1) ändern -- genau das war das vom User beschriebene
    'zoomt ran und geht dann nach rechts/links'-Artefakt (dolly_in/dolly_out/
    diagonal_glide, jetzt entfernt). Pro Bild nur EIN Effekt: reiner Zoom ODER reiner
    Pan/Slide."""
    from engine.render import MOTION_LIBRARY
    for name, m in MOTION_LIBRARY.items():
        zoom_changes = abs(m["z1"] - m["z0"]) > 1e-9
        focus_changes = tuple(m["focus0"]) != tuple(m["focus1"])
        assert not (zoom_changes and focus_changes), \
            f"MOTION_LIBRARY['{name}'] ändert Zoom UND Fokus gleichzeitig: {m}"
    for removed in ("dolly_in", "dolly_out", "diagonal_glide"):
        assert removed not in MOTION_LIBRARY, f"{removed} sollte entfernt sein"


def t_motion_library_pan_tilt_zoom_boosted_to_120_140_percent():
    """Pan/Tilt-Bewegungen hatten vorher nur 1.08 (8%) -- zu subtil laut User-Feedback.
    Jetzt fest im 120-140%-Bereich (User-Vorgabe), Zoom bleibt dabei UNVERÄNDERT über
    die ganze Bewegung (z0==z1, reiner Slide)."""
    from engine.render import MOTION_LIBRARY
    for name in ("pan_left", "pan_right", "tilt_up", "tilt_down"):
        m = MOTION_LIBRARY[name]
        assert m["z0"] == m["z1"], f"{name} muss reiner Pan sein (z0==z1), war: {m}"
        assert 1.2 <= m["z0"] <= 1.4, f"{name} sollte 120-140% Zoom haben, war z0={m['z0']}"


def t_motion_picks_never_return_removed_combined_names():
    """Breiter Sweep über _motion_for_scene für viele Phasen/Pacings/Indizes --
    dolly_in/dolly_out/diagonal_glide dürfen nie mehr als Auswahl zurückkommen (weder
    über Pacing- noch über Phase-Kandidaten noch über Shot-Hints)."""
    from engine.render import _motion_for_scene
    removed = {"dolly_in", "dolly_out", "diagonal_glide"}
    pacings = ["calm", "normal", "punchy"]
    phases = ["OPENING", "RISING_ACTION", "CLIMAX", "RESOLUTION"]
    prompts = ["", "A close-up of a hand", "A wide shot of the skyline", "Generic scene"]
    for i in range(20):
        for pacing in pacings:
            for phase in phases:
                for prompt in prompts:
                    scene = {"i": i, "dur": 3.0, "pacing": pacing, "phase": phase,
                             "phase_source": "llm", "prompt": prompt}
                    m = _motion_for_scene(scene, None)
                    assert m["name"] not in removed, \
                        f"entfernter Name zurückgegeben: {m['name']} (i={i}, pacing={pacing}, phase={phase})"


def t_render_worker_motion_always_recomputed_not_cached():
    """Source-check: die Motion-Zuweisung darf keinen 'if not s.get(motion)'-Guard
    mehr haben, sonst würde eine Bibliotheks-Bereinigung (wie diese) bei einem
    Resume-Render unsichtbar bleiben -- die alte, in plan.json gespeicherte Motion
    würde für immer weiterverwendet."""
    src = open(os.path.join(ROOT, "dashboard.py")).read()
    idx = src.find('stage("motion")')
    assert idx != -1
    body = src[idx:idx + 900]
    assert 'if not s.get("motion")' not in body, \
        "Motion-Zuweisung darf nicht mehr gecacht sein (Guard gefunden)"
    assert 's["motion"] = _motion_for_scene(' in body


def t_hard_deadline_aborts_hanging_call_instead_of_blocking():
    """Ein hängender ElevenLabs-Socket (Idle-Timeout feuert nicht) darf den Job NICHT
    ewig blockieren: der Watchdog-Deckel (future.result(timeout=...)) muss feuern und
    nach erschöpften Retries mit RuntimeError abbrechen. Wir setzen den Deckel im Test
    auf ~0.5s und die Backoffs auf 0, damit der Test in <5s durchläuft."""
    import time as _t
    import engine_elevenlabs as el

    def _hang(url, body, headers):
        _t.sleep(2)  # > Test-Deckel (0.5s), aber kurz genug, dass abgehängte
                     # Worker den atexit-Join des Executors nicht spürbar verzögern
        return {}, "never"

    started = _t.time()
    raised = None
    with patch.object(el, "_urlopen_json", side_effect=_hang), \
         patch.object(el, "EL_HARD_DEADLINE_SEC", 0.5), \
         patch.object(el, "EL_BACKOFF_SEC", [0, 0]):
        try:
            el._elevenlabs_call_with_retry("http://x", {}, {})
        except RuntimeError as e:
            raised = e
    elapsed = _t.time() - started

    assert raised is not None, "erwartet: RuntimeError nach erschöpften Retries, nicht Hänger"
    assert "Timeout" in str(raised), f"Fehlermeldung soll den Timeout nennen, war: {raised}"
    # 3 Versuche à ~0.5s Deckel + 0s Backoff -> deutlich unter dem 30s-Hang
    assert elapsed < 10, f"Call hätte schnell abbrechen müssen, dauerte aber {elapsed:.1f}s"


def t_hard_deadline_normal_call_unaffected():
    """Eine schnelle Antwort läuft ganz normal durch — der Watchdog darf den Happy-Path
    nicht verändern und liefert (json, request-id) unverändert weiter."""
    import engine_elevenlabs as el

    def _fast(url, body, headers):
        return {"ok": True}, "req-xyz"

    with patch.object(el, "_urlopen_json", side_effect=_fast):
        resp, rid = el._elevenlabs_call_with_retry("http://x", {}, {})
    assert resp == {"ok": True} and rid == "req-xyz"


def main():
    tmp_home = setup()
    try:
        summary_section("A1: Chunk-Offset (echte Audiodauer statt letztes Wort-Ende)")
        run(t_a1_mp3_duration_measures_real_file, "ffprobe misst echte MP3-Dauer")
        run(t_a1_chunk_offset_uses_real_duration_not_last_word_end,
            "Chunk-2-Offset nutzt echte Dauer (2.5s), nicht letztes Wort-Ende (2.0s)")
        run(t_a1_model_dependent_chunk_limit, "Chunk-Limit ist modellabhängig")
        run(t_a1_request_id_comes_from_header_not_fake,
            "previous_request_ids kommt aus echtem request-id-Header")
        run(t_a1_v3_never_gets_previous_request_ids, "eleven_v3 bekommt nie previous_request_ids")

        summary_section("K2/K5: save_voice_settings Clamp + Whitelist")
        run(t_k2_speed_clamped_to_official_range, "speed wird auf 0.7-1.2 geclampt")
        run(t_k5_tts_provider_and_minimax_fields_persist,
            "tts_provider/volume/pitch überleben save/load-Roundtrip")

        summary_section("A5: Preserve-Helper (Voiceover-Regenerate verwaist keine Bilder mehr)")
        run(t_a5_preserve_matches_by_normalized_text, "Preserve matched via normalisierten Text")
        run(t_a5_preserve_empty_prev_is_noop, "Preserve ohne alten Plan ist No-Op")
        run(t_a5_transcribe_worker_uses_preserve_helper,
            "_transcribe_generate_worker nutzt den geteilten Helper")

        summary_section("A2/A3: Char-Ref-Fallback-Kette")
        run(t_a2_generate_one_uses_resolve_entity_ref, "generate_one nutzt _resolve_entity_ref")
        run(t_a3_wait_for_entity_anchor_returns_immediately_when_wait_false,
            "wait=False blockiert nicht")
        run(t_a3_wait_for_entity_anchor_treats_rendered_file_as_final,
            "gerenderte Szene ohne source_url gilt als final (kein 170s-Wait)")
        run(t_a3_resolve_entity_ref_stage_1b_local_file_fallback,
            "Stufe 1b: lokale Bilddatei als data-URL-Fallback")

        summary_section("4.2: Partial-Render-Warnung")
        run(t_42_render_start_warns_on_partial_scenes, "render_start warnt bei Teil-Renders")

        summary_section("Symbiose-Fix: Voiceover/Plan-Entkopplung")
        run(t_decouple_plan_has_usable_scenes_true_when_prompted,
            "_plan_has_usable_scenes erkennt promptierten Plan")
        run(t_decouple_plan_has_usable_scenes_false_when_empty_or_missing,
            "_plan_has_usable_scenes: leer/fehlend -> False")
        run(t_decouple_voiceover_skips_rebuild_when_plan_already_prompted,
            "Voiceover generieren lässt geprüften Plan unangetastet")
        run(t_decouple_voiceover_still_rebuilds_when_no_plan_exists,
            "Voice-first (kein Plan) baut weiterhin automatisch")

        summary_section("Symbiose-Fix: Enrichment-Token-Strip vor Alignment")
        run(t_strip_pause_tokens_removes_ellipsis_keeps_real_words,
            "'...'/'—' raus, 'blood.'/'$9' bleiben")
        run(t_strip_pause_tokens_fixes_alignment_word_count_mismatch,
            "Phantom-Tokens verschieben den Wortzeiger nicht mehr")
        run(t_render_worker_calls_strip_before_alignment,
            "_render_worker ruft Strip vor Pause-Trim/Alignment auf")

        summary_section("Symbiose-Fix: Sync-Invariant Präzision")
        run(t_sync_invariant_cut_points_land_on_start_aligned,
            "Schnittpunkte sitzen auf start_aligned, nicht proportional verschoben")
        run(t_sync_invariant_falls_back_when_not_all_aligned,
            "Fallback-Pfad bleibt bei Teil-Alignment unverändert")

        summary_section("Härtung: ElevenLabs-Call harter Per-Chunk-Deadline")
        run(t_hard_deadline_aborts_hanging_call_instead_of_blocking,
            "hängender Socket bricht via Watchdog ab statt ewig zu blockieren")
        run(t_hard_deadline_normal_call_unaffected,
            "schnelle Antwort läuft unverändert durch (json, request-id)")

        summary_section("Cinematic-Mix: Schritt 1 (Audio-Mix)")
        run(t_mix_music_underscore_lufs_not_voice_level, "Musik auf Underscore-Pegel, nicht Voice-Höhe")
        run(t_mix_sfx_category_volumes_match_hierarchy, "SFX-Pegel kategoriebasiert, nicht mehr Flat-0.7")
        run(t_mix_amix_calls_have_normalize_0, "amix normalize=0 (Stimme wird nicht mitgedämpft)")

        summary_section("Cinematic-Mix: Schritt 2 (SFX-Bibliothek + Riser-Timing)")
        run(t_sfx_library_only_maps_license_cleared_files, "SFX_LIBRARY nutzt nur lizenzgeklärte Dateien")
        run(t_riser_runup_starts_before_cut_not_at_cut, "Riser startet VOR dem Cut, Peak sitzt drauf")
        run(t_riser_runup_capped_even_for_long_files, "Riser-Anlauf gedeckelt trotz langer Reverb-Tails")
        run(t_sfx_density_cap_drops_close_big_events, "Dichte-Deckel: kein Stapeln großer SFX")
        run(t_phase_boundary_climax_entry_gets_braam_exit_gets_downshifter,
            "Phasen-Grenzen: CLIMAX-Eintritt=braam, Ausstieg=downshifter")

        summary_section("Cinematic-Mix: Schritt 3 (1-Wort-Captions)")
        run(t_align_scenes_populates_scene_relative_word_slices, "Wort-Slices scene-relativ zu start_aligned")
        run(t_overlay_specs_prefers_word_caption_seq_over_full_caption,
            "word_caption_seq statt Voll-Text-Caption, Fallback wenn words fehlt")
        run(t_render_worker_needs_alignment_also_checks_words,
            "Resume erzwingt Re-Alignment wenn words fehlt")
        run(t_render_worker_persists_words_to_plan_json,
            "plan.json-Persistenz behält words (kein endloses Re-Alignment)")
        run(t_word_caption_sequence_covers_full_clip_with_correct_timing,
            "Sequenz: blank vor 1. Wort, letztes Wort hält bis Clip-Ende")

        summary_section("Cinematic-Mix: Schritt 4 (motivierte Motion)")
        run(t_shot_hint_document_has_priority_over_closeup, "Dokument-Keyword schlägt Close-up")
        run(t_shot_hint_wide_and_no_match, "Wide-Shot erkannt, kein Match -> None")
        run(t_motion_avoids_direction_reversal_from_previous_scene, "Gegenrichtung zur Vorszene gemieden")
        run(t_motion_every_scene_gets_a_real_effect_never_stylistic_static,
            "jede Szene bekommt einen echten Effekt, static nur noch technischer Fallback")
        run(t_motion_hook_intensity_lowered_and_snap_zoom_capped, "Hook@1.0, snap_zoom_in z1<=1.16")

        summary_section("Feinschliff Runde 2: Motion-Tempo, Übergänge, Akt-Einspieler")
        run(t_zoom_in_out_boosted_to_120_140_percent, "zoom_in/zoom_out auf 120-140% angehoben")
        run(t_pan_tilt_travel_shrunk_and_centered, "Pan/Tilt-Wanderung klein + zentriert (langsamer)")
        run(t_static_absent_from_every_stylistic_candidate_pool, "static aus allen Stil-Listen entfernt")
        run(t_transition_rotation_uses_occurrence_position_not_scene_index,
            "Übergangs-Rotation über Vorkommen-Position, nicht Szenenindex")
        run(t_transition_families_expanded_to_four_variants, "wipe/smooth auf 4 Sub-Typen erweitert")
        run(t_render_worker_transition_calls_use_position_lookup,
            "_render_worker nutzt die Positions-Lookup an beiden Aufrufstellen")
        run(t_title_card_scenes_render_like_normal_scenes,
            "Akt-Einspieler rendern wie normale Szenen (echtes Bild statt weißer Karte)")

        summary_section("Nachjustierung Juli 2026: reine Sprecherspur + saubere Motion")
        run(t_no_sound_design_layer_render_worker_uses_raw_voice,
            "_render_worker muxt nur noch die rohe Sprecherspur")
        run(t_motion_library_has_no_combined_zoom_and_pan_recipes,
            "kein Rezept ändert Zoom UND Fokus gleichzeitig")
        run(t_motion_library_pan_tilt_zoom_boosted_to_120_140_percent,
            "Pan/Tilt-Zoom auf 120-140% angehoben")
        run(t_motion_picks_never_return_removed_combined_names,
            "Auswahl liefert nie mehr dolly_in/dolly_out/diagonal_glide")
        run(t_render_worker_motion_always_recomputed_not_cached,
            "Motion wird bei jedem Render frisch berechnet, nicht gecacht")
    finally:
        teardown(tmp_home)

    print(f"\n{'='*60}\n{PASSED} passed, {FAILED} failed\n{'='*60}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
