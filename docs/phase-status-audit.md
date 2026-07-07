# Schwachstellen-Audit gegen aktuellen Stand (2026-07-07)

Konsolidiert aus `Architekturanalyse und Schwachstellenbericht — Storyboard Generator V2.md` (55 Zeilen, 80 Schwachstellen, 5 Phasen).

Stand: alle bisherigen Phasen (M, B-1, Q+38, 33.7, 33.8, K, L, N, O, P, Char-Müll-Fix, Char-Refs-Phase-2) sind gemerged in `main` (Stand 7382287).

## Phase 1 — Architektur-Fundament & Monolith-Auflösung

| Maßnahme | Status | Beleg / Lücke |
|---|---|---|
| **1.1** Monolith aufbrechen (dashboard.py zerschlagen, HTTP/Worker/UI-Routing trennen) | ⚠ **TEILWEISE** | Phase M hat `engine/{scenes,render,audio,prompts,presets}` extrahiert (4657→3285 Z.). Handler-Klasse `H` (~1075 Z.) lebt noch in `dashboard.py` — **komplette Extraktion in `routes/` ist offen** (siehe §1.2 von Phase M: "wenn dashboard.py < 1000 Z., Handler aus dashboard.py ziehen") |
| **1.2** ASGI/FastAPI statt ThreadingHTTPServer | ❌ **NICHT** | `ThreadingHTTPServer` aktiv (`dashboard.py:7`), alle `do_GET`/`do_POST` synchron. Migration zu FastAPI ist **großer Architektur-Refactor** (alle 60+ Routes + Handler-Klasse umschreiben) |
| **1.3** Connection-Pools, Exponential Backoff, Circuit Breaker für externe APIs | ⚠ **TEILWEISE** | `_kie_submit_image` hat 4-Attempt-Retry mit `last_err` Tracking (`dashboard.py:783-808`). Aber: **kein Connection-Pool** (jeder Call neue urllib-Verbindung), kein Circuit Breaker (4 Fehlversuche = Endlosschleife möglich), keine Exponential-Backoff-Staffelung |

## Phase 2 — Datenintegrität & State-Management

| Maßnahme | Status | Beleg / Lücke |
|---|---|---|
| **2.1** Atomare Schreibvorgänge, plan.json via Temp + os.replace, SIGTERM-Shutdown | ❌ **NICHT** | `plan.json` wird direkt mit `json.dump(open(p, "w"))` geschrieben — bei Crash mitten im Schreiben ist die Datei korrupt. Auch `BATCH_JOBS_LOCK` schützt nur vor Concurrency, nicht vor Crash. **Quick-Win-Kandidat für nächsten PR.** |
| **2.2** Pydantic-Modelle für State-Schema & Versionierung | ❌ **NICHT** | `plan.json` ist ein freies Dict ohne Validierung. Schema-Drifts werden erst zur Laufzeit entdeckt. |
| **2.3** StorageProvider-Interface, Paginierung für videos.json | ❌ **NICHT** | Direkte `json.load(open(...))`-Calls überall. Für Production mit vielen Videos muss Paginierung her. |

## Phase 3 — Sicherheit, Deployment & System-Isolation

| Maßnahme | Status | Beleg / Lücke |
|---|---|---|
| **3.1** Authentifizierung & Secrets | ❌ **NICHT** | API-Keys in `~/.kie_key`, `~/.elevenlabs_key`. Keine Login-Seite, keine JWT, kein HTTPS-Setup. **Kritisch vor Production.** |
| **3.2** CSRF, Upload-Checks, Path-Traversal-Schutz, kein shell=True | ⚠ **TEILWEISE** | B-1-Fix hat `os.makedirs(exist_ok=True)` und `validate=False` für Base64. Path-Traversal hardcoded in `ch_sheets(cid)`, `v_plan(cid, vid)` etc. Aber: **kein CSRF-Token-System**, GET-Routes mit Side-Effects (z.B. `/api/generate_all_start`) mutieren State über POST aber sind teils auch als GET definiert |
| **3.3** Docker-Härtung (Multi-Stage, non-root, 0.0.0.0) | ❌ **NICHT** | Kein `Dockerfile` im Repo. |
| **3.4** Health-Endpoint, JSON-Logging, Log-Rotation | ❌ **NICHT** | Kein `/health`-Route. Logs sind `print()`-Statements. **Quick-Win-Kandidat.** |

## Phase 4 — KI-Pipeline, Medien-Generierung & Ressourcen

