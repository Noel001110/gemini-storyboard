"""engine_elevenlabs.py — Phase J: ElevenLabs-Integration als eigenständiges Modul.

Aus dashboard.py extrahiert (Phase J Engine-Refactor). Alle öffentlichen Namen
sind per `from engine_elevenlabs import *` aus dashboard.py weiterhin global
verfügbar — keine Frontend- oder Worker-Code-Änderung notwendig.

Inhalt:
- Konstanten: ELEVENLABS_API, ELEVENLABS_DEFAULT_MODEL, ELEVENLABS_KEY_FILE,
  ELEVENLABS_VOICE_SETTINGS_DEFAULT
- Voice-Settings-Persistenz: ch_voice_id, ch_voice_settings, elevenlabs_key,
  _resolve_voice_id, load_voice_settings, save_voice_settings
- API-Call: _elevenlabs_call_with_retry, elevenlabs_generate
- Orchestrierung: _elevenlabs_persist_and_schedule
- TTS-Preprocessing: _enrich_for_tts (Phase I)
"""
import os
import re
import sys
import json
import time
import base64
import urllib.request
import urllib.error
import subprocess
import threading

# Wildcard-import in dashboard.py braucht eine explizite `__all__`-Liste, weil
# underscore-prefixed Namen (z.B. _enrich_for_tts) bei `import *` NICHT
# standardmäßig übernommen werden. KOMPLETT explizit — keine dir()-
# Comprehension. Reordnen im Modul darf KEINE stille Änderung am
# Wildcard-Export zur Folge haben (das war der User-Feedback-Befund):
# wenn jemand nachträglich eine Funktion hinzufügt und vergisst sie
# in die Liste einzutragen, bricht der Import stillschweigend.
__all__ = [
    # Konstanten
    "ELEVENLABS_API", "ELEVENLABS_DEFAULT_MODEL", "ELEVENLABS_KEY_FILE",
    "ELEVENLABS_VOICE_SETTINGS_DEFAULT", "EL_BACKOFF_SEC",
    "MINIMAX_API", "MINIMAX_DEFAULT_MODEL", "MINIMAX_KEY_FILE",
    "MINIMAX_VOICE_SETTINGS_DEFAULT",
    # Phase-Engine Constants (Phasen B-G)
    "PHASE_SET", "PHASE_TO_ACT", "PHASE_PROMPT_ADDITIONS",
    "PHASE_COLOR_FILTER", "PHASE_VOLUME", "PHASE_ACCENT",
    # Voice-Settings-Persistenz
    "ch_voice_id", "elevenlabs_key", "_minimax_key", "_resolve_voice_id",
    "load_voice_settings", "save_voice_settings",
    # API-Call + Orchestration
    "elevenlabs_generate", "minimax_generate",
    "_elevenlabs_persist_and_schedule", "_tts_persist_and_schedule",
    # TTS-Preprocessing (Phase I)
    "_enrich_for_tts", "TTS_PAUSE_BEFORE_CLIMAX", "TTS_PAUSE_AFTER_PHASE_BREAK",
]  # end __all__ — fully explicit, no auto-discovery via dir()

# Konstanten
ELEVENLABS_API           = "https://api.elevenlabs.io/v1"
ELEVENLABS_DEFAULT_MODEL = "eleven_multilingual_v2"
ELEVENLABS_KEY_FILE      = os.path.expanduser("~/.elevenlabs_key")

# MiniMax Speech — zweiter TTS-Provider (parallel zu ElevenLabs). Provider-Auswahl
# pro Channel via voice_settings.json:tts_provider. Beide Provider liefern eine
# ähnliche JSON-Shape zurück, sodass _tts_persist_and_schedule provider-agnostic
# arbeiten kann. Stand 2026 ist speech-2.6-hd der empfohlene Default für
# Storytelling (Pacing + Emotionalität; siehe ARCHITECTURE §34).
MINIMAX_API              = "https://api.minimaxi.chat/v1"
MINIMAX_DEFAULT_MODEL    = "minimax-speech-2.6-hd"
MINIMAX_KEY_FILE         = os.path.expanduser("~/.minimax_key")

