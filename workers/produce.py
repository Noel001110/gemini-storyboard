"""workers/produce.py — Ein-Knopf-Orchestrator (aus dashboard.py extrahiert,
Refactor Phase 3). Siehe workers/__init__.py für die lazy-import-Konvention.

Kein neuer fachlicher Baustein -- verkettet nur die drei bereits einzeln
extrahierten Worker (Plan/Transkription -> Bilder -> Rendern) hintereinander.
Ruft dashboard._plan_generate_worker / dashboard._batch_generate_worker /
dashboard._render_worker auf -- das sind die bereits nach workers/plan.py,
workers/batch.py, workers/render.py verschobenen Funktionen, über dashboard.py
re-exportiert (kein direkter Import von dort, um dieselbe lazy-import-
Konvention wie jeder andere God-Modul-Helfer beizubehalten).
"""
from __future__ import annotations

import json
import os
import time
import traceback

from core.paths import v_audio, v_plan


def run(cid: str, vid: str, text: str = "", wpm: float = 130.0, sec: float = 4.0) -> None:
    """Plan (falls nötig) -> Alle Bilder generieren -> Rendern, nacheinander. Bevorzugt
    einen bereits bestehenden Plan; sonst ein hochgeladenes Voice-over (Option A); sonst
    das übergebene `text` (Option B, manueller Skript-Pfad). Bricht bei Fehler in einer
    Etappe sofort ab, der Etappen-Name + Fehlergrund landen in PRODUCE_JOBS."""
    import dashboard  # lazy, siehe workers/__init__.py

    key = (cid, vid)

    def set_stage(stage):
        with dashboard._PRODUCE_JOBS_LOCK:
            if key in dashboard.PRODUCE_JOBS:
                dashboard.PRODUCE_JOBS[key]["stage"] = stage

    def stop_requested():
        with dashboard._PRODUCE_JOBS_LOCK:
            return dashboard.PRODUCE_JOBS.get(key, {}).get("stop_requested", False)

    def fail(stage, msg):
        with dashboard._PRODUCE_JOBS_LOCK:
            dashboard.PRODUCE_JOBS[key] = {"running": False, "stage": stage, "stop_requested": False,
                                            "error": msg, "file": None, "ts": time.time()}
        print(f"  [Produce] {cid}/{vid}: Fehler in Etappe '{stage}': {msg}", flush=True)

    try:
        plan_path = v_plan(cid, vid)
        try:
            has_plan = bool(json.load(open(plan_path)).get("scenes"))
        except Exception:
            has_plan = False

        if not has_plan:
            if stop_requested():
                return fail("plan", "Abgebrochen (Stop angefordert)")
            set_stage("plan")
            if os.path.exists(v_audio(cid, vid)):
                try:
                    dashboard._transcribe_generate_worker(cid, vid, sec)
                except Exception as e:
                    return fail("plan", f"Transkription fehlgeschlagen: {e}")
            elif text.strip():
                with dashboard._PLAN_JOBS_LOCK:
                    dashboard.PLAN_JOBS[key] = {"running": True, "step": "Startet …", "error": None, "done": False}
                dashboard._plan_generate_worker(cid, vid, text, wpm, sec)
                with dashboard._PLAN_JOBS_LOCK:
                    plan_err = dashboard.PLAN_JOBS.get(key, {}).get("error")
                if plan_err:
                    return fail("plan", plan_err)
            else:
                return fail("plan", "Kein Voice-over hochgeladen und kein Skript eingegeben.")

        if stop_requested():
            return fail("images", "Abgebrochen (Stop angefordert)")
        set_stage("images")
        with dashboard._BATCH_JOBS_LOCK:
            if dashboard.BATCH_JOBS.get(key, {}).get("running"):
                return fail("images", "Bild-Generierung läuft bereits für dieses Video.")
        dashboard._batch_generate_worker(cid, vid)
        with dashboard._BATCH_JOBS_LOCK:
            batch_err = dashboard.BATCH_JOBS.get(key, {}).get("error")
        if batch_err:
            return fail("images", batch_err)

        if stop_requested():
            return fail("render", "Abgebrochen (Stop angefordert)")
        set_stage("render")
        # Der One-Button-"Produce"-Flow rendert bewusst nur Longform -- eigener
        # (cid, vid, "longform")-Schlüssel statt des obigen `key` (cid, vid), damit er
        # nie denselben RENDER_JOBS-Eintrag wie ein parallel laufender Shorts-Render
        # desselben Videos trifft (siehe _render_worker-Docstring).
        render_key = (cid, vid, "longform")
        with dashboard._RENDER_JOBS_LOCK:
            dashboard.RENDER_JOBS[render_key] = {"running": True, "stop_requested": False, "stage": "startet",
                                                  "done": 0, "total": 0, "error": None, "file": None,
                                                  "started_ts": time.time()}
        dashboard._render_worker(cid, vid, "longform")
        with dashboard._RENDER_JOBS_LOCK:
            render_state = dict(dashboard.RENDER_JOBS.get(render_key, {}))
        if render_state.get("error"):
            return fail("render", render_state["error"])

        with dashboard._PRODUCE_JOBS_LOCK:
            dashboard.PRODUCE_JOBS[key] = {"running": False, "stage": "fertig", "stop_requested": False,
                                            "error": None, "file": render_state.get("file"), "ts": time.time()}
        print(f"  [Produce] {cid}/{vid}: fertig → {render_state.get('file')}", flush=True)
    except Exception as e:
        traceback.print_exc()
        with dashboard._PRODUCE_JOBS_LOCK:
            cur_stage = dashboard.PRODUCE_JOBS.get(key, {}).get("stage", "unbekannt")
        fail(cur_stage, str(e))
