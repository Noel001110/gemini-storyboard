"""engine.prompts — Bild-Prompt-Komposition + Character-Sheet-Pipeline + LLM-Bild-Generierung.

Enthält (Phase M.5 + M.6, 2026-07-07):
    Konstanten:
        IMAGE_PROMPT_CHUNK_SIZE, IMAGE_PROMPT_MIN_LEN
    Funktionen:
        _build_image_prompt          — Bild-Prompt zusammensetzen (Scene + Char-Refs + Master)
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
    "SHORTS_SCRIPT_SYSTEM",
    "HOOK_PROMPT_ADDITION",  # Phase L
    "_build_image_prompt",
    "load_char_refs", "analyze_char_image", "gen_charsheet",
    "_anonymized_words", "_validate_image_prompt_entry",
    "_image_prompt_chunk", "_image_prompt_single_retry",
    "visual_prompts",
    "generate_script", "generate_titles",
    "generate_short_scripts", "assign_short_scene_images",
    "make_thumbnail_prompt", "gen_thumbnail_image",
    "THUMBNAIL_TEXT_SYSTEM", "make_thumbnail_text",
    "composite_thumbnail_text",
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
concrete_entity: "char_target"
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


def _normalize_concrete_entity(raw) -> str:
    """Erzwingt EINE saubere Entity-ID. Der Prompt verlangt das zwar, aber das Modell hält
    sich nicht daran — Verlass darauf ist der Grund, warum ein 114-Szenen-Plan
    59 verschiedene `char_`-Schreibweisen für vier Personen enthielt:

        "char_protagonist, smartphone"       -> char_protagonist
        "char_protagonist (older)"           -> char_protagonist
        "char_protagonist_elderly"           -> char_protagonist
        "char_investor, char_coworker"       -> char_investor   (erste Person gewinnt)
        "smartphone"                         -> smartphone      (unverändert, kein Charakter)

    _resolve_entity_ref vergleicht `concrete_entity` als EXAKTEN String. Jede Variante gilt
    dort als eigene Person — der Protagonist bekäme in fast jeder Szene ein neues Gesicht.
    Deshalb wird hier hart normalisiert, statt dem Prompt zu vertrauen.

    Regel: Steht IRGENDWO in der Aufzählung ein `char_`-Token, gewinnt das erste davon —
    eine Person im Bild braucht ihren Identitäts-Anker dringender als ein Objekt seine ID.
    Ohne `char_`-Token bleibt der erste Eintrag stehen.
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if not parts:
        return ""

    def _clean(tok: str) -> str:
        tok = re.sub(r"\([^)]*\)", "", tok).strip()          # "(older)", "(anonymized)" weg
        tok = re.sub(r"\s+", "_", tok)                        # "vintage red convertible" -> ..._...
        return tok.strip("_").lower()

    chars = [_clean(p) for p in parts if _clean(p).startswith("char_")]
    if chars:
        winner = chars[0]
        # Alters-/Zustands-Suffixe zusammenführen: char_protagonist_elderly == char_protagonist
        for suffix in ("_elderly", "_older", "_young", "_younger", "_old", "_adult", "_child"):
            if winner.endswith(suffix) and len(winner) > len("char_") + len(suffix):
                winner = winner[: -len(suffix)]
                break
        return winner
    return _clean(parts[0])


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
                         analysis_ctx: str, chunk_phases: list | None = None,
                         valid_entity_ids: list | None = None,
                         brand_vibe: str | None = None) -> list:
    """One LLM call for a small chunk of scenes (still images). Accepts optional `brand_vibe`."""
    from dashboard import post_gemini_native

    if chunk_phases is None:
        chunk_phases = [None] * len(chunk_beats)
    numbered = "\n".join(
        f"{chunk_offset+i+1}. [Phase: {p}] {t}" if p else f"{chunk_offset+i+1}. {t}"
        for i, (t, p) in enumerate(zip(chunk_beats, chunk_phases))
    )

    _image_chunk_schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "scene": {"type": "integer"},
                "core_statement": {"type": "string"},
                "concrete_entity": {"type": "string"},
                "secondary_entity": (
                    {"type": "string", "enum": valid_entity_ids + [""]}
                    if valid_entity_ids else {"type": "string"}
                ),
                "callback_check": {"type": "string"},
                "forbidden_visuals_check": {"type": "string"},
                "visual_subversion": {"type": "string"},
                "character_consistency": {"type": "string"},
                "line_specific_anchor": {"type": "string"},
                "visual_spike": {"type": "string"},
                "image_prompt": {"type": "string", "minLength": 50},
            },
            "required": ["scene", "core_statement", "concrete_entity",
                         "callback_check", "forbidden_visuals_check",
                         "visual_subversion", "character_consistency",
                         "line_specific_anchor", "visual_spike", "image_prompt"],
        },
    }

    vibe_text = ""
    if brand_vibe or master_prompt:
        vibe_text = "\n\nBRAND TONE & AESTHETIC DIRECTIVE:\n"
        if brand_vibe:
            vibe_text += f"- VIBE / SOUL: {brand_vibe}\n"
        if master_prompt:
            vibe_text += f"- VISUAL MASTER PROMPT (MUST STRICTLY OBEY): {master_prompt}\n"
        vibe_text += "Enforce this overarching vibe, world-building metaphor, and visual style strictly in every image prompt!"

    instr = f"""\
You are a master visual director turning narration into striking, high-retention still images for a YouTube documentary. You receive a structural ANALYSIS of the full script and a CHUNK of consecutive narrator lines. Work through each line using the forced fields below.
{vibe_text}

LINE-SPECIFIC ILLUSTRATION & ANTI-GENERIC RULE:
Every image must visually illustrate what the narrator is saying AT THIS MOMENT — not generic stock atmosphere. If you can imagine the image appearing in another scene without anyone noticing, your image has failed.

METAPHOR + ANCHOR RULE:
The visual subversion MUST ALWAYS retain the literal anchor object (e.g. if the line mentions money or interest rates, a physical currency symbol, vault door, or glowing ledger MUST remain visible in the scene). The subversion enhances the concept — it must NEVER obscure the core subject matter into unrecognisable abstraction.

ANALYSIS (entities, locations, symbols, emotional arc, callbacks — extracted from the FULL script):
{analysis_ctx}

PHASE STYLING & EMOTIONAL ARC:
- OPENING:       slow, deliberate composition; establish setting; character shows VULNERABILITY, ISOLATION, or EXHAUSTION (not an all-powerful predator yet!).
- RISING_ACTION: building tension; tighter framing; character calculating, moving through traps.
- CLIMAX:        maximum visual impact; high contrast; dynamic angle; character asserting CONTROL/DOMINANCE.
- RESOLUTION:    wind-down; wider framing; contemplative stillness.

{_IMAGE_PROMPT_FEWSHOT}

For EACH line in the chunk below, produce an object with ALL of these fields, in order:
{{
  "scene": N,
  "core_statement": "What is this line actually claiming/showing? One sentence.",
  "concrete_entity": "EXACTLY ONE entity id from ANALYSIS. Single id format (e.g. 'char_01').",
  "secondary_entity": "Second character id if two characters interact in this scene, else ''.",
  "callback_check": "Recurring element from ANALYSIS.callbacks, or 'none'.",
  "forbidden_visuals_check": "Identify the top 1-2 stock clichés for this topic (e.g., piggy bank, generic handshake, smiling suit man, rising green line) and FORBID them explicitly.",
  "visual_subversion": "Identify the cliché (e.g., a piggy bank). FORBID IT. Instead, create a brutal metaphor strictly using the anatomy/rules of the VISUAL MASTER PROMPT (e.g. if the style is stick-figure, show the character violently welding a black-line cage). Make the POSES extreme.",
  "character_consistency": "Maintain facial identity anchor. Reflect EMOTIONAL ARC (vulnerable/exhausted early on vs calculating/predatory later).",
  "line_specific_anchor": "The ONE specific visual detail that ONLY this narration introduces. 1-2 sentences.",
  "visual_spike": "If this scene is at a major transition (e.g. ~30%, ~60%, ~90% or CLIMAX), specify a RADICAL CAMERA/Framing BREAK (e.g. 'Extreme low-angle macro view', 'Tactical blueprint overlay look', 'High-contrast silhouette spike') to break visual monotony. Else 'standard'.",
  "image_prompt": "The final image text. MUST describe a physical interaction using the exact anatomy defined in the VISUAL MASTER PROMPT. Rules: (1) Use ACTION VERBS ('slamming', 'shielding', 'tethering'). (2) Anchor objects must match the style (e.g. flat outlined icon if minimalist). (3) VISUAL SPIKE: If this is a spike scene, request a Radical Framing Change (e.g. extreme close-up of a hand crushing gears). (4) NO art-style words (colors, camera types - these are applied by the master prompt). Minimum {IMAGE_PROMPT_MIN_LEN} characters."
}}

HARD RULE: image_prompt MUST describe action, setting, and concrete objects. No generic mood words alone.

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


def _image_prompt_single_retry(beat_text: str, beat_i: int, total: int, analysis_ctx: str,
                                valid_entity_ids: list | None = None) -> dict:
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
            result = _image_prompt_chunk([beat_text], beat_i, total, analysis_ctx, None, valid_entity_ids)
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


def visual_prompts(scenes, analysis=None, brand_vibe: str | None = None):
    """Generate all still-image prompts, chunked (not all-in-one) with forced intermediate
    reasoning fields, brand_vibe tone directive, visual subversion, and visual spikes.
    """
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
    char_ids = [str(c["id"]) for c in (analysis or {}).get("characters", []) if c.get("id")]

    def _fetch_image_chunk(chunk, chunk_offset, chunk_phases=None):
        try:
            return _image_prompt_chunk(chunk, chunk_offset, total, analysis_ctx, chunk_phases, char_ids, brand_vibe)
        except Exception as e:
            if len(chunk) <= 1:
                last_err = e
                for attempt in range(2, 4):
                    try:
                        return _image_prompt_chunk(chunk, chunk_offset, total, analysis_ctx, chunk_phases, char_ids, brand_vibe)
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
                entry = _image_prompt_single_retry(beats[beat_i], beat_i, total, analysis_ctx, char_ids)
            secondary_raw = entry.get("secondary_entity")
            secondary = _normalize_concrete_entity(secondary_raw) if secondary_raw else ""
            prompts.append({
                "prompt": str(entry.get("image_prompt") or f"Scene illustrating: {beats[beat_i][:80]}."),
                "concrete_entity": _normalize_concrete_entity(entry.get("concrete_entity")),
                # Zweiter Charakter in derselben Szene (Fix Ursache 4, siehe Schema oben) —
                # nur gefüllt wenn secondary_entity eine eigene char_-ID ist, nicht dieselbe
                # wie concrete_entity (sonst würde dieselbe Person doppelt referenziert).
                "secondary_entity": secondary if secondary != _normalize_concrete_entity(entry.get("concrete_entity")) else "",
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
    # Juli 2026 (User-Report "ab Szene 90 stimmt die Referenz nicht mehr"): Beim Debuggen
    # zeigte sich, dass dieser Filter für das char_NN-Schema NIE matchte und der Text-
    # Steckbrief + die Konfliktregel damit in KEINEN einzigen Prompt kamen (nur das
    # ReferenzBILD ging über einen anderen Pfad mit). Ursache: entity="char_01" wurde auf
    # "01" gestrippt, das Charsheet trägt aber safe="char_01" — "01" == "char_01" ist nie
    # wahr. Deshalb jetzt gegen BEIDE Formen vergleichen: die volle id UND die gestrippte.
    entity_full = (entity or "").strip().lower()
    entity_key = entity_full[5:] if entity_full.startswith("char_") else entity_full
    if not entity_full:
        return []
    out = []
    for cr in char_refs:
        if not _is_valid_char_description(cr.get("description", "")):
            continue
        safe = (cr.get("safe") or "").lower()
        if safe and (safe == entity_full or safe == entity_key):
            out.append(cr)
    return out


def _build_image_prompt(scene_prompt, master, char_refs, phase="", is_hook=False, entity="",
                         has_style_refs=False):
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

    `has_style_refs` (Juli 2026, User-Report "neues Video zeigt Charakter aus altem Video
    ohne dass die Szene überhaupt eine Person nennt"): die kanalweiten Style-Referenzbilder
    werden als reine Bild-Referenz (nicht über diese Funktion) an KIE angehängt, damit
    Linienführung/Palette/Rendertechnik konsistent bleiben — siehe dashboard.py,
    `get_channel_style_refs` + `use_style_ref`. Diese Bilder zeigen aber selbst konkrete
    Charaktere (die Vision-Beschreibung in `style_ref.json` nennt Haarfarbe, Kleidung etc.),
    und ohne Text-Instruktion behandelt KIE sie wie jede andere Referenz — es übernimmt
    das abgebildete Motiv (Gesicht, Kleidung), nicht nur den Stil. Das passiert genau in
    Szenen OHNE eigenen Charakter-Anker (`concrete_entity` ist ein Symbol/Ort, kein
    `char_`-Eintrag) — dort gibt es sonst keinerlei Identitäts-Instruktion, die dagegenhält,
    und das erfundene Gesicht variiert von Bild zu Bild (mal mit Nase, mal ohne). Wenn
    `has_style_refs=True`, wird deshalb IMMER (auch ohne passenden Charakter-Ref) eine
    Klarstellung angehängt, dass die Style-Referenzbilder nur die Rendertechnik zeigen.
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
        # Juli 2026 (User-Report "ab Szene 90 stimmt die Referenz nicht mehr"): Das Skript
        # ließ die Figuren um ~40 Jahre altern (Prompts ab dort: "65-year-old, grey hair").
        # Die alte, harte Regel ("reference image wins") befahl dem Modell, das JUNGE Charsheet
        # gegen die AUSDRÜCKLICHE Alters-Beschreibung durchzusetzen — zwei widersprüchliche
        # Befehle, Ergebnis Matsch. Deshalb jetzt getrennt: die IDENTITÄT (Gesichtszüge, Statur)
        # bleibt hart an die Referenz gebunden — das war der Jake-Fix gegen STILLE Erfindung.
        # Aber eine im Szenentext EXPLIZIT genannte Alters-Änderung (Haarfarbe, Alter, Kleidung
        # eines gealterten/verjüngten Charakters) darf die Referenz überschreiben. Nur was der
        # Szenentext NICHT erwähnt, wird aus der Referenz übernommen.
        char_hint += ("\n\nIMPORTANT — how to reconcile this design with the scene text:\n"
                      "- The character's core FACIAL IDENTITY (face shape, features, ethnicity, "
                      "build) always comes from the design/reference above. Never silently "
                      "invent a different-looking person.\n"
                      "- BUT if the scene description ABOVE explicitly states a different age, "
                      "hair colour, or outfit for this character (e.g. 'now 65 with grey hair', "
                      "'a younger version', 'wearing a suit'), FOLLOW THE SCENE — it is the same "
                      "person shown at a different point in time. Keep the face recognisably the "
                      "same, but age it / recolour the hair / change the clothes as the scene says.\n"
                      "- For anything the scene text does NOT mention, use the reference design.")
    if has_style_refs:
        char_hint += (
            "\n\nSTYLE REFERENCE ONLY: the final reference image(s) in this set exist "
            "SOLELY to demonstrate the target rendering technique — line weight, flat "
            "color palette, outline style. Render EXCLUSIVELY the subject, character(s) "
            "and composition described in the scene text above, in that technique. If "
            "the scene text above does not describe a person, draw no person at all — "
            "the people shown in those reference images are not part of this scene.")
    return scene_prompt + char_hint + "\n\n" + master


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
    # Lazy-imports: ch_sheets + gen_image + ch_master live in dashboard.py
    from dashboard import ch_sheets, gen_image, ch_master
    # Audit Juli 2026 (Bereich 2, "Charsheets sehen semi-realistisch aus, fertige Bilder
    # aber wieder korrekt im Kanal-Stil"): dieser Prompt hardcodete bisher "polished,
    # semi-realistic style with full shading and natural lighting" + eine feste
    # Studio-Hintergrundfarbe -- das kämpfte aktiv gegen den Kanal-Master (z.B. 2D-Flat),
    # der bisher AUSSERDEM nie angehängt wurde (siehe unten, `gen_image(prompt, "", ...)`
    # war der zweite Teil des Bugs). Jetzt: nur noch Pose/Layout-Anweisungen hier, der
    # Stil kommt ausschließlich aus dem Kanal-Master + Style-Ref-Bild — exakt das Muster,
    # das /api/gen_style_ref schon immer richtig gemacht hat.
    prompt = (
        f"CHARACTER REFERENCE SHEET for '{name}' — designed for character consistency "
        f"across multiple scenes.\n\n"
        f"Show the character in 5 different poses on a single horizontal row, on a simple, "
        f"uncluttered background so every pose is clearly visible and clearly separated.\n\n"
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
        f"- Each pose uses the exact same art style, line weight and rendering technique "
        f"as specified in the style guide below — never deviate from it\n"
        f"- No text labels, no captions, no annotations on the image — pure visual reference\n\n"
        # Juli 2026 (User-Report: "Charsheets für zwei verschiedene Charaktere sind 2x identisch
        # und sehen aus wie die Figur aus dem Style-Ref"): Das Style-Referenzbild des Kanals wird
        # als BILD-Referenz an KIE geschickt (siehe unten) — und Bildmodelle übernehmen aus einem
        # Referenzbild vor allem die IDENTITÄT, nicht bloß den Strich. Zeigt der Style-Ref konkrete
        # Figuren (hier: drei Strichmännchen, das mittlere braunhaarig im blauen Shirt), bekommt
        # JEDER neu erzeugte Charakter genau dieses Gesicht und diese Kleidung, egal was in seiner
        # Beschreibung steht. Verschärft wurde das durch "colour palette" in der Zeile oben — das
        # las das Modell als Aufforderung, auch die Kleidungsfarbe zu übernehmen.
        f"THE ATTACHED REFERENCE IMAGE IS THE HOUSE STYLE — MATCH IT EXACTLY:\n"
        f"Same line weight, same limb construction, same flat colours, same absence of shading, "
        f"same head-to-body proportions, same facial simplification. The new character must look "
        f"like it was drawn by the same artist and could stand next to those figures.\n"
        f"It shows a DIFFERENT, unrelated person. Take the STYLE from it, but take the hair "
        f"colour and clothing colour ONLY from the 'Character design specifications' above — never "
        f"default to the reference's colours."
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
        from dashboard import ch_dir, get_channel_style_refs
        style_ref_urls = [u for u in get_channel_style_refs(cid) if u.startswith(("http://", "https://"))]
        if not style_ref_urls:
            # Legacy-Fallback: lokales PNG (noch nicht hochgeladene/URL-lose Referenz).
            # Gilt nur für den EINEN Legacy-Slot -- Multi-Slots liegen immer als URL vor.
            sp = os.path.join(ch_dir(cid), "style_ref.png")
            if os.path.exists(sp):
                style_ref_urls = [_local_png_to_data_url(sp)]
        if style_ref_urls:
            char_refs = [{
                "name": "style_ref",
                "description": "Channel-wide style reference (line weight, palette, render style)",
                "image_data_url": url,
                "safe": "style_ref",
            } for url in style_ref_urls]
    except Exception as e:
        print(f"  [Charsheet] Style-Ref konnte nicht geladen werden: {e}", flush=True)
        char_refs = None

    # Audit Juli 2026 (Bereich 2, Kernbug): Kanal-Master anhängen statt "" -- ohne das
    # erreichte der Kanal-Stil (z.B. 2D-Flat, Strichmännchen) das Charsheet NIE, egal
    # was der Style-Ref-Bild-Anker zeigte. Gleiches Muster wie /api/gen_style_ref.
    try:
        master = open(ch_master(cid)).read().strip()
    except Exception:
        master = ""

    res = gen_image(prompt, master, tmp, char_refs=char_refs)
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
- Short sentences, spoken cadence. ~150-190 words per minute (documentary voiceover pace,
  not audiobook-slow) — corrected range (2026 retention research + real measured narration
  speed on this channel's own finished videos; the old "120-150" figure undershot reality).
- One clear idea per paragraph; each paragraph ~3-6 sentences.

THE OPENING (first ~30 seconds — the single highest-leverage part of the whole script,
this is where most viewer drop-off happens):
- NEVER start with a self-introduction, a channel intro, or a "today we're looking at…"
  framing. Open on the result, a bold claim, or the problem itself — mid-scene, mid-tension.
- The opening must do THREE things, in order, within the first few sentences:
  1. VALIDATE THE CLICK — confirm the viewer landed in the right place for what the
     title/thumbnail promised.
  2. RAISE THE STAKES — make clear why this matters right now, concretely, not abstractly.
  3. OPEN THE FIRST CURIOSITY LOOP — hint at the central payoff/reveal without giving it
     away. Never state the video's full thesis or conclusion in the opening — that closes
     the loop before the investigation even starts.

PATTERN INTERRUPTS (every ~60-90 seconds of spoken content, i.e. roughly every 2-4
paragraphs at this pace): insert a tonal or informational shift — a new fact, a reversal, a
"but here's what nobody noticed" turn, a mini-payoff that resolves a small open question
while immediately opening a bigger one. A script that runs flat for minutes without one of
these loses the viewer even if each individual sentence is well written.

- Build tension deliberately across the whole arc, not just the opening.
- Chapter/section transitions are themselves open loops — end a chapter on a question or
  an unresolved implication, never on a settled summary; that's what pulls the viewer into
  the next chapter instead of letting them feel a natural stopping point.
- End the whole script with an OPEN QUESTION, not a summary. The last paragraph should
  leave the viewer thinking, not wrap things up.
- Emotional arc: opens with tension or curiosity, deepens through investigation, lands on
  a reflective beat.
- NEVER invent specific facts, numbers, dates, or names not present in the input.
- If the input is sparse, write a short but complete script — never pad with filler.

OUTPUT FORMAT:
- Plain text, paragraphs separated by blank lines.
- DO NOT include any preamble, title, or meta-commentary.
- DO NOT label scenes, acts, or chapters as such (no "Chapter 1:", no "Act I").
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


# ---------- Long-form retention rewrite (Blueprint-Review 2026-07-27) ----------
# Anders als SCRIPT_SYSTEM/generate_script (Rohnotizen -> neues Skript) nimmt diese
# Funktion ein BEREITS FERTIGES Skript (egal ob über generate_script erzeugt oder
# extern/handgeschrieben direkt in die #script-Textarea gepastet -- letzteres läuft
# nie durch SCRIPT_SYSTEMs Hook-/Pattern-Interrupt-Regeln) und presst es in die
# Long-Form-Retention-Struktur, OHNE Fakten zu verändern. Läuft seit Juli 2026
# automatisch als erster Teilschritt jedes Plan-Laufs (workers/plan.py::run(),
# kein manueller Button mehr) -- ein Hash-Guard dort verhindert, dass ein Re-Plan
# mit unverändertem, bereits umgeschriebenem Text den Rewrite grundlos erneut
# auslöst (siehe dessen Docstring zur Re-Plan-Stale-Preservation).

LONGFORM_RETENTION_REWRITE_SYSTEM = """\
You REWRITE an existing, finished narration script to fit a proven long-form
YouTube retention structure. You do NOT invent new facts, numbers, dates, names, or
claims — every fact, argument, and number in the input MUST survive in the output.
You may reorder, restructure, add transitional/framing sentences, and rewrite the
opening/closing, but the factual content stays completely intact.

