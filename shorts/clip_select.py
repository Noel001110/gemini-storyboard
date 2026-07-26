"""shorts/clip_select.py — Szenen-Auswahl + Audio-Extraktion für die Short-Version
eines bereits gerenderten Videos (Strategie A, MVP "hook_prefix" -- siehe Plan).

Voraussetzung: das Video wurde mindestens einmal als Longform gerendert -- plan.json
hat dann start_aligned/end_aligned pro Szene (frame-genau, aus dem Whisper/ElevenLabs-
Alignment), und die pausen-gekürzte Sprecherspur liegt unter uploads/. Eine Short ist
eine Zweitverwertung eines fertigen Videos, kein eigener Skript-Pfad.

Bewusst dashboard-frei (keine v_plan/v_out-Imports) -- nimmt Pfade/Daten als Argumente
entgegen, damit es sich isoliert testen lässt (Hand-Rolled-Runner-Muster wie
tests/test_pipeline_fixes.py). shorts/api.py verdrahtet die echten Pfade.
"""
from __future__ import annotations

import os
import subprocess

# User-Vorgabe: TikToks Monetarisierungs-Schwelle (Creator Rewards Program) verlangt
# Videos >= 60s -- MIN_DURATION ist deshalb eine UNTERGRENZE, kein Deckel (vorherige
# Fassung deckelte bei 58s und konnte je nach Skript deutlich darunter landen).
# MAX_DURATION bleibt bewusst eng (User-Vorgabe: max. 1:10) -- ein "Short" soll kurz
# bleiben, nicht die vollen 3 Minuten ausreizen, die YouTube inzwischen erlaubt.
MIN_DURATION_SEC_DEFAULT = 60.0
MAX_DURATION_SEC_DEFAULT = 70.0

# Reale Phase-Taxonomie dieser Codebase (verifiziert gegen ein echtes plan.json,
# channels/thestick/videos/the_raise_nobody_noticed_2_0): OPENING/RISING_ACTION/
# CLIMAX/RESOLUTION -- es gibt KEINE "HOOK"-Phase. OPENING ist das reale Äquivalent
# (Korrektur einer früheren Plan-Annahme, die von "HOOK" ausging).
# Priorität für den "hook_prefix"-Ansatz: Hook zuerst, dann der Höhepunkt, dann der
# Ausklang -- RISING_ACTION (typischerweise der "Aufbau"-Teil) kommt zuletzt dazu und
# nur, wenn OPENING+CLIMAX+RESOLUTION zusammen die Mindestdauer noch nicht erreichen.
PHASE_PRIORITY = ("OPENING", "CLIMAX", "RESOLUTION", "RISING_ACTION")


def select_scenes(plan: dict, min_duration: float = MIN_DURATION_SEC_DEFAULT,
                   max_duration: float = MAX_DURATION_SEC_DEFAULT) -> list[dict]:
    """Wählt Szenen nach PHASE_PRIORITY, bis min_duration erreicht ist (Untergrenze,
    kein Deckel) -- reicht OPENING+CLIMAX allein nicht, wird mit RESOLUTION, zuletzt
    RISING_ACTION aufgefüllt. Die Auswahl läuft phasen-priorisiert, das Ergebnis wird
    aber wieder in Original-Story-Chronologie zurücksortiert, bevor es zurückgegeben
    wird -- sonst würde der Short die Handlung durcheinander abspielen.
    Braucht start_aligned/end_aligned (aus einem bereits erfolgten Longform-Render);
    Szenen ohne diese Felder werden übersprungen (kein Render = keine verlässliche
    Zeitbasis für den Audio-Schnitt unten)."""
    scenes = [s for s in plan.get("scenes", [])
              if s.get("start_aligned") is not None and s.get("end_aligned") is not None
              and (s["end_aligned"] - s["start_aligned"]) > 0]
    by_phase: dict = {}
    for s in scenes:
        by_phase.setdefault(s.get("phase"), []).append(s)

    # _render_worker richtet die Short-Audiospur NACH dem Zusammenschneiden erneut per
    # Whisper aus und kürzt Pausen ERNEUT (dieselbe Pipeline wie beim Longform-Render,
    # siehe build_short_plan_scenes-Docstring) -- das schneidet nochmal ein paar
    # Sekunden Luft aus der bereits einmal getrimmten Audiospur (empirisch ~3%).
    # Deshalb wird bei der AUSWAHL selbst ein Sicherheitspuffer draufgeschlagen, damit
    # das fertige Video nach dem zweiten Trim zuverlässig >= min_duration bleibt --
    # nie über max_duration hinaus, das bleibt die harte Obergrenze.
    target = min(min_duration + 6.0, max_duration)

    selected_ids: set = set()
    total = 0.0
    for phase in PHASE_PRIORITY:
        if total >= target:
            break
        for s in by_phase.get(phase, []):
            if total >= target:
                break
            seg_dur = s["end_aligned"] - s["start_aligned"]
            if total + seg_dur > max_duration:
                continue  # diese eine Szene würde den Deckel sprengen -- überspringen, nicht abbrechen
            selected_ids.add(s["i"])
            total += seg_dur

    # Fallback: extrem kurzes Skript, bei dem selbst alle vier Phasen zusammen die
    # Mindestdauer nicht erreichen -- restliche Original-Reihenfolge auffüllen.
    if total < target:
        for s in scenes:
            if s["i"] in selected_ids:
                continue
            seg_dur = s["end_aligned"] - s["start_aligned"]
            if total + seg_dur > max_duration:
                continue
            selected_ids.add(s["i"])
            total += seg_dur
            if total >= min_duration:
                break

    return [s for s in scenes if s["i"] in selected_ids]


