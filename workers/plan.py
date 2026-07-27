"""workers/plan.py — Skript-zu-Szenenplan-Worker (aus dashboard.py extrahiert,
Refactor Phase 3). Siehe workers/__init__.py für die lazy-import-Konvention.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import traceback

from core.paths import ch_sheets, v_analysis_cache, v_out, v_plan
from engine.prompts import gen_charsheet, visual_prompts
from engine.scenes import segment_by_pacing, split_units


def run(cid: str, vid: str, text: str, wpm: float, sec: float) -> None:
    """Runs script -> scenes -> analysis -> image prompts server-side, the same reason
    as _batch_generate_worker: this used to be one blocking HTTP request, so closing the
    tab mid-run looked like nothing happened and re-clicking started a second, fully
    independent LLM pass over the same script."""
    import dashboard  # lazy, siehe workers/__init__.py

    key = (cid, vid)
    try:
        dashboard.ensure_video(cid, vid)
        out = v_out(cid, vid)
        plan_p = v_plan(cid, vid)
        # Juli 2026 (User-Report: "Race-Bug: Plan-Generate hat 91 fertige Bilder gelöscht"):
        # Statt blind alle generierten Files zu löschen, mergen wir den existierenden
        # Plan-State mit dem neuen. Eine Szene behält file/status/source_url wenn der
        # Text identisch geblieben ist — das ist die Heuristik: gleicher Text → gleiche
        # Szene, gerendertes Bild ist noch gültig. Bei Skript-Änderungen wandern die
        # Bilder in _stale/ und werden nicht im Frontend angezeigt, gehen aber nicht
        # verloren (Recovery möglich).
        prev_scenes = {}
        try:
            prev_plan = json.load(open(plan_p))
            for ps in prev_plan.get("scenes", []):
                if ps.get("file") and ps.get("status") == "fertig":
                    prev_scenes[ps["i"]] = ps
        except Exception:
            pass

        # Files zu Szenen die im alten Plan fertig waren UNDER dem alten Namen behalten;
        # alles andere in _stale/ verschieben statt löschen (Recovery möglich).
        stale_dir = os.path.join(out, "_stale")
        for f in os.listdir(out):
            if not f.endswith((".jpg", ".png", ".mp4")):
                continue
            try:
                stem = os.path.splitext(f)[0]
                # Stem kann "000" oder "094_veo_silent" etc. sein
                scene_i = None
                try:
                    scene_i = int(stem.split("_")[0])
                except ValueError:
                    pass
                if scene_i is not None and scene_i in prev_scenes:
                    # Szene ist im alten Plan fertig → vorerst behalten.
                    # Wenn der neue Plan sie nicht (mehr) enthält, wandert sie in _stale/.
                    continue
                # Nicht-matchende Files nach _stale/ verschieben
                os.makedirs(stale_dir, exist_ok=True)
                src = os.path.join(out, f)
                dst = os.path.join(stale_dir, f)
                if os.path.exists(dst):
                    os.remove(dst)
                os.rename(src, dst)
                print(f"  [Plan] Verschiebe alte Datei nach _stale/: {f}", flush=True)
            except Exception as e:
                print(f"  [Plan] Konnte {f} nicht verschieben: {e}", flush=True)

        with dashboard._PLAN_JOBS_LOCK:
            dashboard.PLAN_JOBS[key]["step"] = "Zerlege Skript in Einheiten …"
        units = split_units(text)

        # Analysis runs on the raw atomic units (not pre-grouped scenes) BEFORE
        # segmentation now, because it also assigns the per-unit pacing label (calm/
        # normal/punchy) used to decide scene cuts below — the same LLM pass that reads
        # the emotional arc decides pacing, so cuts land on "a complete thought" instead
        # of a mechanical time interval, and pacing can't drift from what the model
        # already decided is the climax vs. the setup.
        #
        # Juli 2026 (Re-Plan-Determinismus-Bug, gefunden beim Live-Test des
        # Retention-Rewrite-Schritts): analyze_script() ist ein LLM-Call und daher
        # NICHT deterministisch — ein Re-Plan mit unverändertem Skript-Text konnte
        # trotzdem leicht andere pacing-Labels zurückbekommen, wodurch
        # segment_by_pacing() (rein deterministisch, siehe engine/scenes.py) andere
        # Szenengrenzen erzeugte. Die byte-identische Text-Prüfung in
        # _preserve_rendered_scenes() griff dann nicht mehr, obwohl sich am Skript
        # nichts geändert hatte — bereits gerenderte Bilder wurden grundlos auf
        # "geplant" zurückgesetzt. Fix: Ergebnis keyed by Skript-Text-Hash cachen;
        # bei unverändertem Text wird der LLM-Call übersprungen (spart nebenbei
        # auch die API-Kosten für einen ansonsten redundanten Analyse-Call).
        script_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        analysis = None
        cache_p = v_analysis_cache(cid, vid)
        try:
            cached = json.load(open(cache_p))
            if cached.get("script_hash") == script_hash:
                analysis = cached.get("analysis")
                print("  [Plan] Skript-Text unverändert — nutze gecachte Analyse "
                      "(kein erneuter LLM-Call).", flush=True)
        except Exception:
            pass

        if analysis is None:
            with dashboard._PLAN_JOBS_LOCK:
                dashboard.PLAN_JOBS[key]["step"] = f"Analysiere {len(units)} Einheiten (Story-Bogen + Pacing) …"
            analysis = dashboard.analyze_script(units)
            try:
                dashboard._atomic_write_json(
                    cache_p, {"script_hash": script_hash, "analysis": analysis},
                    ensure_ascii=False, indent=1)
            except Exception as e:
                print(f"  [Plan] Analyse-Cache konnte nicht geschrieben werden: {e}", flush=True)

        with dashboard._PLAN_JOBS_LOCK:
            dashboard.PLAN_JOBS[key]["step"] = "Gruppiere Szenen nach Pacing …"
        scenes = segment_by_pacing(units, analysis.get("pacing", []), wpm, sec,
                                    sequences=analysis.get("visual_sequences", []),
                                    callouts=analysis.get("callouts", []))

        with dashboard._PLAN_JOBS_LOCK:
            dashboard.PLAN_JOBS[key]["step"] = f"Schreibe Bild-Prompts für {len(scenes)} Szenen …"
        prompts = visual_prompts(scenes, analysis)

        prompt_error_scenes = []
        # Race-Fix: Wenn der alte Plan eine Szene mit gleichem Text+Index schon fertig
        # gerendert hatte, behalten wir file/status/source_url statt auf "geplant"
        # zurückzufallen. Verhindert den Bug dass ein versehentlicher Doppelklick
        # auf "Plan aus Skript erstellen" während eines laufenden Batch-Renders alle
        # 91 fertigen Bilder als "geplant" markiert.
        for s, pr in zip(scenes, prompts):
            s["prompt"] = pr["prompt"]; s["concrete_entity"] = pr["concrete_entity"]; s["file"] = None
            s["secondary_entity"] = pr.get("secondary_entity", "")
            s["status"] = "geplant"; s["t"] = dashboard.fmt_t(s["start"])
            s["video_prompt"] = ""
            # Juli 2026 (User-Report: mehrere Szenen mit barebones "Scene illustrating: ..."
            # Notprompt statt echtem Bild-Prompt — sichtbar am fehlenden roten Faden).
            # visual_prompts() markiert das jetzt explizit statt es unauffällig durchgehen
            # zu lassen — hier nur durchreichen + fürs Log sammeln, damit der Nutzer sofort
            # sieht, welche Szenen manuelle Nacharbeit brauchen.
            s["prompt_error"] = pr.get("prompt_error", False)
            if s["prompt_error"]:
                prompt_error_scenes.append(s["i"])
        # Race-Fix (Fortsetzung): Preserve gerenderte States aus prev_scenes wenn Text
        # identisch ist. Überschreibt nur file/status/source_url/t/source_url_ts, lässt
        # den frisch generierten prompt/concrete_entity/video_prompt/phasen unangetastet.
        preserved = dashboard._preserve_rendered_scenes(prev_scenes, scenes)
        if preserved:
            print(f"  [Plan] {preserved} bereits gerenderte Szene(n) erhalten "
                  f"(gleicher Text, file+status aus altem Plan übernommen).", flush=True)
        if prompt_error_scenes:
            print(f"  [Plan] WARNUNG: {len(prompt_error_scenes)} Szene(n) mit fehlgeschlagener "
                  f"Prompt-Generierung (prompt_error): {prompt_error_scenes} — Prompt-Text vor "
                  f"Bild-Generierung manuell prüfen/überschreiben.", flush=True)
        dashboard._assign_phases(scenes, analysis, len(scenes))
        # Phase H: also default 'speaker' here for the manual-script path (Phase I's
        # `_transcribe_generate_worker` does the same). Combined they ensure every
        # scene has a `speaker` field regardless of which path generated the plan.
        for s in scenes:
            if "speaker" not in s:
                s["speaker"] = "narrator"
        out_data = {"scenes": scenes, "wpm": wpm, "sec": sec, "characters": analysis.get("characters", [])}
        dashboard._atomic_write_json(v_plan(cid, vid), out_data, ensure_ascii=False, indent=1)

        # Juli 2026 — Auto-Generate Charsheets pro Video:
        # Nach der Script-Analyse liegen die Charaktere (mit id, name_or_role, visual_description)
        # vor. Für jeden Eintrag OHNE existierendes Charsheet im Video-Verzeichnis wird ein
        # 5-Pose-Sheet generiert (via Gemini Vision, bestehende gen_charsheet-Funktion).
        # So hat jedes Video seinen eigenen Charakter-Pool, ohne Müll aus alten Videos.
        characters = analysis.get("characters", []) or []
        if characters:
            with dashboard._PLAN_JOBS_LOCK:
                dashboard.PLAN_JOBS[key]["step"] = f"Generiere {len(characters)} Charakter-Sheet(s) …"
            sheet_dir = ch_sheets(cid, vid)
            existing_sheets = {}  # safe_id -> name (lowercased name for fuzzy match)
            existing_sheets_by_name = {}  # lowercased name -> safe_id
            try:
                for f in os.listdir(sheet_dir):
                    if not f.endswith(".json"):
                        continue
                    sid = os.path.splitext(f)[0]
                    try:
                        m = json.load(open(os.path.join(sheet_dir, f)))
                        n = (m.get("name") or sid).strip().lower()
                        existing_sheets[sid] = n
                        existing_sheets_by_name[n] = sid
                    except Exception:
                        existing_sheets[sid] = sid.lower()
                        existing_sheets_by_name[sid.lower()] = sid
            except OSError:
                pass
            generated = []
            skipped = []
            failed = []
            for ch in characters:
                ch_id = (ch.get("id") or "").strip()
                ch_name = (ch.get("name_or_role") or ch_id or "character").strip()
                ch_desc = (ch.get("visual_description") or "").strip()
                if not ch_id or not ch_desc:
                    skipped.append(ch_name or "?")
                    continue
                # Schutzlogik (Juli 2026, User-Report "Charakter Carreyrou wurde
                # überschrieben"): Wenn LLM eine neue ID zurückgibt (z.B. weil es den
                # Charakter-Namen statt der bisherigen ID wählt), aber ein Sheet mit
                # dem GLEICHEN NAMEN existiert, soll das alte Sheet beibehalten werden.
                # Sonst überschreibt Auto-Generate das vorhandene Charsheet mit einer
                # anderen Datei-ID und löscht das Original.
                name_lower = ch_name.lower()
                if ch_id in existing_sheets:
                    skipped.append(ch_id)
                    continue
                if name_lower in existing_sheets_by_name:
                    skipped.append(f"{ch_id} (name-collision mit {existing_sheets_by_name[name_lower]})")
                    continue
                try:
                    img_bytes = gen_charsheet(cid, ch_name, ch_desc, vid=vid)
                    if img_bytes:
                        # Speichere als <safe>.png + <safe>.json im Video-Verzeichnis
                        sd = ch_sheets(cid, vid)
                        os.makedirs(sd, exist_ok=True)
                        with open(os.path.join(sd, f"{ch_id}.png"), "wb") as f:
                            f.write(img_bytes)
                        with open(os.path.join(sd, f"{ch_id}.json"), "w", encoding="utf-8") as f:
                            json.dump({
                                "name": ch_name, "safe": ch_id,
                                "description": ch_desc,
                                "anonymize": ch.get("anonymize", False),
                                "generated_at": time.time(),
                            }, f, ensure_ascii=False, indent=1)
                        generated.append(ch_id)
                except Exception as e:
                    print(f"  [Plan] Charsheet-Generierung für '{ch_id}' fehlgeschlagen: {e}", flush=True)
                    failed.append(ch_id)
            print(f"  [Plan] Charsheets: generiert={len(generated)}, "
                  f"übersprungen (existiert bereits oder leer)={len(skipped)}, "
                  f"fehlgeschlagen={len(failed)}", flush=True)

        with dashboard._PLAN_JOBS_LOCK:
            dashboard.PLAN_JOBS[key] = {"running": False, "step": "Fertig", "error": None, "done": True, "ts": time.time()}
        print(f"  [Plan] {cid}/{vid}: fertig, {len(scenes)} Szenen", flush=True)
    except Exception as e:
        traceback.print_exc()
        with dashboard._PLAN_JOBS_LOCK:
            dashboard.PLAN_JOBS[key] = {"running": False, "step": "Fehler", "error": str(e), "done": False, "ts": time.time()}
