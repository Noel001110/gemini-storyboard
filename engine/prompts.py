"""engine.prompts — Bild-Prompt-Komposition + Character-Sheet-Pipeline + LLM-Bild-Generierung.

Enthält (Phase M.5 + M.6, 2026-07-07):
    Konstanten:
        IMAGE_PROMPT_CHUNK_SIZE, IMAGE_PROMPT_MIN_LEN
    Funktionen:
        _build_image_prompt          — Bild-Prompt zusammensetzen (Scene + Char-Refs + Master)
        _build_video_prompt          — Video-Prompt zusammensetzen (Veo)
        load_char_refs               — Char-Sheet-Metadaten aus Dateien laden
        analyze_char_image           — LLM-Aufruf: Character-Design-Spec aus Bild
        gen_charsheet                — 5-Pose-Sheet via Bildmodell generieren
        _anonymized_words            — Wörter von anonymize=true-Charakteren
        _validate_image_prompt_entry — Validierung der LLM-Output-Struktur
        _image_prompt_chunk          — LLM-Aufruf pro Chunk
        _image_prompt_single_retry   — Einzel-Retry bei Validation-Fail
        visual_prompts               — Orchestrator: chunked + validation + retry

NICHT hier:
    IMAGE_MASTER_DEFAULT, VIDEO_MASTER_DEFAULT  — bleiben in dashboard.py bis Phase Q
                                                (dann ersetzt durch PRESET_MASTERS)
    PHASE_PROMPT_ADDITIONS                      — lebt in engine_elevenlabs.py, nur Color-/Vignette-
                                                  Filter nutzen es (kein Bild-Prompt-Inject mehr)
    HOOK_PROMPT_ADDITION                        — entfernt aus Bild-Prompt (war Anti-Pattern,
                                                  siehe _build_image_prompt Docstring)
    dashboard.analyze_script, post_gemini_native — LLM-Bridge
"""

from __future__ import annotations

import base64
import json
import os
import re


# ── Public API ────────────────────────────────────────────────────────────────

__all__ = [
    "IMAGE_PROMPT_CHUNK_SIZE", "IMAGE_PROMPT_MIN_LEN",
    "SCRIPT_SYSTEM", "TITLE_SYSTEM", "THUMBNAIL_PROMPT_SYSTEM",
    "HOOK_PROMPT_ADDITION",  # Phase L
    "_build_image_prompt", "_build_video_prompt",
    "load_char_refs", "analyze_char_image", "gen_charsheet",
    "_anonymized_words", "_validate_image_prompt_entry",
    "_image_prompt_chunk", "_image_prompt_single_retry",
    "visual_prompts",
    "generate_script", "generate_titles",
    "make_thumbnail_prompt", "gen_thumbnail_image",
]


# ── Image-Prompt-Generation (LLM-Pipeline) ──────────────────────────────────

IMAGE_PROMPT_CHUNK_SIZE = 12   # scenes per LLM call. July 2026 (Diagnose): 40 Beats/Chunk produced
# 1229-char truncated output (JSON broken mid-entry, "Unterminated string"), 5 Beats/Chunk
# produced clean 4907 chars. 12 leaves a 4x safety margin under maxOutputTokens=8192 even
# for verbose anchors. JSON + few-shot examples + style context (repeated in full on EVERY
# chunk call) get sent far fewer times — that repeated overhead, not raw call count, is
# the real cost driver.
# 20 is a middle ground: cuts repeated-context cost ~55% vs. the earlier value of 9, while
# thinkingLevel=high keeps later-in-chunk quality from degrading like it did on 2.5-flash.
IMAGE_PROMPT_MIN_LEN    = 220  # chars — stills need less than video (no camera-move description) but still concrete

_IMAGE_PROMPT_FEWSHOT = """\
EXAMPLE — TOO SHORT / MISSES THE CONTENT (do not do this):
Line: "Reports suggested that people around him were monitored before his murder."
Bad image_prompt: "Dark ominous scene, surveillance concept"
→ Wrong: doesn't say WHO was monitored, doesn't show the surveillance mechanism, loses the actual fact.

EXAMPLE — CORRECT:
Line: "Reports suggested that people around him were monitored before his murder."
core_statement: "The target's inner circle was surveilled before his death."
concrete_entity: "char_target (anonymized), sym_surveillance_device"
Good image_prompt: "An empty chair in a press room, a phone resting on the floor beside it,
a faint glow on the phone screen suggesting active surveillance, dim somber lighting, nobody
visible in frame, composition emphasizing absence and unease"
→ Why better: translates "inner circle monitored" into a concrete object (glowing phone =
surveillance symbol), and is specific enough to define setting/light/focus — not just a mood word.\
"""


def _anonymized_words(analysis: dict) -> set:
    """Words belonging to characters marked anonymize=true in the Stage-1 analysis.
    These must NOT be required to appear literally in a prompt — the whole point of
    anonymize=true is that the person is depicted as a silhouette/symbol, never named.
    """
    words = set()
    for c in (analysis or {}).get("characters", []):
        if c.get("anonymize"):
            for field in (c.get("id", ""), c.get("name_or_role", "")):
                words.update(w.lower() for w in re.findall(r"[a-zA-Z]{4,}", field))
    return words


