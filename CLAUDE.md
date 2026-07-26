# CLAUDE.md — Arbeitsregeln für dieses Repo

Dieses Repo ist ein lokales Single-User-Tool, aktiv in Produktion genutzt. Die
Regeln hier sind aus dem Enterprise-Refactor (Phase S–3, siehe Git-Log ab
"Checkpoint: working state before enterprise refactor") entstanden — jede
Regel entspricht einem konkreten, während des Refactors gelösten Problem, nicht
generischer Best-Practice-Prosa. `ARCHITECTURE.md` beschreibt WAS das System
tut; diese Datei beschreibt WIE neuer Code reinkommt.

## Zero-Framework bleibt Zero-Framework

Die Haupt-App (`dashboard.py` + `engine/`) ist stdlib-only, kein FastAPI/Flask/
DI-Container (bewusste Nutzer-Entscheidung). Neue Runtime-Deps NUR im isolierten
`.venv_whisper` (Whisper/Pillow-Subprozess) und dort hart gepinnt in
`requirements-whisper.txt`. Dev-Tooling (ruff/mypy/pytest) in
`requirements-dev.txt`, identisch zu `.github/workflows/lint.yml` gepinnt.

## Layering / Import-Richtung (strikt einseitig)

```
app/ (zukünftig) → routes/ → workers/ → engine/ / store/
alles darf core/ importieren, core/ importiert NICHTS davon zurück
```

- **`core/`**: neutrales Fundament (`paths.py`, `logging.py`, `jobs.py`). Darf
  NIE `dashboard`, `engine`, `routes`, `workers` importieren — das ist der ganze
  Zweck (löst den `import dashboard`-Zirkel). Ab Tag 1 mypy-strict.
- **`engine/`**: reine Pipeline-Logik (Prompts, Render, Audio, Bild-Provider).
  Mypy-strict. Darf `core/` importieren, nie `dashboard`/`routes`/`workers`.
- **`workers/`**: Hintergrund-Jobs (Plan/Batch/Render/Thumbnail/Produce), aus
  `dashboard.py` extrahiert. Importiert `core/` und `engine/` direkt oben im
  Modul. Für die noch-nicht-extrahierten God-Modul-Helfer aus `dashboard.py`
  (Job-Dicts+Locks, `load_v_meta`, `get_channel_style_refs`, ...): **lazy
  `import dashboard` INNERHALB der Funktion**, nie oben im Modul (sonst
  Zirkel beim Programmstart). Gleiche, bereits produktiv laufende Konvention
  wie `shorts/api.py`, `youtube/upload.py`, `control/api.py`.
- **`dashboard.py`**: schrumpft schrittweise, re-exportiert extrahierten Code
  unter altem Namen (`from workers.thumbnail import run as _thumbnail_generate_worker`),
  damit kein bestehender Call-Site bricht. Bleibt bis Phase 5 mypy-`ignore_errors`.

## mypy — was strict ist und warum

- Strict: `core/*`, `engine/*`.
- `ignore_errors = true` (Übergang, nicht Endzustand): `dashboard`,
  `engine_elevenlabs`, `render_overlay`, `workers.*`. Grund: ein voll
  typisierter Aufrufer meldet für JEDEN Call in eine (noch) untypisierte
  Funktion `no-untyped-call` — das ist mypy-Default-Verhalten, nicht
  strict-spezifisch. Bis `dashboard.py` selbst typisiert ist (Phase 5+), ist
  das erwartetes Rauschen, kein Regressions-Signal. Nicht versuchen, das durch
  `# type: ignore`-Spam an den Call-Sites zu unterdrücken — das Modul-Override
  ist der richtige Ort.
- `PIL.*`/`numpy.*`: `follow_imports = "skip"` — ihre Stubs nutzen teils
  Syntax, die mit `python_version = "3.11"` kollidiert, lokal reproduzierbar
  sobald `.venv_whisper` numpy installiert hat (CI hat kein numpy, sieht das
  Problem nie). Nicht `python_version` global hochziehen, um das zu fixen —
  das öffnet ein größeres Fass.
- `make lint` läuft `mypy engine/ core/`. Neue Module in `workers/`/`routes/`
  laufen mit, sobald sie im Makefile-Target ergänzt werden — dran denken, wenn
  ein Modul strict werden soll.

## JobRegistry (`core/jobs.py`) — wann einsetzen, wann nicht

Es existiert eine fertige, getestete `JobRegistry` als Ersatz für die 8
globalen `(dict + threading.Lock)`-Paare in `dashboard.py`
(`JOBS`/`BATCH_JOBS`/`PLAN_JOBS`/`THUMB_JOBS`/`RENDER_JOBS`/`VOICE_JOBS`/
`PRODUCE_JOBS`/`ACTIVE_SCENE_JOBS`). **Bewusste Entscheidung: nicht alle 8
vorab migrieren.** Ein Dict piecemeal auf die Registry umzustellen, während
die anderen 7 noch Rohdicts sind, schafft zwei parallele Wahrheiten für
denselben Zustand-Typ ohne echten Zusatznutzen. Migriere ein Dict NUR dann,
wenn du sowieso den Code extrahierst/verschiebst, der es besitzt (dann WIRD
die Registry gebraucht, weil der neue Ort `dashboard.BATCH_JOBS` nicht mehr
ohne Zirkel erreichen kann) — nicht als eigenständigen "Aufräum"-Schritt.

