"""routes/voiceover.py — Prefix /api/voiceover_file, /api/voiceover_delete,
/api/voiceover_preview, /api/voiceover_generate.

Zwölfte Route-Gruppe aus dem dashboard.py-Handler (Refactor Phase 4, Teil 12).
/api/voiceover_generate ruft _tts_persist_and_schedule SYNCHRON auf (kein
threading.Thread) -- die HTTP-Response blockt bis ElevenLabs fertig ist,
genau wie routes/voice.py (Teil 5) und routes/script_gen.py (Teil 6).
VOICE_JOBS/_VOICE_JOBS_LOCK dienen hier nur als Dedupe-/Status-Guard (für
GET /api/voiceover_status, das seit Teil 4 in routes/job_status.py lebt),
nicht als Signal für einen Hintergrund-Worker.

Keine Präfix-Überschneidung unter /api/voiceover_file|delete|preview|
generate|status geprüft (alle divergieren früh genug).

engine_elevenlabs.py ist bereits eigenständig (kein dashboard.py-Zyklus) --
Top-Level-Import für _enrich_for_tts/load_voice_settings/elevenlabs_generate/
_tts_persist_and_schedule. VOICE_JOBS/_VOICE_JOBS_LOCK (noch nicht aus
dashboard.py extrahiert) bleiben lazy importiert.
"""
from __future__ import annotations

import base64
import json
import os
import time
import traceback

from core.paths import v_audio, v_plan, v_uploads
from engine_elevenlabs import (
    _enrich_for_tts,
    _tts_persist_and_schedule,
    elevenlabs_generate,
    load_voice_settings,
)

VOICE_JOB_STALE_SEC = 900


