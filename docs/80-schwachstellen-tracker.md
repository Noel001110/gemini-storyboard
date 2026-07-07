# 80-Schwachstellen-Tracker

Quelle: `docs/Architekturanalyse und Schwachstellenbericht — Storyboard Generator V2.md` (55 Zeilen, 5 Phasen)

**Status-Legende:**
- ✅ **Done** — vollständig implementiert + getestet
- ⚠ **Partial** — angefangen oder teilweise umgesetzt
- 🔄 **In Progress** — aktuell in Arbeit
- ❌ **Open** — noch nichts gemacht

**Phase-Legende:**
- 🔴 **P1** Architektur-Fundament (höchste Priorität)
- 🟠 **P2** Datenintegrität (kritisch)
- 🟡 **P3** Sicherheit & Deployment
- 🟢 **P4** KI-Pipeline & Ressourcen
- 🔵 **P5** Frontend & DX

---

## Phase 1 — Architektur-Fundament (5+3+4 = 12 IDs)

### 1.1 Monolith-Auflösung (dashboard.py zerschlagen)
| ID | Maßnahme | Status | Beleg / Lücke | Commit |
|:----|:----|:----|:----|:----|
| #5 | dashboard.py → engine-Module extrahieren | ✅ | Phase M: scenes/render/audio/prompts/presets, dashboard 4657→3285 Z. | `feat/refactor/m.5-prompts` ff. |
| #18 | HTTP-Server trennen | ❌ | Handler `H` (~1075 Z.) noch in dashboard.py | — |
| #32 | Worker-Orchestrierung trennen | ⚠ | Thread-basiert (Phase H) aber Worker leben in dashboard.py | — |
| #52 | UI-Routing trennen | ❌ | Routen sind `if p == "..."`-Ketten in `H.do_GET/do_POST` | — |
| #57 | Trennung Config/State | ⚠ | `channels.json` ist Config, `plan.json` ist State — gemischt im Code | — |

### 1.2 ASGI/FastAPI statt ThreadingHTTPServer
| ID | Maßnahme | Status | Beleg / Lücke |
|:----|:----|:----|:----|
| #1 | ThreadingHTTPServer → FastAPI | ❌ | dashboard.py:7 `from http.server import ThreadingHTTPServer` |
| #3 | async API-Calls | ❌ | Alle `urllib.request` synchron |
| #59 | aiohttp statt urllib | ❌ | Keine async I/O im Code |

### 1.3 Connection-Management
| ID | Maßnahme | Status | Beleg / Lücke |
|:----|:----|:----|:----|
| #2 | Connection-Pool | ❌ | Jeder KIE-Call neue urllib-Verbindung |
| #14 | Exponential Backoff | ✅ | `e88efed` _kie_retry_with_backoff() mit exp Backoff | | Retry ist linear (4× direkt) in `_kie_submit_image` |
| #15 | Circuit Breaker | ✅ | `e88efed` Circuit Breaker + half-open recovery | | Kein — 4× Retry dann Error, kein globaler Schutz |
| #39 | Timeout-Management | ⚠ | Partial: per-Call `timeout=30` in urllib, kein zentrales |

---

## Phase 2 — Datenintegrität (5+3+2 = 10 IDs)

### 2.1 Atomare Schreibvorgänge
| ID | Maßnahme | Status | Beleg / Lücke |
|:----|:----|:----|:----|
| #6 | Temp-Datei + os.replace für plan.json | ✅ | `90cf2db` _atomic_write_json() + 15 Aufrufstellen | | Direkter `json.dump(open(p,"w"))` überall — bei Crash korrupt |
| #7 | fsync vor rename | ✅ | `90cf2db` os.fsync() in _atomic_write_json | | Nicht implementiert |
| #36 | Channels.json atomar schreiben | ✅ | `90cf2db` _atomic_write_json für channels.json | | Selbes Pattern |
| #60 | Videos.json atomar | ✅ | `90cf2db` _atomic_write_json für videos.json | | — |
| #68 | SIGTERM-Handler für Graceful Shutdown | ✅ | `90cf2db` signal.signal(SIGTERM, _graceful_shutdown) | | Kein Handler registriert |

### 2.2 State-Schema & Versionierung
| ID | Maßnahme | Status | Beleg / Lücke |
|:----|:----|:----|:----|
| #21 | Pydantic-Modelle für plan.json | ❌ | Direkte Dict-Zugriffe überall |
| #22 | Schema-Versionierung in plan.json | ❌ | Kein version-Feld |
| #23 | Validation beim Laden | ❌ | Kein Validierungs-Layer |