ELEVENLABS_VOICE_SETTINGS_DEFAULT = {
    "voice_id": "",
    "model_id": ELEVENLABS_DEFAULT_MODEL,
    "stability": 0.5,
    "similarity_boost": 0.75,
    "style": 0.0,
    "use_speaker_boost": True,
    "output_format": "mp3_44100_128",
}

# Phase-Engine Constants (zugehörig zu Phase B-H, leben hier im selben Modul
# weil sie thematisch an der Voiceover-Pipeline hängen: phase-aware hooks
# wie PHASE_PROMPT_ADDITIONS, PHASE_COLOR_FILTER, PHASE_VOLUME, PHASE_ACCENT)
PHASE_SET = {"OPENING", "RISING_ACTION", "CLIMAX", "RESOLUTION"}
PHASE_TO_ACT = {"OPENING": 0, "RISING_ACTION": 1, "CLIMAX": 2, "RESOLUTION": 3}
PHASE_PROMPT_ADDITIONS = {
    "OPENING":       "STYLE: slow, deliberate composition; establish setting; neutral color palette; static-feeling even if motion comes later.",
    "RISING_ACTION": "STYLE: building tension; tighter framing; movement toward subject; contrast slightly elevated.",
    "CLIMAX":        "STYLE: maximum visual impact; high contrast; dynamic angle; subject dominates frame; emotional saturation.",
    "RESOLUTION":    "STYLE: wind-down; wider framing; softer palette; contemplative stillness.",
}
PHASE_COLOR_FILTER = {
    # Phase P — Ink-Style-Grading-Refinement:
    # - colorbalance auf Mitteltöne/Lichter: kühlere/wärmere Tönung pro Phase
    #   (Plan §0: "Papier-Tönung wirkt deutlich stärker als eq" auf Tusche-Look)
    # - colorbalance-Args: rs/gs/bs = shadow tints, rm/gm/bm = midtones (wirkt am stärksten),
    #                       rl/gl/bl = highlight tints (sehr subtil)
    # - vignette nur für CLIMAX (Plan §3: dezent, PI/5-Bereich) — ffmpeg-seitig ist
    #   `vignette` ein SEPARATER Filter, NICHT ein Parameter von colorbalance.
    #   Daher: bei CLIMAX hängt der Aufrufer (engine/render.py _render_clip) den
    #   Vignette-Filter mit Komma verkettet an: "colorbalance=...,vignette=PI/5".
    # - OPENING kühl (leichte Blau-Tönung = neutraler/neugieriger Einstieg)
    # - RISING_ACTION leicht kühl (steigende Spannung)
    # - CLIMAX warm (gebrochenes Orange/Beige = dramatische Wärme) + Vignette
    # - RESOLUTION leicht grünlich (Auflösung/Nachklang)
    #
    # Werte sind klein gehalten (-0.05..+0.08), keine Filter soll "defekt" wirken (§26.3).
    "OPENING":       "colorbalance=rm=-0.02:gm=0.0:bm=+0.04",
    "RISING_ACTION": "colorbalance=rm=0.0:gm=0.0:bm=+0.02",
    "CLIMAX":        "colorbalance=rm=+0.05:gm=+0.02:bm=-0.02",  # +vignette in _render_clip
    "RESOLUTION":    "colorbalance=rm=-0.01:gm=+0.02:bm=-0.01",
}
PHASE_VOLUME = {
    "OPENING":       0.30,
    "RISING_ACTION": 0.55,
    "CLIMAX":        0.85,
    "RESOLUTION":    0.35,
}
PHASE_ACCENT = {
    "OPENING":       "#888",
    "RISING_ACTION": "#1e6bd6",
    "CLIMAX":        "#c13838",
    "RESOLUTION":    "#1f8a4a",
}


# Kanal-Pfad-Helfer (lokal zu engine_elevenlabs umgezogen — ch_voice_id/ch_voice_settings
# hängen nur an diesem Modul; main dashboard.py hat die Originale behalten, aber wir
# brauchen unsere eigenen, weil dashboard.py hier bereits geschlossene Konstanten liefert).
def ch_voice_id(cid):       return os.path.join(_ch_dir(cid), "voice_id.txt")
def ch_voice_settings(cid): return os.path.join(_ch_dir(cid), "voice_settings.json")
def _ch_dir(cid):
    """Liefert das Channel-Verzeichnis. Wir importieren die Originale ch_dir() aus
    dashboard.py zur Laufzeit (lazy, um zirkuläre Imports zu vermeiden)."""
    from dashboard import ch_dir
    return ch_dir(cid)