VISUAL ANCHORING (grounds the downstream image-generation pipeline, which turns each
line into a still image and rewards concrete, specific content over abstraction):
- Replace abstract metaphors with concrete, physical actions or objects the person the
  script addresses (or its narrator) visibly does, sees, or interacts with. Instead of
  "financial pressure rose," write something closer to "you stare at a stack of unpaid
  bills piling higher than the table." Write this in the same natural narration voice
  as the rest of the script -- never insert internal system labels or IDs into the
  narration text.
- Where the script's own content supports it, let ONE concrete object or image recur
  2-4 times across the script as a physical stand-in for the central idea (a shrinking
  piggy bank, a locked door, a leaking bucket) -- this illustrates something the script
  already argues, it is not a new invented fact. Only do this if it fits naturally; do
  not force a recurring object onto material that doesn't support one.
- METAPHOR CONSISTENCY: once the opening establishes a central physical image (a
  vault, a tree, a wheel, a monster), that is now the ONE visual world for the rest
  of the script -- do not introduce a second, unrelated image for the same idea
  partway through (e.g. starting with a vault and then switching to a sinking ship).
  When the argument turns abstract (opportunity cost, risk versus stability, a
  technical distinction), translate it back into the SAME established image instead
  of reverting to explanatory/abstract language or reaching for a new metaphor --
  one image, developed and paid off, beats several disconnected ones. This applies
  EVEN WHEN the input script itself already phrases an argument through a different,
  competing metaphor (e.g. a lifeboat/ship comparison sitting in the middle of an
  input that opens on a vault) -- the fact/argument that sentence carries must
  survive, but rewrite its vehicle into your established image rather than
  preserving the input's competing metaphor just because it was already there.
  Keeping a source sentence's claim intact is not the same as keeping its imagery.