## Tests

- Runner ist **pytest** (`make test` / `python3 -m pytest`), nicht mehr die
  hand-gerollten `run()`-Skripte direkt (die funktionieren als Fallback
  weiterhin über `python3 tests/test_*.py`, sind aber nicht mehr der Gate).
- Zwei Namenskonventionen sind gültig: `t_*` (die 250+ bestehenden, historisch
  gewachsenen Tests — NICHT umbenennen) und `test_*` (idiomatisch, für neuen
  Code bevorzugt, siehe `tests/test_core_jobs.py`). Beide werden von pytest
  automatisch eingesammelt (`python_functions` in `pyproject.toml`).
- `tests/conftest.py` bindet modul-eigene `setup()`/`teardown()` generisch ein
  — neue Testdateien mit Channel/HOME-Fixture-Bedarf folgen dem Muster in
  `tests/test_pipeline_fixes.py`, neue Testdateien ohne Fixture-Bedarf (reine
  Unit-Tests wie `core/jobs.py`) brauchen kein `setup()`/`teardown()`.
- **Bekannte, akzeptierte Baseline-Ausnahme:** `t_phase_m_dashboard_size_below_4000`
  schlägt aktuell fehl (Datei ist noch >4000 Zeilen). Das ist erwartet bis
  Phase 4 den Handler weiter zerlegt — kein neuer Fehler, nicht versuchen,
  künstlich zu "fixen" (z.B. durch Code auslagern ohne echten Grund).

## Verifikation vor jedem risikoreichen Commit

Für Änderungen an `dashboard.py`, `engine/`, `workers/`, `routes/` (nicht für
reine Doku/Config-Änderungen):

1. `make test` — muss grün sein (außer der oben dokumentierten Ausnahme).
2. Live-Smoke-Test: Server auf einem Test-Port starten, `/api/health` prüfen,
   `SIGTERM` senden, verifizieren dass der Prozess sich wirklich beendet
   (nicht nur ein Flag setzt — siehe Phase 2b-Commit für den Präzedenzfall).
3. Bei Änderungen an tatsächlicher Pipeline-Logik (Bild/Plan/Render/Audio):
   ein echter, kleiner API-Call gegen einen Scratch-Kanal/-Video (NICHT
   `channels/thestick` mit echten Videos anfassen — einen leeren Testordner
   unter `channels/<real_channel>/videos/test/` benutzen, danach wieder
   löschen; `channels/` ist ohnehin `.gitignore`d). Kein voller 20-Minuten-
   Longform-Lauf nötig — ein einzelner Schritt (ein Thumbnail, ein Plan mit
   kurzem Test-Skript) reicht, um zu beweisen dass der echte Codepfad
   (KIE/ElevenLabs-Call inklusive) funktioniert, nicht nur die Unit-Tests.
4. Committen mit einer Nachricht, die WARUM erklärt (nicht nur WAS) — siehe
   bisherige Commits ab dem Refactor-Start als Stil-Referenz.

## Frontend (`dashboard.html`)

- Single-File, stdlib-served (kein Build-Step), Alpine.js + Inline-JS.
- Schritt-Karten sind nummeriert `① … ⑦`, jede Zahl GENAU EINMAL, passend zu
  `data-step-section="1".."7"` auf der jeweils tatsächlich sichtbaren Karte.
  Neue Schritte: Nummer lückenlos einreihen, nicht ans Ende anhängen, wenn sie
  fachlich früher im Prozess gehören.
- Legacy-Pfade (Klimax-Short 9:16, T2V/Video-Modus, Parts-Pipeline) sind
  bewusst NICHT gelöscht, nur aus dem sichtbaren Flow entfernt (`display:none`
  bzw. entfernter Umschalter) — Backend bleibt erreichbar. Nicht wieder
  sichtbar machen ohne explizite Nutzer-Freigabe. Neue "vom Hauptpfad
  abweichende" Experimente folgen demselben Muster: hidden + kommentiert,
  nicht gelöscht, bis explizit klar ist dass sie nie wieder gebraucht werden.

## Was NICHT hierher gehört

Sicherheitskritische/riskante Aktionen (History-Rewrite, `--force`-Push, Löschen
von `channels/`-Daten außerhalb von Scratch-Test-Ordnern) sind NICHT durch
"kannst automatisch weitermachen"-Freigaben für den Refactor gedeckt — dafür
weiterhin explizit nachfragen, unabhängig davon was hier steht.
