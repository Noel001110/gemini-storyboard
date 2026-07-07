# Plan — Nächste Phasen A bis J (Stand nach PR #1, Juli 2026)

**Repo:** `Noel001110/gemini-storyboard`, Branch: `main` @ `53a3e9b` (PR #1 gemerged)
**Vorlauf:** Q (sec-Defaults) und Phase 1 (ElevenLabs) sind live auf `main`. Dieses Dokument plant die nächste Welle.
**Quellen:** `ARCHITECTURE.md` (590 Z.), `IMPLEMENTATION_PLAN.md` (575 Z.), `IMPLEMENTATION_PLAN_NEXT.md`, vorhandenes Code-Verständnis.

---

## 0. Vorab — Reihenfolge-Logik

Die vom Auftraggeber empfohlene Reihenfolge A → B → (C+D parallel) → E → (F+G) → (I+H) → J ist sinnvoll. Begründungen im Originaltext, hier nur die Abhängigkeits-Graph-Form:

```
A ─┐
   ├─→ B ─┬─→ C (parallel mit D)
   │      ├─→ D (parallel mit C)
   │      ├─→ E
   │      ├─→ F
   │      └─→ G
   │      ├─→ I ─→ H
   │      └───────────────→ J (orthogonal, irgendwann)
   └─────────────────────────────────────────────→ J (orthogonal)
```

A ist unabhängig und entkoppelt (Doku). B unlockt C-G (alle brauchen ein verlässliches `phase`-Feld). I+H sind eine eigene Sprach-Einheit. J ist orthogonal.

---

## 1. Phase A — Doku-Update (ARCHITECTURE.md + IMPLEMENTATION_PLAN.md)

**Ziel:** die Doku spiegelt den aktuellen Code (Stand nach PR #1 = Q + Phase 1 + Upstream-Cinematic) und die nächste Welle klar abgrenzt. Aufwand: klein, additiv, kein Code-Touch.

### A.1 — `ARCHITECTURE.md`

**A.1.1 — Neue Sektion §22 „Quick-Win Q (sec-Defaults)"** (zwischen §21 und Dateiende)

Inhalte:
- Was sich geändert hat: `sec`-Default `4 → 5.5`, max `10 → 8`, Backend-Konstante `NORMAL_HARD_CAP_SEC = 5.5`
- Wo: `dashboard.html:306` (HTML) und `dashboard.py` Z. ~631/680 (Backend)
- Honest disclosure: empirische Scene-Count-Reduktion ist **kleiner als** die einfache Audio-Dauer/sec-Schätzung suggeriert (die echte `segment_by_pacing`-Logik hat zusätzlich `target_words` und `hard_cap_words`, die den reinen Default-Shift relativieren). Der UI-Default-Shift + max-Begrenzung sind trotzdem additiv wertvoll.
- Smoke-Test-Footnote: Cap greift von `sec=10` auf 5.5, keine Scene > `MAX_SCENE_SEC`.

**A.1.2 — Neue Sektion §23 „Phase 1: ElevenLabs-Voiceover"**

Inhalte:
- **Was:** Option C im Frontend, MP3 + Word-Timestamps vom Provider, kein Whisper-Lauf im Hauptpfad.
- **Datenmodell-Erweiterungen** (additiv): `voiceover_source`, `voiceover_task_id`, `voiceover_word_timestamps`, `voiceover_chars`, `voiceover_settings_used` — in `audio_meta.json` und `plan.json`. Channel-scoped Files: `channels/<cid>/voice_id.txt` + `voice_settings.json`.
- **Neuer Job-Dict:** `VOICE_JOBS` + `_VOICE_JOBS_LOCK`, in `_cleanup_stale_jobs` integriert (analog zu den 5 bestehenden Dicts).
- **Architektur-Entscheidung (wichtig!):** Phase 1 erweitert `_transcribe_generate_worker` und `_render_worker` um `voiceover_source`-Awareness — **kein neuer Background-Thread, kein neuer Worker**. Variante A (User-Upload) und Variante C (ElevenLabs) laufen durch denselben Worker, nur mit unterschiedlichem `transcribe_and_segment`-Bypass. Begründung im Plan §2.6 von `IMPLEMENTATION_PLAN_NEXT.md`.
- **11er-API-Verhalten:** `with-timestamps`-Endpoint, Retry 5/10/20s für 429/5xx, **sofortiger raise** für 4xx/Schema-Drift. **Kein** stillschweigender Fallback auf Whisper (ARCHITECTURE §6.1).
- **Atomic-Write-Strategie:** `voiceover.mp3` → `audio_meta.json`. Wenn Meta-Write fehlschlägt, wird MP3 wieder gelöscht — kein halbpersistierter Zustand.
- **Idempotenter Resume-Marker:** wenn `voiceover.mp3` + `plan.json` + `audio_meta.json` (mit `voiceover_source="elevenlabs"` und Timestamps) bereits existieren, kein zweiter API-Call — Response `{resume: true}`.

**A.1.3 — Sektion §6.1 (Job-Dicts) ergänzen**

Im `JOBS`-Block §6.1 die Liste von 5 auf 6 Job-Dicts erweitern: `JOBS, BATCH_JOBS, PLAN_JOBS, RENDER_JOBS, PRODUCE_JOBS, VOICE_JOBS`. `_cleanup_stale_jobs` iteriert jetzt über 6 statt 5 — Text entsprechend.

**A.1.4 — Sektion §16.5 (Alignment-Pivot) updaten**

Aktueller Text redet von `Whisper`-Pass für beide Pfade. Jetzt: **drei Quellen** für Word-Timestamps, je nach `voiceover_source`:
- `voiceover_source="elevenlabs"` → Timestamps vom Provider (Phase 1), kein Netzwerk-Call
- `voiceover_source="user_upload"` (oder fehlt) → Whisper-Pass wie bisher
- `_transcribe_generate_worker` hat den ElevenLabs-Fast-Path schon, überspringt Gemini-Transcription wenn Timestamps vorhanden

### A.2 — `IMPLEMENTATION_PLAN.md`

**A.2.1 — Errata-Block oben erweitern** (nach dem bestehenden ElevenLabs-Scribe-Block, Z. 11)

Neuer Errata-Eintrag: „Stand 2026-07: Quick-Win Q (sec-Defaults) und Phase 1 (ElevenLabs-Voiceover) sind produktiv. Die Phase-Nummerierung in §8 unten ist **vor** diesem Update entstanden und stimmt mit der neuen Reihenfolge in `IMPLEMENTATION_PLAN_NEXT.md` und `ARCHITECTURE.md` §§22-23 nicht mehr überein — der Leser soll sich an `ARCHITECTURE.md` orientieren."

**A.2.2 — Sektion §8 (Phasen-Liste) als „historisch, neuere Phasen in `IMPLEMENTATION_PLAN_NEXT.md`" markieren**

Hinweis-Box: „Diese Liste ist Stand vor Q + Phase 1. Für die aktuelle Welle (Phase 3 Story-Phase-Engine, Phase 4-9 Cinematic) siehe `IMPLEMENTATION_PLAN_NEXT.md` und `ARCHITECTURE.md` §22-23."

### A.3 — Verifikation

- `grep -E "Quick-Win|Phase 1 ElevenLabs|NORMAL_HARD_CAP|voiceover_source" ARCHITECTURE.md` → muss ≥4 Treffer haben
- `grep -E "VOICE_JOBS" ARCHITECTURE.md` → muss ≥1 Treffer haben
- Visuell: Inhaltsverzeichnis ganz oben in ARCHITECTURE.md ergänzen um §§22, 23
- Doku-Lesbarkeit: jemand der ARCHITECTURE.md ohne Vorkenntnisse liest, findet den aktuellen Code vollständig erklärt.

### A.4 — Commit

- `docs: ARCHITECTURE §22-23 (Q + Phase 1 ElevenLabs), PLANNING Errata-Block erweitern`
- Eigener Commit, **vor** Phase B.

---

## 2. Phase B — Story-Phase-Engine (Cinematic-Plan §3, alt)

**Ziel:** die Position-basierte Phase-Heuristik (`story_phase(i, total)`) durch eine **LLM-getriebene** Dramaturgie-Analyse ersetzen. Aktiviert sofort `_PHASE_MOTION_CANDIDATES` (schon im Code), macht Phase 4-9 erst sinnvoll möglich.

### B.1 — Datenmodell-Erweiterung (additiv)

**`plan.json` (Top-Level):** nichts Neues auf Top-Level nötig.

**`plan.json["scenes"][i]`** — additive Felder:
```jsonc
{
  // ...alles bestehend...
  "phase": "OPENING" | "RISING_ACTION" | "CLIMAX" | "RESOLUTION",  // single source of truth
  "phase_source": "llm" | "position-fallback",                     // Herkunft (Debug-Greifbarkeit)
  "is_phase_break": bool,    // true wenn diese Szene einen Akt-Wechsel markiert
  "is_climax": bool,         // true wenn diese Szene der dramaturgische Höhepunkt ist
  "act_index": int,          // 0=Setup, 1=Rising, 2=Climax, 3=Resolution
}
```

**`analyze_script`-Output** (LLM-Response, additiv):
```jsonc
{
  // ...alles bestehend (locations, characters, props, arc, callbacks, pacing, visual_sequences, callouts)...
  "phases": [                  // neu: pro Beat (gleicher Index-Raum wie `pacing`)
    {"beat": 0,  "phase": "OPENING"},
    {"beat": 1,  "phase": "OPENING"},
    ...
  ],
  "act_breaks": [12, 35, 58],  // neu: Beat-Indizes wo ein Akt endet / nächster beginnt
  "climax_beat": 35,           // neu: Einzelindex des dramaturgischen Höhepunkts (oder -1 wenn unklar)
}
```

### B.2 — `analyze_script` LLM-Prompt erweitern

**Aktueller Prompt** (Z. 926) instruiert bereits: locations, characters, props, arc, callbacks, **pacing** (calm/normal/punchy). **NEU dazu** (am Ende anhängen, additiv):

> **DRAMATURGY (strictly enforced):**
> 1. Assign a STORY-PHASE to every beat: one of `"OPENING" | "RISING_ACTION" | "CLIMAX" | "RESOLUTION"`. These reflect actual narrative arc, NOT position. Beats near the start are usually OPENING, but a flashback or cold-open may legitimately be in RESOLUTION.
> 2. Identify `act_breaks`: list of beat indices where the dramatic situation changes irreversibly (inciting incident, midpoint reversal, climax, resolution). Typical 3-act structure: 2 breaks; complex narratives: up to 4. Empty list `[]` if the script is single-act.
> 3. Identify the single `climax_beat`: index of the highest-tension moment, where protagonist faces the decisive confrontation. Set `-1` if no clear climax (informational scripts, pure exposition).
> 4. Output as JSON: `{..., "phases": [{"beat": int, "phase": str}], "act_breaks": [int], "climax_beat": int}`. Every beat must have a phase entry. Empty `act_breaks` is valid.

### B.3 — Position-basierte Heuristik als Fallback (mit 80%-Hysterese)

Die Funktion `story_phase(i, total)` (Z. 1168) bleibt **erhalten** als Fallback-Pfad. Anwendungsreihenfolge:

**Hysterese:** ein LLM-Coverage unter 80% aller Beats wird als **Schema-Drift** interpretiert — dann **vollständiger Fallback**, kein teilweises LLM-Vertrauen (entweder genug Daten oder keins). Vermeidet Szenen mit fragwürdigen LLM-Phasen zwischen Szenen mit sicheren Fallback-Phasen.

```
IF LLM-Phasen decken >= 80% aller Beats ab:
    für jede Szene mit LLM-Beat:
        s["phase"]         = LLM-Wert (priorisiert)
        s["phase_source"]  = "llm"
        s["is_phase_break"]= (i ∈ act_breaks)
        s["is_climax"]     = (i == climax_beat)
        s["act_index"]     = mapping[phase]
    für jede Szene OHNE LLM-Beat (Lücke):
        s["phase"]         = story_phase(i, total)
        s["phase_source"]  = "position-fallback"
ELSE (LLM-Coverage < 80%):
    für alle Szenen:
        s["phase"]         = story_phase(i, total)
        s["phase_source"]  = "position-fallback"
        s["is_phase_break"]= false
        s["is_climax"]     = false
        s["act_index"]     = min(3, (i*4 // total))
```

`story_phase()` wird **nicht gelöscht** — sie bleibt die deterministische Backstop.

### B.4 — Persistence-Hook: `_assign_phases()`

Eine zentrale Stelle ersetzt die heute verstreuten `s["phase"] = story_phase(...)`-Aufrufe (Z. 2517 in `_plan_generate_worker`, Z. 3457 in `_transcribe_generate_worker`):

```python
def _assign_phases(scenes: list, analysis: dict, total: int) -> None:
    """Phase 3: LLM-driven phase assignment with 80%-coverage hysteresis.
    Single source of truth = s['phase']; s['phase_source'] = 'llm' | 'position-fallback'."""
    llm_phases = {p.get("beat"): p.get("phase")
                  for p in (analysis or {}).get("phases", [])
                  if p.get("phase") in {"OPENING", "RISING_ACTION", "CLIMAX", "RESOLUTION"}}
    act_breaks = set(analysis.get("act_breaks") or [])
    climax_beat = analysis.get("climax_beat", -1)
    phase_to_act = {"OPENING": 0, "RISING_ACTION": 1, "CLIMAX": 2, "RESOLUTION": 3}
    coverage = len(llm_phases) / max(1, total)
    use_llm = coverage >= 0.8

    n_llm = n_fb = 0
    for s in scenes:
        beat = s.get("beat_index", s["i"])
        if use_llm and beat in llm_phases:
            s["phase"] = llm_phases[beat]
            s["phase_source"] = "llm"
            s["is_phase_break"] = (beat in act_breaks)
            s["is_climax"] = (beat == climax_beat)
            s["act_index"] = phase_to_act[s["phase"]]
            n_llm += 1
        else:
            s["phase"] = story_phase(s["i"], total)
            s["phase_source"] = "position-fallback"
            s["is_phase_break"] = False
            s["is_climax"] = False
            s["act_index"] = min(3, (s["i"] * 4 // max(1, total)))
            n_fb += 1
    print(f"  [Phase] {n_llm}/{total} LLM, {n_fb}/{total} fallback "
          f"(coverage={coverage*100:.0f}%, hysteresis={'ON' if use_llm else 'OFF'})", flush=True)
```

**Wichtig:** `beat_index` ist im Default-Pfad gleich `s["i"]`. Es gibt den LLM-Daten die Möglichkeit, eine **andere Nummerierung** als die finale Scene-Reihenfolge zu verwenden (z.B. wenn Skript Szenen 1, 5, 9 als Climax markiert — die `i`-Werte stimmen mit dem Beat-Index überein, sobald `split_units` eins-zu-eins gemappt wird).

Aufruf in beiden Workern direkt nach der `analyze_script`-Zeile (Z. 2501 in `_plan_generate_worker`, Z. 3437 in `_transcribe_generate_worker`).

### B.5 — `_PHASE_MOTION_CANDIDATES` Aktivierung verifizieren

Existierender Code (Z. 1879-1897) prüft `scene.get("phase")` zuerst — **kein Code-Change nötig**, sobald `s["phase"]` aus B.4 gesetzt ist. Smoke-Test: nach B.4 muss eine `OPENING`-Szene `pan_right/pan_left/tilt_down` aus dem Kandidaten-Pool bekommen, eine `CLIMAX`-Szene `snap_zoom_in`/`diagonal_glide`/`static`. Dies ist ein deterministischer Selektor (`scene["i"] % len(candidates)`), keine Zufalls-Auswahl — Resume bleibt deterministisch.

### B.6 — Frontend-Badges

In `dashboard.html` §Szenen-Rendering (`renderScenes()`):

- Phase-Badge pro Szene: kleines Pill in der oberen rechten Ecke der Szenen-Karte, farbcodiert:
  - `OPENING` → neutral grau
  - `RISING_ACTION` → blau
  - `CLIMAX` → rot (mit subtilem Glow / `outline`)
  - `RESOLUTION` → grün
- `is_climax === true` → zusätzlich subtiler goldener Ring um die ganze Szenen-Karte
- `is_phase_break === true` → dünner vertikaler Strich links neben der Karte (Akt-Trennlinie)

CSS-Anpassungen klein (Klassen `phase-pill-<phase>`, `is-climax`, `is-phase-break`).

### B.7 — Verifikation

**Test B.1 — Schema-Stabilität:**
- `analyze_script`-Prompt ohne `phases`/`act_breaks` (leere Felder): `_assign_phases` fällt vollständig auf `story_phase()` zurück, alle Szenen `phase_source="position-fallback"`, Counter loggt `"0/N LLM, N/N fallback (coverage=0%, hysteresis=OFF)"`. Kein Crash.
- `analyze_script`-Prompt mit vollständigen Daten: alle drei neuen Felder + `phase_source="llm"` auf Szenen gesetzt.

**Test B.2 — 80%-Hysterese:**
- Synthetischer Test mit Coverage=50% (LLM liefert nur jeden zweiten Beat): vollständiger Fallback (kein Mix). Counter zeigt `0/N LLM, N/N fallback (coverage=50%, hysteresis=OFF)`.
- Coverage=85%: LLM-Phasen werden akzeptiert, fehlende Beats fallen pro Szene auf `story_phase()` mit `phase_source="position-fallback"`.

**Test B.3 — Motion-Aktivierung:**
- 8-Min-Skript mit LLM-Phasen rendern, alle `CLIMAX`-Szenen bekommen `snap_zoom_in`/`diagonal_glide`/`static` (deterministisch, scene.i modulo). Bei `phase_source="llm"` = "Motion aus LLM-Phase"; bei `"position-fallback"` = "Motion aus Position-Phase" (visuell identisch, falls LLM-Daten sinnvoll).

**Test B.4 — Backward-Compat:**
- Alter Plan ohne `phase`/`is_phase_break`/`is_climax`/`phase_source`: öffnen, rendern, funktioniert. `story_phase()`-Pfad greift, `phase_source="position-fallback"` wird beim Speichern ergänzt (additive Migration).

**Test B.5 — Frontend:**
- Plan mit LLM-Daten rendern, Browser: alle 4 Phasen-Badges sichtbar, Climax-Szene hat goldenen Ring, Akt-Brüche haben Trennlinie. `phase_source="position-fallback"` ist im **plan.json** sichtbar (Debug-Via-grep), nicht in UI.

**Test B.6 — Cold-Open (Killer-Szenario):**
- Skript beginnt mit Flash-Forward (Beat 0 = CLIMAX, Beat 30 = OPENING-Rückblende). LLM erkennt das, position-basierte Heuristik nicht. Test rendert: erste Szene ist klar als CLIMAX markiert, Motion = `snap_zoom_in`, Farb-Grading = Phase-D (hoher Kontrast). Position-Fallback hätte OPENING geliefert → falsche Motion und Farbe.

### B.8 — Commit

- `feat: Story-Phase-Engine (Phase 3) — LLM-driven act_breaks/climax_beat/phases`
- Eigener Commit, nach A.

### B.9 — Design-Entscheidung (geklärt 2026-07)

**Gewählt:** LLM-Daten überschreiben Position-Heuristik wenn Coverage ≥ 80% aller Beats. Single-Source-of-Truth bleibt `s["phase"]`. Zusatz-Feld `s["phase_source"]: "llm" | "position-fallback"` für Debug-Greifbarkeit (`grep "position-fallback" plan.json` listet sofort die Szenen wo LLM nichts geliefert hat). Hysterese-Schwelle 80% verhindert Mix aus LLM- und Fallback-Phasen bei Schema-Drift.

**Begründung:** Cold-Open-Erkennung ist der Killer-Use-Case — Position-Heuristik kann einen Flash-Forward nicht von einem echten Start unterscheiden. LLM sieht die Story und macht es richtig.

**Code:** siehe `_assign_phases()` in B.4.

---

## Anhang A — geklärte Design-Entscheidungen

- **B.9 Phase-Engine:** LLM-Override mit 80%-Hysterese + `phase_source`-Debug-Feld. Geklärt 2026-07, siehe B.9.
- **A.4 Reihenfolge:** A vor B — Doku muss aktuell sein, bevor das nächste Onboarding sie braucht.
- **C + D parallel:** unabhängig voneinander, beide hängen nur an B's `phase`-Feld. Können in einer Implementierungs-Session zusammen gemacht werden.
- **G Stem-Musik:** braucht Asset-Beschaffung. Wenn keine Stems gefunden werden, Phase G postponen oder überspringen — nicht erzwingen.
- **I vor H:** TTS-Preprocessing liefert SSML-Struktur für Multi-Speaker. Strikte Reihenfolge.
- **J am Ende:** orthogonal, nur wenn Code stabil ist. Nicht parallel zu Feature-Wellen.