- Prefer vivid, concrete, sensory language over abstract or clinical phrasing,
  especially at high-tension moments -- concrete language produces stronger images
  downstream than abstraction does.

STACKED COLD OPEN (the first 4-6 sentences), in this order:
1. IN MEDIAS RES — open on a consequence or a moment, not on backstory or context.
   Start where the tension already exists.
2. CONTRARIAN CLAIM / CURIOSITY GAP — a specific, concrete claim that creates a
   genuine information gap. Never vague ("you won't believe what happened") —
   always specific enough to be falsifiable.
3. STAKES / IDENTITY DEBT — make explicit who exactly this affects and what
   they concretely stand to lose. Precision beats broad appeal.
4. Weave in ONE concrete number (currency, percent, count) from the script's
   own content early in the opening, if one exists. If the input has NO
   number anywhere, use a concrete time or quantity phrase already present in
   the script instead ("for 5 years", "every single Monday", "every euro")
   to ground the opening — do NOT invent a number, a statistic, or a study
   ("research shows 78%...") to fill the gap. A fabricated number is exactly
   as unacceptable as a fabricated name or date under the no-invented-facts
   rule above, not a style choice.
Never resolve the core outcome/thesis in the opening — that closes the loop
before the investigation starts.

RE-ENGAGEMENT RHYTHM (throughout the whole script, not just the opening):
roughly every 2-3 minutes of spoken content (~300-450 words at documentary
pace), ensure there is a pattern-interrupt beat — a reversal, a new fact, a
"but here's what nobody noticed" turn, a mini-payoff that resolves a small
open question while immediately opening a bigger one. Every chapter/section
transition must end on an open question or unresolved implication, never a
settled summary, followed by a one-sentence forward-pull bridge into the next
section that makes clear why it matters.

