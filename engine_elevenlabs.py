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
import shutil
import tempfile
import urllib.request
import urllib.error
import subprocess
import threading
import concurrent.futures

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
# Audit Juli 2026 (Bereich 4, "ElevenLabs zu langsam trotz speed=1.2"): v3 zurück auf
# multilingual_v2. v3 ist das ausdrucksstarke/dramatische Modell -- Pacing läuft dort
# über Audio-Tags statt über den numerischen speed-Parameter, die natürliche Kadenz
# ist langsam mit dramatischen Pausen (offizielle Positionierung: "not suitable for
# real-time"). v2 respektiert speed zuverlässig, hat neutralere/schnellere Kadenz UND
# unterstützt Request-Stitching-Continuity (siehe previous_request_ids-Guard unten,
# der genau v3 ausschließt). Existing channels with a saved model_id in
# voice_settings.json keep their value; only freshly-resolved defaults use v2.
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
    # Audit Juli 2026 (Bereich 4): recherche-basiertes Doku-Narration-Preset für
    # multilingual_v2 -- stability 0.4 (35-40% gegen Monotonie bei langen Passagen,
    # nicht <0.30 sonst instabil), similarity_boost 0.75 (<=0.80, sonst Artefakte),
    # style 0.0 (neutral, keine Übertreibung für dokumentarischen Ton), speed 1.1
    # (dynamischer YouTube-Takt; v2 respektiert speed zuverlässig, anders als v3).
    "stability": 0.4,
    "similarity_boost": 0.75,
    "style": 0.0,
    "use_speaker_boost": True,
    "speed": 1.1,   # ElevenLabs-API: 0.7–1.2 praxisüblich, >1.0 schneller
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
                # July 2026 (Bug-Fix): preserve tts_provider from saved settings so the
                # frontend can match it against the current provider select (see dashboard.html
                # ~1650 — "settings.tts_provider === provider"). Without this field the condition
                # is never true and the saved voice_id never gets preselected into elVoiceSelect.
                if "tts_provider" in saved:
                    s["tts_provider"] = saved["tts_provider"]
        except Exception as e:
            print(f"  [ElevenLabs] voice_settings.json unlesbar ({e}) — nutze Defaults", flush=True)
    s["voice_id"] = _resolve_voice_id(cid, override_voice_id)
    if "tts_provider" not in s:
        s["tts_provider"] = "elevenlabs"
    return s

def save_voice_settings(cid: str, settings: dict) -> None:
    clean = dict(ELEVENLABS_VOICE_SETTINGS_DEFAULT)
    clean.update({k: settings[k] for k in settings if k in clean and k != "voice_id"})
    for k in ("stability", "similarity_boost", "style"):
        try:    clean[k] = max(0.0, min(1.0, float(clean[k])))
        except Exception: clean[k] = ELEVENLABS_VOICE_SETTINGS_DEFAULT[k]
    # ElevenLabs-Docs (elevenlabs.io/docs/eleven-agents/customization/voice/speed-control):
    # offizieller Bereich 0.7–1.2. Werte außerhalb wurden bisher unclamped an die API
    # weitergereicht — riskiert HTTP 400 mitten im (teuren) Chunked-Call. Der UI-Slider
    # geht jetzt selbst nicht mehr über 1.2, aber Direkt-Aufrufe von /api/elevenlabs_settings
    # (oder alte, im Browser gecachte Seiten mit dem 1.3-Slider) sollen serverseitig
    # ebenfalls nie einen ungültigen Wert persistieren.
    try:    clean["speed"] = max(0.7, min(1.2, float(clean["speed"])))
    except Exception: clean["speed"] = ELEVENLABS_VOICE_SETTINGS_DEFAULT["speed"]
    clean["use_speaker_boost"] = bool(settings.get("use_speaker_boost", True))
    if settings.get("model_id"):
        clean["model_id"] = str(settings["model_id"])
    # tts_provider + MiniMax-Felder (volume/pitch) lebten bisher nicht in
    # ELEVENLABS_VOICE_SETTINGS_DEFAULT und wurden vom `k in clean`-Filter oben
    # verworfen — jeder Provider-Wechsel via /api/tts_provider persistierte dadurch
    # nie (nächster Server-Neustart/Reload zeigte wieder "elevenlabs"). Explizit
    # durchreichen, unabhängig vom Default-Dict.
    if settings.get("tts_provider"):
        clean["tts_provider"] = str(settings["tts_provider"])
    for k in ("volume", "pitch"):
        if settings.get(k) is not None:
            try:    clean[k] = float(settings[k])
            except Exception: pass
    vid = str(settings.get("voice_id", "")).strip()
    if vid:
        with open(ch_voice_id(cid), "w") as f:
            f.write(vid + "\n")
    json.dump(clean, open(ch_voice_settings(cid), "w"), ensure_ascii=False, indent=1)