def _validate_image_prompt_entry(entry: dict, anonymized_words: "set | frozenset" = frozenset()) -> bool:
    ip = (entry.get("image_prompt") or "").strip()
    if len(ip) < IMAGE_PROMPT_MIN_LEN:
        return False
    entity = (entry.get("concrete_entity") or "").strip().lower()
    if entity and entity not in ("none", "n/a", "-"):
        # Juli 2026 (User-Report): the LLM sometimes echoes the raw internal entity id
        # (with its literal underscores, e.g. "char_elizabeth_holmes") straight into the
        # visible image_prompt instead of writing a natural-language description — KIE
        # then sees a meaningless code-like token instead of an actual description.
        if "_" in entity and entity in ip.lower():
            return False
        words = [w for w in re.findall(r"[a-zA-Z]{4,}", entity)
                 if w not in ("char", "loc", "sym", "anonymized") and w.lower() not in anonymized_words]
        if words and not any(w.lower() in ip.lower() for w in words):
            return False
    return True


def _image_prompt_chunk(chunk_beats: list, chunk_offset: int, total: int,
                         analysis_ctx: str, chunk_phases: list | None = None) -> list:
    """One LLM call for a small chunk of scenes (still images — no story-phase/camera-move
    logic like video; instead forces explicit character-consistency notes, since stills have
    no 'last frame of previous clip' anchor to inherit continuity from).
    """
    # Lazy-imports: LLM bridge functions live in dashboard.py
    from dashboard import post_gemini_native

    if chunk_phases is None:
        chunk_phases = [None] * len(chunk_beats)
    numbered = "\n".join(
        f"{chunk_offset+i+1}. [Phase: {p}] {t}" if p else f"{chunk_offset+i+1}. {t}"
        for i, (t, p) in enumerate(zip(chunk_beats, chunk_phases))
    )

    # July 2026 — Gemini responseSchema (passed through KIE.ai). Forces Gemini to
    # produce output that exactly matches this schema: no missing fields, no
    # unescaped quotes in user-facing strings, no truncation mid-value. Combined
    # with thinking_level="low" + maxOutputTokens=16384 in post_gemini_native,
    # this eliminates the JSON-parse cascade that was eating ~80% of long-script runs.
    _image_chunk_schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "scene": {"type": "integer"},
                "core_statement": {"type": "string"},
                "concrete_entity": {"type": "string"},
                "callback_check": {"type": "string"},
                "character_consistency": {"type": "string"},
                "line_specific_anchor": {"type": "string"},
                "image_prompt": {"type": "string", "minLength": 50},
            },
            "required": ["scene", "core_statement", "concrete_entity",
                         "callback_check", "character_consistency",
                         "line_specific_anchor", "image_prompt"],
        },
    }

    instr = f"""\
You are a storyboard director turning narration into single still images. You receive a
structural ANALYSIS of the full script and a CHUNK of consecutive narrator lines. Work
through each line using the forced fields below — do not skip straight to the final prompt.

LINE-SPECIFIC ILLUSTRATION (July 2026, User-Report: "Bilder wirken fast schon generisch"):
Every image must visually illustrate what the narrator is saying AT THIS MOMENT — not
what they said earlier, not what they will say next, and not a generic atmosphere that
could fit any scene. If you can imagine the image appearing in another scene without
anyone noticing, your image has failed. The single hardest rule of this work is:
the image is a visual translation of THIS line and nothing else.

ANALYSIS (entities, locations, symbols, emotional arc, callbacks — extracted from the FULL script):
{analysis_ctx}

PHASE STYLING (Phase C, Juli 2026) — each numbered line below is annotated with its
narrative phase. Adapt the image style to that phase:
- OPENING:       slow, deliberate composition; establish setting; neutral color palette; static-feeling even if motion comes later.
- RISING_ACTION: building tension; tighter framing; movement toward subject; contrast slightly elevated.
- CLIMAX:        maximum visual impact; high contrast; dynamic angle; subject dominates frame; emotional saturation.
- RESOLUTION:    wind-down; wider framing; softer palette; contemplative stillness.
Don't override the LINE'S TEXT — these cues modulate STYLING, not subject matter.

{_IMAGE_PROMPT_FEWSHOT}

For EACH line in the chunk below, produce an object with ALL of these fields, in order:
{{
  "scene": N,
  "core_statement": "What is this line actually claiming/showing? One sentence.",
  "concrete_entity": "The EXACT entity id from ANALYSIS (locations/characters/recurring_symbols)
                       relevant here. If none fits, name the new concrete thing from the line
                       itself (person/place/object/technology). Abstract metaphor ONLY if the
                       line truly has no concrete referent.",
  "callback_check": "Does ANALYSIS.callbacks say this scene references an earlier one? If yes,
                      name the recurring element that MUST appear in image_prompt. Else 'none'.",
  "character_consistency": "Since this is a single still with no motion/continuity anchor from
                             a previous clip, ANCHOR the character's identity (head shape,
                             proportions, distinguishing features like hair color/style,
                             signature outfit) so they remain recognizable across scenes.
                             BUT VARY pose, camera angle, framing, and facial expression per
                             scene to match THIS line's emotional beat — do NOT repeat the
                             same pose+expression+composition across scenes even if the
                             underlying identity description is identical.",
  "line_specific_anchor": "BEFORE you write image_prompt: list the ONE specific visual element
                            that makes THIS line different from the lines around it. What would
                            be wrong or missing if you swapped this image with the previous or
                            next scene's image? Examples: 'the cracked vial that wasn't there
                            before' (showing the test failed), 'the crowd turning away from the
                            founder' (showing rejection), 'a single yellow leaf on otherwise green
                            branches' (showing decline). If you cannot name one such element,
                            your scene is too generic — re-read the line and find the visual
                            detail that ONLY this narration introduces. 1-2 sentences.",
  "image_prompt": "The final image text. MUST visibly include concrete_entity AND the
                    callback_check element (if not 'none'). MUST visibly include the
                    line_specific_anchor — without it the image could belong to ANY scene,
                    and that is exactly what we are forbidding here. MUST reflect the
                    IDENTITY ANCHOR of character_consistency (hair, proportions, signature
                    outfit) but may VARY pose, framing, expression to match the scene —
                    repeating the same pose+expression across consecutive scenes makes
                    characters look like the same photo reused. NO art-style words
                    here (line weight, color palette etc. — that's applied separately from
                    the master prompt). Must explicitly name: (1) the concrete main subject,
                    (2) the setting/location, (3) the composition/framing, AND (4) the
                    line_specific_anchor. A prompt that describes only a generic mood
                    without these four elements is invalid. Minimum {IMAGE_PROMPT_MIN_LEN} characters."
}}

HARD RULE: if a line names a concrete person, place, or technology, image_prompt MUST show
exactly that — check this yourself against your own concrete_entity field before writing it.

SENSITIVE content (violence/death/abuse/trafficking): tasteful symbolism only, never graphic.

NARRATOR LINES IN THIS CHUNK:
{numbered}

Return a JSON array of {len(chunk_beats)} objects, one per line above, in the same order.
"""
    txt = post_gemini_native([{"role": "user", "content": instr}], json_mode=True, temp=0.6,
                            thinking_level="high", response_schema=_image_chunk_schema)
    # July 2026 (User-Report): thinking zurück auf "high" — der User hat beobachtet dass
    # "low" zu schlechterer Bildqualität führt (Reasoning fehlt für kreative
    # Bild-Prompts). Mit dem responseSchema-Fix + maxOutputTokens=16384 haben wir
    # genug Puffer, dass "high" nicht mehr truncated wird.
    # July 2026 — robust JSON parsing: the new `line_specific_anchor` field frequently
    # contains quoted phrases or dialogue-style snippets that the LLM does not always
    # escape properly, breaking json.loads(). Without recovery, every parse-fail cascades
    # into chunk-splitting + single-scene retries, ballooning 5-min generations into 15+
    # min. Recovery strategy: try strict parse, then strip down to the outermost JSON array
    # via regex (handles surrounding prose/markdown), then return [] as a last resort.
    try:
        arr = json.loads(txt)
    except json.JSONDecodeError:
        m = re.search(r"\[\s*\{.*\}\s*\]", txt, re.DOTALL)
        if not m:
            print(f"  [Plan] WARNUNG: keine JSON-Array-Struktur in Gemini-Antwort erkennbar "
                  f"(Antwort-Laenge {len(txt)} chars). Chunk wird als leer zurueckgegeben, "
                  f"einzelne Szenen gehen in den Retry-Pfad.", flush=True)
            return []
        try:
            arr = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            print(f"  [Plan] WARNUNG: JSON-Recovery fehlgeschlagen ({e}). Chunk leer.", flush=True)
            return []
    if isinstance(arr, dict):
        for v in arr.values():
            if isinstance(v, list) and len(v) == len(chunk_beats):
                arr = v; break
    if not isinstance(arr, list) or len(arr) != len(chunk_beats):
        raise ValueError(f"unexpected chunk response shape ({type(arr)}, len={len(arr) if isinstance(arr,list) else '?'})")
    return arr