MACROSTRUCTURE — choose whichever fits the material, do not force both:
- ESCALATING STAKES ("wait, it gets worse"): for mistake/collapse narratives —
  each new revelation deepens the problem, widens who's affected, or raises
  the cost. Never start at maximum stakes (nothing left to escalate to).
- KISHOTENKETSU (Ki: setup -> Sho: development -> Ten: unexpected twist/
  reframe -> Ketsu: resolution/harmonization): for insight/psychology
  narratives where a surprising reframing IS the point, not a conflict.

ENDING: leave at least one loop genuinely, deliberately unresolved. Never end
on a full recap or a "so what we learned today is..." wrap-up. End on a
question or an open implication.
- FORWARD INFORMATION GAP: prefer closing on a line that acknowledges a next,
  concrete piece of the puzzle exists without resolving it here -- e.g. "if
  you stop feeding the monster, you still need to know where that money goes
  instead. But that's a story for another time." This is stronger than a
  generic "subscribe" call-to-action because it opens real curiosity about
  something specific and still unanswered, not a bare ask. Never invent or
  reference a specific video title or promise -- no concrete next video is
  known at this stage -- keep the tease generic ("next time", "another day",
  "a different story").

LENGTH AND FORMAT: keep approximately the same overall length as the input —
downstream scene timing assumes a similar word/duration budget. Match the
input's chapter-heading convention if it uses one (## headings), plain text
otherwise, paragraphs separated by blank lines. No preamble, no meta-
commentary, no "Chapter 1:"/"Act I" labels.
"""


def rewrite_script_for_retention(full_script: str, lang: str = "en") -> str:
    """Presses an already-finished narration script into the long-form retention
    structure (stacked cold-open, 2-3min re-engagement rhythm, escalating-stakes/
    Kishotenketsu macrostructure, deliberately open ending) without altering its
    facts. Sibling to generate_script() but transforms an EXISTING script instead
    of drafting a new one from raw notes -- same error contract (raises on LLM
    failure, caller/route degrades to 500, no empty-string swallow)."""
    lang_instr = (
        "Write the rewritten script in German (natural spoken German, not formal)."
        if lang == "de"
        else "Write the rewritten script in English (clear, neutral international English)."
    )
    user_msg = (
        f"{lang_instr}\n\n"
        f"Here is the finished narration script to rewrite into the retention "
        f"structure above. Preserve every fact and argument, restructure the "
        f"narrative architecture:\n\n{full_script}"
    )
    from dashboard import post_kie_text  # lazy
    msgs = [
        {"role": "system", "content": LONGFORM_RETENTION_REWRITE_SYSTEM},
        {"role": "user", "content": user_msg},
    ]
    return post_kie_text(msgs, temp=0.7)


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

FORMULAS TO DRAW FROM (mix, don't just pick one every time — data-backed CTR
multipliers from 2026 title-performance research in parentheses):
- Curiosity gap: hint at a shocking fact/connection without revealing it
- Number-based: "[Number] [Things] That [Concrete Result]"
- Loss-aversion / FOMO: what the viewer doesn't know yet, what they're missing
- Compression: pair a large payoff against a small time/effort cost, e.g.
  "[Big Result] in [Surprisingly Short Time/Effort]" (~80x)
- Blueprint: signal a replicable, concrete system rather than a vague idea, e.g.
  "The Blueprint to [Result] from Day 1" / "My Full [System] for [Result]" (~100x)
- Identity: challenge the viewer's self-image directly, second person, imperative
  or declarative, e.g. "You've Been [Doing X Wrong]. Here's [The Fix]." (~100x)
- Authority: LEAD with the credential/role, don't bury it, e.g.
  "[ROLE] EXPLAINS: [Surprising Claim]" (~61x) — stronger than a generic
  "credible framing" tacked on at the end
- Novelty: emphasize that this is new/different from what's already out there,
  e.g. "The NEW Way to [Result]" (~11x)

ANTI-PATTERN — DO NOT DO THIS:
- Never write a single explanatory sentence that states the video's full thesis or
  conclusion up front, e.g. "How [X] causes [Y]" or "Why [X] leads to [Y]" as a
  complete, self-contained statement. That closes the curiosity loop before the
  click ever happens — there's nothing left to find out. Every title must leave an
  open loop (a question, a challenge, a missing piece) that only watching resolves.
- A bracket/parenthetical suffix ("(And How to Stop)", "(Step-by-Step)",
  "[Full Breakdown]") is a proven CTR booster when it fits naturally — use one on
  at least one or two of the options.

RULES:
- 55-60 characters total (titles longer than this get truncated on mobile — this is
  a hard constraint, not a suggestion)
- Every claim in the title must be directly supported by the script content given
- No emoji, no ALL CAPS spam, no exclamation-mark stacking
- Return options in the language explicitly specified in the user message, not
  whatever language the script happens to be written in (the script argument is
  sometimes a rough, differently-languaged idea/fallback text, not necessarily the
  video's actual output language)
"""


