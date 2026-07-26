"""routes/images.py — Prefix /api/generate_one.

Fünfzehnte Route-Gruppe aus dem dashboard.py-Handler (Refactor Phase 4,
Teil 15). Erste der beiden "harten" Routen: ~130 Zeilen inline Business-Logik
(Prompt-Bau, Charsheet-/Chain-/Style-Ref-Auflösung), die -- anders als
batch/render/plan/thumbnail/produce -- NIE in ein workers/-Modul extrahiert
wurde. Dieser Move ist bewusst eine reine Verschiebung (keine Logik-
Extraktion nach engine/workers vorab) -- der Nutzer hat sich explizit gegen
den größeren "69-Funktionen"-Umbau entschieden und für "Plan zu Ende machen"
(= Phase 4 fertigstellen).

_resolve_chain_refs/_resolve_entity_ref/_find_charsheet_png (engine/scenes.py)
und get_public_charsheet_url (engine/imagegen.py) sind bereits mypy-strict
ohne dashboard.py-Abhängigkeit -- Top-Level-Import sicher. Die noch nicht
extrahierten dashboard.py-Helfer (scene_depicts_people, nearest_character_entity,
charsheet_refs_for_entity, get_channel_style_refs, read_master,
get_video_image_model, _atomic_write_json, _image_job_worker, JOBS,
ACTIVE_SCENE_JOBS, IMAGE_GEN_SEMAPHORE, _PLAN_WRITE_LOCK,
_ACTIVE_SCENE_JOBS_LOCK) bleiben lazy importiert.
"""
from __future__ import annotations

import json
import os
import threading
import time

from core.paths import v_out, v_plan
from engine.imagegen import _kie_submit_image, get_public_charsheet_url
from engine.prompts import _build_image_prompt
from engine.scenes import _find_charsheet_png, _resolve_chain_refs, _resolve_entity_ref