def _image_prompt_single_retry(beat_text: str, beat_i: int, total: int, analysis_ctx: str) -> dict:
    """Focused single-scene retry for entries that failed validation in the batch call.

    Juli 2026 (User-Report: mehrere Szenen landeten mit einem barebones
    "Scene illustrating: ... Simple, clear composition."-Notprompt statt eines echten
    Bild-Prompts — kein visueller roter Faden, Stilbrüche): vorher genau EIN Versuch,
    dann sofort der unmarkierte Fallback bei jedem Fehler (auch einem simplen,
    transienten JSON-Parse-Fehler). Jetzt bis zu 3 Versuche; erst wenn wirklich alle
    scheitern, greift der Fallback — und der wird als "prompt_error" markiert statt
    unauffällig wie eine normale Szene durchzugehen, damit er auffindbar und gezielt
    nachbearbeitbar bleibt statt stillschweigend eine schwache Bild-Generierung zu
    verursachen.
    """
    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            result = _image_prompt_chunk([beat_text], beat_i, total, analysis_ctx)
            return result[0]
        except Exception as e:
            last_err = e
            print(f"  [Plan] Bild-Einzel-Retry Szene {beat_i} Versuch {attempt}/3 fehlgeschlagen: {e}", flush=True)
    print(f"  [Plan] FEHLER: Szene {beat_i} — Prompt-Generierung nach 3 Versuchen endgueltig "
          f"gescheitert ({last_err}). Als prompt_error markiert.", flush=True)
    return {
        "scene": beat_i + 1, "concrete_entity": "",
        "image_prompt": f"Scene illustrating: {beat_text[:80]}. Simple, clear composition.",
        "prompt_error": True,
    }