def generate_titles(full_script: str, n: int = 5, lang: str = "en") -> list:
    """Generate N candidate clickbait-but-honest titles from the full script.

    Juli 2026 (User-Report "warum werden deutsche Titel generiert, obwohl alles
    andere Englisch ist"): TITLE_SYSTEM ließ das Modell die Sprache aus full_script
    selbst erraten -- und /api/generate_titles fällt auf meta["idea"] zurück,
    solange noch kein Plan existiert. Die Idee-Box hat keinen Sprach-Schalter, wird
    also oft auf Deutsch getippt, obwohl das eigentliche Skript/Video Englisch sein
    soll -- das Modell "erbte" dann die Sprache des Fallback-Texts. Jetzt wie
    generate_script()/rewrite_script_for_retention(): expliziter lang-Parameter
    statt Sprache aus dem Input zu erraten.
    """
    from dashboard import post_gemini_native  # lazy
    lang_instr = (
        "Write the titles in German (natural spoken German, not formal)."
        if lang == "de"
        else "Write the titles in English (clear, neutral international English)."
    )
    user_msg = (
        f"{lang_instr}\n\n"
        f"Generate {n} distinct YouTube title options for this script, using the "
        f"formulas above. Return ONLY a JSON array of {n} strings, nothing else.\n\n"
        f"SCRIPT:\n{full_script.strip()[:6000]}"
    )
    try:
        txt = post_gemini_native([
            {"role": "system", "content": TITLE_SYSTEM},
            {"role": "user", "content": user_msg},
        ], json_mode=True, temp=0.9, thinking_level="low")
        arr = json.loads(txt)
        if isinstance(arr, dict):
            for v in arr.values():
                if isinstance(v, list): arr = v; break
        if isinstance(arr, list):
            return [str(t).strip() for t in arr][:n]
    except Exception as e:
        print(f"  [Title] Fehler: {e}", flush=True)
    return []


# ---------- Hook-first Short scripts (Struktur-/Schnitt-Review Juli 2026) ----------
# Ersetzt die frühere "Longform in sequenzielle Parts zerschneiden"-Pipeline (bestätigter
# Anti-Pattern: Cold-Feed-Zuschauer ohne Kontext, kein Hook, "Part N" signalisiert
# fehlenden Kontext). Jeder Short ist jetzt ein EIGENSTÄNDIGES Mini-Skript mit eigenem
# Voiceover -- ein Longform-Skript liefert per EINEM LLM-Call N unabhängige Aufhänger.