### 2.3 Storage-Provider-Abstraktion
| ID | Maßnahme | Status | Beleg / Lücke |
|:----|:----|:----|:----|
| #24 | StorageProvider-Interface | ❌ | Direkte `open()`-Calls |
| #10 | Paginierung für videos.json | ❌ | Alle Videos werden geladen |

---

## Phase 3 — Sicherheit, Deployment (3+5+5+4 = 17 IDs)

### 3.1 Authentifizierung & Secrets
| ID | Maßnahme | Status | Beleg / Lücke |
|:----|:----|:----|:----|
| #41 | API-Keys in .env statt ~/.keys | ❌ | `~/.kie_key`, `~/.elevenlabs_key` |
| #43 | Passwortschutz / JWT | ❌ | Dashboard ist offen |
| #44 | HTTPS / Reverse Proxy | ❌ | Kein TLS-Setup |

### 3.2 Input-Validierung & Security
| ID | Maßnahme | Status | Beleg / Lücke |
|:----|:----|:----|:----|
| #45 | CSRF-Schutz | ❌ | Keine Token-Validierung |
| #46 | strikte Upload-Checks | ⚠ | B-1-Fix: validate=False, aber keine MIME-Sniffing-Validierung |
| #49 | Path-Traversal-Verhinderung | ✅ | Hardcoded paths (`ch_sheets(cid)`, `v_plan(cid, vid)`) |
| #50 | Kein shell=True | ✅ | `subprocess.run([...args])` mit Listen — keine Shell |
| #54 | URL-Validierung für ext. APIs | ⚠ | `char_ref_url.txt` wird unkontrolliert an KIE geschickt |

### 3.3 Docker-Härtung
| ID | Maßnahme | Status | Beleg / Lücke |
|:----|:----|:----|:----|
| #20 | Multi-Stage Dockerfile | ❌ | Kein Dockerfile im Repo |
| #62 | Non-Root User | ❌ | — |
| #63 | Volume-Permissions | ❌ | — |
| #66 | Host-Binding 0.0.0.0 | ❌ | Aktuell nur localhost |
| #70 | Docker-Compose / Healthcheck | ❌ | — |

### 3.4 Monitoring & Health
| ID | Maßnahme | Status | Beleg / Lücke |
|:----|:----|:----|:----|
| #38 | /health-Endpoint | ✅ | `3e28bc4` GET /health + Uptime + Git-Commit |
| #40 | strukturiertes JSON-Logging | ✅ | `10dbd60` _log() mit JSON/Text-Modus (LOG_JSON env) |
| #65 | Docker Log-Rotation | ❌ | — |
| #67 | Metrics-Endpoint | ❌ | — |

---

## Phase 4 — KI-Pipeline & Ressourcen (4+6+8 = 18 IDs)

### 4.1 Ressourcen-Limits
| ID | Maßnahme | Status | Beleg / Lücke |
|:----|:----|:----|:----|
| #4 | OOM-Protection | ❌ | Keine Memory-Limits |
| #61 | cgroups-Limits | ❌ | — |
| #64 | Zombie-Prozess-Reaping | ❌ | Threads können hängen bleiben |
| #69 | Temp-Files sauber löschen | ⚠ | `render_tmp/` wird im Worker aufgeräumt, kein Crash-Recovery |

### 4.2 Video-Pipeline optimieren
| ID | Maßnahme | Status | Beleg / Lücke |
|:----|:----|:----|:----|
| #29 | FFMPEG asynchron | ❌ | `subprocess.run` ist sync, aber im Worker-Thread entkoppelt |
| #30 | Hardware-Beschleunigung | ✅ | `_probe_video_encoder` wählt `h264_videotoolbox` automatisch |
| #31 | Farbkonvertierung fixen | ⚠ | `yuv420p` für Output, aber Quell-Material unterschiedlich |
| #73 | FPS-Sync prüfen | ✅ | `_apply_sync_invariant` frame-genau |
| #74 | Audio-Sync prüfen | ⚠ | Keine separate Validierung |
| #78 | Crossfade-Timing | ⚠ | Funktional, aber keine automatische Validierung |