def visual_prompts(scenes, analysis=None):
    """Generate all still-image prompts, chunked (not all-in-one) with forced intermediate
    reasoning fields and a validation+retry pass — same structure as video_prompts_batch(),
    adapted for stills (no story-phase/camera-move logic, explicit character-consistency
    field instead since there's no chain-extend anchor between separate images).

    Returns list of {"prompt": str, "concrete_entity": str} dicts, one per scene, same
    order as scenes. concrete_entity is already computed per entry for validation
    purposes below — it used to be discarded after that; now it's returned too so
    callers can persist it onto the scene (used for conditional character-reference
    attachment, see _batch_generate_worker). Style (master prompt) is NOT included in
    the prompt text — it's appended separately in _build_image_prompt().
    """
    # Lazy-imports: analyze_script lives in dashboard.py
    from dashboard import analyze_script

    beats = [s["text"] for s in scenes]
    total = len(beats)
    if total == 0:
        return []

    if analysis is None:
        print(f"  [Plan] Analysiere {total} Beats …", flush=True)
        analysis = analyze_script(beats)
    analysis_ctx = json.dumps(analysis, ensure_ascii=False, indent=1) if analysis else "{}"
    anon_words = _anonymized_words(analysis)

    def _fetch_image_chunk(chunk, chunk_offset, chunk_phases=None):
        """Try the chunk; on failure (incl. truncated/malformed JSON on large chunks),
        split in half and retry each half instead of giving up the whole chunk to the
        generic fallback — a truncation only costs half the chunk, not all of it.

        Juli 2026: at the leaf (chunk size 1, can't split further) this used to give up
        after a single failed attempt — same "either it works or the scene silently gets
        a near-empty placeholder prompt" problem as _image_prompt_single_retry. Now
        retries 3x at the leaf before falling back, and the fallback is marked
        prompt_error=True so it's never mistaken for a normal, fully-formed prompt.
        """
        try:
            return _image_prompt_chunk(chunk, chunk_offset, total, analysis_ctx, chunk_phases)
        except Exception as e:
            if len(chunk) <= 1:
                last_err = e
                for attempt in range(2, 4):
                    try:
                        return _image_prompt_chunk(chunk, chunk_offset, total, analysis_ctx, chunk_phases)
                    except Exception as e2:
                        last_err = e2
                        print(f"  [Plan] Bild-Chunk-Fehler (Szene {chunk_offset}) Versuch {attempt}/3: {e2}", flush=True)
                print(f"  [Plan] FEHLER: Szene {chunk_offset} — Chunk-Generierung nach 3 Versuchen "
                      f"endgueltig gescheitert ({last_err}). Als prompt_error markiert.", flush=True)
                return [{"image_prompt": f"Scene illustrating: {chunk[0][:80]}. Simple, clear composition.",
                         "concrete_entity": "", "prompt_error": True}]
            mid = len(chunk) // 2
            print(f"  [Plan] Bild-Chunk-Fehler: {e} — teile Chunk und wiederhole …", flush=True)
            left  = _fetch_image_chunk(chunk[:mid], chunk_offset, (chunk_phases or [])[:mid])
            right = _fetch_image_chunk(chunk[mid:], chunk_offset + mid, (chunk_phases or [])[mid:])
            return left + right

    prompts: list[dict] = []
    phases = [s.get("phase", "") for s in scenes]
    chunks = [beats[i:i+IMAGE_PROMPT_CHUNK_SIZE] for i in range(0, total, IMAGE_PROMPT_CHUNK_SIZE)]
    chunks_phases = [phases[i:i+IMAGE_PROMPT_CHUNK_SIZE] for i in range(0, total, IMAGE_PROMPT_CHUNK_SIZE)]
    offset = 0
    for ci, chunk in enumerate(chunks):
        print(f"  [Plan] Bild-Chunk {ci+1}/{len(chunks)} ({len(chunk)} Szenen) …", flush=True)
        entries = _fetch_image_chunk(chunk, offset, chunks_phases[ci])

        for j, entry in enumerate(entries):
            beat_i = offset + j
            if not _validate_image_prompt_entry(entry, anon_words):
                print(f"  [Plan] Szene {beat_i} zu kurz/generisch — Einzel-Retry …", flush=True)
                entry = _image_prompt_single_retry(beats[beat_i], beat_i, total, analysis_ctx)
            prompts.append({
                "prompt": str(entry.get("image_prompt") or f"Scene illustrating: {beats[beat_i][:80]}."),
                "concrete_entity": str(entry.get("concrete_entity") or ""),
                "prompt_error": bool(entry.get("prompt_error", False)),
            })
        offset += len(chunk)

    return prompts


# ── Phase-Style-Lookup ───────────────────────────────────────────────────────

# _phase_prompt_addition() removed July 2026: phase cues are no longer injected into the
# image prompt (image style is owned by master + style_ref image). PHASE_PROMPT_ADDITIONS
# in engine_elevenlabs.py still feeds PHASE_COLOR_FILTER / PHASE_VOLUME / PHASE_ACCENT
# for FFmpeg-side dramaturgy. Callers that need the lookup should import
# `engine_elevenlabs.PHASE_PROMPT_ADDITIONS` directly.


# ── Prompt-Komposition ───────────────────────────────────────────────────────

# Phase L — Hook-Style-Cue (analog PHASE_PROMPT_ADDITIONS, aber für Hook-Szenen).
# Hart injiziert wie die Phase-Cues, damit der Hook-Charakter garantiert wird und
# nicht von einem weichen LLM-Hint abhängt.
HOOK_PROMPT_ADDITION = (
    "single striking focal subject, maximum negative space, poster-like composition, "
    "immediate visual hook that stops the scroll — viewer must understand the image in "
    "under one second"
)


