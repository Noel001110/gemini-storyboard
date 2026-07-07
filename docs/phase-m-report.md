# Phase M — Engine-Extract: Abschlussbericht (2026-07-07)

## Ergebnis

**Dashboard.py: 4657 → 3285 Zeilen (-1372, -29%).**

| Modul | Zeilen | Inhalt |
|---|---|---|
| `engine/scenes.py` | 292 | Text-Segmentierung, Pacing, Sequenz-Ketten (Doppel-Anker) |
| `engine/render.py` | 527 | Visuelle Render-Pipeline (ffmpeg), Motion-/Transition-Bibliotheken |
| `engine/audio.py` | 210 | Sound-Design (Musik + SFX + Voice-Mixing) |
| `engine/prompts.py` | 568 | Prompt-Komposition, Char-Sheet-Pipeline, LLM-Bild-Generierung |
| `engine/__init__.py` | 15 | Modul-Doku |
| `routes/__init__.py` | 67 | Routing-Konvention, `register_engine_paths()`, `ENDPOINT_REGISTRY` |
| **dashboard.py** | **3285** | **App-Init, Job-Tracker, Worker, HTTP-Handler, KIE-/Veo-/Whisper-Integration** |

## Was erreicht wurde

### M.0 — Verzeichnisstruktur
Leere `engine/` und `routes/` Packages angelegt. Keine Code-Änderung.

### M.1 — Routing-Konvention
Da der Handler in `dashboard.py` 60+ globale Funktionen referenziert, wurde **kein**
Komplett-Umzug versucht (zu hoher Risiko-Diff). Stattdessen:
- `register_engine_paths()` für Lazy-Import-Sicherheit
- `ENDPOINT_REGISTRY` als Erweiterungspunkt für spätere Handler-Migration

