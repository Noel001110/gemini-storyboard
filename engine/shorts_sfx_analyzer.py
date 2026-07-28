"""engine/shorts_sfx_analyzer.py -- das "Gehirn" des neuen Shorts-SFX-Systems.

Zwei Schichten, siehe `analyze_short_for_sfx()`:

1. Deterministische Basis-Schicht: reicht die vom Aufrufer (workers/render.py, der bei
   jedem Schnitt ohnehin schon `_transition_for_scene()` aufruft) übergebenen
   Übergangs-Events unverändert durch -- Bing/Plop/Woosh sind bereits über
   `engine.render.TRANSITION_LIBRARY` an jeden Schnitt gebunden, kein LLM nötig.
2. Semantische Schicht: EIN gebündelter Gemini-Call für alle dynamisch unter `Sounds/*`
   gefundenen NICHT-mechanischen Kategorien (aktuell keine -- nur Bing/Plop/Woosh
   existieren auf der Platte, siehe engine/sfx_library.py -- wird also erst aktiv,
   sobald der User z.B. `Sounds/Cash/` anlegt, ohne Codeänderung hier). Wort-Index-
   basiertes Timing (LLM liefert nur Index + Kategorie, nie Dateiname/Freitext-Zeit),
   code-erzwungene Guards (Platzierungs-Fenster, Pacing-Dichte-Sparsamkeit, dynamisch
   gegen die Vorwort-Grenze geclampter Vorlauf).

Reine Entscheidungslogik -- kein ffmpeg (siehe engine/audio.py `_place_sfx` für die
Ausführung), kein Dateisystem-Scan (siehe engine/sfx_library.py für die Discovery).
Fällt bei jedem LLM-Fehler oder wenn es (noch) keine semantischen Kategorien gibt auf
die reine deterministische Schicht zurück -- nie ein Renderabbruch, gleiches
Resilienz-Prinzip wie `engine.prompts.assign_short_scene_images`.
"""
from __future__ import annotations

import json

_HOOK_WINDOW_SEC = 3.0
_REENGAGEMENT_WINDOW_SEC = 2.0

# Pacing-Dichte-Guard statt starrem "max N pro 60s" (Review-Ergänzung, siehe Plan):
# punchy-Abschnitte dürfen dichter gepackt sein, calm-Abschnitte bleiben near-silence.
_MIN_GAP_BY_PACING = {"punchy": 3.0, "calm": 8.0, "normal": 5.0}
# Sicherheitsnetz gegen einen kompletten Prompt-Ausrutscher, bewusst weiter gefasst als
# die ursprünglich angedachte feste "3-4 pro 60s"-Zahl (siehe Plan, Pacing-Dichte-Guard).
_OVERALL_SAFETY_CAP_PER_60S = 6.0

_LEAD_IN_FACTOR = 0.1
_LEAD_IN_CAP_SEC = 0.05


def _flatten_words(scenes: list) -> list:
    """Baut die flache, absolute Wort-Zeitleiste des gesamten Shorts aus den bereits
    pro Szene ausgerichteten `words`-Slices (`dashboard.align_scenes_to_whisper`,
    scene-relativ zu `start_aligned` -- dieselbe Konvention wie `_compute_accent_t`).
    Aus den PERSISTIERTEN Szenendaten gebaut statt aus einer Variable, die nur beim
    frischen Alignment im Scope wäre -- funktioniert dadurch identisch bei einem
    Resume-Render (Szenen schon vorher ausgerichtet, kein erneuter Whisper-Lauf)."""
    words = []
    for s in scenes:
        start_aligned = s.get("start_aligned")
        if start_aligned is None:
            continue
        for w in s.get("words") or []:
            words.append({"word": w["word"],
                          "start": start_aligned + w["start"],
                          "end": start_aligned + w["end"]})
    return words


def _reengagement_points(scenes: list) -> list:
    """Zeitpunkte, an denen sich `phase` gegenüber der Vorszene ändert -- konkreter,
    code-prüfbarer Auslöser für "größerer Themenwechsel" (statt eines vagen "irgendwo
    in der Mitte"), siehe `placement_window="hook_and_reengagement"`."""
    points = []
    prev_phase = None
    for s in scenes:
        phase = s.get("phase")
        start = s.get("start_aligned")
        if phase and prev_phase and phase != prev_phase and start is not None:
            points.append(start)
        if phase:
            prev_phase = phase
    return points