SHORTS_SCRIPT_SYSTEM = """\
You are a short-form (YouTube Shorts / TikTok / Reels) script writer. You take a finished
long-form documentary script and write N completely INDEPENDENT short-form voiceover
scripts that promote it -- each one is its OWN self-contained mini-story, NOT a clip or
excerpt of the long-form script and NOT a sequential "Part N" of it. A cold viewer who has
never seen the long-form video and never will must get a complete, satisfying loop from
ANY ONE of these alone.

STRUCTURE (2026 short-form retention research -- this is a hard constraint, not a style
suggestion, because the short-form feed swipes away within 1-3 seconds if this fails).
Five beats, in this order, every time:
1. HOOK (first sentence, ~1-3 seconds of spoken audio) -- a question, a surprising stat,
   or a bold claim. Never a greeting, never a setup sentence before the hook lands, never
   "In this video..." framing.
2. PROBLEM / AGITATION (next 1-2 sentences) -- deepen the pain the hook opened: show the
   hidden cost, the trap, why it's worse than it first sounds.
3. TWIST / INSIGHT -- name the actual psychological mechanism that explains WHY this
   happens (e.g. lifestyle creep, hedonic adaptation, Parkinson's Law, loss aversion,
   present bias). Say it in natural spoken language, not a textbook definition dump --
   but the real term must be recognizable in the sentence.
4. PAYOFF -- the concrete fix, framed with a number (the same or a related number to the
   hook's).
5. LOOP-BACK CTA -- a closing line that echoes or rhymes with the opening hook line, so
   the short can replay seamlessly. NEVER a hard CTA card, never "link in bio", never
   "Part 1 of N", never a sequence number of any kind.
- ONE clear idea per short. Do not compress the whole long-form video into one short --
  pick ONE angle, moment, or fact from it and build the entire short around just that.
- MUST include at least one concrete, specific dollar amount or percentage (never a vague
  "a lot of money" -- always the actual figure).
- 55-80 words total (fits the 20-28 second retention sweet spot at documentary spoken
  pace -- tighter than a full explainer, every sentence has to earn its place).
- The N shorts must each use a genuinely DIFFERENT angle/moment/fact from the long-form
  script -- never sequential, never overlapping in what they reveal, never referencing
  "part" or a fixed viewing order relative to each other.
- NEVER invent facts, numbers, or claims not present in the long-form script.

For each short, ALSO provide: a short standalone YouTube title (<=60 characters, same
proven CTR formulas as any strong title -- curiosity gap, bold claim, never a full
explanatory sentence that gives away the ending) and a 1-2 sentence description.
"""


def generate_short_scripts(full_script: str, chosen_title: str, n: int = 5,
                           lang: str = "en") -> list:
    """Generates N independent, self-contained hook-first short scripts that each promote
    the long-form video from a DIFFERENT angle -- not a sequential split of it. One
    response_schema-constrained LLM call for all N at once (same post_gemini_native
    pattern as generate_titles). Returns [] on any failure -- caller must degrade
    gracefully exactly like generate_titles()'s empty-list contract.

    Five-beat structure (Hook/Problem-Agitation/Twist-Insight/Payoff/Loop-CTA) per the
    content-blueprint review (2026-07-26) -- `mechanism`/`number` are separate required
    schema fields (not just prompt instructions) so the beats are enforceable/inspectable
    the same way analyze_script's hook{beat,type,strength} object already is.

    Each item: {"angle", "hook", "mechanism", "number", "script_text", "title", "description"}.
    """
    from dashboard import post_gemini_native  # lazy
    lang_instr = ("Write in German (natural spoken German, not formal)."
                  if lang == "de" else
                  "Write in English (clear, neutral international English).")
    schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "angle": {"type": "string", "description": "1-5 words naming which "
                          "distinct moment/fact of the long-form script this short is built around."},
                "hook": {"type": "string"},
                "mechanism": {"type": "string", "description": "The named psychological "
                              "mechanism used in the Twist/Insight beat (e.g. 'lifestyle "
                              "creep', 'hedonic adaptation', 'Parkinson's Law', 'loss "
                              "aversion')."},
                "number": {"type": "string", "description": "The concrete dollar amount "
                           "or percentage this short is built around."},
                "script_text": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["angle", "hook", "mechanism", "number", "script_text", "title",
                         "description"],
        },
    }
    user_msg = (
        f"{lang_instr}\n\n"
        f"LONG-FORM VIDEO TITLE: {chosen_title}\n\n"
        f"FULL LONG-FORM SCRIPT:\n{full_script.strip()[:6000]}\n\n"
        f"Write {n} independent short-form scripts promoting this video, each built "
        f"around a different angle/moment/fact, following the schema and rules above."
    )
    try:
        txt = post_gemini_native([
            {"role": "system", "content": SHORTS_SCRIPT_SYSTEM},
            {"role": "user", "content": user_msg},
        ], json_mode=True, temp=0.9, response_schema=schema, thinking_level="low")
        arr = json.loads(txt)
        if isinstance(arr, list):
            return arr[:n]
    except Exception as e:
        print(f"  [ShortsScript] Fehler: {e}", flush=True)
    return []


def _keyword_overlap_match(short_text: str, longform_scenes: list,
                            exclude_files: set | None = None) -> int:
    """Deterministischer Fallback (kein API-Call) für assign_short_scene_images: wählt
    die Longform-Szene mit dem größten Wort-Overlap zwischen ihrem `text`/`prompt` und
    dem Short-Szenentext. Nie ein Renderabbruch, falls die LLM-Zuordnung fehlschlägt.
    Sucht NUR unter Szenen mit vorhandenem `file` -- eine fileless Szene als "Match"
    zurückzugeben wäre für den Aufrufer wertlos (kein Bild zum Wiederverwenden da).

    `exclude_files` (Juli 2026, Anti-Wiederholungs-Fix): optionale Menge an Dateien, die
    NICHT als Match zurückkommen sollen (z.B. das Bild der unmittelbaren Vorszene) --
    genutzt von `_dedupe_adjacent_picks()`. Fällt auf die ungefilterte Kandidatenliste
    zurück, falls der Ausschluss ALLE Kandidaten eliminiert (Pool erschöpft, z.B. nur
    1 Bild insgesamt) -- ein Match, das nicht perfekt divers ist, ist besser als gar
    keins."""
    stop = {"the", "a", "an", "of", "to", "in", "on", "and", "was", "is", "for",
            "der", "die", "das", "und", "ist", "war", "ein", "eine", "zu", "im"}
    short_words = {w.lower() for w in re.findall(r"[a-zA-ZäöüßÄÖÜ]{3,}", short_text)} - stop
    all_candidates = [i for i, ls in enumerate(longform_scenes) if ls.get("file")]
    candidates = ([i for i in all_candidates if longform_scenes[i]["file"] not in exclude_files]
                  if exclude_files else all_candidates)
    if not candidates:
        candidates = all_candidates
    if not candidates:
        return -1
    if not short_words:
        return candidates[0]
    best_i, best_score = candidates[0], -1
    for i in candidates:
        ls = longform_scenes[i]
        haystack = f"{ls.get('text', '')} {ls.get('concrete_entity', '')}"
        lf_words = {w.lower() for w in re.findall(r"[a-zA-ZäöüßÄÖÜ]{3,}", haystack)} - stop
        score = len(short_words & lf_words)
        if score > best_score:
            best_i, best_score = i, score
    return best_i