def _filter_char_refs_for_entity(char_refs, entity=""):
    """Only a charsheet whose name exactly matches this scene's concrete_entity belongs
    in that scene's prompt as a TEXT character-design override.

    Deliberately excludes the generic global 'style_ref' charsheet (the single reference
    image set in Settings) from this text filter — it is a visual style anchor, attached
    separately as an *image* reference (see dashboard.py's style_ref_url handling), never
    as a forced textual "this exact build/outfit wins" directive. An earlier version of
    this filter treated 'style_ref' as always-included text, which meant its
    Gemini-Vision-derived description (e.g. "stout build, teal sweater, brown trousers")
    silently overrode the scene's own, correct character description ("blonde hair,
    black turtleneck") in every single scene — producing a wrong-looking character that
    matched neither the reference image's actual look nor the intended prompt.

    Without any filtering at all, EVERY charsheet ever created in the channel gets glued
    onto EVERY scene's prompt regardless of which video/character it belongs to — a
    channel previously used for a different video (e.g. a journalist story) leaves
    behind charsheets that then silently contaminate a brand-new, unrelated video.
    """
    if not char_refs:
        return []
    entity_key = entity[5:] if entity.startswith("char_") else entity
    entity_key = entity_key.strip().lower()
    if not entity_key:
        return []
    out = []
    for cr in char_refs:
        if not _is_valid_char_description(cr.get("description", "")):
            continue
        safe = (cr.get("safe") or "").lower()
        if safe == entity_key:
            out.append(cr)
    return out


def _build_image_prompt(scene_prompt, master, char_refs, phase="", is_hook=False, entity=""):
    """Compose the final image-generation prompt: scene text + (filtered) character refs + master.

    July 2026 (User-Report): Phase cues and Hook cues used to be hard-injected here. That was a
    layering mistake — image style is owned by master + style_ref image. Phase/hook effects
    (colorbalance, vignette, snap-zoom) belong in engine/render.py via FFmpeg / motion rules,
    never in the KIE prompt. Injected cue-words ("high contrast, dynamic angle, emotional
    saturation") were triggering KIE to switch art direction per phase (anime-leaning CLIMAX,
    photoreal-leaning RESOLUTION) on top of any real style bug.

    `phase` and `is_hook` are kept in the signature for backward compatibility with the few
    callers in dashboard.py / tests; they are no longer used inside this function.

    Char-Ref-Filter (Phase 1): Müll-Injection-Schutz, plus entity-scoping (Phase 1b) via
    `_filter_char_refs_for_entity` — see that function's docstring for why unscoped
    injection was actively wrong.
    """
    char_hint = ""
    relevant_refs = _filter_char_refs_for_entity(char_refs, entity)
    for cr in relevant_refs:
        desc = cr.get("description", ""); name = cr.get("name", "Figur")
        char_hint += (f"\n\nCHARACTER DESIGN for '{name}': {desc}"
                      f"\nApply this exact design in whatever pose this scene requires.")
    if relevant_refs:
        # Without this, the scene's own (auto-written, reference-unaware) text
        # description can invent conflicting physical traits — e.g. the scene text
        # says "blonde hair" while the actual reference photo/charsheet is brunette —
        # and the model has no instruction on which one to trust.
        char_hint += ("\n\nIMPORTANT: The character design(s) above, and any attached "
                      "reference image, define this character's true appearance (hair, "
                      "face, outfit, build). If the scene description below conflicts "
                      "with them, the character design / reference image wins.")
    return scene_prompt + char_hint + "\n\n" + master


def _build_video_prompt(scene_prompt: str, vid_master: str) -> str:
    """Append the literal master prompt to the scene action description.
    Veo only ever sees the final submitted string — it has no access to the
    dashboard's master prompt field, so the style must be embedded here every
    time, not just hinted to the LLM that writes the scene description.
    """
    return scene_prompt.strip() + "\n\nVISUAL STYLE (apply exactly):\n" + vid_master.strip()


# ── Character-Sheets ─────────────────────────────────────────────────────────

# ── Phase 1: Müll-Injection-Schutz ──────────────────────────────────────────────

# Test-Stick-Figure-Patterns aus früheren Tests. Diese Strings übersteuern den Master-Prompt
# und produzieren Strichmännchen statt des gewählten Preset-Stils. Werden als Müll gefiltert.
_MULL_PATTERNS = (
    "torso is a single vertical line",
    "minimalist stick-figure aesthetic",
    "single lines with rounded joints",
    "limbs terminate in rounded ends",
    "no hands or feet",
)


def _is_valid_char_description(desc: str) -> bool:
    """Müll-Injection-Schutz für charsheet.description.

    Returns False wenn description zu kurz ist (<30 Zeichen) oder explizite
    Test-Müll-Marker enthält. Echte Char-Beschreibungen sind ≥30 Zeichen.
    """
    if not desc:
        return False
    desc_stripped = desc.strip()
    if len(desc_stripped) < 30:
        return False
    desc_lower = desc_stripped.lower()
    if any(p in desc_lower for p in _MULL_PATTERNS):
        return False
    return True


# ── Phase 2: charsheet-PNGs als data-URL für KIE-Bildreferenz ───────────────────