def _within_placement_window(t: float, window: str, reengagement_points: list) -> bool:
    if window == "anywhere":
        return True
    if t <= _HOOK_WINDOW_SEC:
        return True
    if window == "hook_and_reengagement":
        return any(p <= t <= p + _REENGAGEMENT_WINDOW_SEC for p in reengagement_points)
    return False  # window == "hook": außerhalb des Hook-Fensters ist jeder Vorschlag ungültig


def _pacing_at(t: float, scenes: list) -> str:
    for s in scenes:
        start, end = s.get("start_aligned"), s.get("end_aligned")
        if start is not None and end is not None and start <= t <= end:
            return s.get("pacing", "normal")
    return "normal"


def _lead_in_start(word: dict, prev_word: dict | None) -> float:
    """Dynamischer, geclampter Vorlauf statt fest 0.1s (Review-Fix): bei hohem Tempo +
    engem Pausen-Cap (siehe workers/render.py) wäre ein fixer Vorlauf riskant nah am/über
    das VORHERIGE Wort. `min(0.1 * Wortdauer, 0.05s)`, zusätzlich nie vor `prev_word.end`."""
    dur = max(0.0, word["end"] - word["start"])
    lead = min(_LEAD_IN_FACTOR * dur, _LEAD_IN_CAP_SEC)
    t = word["start"] - lead
    if prev_word is not None:
        t = max(t, prev_word["end"])
    return max(0.0, t)


def _deterministic_events(transition_events: list) -> list:
    events = []
    for i, ev in enumerate(transition_events):
        sfx, start = ev.get("sfx"), ev.get("start")
        if not sfx or start is None:
            continue
        events.append({"start": float(start), "sfx": sfx, "seed": i, "duck_voice": False})
    return events


# Juli 2026, Real-Daten-Fix (echter Short "how_to_gett_100k_easy" beim Verifizieren
# getestet): Shorts bekommen JEDEN Schnittpunkt als Übergang (siehe workers/render.py,
# Cuts liegen typischerweise alle ~2-4s) -- der volle Pacing-Mindestabstand (bis zu 8s
# bei "calm") GEGEN JEDEN Übergang hätte semantische Treffer in praktisch jedem Short
# strukturell unmöglich gemacht (in einem echten Test wurden dadurch ALLE 3 sinnvollen
# LLM-Vorschläge verworfen, obwohl keiner davon wirklich kollidiert hätte). Übergänge
# brauchen nur einen kleinen KOLLISIONS-Puffer (kein Doppel-Sound im selben Moment),
# keinen vollen Dichte-Abstand -- der volle Pacing-Mindestabstand gilt weiterhin
# zwischen semantischen Treffern untereinander (das eigentliche "Häufung"-Problem).
_TRANSITION_COLLISION_BUFFER_SEC = 0.6


def _apply_sparsity_guard(deterministic_events: list, semantic_candidates: list,
                           scenes: list, total_duration: float) -> list:
    """Merged Basis- und semantische Events in eine Zeitleiste. Übergänge sind bereits
    an echte Schnitte gebunden und bleiben immer erhalten; semantische Kandidaten dürfen
    nicht mit einem Übergangs-Sound kollidieren (kleiner fixer Puffer) und müssen sich
    UNTEREINANDER am Pacing-abhängigen Mindestabstand messen (verhindert Häufung)."""
    accepted_det = sorted(deterministic_events, key=lambda e: e["start"])
    accepted_sem: list = []
    # Sicherheitsnetz gilt NUR für die semantische Zusatzmenge, unabhängig von der
    # Übergangs-Anzahl (Real-Daten-Fix: bei Shorts mit einem Übergang an praktisch jedem
    # Schnitt -- der Regelfall, siehe workers/render.py -- hätte ein gemeinsamer Deckel
    # das semantische Budget schon vor dem ersten Kandidaten aufgebraucht).
    semantic_cap = max(1, round(_OVERALL_SAFETY_CAP_PER_60S * max(total_duration, 1.0) / 60.0))
    for ev in sorted(semantic_candidates, key=lambda e: e["start"]):
        if len(accepted_sem) >= semantic_cap:
            break
        if any(abs(ev["start"] - a["start"]) < _TRANSITION_COLLISION_BUFFER_SEC for a in accepted_det):
            continue
        min_gap = _MIN_GAP_BY_PACING.get(_pacing_at(ev["start"], scenes), _MIN_GAP_BY_PACING["normal"])
        if any(abs(ev["start"] - a["start"]) < min_gap for a in accepted_sem):
            continue
        if any(a["sfx"] == ev["sfx"] and abs(ev["start"] - a["start"]) < min_gap * 2 for a in accepted_sem):
            continue
        accepted_sem.append(ev)
    return sorted(accepted_det + accepted_sem, key=lambda e: e["start"])