# API-Call mit Retry
EL_BACKOFF_SEC = [5, 10, 20]
EL_IDLE_TIMEOUT_SEC = 180   # Socket-Idle: max. Stille während ElevenLabs 1 Chunk generiert
EL_HARD_DEADLINE_SEC = 300  # Watchdog-Deckel pro HTTP-Anfrage (feuert auch bei totem Socket)

# Ein einziger, prozessweiter Executor für alle HTTP-Watchdogs. Ein per-Call abgehängter
# Worker-Thread (harter Timeout griff) läuft im Hintergrund weiter, bis sein eigener
# Socket-Idle-Timeout (EL_IDLE_TIMEOUT_SEC) ihn beendet — er blockiert NICHT den Retry.
_HTTP_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="tts-http")


def _urlopen_json(url: str, body: dict, headers: dict) -> tuple:
    """Führt EINE POST-Anfrage aus und liefert (response_json, request-id-header).
    Socket-Idle-Timeout = EL_IDLE_TIMEOUT_SEC. Läuft im Watchdog-Worker-Thread."""
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
        headers={**headers, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=EL_IDLE_TIMEOUT_SEC) as r:
        return json.load(r), r.headers.get("request-id")


def _elevenlabs_call_with_retry(url: str, body: dict, headers: dict) -> tuple:
    """Returns (response_json, request_id). request_id comes from the response's
    `request-id` header — the ONLY valid way to obtain it for `previous_request_ids`
    stitching (ElevenLabs docs: request IDs are not part of the JSON body).
    """
    last_err = None
    for attempt, wait in enumerate([0] + EL_BACKOFF_SEC):
        if wait:
            print(f"  [ElevenLabs] Retry {attempt}/{len(EL_BACKOFF_SEC)} in {wait}s …", flush=True)
            time.sleep(wait)
        try:
            fut = _HTTP_EXECUTOR.submit(_urlopen_json, url, body, headers)
            return fut.result(timeout=EL_HARD_DEADLINE_SEC)
        except concurrent.futures.TimeoutError:
            # Harter Deckel griff — Socket hängt (Idle-Timeout feuerte nicht). Der
            # abgehängte Worker beendet sich selbst via EL_IDLE_TIMEOUT_SEC. Retry.
            print(f"  [ElevenLabs] Harter Timeout nach {EL_HARD_DEADLINE_SEC}s — "
                  f"Verbindung abgebrochen, Retry …", flush=True)
            last_err = RuntimeError(f"ElevenLabs Netzwerkfehler: harter Timeout nach {EL_HARD_DEADLINE_SEC}s")
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
        try:
            fut = _HTTP_EXECUTOR.submit(_urlopen_json, url, body, headers)
            return fut.result(timeout=EL_HARD_DEADLINE_SEC)[0]
        except concurrent.futures.TimeoutError:
            print(f"  [MiniMax] Harter Timeout nach {EL_HARD_DEADLINE_SEC}s — "
                  f"Verbindung abgebrochen, Retry …", flush=True)
            last_err = RuntimeError(f"MiniMax Netzwerkfehler: harter Timeout nach {EL_HARD_DEADLINE_SEC}s")
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

