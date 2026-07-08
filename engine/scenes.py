"""engine.scenes — Szenen-Segmentierung + Sequenz-Ketten.

Enthält (Phase M.2, 2026-07-07):
    split_units                          — Text in atomare Einheiten zerlegen
    segment_by_pacing                    — Einheiten + Pacing-Labels → Szenen
    _renumber_seq_pos                    — seq_pos 0,1,2... pro seq_id vergeben
    _apply_visual_sequences_direct       — Audio-Pfad: seq_id direkt zuordnen
    _wait_for_chain_scene                — Polling auf plan.json für Sequenz-Anker
    _resolve_chain_refs                  — Doppel-Anker-Logik (Anker + Vorgänger)

BLEIBEN in dashboard.py bis Phase M.6 (Orchestrator-Aufräumen), weil sie
LLM-Config + Helper-Funktionen brauchen, die noch nicht extrahiert sind:
    analyze_script    — braucht LLM-URL-Config + JSON-Parsing-Helper
    _assign_phases    — braucht Phase-Coverage-Threshold + LLM-Datenstruktur
    visual_prompts    — braucht gen_image() (kommt nach engine/render.py in M.3)
    _build_image_prompt — kommt nach engine/prompts.py in M.5

Konstanten:
    MAX_SCENE_SEC, PACING_TARGET_SEC,
    NORMAL_HARD_CAP_SEC, PACING_WARN_THRESHOLD  — werden hier definiert und aus
    dashboard.py re-exportiert (Rückwärtskompatibilität).

Lazy-Import-Konvention (siehe routes/__init__.py):
    Andere engine-Module dürfen NICHT oben importiert werden — nur innerhalb
    von Funktionen. Damit ist `engine.scenes` zyklenfrei zu allen anderen
    engine-Modulen und zu dashboard.py.
"""

from __future__ import annotations

import json
import time


# ── Constants (Phase-Scene-Logic, extracted from dashboard.py Z. 658-666) ──────

MAX_SCENE_SEC = 6.0          # hard cap — no scene may hold longer than this
PACING_TARGET_SEC = {"calm": 5.0, "punchy": 1.1}
NORMAL_HARD_CAP_SEC = 5.5     # cap the "normal" target
PACING_WARN_THRESHOLD = 0.30  # >30% punchy → classifier likely over-fired


# ── Public API ────────────────────────────────────────────────────────────────

__all__ = [
    "MAX_SCENE_SEC",
    "PACING_TARGET_SEC",
    "NORMAL_HARD_CAP_SEC",
    "PACING_WARN_THRESHOLD",
    "split_units",
    "segment_by_pacing",
    "_renumber_seq_pos",
    "_apply_visual_sequences_direct",
    "_wait_for_chain_scene",
    "_resolve_chain_refs",
]


def split_units(text):
    """Split a script text into atomic units (sentences; long sentences additionally at
    commas/semicolons). Purely structural — no LLM call.
    """
    import re
    out = []
    for sent in re.findall(r"[^.!?]+[.!?]?", text):
        sent = sent.strip()
        if not sent:
            continue
        if len(sent.split()) <= 22:
            out.append(sent)
        else:
            for part in re.split(r"(?<=[,;:])\s+", sent):
                if part.strip():
                    out.append(part.strip())
    return out