# Voice-Settings-Persistenz
def elevenlabs_key() -> str:
    p = ELEVENLABS_KEY_FILE
    if not os.path.exists(p):
        raise RuntimeError(
            f"ElevenLabs-Key fehlt: {p} — bitte `echo \"$ELEVENLABS_API_KEY\" > {p} && "
            f"chmod 600 {p}` einmalig ausführen."
        )
    return open(p).read().strip()

def _minimax_key() -> str:
    """Liest MiniMax-API-Key aus ~/.minimax_key oder env MINIMAX_API_KEY.
    Wirft RuntimeError mit Setup-Anleitung wenn fehlt — gleiche Konvention wie
    elevenlabs_key() und kie_key(). Setup: `echo "$MINIMAX_API_KEY" > ~/.minimax_key && chmod 600`."""
    env = os.environ.get("MINIMAX_API_KEY", "").strip()
    if env:
        return env
    p = MINIMAX_KEY_FILE
    if not os.path.exists(p):
        raise RuntimeError(
            f"MiniMax-Key fehlt: {p} — bitte `echo \"$MINIMAX_API_KEY\" > {p} && "
            f"chmod 600 {p}` einmalig ausführen."
        )
    return open(p).read().strip()

def _resolve_voice_id(cid: str, override: str = "") -> str:
    if override:
        return override.strip()
    p = ch_voice_id(cid)
    if os.path.exists(p):
        v = open(p).read().strip()
        if v:
            return v
    return os.environ.get("ELEVENLABS_VOICE_DEFAULT", "").strip()

def load_voice_settings(cid: str, override_voice_id: str = "") -> dict:
    s = dict(ELEVENLABS_VOICE_SETTINGS_DEFAULT)
    sp = ch_voice_settings(cid)
    if os.path.exists(sp):
        try:
            saved = json.load(open(sp))
            if isinstance(saved, dict):
                s.update({k: v for k, v in saved.items() if v != "" or k == "voice_id"})
        except Exception as e:
            print(f"  [ElevenLabs] voice_settings.json unlesbar ({e}) — nutze Defaults", flush=True)
    s["voice_id"] = _resolve_voice_id(cid, override_voice_id)
    return s

def save_voice_settings(cid: str, settings: dict) -> None:
    clean = dict(ELEVENLABS_VOICE_SETTINGS_DEFAULT)
    clean.update({k: settings[k] for k in settings if k in clean and k != "voice_id"})
    for k in ("stability", "similarity_boost", "style"):
        try:    clean[k] = max(0.0, min(1.0, float(clean[k])))
        except Exception: clean[k] = ELEVENLABS_VOICE_SETTINGS_DEFAULT[k]
    clean["use_speaker_boost"] = bool(settings.get("use_speaker_boost", True))
    if settings.get("model_id"):
        clean["model_id"] = str(settings["model_id"])
    vid = str(settings.get("voice_id", "")).strip()
    if vid:
        with open(ch_voice_id(cid), "w") as f:
            f.write(vid + "\n")
    json.dump(clean, open(ch_voice_settings(cid), "w"), ensure_ascii=False, indent=1)


# API-Call mit Retry
EL_BACKOFF_SEC = [5, 10, 20]