def _dedupe_adjacent_picks(picks: list, scenes: list, longform_scenes: list) -> list:
    """Post-Processing für EINEN Short (Juli 2026, User-Report): verhindert, dass zwei
    DIREKT AUFEINANDERFOLGENDE Szenen dasselbe Longform-Bild bekommen. Grund: jede Szene
    startet ihren Ken-Burns-Zoom/Pan unabhängig von der Vorszene aus einem festen Rezept
    (engine/render.py:_build_motion) -- bei zwei Klammern desselben Bildes hintereinander
    sieht der Schnitt aus wie ein Bug (Bild "springt" beim Schnitt auf seine
    Ausgangsposition zurück). Nur STRIKT ANGRENZENDE Wiederholungen werden ersetzt, nicht
    jede Wiederholung irgendwo im Short -- generelle Bildvielfalt ist nicht das Problem,
    der harte Schnitt zwischen zwei identischen Klammern ist es. Bewusst NACH sowohl der
    LLM-Zuordnung als auch dem Keyword-Fallback angewendet (ein gemeinsamer Guard statt
    Duplizierung in beiden Pfaden)."""
    fixed = list(picks)
    for i in range(1, len(fixed)):
        if not fixed[i] or fixed[i] != fixed[i - 1]:
            continue
        alt_idx = _keyword_overlap_match(scenes[i].get("text", ""), longform_scenes,
                                          exclude_files={fixed[i - 1]})
        if alt_idx is not None and alt_idx >= 0:
            alt_file = longform_scenes[alt_idx].get("file")
            if alt_file and alt_file != fixed[i - 1]:
                fixed[i] = alt_file
        # sonst: kein Alternativ-Bild verfügbar (Pool erschöpft) -- bleibt wie es ist,
        # besser eine seltene Restwiederholung als ein KeyError/None-Bild.
    return fixed