def analyze_short_for_sfx(scenes: list, transition_events: list,
                           categories: dict | None = None) -> list:
    """Liefert die fertige SFX-Event-Liste für `engine.audio._place_sfx` (Liste von
    `{"start", "sfx", "seed", "duck_voice"}`-Dicts).

    `scenes`: die bereits Whisper-ausgerichteten Short-Szenen (brauchen `start_aligned`/
    `end_aligned`/`words`/`pacing`/`phase` -- alles bereits vorhandene Felder aus dem
    Render-Worker, keine neuen).
    `transition_events`: vom Aufrufer bereits berechnete Übergangs-SFX (`{"start", "sfx"}`
    je Schnitt, `sfx` == `TRANSITION_LIBRARY[...]["sfx"]`) -- workers/render.py hat diese
    Werte ohnehin schon pro Schnitt vorliegen (`_transition_for_scene`).
    `categories`: optionaler Override von `engine.sfx_library.discover_categories()`
    (v.a. für Tests -- ohne Argument wird der echte `Sounds/`-Ordner gescannt).
    """
    # Lazy (Architektur-Invariante, siehe t_phase_m_no_eager_cross_engine_imports):
    # engine-Module importieren sich gegenseitig nur innerhalb von Funktionen, nie
    # top-level im Modul-Body -- sonst Importzyklus-Risiko sobald ein Modul das andere
    # erweitert.
    from engine.sfx_library import discover_categories, get_category_meta, semantic_categories

    all_categories = categories if categories is not None else discover_categories()
    deterministic = _deterministic_events(transition_events)

    sem_categories = semantic_categories(all_categories)
    if not sem_categories:
        return deterministic

    words = _flatten_words(scenes)
    if not words:
        return deterministic

    total_duration = max((w["end"] for w in words), default=0.0)
    reengagement_points = _reengagement_points(scenes)
    meta_by_cat = {cat: get_category_meta(cat) for cat in sem_categories}

    cat_lines = "\n".join(
        f"- {cat}: {meta_by_cat[cat].description or '(no tonal description set)'}"
        for cat in sem_categories
    )
    word_lines = "\n".join(f"{i}:{w['word']}" for i, w in enumerate(words))

    system_msg = (
        "You place semantic sound-effect cues into a short-form video transcript. "
        "You choose a FUNCTIONAL CATEGORY per cue, never a specific audio file -- the "
        "actual file is picked deterministically afterwards, that part is not your job.\n\n"
        "Rules:\n"
        "- Match the EMOTIONAL DIRECTION/INTENSITY of the moment, not bare keyword "
        "overlap. A money-word used in a NEGATIVE/loss context must NOT trigger a "
        "positive category, and vice versa -- read each category's tonal note "
        "carefully, it tells you when NOT to use it too.\n"
        "- Be sparse: only genuinely fitting moments. Do not force a cue in just "
        "because a related word appears.\n"
        "- Never suggest the same category twice in a row.\n"
        "- word_index must be an index that exists in the given transcript."
    )
    user_msg = (
        f"CATEGORIES (name: meaning/tone):\n{cat_lines}\n\n"
        f"TRANSCRIPT (word_index:word):\n{word_lines}\n\n"
        "Suggest sound-effect cues as a JSON array of {word_index, category}."
    )
    schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "word_index": {"type": "integer"},
                "category": {"type": "string"},
            },
            "required": ["word_index", "category"],
        },
    }
    try:
        from dashboard import post_gemini_native  # lazy, siehe engine/prompts.py-Konvention
        txt = post_gemini_native([
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ], json_mode=True, temp=0.4, response_schema=schema, thinking_level="low")
        suggestions = json.loads(txt)
    except Exception as e:
        print(f"  [ShortsSFX] Semantische Schicht übersprungen: {e}", flush=True)
        return deterministic

    candidates = []
    if isinstance(suggestions, list):
        for item in suggestions:
            try:
                idx = int(item["word_index"])
                cat = str(item["category"])
            except (KeyError, TypeError, ValueError):
                continue
            if cat not in sem_categories or not (0 <= idx < len(words)):
                continue
            meta = meta_by_cat[cat]
            word = words[idx]
            prev_word = words[idx - 1] if idx > 0 else None
            start = _lead_in_start(word, prev_word)
            if not _within_placement_window(start, meta.placement_window, reengagement_points):
                continue
            candidates.append({"start": start, "sfx": cat, "seed": idx, "duck_voice": meta.loud})

    return _apply_sparsity_guard(deterministic, candidates, scenes, total_duration)