def segment_by_pacing(units: list, pacing: list, wpm: float, normal_sec: float,
                       sequences: list = None, callouts: list = None) -> list:
    """Group atomic units into scenes using per-unit pacing labels.

    Full rationale (kept identical to dashboard.py Z. 670-692):
    - calm beats can be grouped together and held up to MAX_SCENE_SEC
    - punchy beats are never merged with neighbors, get compressed to ~1s
    - normal beats use the user's own sec-per-image dial
    - A sequence boundary forces a scene cut exactly like a pacing-label change
    - seq_pos is NOT taken from the LLM's per-unit value — it's reassigned 0,1,2...
      per seq_id AFTER grouping, since calm-merge / punchy-split mean raw per-unit
      position no longer lines up with the final scene count.
    """
    label_by_i = {p.get("beat"): p.get("label", "normal") for p in (pacing or []) if isinstance(p, dict)}
    seq_by_i = {}
    reason_by_sid = {}
    for seq in (sequences or []):
        if not isinstance(seq, dict):
            continue
        sid = seq.get("seq_id")
        if sid is not None and seq.get("reason"):
            reason_by_sid[sid] = seq["reason"]
        for beat_i in seq.get("beats", []) or []:
            if sid is not None:
                seq_by_i[beat_i] = sid
    callout_by_i = {c.get("beat"): c.get("text") for c in (callouts or [])
                    if isinstance(c, dict) and c.get("text")}
    targets = {"calm": PACING_TARGET_SEC["calm"],
               "normal": max(1.5, min(normal_sec, NORMAL_HARD_CAP_SEC)),
               "punchy": PACING_TARGET_SEC["punchy"]}
    hard_cap_words = max(3, round(MAX_SCENE_SEC * wpm / 60.0))

    n_punchy = sum(1 for i in range(len(units)) if label_by_i.get(i, "normal") == "punchy")
    if units and n_punchy / len(units) > PACING_WARN_THRESHOLD:
        print(f"  [Plan] WARNUNG: {n_punchy}/{len(units)} Einheiten als 'punchy' eingestuft "
              f"({n_punchy/len(units)*100:.0f}%) — ungewöhnlich hoch, ggf. Fehlklassifizierung", flush=True)

    segs, seg_seq_ids, seg_labels, seg_callouts = [], [], [], []
    cur, cur_label, cur_seq, cur_callout = [], None, None, None
    def flush():
        nonlocal cur, cur_seq, cur_callout
        if cur:
            segs.append(" ".join(cur))
            seg_seq_ids.append(cur_seq)
            seg_labels.append(cur_label)
            seg_callouts.append(cur_callout)
            cur = []
            cur_seq = None
            cur_callout = None

    for i, u in enumerate(units):
        label = label_by_i.get(i, "normal")
        if label not in targets:
            label = "normal"
        seq_id = seq_by_i.get(i)
        callout = callout_by_i.get(i)
        words = u.split()
        target_words = max(2, round(targets[label] * wpm / 60.0))

        if label == "punchy":
            flush()
            if len(words) > hard_cap_words * 2:
                for j in range(0, len(words), hard_cap_words):
                    segs.append(" ".join(words[j:j+hard_cap_words])); seg_seq_ids.append(seq_id); seg_labels.append(label); seg_callouts.append(callout)
            elif len(words) > target_words * 1.8:
                mid = len(words) // 2
                segs.append(" ".join(words[:mid])); seg_seq_ids.append(seq_id); seg_labels.append(label); seg_callouts.append(callout)
                segs.append(" ".join(words[mid:])); seg_seq_ids.append(seq_id); seg_labels.append(label); seg_callouts.append(callout)
            else:
                segs.append(u); seg_seq_ids.append(seq_id); seg_labels.append(label); seg_callouts.append(callout)
            cur_label = None
            continue

        if len(words) > hard_cap_words:
            flush()
            for j in range(0, len(words), hard_cap_words):
                segs.append(" ".join(words[j:j+hard_cap_words])); seg_seq_ids.append(seq_id); seg_labels.append(label); seg_callouts.append(callout)
            cur_label = None
            continue

        if cur and (cur_label != label or cur_seq != seq_id or len(cur) + len(words) > hard_cap_words):
            flush()
        cur.extend(words)
        cur_label = label
        cur_seq = seq_id
        if callout:
            cur_callout = callout
        if len(cur) >= target_words:
            flush()
            cur_label = None
    flush()

    scenes, t = [], 0.0
    for i, seg in enumerate(segs):
        dur = len(seg.split()) / (wpm / 60.0)
        scene = {"i": i, "start": round(t, 1), "dur": round(dur, 1), "text": seg,
                 "pacing": seg_labels[i] or "normal"}
        if seg_seq_ids[i] is not None:
            scene["seq_id"] = seg_seq_ids[i]
            reason = reason_by_sid.get(seg_seq_ids[i])
            if reason:
                scene["seq_reason"] = reason
        if seg_callouts[i]:
            scene["callout"] = seg_callouts[i]
        scenes.append(scene)
        t += dur

    _renumber_seq_pos(scenes)
    return scenes


def _renumber_seq_pos(scenes: list) -> None:
    """Assign seq_pos 0,1,2... per seq_id in final scene order, in-place.

    Used by both segmentation paths: the manual-script path (where merging/splitting
    means the LLM's raw per-unit position no longer lines up with final scenes) and
    the audio-transcription path (where it's defensive — the LLM's "beats" list should
    already be in order, but trusting our own recount instead of the raw value costs
    nothing and guards against an out-of-order response).

    Szenen ohne seq_id bekommen seq_pos=0 als Defaut (kein "skip", nur "kein Anker").
    """
    seq_counters = {}
    for scene in scenes:
        sid = scene.get("seq_id")
        if sid is None:
            scene["seq_pos"] = 0
            continue
        scene["seq_pos"] = seq_counters.get(sid, 0)
        seq_counters[sid] = scene["seq_pos"] + 1