def _elevenlabs_call_with_retry(url: str, body: dict, headers: dict) -> dict:
    last_err = None
    for attempt, wait in enumerate([0] + EL_BACKOFF_SEC):
        if wait:
            print(f"  [ElevenLabs] Retry {attempt}/{len(EL_BACKOFF_SEC)} in {wait}s …", flush=True)
            time.sleep(wait)
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
            headers={**headers, "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:    err_body = json.loads(e.read() or b"{}")
            except: err_body = {}
            detail = (err_body.get("detail") if isinstance(err_body, dict) else None) or e.reason
            last_err = RuntimeError(f"ElevenLabs HTTP {e.code}: {detail}")
            if e.code not in (408, 425, 429) and e.code < 500:
                raise last_err
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = RuntimeError(f"ElevenLabs Netzwerkfehler: {e}")
        except Exception as e:
            last_err = RuntimeError(f"ElevenLabs Fehler: {e}")
    raise last_err or RuntimeError("ElevenLabs: retries exhausted")

def _minimax_call_with_retry(url: str, body: dict, headers: dict) -> dict:
    """MiniMax-HTTP-Wrapper mit identischem Retry-Verhalten wie ElevenLabs.
    MiniMax verwendet Bearer-Auth statt xi-api-key. Wirft RuntimeError sofort
    auf 4xx (außer 408/425/429), retry 5xx+429 mit exponentiellem Backoff.
    Identische Return-Shape zu ElevenLabs (audio_base64 + words[]) wäre Provider-ideal,
    ist hier aber pragmatisch als opaque passthrough implementiert: MiniMax liefert
    aktuell nur Audio-Bytes ohne Word-Timestamps; wir generieren Word-Timestamps
    lokal via proportionaler Text/Length-Heuristik, falls nötig. echt-getestet wird
    das erst, wenn der User einen MiniMax-Key + Voice einrichtet."""
    last_err = None
    for attempt, wait in enumerate([0] + EL_BACKOFF_SEC):
        if wait:
            print(f"  [MiniMax] Retry {attempt}/{len(EL_BACKOFF_SEC)} in {wait}s …", flush=True)
            time.sleep(wait)
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
            headers={**headers, "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:    err_body = json.loads(e.read() or b"{}")
            except: err_body = {}
            base_msg = err_body.get("base_resp", {}).get("status_msg", "") if isinstance(err_body, dict) else ""
            detail = base_msg or (e.reason if hasattr(e, "reason") else str(e))
            last_err = RuntimeError(f"MiniMax HTTP {e.code}: {detail}")
            if e.code not in (408, 425, 429) and e.code < 500:
                raise last_err
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = RuntimeError(f"MiniMax Netzwerkfehler: {e}")
        except Exception as e:
            last_err = RuntimeError(f"MiniMax Fehler: {e}")
    raise last_err or RuntimeError("MiniMax: retries exhausted")

def elevenlabs_generate(text: str, settings: dict) -> dict:
    voice_id = settings.get("voice_id") or ""
    if not voice_id:
        raise RuntimeError("Keine voice_id konfiguriert: ~/.elevenlabs_key oder channels/<cid>/voice_id.txt befüllen.")
    key = elevenlabs_key()
    url = f"{ELEVENLABS_API}/text-to-speech/{voice_id}/with-timestamps"
    body = {
        "text": text,
        "model_id": settings.get("model_id") or ELEVENLABS_DEFAULT_MODEL,
        "voice_settings": {
            "stability": float(settings.get("stability", 0.5)),
            "similarity_boost": float(settings.get("similarity_boost", 0.75)),
            "style": float(settings.get("style", 0.0)),
            "use_speaker_boost": bool(settings.get("use_speaker_boost", True)),
        },
        "output_format": settings.get("output_format") or "mp3_44100_128",
    }
    headers = {"xi-api-key": key, "Accept": "application/json"}
    resp = _elevenlabs_call_with_retry(url, body, headers)

    if not isinstance(resp, dict):
        raise RuntimeError(f"ElevenLabs-Antwort ist kein JSON-Objekt: {type(resp).__name__}")
    if not resp.get("audio_base64"):
        raise RuntimeError("ElevenLabs antwortete ohne audio_base64 — bitte erneut versuchen.")
    alignment = resp.get("alignment") or {}
    words = alignment.get("words") if isinstance(alignment, dict) else None
    if not isinstance(words, list) or not words:
        raise RuntimeError(
            "ElevenLabs antwortete ohne alignment.words — bitte erneut versuchen "
            "(Provider-Schema-Drift oder leerer Text?)."
        )
    norm = []
    for w in words:
        if not isinstance(w, dict):
            continue
        txt = (w.get("text") or w.get("word") or "").strip()
        if not txt:
            continue
        try:
            s = float(w.get("start", 0.0)); e = float(w.get("end", s + 0.01))
        except (TypeError, ValueError):
            continue
        norm.append({"word": txt, "start": max(0.0, s), "end": max(s + 0.01, e)})
    if not norm:
        raise RuntimeError("ElevenLabs-alignment.words enthielt keine verwertbaren Wörter.")
    return {
        "audio_base64": resp["audio_base64"],
        "words": norm,
        "task_id": f"el_{voice_id[:8]}_{int(time.time())}",
    }


def minimax_generate(text: str, settings: dict) -> dict:
    """MiniMax Speech-Generierung. MiniMax liefert aktuell (Stand 2026) Audio-Bytes
    OHNE per-Word-Timestamps (anders als ElevenLabs). Wir generieren proportional
    Word-Starts/Ends aus Wortanzahl + geschätzter Dauer. Das ist nicht so genau wie
    ElevenLabs-timestamps, reicht aber für die Scene-Alignment-Pipeline in
    _render_worker (Stage "timing"). Bei späteren MiniMax-Updates mit Word-Timestamps
    einfach hier den _extract_minimax_words() ersetzen.

    Liefert gleiche Return-Shape wie elevenlabs_generate() — Provider-agnostic
    Konsumenten (_tts_persist_and_schedule) müssen nicht wissen welcher Provider."""
    voice_id = settings.get("voice_id") or ""
    if not voice_id:
        raise RuntimeError(
            "Keine MiniMax-Voice-ID konfiguriert. Bitte im Audio-Step eine Voice "
            "aus dem Dropdown wählen (Settings-Modal oder Voice-Dropdown)."
        )
    model = settings.get("model_id") or MINIMAX_DEFAULT_MODEL
    # MiniMax-API-Body (Format an Stand 2026 — möglicherweise Anpassungen bei Updates)
    body = {
        "model": model,
        "text": text,
        "stream": False,
        "voice_setting": {
            "voice_id": voice_id,
            "speed":   float(settings.get("speed", 1.0)),
            "vol":     float(settings.get("volume", 1.0)),
            "pitch":   int(settings.get("pitch", 0)),
        },
        "audio_setting": {
            "sample_rate": 32000,
            "bitrate":     128000,
            "format":      "mp3",
        },
    }
    headers = {
        "Authorization": f"Bearer {_minimax_key()}",
        "Content-Type":  "application/json",
    }
    resp = _minimax_call_with_retry(f"{MINIMAX_API}/t2a_v2", body, headers)

    if not isinstance(resp, dict):
        raise RuntimeError(f"MiniMax-Antwort kein JSON-Objekt: {type(resp).__name__}")
    # MiniMax-Response-Format (Stand 2026): data.audio (hex-encoded bytes) ODER
    # data.audio (base64). Wir unterstützen beide — Provider-Agnostik.
    audio_hex = (resp.get("data") or {}).get("audio")
    if not audio_hex:
        raise RuntimeError(f"MiniMax-Antwort ohne data.audio: {resp.get('base_resp', resp)}")
    # MiniMax-Endpoints liefern entweder hex-encodierte oder base64-codierte Bytes
    # je nach Modell. Detect via String-Länge: hex ist 2x byte-länge, base64 ist ~1.33x.
    try:
        # Probieren base64 zuerst (häufiger), fallback hex
        try:
            audio_bytes = base64.b64decode(audio_hex, validate=True)
        except Exception:
            audio_bytes = bytes.fromhex(audio_hex)
    except Exception as e:
        raise RuntimeError(f"MiniMax-audio-Encoding unbekannt: {e}")

    # Word-Timestamps proportional aus Textlänge + estimated_duration erzeugen.
    words_input = [w for w in re.split(r"\s+", text.strip()) if w]
    estimated_duration = max(2.0, len(words_input) * 0.42)  # ~140 WPM Default
    if words_input:
        per = estimated_duration / len(words_input)
        words = [{"word": w, "start": i * per, "end": (i + 1) * per} for i, w in enumerate(words_input)]
    else:
        words = []

    return {
        "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
        "words": words,
        "task_id": f"mm_{voice_id[:8]}_{int(time.time())}",
    }


# Provider-Auswahl + Default-Settings (MiniMax-spezifisch)
MINIMAX_VOICE_SETTINGS_DEFAULT = {
    "voice_id": "",
    "model_id": MINIMAX_DEFAULT_MODEL,
    "speed":    1.0,
    "volume":   1.0,
    "pitch":    0,
}


# Phase I: TTS-Preprocessing
TTS_PAUSE_BEFORE_CLIMAX = "... "
TTS_PAUSE_AFTER_PHASE_BREAK = "\n\n"

def _enrich_for_tts(text: str, scenes: list = None) -> str:
    """Phase I: enrich raw narration text with TTS-friendly pause/emphasis markers.

    Idempotent by construction (split-then-join pattern): each second call
    produces the same output as the first. Markers applied:
      - ' ... ' between sentences (subtle breath between capital-letter boundaries)
      - '... ' before is_climax scenes (extra emphasis)
      - '\n\n' before is_phase_break scenes (act-change pause)
    Returns the enriched text. If `scenes` is None, only sentence-level
    enrichment runs (the relative-scene lookup is optional).

    Abbreviations: detected and skipped via pre-pass sentinel. Without this, common
    abbreviations like 'Dr. Müller', 'USA. Wir', 'z.B. diese', 'Mio. Dollar' would
    trigger the sentence-break heuristic on their internal period, producing extra
    ' ... ' pauses and breaking TTS audio ('Doktor ... Müller' instead of 'Doktor Müller').
    """
    enriched = text.strip()
    if not enriched:
        return enriched
    # Abbreviations: pre-pass with sentinel so the split-then-join doesn't see them.
    # Heuristic: short (1-3 letter) word, NOT prefixed by another letter (word-boundary),
    # followed by '. <capital>' = typical abbreviation. Negative lookbehind '(?<![letter])'
    # is fixed-width → safe in Python re. Sentinel is a Unicode non-printing char
    # (U+200E LEFT-TO-RIGHT MARK) that survives re.split and re.replace rounds without
    # being mis-recognized as word boundary or whitespace.
    SENTINEL = "‎"
    enriched = re.sub(
        r"(?<![A-Za-zÀ-ÿ])([A-Za-zÀ-ÿ]{1,3})\.(\s+)(?=[A-ZÀ-ÿ])",
        lambda m: m.group(1) + SENTINEL + m.group(2),
        enriched,
    )
    # Now split safely — abbreviations are masked out, real sentence boundaries
    # still match. Strip trailing ellipsis fragments from each part so the ' ... '
    # join produces at most ONE separator between any two sentences — idempotent on
    # repeated calls.
    parts = re.split(r"(?<=\.)\s+(?=[A-ZÀ-Ü])", enriched)
    cleaned = [re.sub(r"\s*\.\s*\.\s*\.\s*$", "", p).replace(SENTINEL, ".")
                for p in parts]
    enriched = " ... ".join(cleaned)
    if scenes:
        for s in scenes:
            txt = (s.get("text") or "").strip()
            if not txt:
                continue
            # Marker composition: phase_break FIRST (structural pause), climax AFTER
            # (emphasis). Phase-break is "\n\n" (no trailing whitespace); climax is
            # "... " (leading ellipsis, trailing space) — both kept VERBATIM so the
            # produced markers are visible after the replace. lstrip()/rstrip() on
            # the joined marker would silently drop the "\n\n" (lstrip whitespace
            # only) — a bug in the original implementation that's fixed by NOT
            # stripping here.
            prefix = ""
            if s.get("is_phase_break"):
                prefix += TTS_PAUSE_AFTER_PHASE_BREAK
            if s.get("is_climax"):
                prefix += TTS_PAUSE_BEFORE_CLIMAX
            # Idempotency: only insert if not already present (otherwise repeated calls
            # would compound markers, e.g. "\n\n... szene 4" → "\n\n... \n\n... szene 4").
            if prefix and prefix + txt not in enriched and txt in enriched:
                enriched = enriched.replace(txt, prefix + txt, 1)
    return enriched


def _tts_persist_and_schedule(cid: str, vid: str, text: str,
                                 settings: dict = None,
                                 override_voice_id: str = "") -> dict:
    """Provider-agnostic TTS-Pipeline. Dispatched auf Basis von `tts_provider`
    in voice_settings.json (default: 'elevenlabs'). Beide Provider liefern eine
    einheitliche Return-Shape (audio_base64 + words + task_id)."""
    if not vid:
        raise RuntimeError("Kein Video ausgewählt.")
    ensure_video(cid, vid)
    final_settings = load_voice_settings(cid, override_voice_id=override_voice_id)
    if settings:
        for k, v in settings.items():
            if k in final_settings:
                final_settings[k] = v
    # Auch MiniMax-Felder in final_settings mergen, falls vorhanden
    if final_settings.get("tts_provider") == "minimax":
        final_settings.update({k: v for k, v in settings.items() if k in MINIMAX_VOICE_SETTINGS_DEFAULT})
    provider = final_settings.get("tts_provider") or "elevenlabs"
    if provider == "minimax":
        return _minimax_persist_and_schedule(cid, vid, text, final_settings)
    # Default: ElevenLabs (backward-compat)
    return _elevenlabs_persist_and_schedule(cid, vid, text, final_settings)


def _elevenlabs_persist_and_schedule(cid: str, vid: str, text: str,
                                     settings: dict = None,
                                     override_voice_id: str = "") -> dict:
    """Synchronous wrapper around elevenlabs_generate() + idempotent persistence +
    schedules _transcribe_generate_worker in the background. Raises on every failure
    mode (no partial persistence)."""
    if not vid:
        raise RuntimeError("Kein Video ausgewählt.")
    # Lazy-import dashboard-only helpers so we don't create an import cycle.
    from dashboard import (ensure_video, ch_voice_id, ch_voice_settings,
                            v_uploads, v_audio, v_plan, _VOICE_JOBS_LOCK,
                            VOICE_JOBS, _transcribe_generate_worker)
    ensure_video(cid, vid)

    final_settings = load_voice_settings(cid, override_voice_id=override_voice_id)
    if settings:
        for k, v in settings.items():
            if k in final_settings:
                final_settings[k] = v
    voiceover_mp3 = os.path.join(v_uploads(cid, vid), "voiceover.mp3")
    trimmed_path  = os.path.join(v_uploads(cid, vid), "voiceover_trimmed.wav")
    if os.path.exists(trimmed_path):
        try:    os.remove(trimmed_path)
        except Exception: pass

    raw = elevenlabs_generate(text, final_settings)
    audio_bytes = base64.b64decode(raw["audio_base64"])
    words       = raw["words"]
    task_id     = raw["task_id"]
    char_count  = len(text)

    meta_written = False
    try:
        with open(voiceover_mp3, "wb") as f:
            f.write(audio_bytes)
        meta = {
            "path": voiceover_mp3,
            "mime": "audio/mpeg",
            "name": "voiceover.mp3",
            "voiceover_source": "elevenlabs",
            "voiceover_task_id": task_id,
            "voiceover_chars": char_count,
            "voiceover_word_timestamps": words,
            "voiceover_settings_used": {
                "voice_id":         final_settings.get("voice_id", ""),
                "model_id":         final_settings.get("model_id", ELEVENLABS_DEFAULT_MODEL),
                "stability":        final_settings.get("stability"),
                "similarity_boost": final_settings.get("similarity_boost"),
                "style":            final_settings.get("style"),
                "use_speaker_boost": final_settings.get("use_speaker_boost"),
            },
        }
        json.dump(meta, open(v_audio(cid, vid), "w"), ensure_ascii=False, indent=1)
        meta_written = True
    finally:
        if not meta_written and os.path.exists(voiceover_mp3):
            try:    os.remove(voiceover_mp3)
            except Exception: pass

    from dashboard import _PLAN_WRITE_LOCK
    with _PLAN_WRITE_LOCK:
        try:
            plan = json.load(open(v_plan(cid, vid)))
            for s in plan.get("scenes", []):
                s.pop("start_aligned", None)
                s.pop("end_aligned", None)
            json.dump(plan, open(v_plan(cid, vid), "w"), ensure_ascii=False, indent=1)
        except Exception:
            pass

    plan_thread_sec = float(settings.get("sec", 5.5)) if isinstance(settings, dict) else 5.5
    def _run():
        try:
            _transcribe_generate_worker(cid, vid, plan_thread_sec)
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"  [ElevenLabs→Plan] Fehler in _transcribe_generate_worker: {e}", flush=True)
    threading.Thread(target=_run, daemon=True).start()

    with _VOICE_JOBS_LOCK:
        VOICE_JOBS[(cid, vid)] = {
            "running": False,
            "stage": "fertig",
            "error": None,
            "voiceover_source": "elevenlabs",
            "voiceover_task_id": task_id,
            "voiceover_chars": char_count,
            "ts": time.time(),
            "resume": False,
        }
    print(f"  [ElevenLabs] {cid}/{vid}: voiceover.mp3 ({len(audio_bytes)//1024} KB, "
          f"{len(words)} Wörter, chars={char_count}) — Plan-Worker gestartet", flush=True)
    return {
        "ok": True,
        "task_id": task_id,
        "audio_kb": len(audio_bytes) // 1024,
        "n_words": len(words),
        "chars": char_count,
    }


def _minimax_persist_and_schedule(cid: str, vid: str, text: str,
                                  settings: dict = None) -> dict:
    """MiniMax-Pendant zu _elevenlabs_persist_and_schedule. Persistiert
    voiceover.mp3 + audio_meta.json, leitet _transcribe_generate_worker im
    Hintergrund an, aktualisiert VOICE_JOBS. Identischer Idempotency-Resume-
    Marker (voiceover_source = "minimax") damit /api/voiceover_generate beim
    zweiten Klick direkt 'fertig' zurückgibt."""
    if not vid:
        raise RuntimeError("Kein Video ausgewählt.")
    from dashboard import (ensure_video, v_uploads, v_audio, v_plan,
                           _VOICE_JOBS_LOCK, VOICE_JOBS,
                           _transcribe_generate_worker)
    ensure_video(cid, vid)
    voiceover_mp3 = os.path.join(v_uploads(cid, vid), "voiceover.mp3")
    trimmed_path  = os.path.join(v_uploads(cid, vid), "voiceover_trimmed.wav")
    if os.path.exists(trimmed_path):
        try:    os.remove(trimmed_path)
        except Exception: pass

    raw = minimax_generate(text, settings)
    audio_bytes = base64.b64decode(raw["audio_base64"])
    words       = raw["words"]
    task_id     = raw["task_id"]
    char_count  = len(text)

    meta_written = False
    try:
        with open(voiceover_mp3, "wb") as f:
            f.write(audio_bytes)
        meta = {
            "path": voiceover_mp3,
            "mime": "audio/mpeg",
            "name": "voiceover.mp3",
            "voiceover_source": "minimax",
            "voiceover_task_id": task_id,
            "voiceover_chars": char_count,
            "voiceover_word_timestamps": words,
            "voiceover_settings_used": {
                "voice_id": settings.get("voice_id", ""),
                "model_id": settings.get("model_id", MINIMAX_DEFAULT_MODEL),
                "speed":    settings.get("speed", 1.0),
                "volume":   settings.get("volume", 1.0),
                "pitch":    settings.get("pitch", 0),
            },
        }
        json.dump(meta, open(v_audio(cid, vid), "w"), ensure_ascii=False, indent=1)
        meta_written = True
    finally:
        if not meta_written and os.path.exists(voiceover_mp3):
            try:    os.remove(voiceover_mp3)
            except Exception: pass

    from dashboard import _PLAN_WRITE_LOCK
    with _PLAN_WRITE_LOCK:
        try:
            plan = json.load(open(v_plan(cid, vid)))
            for s in plan.get("scenes", []):
                s.pop("start_aligned", None)
                s.pop("end_aligned", None)
            json.dump(plan, open(v_plan(cid, vid), "w"), ensure_ascii=False, indent=1)
        except Exception:
            pass

    plan_thread_sec = float(settings.get("sec", 5.5)) if isinstance(settings, dict) else 5.5

    def _run():
        try:
            _transcribe_generate_worker(cid, vid, plan_thread_sec)
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"  [MiniMax→Plan] Fehler: {e}", flush=True)
    threading.Thread(target=_run, daemon=True).start()

    with _VOICE_JOBS_LOCK:
        VOICE_JOBS[(cid, vid)] = {
            "running": False, "stage": "fertig", "error": None,
            "voiceover_source": "minimax",
            "voiceover_task_id": task_id,
            "voiceover_chars": char_count,
            "ts": time.time(), "resume": False,
        }
    print(f"  [MiniMax] {cid}/{vid}: voiceover.mp3 ({len(audio_bytes)//1024} KB, "
          f"{len(words)} Wörter, chars={char_count}) — Plan-Worker gestartet", flush=True)
    return {
        "ok": True, "task_id": task_id,
        "audio_kb": len(audio_bytes) // 1024,
        "n_words": len(words),
        "chars": char_count,
    }