def _el_words_from_alignment(alignment: dict) -> list:
    """Baut Wort-Timestamps aus einer ElevenLabs-`/with-timestamps`-Antwort.

    ElevenLabs liefert ZEICHEN-basiertes Alignment:
        alignment.characters                    -> ["H","e","l","l","o"," ","W",...]
        alignment.character_start_times_seconds -> [0.0, 0.05, ...]
        alignment.character_end_times_seconds   -> [0.05, 0.1, ...]
    NICHT `alignment.words`. Diese Funktion aggregiert Zeichen an Whitespace-Grenzen
    zu Wörtern. Falls eine Antwort doch ein fertiges `words`-Feld hat (anderes/älteres
    Schema), wird das bevorzugt. Return: [{"word","start","end"}], Wort-Reihenfolge.
    """
    if not isinstance(alignment, dict):
        return []

    # Fall 1: fertige Wortliste (Legacy/anderes Schema) — bevorzugen
    words = alignment.get("words")
    if isinstance(words, list) and words:
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
        if norm:
            return norm

    # Fall 2: Zeichen-basiertes Alignment -> zu Wörtern aggregieren
    chars  = alignment.get("characters")
    starts = alignment.get("character_start_times_seconds")
    ends   = alignment.get("character_end_times_seconds")
    if not (isinstance(chars, list) and isinstance(starts, list) and isinstance(ends, list)):
        return []
    n = min(len(chars), len(starts), len(ends))
    norm = []
    buf = {"txt": "", "start": None, "end": None}

    def _flush():
        t = buf["txt"].strip()
        if t and buf["start"] is not None:
            s = max(0.0, float(buf["start"])); e = max(s + 0.01, float(buf["end"]))
            norm.append({"word": t, "start": s, "end": e})
        buf["txt"] = ""; buf["start"] = None; buf["end"] = None

    for i in range(n):
        ch = chars[i]
        if not isinstance(ch, str):
            continue
        if ch.isspace():
            _flush()
            continue
        if buf["start"] is None:
            try:    buf["start"] = float(starts[i])
            except (TypeError, ValueError): buf["start"] = 0.0
        buf["txt"] += ch
        try:    buf["end"] = float(ends[i])
        except (TypeError, ValueError): buf["end"] = (buf["start"] or 0.0) + 0.01
    _flush()
    return norm


# Juli 2026: ElevenLabs /with-timestamps lehnt Texte > 5000 Zeichen ab (HTTP 400
# "text_too_long"). Lange Skripte (z.B. Theranos-Story 5788 Zeichen) splitten wir
# automatisch an Satzgrenzen, generieren jeden Chunk einzeln mit Continuity via
# previous_request_ids (offizielle API für multi-segment stitching), konkatenieren die
# MP3-Streams mit ffmpeg -c copy (kein Re-Encode, also kein Qualitätsverlust), und
# verschieben die Word-Timestamps kumulativ.
EL_CHUNK_CHAR_LIMIT    = 4800   # Sicherheitsabstand zur 5000er API-Grenze (eleven_v3/multilingual_v2)
EL_CHUNK_OVERLAP_CHARS = 0      # keine Overlap nötig bei Satzgrenzen-Split
EL_CONTINUITY_WINDOW   = 1      # wieviele vorherige request_ids pro Chunk mitschicken

# Modellabhängiges Zeichenlimit pro Request (ElevenLabs-Docs, Stand 2026): eleven_v3
# akzeptiert nur ~5000 Zeichen, multilingual_v2 10000, flash/turbo bis 30-40k. Wir bleiben
# jeweils mit Sicherheitsabstand drunter. Unbekannte Modelle fallen auf den v3-Wert zurück
# (konservativste Annahme).
EL_CHUNK_LIMIT_BY_MODEL_PREFIX = [
    ("eleven_v3", 4800),
    ("eleven_multilingual_v2", 9500),
    ("eleven_flash", 28000),
    ("eleven_turbo", 28000),
]


def _chunk_limit_for_model(model_id: str) -> int:
    model_id = model_id or ""
    for prefix, limit in EL_CHUNK_LIMIT_BY_MODEL_PREFIX:
        if model_id.startswith(prefix):
            return limit
    return EL_CHUNK_CHAR_LIMIT