def _apply_visual_sequences_direct(scenes: list, sequences: list) -> None:
    """Audio-transcription path: scenes already 1:1 with beats; seq_id via direct index.

    Only used by the audio path where no calm-merge / punchy-split happens. Manual
    scripts use segment_by_pacing instead.
    """
    for seq in (sequences or []):
        if not isinstance(seq, dict):
            continue
        sid = seq.get("seq_id")
        if sid is None:
            continue
        reason = seq.get("reason")
        for beat_i in seq.get("beats", []) or []:
            if isinstance(beat_i, int) and 0 <= beat_i < len(scenes):
                scenes[beat_i]["seq_id"] = sid
                if reason:
                    scenes[beat_i]["seq_reason"] = reason
    _renumber_seq_pos(scenes)


# ── Sequence chain (Doppel-Anker) — the most regression-sensitive code ────────
# These two functions are the reason M.2 exists before M.3/M.4/M.5: they are
# the core of "Das Auto ist kaputt" continuity, and the §11.4 tests need them
# in an importable module to run against. Every other phase (L, M-extract,
# O, 38, 39) must respect their invariants — see CINEMATIC_UPGRADE_PLAN.md §11.3.

def _wait_for_chain_scene(plan_path: str, seq_id, target_pos: int, timeout: float = 170.0) -> dict:
    """Block until the scene at (seq_id, target_pos) in plan.json has source_url,
    has failed, or the timeout elapses.

    Necessary because the batch worker dispatches up to MAX_CONCURRENT_IMAGE_GENS scenes
    at once — an anchor (seq_pos 0) and its first continuation (seq_pos 1) can land in
    the SAME concurrent batch, so the continuation must not read plan.json before the
    anchor's image actually finished uploading.

    Returns the scene dict (possibly empty if it failed or timed out) rather than
    raising — the caller falls back to no chain ref in that case.

    See §11.3 Schutzregel: timeout (170s) MUST stay > IMAGE_JOB_MAX_POLLS * 3s (150s).
    If you change the poll constant, change the timeout here too, and re-run
    t_seq_wait_timeout_exceeds_poll_max.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            plan = json.load(open(plan_path))
            match = next((s for s in plan["scenes"]
                          if s.get("seq_id") == seq_id and s.get("seq_pos") == target_pos), None)
            if match and (match.get("source_url") or match.get("status") == "fehler"):
                return match
        except Exception:
            pass
        time.sleep(2)
    try:
        plan = json.load(open(plan_path))
        return next((s for s in plan["scenes"]
                     if s.get("seq_id") == seq_id and s.get("seq_pos") == target_pos), {})
    except Exception:
        return {}


def _resolve_chain_refs(plan_path: str, scene: dict) -> tuple:
    """Returns (ref_urls, debug_info) for a scene that's part of a visual sequence.

    Doppel-Anker (CINEMATIC_UPGRADE_PLAN.md §11.1, Schutzregel 2):
    seq_pos 0 is the anchor — no chain reference needed, just the normal channel
    character reference. seq_pos >= 1 references BOTH the sequence's anchor image AND
    its immediate predecessor (deduplicated when they're the same image, i.e.
    seq_pos == 1) — a single fixed foundation image is what keeps nano-banana-2
    visually consistent; chaining only off the immediate predecessor would accumulate
    drift with every new generation.
    """
    if scene.get("seq_id") is None or scene.get("seq_pos", 0) == 0:
        return [], {}
    seq_id, pos = scene["seq_id"], scene["seq_pos"]
    anchor = _wait_for_chain_scene(plan_path, seq_id, 0)
    prev = anchor if pos - 1 == 0 else _wait_for_chain_scene(plan_path, seq_id, pos - 1)
    refs, debug = [], {}
    if anchor.get("source_url"):
        refs.append(anchor["source_url"]); debug["chain_anchor_file"] = anchor.get("file")
    if prev.get("source_url") and prev.get("file") != anchor.get("file"):
        refs.append(prev["source_url"]); debug["chain_prev_file"] = prev.get("file")
    return refs, debug


# ── Cross-scene character continuity (Juli 2026, User-Report: "Elizabeth sieht in
# jeder Szene anders aus") ─────────────────────────────────────────────────────
# _resolve_chain_refs only chains scenes inside the SAME visual sequence (seq_id) —
# most videos never group repeated-character scenes into a sequence at all (e.g. a
# character reappearing in scene 0, 3 and 5 with nothing in between), so those scenes
# had ZERO reference to each other and nano-banana-2 redesigned the character from
# scratch every time. This mirrors the sequence anchor's drift-avoidance reasoning:
# always reference the FIRST generated occurrence of a character (a fixed anchor),
# never the most recent one — chaining off "whatever came last" would let the design
# drift a little further with every repeat appearance.

def _wait_for_entity_anchor_scene(plan_path: str, entity: str, anchor_i: int, timeout: float = 170.0) -> dict:
    """Block until the scene at index `anchor_i` (the first scene showing `entity`) has
    source_url, has failed, or the timeout elapses. Same reasoning as
    _wait_for_chain_scene: the batch worker can dispatch the anchor and a later repeat
    of the same character in the same concurrent wave."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            plan = json.load(open(plan_path))
            match = next((s for s in plan["scenes"]
                          if s.get("i") == anchor_i and s.get("concrete_entity") == entity), None)
            if match and (match.get("source_url") or match.get("status") == "fehler"):
                return match
        except Exception:
            pass
        time.sleep(2)
    try:
        plan = json.load(open(plan_path))
        return next((s for s in plan["scenes"] if s.get("i") == anchor_i), {})
    except Exception:
        return {}