def _local_png_to_data_url(local_path: str) -> str:
    """Liest eine lokale PNG-Datei und returnt sie als data:image/png;base64,...

    KIE akzeptiert data-URLs direkt im image_input/image_urls-Parameter. Vermeidet
    litterbox-Upload (war flaky, 403) und TTL-Probleme. Daten-URL-Größe: ~1.5 MB pro PNG
    wird zu ~2 MB Base64 — KIE akzeptiert problemlos einzelne Bilder dieser Größe.
    """
    with open(local_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def load_char_refs(cid="default", vid=None):
    """Load character-sheet metadata from JSON files in the channel's (or video's) charsheets dir.

    July 2026: charsheets are now per-video. Pass vid to load from
    channels/<cid>/videos/<vid>/charsheets/. Without vid, falls back to the channel-global pool.
    Per-video takes precedence when the directory exists.

    Phase 1 (Müll-Injection-Schutz): Jedes Charsheet wird durch _is_valid_char_description
    validiert. Müll-JSONs werden komplett übersprungen — sonst übersteuern die Test-Stil-Specs
    den Master-Prompt und produzieren Strichmännchen.

    Phase 2 (Bild-Referenz): Wenn die zugehörige PNG-Datei existiert, wird sie als data:image/png;base64,...
    an das Bildmodell gehängt (via meta["image_data_url"]). KIE akzeptiert data-URLs direkt —
    kein litterbox-Upload, kein 403-Risiko, kein TTL-Problem.
    """
    # Lazy-import to avoid cycle: ch_sheets is in dashboard.py
    from dashboard import ch_sheets
    sheet_dir = ch_sheets(cid, vid)
    refs = []
    try:
        files = os.listdir(sheet_dir)
    except OSError:
        return refs
    for f in sorted(files):  # deterministische Reihenfolge
        if not f.endswith(".json"):
            continue
        try:
            meta = json.load(open(os.path.join(sheet_dir, f)))
            desc = meta.get("description", "")
            if not _is_valid_char_description(desc):
                continue
            # Phase 2: PNG als data-URL einlesen, wenn vorhanden
            png_path = os.path.join(sheet_dir, f.replace(".json", ".png"))
            if os.path.exists(png_path):
                try:
                    meta["image_data_url"] = _local_png_to_data_url(png_path)
                except OSError:
                    pass
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


def gen_charsheet(cid, name, description, vid=None):
    """Generate a character reference sheet image (5 poses) and return the bytes.

    July 2026: vid is now accepted. When given, the temp PNG is written into the
    per-video charsheets directory. Without vid, falls back to the channel-global
    pool (backwards-compat for old call sites).
    """
    # Lazy-imports: ch_sheets + gen_image live in dashboard.py
    from dashboard import ch_sheets, gen_image
    # July 2026 (User-Report: "5 Strichmännchen statt Charakter-Sheet"): Der alte
    # Prompt war ein Test-Stub (white background, black ink only, medium-weight lines,
    # no shading). KIE nano-banana-2 hat das brav als Strichmännchen-Skizzen
    # gerendert. Wir wollen aber einen Character-Reference-Sheet, der im Bild-Generate
    # als Stil-Anker dient — also: realistische Posen, konsistentes Aussehen,
    # volle Beleuchtung. Hintergrund bleibt neutral (Studio) für saubere Ref-Verwendung.
    prompt = (
        f"CHARACTER REFERENCE SHEET for '{name}' — designed for character consistency "
        f"across multiple scenes.\n\n"
        f"Show the character in 5 different poses on a single horizontal row, on a clean "
        f"light-grey studio background. Each pose is clearly separated with subtle "
        f"white spacing. The character must be drawn in a polished, semi-realistic style "
        f"with full shading and natural lighting.\n\n"
        f"Poses (left to right):\n"
        f"1. NEUTRAL — front-facing, standing relaxed, arms at sides, neutral expression\n"
        f"2. THREE-QUARTER VIEW — slight angle, hands on hips, confident expression\n"
        f"3. WALKING — mid-stride, side profile, dynamic pose\n"
        f"4. CLOSE-UP PORTRAIT — head and shoulders only, looking slightly off-camera, "
        f"engaged expression\n"
        f"5. ACTION — gesturing with one hand raised, mid-conversation pose\n\n"
        f"Character design specifications: {description}\n\n"
        f"CRITICAL CONSISTENCY REQUIREMENTS:\n"
        f"- All 5 poses MUST share identical face shape, head size, hair colour and style, "
        f"skin tone, and clothing design\n"
        f"- Same age, same body proportions across all poses\n"
        f"- Each pose uses the same colour palette and rendering technique\n"
        f"- No text labels, no captions, no annotations on the image — pure visual reference\n"
        f"- Light grey studio background (#F0F0F0) with soft even lighting on the character"
    )
    # Pre-33.2 cleanup: backslash inside an f-string expression part is illegal on
    # Python 3.11/3.12 (PEP 701, allowed only in 3.13+). Extracting the regex
    # sanitizer value to its own line keeps the server startable on 3.11/3.12.
    tmp_name = re.sub(r"[^\w]", "_", name)
    sheet_dir = ch_sheets(cid, vid)
    os.makedirs(sheet_dir, exist_ok=True)  # per-video charsheets dir may not exist yet
    tmp = os.path.join(sheet_dir, f"_tmp_{tmp_name}.jpg")

    # Juli 2026 (User-Report: "charsheets sehen für unterschiedliche Kanäle anders aus"):
    # Wir laden das Style-Referenz-Bild des Kanals (channels/<cid>/style_ref.png)
    # und reichen es an KIE als Bild-Referenz mit, damit das Charsheet im kanal-eigenen
    # Stil gerendert wird (Tusche, Photorealismus, Wasserfarben, etc.). Ohne diese
    # Referenz rendert KIE die Charsheets in einem generischen Look.
    char_refs = None
    try:
        from dashboard import get_channel_style_ref
        style_ref_url = get_channel_style_ref(cid)
        if style_ref_url and style_ref_url.startswith(("http://", "https://")):
            # Remote URL — direkt durchreichen
            char_refs = [{
                "name": "style_ref",
                "description": "Channel-wide style reference (line weight, palette, render style)",
                "image_data_url": style_ref_url,
                "safe": "style_ref",
            }]
        else:
            # Lokales Bild (oder fehlt) — vom Disk laden, in data-URL kodieren
            sp = os.path.join(ch_dir(cid), "style_ref.png")
            if os.path.exists(sp):
                char_refs = [{
                    "name": "style_ref",
                    "description": "Channel-wide style reference (line weight, palette, render style)",
                    "image_data_url": _local_png_to_data_url(sp),
                    "safe": "style_ref",
                }]
    except Exception as e:
        print(f"  [Charsheet] Style-Ref konnte nicht geladen werden: {e}", flush=True)
        char_refs = None

    res = gen_image(prompt, "", tmp, char_refs=char_refs)
    if res["ok"]:
        data = open(tmp, "rb").read()
        try: os.unlink(tmp)
        except: pass
        return data
    raise RuntimeError(f"Character sheet generation failed: {res.get('error')}")

# ── Script-Generation (LLM, Simplicissimus-Stil) ─────────────────────────────
# Diese Prompts/Konstanten sind LLM-System-Prompts — gehören thematisch zu
# engine.prompts.py (Pipeline der Text-Generierung). Wird per Lazy-Import aus
# dashboard.py aufgerufen.

SCRIPT_SYSTEM = """\
You are a documentary script writer. Your style matches Simplicissimus — the German YouTube channel known for narrative-documentary storytelling with investigative tension.

Your task: turn a raw transcript, notes, or video idea into a polished documentary voiceover script.

REQUIREMENTS:
- First-person or close-third-person narrator voice, consistent throughout
- Short sentences, spoken cadence. ~120-150 words per minute.
- One clear idea per paragraph; each paragraph ~3-6 sentences.
- Build tension deliberately: HOOK in the opening 1-2 sentences (a concrete scene, a striking claim, a question that pulls the viewer in).
- End with an OPEN QUESTION, not a summary. The last paragraph should leave the viewer thinking, not wrap things up.
- Emotional arc: opens with tension or curiosity, deepens through investigation, lands on a reflective beat.
- NEVER invent specific facts, numbers, dates, or names not present in the input.
- If the input is sparse, write a short but complete script — never pad with filler.

OUTPUT FORMAT:
- Plain text, paragraphs separated by blank lines.
- DO NOT include any preamble, title, or meta-commentary.
- DO NOT label scenes, acts, or chapters.
- Chapter titles as ## headings. Blank line between paragraphs.
- The output must NOT be word-for-word identical to the input — it must be freshly written in this style.
"""


def generate_script(raw_input: str, lang: str) -> str:
    lang_instr = (
        "Write the script in German (natural spoken German, not formal)."
        if lang == "de"
        else "Write the script in English (clear, neutral international English)."
    )
    user_msg = (
        f"{lang_instr}\n\n"
        f"Here is the raw input — a transcript, rough notes, or video ideas. "
        f"Rewrite it as a polished documentary voiceover script following the schema above. "
        f"Keep all key facts and arguments, but rephrase everything freshly:\n\n"
        f"{raw_input}"
    )
    # Lazy: post_kie_text lives in dashboard.py
    from dashboard import post_kie_text
    msgs = [
        {"role": "system", "content": SCRIPT_SYSTEM},
        {"role": "user", "content": user_msg},
    ]
    return post_kie_text(msgs, temp=0.8)


# ---------- Title generator (viral/clickbait, research-backed formulas) ----------
# Formulas per 2026 CTR research: curiosity gap + loss-aversion/FOMO + a concrete
# number or fact + an emotional hook, 55-60 chars so it doesn't truncate on mobile.
# "Exaggerate the tension, not the outcome" — titles must stay factually accurate to
# the script, no fabricated claims.

TITLE_SYSTEM = """\
You are a YouTube title strategist. You write titles using proven high-CTR formulas,
but you NEVER misrepresent what the video actually contains — you exaggerate the
TENSION and stakes already present in the script, never invent a claim the script
doesn't support. Misleading clickbait is not acceptable; a strong honest hook is.

FORMULAS TO DRAW FROM (mix, don't just pick one every time):
- Curiosity gap: hint at a shocking fact/connection without revealing it
- Number-based: "[Number] [Things] That [Concrete Result]"
- Loss-aversion / FOMO: what the viewer doesn't know yet, what they're missing
- Personal-authority: "[credible framing]. Here's what [it] means for you."

RULES:
- 55-60 characters total (titles longer than this get truncated on mobile — this is
  a hard constraint, not a suggestion)
- Every claim in the title must be directly supported by the script content given
- No emoji, no ALL CAPS spam, no exclamation-mark stacking
- Return options in the exact language the script is written in
"""


def generate_titles(full_script: str, n: int = 5) -> list:
    """Generate N candidate clickbait-but-honest titles from the full script."""
    from dashboard import post_gemini_native  # lazy
    user_msg = (
        f"Generate {n} distinct YouTube title options for this script, using the "
        f"formulas above. Return ONLY a JSON array of {n} strings, nothing else.\n\n"
        f"SCRIPT:\n{full_script.strip()[:6000]}"
    )
    try:
        txt = post_gemini_native([
            {"role": "system", "content": TITLE_SYSTEM},
            {"role": "user", "content": user_msg},
        ], json_mode=True, temp=0.9)
        arr = json.loads(txt)
        if isinstance(arr, dict):
            for v in arr.values():
                if isinstance(v, list): arr = v; break
        if isinstance(arr, list):
            return [str(t).strip() for t in arr][:n]
    except Exception as e:
        print(f"  [Title] Fehler: {e}", flush=True)
    return []


# ---------- Thumbnail generator ----------
# Research-backed rules (2026 CTR studies): one dominant subject, one message, one
# second to understand. Strong contrast (dark bg + light subject, or reverse).
# Expressive/exaggerated emotion — thumbnails with visible expression see 20-30%
# higher CTR. Max 3-5 words of on-image text (under 4 words = ~30% higher CTR than
# text-heavy designs). 2-3 colors max. 1280x720 (16:9), sharp focus, rule of thirds.

THUMBNAIL_PROMPT_SYSTEM = """\
You write a single image-generation prompt for a YouTube THUMBNAIL — this is a
fundamentally different job than a storyboard scene. A thumbnail must work as a tiny,
high-contrast image glanced at for under a second in a crowded feed. Apply these
non-negotiable rules:

1. ONE dominant subject only — the main character or the single most concrete symbol
   of the video's hook. No busy multi-element scenes.
2. STRONG CONTRAST — either a light subject on a dark background or a dark subject on
   a light background. Never a low-contrast, evenly-lit scene.
3. EXAGGERATED, READABLE EMOTION on the subject if it's a character — shock, alarm,
   intense focus, fear, urgency. Subtle/neutral expressions do not work for thumbnails.
4. RULE OF THIRDS — subject off-center, clear headroom, nothing important near the edges.
5. NO more than one small supporting prop/symbol tied directly to the video's hook.
6. Do not describe on-image text here — text is composited separately.
7. Keep the established character/art style exactly as given in STYLE CONTEXT, but push
   the POSE, EXPRESSION, and LIGHTING to thumbnail-appropriate extremes — a thumbnail
   is the most exaggerated, highest-contrast frame of the whole video, not a typical one.

Output ONE dense paragraph, 50-70 words. Start with the subject and its expression.
"""


def make_thumbnail_prompt(full_script: str, master_style: str) -> str:
    """Builds the single most attention-grabbing image prompt for this video's thumbnail,
    grounded in the actual hook/subject of the script (not a generic dramatic pose)."""
    from dashboard import post_gemini_native  # lazy
    user_msg = (
        f"STYLE CONTEXT (character/art style — follow exactly, push expression/lighting "
        f"to thumbnail extremes):\n{master_style.strip()}\n\n"
        f"FULL SCRIPT — identify the single most shocking/central hook and depict that:\n"
        f"{full_script.strip()[:4000]}\n\n"
        f"Write the thumbnail image prompt now."
    )
    try:
        return post_gemini_native([
            {"role": "system", "content": THUMBNAIL_PROMPT_SYSTEM},
            {"role": "user", "content": user_msg},
        ], temp=0.7).strip()
    except Exception as e:
        print(f"  [Thumbnail] Prompt-Fehler: {e}", flush=True)
        return "A single figure in a moment of shocked realization, strong dramatic lighting, high contrast."


def gen_thumbnail_image(prompt: str, master_style: str, out_path: str,
                         model: str = "nano-banana-2", ref_urls: list | None = None) -> dict:
    """Submits + polls + downloads a 16:9 thumbnail image. Reuses the same KIE image
    pipeline as scene generation, just with thumbnail-specific dimensions/prompt."""
    # Lazy-imports: KIE pipeline functions live in dashboard.py
    import time, urllib.request
    from dashboard import _kie_submit_image, kie_key, KIE_API

    full_prompt = prompt.strip() + "\n\n" + master_style.strip()
    try:
        task_id = _kie_submit_image(full_prompt, model=model, ref_urls=ref_urls)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    print(f"  [Thumbnail] Task {task_id} läuft …", flush=True)
    poll_url  = f"{KIE_API}/recordInfo?taskId={task_id}"
    poll_hdrs = {"Authorization": f"Bearer {kie_key()}"}
    for poll_i in range(50):
        time.sleep(3)
        try:
            with urllib.request.urlopen(urllib.request.Request(poll_url, headers=poll_hdrs), timeout=15) as r2:
                info = json.load(r2).get("data", {})
        except Exception as e:
            print(f"  [Thumbnail] Poll-Fehler: {e}", flush=True); continue
        state = info.get("state", "")
        if state != "waiting" or poll_i % 5 == 0:
            print(f"  [Thumbnail] {state}", flush=True)
        if state == "success":
            try:    urls = json.loads(info.get("resultJson", "{}")).get("resultUrls", [])
            except: urls = []
            if not urls: return {"ok": False, "error": "KIE: kein Bild in resultUrls"}
            try:
                dl_req = urllib.request.Request(urls[0],
                    headers={"Referer": "https://kie.ai/", "User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(dl_req, timeout=60) as img_r:
                    open(out_path, "wb").write(img_r.read())
            except Exception as e:
                return {"ok": False, "error": f"Download fehlgeschlagen: {e}"}
            return {"ok": True, "file": os.path.basename(out_path), "source_url": urls[0]}
        if state == "fail":
            return {"ok": False, "error": f"KIE fehlgeschlagen: {info.get('failMsg','unbekannt')}"}
    return {"ok": False, "error": "KIE Timeout (>150s)"}