### M.2 — `engine/scenes.py` + §11.4-Tests
**Wichtigste Phase.** 6 Funktionen + 4 Konstanten extrahiert. Gleichzeitig:
- **S1-Fix**: Docstring von `_batch_generate_worker` korrigiert (behauptete „no ordering
  dependency" während die Funktion `_resolve_chain_refs` aufruft).
- **7 neue Tests** für die §11-Schwachstellen S1–S4 (siehe `tests/test_cinematic_e2e.py`).

### M.3 — `engine/render.py`
15 Render-Funktionen + 4 Render-Konstanten + 4 Bibliotheken + 2 PNG-Helper extrahiert.
Lazy-Import für `PHASE_COLOR_FILTER` (Zyklenfreiheit zu `engine_elevenlabs`).

### M.4 — `engine/audio.py`
5 Audio-Funktionen + 3 Audio-Konstanten extrahiert. Lazy-Imports zu `engine.render`
(`_transition_for_scene`) und `engine_elevenlabs` (`PHASE_VOLUME`).

### M.5 — `engine/prompts.py`
6 Prompt-Funktionen + Char-Sheet-Pipeline extrahiert. `PHASE_COVERAGE_THRESHOLD` wieder
in `dashboard.py` ergänzt (war im Kommentar-Block mit verloren gegangen).

### M.6 — LLM-Pipeline raus
- Schritt 1: `visual_prompts` + Validations-Helper + `_image_prompt_chunk` +
  `_image_prompt_single_retry` + `_IMAGE_PROMPT_FEWSHOT` nach `engine/prompts.py`.
- Schritt 2: `generate_script`, `generate_titles`, `make_thumbnail_prompt`,
  `gen_thumbnail_image` + die 3 System-Prompts (`SCRIPT_SYSTEM`, `TITLE_SYSTEM`,
  `THUMBNAIL_PROMPT_SYSTEM`) nach `engine/prompts.py`.

### M.7 — Final-Verifikation (dieser Branch)
- **61/61 Tests grün** (54 alte + 7 neue §11.4-Tests).
- Alle Cross-Module-Identitäten verifiziert (dashboard.X is engine.Y).
- Migrations-Bericht (dieses Dokument).

## Was NICHT erreicht wurde

**Ziel des Plans** war `dashboard.py → 600-800 Zeilen`. **Tatsächlich: 3285 Zeilen.**

Was bleibt in `dashboard.py`:

| Block | Zeilen | Warum noch da |
|---|---|---|
| HTTP-Handler `H` | ~1075 | 60+ globale Refs — vollständiger Umzug ist eigene Phase (M.7+) |
| Worker-Orchestratoren (`_render_worker`, `_batch_generate_worker`, `_plan_generate_worker`, `_produce_worker`) | ~700 | Bleiben — sie sind echte Orchestratoren, koordinieren Engine-Module |
| KIE-Image-Pipeline (`gen_image`, `_kie_submit_image`, Worker, Polling) | ~430 | Eigenes `engine/kie.py` machbar, aber zyklus-anfällig — spätere Phase |
| Veo-Video-Pipeline (`gen_veo`, `extend_veo`, `poll_veo`, `make_video_prompt`) | ~280 | Eigenes `engine/veo.py` machbar |
| Whisper-Transkription | ~270 | Eigenes `engine/whisper.py` machbar |
| State-Management (JOBS, BATCH_JOBS, Path-Helpers, CRUD) | ~300 | Orchestrator-Boilerplate |
| Diverse Helper | Rest | |

**Ehrliche Einschätzung:** Das Ziel war ehrgeizig und setzt vollständige Handler-Extraktion
voraus. Die jetzige Reduktion von 29% ist substanziell — und beseitigt genau die XXL-Quelle
(die Render-/Audio-/Szenen-Logik in Z. 1300-2700), die das größte Wachstums-Risiko hatte.

**Was NICHT in den Monolithen wachsen wird** ab jetzt:
- Phase K (Sound-Pool): Code wächst in `engine/audio.py`, nicht `dashboard.py`
- Phase L (Hook/Leitfrage): in `engine/prompts.py` (analyze_script)
- Phase O (Akzent-Puls): in `engine/render.py`
- Phase N (Daten-Overlays): in `render_overlay.py` oder `engine/render.py`
- Phase P (Grading-Refinement): in `engine/render.py`
- Phase 39 (I2V): in neuem `engine/veo.py`

## Tests

`tests/test_cinematic_e2e.py` enthält jetzt **61 Tests**:

| Phase | Anzahl | Inhalt |
|---|---|---|
| Round-3 (33.4.2-prep, 33.4.1, 33.4.2, 34, 34.1, 33.x-Stepper) | 27 | UI-Rebuild-Phasen (Stepper, Settings, TTS-Provider) |
| Cross-Phase Integration | 1 | Realistic script through all 8 cinematic phases |
| Round-5 (Resume-Safety) | 5 | ElevenLabs double-click, KIE 429 retry, frontend XSS, race detection, whisper warn |
| Phase B-H-J (Cinematic Phases 1) | 14 | Story-Phase-Engine, Pacing, Color-Grading, Title-Cards, Counter, Music-Volume, TTS-Enrichment, Engine-Refactor |
| **Phase 11 (§11.4) Sequence-Chain** | **7** | **NEU in M.2: Doppel-Anker, todo-Reihenfolge, Timeout, Continuity, Motion-Vererbung, Renumber, S1-Docstring-Fix** |
| Phase 33.x UI (33.1-33.4.2) | 7 | HTML/CSS-Tests |

Alle 61 Tests grün.

## Re-Exporte (Rückwärtskompatibilität)

Alle aus `dashboard.py` extrahierten Symbole werden per `from engine.X import *` re-exportiert.
Dadurch funktionieren alle bestehenden Caller (`dashboard._render_clip(...)`, etc.) ohne
Änderung. **Identitäts-Check** (gleiche `is`-Beziehung): verifiziert.

## Konventionen für Folge-Phasen

1. **Lazy-Imports zwischen engine-Modulen** (vermeidet Zyklen)
2. **`__all__` in jedem engine-Modul** (klare Public-API)
3. **Re-Export in `dashboard.py`** (Rückwärtskompatibilität)
4. **Kein Cross-Module-State** (jedes Modul ist self-contained)
5. **Konstanten im Modul, das sie braucht** (z.B. `RENDER_FPS` in `engine/render.py`,
   nicht in `dashboard.py`)

## Folge-Schritte nach M

Siehe `CINEMATIC_UPGRADE_PLAN.md` §8.3 (kombinierte Reihenfolge):
- **B-1**: Char-Upload-Bug fixen (eigener Branch, parallel)
- **Phase Q + 38**: `flat_cartoon_doc` Default-Preset (universelles Tool, kein Strichmännchen)
- **Phase K**: Sound-Pool + MUSIC_BEDS (sobald kuratierte Assets da sind)
- **Phase L**: Hook + Leitfrage (nach §11.4-Tests)
- **Phase O, N, P**: Akzent-Puls, Daten-Overlays, Grading-Refinement
- **Phase 39**: T2V→I2V
- **35, 36**: Test-Coverage, Architecture-Lint

Jede Cinematic-Phase wächst jetzt in das thematisch passende `engine/*`-Modul statt
in den Monolithen. Die Dashboard-Größe bleibt stabil.