| Maßnahme | Status | Beleg / Lücke |
|---|---|---|
| **4.1** OOM-Protection, cgroups-Limits, Temp-Files sauber löschen | ⚠ **TEILWEISE** | `IMAGE_GEN_SEMAPHORE` als Concurrency-Cap (gut). Aber: keine Speicherlimits, keine Zombie-Prozess-Reaping. `render_tmp/`-Cleanup läuft in `_render_worker` aber kein Crash-Recovery. |
| **4.2** FFMPEG asynchron, Hardware-Beschleunigung, FPS-Sync | ⚠ **TEILWEISE** | `subprocess.run(..., timeout=...)` blockt den Thread (gut isoliert in Worker-Thread). `h264_videotoolbox` automatisch erkannt in `_probe_video_encoder`. FPS-Sync ist via `_apply_sync_ininvariant` frame-genau. Aber: `subprocess.run` ist sync — kein echtes Async I/O. |
| **4.3** Fallback-Modelle, Prompt-Injection-Schutz, Seed-Locking | ⚠ **TEILWEISE** | **Prompt-Injection-Schutz für Charsheets: ✅ (Char-Müll-Fix, 5 Tests).** Aber: keine Model-Fallbacks (KIE 429 = Endlosschleife), kein Seed-Locking (Reproduzierbarkeit schwach), keine Token-Management-Validierung. |

## Phase 5 — Frontend, UI & Entwickler-Ergonomie

| Maßnahme | Status | Beleg / Lücke |
|---|---|---|
| **5.1** WebSockets/SSE statt Polling, Rate-Limiting | ❌ **NICHT** | Polling via `setInterval` im Frontend. Keine WebSocket-/SSE-Infrastruktur. |
| **5.2** Alpine-Komponenten aufbrechen, Tailwind kompilieren | ❌ **NICHT** | `dashboard.html` ist ein einziges 2787-Zeilen-Monolith-Dokument. Tailwind läuft wahrscheinlich über CDN (`tailwindcss.com`-Skript). |
| **5.3** Type Hints (Mypy), Magic Numbers, zentrales Exception-Handling, Linting (Ruff) | ⚠ **TEILWEISE** | Code hat teilweise Type Hints (in `engine/`-Modulen), aber nicht durchgehend. Magic Numbers im `dashboard.py` (z.B. `time.sleep(2)` hartkodiert, `RENDER_FPS = 30` als Modul-Konstante gut aber verstreut). Keine `pyproject.toml` für Ruff/Mypy-Konfiguration. |

## Empfohlene Reihenfolge (Quick-Wins zuerst)

1. **Phase 2.1 — Atomare plan.json Writes** (~30 Min, hoher Wert)
   - Helper `_atomic_write_json(path, data)`: tmp-Datei in gleicher Directory → fsync → `os.replace()`
   - Alle `json.dump(open(p, "w"))`-Aufrufe durch Helper ersetzen
   - SIGTERM-Handler registrieren für sauberen Shutdown

2. **Phase 3.4 — /health-Endpoint** (~15 Min)
   - Neue Route `if p == "/api/health":` → return `{"status": "ok", "uptime": ..., "active_jobs": ...}`
   - Strukturierte JSON-Logging-Klasse (einfache Variante ohne externe Lib)

3. **Phase 1.1 (Rest) — HTTP-Handler aus dashboard.py extrahieren** (~1-2 Tage)
   - Siehe Phase M.6: Handler `H` (~1075 Z.) ist der größte verbleibende Block
   - `routes/dashboard_routes.py` mit `register(app)`-Funktion
   - Risiko: hoher Refactor, muss in kleinere Schritte aufgeteilt werden

4. **Phase 1.3 — Connection-Pool für KIE.ai** (~1-2 Std)
   - `urllib3.PoolManager` mit `Retry`-Adapter
   - Circuit Breaker für KIE: nach N Fehlversuchen in 60s → temporär deaktivieren
   - Exponential Backoff zwischen Retries

5. **Phase 5.3 — Type Hints + Ruff** (~1-2 Std)
   - `pyproject.toml` mit `[tool.ruff]` und `[tool.mypy]`
   - Inkrementelle Adoption: `ruff check --fix` + Mypy strict für `engine/*` (bereits am typsichersten)

## Lücken / nicht abgedeckt

- Die genauen 80 Schwachstellen sind nicht in dem kurzen Dokument aufgelistet. Das Dokument verweist nur auf IDs (#1-80). Für eine vollständige Auditierung müsste das längere Quell-Dokument vorhanden sein (im PDF-Body oder als Anhang).
- Performance/Load-Tests fehlen komplett — das System wurde nie unter Last (>10 paralleler Render-Jobs) getestet.

## Phase-Verknüpfungen

Phase 1 ist die **Grundlage**. Phase 2-5 hängen davon ab, dass Phase 1 stabil läuft. Tatsächlich ist aber Phase 1.1 (Monolith) zu 70% erledigt (Phase M hat den Großteil extrahiert). Der **Handler-Block** in `dashboard.py` ist der größte verbleibende Brocken.

Pragmatische Empfehlung: bevor der nächste Big-Bang-Refactor (Phase 1.2 FastAPI, Phase 1.1 Rest) gemacht wird, sollten die Quick-Wins (Phase 2.1, 3.4, 1.3, 5.3) abgehakt werden, weil sie sofort Production-Wert liefern ohne große Architektur-Risiken.