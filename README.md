# Storyboard Generator

Lokales Tool, das aus einem Sprecher-Skript (Text oder Voiceover-Upload) automatisch ein fertiges Erklärvideo baut: Skript → Szenen mit Timing → Bild-Prompts → generierte Bilder über KIE.ai → Ken-Burns-Clips mit Übergängen, Sound-Design und Text-Overlays → fertiges `final.mp4`.

Details zu Architektur, Datenmodell und allen bisherigen Ausbaustufen stehen in [ARCHITECTURE.md](ARCHITECTURE.md).

## Tech-Stack

- **Backend**: `dashboard.py` — ein einzelnes File, nur Python-Stdlib (`http.server`), kein Flask/FastAPI.
- **Frontend**: `dashboard.html` — Vanilla JS, kein Build-Step, kein Framework.
- **Rendering**: reines ffmpeg (Ken-Burns via `zoompan`, Crossfades via `xfade`, kein MoviePy/Remotion).
- **Externe Dienste**: [KIE.ai](https://kie.ai) für Bild-/Text-/Videogenerierung, optional [ElevenLabs](https://elevenlabs.io) für Voiceover.
- **Datenhaltung**: keine Datenbank — alles als JSON-Dateien unter `channels/`.

## Voraussetzungen

- Python 3 (Stdlib reicht für den Hauptprozess)
- `ffmpeg` im PATH (inkl. `ffprobe`)
- Ein KIE.ai-API-Key in `~/.kie_key` (einzeilig, sonst brechen alle KI-Funktionen ab)
- Optional, für Wort-genaues Timing/Untertitel/Pausen-Kürzung: isoliertes venv `.venv_whisper` mit `faster-whisper` (separat von der Stdlib-only-Regel des Hauptprozesses — läuft nur als Subprozess für `whisper_transcribe.py`)
- Optional, für automatisch generiertes Voiceover: ein ElevenLabs-API-Key in `~/.elevenlabs_key`, plus `channels/<cid>/voice_id.txt` und `voice_settings.json` pro Kanal

## Start

```bash
./start.sh [port]        # Standard: 8000, öffnet automatisch den Browser
```

oder direkt:

```bash
python3 dashboard.py --port 8765
```

Frontend-Änderungen (`dashboard.html`) wirken sofort ohne Neustart — die Datei wird bei jedem Request frisch von der Platte gelesen. Backend-Änderungen (`dashboard.py`) brauchen einen Neustart; vorher über die `*_status`-Endpunkte (`/api/generate_all_status`, `/api/plan_status`, `/api/render_status`, `/api/produce_status`) prüfen, dass kein Job für das aktive Video läuft.

## Ablage

Alle Kanäle/Videos/generierten Assets liegen unter `channels/` (gitignored — reine Laufzeitdaten, kein Quellcode). Struktur und Feldbedeutungen sind in [ARCHITECTURE.md](ARCHITECTURE.md) Abschnitt 3 dokumentiert.