def handle(method, path, handler, qs, cid, vid, body):
    if method == "GET" and path == "/api/voiceover_file":
        # Juli 2026 (User-Report: "generiertes Voiceover wird im Frontend nicht
        # angezeigt"): bis jetzt hatte der Server keinen Endpunkt der die
        # voiceover.mp3 aus uploads/ ausliefert — der Python http.server
        # antwortet 404 auf alles was nicht explizit gemappt ist, deshalb konnte
        # der Browser das Audio nicht abspielen obwohl die Datei auf Disk lag.
        # Diese Route streamt die MP3 mit korrektem Content-Type.
        if not vid:
            handler._send(400, {"error": "Kein Video ausgewählt"})
            return True, None
        audio_path = os.path.join(v_uploads(cid, vid), "voiceover.mp3")
        if not os.path.exists(audio_path):
            handler._send(404, {"error": "Kein Voiceover vorhanden"})
            return True, None
        try:
            with open(audio_path, "rb") as f:
                data = f.read()
            # send_response + send_header + end_headers + body — explizit weil
            # _send() nur JSON serialisiert
            handler.send_response(200)
            handler.send_header("Content-Type", "audio/mpeg")
            handler.send_header("Content-Length", str(len(data)))
            handler.send_header("Accept-Ranges", "bytes")
            # Cache-Bust: bei jedem GET andere URL, damit nach Re-Generate
            # der Browser nicht den alten Player-State behält.
            handler.send_header("Cache-Control", "no-cache")
            handler.end_headers()
            handler.wfile.write(data)
        except Exception as e:
            handler._send(500, {"error": f"Audio-Serve fehlgeschlagen: {e}"})
        return True, None

    if method != "POST":
        return False, None

    if path == "/api/voiceover_delete":
        import dashboard
        # User-Aktion "Voiceover löschen" — entfernt MP3 + audio_meta.json.
        # Der nächste /api/voiceover_generate läuft dann als echter Fresh-Call
        # (statt Resume-Pfad) weil audio_meta nicht mehr existiert.
        if not vid:
            handler._send(400, {"error": "Kein Video ausgewählt"})
            return True, None
        uploads_dir = v_uploads(cid, vid)
        for fn in ("voiceover.mp3", "voiceover_trimmed.wav", "audio_meta.json"):
            fp = os.path.join(uploads_dir, fn)
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                except OSError:
                    pass
        handler._send(200, {"ok": True})
        return True, None

    if path == "/api/voiceover_preview":
        d = body or {}
        text = (d.get("text") or "Hallo Welt, das ist ein Stimm-Sample.").strip()[:500]
        settings = {k: d.get(k) for k in (
            "voice_id", "model_id", "stability", "similarity_boost",
            "style", "speed", "use_speaker_boost", "output_format") if d.get(k) is not None}
        # Juli 2026 Fix: `settings` wurde bisher gebaut und dann bis auf voice_id
        # komplett verworfen — "Voice testen" spielte immer die zuletzt GESPEICHERTEN
        # Slider-Werte vor, nie die, die der Nutzer im Moment gerade zieht (Preview
        # sollte genau das Gegenteil sein: ein Vorhören VOR dem Speichern). Jetzt: wie
        # in _tts_persist_and_schedule werden die persistierten Settings geladen und
        # dann mit den mitgeschickten Werten überschrieben.
        final_settings = load_voice_settings(cid, override_voice_id=(
            settings.get("voice_id") if settings.get("voice_id") else ""))
        for k, v in settings.items():
            if k in final_settings:
                final_settings[k] = v
        try:
            raw = elevenlabs_generate(text, final_settings)
            audio_b64 = raw["audio_base64"]
        except Exception as e:
            handler._send(500, {"error": f"ElevenLabs Preview fehlgeschlagen: {e}"})
            return True, None
        audio_bytes = base64.b64decode(audio_b64)
        handler._send(200, audio_bytes, "audio/mpeg")
        return True, None

    if path == "/api/voiceover_generate":
        import dashboard
        if not vid:
            handler._send(400, {"error": "Kein Video ausgewählt."})
            return True, None
        d = body or {}
        text = (d.get("text") or "").strip()
        if not text:
            handler._send(400, {"error": "Kein Skript-Text für ElevenLabs — bitte erst Skript in ② eintippen."})
            return True, None
        # Phase I: enrich text with TTS-friendly pause/emphasis markers. We don't have
        # access to scene boundaries yet (those come from plan.json which is built
        # AFTER this call) — but sentence-level "..." insertion runs on the raw text
        # without needing scenes. Scene-based enrichment (climax / phase-break markers)
        # is a no-op in the first generation; will activate once plan.json exists and
        # a regenerate is triggered.
        text = _enrich_for_tts(text, scenes=None)
        # Optional sec override for downstream scene-pacing (defaults to NORMAL_HARD_CAP_SEC).
        sec = d.get("sec")
        settings = {k: d.get(k) for k in (
            "voice_id", "model_id", "stability", "similarity_boost",
            "style", "speed", "use_speaker_boost", "output_format", "sec") if d.get(k) is not None}
        # Resume-Marker: if audio_meta.json + plan.json beide schon vorhanden und
        # voiceover_source == "elevenlabs", KEIN API-Call. Idempotent wie User-Feedback
        # Punkt 3 verlangt.
        meta_path = v_audio(cid, vid)
        plan_p = v_plan(cid, vid)
        try:
            meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
        except Exception:
            meta = {}
        # Juli 2026 Fix: die vorherige Fassung dieser Bedingung war
        # "A and B if meta else False and C and D" — Pythons Operator-Vorrang liest
        # das als "(A and B) if meta else (False and C and D)", NICHT als
        # "meta and A and B and C and D" wie die Intention nahelegt. Solange meta
        # nicht-leer war, wurden C (word_timestamps vorhanden) und D (plan.json
        # existiert) dadurch NIE geprüft — ein Resume wurde schon gemeldet wenn nur
        # audio_meta.json + der Audio-Pfad existierten, auch ohne brauchbare
        # Timestamps oder überhaupt einen Plan.
        if (bool(meta)
            and meta.get("voiceover_source") == "elevenlabs"
            and os.path.exists(meta.get("path", ""))
            and meta.get("voiceover_word_timestamps")
            and os.path.exists(plan_p)):
            src = meta.get("voiceover_source")
            with dashboard._VOICE_JOBS_LOCK:
                dashboard.VOICE_JOBS[(cid, vid)] = {
                    "running": False, "stage": "fertig (resume)",
                    "error": None, "voiceover_source": src,
                    "voiceover_task_id": meta.get("voiceover_task_id"),
                    "voiceover_chars": meta.get("voiceover_chars"),
                    "ts": time.time(), "resume": True,
                }
            handler._send(200, {
                "ok": True, "task_id": meta.get("voiceover_task_id"),
                "resume": True,
                "n_words": len(meta.get("voiceover_word_timestamps") or []),
                "chars": meta.get("voiceover_chars"),
            })
            return True, None
        # Mark running BEFORE the call so a fast frontend polling loop sees a
        # state — most calls will be running for several seconds.
        # Atomic-Pre-Job-Lock (Round-5 Fix-1): verify no existing job runs. Without
        # this guard, two rapid clicks would each set running=True, both submit
        # ElevenLabs-Calls, double-bill the user, and race-write voiceover.mp3.
        with dashboard._VOICE_JOBS_LOCK:
            existing = dashboard.VOICE_JOBS.get((cid, vid), {})
            # Der Dedupe-Riegel darf nicht ewig halten. Stirbt der Request-Thread,
            # ohne running=False zu setzen (Client-Disconnect beim Reload, Laptop-
            # Zuklappen mitten im Call), bleibt das Flag stehen — und _cleanup_stale_jobs
            # fasst laufende Jobs bewusst NICHT an. Ergebnis: jeder weitere Klick auf
            # "Voiceover generieren" landet hier, bekommt ok+deduped zurück und tut
            # nichts. Im Frontend sieht das aus, als reagiere der Button gar nicht mehr,
            # und nur ein Server-Neustart half. Ein Job, der älter ist als der harte
            # ElevenLabs-Deckel (EL_HARD_DEADLINE_SEC=300s) plus Puffer, KANN nicht mehr
            # echt laufen — den überschreiben wir statt zu blockieren.
            age = time.time() - (existing.get("ts") or 0)
            if existing.get("running") and age <= VOICE_JOB_STALE_SEC:
                print(f"  [ElevenLabs] Job für {cid}/{vid} bereits in Arbeit — dupliziere nicht", flush=True)
                handler._send(200, {
                    "ok": True, "task_id": existing.get("voiceover_task_id"),
                    "deduped": True,
                    "chars": existing.get("voiceover_chars"),
                })
                return True, None
            if existing.get("running"):
                print(f"  [ElevenLabs] Verwaister Job für {cid}/{vid} (running seit "
                      f"{age/60:.1f} min, älter als {VOICE_JOB_STALE_SEC/60:.0f} min) — "
                      f"Thread ist tot, Sperre wird gelöst.", flush=True)
            dashboard.VOICE_JOBS[(cid, vid)] = {
                "running": True, "stage": "elevenlabs-generate",
                "error": None, "voiceover_source": "elevenlabs",
                "voiceover_task_id": None, "voiceover_chars": None,
                "ts": time.time(), "resume": False,
            }
        try:
            result = _tts_persist_and_schedule(cid, vid, text,
                settings=settings if sec is None else {**settings})
            handler._send(200, result)
        except Exception as e:
            traceback.print_exc()
            with dashboard._VOICE_JOBS_LOCK:
                dashboard.VOICE_JOBS[(cid, vid)] = {
                    "running": False, "stage": "error",
                    "error": str(e), "voiceover_source": "elevenlabs",
                    "ts": time.time(), "resume": False,
                }
            handler._send(500, {"error": f"ElevenLabs fehlgeschlagen: {e}"})
        return True, None

    return False, None
