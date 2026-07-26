"""routes/voice.py — Prefix /api/elevenlabs_voices, /api/tts_provider,
/api/elevenlabs_settings.

Fünfte Route-Gruppe aus dem dashboard.py-Handler (Refactor Phase 4, Teil 5).
Erste Gruppe die einen echten externen API-Call macht (ElevenLabs Voice-Liste)
-- reiner Read/Write auf Settings + ein synchroner GET-Call nach außen, aber
kein Worker-Thread.

engine_elevenlabs.py ist bereits ein eigenständiges Modul ohne dashboard.py-
Abhängigkeit (Phase-J-Extraktion, vor diesem Refactor) -- Top-Level-Import
hier ist sicher, kein Zyklus-Risiko, kein lazy `import dashboard` nötig.

Hinweis (2026-07-26, Nutzer-Rückmeldung): MiniMax (zweiter TTS-Provider,
Phase 34) war ungenutzt und wurde auf Nutzer-Wunsch restlos entfernt --
/api/minimax_voices existiert nicht mehr, ElevenLabs ist der einzige
Provider. /api/tts_provider bleibt als generische Infrastruktur bestehen
(akzeptiert nur noch "elevenlabs"), falls je ein weiterer Provider dazukommt.
"""
from __future__ import annotations

import json
import urllib.request

from engine_elevenlabs import (
    ELEVENLABS_API,
    ELEVENLABS_DEFAULT_MODEL,
    ELEVENLABS_VOICE_SETTINGS_DEFAULT,
    elevenlabs_key,
    load_voice_settings,
    save_voice_settings,
)


def handle(method, path, handler, qs, cid, vid, body):
    if method == "GET" and path == "/api/elevenlabs_voices":
        # Lists library voices + any cloned voices the account owns (account-only;
        # no env fallback here — without a key the user just gets an empty list,
        # which the dropdown renders as "configure voice first").
        try:
            req = urllib.request.Request(f"{ELEVENLABS_API}/voices",
                headers={"xi-api-key": elevenlabs_key(), "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.load(r)
            voices = data.get("voices", [])
            handler._send(200, {"voices": [{
                "voice_id": v.get("voice_id"),
                "name": v.get("name"),
                "category": v.get("category"),
                "preview_url": v.get("preview_url"),
            } for v in voices]})
        except Exception as e:
            handler._send(200, {"voices": [], "error": str(e)})
        return True, None

    if method == "GET" and path == "/api/tts_provider":
        s = load_voice_settings(cid)
        handler._send(200, {"tts_provider": s.get("tts_provider", "elevenlabs")})
        return True, None

    if method == "POST" and path == "/api/tts_provider":
        d = body or {}
        new_provider = d.get("tts_provider", "").strip()
        if new_provider not in ("elevenlabs", ""):
            handler._send(400, {"error": f"Unknown tts_provider: {new_provider}"})
            return True, None
        s = load_voice_settings(cid)
        s["tts_provider"] = new_provider
        save_voice_settings(cid, s)
        handler._send(200, {"ok": True, "tts_provider": new_provider})
        return True, None

    if method == "GET" and path == "/api/elevenlabs_settings":
        handler._send(200, load_voice_settings(cid))
        return True, None

    if method == "POST" and path == "/api/elevenlabs_settings":
        # Per-field defaults — callers may POST just one slider, everything else
        # should keep its persisted value rather than silently regressing (an empty
        # string for use_speaker_boost would turn into False via bool()).
        d = body or {}
        save_voice_settings(cid, {
            "voice_id": d.get("voice_id", ""),
            "model_id": d.get("model_id") or ELEVENLABS_DEFAULT_MODEL,
            "stability": d.get("stability") if d.get("stability") is not None else ELEVENLABS_VOICE_SETTINGS_DEFAULT["stability"],
            "similarity_boost": d.get("similarity_boost") if d.get("similarity_boost") is not None else ELEVENLABS_VOICE_SETTINGS_DEFAULT["similarity_boost"],
            "style": d.get("style") if d.get("style") is not None else ELEVENLABS_VOICE_SETTINGS_DEFAULT["style"],
            # ElevenLabs-API: speed default 1.0. Range praxisüblich 0.7–1.2.
            # Werte >1.0 sprechen schneller, <1.0 langsamer.
            "speed": d.get("speed") if d.get("speed") is not None else ELEVENLABS_VOICE_SETTINGS_DEFAULT["speed"],
            # bool fields: explicit false string is OK, missing key means "leave alone"
            "use_speaker_boost": bool(d["use_speaker_boost"]) if "use_speaker_boost" in d else ELEVENLABS_VOICE_SETTINGS_DEFAULT["use_speaker_boost"],
            "output_format": d.get("output_format") or ELEVENLABS_VOICE_SETTINGS_DEFAULT["output_format"],
        })
        handler._send(200, load_voice_settings(cid))
        return True, None

    return False, None