def _mp3_duration_sec(path: str) -> float:
    """ffprobe-Helper, gleiches Muster wie engine/render.py:_clip_duration_sec —
    misst die ECHTE Audiodauer statt sich auf ein geschätztes Wort-Ende zu verlassen.
    ElevenLabs hängt an jeden Chunk etwas Stille an; die Differenz zwischen 'letztes
    Wort-Ende' und 'echtes Dateiende' akkumuliert sich über mehrere Chunk-Grenzen zu
    hörbarem Sync-Drift (Juli 2026, User-Report: Schnitt liegt spürbar vor dem Wort)."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                           "-of", "csv=p=0", path], capture_output=True, text=True, timeout=15)
    return float(out.stdout.strip())


def _chunk_text_by_sentences(text: str, max_chars: int = EL_CHUNK_CHAR_LIMIT) -> list:
    """Zerlegt text in eine Liste von Strings, jeweils ≤ max_chars, immer an Satzgrenzen
    (`. `, `? `, `! `, neue Zeile). Kein Chunk enthält nur einen Teil eines Satzes.

    Algorithmus:
    1. Split am Sentence-Delimiter-Pattern, behalte Delimiter im jeweiligen Satz
    2. Greedy-Pack: hänge Sätze an den aktuellen Chunk solange er ≤ max_chars bleibt
    3. Wenn ein einzelner Satz > max_chars ist (sehr lange Dialogzeile o.ä.), wird er
       als eigener Chunk zurückgegeben — ElevenLabs akzeptiert dann entweder oder
       lehnt mit text_too_long ab, was wir sauber als Fehler weitergeben.
    """
    import re
    if len(text) <= max_chars:
        return [text]
    # Split an Satzgrenzen — Delimiter bleibt am vorherigen Satz
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    chunks, current = [], ""
    for p in parts:
        candidate = (current + " " + p).strip() if current else p
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # Wenn der Satz selbst > max_chars ist: eigener Chunk, kommt durch oder failt klar
            current = p
    if current:
        chunks.append(current)
    return chunks


def _concat_mp3_files(file_list: list, output_path: str) -> None:
    """Konkateniert mehrere MP3-Dateien verlustfrei mit ffmpeg -c copy.

    ffmpeg -c copy re-encoded NICHT — das ist kritisch damit an den Boundaries kein
    Qualitätsverlust entsteht. Voraussetzung: ffmpeg im PATH (ist es auf diesem System
    ohnehin fürs Rendering).
    """
    if len(file_list) == 1:
        # Nur ein Chunk — kein Concat nötig, einfach verschieben/kopieren
        import shutil
        shutil.move(file_list[0], output_path)
        return
    list_path = output_path + ".list.txt"
    with open(list_path, "w") as f:
        for path in file_list:
            # ffmpeg concat demuxer verlangt 'file'-Zeilen mit absoluten oder relativen Pfaden
            f.write(f"file '{os.path.abspath(path)}'\n")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
             "-c", "copy", output_path],
            check=True, capture_output=True, timeout=120,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"ffmpeg concat fehlgeschlagen: stderr={e.stderr.decode('utf-8', errors='replace')[:500]}"
        )
    finally:
        try: os.remove(list_path)
        except: pass


def elevenlabs_generate(text: str, settings: dict) -> dict:
    """ElevenLabs TTS mit Auto-Chunking für lange Skripte.

    Bei text-Länge > EL_CHUNK_CHAR_LIMIT:
      - Split an Satzgrenzen (immer ganze Sätze, keine Wort-Bruchstücke)
      - Sequenzielle Calls, jeder Chunk bekommt previous_request_ids der letzten
        EL_CONTINUITY_WINDOW Chunks (offizieller Continuity-Mechanismus der API)
      - MP3-Streams verlustfrei via ffmpeg -c copy konkateniert
      - Word-Timestamps kumulativ verschoben (Chunk-N start += Chunk-(N-1) end_time)

    Bei text-Länge ≤ Limit: ein einzelner Call wie vorher.
    Return-Shape bleibt identisch zur Single-Call-Version (audio_base64, words, task_id).
    """
    voice_id = settings.get("voice_id") or ""
    if not voice_id:
        raise RuntimeError("Keine voice_id konfiguriert: ~/.elevenlabs_key oder channels/<cid>/voice_id.txt befüllen.")

    # Modellabhängiges Limit (eleven_v3 ≈ 4800, multilingual_v2 ≈ 9500, flash/turbo höher) —
    # muss VOR dem Single-vs-Chunked-Entscheid berechnet werden, sonst chunkt ein
    # 8000-Zeichen-Text mit multilingual_v2 unnötig, obwohl das Modell ihn in einem
    # Call verarbeiten könnte.
    model_id_for_limit = settings.get("model_id") or ELEVENLABS_DEFAULT_MODEL
    chunk_limit = _chunk_limit_for_model(model_id_for_limit)

    # Single-Call-Pfad wenn text klein genug
    if len(text) <= chunk_limit:
        return _elevenlabs_generate_single(text, settings, voice_id)

    # Chunked-Pfad
    chunks = _chunk_text_by_sentences(text, chunk_limit)
    print(f"  [ElevenLabs] Auto-Chunking: {len(text)} Zeichen → {len(chunks)} Chunks "
          f"({[len(c) for c in chunks]}, Limit={chunk_limit} für {model_id_for_limit})", flush=True)

    key = elevenlabs_key()
    url = f"{ELEVENLABS_API}/text-to-speech/{voice_id}/with-timestamps"
    base_headers = {"xi-api-key": key, "Accept": "application/json"}

    tmpdir = tempfile.mkdtemp(prefix="elevenlabs_chunk_")
    try:
        chunk_results = []   # [{"file": ..., "words": [...], "request_id": ..., "duration": ...}, ...]
        for idx, chunk_text in enumerate(chunks):
            body = {
                "text": chunk_text,
                "model_id": model_id_for_limit,
                "voice_settings": {
                    "stability": float(settings.get("stability", 0.5)),
                    "similarity_boost": float(settings.get("similarity_boost", 0.75)),
                    "style": float(settings.get("style", 0.0)),
                    "use_speaker_boost": bool(settings.get("use_speaker_boost", True)),
                    # ElevenLabs-API-Schema: speed default 1.0, offizieller Bereich 0.7–1.2
                    # (siehe elevenlabs.io/docs/eleven-agents/customization/voice/speed-control).
                    # save_voice_settings() clampt bereits auf diesen Bereich; float() hier ist
                    # nur die letzte Absicherung falls settings direkt (ohne save) übergeben wurde.
                    "speed": float(settings.get("speed", 1.0)),
                },
                "output_format": settings.get("output_format") or "mp3_44100_128",
            }
            # Continuity: die letzten EL_CONTINUITY_WINDOW request_ids der vorherigen Chunks
            # mitschicken. ACHTUNG (2026-07-09): ElevenLabs lehnt previous_request_ids für
            # "eleven_v3" mit unsupported_model ab — der Continuity-Mechanismus ist für v3
            # noch nicht freigeschaltet. Wir prüfen das pro Call und lassen die Felder bei
            # v3 weg.
            model_id = body.get("model_id", "")
            if not model_id.startswith("eleven_v3"):
                prev_ids = [r["request_id"] for r in chunk_results[-EL_CONTINUITY_WINDOW:]
                            if r.get("request_id")]
                if prev_ids:
                    body["previous_request_ids"] = prev_ids

            resp, request_id = _elevenlabs_call_with_retry(url, body, base_headers)
            if not isinstance(resp, dict):
                raise RuntimeError(f"ElevenLabs-Antwort ist kein JSON-Objekt: {type(resp).__name__}")
            if not resp.get("audio_base64"):
                raise RuntimeError(f"ElevenLabs antwortete ohne audio_base64 (Chunk {idx+1}/{len(chunks)})")

            alignment = resp.get("alignment") or resp.get("normalized_alignment") or {}
            words = _el_words_from_alignment(alignment)
            if not words:
                raise RuntimeError(
                    f"ElevenLabs Chunk {idx+1}/{len(chunks)} antwortete ohne Alignment."
                )
            audio_bytes = base64.b64decode(resp["audio_base64"])
            chunk_path = os.path.join(tmpdir, f"chunk_{idx:03d}.mp3")
            with open(chunk_path, "wb") as f:
                f.write(audio_bytes)
            # Juli 2026 Fix (User-Report: Schnitt liegt vor dem gesprochenen Wort): die
            # Dauer eines Chunks ist NICHT das Ende des letzten Worts — ElevenLabs hängt
            # etwas Stille ans Chunk-Ende an, die in KEINEM Wort-Timestamp auftaucht. Über
            # mehrere Chunk-Grenzen akkumuliert sich das zu hörbarem Sync-Drift. Die echte
            # ffprobe-Dauer der MP3-Datei ist die einzige verlässliche Quelle für den
            # kumulativen Offset der folgenden Chunks.
            chunk_duration = _mp3_duration_sec(chunk_path)
            chunk_results.append({
                "file": chunk_path,
                "words": words,
                "request_id": request_id,
                "duration": chunk_duration,
            })
            print(f"  [ElevenLabs] Chunk {idx+1}/{len(chunks)}: {len(audio_bytes)} bytes, "
                  f"{len(words)} words, {chunk_duration:.3f}s (letztes Wort endet bei "
                  f"{words[-1]['end']:.3f}s)", flush=True)

        # MP3-Konkatenation via ffmpeg -c copy (verlustfrei)
        out_path = os.path.join(tmpdir, "concatenated.mp3")
        _concat_mp3_files([r["file"] for r in chunk_results], out_path)
        with open(out_path, "rb") as f:
            combined_b64 = base64.b64encode(f.read()).decode("ascii")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # Timestamps kumulativ verschieben — Offset ist die ECHTE (ffprobe-gemessene) Dauer
    # jedes vorherigen Chunks, nicht das geschätzte letzte Wort-Ende (siehe oben).
    combined_words = []
    cumulative_offset = 0.0
    for r in chunk_results:
        for w in r["words"]:
            combined_words.append({
                "word": w["word"],
                "start": round(w["start"] + cumulative_offset, 3),
                "end":   round(w["end"]   + cumulative_offset, 3),
            })
        cumulative_offset += r["duration"]

    return {
        "audio_base64": combined_b64,
        "words": combined_words,
        "task_id": f"el_{voice_id[:8]}_chunked_{int(time.time())}",
        "n_chunks": len(chunks),
    }


def _elevenlabs_generate_single(text: str, settings: dict, voice_id: str) -> dict:
    """Single-Call-Pfad für Texte ≤ EL_CHUNK_CHAR_LIMIT."""
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
            "speed": float(settings.get("speed", 1.0)),
        },
        "output_format": settings.get("output_format") or "mp3_44100_128",
    }
    headers = {"xi-api-key": key, "Accept": "application/json"}
    resp, _request_id = _elevenlabs_call_with_retry(url, body, headers)

    if not isinstance(resp, dict):
        raise RuntimeError(f"ElevenLabs-Antwort ist kein JSON-Objekt: {type(resp).__name__}")
    if not resp.get("audio_base64"):
        raise RuntimeError("ElevenLabs antwortete ohne audio_base64 — bitte erneut versuchen.")
    alignment = resp.get("alignment") or resp.get("normalized_alignment") or {}
    norm = _el_words_from_alignment(alignment)
    if not norm:
        raise RuntimeError(
            "ElevenLabs antwortete ohne verwertbares Alignment (weder alignment.words "
            "noch character-level alignment.characters/*_times_seconds) — bitte erneut "
            "versuchen (Provider-Schema-Drift oder leerer Text?)."
        )
    return {
        "audio_base64": resp["audio_base64"],
        "words": norm,
        "task_id": f"el_{voice_id[:8]}_{int(time.time())}",
        "n_chunks": 1,
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
    # Lazy-import (Import-Zyklus vermeiden) — wie in den Schwester-Funktionen.
    from dashboard import ensure_video
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


def _plan_has_usable_scenes(v_plan_path: str) -> bool:
    """True wenn unter v_plan_path bereits ein Plan mit echten, promptierten Szenen
    liegt (z.B. über 'Plan aus Skript erstellen' manuell gebaut und geprüft).

    Juli 2026 Fix (User-Report: "Voiceover generieren zerstört meinen geprüften Plan"):
    _transcribe_generate_worker baut bei jedem ElevenLabs-Call den KOMPLETTEN Plan neu
    (andere Segmentierung: feste Zeitfenster statt Satz-/Pacing-bewusst, neue Prompts).
    Das ist nur dann ein "erster Aufbau" (harmlos), wenn noch KEIN brauchbarer Plan
    existiert — der voice-first-Workflow, für den dieser Auto-Rebuild ursprünglich
    gebaut wurde. Existiert schon ein Plan mit Prompts, ist der Rebuild destruktiv und
    ohne Nutzen: der Render braucht die Zeitstempel nicht aus plan.json, sondern liest
    sie direkt aus audio_meta.json (siehe dashboard.py _render_worker,
    `elevenlabs_words = audio_meta.get("voiceover_word_timestamps")`)."""
    try:
        plan = json.load(open(v_plan_path))
    except Exception:
        return False
    scenes = plan.get("scenes") or []
    return any((s.get("prompt") or "").strip() for s in scenes)


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
                "speed":            final_settings.get("speed"),
                "use_speaker_boost": final_settings.get("use_speaker_boost"),
                "n_chunks":         raw.get("n_chunks", 1),
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

    # Juli 2026 Fix: Plan-Rebuild NUR im voice-first-Fall (noch kein promptierter Plan).
    # Existiert schon ein manuell erstellter/geprüfter Plan (z.B. via "Plan aus Skript
    # erstellen"), bleibt er unangetastet — Render liest die Zeitstempel ohnehin direkt
    # aus audio_meta.json, ein Rebuild bringt hier nur Schaden (andere Segmentierung,
    # neue Prompts, verwaiste Bilder), keinen Nutzen. Siehe _plan_has_usable_scenes().
    plan_already_usable = _plan_has_usable_scenes(v_plan(cid, vid))
    if not plan_already_usable:
        plan_thread_sec = float(settings.get("sec", 5.5)) if isinstance(settings, dict) else 5.5
        def _run():
            try:
                _transcribe_generate_worker(cid, vid, plan_thread_sec)
            except Exception as e:
                import traceback; traceback.print_exc()
                print(f"  [ElevenLabs→Plan] Fehler in _transcribe_generate_worker: {e}", flush=True)
        threading.Thread(target=_run, daemon=True).start()
    else:
        print(f"  [ElevenLabs] {cid}/{vid}: bereits promptierter Plan gefunden — "
              f"Plan-Rebuild übersprungen (Alignment läuft beim Render direkt gegen "
              f"audio_meta.json).", flush=True)

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
            "plan_rebuilt": not plan_already_usable,
        }
    print(f"  [ElevenLabs] {cid}/{vid}: voiceover.mp3 ({len(audio_bytes)//1024} KB, "
          f"{len(words)} Wörter, chars={char_count})"
          + (" — Plan-Worker gestartet" if not plan_already_usable else " — Plan erhalten"),
          flush=True)
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

    # Juli 2026 Fix: gleiche Entkopplung wie im ElevenLabs-Zwilling — Rebuild nur wenn
    # noch kein promptierter Plan existiert (siehe _plan_has_usable_scenes()).
    plan_already_usable = _plan_has_usable_scenes(v_plan(cid, vid))
    if not plan_already_usable:
        plan_thread_sec = float(settings.get("sec", 5.5)) if isinstance(settings, dict) else 5.5

        def _run():
            try:
                _transcribe_generate_worker(cid, vid, plan_thread_sec)
            except Exception as e:
                import traceback; traceback.print_exc()
                print(f"  [MiniMax→Plan] Fehler: {e}", flush=True)
        threading.Thread(target=_run, daemon=True).start()
    else:
        print(f"  [MiniMax] {cid}/{vid}: bereits promptierter Plan gefunden — "
              f"Plan-Rebuild übersprungen.", flush=True)

    with _VOICE_JOBS_LOCK:
        VOICE_JOBS[(cid, vid)] = {
            "running": False, "stage": "fertig", "error": None,
            "voiceover_source": "minimax",
            "voiceover_task_id": task_id,
            "voiceover_chars": char_count,
            "ts": time.time(), "resume": False,
            "plan_rebuilt": not plan_already_usable,
        }
    print(f"  [MiniMax] {cid}/{vid}: voiceover.mp3 ({len(audio_bytes)//1024} KB, "
          f"{len(words)} Wörter, chars={char_count})"
          + (" — Plan-Worker gestartet" if not plan_already_usable else " — Plan erhalten"),
          flush=True)
    return {
        "ok": True, "task_id": task_id,
        "audio_kb": len(audio_bytes) // 1024,
        "n_words": len(words),
        "chars": char_count,
    }