def split_into_parts(plan: dict, min_duration: float = MIN_DURATION_SEC_DEFAULT,
                      max_duration: float = MAX_DURATION_SEC_DEFAULT) -> list:
    """Läuft ALLE Szenen chronologisch durch (keine Phasen-Filterung wie select_scenes)
    und gruppiert sie in aufeinanderfolgende Parts für die "ganzes Video als Serie"-
    Pipeline (shorts/api.py: /api/shorts/split_parts). Ein Part wird abgeschlossen,
    sobald er die (gepufferte) Zieldauer erreicht -- NIE mitten in einer Szene getrennt
    (User-Vorgabe: "net apprupt im Satz enden"). Eine einzelne Szene, die für sich
    genommen schon > max_duration ist, wird trotzdem als eigener (übergroßer) Part
    akzeptiert -- Schnittqualität schlägt striktes Dauerlimit. Derselbe 6s-Sicherheits-
    puffer wie select_scenes (siehe dort) gleicht den zweiten Pause-Trim-Durchlauf in
    _render_worker aus. Der letzte Part kann kürzer als min_duration ausfallen, wenn der
    Rest des Videos nicht mehr reicht."""
    scenes = [s for s in plan.get("scenes", [])
              if s.get("start_aligned") is not None and s.get("end_aligned") is not None
              and (s["end_aligned"] - s["start_aligned"]) > 0]
    scenes.sort(key=lambda s: s["i"])

    target = min(min_duration + 6.0, max_duration)
    parts: list = []
    current: list = []
    total = 0.0
    for s in scenes:
        seg_dur = s["end_aligned"] - s["start_aligned"]
        if current and total >= target:
            parts.append(current)
            current = []
            total = 0.0
        current.append(s)
        total += seg_dur
    if current:
        parts.append(current)
    return parts


def build_short_audio(scenes: list[dict], src_audio_path: str, out_audio_path: str,
                       tmp_dir: str) -> float:
    """Extrahiert [start_aligned, end_aligned] pro gewählter Szene aus src_audio_path
    und hängt sie in Originalreihenfolge aneinander (ffmpeg -ss/-t pro Segment + concat-
    Demuxer, gleiches Muster wie _assemble_clips für Video). Gibt die tatsächliche
    Gesamtdauer zurück (ffprobe auf das fertige Ergebnis -- Ground Truth für
    _apply_sync_invariant, nicht die Summe der Einzeldauern, die durch ffmpeg-
    Rundung minimal abweichen kann)."""
    seg_paths = []
    concat_list_path = os.path.join(tmp_dir, "_short_concat_list.txt")
    try:
        for idx, s in enumerate(scenes):
            seg_path = os.path.join(tmp_dir, f"_short_seg_{idx:03d}.wav")
            dur = s["end_aligned"] - s["start_aligned"]
            cmd = ["ffmpeg", "-y", "-i", src_audio_path,
                   "-ss", str(s["start_aligned"]), "-t", str(dur),
                   "-c", "pcm_s16le", seg_path]
            result = subprocess.run(cmd, capture_output=True, timeout=30)
            if result.returncode != 0:
                raise RuntimeError(
                    f"Audio-Segment-Extraktion fehlgeschlagen (Szene {s.get('i')}): "
                    f"{result.stderr.decode(errors='replace')[-300:]}")
            seg_paths.append(seg_path)

        with open(concat_list_path, "w") as f:
            for sp in seg_paths:
                f.write(f"file '{sp}'\n")
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path,
               "-c", "pcm_s16le", out_audio_path]
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f"Audio-Concat fehlgeschlagen: "
                                f"{result.stderr.decode(errors='replace')[-300:]}")
    finally:
        for sp in seg_paths:
            try: os.remove(sp)
            except OSError: pass
        try: os.remove(concat_list_path)
        except OSError: pass

    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                             "-of", "csv=p=0", out_audio_path],
                           capture_output=True, text=True, timeout=15)
    return float(probe.stdout.strip())


def build_short_plan_scenes(selected: list[dict]) -> list[dict]:
    """Baut die Szenenliste fürs short_plan.json aus den gewählten Original-Szenen.

    Löscht start_aligned/end_aligned/words/clip_file/transition_type -- diese Felder
    beziehen sich auf die ALTE (lange) Zeitachse und wären auf der neuen, kürzeren
    Short-Audiospur falsch. _render_worker's needs_alignment-Check (dashboard.py)
    sieht dadurch fehlende start_aligned/words und stößt automatisch einen frischen
    Whisper-Lauf auf der neuen Short-Audiospur an -- exakt dieselbe, bereits
    existierende Alignment-Logik wie beim Longform-Render, keine neue Zeit-Umrechnung
    nötig. `dur` wird auf die tatsächliche (bekannte, korrekte) Original-Segmentlänge
    gesetzt -- der beste verfügbare Startwert für _apply_sync_invariant.
    `motion` bleibt unangetastet: _render_worker berechnet Motion ohnehin bei JEDEM
    Render neu (nie gecacht), unabhängig vom scene-dict-Inhalt."""
    scenes = []
    running_start = 0.0
    for orig in selected:
        s = dict(orig)
        dur = orig["end_aligned"] - orig["start_aligned"]
        s["dur"] = dur
        s["start"] = running_start
        running_start += dur
        for key in ("start_aligned", "end_aligned", "words", "clip_file", "transition_type"):
            s.pop(key, None)
        scenes.append(s)
    return scenes
