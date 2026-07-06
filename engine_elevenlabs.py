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
    # Phase-Engine Constants (Phasen B-G)
    "PHASE_SET", "PHASE_TO_ACT", "PHASE_PROMPT_ADDITIONS",
    "PHASE_COLOR_FILTER", "PHASE_VOLUME", "PHASE_ACCENT",
    # Voice-Settings-Persistenz
    "ch_voice_id", "elevenlabs_key", "_resolve_voice_id",
    "load_voice_settings", "save_voice_settings",
    # API-Call + Orchestration
    "elevenlabs_generate", "_elevenlabs_persist_and_schedule",
    # TTS-Preprocessing (Phase I)
    "_enrich_for_tts", "TTS_PAUSE_BEFORE_CLIMAX", "TTS_PAUSE_AFTER_PHASE_BREAK",
]  # end __all__ — fully explicit, no auto-discovery via dir()

# Konstanten
ELEVENLABS_API           = "https://api.elevenlabs.io/v1"
ELEVENLABS_DEFAULT_MODEL = "eleven_multilingual_v2"
ELEVENLABS_KEY_FILE      = os.path.expanduser("~/.elevenlabs_key")

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
    "OPENING":       "eq=contrast=1.0:saturation=0.9:brightness=0.0",
    "RISING_ACTION": "eq=contrast=1.1:saturation=1.05:brightness=0.0",
    "CLIMAX":        "eq=contrast=1.3:saturation=1.2:brightness=-0.02",
    "RESOLUTION":    "eq=contrast=0.95:saturation=0.85:brightness=0.03",
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
    """
    enriched = text.strip()
    if not enriched:
        return enriched
    # Split on sentence boundaries (". X" with X capital). Strip trailing
    # ellipsis fragments from each part so the ' ... ' join produces at most
    # ONE separator between any two sentences — idempotent on repeated calls.
    parts = re.split(r"(?<=\.)\s+(?=[A-ZÄÖÜ])", enriched)
    cleaned = [re.sub(r"\s*\.\s*\.\s*\.\s*$", "", p) for p in parts]
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