def _resolve_entity_ref(plan_path: str, scene: dict) -> tuple:
    """Returns (ref_urls, debug_info): the first already-generated (or in-flight)
    scene showing the same concrete_entity as `scene`, so every repeat appearance of a
    character reuses the very first generated image of them as a visual anchor —
    independent of visual sequences (see module comment above)."""
    entity = str(scene.get("concrete_entity", ""))
    if not entity.startswith("char_"):
        return [], {}
    try:
        plan = json.load(open(plan_path))
    except Exception:
        return [], {}
    earlier = [s for s in plan.get("scenes", [])
               if s.get("concrete_entity") == entity and s.get("i", -1) < scene.get("i", -1)]
    if not earlier:
        return [], {}
    anchor_i = min(s["i"] for s in earlier)
    anchor = _wait_for_entity_anchor_scene(plan_path, entity, anchor_i)
    if anchor.get("source_url"):
        return [anchor["source_url"]], {"entity_anchor_file": anchor.get("file"), "entity_anchor_i": anchor_i}
    return [], {}


# ── Phase O: Wort-Akzent-Puls ─────────────────────────────────────────────────

# Pause-Schwelle für Akzent-Kandidaten (Plan §4.4)
ACCENT_PAUSE_THRESHOLD_SEC = 0.25
# Mindest-Dauer einer Szene damit ein Akzent berechnet wird
ACCENT_MIN_SCENE_DUR_SEC = 2.0


def _is_accent_eligible(scene: dict) -> bool:
    """Phase O Eligibility-Check: nur punchy oder CLIMAX-Szenen ab 2s Länge."""
    if scene.get("dur", 0) < ACCENT_MIN_SCENE_DUR_SEC:
        return False
    pacing = scene.get("pacing", "normal")
    phase = scene.get("phase", "")
    return pacing == "punchy" or phase == "CLIMAX" or scene.get("is_climax", False)


def _compute_accent_t(scene_start: float, scene_end: float, words: list) -> float | None:
    """Phase O Akzent-Berechnung (deterministisch, kein LLM).

    Sucht im Wortbereich der Szene die längste Folgepause ≥ 0.25s. Tiebreak:
    längere Pause + Wort ≥ 8 Zeichen ODER Zahl gewinnt (Betonungs-Proxy für
    Sprecher-Akzente auf substantiellen Inhalten).

    Returns: Sekunden relativ zum Clip-Start (= scene_start), oder None wenn
    keine geeignete Pause gefunden.
    """
    in_scene = [w for w in words
                if w.get("start", 0) >= scene_start
                and w.get("end", 0) <= scene_end]
    if len(in_scene) < 2:
        return None

    def _score_word(w: dict) -> float:
        word = w.get("word", "")
        has_long = len(word) >= 8
        has_digit = any(c.isdigit() for c in word)
        return 0.5 if (has_long or has_digit) else 0.0

    candidates = []
    for i in range(len(in_scene) - 1):
        w_i, w_next = in_scene[i], in_scene[i + 1]
        gap = w_next["start"] - w_i["end"]
        if gap < ACCENT_PAUSE_THRESHOLD_SEC:
            continue
        mid_t = (w_i["end"] + w_next["start"]) / 2
        # Score = Pause + Wort-Score (Tiebreak)
        score = gap + _score_word(w_i) + 0.3 * _score_word(w_next)
        candidates.append((score, mid_t - scene_start, gap, w_i.get("word", "")))

    if not candidates:
        return None
    # Höchster Score gewinnt
    candidates.sort(reverse=True, key=lambda c: c[0])
    return candidates[0][1]