def handle(method, path, handler, qs, cid, vid, body):
    if method != "POST" or path != "/api/generate_one":
        return False, None

    import dashboard

    if not vid:
        handler._send(400, {"error": "Kein Video ausgewählt"})
        return True, None
    d = body or {}
    i = int(d["i"])
    prompt = d.get("prompt", "")
    scene_key = (cid, vid, i)
    with dashboard._ACTIVE_SCENE_JOBS_LOCK:
        existing_job_id = dashboard.ACTIVE_SCENE_JOBS.get(scene_key)
        if existing_job_id and dashboard.JOBS.get(existing_job_id, {}).get("status") == "running":
            # Already generating this exact scene (double-click, second tab, or
            # "Alle generieren" running twice) — hand back the SAME job instead
            # of paying for and starting a second KIE task.
            print(f"  [KIE] Szene {i} bereits in Arbeit ({existing_job_id}) — dupliziere nicht", flush=True)
            handler._send(200, {"ok": True, "job_id": existing_job_id, "deduped": True})
            return True, None
    fn = f"{i:03d}.jpg"
    out_path = os.path.join(v_out(cid, vid), fn)
    # Phase C: read phase from plan.json so the image prompt gets the
    # PHASE_PROMPT_ADDITIONS hard-injection when available. plan.json is the
    # single source of truth for scene state.
    # Juli 2026 Fix (NameError-Edge): scene_for_phase MUSS vor dem try existieren —
    # vorher warf ein fehlschlagender json.load() eine NameError auf der
    # anschließenden scene_for_phase.get()-Zeile, weil die except-Klausel nur
    # scene_phase zurücksetzte, nie scene_for_phase selbst.
    scene_for_phase: dict = {}
    try:
        plan = json.load(open(v_plan(cid, vid)))
        scene_for_phase = (plan.get("scenes") or [{}])[i] if i < len(plan.get("scenes", [])) else {}
        scene_phase = scene_for_phase.get("phase", "")
    except Exception:
        plan = {}
        scene_phase = ""
    entity = str(scene_for_phase.get("concrete_entity", ""))
    # Fix 1/4 auch hier (siehe _batch_generate_worker): dieser Pfad übergab
    # ebenfalls char_refs=None und kein entity — ausgerechnet der Klick, mit dem
    # man ein misslungenes Bild korrigiert, schickte den Steckbrief nie mit.
    prompt_entity = entity
    if (not entity.startswith("char_")) and scene_for_phase \
            and dashboard.scene_depicts_people(scene_for_phase):
        prompt_entity = dashboard.nearest_character_entity(plan, scene_for_phase) or entity
    char_refs, entity_key = dashboard.charsheet_refs_for_entity(plan, cid, vid, prompt_entity)
    # Vorgezogen (war vorher erst nach dem Semaphore-Acquire weiter unten): wird
    # jetzt schon hier für has_style_refs gebraucht (siehe _build_image_prompt).
    style_ref_urls = dashboard.get_channel_style_refs(cid)
    full_prompt = _build_image_prompt(prompt, dashboard.read_master(cid), char_refs,
                                      phase=scene_phase, entity=entity_key,
                                      has_style_refs=bool(style_ref_urls))
    # Juli 2026 Fix (Audit A2 "generate_one hat den Fallback-Fix nicht"): dieser
    # Pfad hatte bisher eine eigene, abgespeckte Inline-Logik, die NUR source_url
    # kannte — ausgerechnet der manuelle "Neu generieren"-Klick (mit dem man
    # schlechte Bilder korrigiert) hatte dadurch NIE den 3-Stufen-Fallback
    # (Charsheet-PNG, Name-Match, lokale Bilddatei) aus _resolve_entity_ref, den
    # der Batch-Worker seit Juli 2026 hat. Jetzt identische Logik, nur mit
    # wait=False (kein paralleler Sibling-Dispatch bei einem Einzelklick, also
    # kein Grund auf eine Anchor-Szene zu warten).
    entity_refs, entity_debug = _resolve_entity_ref(v_plan(cid, vid), scene_for_phase, wait=False)
    if entity_debug.get("is_local"):
        entity_refs = [get_public_charsheet_url(ref) for ref in entity_refs]

    # A2, gleiche Logik wie im Batch-Worker (_batch_generate_worker) — zweiter
    # Charakter in derselben Szene bekommt seine eigene Referenz mit angehängt.
    secondary_entity = str(scene_for_phase.get("secondary_entity", "") or "")
    if secondary_entity and secondary_entity != prompt_entity:
        sec_png, _sec_dbg = _find_charsheet_png(plan, cid, vid, secondary_entity)
        if sec_png:
            entity_refs = entity_refs + [get_public_charsheet_url(sec_png)]

    if entity_refs:
        if scene_for_phase.get("seq_id") is not None and scene_for_phase.get("seq_pos", 0) >= 1:
            full_prompt += (
                "\n\nCONTINUITY (STRICT): This is a continuation of the exact same "
                "shot as the reference image(s). You MUST perfectly match the "
                "identity, outfit, and background environment shown in the "
                "references. Change ONLY the camera angle/framing or the specific "
                "action described above.")
        else:
            full_prompt += (
                "\n\nCHARACTER CONTINUITY: The reference image shows this exact character. "
                "Preserve their FACIAL identity first — same eyes, nose, face shape — then "
                "hair color/style, then outfit and build. Keep hands correct (five fingers). "
                "Vary only pose, framing and expression to fit this scene's action above.")
    # Global concurrency cap — blocks here if MAX_CONCURRENT_IMAGE_GENS are already
    # in flight from ANY source (batch or other individual clicks), instead of
    # firing this KIE submission immediately alongside all the others. Released by
    # _image_job_worker once this scene's generation fully finishes.
    dashboard.IMAGE_GEN_SEMAPHORE.acquire()
    # Juli 2026 (User-Report: "sobald kein Mensch im Prompt ist, denkt er sich
    # was aus"): Referenzbild jetzt an JEDE Szene (nicht mehr nur char_-Entities)
    # — es ist ein reiner STIL-Anker (siehe Master-Prompt), kein erzwungenes
    # "diese Person muss hier stehen" mehr, daher unproblematisch auch für
    # Landschafts-/Symbol-Szenen. Nur wenn schon ein spezifischeres, bereits
    # generiertes Bild desselben Charakters existiert (entity_refs), gilt NUR das
    # — nie beide Referenzbilder gleichzeitig (siehe Farb-Inkonsistenz-Fix in
    # _batch_generate_worker). Keine Charsheet-Text/Bild-Injection mehr (alte,
    # kanalweite Charsheets aus einem anderen Video überstimmten sonst die
    # Szenenbeschreibung).
    chain_refs, chain_debug = _resolve_chain_refs(v_plan(cid, vid), scene_for_phase)
    # D1 (Evaluation Juli 2026, Fund 1): Style-Ref(s) IMMER anhängen, nicht mehr
    # nur wenn kein Chain-/Entity-Anchor existiert -- sonst verliert die
    # Einzelklick-Generierung genau wie der Batch-Worker den Grafik-Stil-Anker bei
    # Charakter-Szenen. Reihenfolge: Identität zuerst, Stil zuletzt. Audit Juli
    # 2026 (Bereich 3): bis zu 3 Style-Refs statt nur einem.
    use_style_ref = bool(style_ref_urls)
    refs = chain_refs + entity_refs + (style_ref_urls if use_style_ref else [])
    try:
        task_id = _kie_submit_image(full_prompt, model=dashboard.get_video_image_model(cid, vid),
                                     ref_urls=refs or None)
    except Exception as e:
        dashboard.IMAGE_GEN_SEMAPHORE.release()
        handler._send(500, {"error": str(e)})
        return True, None
    job_id = f"{cid}_{vid}_{i}_{int(time.time())}"
    dashboard.JOBS[job_id] = {"status": "running", "progress": 0, "file": None,
                              "source_url": None, "ts": None, "error": None}
    with dashboard._ACTIVE_SCENE_JOBS_LOCK:
        dashboard.ACTIVE_SCENE_JOBS[scene_key] = job_id
    # Mark scene as running in plan
    with dashboard._PLAN_WRITE_LOCK:
        try:
            plan = json.load(open(v_plan(cid, vid)))
            for s in plan["scenes"]:
                if s["i"] == i:
                    if prompt:
                        s["prompt"] = prompt
                    s["status"] = "läuft"
            dashboard._atomic_write_json(v_plan(cid, vid), plan, ensure_ascii=False, indent=1)
        except Exception:
            pass
    threading.Thread(
        target=dashboard._image_job_worker,
        args=(job_id, task_id, out_path, v_plan(cid, vid), i, scene_key),
        daemon=True
    ).start()
    handler._send(200, {"ok": True, "job_id": job_id})
    return True, None
