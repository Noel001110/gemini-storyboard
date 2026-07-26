"""workers/render.py — Render-Worker (aus dashboard.py extrahiert, Refactor
Phase 3). Siehe workers/__init__.py für die lazy-import-Konvention.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import traceback

from core.logging import log_event as _log
from core.paths import v_audio, v_out, v_plan, v_render_tmp, v_uploads
from engine.render import (
    RENDER_FPS,
    RENDER_TARGETS,
    _apply_sync_invariant,
    _assemble_clips,
    _crossfade_clips,
    _has_transition_before,
    _motion_for_scene,
    _mux_audio,
    _render_clip,
    _render_selfcheck,
    _transition_for_scene,
)


def run(cid: str, vid: str, target: str = "longform") -> None:
    """Orchestrates: prepare (sync invariant) -> motion -> clips (one Ken Burns clip per
    scene, resume-safe) -> assemble (concat) -> audio (mux) -> review (ffprobe checks).
    Sequential — one ffmpeg process at a time, no thread pool over rendering (that would
    oversubscribe the CPU well beyond what the images' 8-way concurrency already does).

    `target` (Shorts-Erweiterung): key wird (cid, vid, target) und render_dir bekommt
    einen zielspezifischen Unterordner, damit ein Shorts- und ein Longform-Render
    desselben Videos nie denselben RENDER_JOBS-Eintrag oder dieselben render_tmp/-Dateien
    teilen. Default hält den einen bestehenden Aufruf ohne target unverändert lauffähig."""
    import dashboard  # lazy, siehe workers/__init__.py

    key = (cid, vid, target)
    render_target = RENDER_TARGETS[target]
    # Nicht-Longform-Targets (Shorts) rendern aus einem eigenen, von shorts/api.py
    # geschriebenen Plan + einer eigenen (kürzeren, bereits geschnittenen) Audiospur --
    # siehe shorts/clip_select.py. Longform-Pfade bleiben dadurch komplett unangetastet.
    if target == "longform":
        plan_path = v_plan(cid, vid)
    else:
        plan_path = os.path.join(v_out(cid, vid), f"{target}_plan.json")
    render_dir = os.path.join(v_render_tmp(cid, vid), target)
    os.makedirs(render_dir, exist_ok=True)

    def stage(name, done=0, total=0):
        with dashboard._RENDER_JOBS_LOCK:
            if key in dashboard.RENDER_JOBS:
                dashboard.RENDER_JOBS[key].update(stage=name, done=done, total=total)

    def stop_requested():
        with dashboard._RENDER_JOBS_LOCK:
            return dashboard.RENDER_JOBS.get(key, {}).get("stop_requested", False)

    cleanup_done = False
    try:
        try:
            plan = json.load(open(plan_path))
        except Exception as e:
            raise RuntimeError(f"Plan lesen: {e}")

        scenes = [s for s in plan["scenes"] if s.get("file")]
        if not scenes:
            raise RuntimeError("Keine generierten Bilder vorhanden — erst Bilder generieren.")

        stage("prepare")
        try:
            if target == "longform":
                audio_meta = json.load(open(v_audio(cid, vid)))
            else:
                audio_meta = json.load(open(os.path.join(v_uploads(cid, vid), f"{target}_audio_meta.json")))
            audio_path = audio_meta.get("path", "")
        except Exception:
            audio_path = ""
        if not audio_path or not os.path.exists(audio_path):
            raise RuntimeError("Kein hochgeladenes Voice-over gefunden — Rendern braucht eine durchgehende Audiospur.")

        # Whisper-Alignment + Pausen-Kürzung laufen HIER, nicht im /api/transcribe-
        # Handler -- an dieser Stelle liegt IMMER ein hochgeladenes Voice-over vor,
        # unabhängig davon, ob die Szenen-Texte ursprünglich aus der Audio-
        # Transkription (Option A) oder dem manuellen Skript-Pfad (Option B, geschätzte
        # WPM-Timeline) stammen. Ein einziger Punkt statt zwei getrennter, damit BEIDE
        # Pfade dieselbe frame-genaue Timing-Qualität bekommen (ARCHITECTURE.md 16.5).
        # Die gekürzte Audiospur wird NEBEN dem Original abgelegt (v_uploads, nicht
        # render_tmp/, das nach jedem Render gelöscht wird) -- ihre Existenz ist damit
        # selbst der Resume-Marker: schon getrimmt + Szenen schon ausgerichtet heißt
        # kein erneuter Whisper-Lauf bei einem Wiederholungs-Render.
        trimmed_name = "voiceover_trimmed.wav" if target == "longform" else f"{target}_voiceover_trimmed.wav"
        trimmed_audio_path = os.path.join(v_uploads(cid, vid), trimmed_name)
        # Cinematic-Mix Juli 2026 (Schritt 3): auch neu alignen, wenn start_aligned
        # zwar schon gesetzt ist, aber `words` (Wort-Slices für die 1-Wort-Captions)
        # fehlt -- z.B. bei einem VOR diesem Feature bereits erfolgreich gerenderten
        # Video. Ohne diesen Zusatz-Check würde die Resume-Optimierung unten stillschweigend
        # überspringen und die alten Bauchbinden-Captions blieben für immer aktiv.
        needs_alignment = any(s.get("start_aligned") is None or not s.get("words") for s in scenes)
        # ElevenLabs fast-path: when audio_meta.json carries word timestamps (Phase 1),
        # we still need pause-trim, but the SLOW step (Whisper transcription) is replaced
        # by the provider-side timestamps we already captured at generate-time. The same
        # _compute_pause_trims / _adjust_words_for_trims / align_scenes_to_whisper chain
        # then runs unchanged — both code paths produce identical scene timing.
        elevenlabs_words = audio_meta.get("voiceover_word_timestamps") if audio_meta else None
        if needs_alignment or not os.path.exists(trimmed_audio_path):
            stage("timing")
            try:
                if isinstance(elevenlabs_words, list) and elevenlabs_words:
                    whisper_words = [{"word": w["word"], "start": w["start"], "end": w["end"]}
                                     for w in elevenlabs_words]
                    whisper_lang = "elevenlabs"
                    whisper_prob = 1.0
                else:
                    whisper_result = dashboard.transcribe_words_whisper(audio_path)
                    whisper_words = whisper_result["words"]
                    whisper_lang  = whisper_result.get("language", "unknown")
                    whisper_prob  = whisper_result.get("language_probability", 0.0)
                # Juli 2026 Fix: "..."-Enrichment-Marker aus der ElevenLabs-Wortliste
                # entfernen, BEVOR Pausen-Trim und Alignment darauf laufen — siehe
                # _strip_pause_tokens() Docstring. No-Op für Whisper (kennt solche
                # Tokens nicht), also gefahrlos für beide Pfade.
                n_before_strip = len(whisper_words)
                whisper_words = dashboard._strip_pause_tokens(whisper_words)
                if len(whisper_words) != n_before_strip:
                    print(f"  [{'ElevenLabs' if whisper_lang == 'elevenlabs' else 'Whisper'}] "
                          f"{n_before_strip - len(whisper_words)} Pausen-Marker-Token(s) vor "
                          f"Alignment entfernt ({n_before_strip} → {len(whisper_words)} Wörter).",
                          flush=True)
                trims = dashboard._compute_pause_trims(whisper_words)
                dashboard._trim_audio_pauses(audio_path, trims, trimmed_audio_path)
                adjusted_words = dashboard._adjust_words_for_trims(whisper_words, trims)
                dashboard.align_scenes_to_whisper(scenes, adjusted_words)
                trimmed_total = sum(b - a for a, b in trims)
                source_tag = "ElevenLabs" if isinstance(elevenlabs_words, list) and elevenlabs_words else "Whisper"
                print(f"  [{source_tag}] {len(whisper_words)} Wörter ausgerichtet, "
                      f"{len(trims)} Pause(n) auf {dashboard.MAX_PAUSE_SEC}s gekürzt (-{trimmed_total:.1f}s) "
                      f"(Sprache: {whisper_lang}, p={whisper_prob})",
                      flush=True)

                # Phase O: Wort-Akzent-Puls -- User-Feedback (Klimax-Short-Test): der Gauß-
                # Zoom-Puls (_render_clip, engine/render.py) wirkt in CLIMAX-lastigem
                # Material wie ein störendes "Herzschlag"-Zittern. Deaktiviert: accent_t
                # wird nie mehr gesetzt, _render_clip's Puls-Zweig (`if accent_t is not
                # None`) greift dadurch nie mehr. _is_accent_eligible/_compute_accent_t
                # bleiben als toter, jederzeit reaktivierbarer Code erhalten.
                for s in scenes:
                    s.pop("accent_t", None)
            except Exception as e:
                # Graceful degradation: scenes keep their estimated start/dur, the
                # sync invariant and SFX timing simply fall back to those (both
                # already check start_aligned/end_aligned for None before using them),
                # and the raw (untrimmed) upload is used for muxing below.
                print(f"  [Whisper] Übersprungen, geschätzte Timeline bleibt aktiv: {e}", flush=True)

        # Everything downstream (sync invariant, sound design, final mux) uses the
        # pause-trimmed audio whenever it exists — it's the same recording, just
        # shorter, so nothing about "which audio is the real one" changes for the
        # viewer, only that dead air is gone.
        if os.path.exists(trimmed_audio_path) and os.path.getsize(trimmed_audio_path) > 0:
            audio_path = trimmed_audio_path

        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "csv=p=0", audio_path], capture_output=True, text=True, timeout=15)
        audio_duration = float(probe.stdout.strip())

        frames = _apply_sync_invariant(scenes, audio_duration, RENDER_FPS)
        for s, f in zip(scenes, frames):
            s["_frames"] = f

        stage("motion")
        # Immer frisch berechnen, nicht cachen: _motion_for_scene ist reine, günstige
        # Regellogik (kein LLM-Call) -- ein "if not s.get('motion')"-Guard würde nach
        # einer Motion-Bibliotheks-Änderung (wie der Juli-2026-Bereinigung) die ALTE,
        # in plan.json gespeicherte Motion für immer weiter benutzen, obwohl der Code
        # längst korrigiert ist. transition_type wird aus demselben Grund ebenfalls
        # jedes Mal neu berechnet, kein Cache-Guard.
        for idx, s in enumerate(scenes):
            s["motion"] = _motion_for_scene(s, scenes[idx - 1] if idx > 0 else None)

        # Transition points (same rule as the whoosh SFX event) get their PRECEDING
        # scene's clip rendered with extra frames — exactly THIS transition's own
        # duration (varies per pacing family, see TRANSITION_LIBRARY: calm lingers at
        # 0.8s, punchy snaps at 0.3s) — tacked on. The crossfade then consumes exactly
        # that overlap, so the merged clip's total duration still equals the original,
        # uncompensated sum of both scenes' planned durations, keeping the frame-exact
        # sync invariant intact regardless of which duration was used.
        transition_at = [idx for idx in range(len(scenes)) if _has_transition_before(scenes, idx)]
        # transition_seq_idx (Position INNERHALB transition_at, nicht der rohe
        # Szenenindex) treibt die Sub-Typ-Rotation in _transition_for_scene --
        # Feinschliff Runde 2, siehe dessen Docstring. Einmal als Lookup gebaut, damit
        # der zweite Loop unten (Crossfade-Merge) dieselbe Position ohne O(n)-.index()
        # nachschlagen kann.
        transition_seq_idx_by_scene_idx = {idx: pos for pos, idx in enumerate(transition_at)}
        for idx in transition_at:
            prev = scenes[idx - 1]
            _ttype, _sfx, t_duration = _transition_for_scene(scenes[idx], transition_seq_idx_by_scene_idx[idx])
            transition_frames = round(t_duration * RENDER_FPS)
            prev["_frames"] = (prev.get("_frames") or round(prev["dur"] * RENDER_FPS)) + transition_frames

        overlay_opts = dashboard.get_video_overlay_opts(cid, vid)
        stage("clips", 0, len(scenes))
        clip_paths = []
        for idx, s in enumerate(scenes):
            if stop_requested():
                raise RuntimeError("Abgebrochen (Stop angefordert)")
            clip_path = os.path.join(render_dir, f"{s['i']:03d}.mp4")
            img_path = os.path.join(v_out(cid, vid), s["file"])
            _render_clip(img_path, s, clip_path, fps=RENDER_FPS, overlay_opts=overlay_opts,
                         target=render_target)
            clip_paths.append(clip_path)
            s["clip_file"] = os.path.basename(clip_path)
            stage("clips", idx + 1, len(scenes))

        # Merge in crossfades ONLY at the identified transition points — every other
        # cut stays a hard cut via the lossless concat demuxer below. Each merge
        # replaces the last entry of the growing output list (which may itself already
        # be a merge from a previous transition), so back-to-back transitions chain
        # correctly instead of referencing an already-consumed clip.
        stage("transitions", 0, len(transition_at))
        merged_paths = []
        transitions_done = 0
        for idx, s in enumerate(scenes):
            path = clip_paths[idx]
            if idx in transition_at and merged_paths:
                transition_type, _sfx, t_duration = _transition_for_scene(s, transition_seq_idx_by_scene_idx[idx])
                s["transition_type"] = transition_type  # sichtbar in plan.json, zum Nachvollziehen
                merged_path = os.path.join(render_dir, f"xfade_{s['i']:03d}.mp4")
                _crossfade_clips(merged_paths[-1], path, merged_path, t_duration, transition_type)
                merged_paths[-1] = merged_path
                transitions_done += 1
                stage("transitions", transitions_done, len(transition_at))
            else:
                merged_paths.append(path)
        clip_paths = merged_paths

        stage("assemble")
        silent_path = os.path.join(render_dir, "silent.mp4")
        _assemble_clips(clip_paths, silent_path)

        stage("audio")
        # target-spezifischer Dateiname: verhindert, dass ein Shorts-Render das
        # bestehende Longform-final.mp4 desselben Videos überschreibt.
        final_name = "final.mp4" if target == "longform" else f"final_{target}.mp4"
        final_path = os.path.join(v_out(cid, vid), final_name)
        # User-Entscheidung Juli 2026: keine Musik/SFX mehr im Render -- der User legt
        # Soundeffekte künftig selbst extern über die fertige Sprecherspur. Nur noch die
        # bereits pausen-gekürzte Sprecherspur (audio_path) wird gemuxt, unverändert.
        # engine/audio.py (_build_final_audio + Sound-Design-Kette aus Schritt 1+2)
        # bleibt im Repo erhalten (getestet, reaktivierbar), wird hier nur nicht mehr
        # aufgerufen. Die frame-genaue Sync-Invariante ist davon unberührt -- die
        # arbeitet ausschließlich auf den Video-Clip-Längen, nicht auf der Audiospur.
        _mux_audio(silent_path, audio_path, final_path)

        stage("review")
        checks = _render_selfcheck(final_path, audio_duration)
        if not all(checks.values()):
            raise RuntimeError(f"Selbstprüfung fehlgeschlagen: {checks}")

        with dashboard._PLAN_WRITE_LOCK:
            fresh_plan = json.load(open(plan_path))
            by_i = {s["i"]: s for s in fresh_plan["scenes"]}
            for s in scenes:
                if s["i"] in by_i:
                    by_i[s["i"]]["motion"] = s["motion"]
                    by_i[s["i"]]["clip_file"] = s["clip_file"]
                    if "transition_type" in s:
                        by_i[s["i"]]["transition_type"] = s["transition_type"]
                    if s.get("start_aligned") is not None:
                        by_i[s["i"]]["start_aligned"] = s["start_aligned"]
                        by_i[s["i"]]["end_aligned"] = s["end_aligned"]
                    # Cinematic-Mix Juli 2026 (Schritt 3): `words` (Wort-Slices für die
                    # 1-Wort-Captions) mitpersistieren -- sonst würde needs_alignment
                    # (oben) bei JEDEM künftigen Resume-Render erneut True liefern und
                    # die Whisper/ElevenLabs-Ausrichtung unnötig wiederholen, obwohl
                    # start_aligned längst korrekt vorliegt.
                    if s.get("words") is not None:
                        by_i[s["i"]]["words"] = s["words"]
            fresh_plan["audio_duration"] = audio_duration
            render_record = {"file": final_name, "ts": int(time.time()), "checks": checks}
            # dashboard.html liest bisher ausschließlich plan.render.file (Longform-Vorschau) --
            # dieses Feld bleibt deshalb Longform-exklusiv. render_by_target[target] ist das
            # zusätzliche, kollisionsfreie Feld für jeden Ziel-Typ (auch longform selbst),
            # das die künftige Shorts-Karte (dashboard.html) liest.
            fresh_plan.setdefault("render_by_target", {})[target] = render_record
            if target == "longform":
                fresh_plan["render"] = render_record
            dashboard._atomic_write_json(plan_path, fresh_plan, ensure_ascii=False, indent=1)

        #         # Only delete render_tmp after mux + selfcheck succeeded, and only this
        # directory — v_render_tmp() is deliberately separate from v_out()/generated/,
        # never the same path, so this can never touch the actual generated images.
        # Schwäche #69: cleanup_done-Flag damit finally-Block nicht doppelt räumt.
        cleanup_done = True
        shutil.rmtree(render_dir, ignore_errors=True)

        with dashboard._RENDER_JOBS_LOCK:
            dashboard.RENDER_JOBS[key] = {"running": False, "stop_requested": False, "stage": "fertig",
                                           "done": len(scenes), "total": len(scenes), "error": None, "file": final_name,
                                           "ts": time.time()}
        print(f"  [Render] {cid}/{vid} ({target}): fertig → {final_name}", flush=True)
    except Exception as e:
        traceback.print_exc()
        with dashboard._RENDER_JOBS_LOCK:
            prev = dashboard.RENDER_JOBS.get(key, {})
            dashboard.RENDER_JOBS[key] = {"running": False, "stop_requested": False, "stage": "error",
                                           "done": prev.get("done", 0), "total": prev.get("total", 0),
                                           "error": str(e), "file": None, "ts": time.time()}
        print(f"  [Render] {cid}/{vid} ({target}): Fehler: {e}", flush=True)
    finally:
        # Schwäche #69: Cleanup läuft IMMER — auch bei Crash, SIGTERM, oder Erfolg.
        if not cleanup_done:
            shutil.rmtree(render_dir, ignore_errors=True)
            _log("INFO", "render_tmp_cleanup_on_error", cid=cid, vid=vid)
