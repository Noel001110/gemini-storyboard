"""workers/batch.py — "Alle Bilder generieren"-Batch-Worker (aus dashboard.py
extrahiert, Refactor Phase 3). Siehe workers/__init__.py für die lazy-import-
Konvention.

_image_job_worker/_image_job_worker_inner/_mark_scene_error bleiben bewusst in
dashboard.py: sie sind KEIN Batch-exklusiver Code, sondern auch vom
/api/generate_one-Einzelklick-Pfad genutzt (der noch nicht extrahiert ist,
siehe Phase 4) -- über dashboard.X erreicht wie jeder andere verbliebene
God-Modul-Helfer.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import time

from core.paths import v_out, v_plan
from engine.imagegen import (
    _CHARSHEET_UPLOAD_CACHE,
    _CHARSHEET_UPLOAD_LOCK,
    _kie_submit_image,
    get_public_charsheet_url,
    upload_image_public,
)
from engine.prompts import _build_image_prompt
from engine.scenes import _find_charsheet_png, _resolve_chain_refs, _resolve_entity_ref
from engine.upscale import upscale_images_batch

# Zeitbasiert statt "warte auf genau N fertige Bilder" (Rollender Upscale-Sweep,
# siehe run()): bei ungerader/kleiner Restmenge (z.B. 4 von 100 Szenen noch offen,
# weil der Rest per Hand generiert wurde) würde ein zähl-basierter Trigger nie
# auslösen. Ein Zeit-Trigger sweept einfach, was gerade fertig ist -- unabhängig
# von der Gesamtzahl, plus ein garantierter Abschluss-Sweep am Ende (siehe unten).
UPSCALE_SWEEP_INTERVAL_SEC = 25


def run(cid: str, vid: str, force: bool = False) -> None:
    """Runs 'Alle Bilder generieren' entirely server-side — survives page reloads and
    tab closes. Dispatches up to MAX_CONCURRENT_IMAGE_GENS scenes at once (KIE's real
    limits support 100+ concurrent tasks, see IMAGE_GEN_SEMAPHORE) instead of one at a
    time.

    IMPORTANT — scene ordering IS a dependency for visual sequences (see
    CINEMATIC_UPGRADE_PLAN.md §11): a scene with seq_pos >= 1 references BOTH its
    sequence anchor (seq_pos 0) AND its immediate predecessor via
    _resolve_chain_refs(). The anchor must be present in plan.json before a
    continuation reads from it — see _wait_for_chain_scene for the poll/timeout
    mechanism.

    The `todo` list below MUST preserve the original scene order — see
    CINEMATIC_UPGRADE_PLAN.md §11.3 Schutzregel 1. No `sort`/`sorted`/`reverse` on
    `todo`. Enforced by t_seq_todo_preserves_scene_order.

    (Sequential, order-dependent Veo extension of a previous scene's last frame is a
    completely separate, still fully-sequential per-click code path around
    MAX_CHAIN_LENGTH; this function never touches it.)"""
    import dashboard  # lazy, siehe workers/__init__.py

    key = (cid, vid)
    plan_path = v_plan(cid, vid)
    try:
        plan = json.load(open(plan_path))
    except Exception as e:
        with dashboard._BATCH_JOBS_LOCK:
            dashboard.BATCH_JOBS[key] = {"running": False, "stop_requested": False, "done": 0,
                                          "total": 0, "current_i": [], "error": f"Plan lesen: {e}", "ts": time.time()}
        return

    scenes = plan["scenes"]
    total = len(scenes)
    # force=True: ALLE Szenen neu generieren (auch bereits vorhandene Bilder);
    # sonst nur die offenen. Reihenfolge bleibt erhalten (§11.3 Schutzregel 1 —
    # kein sort/reverse auf todo).
    todo = list(scenes) if force else [s for s in scenes if not s.get("file")]
    with dashboard._BATCH_JOBS_LOCK:
        dashboard.BATCH_JOBS[key] = {"running": True, "stop_requested": False,
                                      "done": total - len(todo), "total": total,
                                      "current_i": [], "error": None}
    print(f"  [BatchGen] {cid}/{vid}: {len(todo)} von {total} Szenen offen "
          f"(bis zu {dashboard.MAX_CONCURRENT_IMAGE_GENS} parallel)", flush=True)

    master = dashboard.read_master(cid)
    image_model = dashboard.get_video_image_model(cid, vid)
    style_ref_urls = dashboard.get_channel_style_refs(cid)

    def process_scene(scene):
        i = scene["i"]
        with dashboard._BATCH_JOBS_LOCK:
            if dashboard.BATCH_JOBS[key]["stop_requested"]:
                # Stop halts NEW dispatches only — scenes already in flight when Stop
                # was pressed keep running to completion (KIE tasks can't be cancelled
                # mid-flight, and killing the poll loop would just orphan the task).
                return
            dashboard.BATCH_JOBS[key]["current_i"].append(i)
        try:
            # Re-read the plan fresh in case a manual single-scene click already filled
            # this scene in while other scenes were being worked on.
            try:
                fresh_plan = json.load(open(plan_path))
                fresh_scene = next((s for s in fresh_plan["scenes"] if s["i"] == i), None)
                if fresh_scene and fresh_scene.get("file"):
                    return
            except Exception:
                pass

            # Juli 2026 (User: "entweder es geht richtig oder gar nicht, der Rest ist
            # Verschwendung"): eine Szene mit prompt_error=True hat KEINEN echten
            # Bild-Prompt bekommen (nur einen barebones Notprompt nach 3 gescheiterten
            # LLM-Versuchen, siehe visual_prompts). Ein KIE-Aufruf darauf würde nur eine
            # schwache, stilistisch beliebige Bild-Generierung verschwenden. Batch
            # überspringt sie und markiert sie klar als "fehler" statt sie unauffällig
            # mitlaufen zu lassen — manuelles Nachbessern des Prompts vor Einzel-Klick
            # bleibt möglich.
            if scene.get("prompt_error"):
                print(f"  [BatchGen] Szene {i}: prompt_error — übersprungen, "
                      f"Prompt manuell prüfen und Szene einzeln generieren", flush=True)
                with dashboard._PLAN_WRITE_LOCK:
                    try:
                        p2 = json.load(open(plan_path))
                        for s in p2["scenes"]:
                            if s["i"] == i:
                                s["status"] = "fehler"
                        dashboard._atomic_write_json(plan_path, p2, ensure_ascii=False, indent=1)
                    except Exception:
                        pass
                return

            scene_key = (cid, vid, i)
            fn = f"{i:03d}.jpg"
            out_path = os.path.join(v_out(cid, vid), fn)

            with dashboard._ACTIVE_SCENE_JOBS_LOCK:
                existing_job_id = dashboard.ACTIVE_SCENE_JOBS.get(scene_key)
                already_running = bool(existing_job_id and dashboard.JOBS.get(existing_job_id, {}).get("status") == "running")

            if already_running:
                # A manual click is already generating this exact scene — poll for it to
                # finish instead of submitting a second KIE task for the same scene.
                while dashboard.JOBS.get(existing_job_id, {}).get("status") == "running":
                    time.sleep(2)
                # Report it back regardless of whether the OTHER job already upscaled it
                # (manual click, skip_upscale=False) — an already-4K image just no-ops
                # through a redundant sweep pass, cheap insurance against the rarer case
                # where the other in-flight job is itself a second concurrent batch run
                # (skip_upscale=True) that would otherwise never sweep it either.
                if dashboard.JOBS.get(existing_job_id, {}).get("status") == "done":
                    return out_path
                return None
            else:
                # Chain-refs + entity-ref resolution can BLOCK (waiting on a sibling
                # sequence scene or the character's first occurrence, see
                # _wait_for_chain_scene / _wait_for_entity_anchor_scene) — deliberately
                # done here, outside any lock, so a waiting scene never holds
                # _ACTIVE_SCENE_JOBS_LOCK/_BATCH_JOBS_LOCK and doesn't block unrelated
                # scenes from registering/checking in.
                chain_refs, chain_debug = _resolve_chain_refs(plan_path, scene)
                # Conditional character reference (not blindly attached to every scene):
                # only when this scene's chosen concrete_entity actually IS a character
                # from the analysis — pure landscape/symbol scenes skip it, saving KIE
                # tokens and avoiding mis-conditioning a scene with no character in it.
                entity = str(scene.get("concrete_entity", ""))
                # Cross-scene character continuity (Juli 2026, User-Report: "Elizabeth
                # sieht in jeder Szene anders aus"): _resolve_chain_refs only chains
                # scenes inside the same visual sequence — most repeat appearances of a
                # character have no seq_id at all (e.g. scene 0/3/5 with nothing in
                # between), so they had zero reference to each other. This attaches the
                # FIRST generated scene of the same character as a fixed visual anchor.
                entity_refs, entity_debug = _resolve_entity_ref(plan_path, scene)
                # Juli 2026 (User-Report: "sobald kein Mensch im Prompt ist, denkt er
                # sich was aus, wird fast hyperrealistisch"): das globale Referenzbild
                # früher NUR bei entity.startswith("char_") mitgeschickt — das war Restlogik
                # aus der Zeit, als das Referenzbild noch als erzwungene CHARAKTER-Vorgabe
                # galt ("das ist exakt diese Person"), wo ein Referenzbild in einer reinen
                # Landschafts-/Symbol-Szene tatsächlich riskiert hätte, ungewollt eine Person
                # hineinzuzeichnen. Seit dem Umbau auf reinen STIL-Anker (siehe Master-Prompt:
                # "match the reference image's art style") gilt dieses Risiko nicht mehr — im
                # Gegenteil, OHNE Referenzbild hatte jede Nicht-Charakter-Szene gar keinen
                # visuellen Anker mehr und driftete stilistisch ab (genau der "kein Guss"-
                # Effekt). Jetzt: Referenzbild an JEDE Szene, außer es gibt bereits eine
                # spezifischere eigene Referenz (chain_refs = gleicher Shot, entity_refs =
                # erste Erscheinung desselben Charakters) — dann NIE zwei Bilder gleichzeitig
                # (siehe Farb-Inkonsistenz-Fix), sondern nur die spezifischere.
                # Fix 4: Szene zeigt Menschen/Körperteile, hat aber keinen Charakter-Anker
                # (concrete_entity ist ein Objekt). Dann den Charakter heranziehen, um den
                # es im Kontext gerade geht — sonst erfindet das Modell Hautton und Strich
                # frei (Szene 73: weiße, hochglänzende Haut, kompletter Stilbruch).
                prompt_entity = entity
                if (not entity.startswith("char_")) and not entity_refs \
                        and dashboard.scene_depicts_people(scene):
                    fallback_entity = dashboard.nearest_character_entity(plan, scene)
                    if fallback_entity:
                        fb_png, fb_dbg = _find_charsheet_png(plan, cid, vid, fallback_entity)
                        if fb_png:
                            entity_refs = [fb_png]
                            entity_debug = fb_dbg
                            prompt_entity = fallback_entity
                            print(f"  [BatchGen] Szene {i}: kein Charakter-Anker "
                                  f"(entity={entity!r}), aber Menschen im Prompt — nutze "
                                  f"Charsheet von {fallback_entity}", flush=True)

                # Lokale Pfade (aus Charsheets) in öffentliche URLs wandeln
                if entity_debug.get("is_local"):
                    entity_refs = [get_public_charsheet_url(ref) for ref in entity_refs]

                # A2 (Ursache 4, User-Report Bilder UI#84/#93: zwei Personen im selben Bild
                # — nur EINE Referenz wurde angehängt, die zweite Person driftete/wurde neu
                # erfunden). secondary_entity kommt aus visual_prompts() (engine/prompts.py).
                # Eigenständig zu einer öffentlichen URL konvertiert — entity_debug["is_local"]
                # oben beschreibt nur die Herkunft der PRIMÄREN Referenz, nicht dieser hier;
                # _find_charsheet_png liefert immer einen lokalen Pfad, nie eine fertige URL.
                secondary_entity = str(scene.get("secondary_entity", "") or "")
                if secondary_entity and secondary_entity != prompt_entity:
                    sec_png, _sec_dbg = _find_charsheet_png(plan, cid, vid, secondary_entity)
                    if sec_png:
                        entity_refs = entity_refs + [get_public_charsheet_url(sec_png)]
                        print(f"  [BatchGen] Szene {i}: zweiter Charakter "
                              f"{secondary_entity!r} zusätzlich referenziert", flush=True)

                # Evaluation Juli 2026 (Fund 1, "Grafik-Look driftet in Charakter-Szenen"):
                # der Style-Ref wurde bisher WEGGELASSEN, sobald eine Szene schon einen
                # Chain-/Entity-Anchor hatte ("nie zwei Bilder gleichzeitig"). nano-banana-2
                # nimmt aber bis zu 14 Referenzbilder — es gibt keinen technischen Grund,
                # Identitäts- und Stil-Referenz exklusiv zu behandeln. Jetzt: Style-Ref(s)
                # IMMER anhängen, Reihenfolge Identität zuerst (frühe Referenzbilder werden
                # stärker gewichtet, siehe Recherche), Stil zuletzt. Audit Juli 2026
                # (Bereich 3): bis zu 3 Style-Refs statt nur einem.
                use_style_ref = bool(style_ref_urls)
                refs = chain_refs + entity_refs + (style_ref_urls if use_style_ref else [])
                # Fix 1 (Hauptursache): char_refs + entity ÜBERGEBEN. Vorher stand hier
                # `None` und kein entity — dadurch blieb char_hint in _build_image_prompt
                # immer leer, und weder der kanonische Steckbrief ("Narrator: rotes
                # T-Shirt") noch die Konfliktregel ("bei Widerspruch gewinnt das
                # Referenzbild") erreichten KIE jemals. Siehe charsheet_refs_for_entity().
                char_refs, entity_key = dashboard.charsheet_refs_for_entity(plan, cid, vid, prompt_entity)
                full_prompt = _build_image_prompt(scene.get("prompt", ""), master, char_refs,
                                                  phase=scene.get("phase", ""), entity=entity_key,
                                                  has_style_refs=use_style_ref)
                if scene.get("seq_id") is not None and scene.get("seq_pos", 0) >= 1:
                    # Positive constraints only — negated instructions ("do NOT redesign")
                    # are weighted weaker by instruction-following image models and can
                    # even be misread as a focus cue ("pink elephant effect").
                    #
                    # Juli 2026 (User-Report "#28 einer Rapid-Buzzword-Sequenz bleibt trotz
                    # 4x Neu-Generieren 1:1 dasselbe Bild"): verifiziert mit echten Dateien
                    # (jedes Mal andere Bytes/Hash, 4 verschiedene KIE-Task-IDs im Log — kein
                    # Caching-/Routing-Bug). Ursache: die alte Formulierung erlaubte explizit
                    # nur "camera angle/framing" und "die beschriebene Aktion" als Änderung —
                    # welches Wort/welche Zahl auf dem Screen steht, war nirgends genannt, das
                    # Modell fror es also als Teil des zu erhaltenden "background environment"
                    # ein. Additiver Fix, NICHTS von den bestehenden Locks (Identität, Outfit,
                    # Hintergrund-Umgebung, Kamera-Setup, Rendering-Stil) wird gelockert — nur
                    # eine zusätzliche, eng umrissene Erlaubnis für eingeblendeten Text kommt
                    # dazu, die nur bei Screens/Schildern/Flächen mit Text überhaupt greift.
                    full_prompt += (
                        "\n\nCONTINUITY (STRICT): This is a continuation of the exact same "
                        "shot as the reference image(s) — same camera setup, same "
                        "background environment, same overall rendering style. Update the "
                        "following to match THIS scene's description exactly: the camera "
                        "angle/framing, the specific action described above, and any "
                        "on-screen text, word, number or graphic shown on a screen, sign or "
                        "surface — always render exactly what THIS scene's description "
                        "names there, even if the reference image shows a different word "
                        "or graphic in that spot.")
                elif entity_refs:
                    # Same character, but NOT the same shot — unlike the sequence case
                    # above, background/pose/action must follow the scene description,
                    # only the character's identity is pinned to the reference.
                    full_prompt += (
                        "\n\nCHARACTER CONTINUITY: The reference image shows this exact character. "
                        "Preserve their FACIAL identity first — same eyes, nose, face shape — then "
                        "hair color/style, then outfit and build. Keep hands correct (five fingers). "
                        "Vary only pose, framing and expression to fit this scene's action above.")
                print(f"  [BatchGen] Szene {i}: char_ref {'angehängt' if use_style_ref else 'NICHT angehängt'} "
                      f"(concrete_entity={entity!r}), Ketten-Refs: {len(chain_refs)}, "
                      f"Entity-Refs: {len(entity_refs)}", flush=True)

                # Global cap shared with individual clicks (see IMAGE_GEN_SEMAPHORE) —
                # bounds how many scenes (from here or elsewhere) are ever in flight with
                # KIE at once, regardless of how many scenes this batch tries to dispatch.
                dashboard.IMAGE_GEN_SEMAPHORE.acquire()
                try:
                    task_id = _kie_submit_image(full_prompt, model=image_model, ref_urls=refs or None)
                except Exception as e:
                    dashboard.IMAGE_GEN_SEMAPHORE.release()
                    err_text = str(e).lower()
                    retried = False
                    if refs and "credit" not in err_text and "balance" not in err_text and "frequency" not in err_text:
                        # A chain/character reference URL may have expired (KIE's public
                        # temp-hosting isn't permanent) — re-upload the local files fresh
                        # and retry once before giving up the scene over a stale URL.
                        print(f"  [BatchGen] Szene {i}: Submit mit Referenzen fehlgeschlagen ({e}) "
                              f"— lade Referenzen neu hoch und versuche erneut …", flush=True)
                        try:
                            fresh_refs = []
                            for ref_file in (chain_debug.get("chain_anchor_file"), chain_debug.get("chain_prev_file")):
                                if ref_file:
                                    local_path = os.path.join(v_out(cid, vid), ref_file)
                                    if os.path.exists(local_path):
                                        fresh_refs.append(upload_image_public(local_path))
                            # Juli 2026 Fix (Audit A4): der Retry ließ den Entity-Anker
                            # (Charakter-Referenz) bisher komplett weg — ein Submit-Fehler
                            # wegen abgelaufener chain_refs führte dazu, dass der Retry ganz
                            # OHNE Charakter-Referenz lief, selbst wenn die entity_refs-URL
                            # noch gültig gewesen wäre. Zwei Fälle: "anchor-scene" ist eine
                            # KIE-CDN-URL mit TTL — die lokale Bilddatei frisch neu hochladen,
                            # genau wie bei den chain_refs oben. Lokale Charsheets ebenfalls
                            # frisch hochladen (Cache löschen).
                            if entity_debug.get("source") == "anchor-scene" and entity_debug.get("entity_anchor_file"):
                                local_path = os.path.join(v_out(cid, vid), entity_debug["entity_anchor_file"])
                                if os.path.exists(local_path):
                                    fresh_refs.append(upload_image_public(local_path))
                            elif entity_debug.get("is_local") and entity_debug.get("entity_anchor_file"):
                                # D2 (Evaluation Juli 2026): kombinierte Charsheet+Anchor-Refs
                                # haben ZWEI lokale Dateien -- charsheet_file zuerst frisch
                                # hochladen (sonst würde der Retry den Identitäts-Anker
                                # stillschweigend verlieren), dann die Anchor-Szene.
                                charsheet_local = entity_debug.get("charsheet_file")
                                if charsheet_local and os.path.exists(charsheet_local):
                                    with _CHARSHEET_UPLOAD_LOCK:
                                        _CHARSHEET_UPLOAD_CACHE.pop(charsheet_local, None)
                                    fresh_refs.append(get_public_charsheet_url(charsheet_local))
                                local_path = entity_debug["entity_anchor_file"]
                                if os.path.exists(local_path):
                                    with _CHARSHEET_UPLOAD_LOCK:
                                        _CHARSHEET_UPLOAD_CACHE.pop(local_path, None)
                                    fresh_refs.append(get_public_charsheet_url(local_path))
                            elif entity_refs:
                                fresh_refs.extend(entity_refs)
                            if use_style_ref:
                                fresh_refs.extend(style_ref_urls)
                            dashboard.IMAGE_GEN_SEMAPHORE.acquire()
                            try:
                                task_id = _kie_submit_image(full_prompt, model=image_model, ref_urls=fresh_refs or None)
                                retried = True
                            except Exception as e2:
                                dashboard.IMAGE_GEN_SEMAPHORE.release()
                                print(f"  [BatchGen] Szene {i} Submit-Fehler (nach Referenz-Retry): {e2}", flush=True)
                        except Exception as e2:
                            print(f"  [BatchGen] Szene {i}: Referenz-Neu-Upload fehlgeschlagen: {e2}", flush=True)
                    if not retried:
                        print(f"  [BatchGen] Szene {i} Submit-Fehler: {e}", flush=True)
                        dashboard._mark_scene_error(plan_path, i)
                        if "credit" in err_text or "balance" in err_text:
                            # Not a per-scene problem — the account is out of KIE credits,
                            # so every remaining scene would fail identically. Stop
                            # dispatching NEW scenes immediately instead of burning
                            # through the rest of the queue with the same fatal error.
                            with dashboard._BATCH_JOBS_LOCK:
                                dashboard.BATCH_JOBS[key]["stop_requested"] = True
                                dashboard.BATCH_JOBS[key]["error"] = str(e)
                        return
                job_id = f"{cid}_{vid}_{i}_{int(time.time())}"
                dashboard.JOBS[job_id] = {"status": "running", "progress": 0, "file": None,
                                           "source_url": None, "ts": None, "error": None}
                # Round-5 Fix-4 (race-detect): atomic check-and-set in ACTIVE_SCENE_JOBS_LOCK
                # so that two concurrent batch paths (or batch + manual single click)
                # for the SAME scene don't double-submit KIE-Tasks. Without this, a
                # rapid "Generate Scene 5" click + a "Generate all" batch passing
                # through scene 5 would BOTH submit, double-billing the user.
                # Note: there's an earlier dedup-check at L1630-1632 (poll-wait pattern),
                # but it has a TOCTOU window between lock-release and KIE submit — this
                # second check closes it.
                with dashboard._ACTIVE_SCENE_JOBS_LOCK:
                    existing_job = dashboard.ACTIVE_SCENE_JOBS.get(scene_key)
                    if existing_job and dashboard.JOBS.get(existing_job, {}).get("status") == "running":
                        print(f"  [BatchGen] Szene {i} bereits in Arbeit ({existing_job}) — Batch überspringt", flush=True)
                        dashboard.JOBS.pop(job_id, None)   # unused slot
                        dashboard.IMAGE_GEN_SEMAPHORE.release()
                        return
                    dashboard.ACTIVE_SCENE_JOBS[scene_key] = job_id
                # Mark "läuft" in plan.json so the individual scene tiles show "Wird
                # generiert …" while the batch is running, not just for scenes started
                # via a manual single-scene click (that already did this on its own).
                # Also persist the chain/style-ref debug fields (Review-Auflage: sichtbar
                # nachvollziehbar, welche Szenen ohne Charakter-Referenz liefen).
                with dashboard._PLAN_WRITE_LOCK:
                    try:
                        p2 = json.load(open(plan_path))
                        for s in p2["scenes"]:
                            if s["i"] == i:
                                s["status"] = "läuft"
                                s["style_ref_applied"] = use_style_ref
                                if chain_debug.get("chain_anchor_file"):
                                    s["chain_anchor_file"] = chain_debug["chain_anchor_file"]
                                if chain_debug.get("chain_prev_file"):
                                    s["chain_prev_file"] = chain_debug["chain_prev_file"]
                        dashboard._atomic_write_json(plan_path, p2, ensure_ascii=False, indent=1)
                    except Exception:
                        pass
                try:
                    # skip_upscale=True: der Batch-Worker sammelt fertige Bilder selbst
                    # und upscaled sie gebündelt (siehe run() unten, rollender Sweep) --
                    # Verzeichnis-Modus amortisiert den Modell-Load-Overhead über viele
                    # Bilder statt ihn pro Bild einzeln zu zahlen (siehe engine/upscale.py).
                    dashboard._image_job_worker_inner(job_id, task_id, out_path, plan_path, i,
                                                       skip_upscale=True)
                finally:
                    dashboard.IMAGE_GEN_SEMAPHORE.release()
                    with dashboard._ACTIVE_SCENE_JOBS_LOCK:
                        if dashboard.ACTIVE_SCENE_JOBS.get(scene_key) == job_id:
                            del dashboard.ACTIVE_SCENE_JOBS[scene_key]
                if dashboard.JOBS.get(job_id, {}).get("status") == "done":
                    return out_path
                return None
        finally:
            with dashboard._BATCH_JOBS_LOCK:
                dashboard.BATCH_JOBS[key]["done"] += 1
                if i in dashboard.BATCH_JOBS[key]["current_i"]:
                    dashboard.BATCH_JOBS[key]["current_i"].remove(i)

    pending_upscale: list = []
    last_sweep_ts = time.time()

    def sweep(reason: str) -> None:
        nonlocal pending_upscale, last_sweep_ts
        if not pending_upscale:
            return
        n = len(pending_upscale)
        with dashboard._BATCH_JOBS_LOCK:
            dashboard.BATCH_JOBS[key]["stage"] = f"hochskalieren ({n} Bilder) …"
        print(f"  [BatchGen] {cid}/{vid}: Upscale-Sweep ({reason}), {n} Bilder", flush=True)
        upscale_images_batch(pending_upscale)
        pending_upscale = []
        last_sweep_ts = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=dashboard.MAX_CONCURRENT_IMAGE_GENS) as pool:
        futures = []
        for scene in todo:
            with dashboard._BATCH_JOBS_LOCK:
                if dashboard.BATCH_JOBS[key]["stop_requested"]:
                    break
            futures.append(pool.submit(process_scene, scene))
        # as_completed statt Submit-Reihenfolge: liefert jedes fertige Bild sofort,
        # sobald es TATSÄCHLICH fertig ist -- Voraussetzung für den rollenden
        # Upscale-Sweep, der die GPU-Leerlaufzeit während laufender KIE-Downloads
        # nutzt statt erst nach Abschluss ALLER Szenen zu batchen (siehe Chat:
        # KIE-Phase dauert ohnehin ~20min bei 100 Bildern, GPU liegt die ganze
        # Zeit brach -- Sweeps währenddessen machen den Upscale-Anteil quasi gratis).
        for f in concurrent.futures.as_completed(futures):
            out_path = f.result()
            if out_path:
                pending_upscale.append(out_path)
            if pending_upscale and (time.time() - last_sweep_ts) >= UPSCALE_SWEEP_INTERVAL_SEC:
                sweep("rollend")
        # Garantierter Abschluss-Sweep: fängt den Rest ein, ganz gleich wie klein
        # diese letzte Gruppe ist (auch nur 1 Bild) -- kein Bild bleibt unskaliert
        # liegen, unabhängig von Gesamtzahl oder Timing der Sweeps oben (siehe Chat:
        # der Fall "96 statt 100 offen, weil der Rest per Hand generiert wurde" darf
        # nicht dazu führen, dass die letzte Gruppe nie gesweept wird).
        sweep("abschluss")

    with dashboard._BATCH_JOBS_LOCK:
        stopped = dashboard.BATCH_JOBS[key]["stop_requested"]
        dashboard.BATCH_JOBS[key]["running"] = False
        dashboard.BATCH_JOBS[key]["current_i"] = []
        dashboard.BATCH_JOBS[key]["ts"] = time.time()
    print(f"  [BatchGen] {cid}/{vid}: {'gestoppt' if stopped else 'fertig'}", flush=True)
