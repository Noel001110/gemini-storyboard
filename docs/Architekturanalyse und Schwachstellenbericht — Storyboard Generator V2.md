# **Architekturanalyse und Schwachstellenbericht — Storyboard Generator V2**

Dieses Dokument konsolidiert die vollständige Architekturanalyse (80 identifizierte Schwachstellen) und ordnet sie in einen strikten, priorisierten Ausführungsplan ein. Wie besprochen, bildet die Auflösung des Monolithen und der Umstieg auf eine asynchrone Architektur die absolute Grundlage für alle weiteren Schritte.

## **Phase 1: Architektur-Fundament & Monolith-Auflösung (Höchste Priorität)**

Ohne diesen Schritt sind alle weiteren Optimierungen hinfällig. Der ThreadingHTTPServer muss ersetzt und die Hintergrundjobs asynchron ausgelagert werden.

| Priorität | ID | Maßnahme / Schwachstelle   |
| :---- | :---- | :---- |
| 1.1 | \#5, \#18, \#32, \#52, \#57 | **Auflösung des Monolithen:** dashboard.py zerschlagen. Trennung von HTTP-Server, Worker-Orchestrierung und UI-Routing. |
| 1.2 | \#1, \#3, \#59 | **Asynchrones I/O & FastAPI:** Ersetzen des ThreadingHTTPServer durch ASGI/FastAPI. Umbau der API-Calls auf asynchrone Requests (aiohttp). |
| 1.3 | \#2, \#14, \#15, \#39 | **Verbindungs-Management:** Implementierung von Connection-Pools, Exponential Backoff und Circuit Breakern für externe APIs (KIE.ai, ElevenLabs). |

## **Phase 2: Datenintegrität & State-Management (Kritisch)**

Nachdem der Server stabil läuft, muss sichergestellt werden, dass keine Daten durch Abstürze verloren gehen.

| Priorität | ID | Maßnahme / Schwachstelle   |
| :---- | :---- | :---- |
| 2.1 | \#6, \#7, \#36, \#60, \#68 | **Atomare Schreibvorgänge:** Absicherung der plan.json. Nutzung von Temp-Dateien und os.replace. Sicherer Graceful Shutdown mit SIGTERM-Handling. |
| 2.2 | \#21, \#22, \#23 | **State-Schema & Versionierung:** Trennung von Config und State. Einführung von Pydantic-Modellen zur Validierung und Versionsverwaltung. |
| 2.3 | \#24, \#10 | **Abstraktion der Datenhaltung:** StorageProvider-Interface einführen, um zukünftig von der direkten Dateisystem-Bindung abzuweichen. Paginierung für videos.json. |

## **Phase 3: Sicherheit, Deployment & System-Isolation**

Die Anwendung wird für den produktiven Einsatz auf einem VPS oder in Docker gehärtet.

| Priorität | ID | Maßnahme / Schwachstelle   |
| :---- | :---- | :---- |
| 3.1 | \#41, \#43, \#44 | **Authentifizierung & Secrets:** API-Keys in .env auslagern. Passwortschutz / JWT für das Dashboard einführen. HTTPS/Reverse Proxy vorbereiten. |
| 3.2 | \#45, \#46, \#49, \#50, \#54 | **Input-Validierung & Security:** CSRF-Schutz, strikte Upload-Checks, Path-Traversal-Verhinderung und Beseitigung von shell=True in Subprozessen. |
| 3.3 | \#20, \#62, \#63, \#66, \#70 | **Docker-Härtung:** Multi-Stage Dockerfile erstellen, nicht als Root ausführen, Volume-Permissions korrigieren und Host-Binding auf 0.0.0.0 setzen. |
| 3.4 | \#38, \#40, \#65, \#67 | **Monitoring & Health:** /health-Endpoint hinzufügen, strukturiertes JSON-Logging und Docker-Log-Rotation aktivieren. |

## **Phase 4: KI-Pipeline, Medien-Generierung & Ressourcen**

Fehlerbehandlung und Effizienzsteigerung bei den rechen- und kostenintensiven KI-Prozessen.

| Priorität | ID | Maßnahme / Schwachstelle   |
| :---- | :---- | :---- |
| 4.1 | \#4, \#61, \#64, \#69 | **Ressourcen-Limits:** OOM-Protection, cgroups-Limits konfigurieren, Zombie-Prozesse per init-System vermeiden und Temp-Files sauber löschen. |
| 4.2 | \#29, \#30, \#31, \#73, \#74, \#78 | **Video-Pipeline optimieren:** FFMPEG asynchron und mit Hardware-Beschleunigung ausführen. Farbkonvertierung und FPS-Sync reparieren. |
| 4.3 | \#25, \#26, \#27, \#28, \#71, \#75, \#77, \#80 | **KI-Orchestrierung:** Fallback-Modelle definieren, Prompt-Injection-Schutz, Token-Management, dynamische Charakter-Referenzen und Seed-Locking implementieren. |

## **Phase 5: Frontend, UI & Entwickler-Ergonomie**

Zuletzt wird das Dashboard robust gemacht und der Code für zukünftige Wartbarkeit aufbereitet.

| Priorität | ID | Maßnahme / Schwachstelle   |
| :---- | :---- | :---- |
| 5.1 | \#8, \#11, \#13, \#35 | **Kommunikations-Paradigma:** Polling durch WebSockets (oder SSE) ersetzen. Rate-Limiting einführen, um Server-Load zu senken. |
| 5.2 | \#33, \#34, \#12 | **Frontend-Architektur:** Alpine-Monolith in Komponenten aufbrechen. Tailwind lokal kompilieren (Purge). Cache-Busting für Bilder implementieren. |
| 5.3 | \#51, \#53, \#55, \#56, \#58 | **Code-Qualität:** Type Hints (Mypy) hinzufügen, Magic Numbers entfernen, zentrales Exception-Handling und Linting (Ruff) integrieren. |