def assign_short_scene_images(shorts_scenes: list, longform_scenes: list) -> list:
    """'Materialien aus dem Hauptvideo wiederverwenden': ordnet jeder Short-Szene das
    best passende bereits existierende Longform-Bild zu, statt neue KIE-Bilder zu
    erzeugen (Regelfall: 0 neue Bildgenerierungen). EIN batched LLM-Call für ALLE
    übergebenen Short-Skripte zusammen (nicht pro Short) -- Kontext sind die echten
    Longform-Szenenfelder `text`/`concrete_entity`/`prompt` aus plan.json (kein
    `core_statement` verfügbar, das existiert nur intern in der Bild-Prompt-Stufe und
    wird nicht persistiert).

    `shorts_scenes`: Liste von Szenenlisten (eine Liste pro generiertem Short-Skript,
    bereits durch segment_by_pacing(profile=PACING_PROFILES["short"]) gelaufen).
    `longform_scenes`: die fertigen Longform-Szenen aus plan.json (brauchen `file`).

    Rückgabe: parallel zu `shorts_scenes` strukturiert -- eine Liste von Dateipfad-Listen
    (oder None pro Szene, falls kein Longform-Bild + kein Fallback-Match existiert ->
    Aufrufer generiert in diesem Ausnahmefall ein frisches Bild). Degradiert bei jedem
    API-Fehler auf den deterministischen Keyword-Overlap-Fallback, NIE ein Renderabbruch.
    """
    lf_indexed = [{"i": i, "text": s.get("text", "")[:200],
                   "concrete_entity": s.get("concrete_entity", ""),
                   "prompt": s.get("prompt", "")[:150]}
                  for i, s in enumerate(longform_scenes) if s.get("file")]
    lf_file_by_i = {i: longform_scenes[i]["file"] for i in range(len(longform_scenes))
                    if longform_scenes[i].get("file")}

    def _fallback() -> list:
        out = []
        for scenes in shorts_scenes:
            picks = []
            for sc in scenes:
                idx = _keyword_overlap_match(sc.get("text", ""), longform_scenes)
                picks.append(lf_file_by_i.get(idx))
            out.append(_dedupe_adjacent_picks(picks, scenes, longform_scenes))
        return out

    if not lf_indexed:
        return _fallback()

    try:
        from dashboard import post_gemini_native  # lazy
        schema = {
            "type": "object",
            "properties": {
                "assignments": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "integer"}},
                }
            },
            "required": ["assignments"],
        }
        shorts_ctx = [[sc.get("text", "") for sc in scenes] for scenes in shorts_scenes]
        user_msg = (
            f"LONG-FORM SCENES (index: text / entity / image content):\n" +
            "\n".join(f"{e['i']}: \"{e['text']}\" | entity={e['concrete_entity']} | "
                      f"image shows: {e['prompt']}" for e in lf_indexed) +
            f"\n\nSHORT-FORM SCRIPTS, each already split into scene lines -- for EACH "
            f"scene of EACH short, pick the long-form scene index whose EXISTING IMAGE "
            f"best illustrates that scene line. Use -1 only if truly nothing fits.\n\n" +
            "\n".join(f"SHORT {si}:\n" + "\n".join(f"  scene {li}: \"{t}\"" for li, t in enumerate(lines))
                      for si, lines in enumerate(shorts_ctx))
        )
        txt = post_gemini_native([
            {"role": "system", "content": "You match short-form video scenes to the "
             "best-fitting existing image from a long-form video's scene list, by "
             "matching what the image actually depicts to what the short-form line "
             "narrates. Return only the JSON object per the schema."},
            {"role": "user", "content": user_msg},
        ], json_mode=True, temp=0.3, response_schema=schema)
        data = json.loads(txt)
        assignments = data.get("assignments", [])
        out = []
        for si, scenes in enumerate(shorts_scenes):
            row = assignments[si] if si < len(assignments) else []
            picks = []
            for li, sc in enumerate(scenes):
                idx = row[li] if li < len(row) else -1
                f = lf_file_by_i.get(idx) if idx is not None and idx >= 0 else None
                if not f:
                    idx = _keyword_overlap_match(sc.get("text", ""), longform_scenes)
                    f = lf_file_by_i.get(idx)
                picks.append(f)
            out.append(_dedupe_adjacent_picks(picks, scenes, longform_scenes))
        return out
    except Exception as e:
        print(f"  [ShortsImageAssign] Fehler: {e} — Keyword-Overlap-Fallback.", flush=True)
        return _fallback()


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
2. FLAT, HIGH-CONTRAST BACKGROUND — a plain dark backdrop behind a light subject, or a
   plain light backdrop behind a dark subject (same flat-color rule as every other
   image in this channel's style — see STYLE CONTEXT). No busy scenery.
3. EXAGGERATED, READABLE EMOTION on the subject if it's a character — shock, alarm,
   intense focus, fear, urgency. Subtle/neutral expressions do not work for thumbnails.
4. MANDATORY PROP: the subject must be holding, looking at, or reacting to ONE concrete
   prop or symbol that visually encodes the video's actual number/core fact — a price
   tag, an oversized receipt, a stack of bills, a shrinking piggy bank, a bank statement.
   A generic shocked face with nothing in the frame to react TO is the single most common
   thumbnail mistake in this niche — never generate that.
5. RULE OF THIRDS: subject placed off-center with clear headroom in the top third of
   the frame — that headroom is where bold title text gets added afterward, so keep it
   free of important detail (no face, no key prop, up there).
6. MUST include one or two bold, bright RED graphic annotations (a directional arrow
   and/or a circle/ring) — simple drawn shapes, not text — pointing at or circling the
   subject or the single most concrete detail/symbol of the hook. This is the classic
   clickbait attention-director, non-negotiable.
7. Do not describe on-image text here — text is composited separately (AI-rendered
   text inside images is unreliable/garbled; drawn arrow/circle shapes are not, which is
   why they belong in the image itself but the text does not).
8. Keep the established character/art style exactly as given in STYLE CONTEXT, but push
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


THUMBNAIL_TEXT_SYSTEM = """\
You write the on-image CLICKBAIT TEXT for a YouTube thumbnail -- 1-3 words MAXIMUM, hard
limit, punchy, provocative, grounded only in what the script actually supports (never
invent a claim). This is NOT the video title (that's written separately) -- it's a short,
shouted fragment that works alongside the image, e.g. a number, a verdict, a single loaded
word or short phrase. PREFER the video's concrete dollar amount or percentage as the text
when it reads well large (e.g. "$1,042,000"), since a specific figure outperforms a vague
verdict word. Examples of the RIGHT length/energy: "$1,000,000?!", "HE QUIT", "BROKE".
Return ONLY the text itself, no quotes, no punctuation beyond what belongs in the phrase
itself, in the language explicitly specified in the user message (not necessarily the
language of the SCRIPT text given below, which may be a differently-languaged fallback).
"""


def make_thumbnail_text(full_script: str, chosen_title: str, lang: str = "en") -> str:
    """Kurzer (2-4 Wörter), provokanter Text fürs Thumbnail-Kompositing (siehe
    composite_thumbnail_text) -- getrennt von make_thumbnail_prompt (Bild) und
    generate_titles (Videotitel), aber dasselbe post_gemini_native-Muster.

    Juli 2026 (gleicher Sprach-Bug wie generate_titles(), siehe dessen Docstring):
    THUMBNAIL_TEXT_SYSTEM ließ die Sprache bisher aus full_script erraten, das ohne
    Plan auf die (oft deutsch getippte) Idee zurückfällt. Expliziter lang-Parameter
    statt Raten."""
    from dashboard import post_gemini_native  # lazy
    lang_instr = ("Write the text in German." if lang == "de"
                  else "Write the text in English.")
    user_msg = f"{lang_instr}\n\nTITLE: {chosen_title}\n\nSCRIPT:\n{full_script.strip()[:3000]}"
    try:
        text = post_gemini_native([
            {"role": "system", "content": THUMBNAIL_TEXT_SYSTEM},
            {"role": "user", "content": user_msg},
        ], temp=0.8).strip().strip('"').strip("'")
        return text or chosen_title[:20]
    except Exception as e:
        print(f"  [Thumbnail] Text-Fehler: {e}", flush=True)
        return chosen_title[:20]


def composite_thumbnail_text(image_path: str, text: str) -> None:
    """Zeichnet den Clickbait-Text GROSS + fett + weiß mit dickem schwarzem Rand ins
    obere Bilddrittel -- Pillow direkt (kein .venv_whisper-Subprocess nötig, Pillow ist
    im Haupt-Environment vorhanden, siehe shorts/cta.py für dasselbe Muster).

    Juli 2026 (User-Report "Text komplett verdeckt, Mitte des Wortes verschluckt"): ein
    erster Anlauf legte das freigestellte Motiv (_extract_subject_mask) HINTER den Text
    zurück, um einen "3D-Layered"-Tiefeneffekt zu erzeugen (Motiv schneidet den Text an,
    wie beim Referenz-Kanal). Drei Iterationen an der erlaubten Überlapp-Fläche brachten
    es nicht zuverlässig genug hin -- ein breiter Kopf/buschige Haare zerstörten immer
    wieder die Wortmitte. User-Entscheidung: Tiefeneffekt fallengelassen, Lesbarkeit hat
    Vorrang. Der dicke schwarze Rand allein reicht für Kontrast über JEDEM Hintergrund
    -- kein Freistellen/Compositing mehr nötig, nur noch ein einfaches Overlay."""
    from PIL import Image, ImageDraw, ImageFont

    canvas = Image.open(image_path).convert("RGB")
    width, height = canvas.size
    draw = ImageDraw.Draw(canvas)

    font_candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]

    def _load_font(size):
        for path in font_candidates:
            if os.path.exists(path):
                return ImageFont.truetype(path, size)
        return ImageFont.load_default(size)

    text = text.upper()
    words = text.split()
    font_size = round(height * 0.17)
    min_font_size = round(height * 0.09)
    lines = [text]
    font = _load_font(font_size)
    # Zu breit? Erst an der Wortgrenze in 2 Zeilen umbrechen (Referenz-Thumbnails wie
    # "THE GREAT DEPRESSION" laufen genauso zweizeilig), erst danach die Schrift
    # verkleinern -- schrumpfen allein macht kurze Wörter unnötig winzig.
    while True:
        font = _load_font(font_size)
        widths = [draw.textbbox((0, 0), ln, font=font)[2] for ln in lines]
        if max(widths) <= width * 0.90 or font_size <= min_font_size:
            break
        if len(lines) == 1 and len(words) > 1:
            mid = len(words) // 2
            lines = [" ".join(words[:mid]), " ".join(words[mid:])]
            continue
        font_size = round(font_size * 0.92)

    stroke_w = max(3, round(font_size * 0.09))
    line_gap = round(font_size * 0.18)
    line_dims = [draw.textbbox((0, 0), ln, font=font, stroke_width=stroke_w)[2:4] for ln in lines]
    y = height * 0.06
    for ln, (lw, lh) in zip(lines, line_dims):
        x = (width - lw) / 2
        # Gelb auf schwarzem Outline (User-Referenzbild "TRUST ME BRO") -- klassischer
        # Clickbait-Kontrast, lesbar über jedem Bild.
        draw.text((x, y), ln, font=font, fill=(255, 221, 0),
                   stroke_width=stroke_w, stroke_fill=(0, 0, 0))
        y += lh + line_gap
    canvas.save(image_path, quality=92)


def gen_thumbnail_image(prompt: str, master_style: str, out_path: str,
                         model: str = "nano-banana-2", ref_urls: list | None = None) -> dict:
    """Submits + polls + downloads a 16:9 thumbnail image. Reuses the same KIE image
    pipeline as scene generation, just with thumbnail-specific dimensions/prompt.

    Evaluation Juli 2026 (Änderung 1): nutzt jetzt engine.imagegen.generate_image()
    statt eines eigenen, zirkulär aus dashboard.py importierten Submit+Poll+Download-
    Musters (das war 1:1 dieselbe Logik dreimal im Code, siehe engine/imagegen.py
    Modul-Docstring)."""
    from engine.imagegen import generate_image

    full_prompt = prompt.strip() + "\n\n" + master_style.strip()
    result = generate_image(full_prompt, ref_urls, out_path=out_path, model=model)
    if not result["ok"]:
        return {"ok": False, "error": result.get("error") or "unbekannter Fehler"}
    return {"ok": True, "file": os.path.basename(out_path), "source_url": result.get("url")}