### 4.3 KI-Orchestrierung
| ID | Maßnahme | Status | Beleg / Lücke |
|:----|:----|:----|:----|
| #25 | Fallback-Modelle | ❌ | KIE 429 = Retry-Loop, kein globales Fallback |
| #26 | Prompt-Injection-Schutz (Charsheets) | ✅ | Char-Müll-Fix: `_is_valid_char_description` (5 Tests) |
| #27 | Token-Management | ❌ | Kein Token-Budget, keine Validierung |
| #28 | Dynamische Charakter-Referenzen | ⚠ | Phase 2 implementiert (data-URLs), aber keine Runtime-Auswahl pro Szene |
| #71 | Seed-Locking | ❌ | Keine Reproduzierbarkeits-Garantien |
| #75 | Kosten-Tracking pro Render | ❌ | Kein Logging der KIE-Kosten |
| #77 | Rate-Limit-Handling global | ⚠ | `_kie_rate_limit_wait` aber kein globaler Counter |
| #80 | Modell-Versionierung | ❌ | `nano-banana-2` hardcoded |

---

## Phase 5 — Frontend & DX (4+3+5 = 12 IDs)

### 5.1 WebSockets/SSE statt Polling
| ID | Maßnahme | Status | Beleg / Lücke |
|:----|:----|:----|:----|
| #8 | WebSockets/SSE | ❌ | `setInterval`-Polling |
| #11 | Server-Sent Events | ❌ | — |
| #13 | Rate-Limiting Frontend | ❌ | — |
| #35 | Job-Status-Streaming | ⚠ | Partial: Polling alle 2s |

### 5.2 Frontend-Architektur
| ID | Maßnahme | Status | Beleg / Lücke |
|:----|:----|:----|:----|
| #33 | Alpine-Komponenten aufbrechen | ❌ | dashboard.html ist 2787-Zeilen-Monolith |
| #34 | Tailwind lokal kompilieren (Purge) | ❌ | Tailwind wahrscheinlich über CDN |
| #12 | Cache-Busting für Bilder | ❌ | Keine Cache-Headers |

### 5.3 Code-Qualität
| ID | Maßnahme | Status | Beleg / Lücke |
|:----|:----|:----|:----|
| #51 | Type Hints (Mypy) | ⚠ | In `engine/*` teilweise, dashboard.py verstreut |
| #53 | Magic Numbers entfernen | ⚠ | Konstanten existieren (RENDER_FPS, etc.), aber hartkodierte `time.sleep(2)` etc. |
| #55 | Zentrales Exception-Handling | ❌ | Pro-Funktion `try/except`, kein globaler Handler |
| #56 | Ruff Linting | ❌ | Keine `pyproject.toml` |
| #58 | Linting in CI | ❌ | — |

---

## IDs ohne dokumentierten Eintrag

Aus den 80 IDs sind in der MD-Tabelle **69 dokumentiert**. Die folgenden 11 IDs sind in keiner Phase-Tabelle aufgeführt und brauchen Klärung:

| ID | Status |
|:----|:----|
| #9  | ❓ nicht dokumentiert |
| #16 | ❓ nicht dokumentiert |
| #17 | ❓ nicht dokumentiert |
| #19 | ❓ nicht dokumentiert |
| #37 | ❓ nicht dokumentiert |
| #42 | ❓ nicht dokumentiert |
| #47 | ❓ nicht dokumentiert |
| #48 | ❓ nicht dokumentiert |
| #72 | ❓ nicht dokumentiert |
| #76 | ❓ nicht dokumentiert |
| #79 | ❓ nicht dokumentiert |

→ Wenn das Quell-Audit-Dokument vollständig vorliegt, müssen diese 11 IDs nachgetragen werden.

---

## Summary

| Phase | Done | Partial | In Progress | Open | Unbekannt | Total |
|:----|:----:|:----:|:----:|:----:|:----:|:----:|
| 1 (Architektur) | 3 | 2 | 0 | 7 | — | 12 |
| 2 (Daten) | 5 | 0 | 0 | 5 | — | 10 |
| 3 (Sicherheit) | 4 | 2 | 0 | 11 | — | 17 |
| 4 (KI-Pipeline) | 2 | 2 | 0 | 14 | — | 18 |
| 5 (Frontend/DX) | 0 | 1 | 0 | 11 | — | 12 |
| Unbekannt | — | — | — | — | 11 | 11 |
| **Total** | **14** | **7** | **0** | **48** | **11** | **80** |

→ **80 Schwachstellen, davon 5 done, 7 partial, 1 in progress, 56 open, 11 unknown.**

## Quick-Win-Reihenfolge (Produktion zuerst)

2. ~~**#38** /health-Endpoint~~ ✅
2. ~~**#14**~~ ✅
3. ~~**#40**~~ ✅
4. **#56** Ruff Linting
5. **#69** Temp-Files sauber löschen (Crash-Recovery)
3. ~~**#40**~~ ✅