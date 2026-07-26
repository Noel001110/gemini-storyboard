"""shorts/api.py — Prefix /api/shorts/, gemountet in dashboard.py main().

Zwei Endpunkte:
- POST /api/shorts/generate: Klimax-Highlight (OPENING/CLIMAX-Auswahl, siehe
  shorts/clip_select.py:select_scenes). Status/Stop laufen bewusst über die bereits
  target-parametrisierten /api/render_status und /api/render_stop (dashboard.py) --
  kein Duplikat-Endpoint nötig, dieselbe RENDER_JOBS-Instanz wird einfach mit
  target="short_vertical" abgefragt/gestoppt.
- POST /api/shorts/split_parts: das GANZE Video in eine Serie sequenzieller Parts
  zerlegen (split_into_parts), je mit CTA-Endkarte (shorts/cta.py), automatisch
  generiertem Titel/Description (youtube/metadata.py) und automatisch in die
  Upload-Queue gelegt (store/db.py). Eigener PARTS_JOBS-Fortschritts-Dict + GET
  /api/shorts/parts_status, da dies ein mehrstufiger Prozess über mehrere Renders
  hinweg ist (nicht 1:1 auf RENDER_JOBS abbildbar).

Import von `dashboard` passiert absichtlich erst INNERHALB von handle()/_generate_worker
(lazy) -- dashboard.py importiert dieses Modul aus main(), also nachdem dashboard.py
selbst vollständig geladen ist. Ein Top-Level-Import hier würde einen Zyklus riskieren.
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time

from shorts.clip_select import (
    select_scenes, split_into_parts, build_short_plan_scenes, build_short_audio,
    MIN_DURATION_SEC_DEFAULT, MAX_DURATION_SEC_DEFAULT,
)
from shorts.cta import build_cta_clip, append_cta

# {(cid, vid): {"running", "current_part", "total_parts", "current_target", "stage",
#               "error", "queue_ids"}} -- eigenes Dict, analog RENDER_JOBS/PRODUCE_JOBS
# (gleiches try/except/finally-Muster gegen die bekannte "running blieb hängen"-
# Fehlerklasse dieser Codebase).
PARTS_JOBS: dict = {}
_PARTS_JOBS_LOCK = threading.Lock()

# Struktur-/Schnitt-Review Juli 2026: ersetzt die "Longform sequenziell in Parts
# zerschneiden"-Pipeline oben -- bestätigter Anti-Pattern (kein Hook, Cold-Feed-
# Zuschauer ohne Kontext, "Part N" signalisiert fehlenden Kontext, CTA-Endkarte killt
# Completion). Jeder Hook-Short ist ein EIGENSTÄNDIGES Mini-Skript mit eigenem
# Voiceover, siehe engine/prompts.py:generate_short_scripts/assign_short_scene_images.
# Gleiches Job-Dict-Muster wie PARTS_JOBS.
HOOKS_JOBS: dict = {}
_HOOKS_JOBS_LOCK = threading.Lock()


def _fail(dashboard, key, msg):
    with dashboard._RENDER_JOBS_LOCK:
        dashboard.RENDER_JOBS[key] = {
            "running": False, "stop_requested": False, "stage": "error",
            "done": 0, "total": 0, "error": msg, "file": None, "ts": time.time(),
        }
    print(f"  [Shorts] {key}: Fehler: {msg}", flush=True)


def _generate_worker(cid: str, vid: str, target: str, min_duration: float, max_duration: float):
    import dashboard  # lazy, siehe Modul-Docstring
    key = (cid, vid, target)
    try:
        plan_path = dashboard.v_plan(cid, vid)
        try:
            plan = json.load(open(plan_path))
        except Exception as e:
            return _fail(dashboard, key, f"Longform-Plan nicht lesbar — erst Longform rendern: {e}")

        selected = select_scenes(plan, min_duration=min_duration, max_duration=max_duration)
        if not selected:
            return _fail(dashboard, key,
                         "Keine Szenen mit start_aligned gefunden — das Longform-Video muss "
                         "zuerst mindestens einmal fertig gerendert sein.")

        src_audio_path = os.path.join(dashboard.v_uploads(cid, vid), "voiceover_trimmed.wav")
        if not os.path.exists(src_audio_path):
            return _fail(dashboard, key,
                         "Longform-Sprecherspur (voiceover_trimmed.wav) fehlt — erst Longform rendern.")

        short_scenes = build_short_plan_scenes(selected)

        uploads_dir = dashboard.v_uploads(cid, vid)
        os.makedirs(uploads_dir, exist_ok=True)
        short_audio_path = os.path.join(uploads_dir, f"{target}_voiceover.wav")
        build_short_audio(selected, src_audio_path, short_audio_path, uploads_dir)

        audio_meta_path = os.path.join(uploads_dir, f"{target}_audio_meta.json")
        dashboard._atomic_write_json(audio_meta_path, {"path": short_audio_path},
                                      ensure_ascii=False, indent=1)

        os.makedirs(dashboard.v_out(cid, vid), exist_ok=True)
        short_plan_path = os.path.join(dashboard.v_out(cid, vid), f"{target}_plan.json")
        dashboard._atomic_write_json(short_plan_path, {"scenes": short_scenes},
                                      ensure_ascii=False, indent=1)

        # RENDER_JOBS[key] steht schon auf running=True (von handle() unten gesetzt,
        # bevor dieser Thread startete) -- _render_worker aktualisiert stage/done/total
        # selbst und setzt am Ende running=False (fertig oder error), exakt wie beim
        # bestehenden /api/render_start.
        dashboard._render_worker(cid, vid, target)
    except Exception as e:
        _fail(dashboard, key, str(e))


def _fail_parts(key, msg):
    with _PARTS_JOBS_LOCK:
        # completed_parts bleibt erhalten, falls schon welche fertig waren, bevor ein
        # späterer Part scheiterte -- die fertigen Dateien/Queue-Einträge sind gültig
        # und sollen im Frontend sichtbar bleiben, auch wenn der Gesamtlauf abbricht.
        prev = PARTS_JOBS.get(key, {})
        PARTS_JOBS[key] = {
            "running": False, "current_part": prev.get("current_part", 0),
            "total_parts": prev.get("total_parts", 0),
            "current_target": None, "stage": "error", "error": msg,
            "queue_ids": prev.get("queue_ids", []),
            "completed_parts": prev.get("completed_parts", []),
            "ts": time.time(),
        }
    print(f"  [Parts] {key}: Fehler: {msg}", flush=True)


def _parts_worker(cid: str, vid: str):
    """Zerlegt das GANZE Longform-Video in eine Serie sequenzieller Parts (siehe Modul-
    Docstring). Läuft komplett in diesem einen Hintergrund-Thread -- ein 7-Teile-Video
    dauert entsprechend ~7x so lange wie ein einzelner Short-Render (RENDER_SEMAPHORE(1)
    erzwingt sowieso serielles Rendern), PARTS_JOBS macht den Fortschritt sichtbar."""
    import dashboard  # lazy, siehe Modul-Docstring
    import store.db as db
    from youtube.metadata import generate_packaging, get_categories

    key = (cid, vid)
    try:
        plan_path = dashboard.v_plan(cid, vid)
        try:
            plan = json.load(open(plan_path))
        except Exception as e:
            return _fail_parts(key, f"Longform-Plan nicht lesbar — erst Longform rendern: {e}")

        parts = split_into_parts(plan)
        if not parts:
            return _fail_parts(key,
                                "Keine Szenen mit start_aligned gefunden — das Longform-Video "
                                "muss zuerst mindestens einmal fertig gerendert sein.")

        src_audio_path = os.path.join(dashboard.v_uploads(cid, vid), "voiceover_trimmed.wav")
        if not os.path.exists(src_audio_path):
            return _fail_parts(key,
                                "Longform-Sprecherspur (voiceover_trimmed.wav) fehlt — "
                                "erst Longform rendern.")

        meta = dashboard.load_v_meta(cid, vid)
        base_title = meta.get("selected_title") or (meta.get("titles") or [""])[0]
        if not base_title:
            return _fail_parts(key, "Erst einen Titel für das Longform-Video auswählen.")

        total_parts = len(parts)
        categories = get_categories(cid)

        with _PARTS_JOBS_LOCK:
            PARTS_JOBS[key] = {
                "running": True, "current_part": 0, "total_parts": total_parts,
                "current_target": None, "stage": "startet", "error": None,
                "queue_ids": [], "completed_parts": [],
            }

        uploads_dir = dashboard.v_uploads(cid, vid)
        os.makedirs(uploads_dir, exist_ok=True)
        os.makedirs(dashboard.v_out(cid, vid), exist_ok=True)
        queue_ids = []

        for n, part_scenes in enumerate(parts, start=1):
            target = f"short_part_{n}"
            with _PARTS_JOBS_LOCK:
                PARTS_JOBS[key].update(current_part=n, current_target=target, stage="rendert")

            # Eigener RenderTarget-Name pro Part (dieselben Maße wie short_vertical) --
            # vermeidet Datei-/Job-Key-Kollisionen zwischen Parts und dem Klimax-Short,
            # exakt dasselbe Muster wie short_vertical selbst (engine/render.py).
            dashboard.RENDER_TARGETS[target] = dashboard.RenderTarget(
                name=target, width=1080, height=1920, fps=dashboard.RENDER_FPS,
                supersample_width=2160, pad_style="blur")

            short_scenes = build_short_plan_scenes(part_scenes)
            short_audio_path = os.path.join(uploads_dir, f"{target}_voiceover.wav")
            build_short_audio(part_scenes, src_audio_path, short_audio_path, uploads_dir)

            audio_meta_path = os.path.join(uploads_dir, f"{target}_audio_meta.json")
            dashboard._atomic_write_json(audio_meta_path, {"path": short_audio_path},
                                          ensure_ascii=False, indent=1)
            short_plan_path = os.path.join(dashboard.v_out(cid, vid), f"{target}_plan.json")
            dashboard._atomic_write_json(short_plan_path, {"scenes": short_scenes},
                                          ensure_ascii=False, indent=1)

            render_key = (cid, vid, target)
            with dashboard._RENDER_JOBS_LOCK:
                dashboard.RENDER_JOBS[render_key] = {
                    "running": True, "stop_requested": False, "stage": "startet",
                    "done": 0, "total": 0, "error": None, "file": None,
                    "started_ts": time.time(),
                }
            dashboard._render_worker(cid, vid, target)
            with dashboard._RENDER_JOBS_LOCK:
                render_state = dict(dashboard.RENDER_JOBS.get(render_key, {}))
            if render_state.get("error"):
                return _fail_parts(key, f"Part {n} Render fehlgeschlagen: {render_state['error']}")

            with _PARTS_JOBS_LOCK:
                PARTS_JOBS[key].update(stage="cta")
            base_video_path = os.path.join(dashboard.v_out(cid, vid), f"final_{target}.mp4")
            cta_clip_path = os.path.join(uploads_dir, f"{target}_cta.mp4")
            final_with_cta_path = os.path.join(dashboard.v_out(cid, vid),
                                                f"final_{target}_with_cta.mp4")
            build_cta_clip(cta_clip_path, 1080, 1920, dashboard.RENDER_FPS)
            append_cta(base_video_path, cta_clip_path, final_with_cta_path)
            try: os.remove(cta_clip_path)
            except OSError: pass

            with _PARTS_JOBS_LOCK:
                PARTS_JOBS[key].update(stage="packaging")
            part_script = " ".join(s.get("text", "") for s in part_scenes)
            # Ein Fehlschlag hier degradiert nur zu einer schwachen Fallback-Description
            # statt den ganzen Part scheitern zu lassen -- ein zusätzlicher Versuch lohnt
            # sich deshalb, bevor auf den Fallback zurückgefallen wird (post_gemini_native
            # selbst retryt intern schon einmal; das hier ist eine zweite, unabhängige
            # Schicht darüber, beobachtet live an Part 3 des echten Testvideos: erster
            # Versuch schlug fehl, ein manueller Re-Run direkt danach lief sauber durch --
            # transient, kein deterministischer Bug).
            packaging = None
            last_err = None
            for attempt in range(2):
                try:
                    packaging = generate_packaging(part_script, base_title, categories)
                    break
                except Exception as e:
                    last_err = e
                    print(f"  [Parts] Packaging für Part {n}, Versuch {attempt + 1}/2 "
                          f"fehlgeschlagen: {e}", flush=True)
            if packaging is None:
                packaging = {"description": "", "tags": [],
                             "category_id": categories[0]["id"] if categories else ""}
                print(f"  [Parts] Packaging für Part {n} endgültig fehlgeschlagen, "
                      f"leere Felder: {last_err}", flush=True)

            # User-Feedback: "Part N" gehört an den ANFANG (nicht als Suffix), englisch
            # ("Part" statt "Teil") -- konsistent mit DEFAULT_LANGUAGE="en" (youtube/
            # upload.py) und dem Rest der generierten Inhalte (Skript ist englisch).
            part_title = f"Part {n}: {base_title}"
            part_description = (
                f"{packaging['description']}\n\n"
                f"Part {n} of {total_parts}. Full video on our channel."
            ).strip()

            with _PARTS_JOBS_LOCK:
                PARTS_JOBS[key].update(stage="queueing")
            # Gleicher simpler Default-Termin wie die bestehende Warteschlangen-Brücke
            # (dashboard.html: updateQueueBridgeVisibility(), +1 Tag) -- User-Entscheidung:
            # keine eigene Staffelungs-Logik, Zeitpunkte werden später in Control angepasst.
            scheduled_at = time.time() + 86400
            qid = db.queue_add(cid, vid, target, final_with_cta_path, part_title,
                                scheduled_at, short_id=f"part_{n}",
                                description=part_description, tags=packaging["tags"],
                                category_id=packaging["category_id"])
            queue_ids.append(qid)

            # Progressiv sichtbar machen -- die Datei liegt fertig auf der Platte, sobald
            # dieser Part durch ist, muss nicht auf den ganzen Batch warten (User-Wunsch).
            with _PARTS_JOBS_LOCK:
                PARTS_JOBS[key]["completed_parts"] = PARTS_JOBS[key].get("completed_parts", []) + [{
                    "part": n, "queue_id": qid,
                    "file": f"final_{target}_with_cta.mp4", "title": part_title,
                }]

        with _PARTS_JOBS_LOCK:
            PARTS_JOBS[key]["running"] = False
            PARTS_JOBS[key]["current_target"] = None
            PARTS_JOBS[key]["stage"] = "fertig"
            PARTS_JOBS[key]["error"] = None
            PARTS_JOBS[key]["queue_ids"] = queue_ids
            PARTS_JOBS[key]["ts"] = time.time()
        print(f"  [Parts] {cid}/{vid}: {total_parts} Parts erzeugt und in die Warteschlange "
              f"gelegt.", flush=True)
    except Exception as e:
        import traceback; traceback.print_exc()
        _fail_parts(key, str(e))


def _fail_hooks(key, msg):
    with _HOOKS_JOBS_LOCK:
        # completed_hooks bleibt erhalten (gleiche Begründung wie _fail_parts) --
        # bereits fertige Hook-Shorts sind gültig und bleiben im Frontend sichtbar.
        prev = HOOKS_JOBS.get(key, {})
        HOOKS_JOBS[key] = {
            "running": False, "current_hook": prev.get("current_hook", 0),
            "total_hooks": prev.get("total_hooks", 0),
            "stage": "error", "error": msg,
            "completed_hooks": prev.get("completed_hooks", []),
            "ts": time.time(),
        }
    print(f"  [Hooks] {key}: Fehler: {msg}", flush=True)


def _hooks_worker(cid: str, vid: str):
    """Baut alle bestätigten Hook-first-Short-Skripte (dashboard.load_v_meta(cid,vid)
    ["confirmed_hook_scripts"], vom Review-Schritt gesetzt) zu fertigen, eigenständigen
    vertikalen Videos: eigenes Voiceover pro Short (ElevenLabs), Bilder aus dem bereits
    generierten Longform-Bildpool wiederverwendet (assign_short_scene_images, EIN
    batched Call für alle Shorts zusammen), Render über den bestehenden, unveränderten
    _render_worker (der Whisper-Alignment/Pause-Trim bereits selbst übernimmt, sobald
    ein audio_meta.json + {target}_plan.json vorliegen -- exakt dasselbe Muster wie
    _generate_worker/_parts_worker oben)."""
    import dashboard  # lazy, siehe Modul-Docstring
    import store.db as db
    from engine.prompts import assign_short_scene_images
    from engine.scenes import split_units, segment_by_pacing, PACING_PROFILES

    key = (cid, vid)
    try:
        meta = dashboard.load_v_meta(cid, vid)
        scripts = meta.get("confirmed_hook_scripts") or []
        if not scripts:
            return _fail_hooks(key, "Keine bestätigten Short-Skripte — erst in Schritt "
                                     "⑥ Skripte erzeugen und bestätigen.")

        plan_path = dashboard.v_plan(cid, vid)
        try:
            plan = json.load(open(plan_path))
        except Exception as e:
            return _fail_hooks(key, f"Longform-Plan nicht lesbar — erst Longform rendern: {e}")
        longform_scenes = [s for s in plan.get("scenes", []) if s.get("file")]
        if not longform_scenes:
            return _fail_hooks(key, "Longform hat noch keine generierten Bilder — "
                                     "erst Longform fertigstellen.")

        # wpm-Kalibrierung (Struktur-/Schnitt-Review): reale Sprechrate DIESES Kanals
        # statt einer festen Annahme -- derselbe Fix wie beim Longform-Rhythmus.
        # `or 150.0` ist reine Typ-Absicherung -- _measure_channel_wpm gibt mit seinem
        # eigenen Default-Fallback nie None zurück, nur bei explizitem fallback=None.
        wpm = dashboard._measure_channel_wpm(cid) or 150.0
        short_profile = PACING_PROFILES["short"]

        total_hooks = len(scripts)
        with _HOOKS_JOBS_LOCK:
            HOOKS_JOBS[key] = {
                "running": True, "current_hook": 0, "total_hooks": total_hooks,
                "stage": "segmentiert", "error": None, "completed_hooks": [],
            }

        # Alle Skripte zuerst segmentieren (reine Text-Logik, kein API-Call) -- erst
        # DANACH ein einziger batched Bild-Zuordnungs-Call für alle Shorts zusammen
        # (Kosten-/Latenz-Budget: 2 zusätzliche LLM-Calls insgesamt, siehe Plan).
        all_scenes = []
        for script in scripts:
            units = split_units(script.get("script_text", ""))
            scenes = segment_by_pacing(units, [], wpm, short_profile.normal_sec,
                                        profile=short_profile)
            all_scenes.append(scenes)

        with _HOOKS_JOBS_LOCK:
            HOOKS_JOBS[key].update(stage="bild_zuordnung")
        assignments = assign_short_scene_images(all_scenes, longform_scenes)

        voice_settings = dashboard.load_voice_settings(cid)
        uploads_dir = dashboard.v_uploads(cid, vid)
        out_dir = dashboard.v_out(cid, vid)
        os.makedirs(uploads_dir, exist_ok=True)
        os.makedirs(out_dir, exist_ok=True)

        # Fallback-Bildstil für den seltenen Ausnahmefall, dass weder LLM-Zuordnung noch
        # Keyword-Overlap ein Longform-Bild finden (gleiches Muster wie
        # _thumbnail_generate_worker: Video-Modus entscheidet, welcher Master gilt).
        try:
            mode = dashboard.get_video_mode(cid, vid)
            master_style = (open(dashboard.ch_vid_master(cid)).read().strip() if mode == "video"
                            else dashboard.read_master(cid)) or dashboard.VIDEO_MASTER_DEFAULT
        except Exception:
            master_style = dashboard.VIDEO_MASTER_DEFAULT

        queue_ids = []
        for n, (script, scenes, files) in enumerate(zip(scripts, all_scenes, assignments), start=1):
            target = f"short_hook_{n}"
            with _HOOKS_JOBS_LOCK:
                HOOKS_JOBS[key].update(current_hook=n, stage="bilder")

            # Eigener RenderTarget-Name pro Hook-Short (gleiche Maße wie short_vertical) --
            # vermeidet Datei-/Job-Key-Kollisionen, exakt dasselbe Muster wie Parts oben.
            dashboard.RENDER_TARGETS[target] = dashboard.RenderTarget(
                name=target, width=1080, height=1920, fps=dashboard.RENDER_FPS,
                supersample_width=2160, pad_style="blur")

            for i, sc in enumerate(scenes):
                sc["i"] = i
                assigned = files[i] if i < len(files) else None
                if not assigned:
                    # Letzter Fallback: frisches Bild generieren (Ausnahmefall -- der
                    # Regelfall ist 0 neue KIE-Calls, siehe Plan).
                    fresh_name = f"{target}_{i:03d}.jpg"
                    fresh_path = os.path.join(out_dir, fresh_name)
                    res = dashboard.gen_image(
                        f"A scene illustrating: {sc.get('text', '')[:150]}",
                        master_style, fresh_path)
                    assigned = fresh_name if res.get("ok") else None
                sc["file"] = assigned

            with _HOOKS_JOBS_LOCK:
                HOOKS_JOBS[key].update(stage="audio")
            script_text = script.get("script_text", "")
            enriched = dashboard._enrich_for_tts(script_text, scenes=None)
            try:
                raw = dashboard.elevenlabs_generate(enriched, voice_settings)
            except Exception as e:
                return _fail_hooks(key, f"Hook-Short {n} Voiceover fehlgeschlagen: {e}")
            audio_bytes = base64.b64decode(raw["audio_base64"])
            audio_path = os.path.join(uploads_dir, f"{target}_voiceover.mp3")
            with open(audio_path, "wb") as f:
                f.write(audio_bytes)

            audio_meta_path = os.path.join(uploads_dir, f"{target}_audio_meta.json")
            dashboard._atomic_write_json(audio_meta_path, {
                "path": audio_path,
                # ElevenLabs-Fast-Path (Phase 1, bereits bestehende Konvention): eigene
                # Wort-Zeitstempel ersparen _render_worker einen separaten Whisper-Lauf.
                "voiceover_word_timestamps": raw.get("words"),
            }, ensure_ascii=False, indent=1)

            short_plan_path = os.path.join(out_dir, f"{target}_plan.json")
            dashboard._atomic_write_json(short_plan_path, {"scenes": scenes},
                                          ensure_ascii=False, indent=1)

            with _HOOKS_JOBS_LOCK:
                HOOKS_JOBS[key].update(stage="rendert")
            render_key = (cid, vid, target)
            with dashboard._RENDER_JOBS_LOCK:
                dashboard.RENDER_JOBS[render_key] = {
                    "running": True, "stop_requested": False, "stage": "startet",
                    "done": 0, "total": 0, "error": None, "file": None,
                    "started_ts": time.time(),
                }
            dashboard._render_worker(cid, vid, target)
            with dashboard._RENDER_JOBS_LOCK:
                render_state = dict(dashboard.RENDER_JOBS.get(render_key, {}))
            if render_state.get("error"):
                return _fail_hooks(key, f"Hook-Short {n} Render fehlgeschlagen: "
                                         f"{render_state['error']}")

            with _HOOKS_JOBS_LOCK:
                HOOKS_JOBS[key].update(stage="queueing")
            final_path = os.path.join(out_dir, f"final_{target}.mp4")
            # User-Wunsch Juli 2026: fertige Shorts zusätzlich nach shorts_export/<cid>/
            # <vid>/ kopieren -- eigener Ordner, nach Video sortiert, zum Weiterleiten,
            # statt zwischen 70+ Szenenbildern in generated/ suchen zu müssen.
            dashboard.export_short_copy(cid, vid, final_path, f"short_{n}.mp4")
            title = script.get("title") or f"Short {n}"
            description = script.get("description", "")
            # Category/Tags vom Longform übernommen (geteilte Klassifikation, kein
            # zusätzlicher Packaging-Call pro Hook-Short -- gleiches Prinzip wie der
            # bestehende Klimax-Short, der ebenfalls VIDEO_META/Packaging teilt).
            # Juli 2026 (User-Wunsch "Shorts im 1-Tages-Abstand um 20 Uhr, Longform am
            # selben Tag wie Short 1"): day_offset=n-1 -- Short 1 landet auf demselben
            # Anker-Tag wie das Longform (day_offset=0 dort), Short 2 einen Tag später,
            # usw. Siehe next_publish_slot()-Docstring in youtube/upload.py.
            from youtube.upload import next_publish_slot
            scheduled_at = next_publish_slot(cid, vid, day_offset=n - 1)
            qid = db.queue_add(cid, vid, target, final_path, title, scheduled_at,
                                short_id=f"hook_{n}", description=description,
                                tags=meta.get("tags") or [], category_id=meta.get("category_id"))
            queue_ids.append(qid)

            # Progressiv sichtbar -- die Datei liegt fertig, sobald dieser Hook durch
            # ist, muss nicht auf den ganzen Batch warten (gleiches Prinzip wie Parts).
            with _HOOKS_JOBS_LOCK:
                HOOKS_JOBS[key]["completed_hooks"] = HOOKS_JOBS[key].get("completed_hooks", []) + [{
                    "hook": n, "queue_id": qid, "file": f"final_{target}.mp4",
                    "title": title, "angle": script.get("angle", ""),
                }]

        with _HOOKS_JOBS_LOCK:
            HOOKS_JOBS[key]["running"] = False
            HOOKS_JOBS[key]["stage"] = "fertig"
            HOOKS_JOBS[key]["error"] = None
            HOOKS_JOBS[key]["queue_ids"] = queue_ids
            HOOKS_JOBS[key]["ts"] = time.time()
        print(f"  [Hooks] {cid}/{vid}: {total_hooks} Hook-Shorts erzeugt und in die "
              f"Warteschlange gelegt.", flush=True)
    except Exception as e:
        import traceback; traceback.print_exc()
        _fail_hooks(key, str(e))


def handle(method, path, handler, qs, cid, vid, body):
    if method == "POST" and path == "/api/shorts/generate":
        import dashboard
        if not vid:
            handler._send(400, {"error": "Kein Video ausgewählt"})
            return True, None
        d = body or {}
        target = d.get("target", "short_vertical")
        if target == "longform" or target not in dashboard.RENDER_TARGETS:
            handler._send(400, {"error": f"Ungültiges Shorts-Target: {target}"})
            return True, None
        min_duration = float(d.get("min_duration", MIN_DURATION_SEC_DEFAULT))
        max_duration = float(d.get("max_duration", MAX_DURATION_SEC_DEFAULT))
        key = (cid, vid, target)
        with dashboard._RENDER_JOBS_LOCK:
            if dashboard.RENDER_JOBS.get(key, {}).get("running"):
                handler._send(200, {"ok": True, "already_running": True})
                return True, None
            dashboard.RENDER_JOBS[key] = {
                "running": True, "stop_requested": False, "stage": "startet",
                "done": 0, "total": 0, "error": None, "file": None,
                "started_ts": time.time(),
            }
        threading.Thread(target=_generate_worker, args=(cid, vid, target, min_duration, max_duration),
                          daemon=True).start()
        handler._send(200, {"ok": True, "already_running": False})
        return True, None

    if method == "POST" and path == "/api/shorts/split_parts":
        if not vid:
            handler._send(400, {"error": "Kein Video ausgewählt"})
            return True, None
        key = (cid, vid)
        with _PARTS_JOBS_LOCK:
            if PARTS_JOBS.get(key, {}).get("running"):
                handler._send(200, {"ok": True, "already_running": True})
                return True, None
            PARTS_JOBS[key] = {
                "running": True, "current_part": 0, "total_parts": 0,
                "current_target": None, "stage": "startet", "error": None,
                "queue_ids": [], "completed_parts": [],
            }
        threading.Thread(target=_parts_worker, args=(cid, vid), daemon=True).start()
        handler._send(200, {"ok": True, "already_running": False})
        return True, None

    if method == "GET" and path == "/api/shorts/parts_status":
        with _PARTS_JOBS_LOCK:
            state = PARTS_JOBS.get((cid, vid))
        handler._send(200, state or {"running": False})
        return True, None

    if method == "GET" and path == "/api/shorts/parts_list":
        # Durable Sicht auf bereits erzeugte Parts -- PARTS_JOBS (In-Memory) verliert
        # seinen Stand bei jedem Server-Neustart, genau wie RENDER_JOBS; upload_queue
        # (SQLite) ist die Wahrheit, die einen Neustart überlebt (dasselbe Prinzip, das
        # store/db.py schon fürs OAuth/Upload-Tracking nutzt). Ohne diesen Endpoint
        # verschwinden fertige Parts aus dem Dashboard, sobald der Server neu startet --
        # der Klimax-Short hat dafür den spekulativen <video>-Tag-Trick, Parts brauchen
        # das hier, weil ihre Dateinamen (final_short_part_N_with_cta.mp4) nicht fix sind.
        import store.db as db
        if not vid:
            handler._send(200, {"parts": []})
            return True, None

        def _part_num(row):
            m = re.search(r"short_part_(\d+)$", row["render_target"])
            return int(m.group(1)) if m else 0

        rows = [r for r in db.queue_list()
                if r["cid"] == cid and r["vid"] == vid
                and r["render_target"].startswith("short_part_")]
        rows.sort(key=_part_num)
        parts = [{
            "part": _part_num(r), "queue_id": r["id"],
            "file": os.path.basename(r["file_path"]), "title": r["title"],
            "status": r["status"],
        } for r in rows]
        handler._send(200, {"parts": parts})
        return True, None

    # ── Hook-first Shorts (Struktur-/Schnitt-Review Juli 2026) ────────────────────
    if method == "POST" and path == "/api/shorts/generate_hook_scripts":
        # Synchron, wie das bestehende /api/generate_titles -- ein einzelner
        # LLM-Call, kein eigener Job-Poll-Mechanismus nötig.
        import dashboard
        from engine.prompts import generate_short_scripts
        if not vid:
            handler._send(400, {"error": "Kein Video ausgewählt"})
            return True, None
        d = body or {}
        n = int(d.get("n", 5))
        try:
            plan = json.load(open(dashboard.v_plan(cid, vid)))
            full_script = " ".join(s.get("text", "") for s in plan.get("scenes", []))
        except Exception:
            full_script = ""
        if not full_script.strip():
            handler._send(400, {"error": "Kein Longform-Skript vorhanden — erst Skript/Plan erstellen."})
            return True, None
        meta = dashboard.load_v_meta(cid, vid)
        chosen_title = meta.get("selected_title") or (meta.get("titles") or [""])[0]
        if not chosen_title:
            handler._send(400, {"error": "Erst einen Titel für das Longform-Video auswählen."})
            return True, None
        lang = (dashboard.load_v_script(cid, vid) or {}).get("language", "en")
        print(f"  [Hooks] Generiere {n} Hook-Short-Skripte …", flush=True)
        scripts = generate_short_scripts(full_script, chosen_title, n=n, lang=lang)
        if not scripts:
            handler._send(500, {"error": "Short-Skript-Generierung fehlgeschlagen."})
            return True, None
        meta["hook_scripts"] = scripts
        dashboard.save_v_meta(cid, vid, meta)
        handler._send(200, {"ok": True, "scripts": scripts})
        return True, None

    if method == "POST" and path == "/api/shorts/confirm_hook_scripts":
        # Review-Gate (Schritt ⑥): der Nutzer sieht/editiert/verwirft die generierten
        # Skripte, BEVOR der teure Teil (TTS + Bild-Zuordnung + 5x Render) losläuft --
        # exakt wie bei der bestehenden Titel-Auswahl (#titleListStep).
        import dashboard
        if not vid:
            handler._send(400, {"error": "Kein Video ausgewählt"})
            return True, None
        d = body or {}
        scripts = d.get("scripts") or []
        if not isinstance(scripts, list) or not scripts:
            handler._send(400, {"error": "Keine Skripte übergeben."})
            return True, None
        meta = dashboard.load_v_meta(cid, vid)
        meta["confirmed_hook_scripts"] = scripts
        dashboard.save_v_meta(cid, vid, meta)
        handler._send(200, {"ok": True})
        return True, None

    if method == "POST" and path == "/api/shorts/build_hooks":
        if not vid:
            handler._send(400, {"error": "Kein Video ausgewählt"})
            return True, None
        key = (cid, vid)
        with _HOOKS_JOBS_LOCK:
            if HOOKS_JOBS.get(key, {}).get("running"):
                handler._send(200, {"ok": True, "already_running": True})
                return True, None
            HOOKS_JOBS[key] = {
                "running": True, "current_hook": 0, "total_hooks": 0,
                "stage": "startet", "error": None,
                "queue_ids": [], "completed_hooks": [],
            }
        threading.Thread(target=_hooks_worker, args=(cid, vid), daemon=True).start()
        handler._send(200, {"ok": True, "already_running": False})
        return True, None

    if method == "GET" and path == "/api/shorts/hooks_status":
        with _HOOKS_JOBS_LOCK:
            state = HOOKS_JOBS.get((cid, vid))
        handler._send(200, state or {"running": False})
        return True, None

    if method == "GET" and path == "/api/shorts/hooks_list":
        # Durable Sicht, gleiches Prinzip wie /api/shorts/parts_list -- HOOKS_JOBS
        # (In-Memory) verliert seinen Stand bei jedem Server-Neustart.
        import store.db as db
        if not vid:
            handler._send(200, {"hooks": []})
            return True, None

        def _hook_num(row):
            m = re.search(r"short_hook_(\d+)$", row["render_target"])
            return int(m.group(1)) if m else 0

        rows = [r for r in db.queue_list()
                if r["cid"] == cid and r["vid"] == vid
                and r["render_target"].startswith("short_hook_")]
        rows.sort(key=_hook_num)
        hooks = [{
            "hook": _hook_num(r), "queue_id": r["id"],
            "file": os.path.basename(r["file_path"]), "title": r["title"],
            "status": r["status"],
        } for r in rows]
        handler._send(200, {"hooks": hooks})
        return True, None

    return False, None
