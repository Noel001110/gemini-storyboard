# Storyboard Generator

Lokales Tool, das aus einem Sprecher-Skript ein fertiges Erklärvideo baut:
**Skript → Szenen mit Timing → Bild-Prompts → generierte Bilder (KIE.ai) →
Ken-Burns-Clips mit Übergängen, Overlays und Voiceover → fertiges `final.mp4`**

Sound (Musik/SFX) kommt bewusst nicht aus dem System — das machst du danach
in Logic/DaVinci auf dem fertigen `final.mp4`. ARCHITECTURE.md §"Audio-Realität"
erklärt warum.

Details zu Architektur, Datenmodell und Phasen stehen in
[ARCHITECTURE.md](ARCHITECTURE.md). Was dort steht, wird hier nicht dupliziert.

## Tech-Stack

- **Backend**: `dashboard.py` (Python-Stdlib `http.server`) + `engine/`-Module
- **Frontend**: `dashboard.html` (Vanilla JS, Alpine + Tailwind via CDN, kein Build)
- **Rendering**: ffmpeg (`zoompan` für Ken-Burns, `xfade` für Übergänge)
- **KI**: [KIE.ai](https://kie.ai) für Bilder/LLM, optional [ElevenLabs](https://elevenlabs.io) für Voiceover
- **Datenhaltung**: keine DB — JSON-Dateien unter `channels/` (gitignored)

## Voraussetzungen

- Python 3.10+ (Stdlib reicht für den Hauptprozess)
- `ffmpeg` + `ffprobe` im PATH
- `~/.kie_key` (einzeilig) — sonst brechen alle KI-Funktionen ab
- Optional für Voiceover: `~/.elevenlabs_key` + `channels/<cid>/voice_id.txt`
- Optional für Wort-Timing der Voiceover-WAV/MP3-Files: `.venv_whisper` mit `faster-whisper`

## Start

```bash
./start.sh [port]                # Standard: 8000, öffnet automatisch den Browser
python3 dashboard.py --port 8765 # direkter Start ohne Browser-Auto-Open
```

Frontend (`dashboard.html`) wirkt sofort — wird pro Request frisch geladen.
Backend (`dashboard.py`) braucht Neustart. Vorher prüfen dass kein Job läuft
(`/api/generate_all_status`, `/api/plan_status`, `/api/render_status`,
`/api/produce_status`).

## Was du im UI steuerst

Pro **Kanal** (`channels/<cid>/`) beliebig viele **Videos** (`channels/<cid>/videos/<vid>/`).
Die Pipeline pro Video:

1. **Skript** — Text eintippen oder aus Titel/Thema generieren. Wird debounced
   in `script.json` persistiert (überlebt Browser-/Geräte-Wechsel).
2. **Plan** — LLM zerlegt das Skript in Szenen mit Whisper-getimetem Voiceover.
3. **Bilder** — pro Szene KIE.ai-Generierung, mit Style-Ankern + Char-Referenzen
   für visuelle Konsistenz.
4. **Render** — ffmpeg assembliert `final.mp4` (Voice + Bilder, ohne Sound).

Optional pro Video: Tonauswahl (ElevenLabs / minimax), Bild-Modell
(`nano-banana-2` / `-lite`), Video-Modus (statische Bilder vs. Veo-Videos),
Overlays (Captions/Callouts/Chapters), Thumbnail-Generierung.

Eine schrittweise Anleitung pro Tab steht im Frontend selbst
(Hinweistexte neben jedem Steuerelement).

## Was du NICHT im UI steuerst

- Musik-Auswahl / Sound-Effekte — bewusst out-of-scope, machst du in Logic
- Audio-Mix / SFX-Trigger an Szenen-Grenzen
- Auto-Fix-Schleifen für "schlechte" Bilder — du klickst "Neu generieren"

## Datei-Layout (was wofür)

```
dashboard.py             HTTP-Handler + Routing + persistente Helper
dashboard.html           Frontend (komplette UI in einer Datei)
engine/
  scenes.py              Text→Szenen-Segmentierung
  render.py              Szenen→Video (ffmpeg-Orchestrierung)
  audio.py               Voice→Sync (Pause-Trim, Timing-Invariant)
  prompts.py             Prompt-Bau (Style-Anker, Phase-Cues, Charsheets)
  presets.py             5 Stil-Presets (flat_cartoon_doc, editorial_minimal, …)
engine_elevenlabs.py     ElevenLabs-TTS-Integration + Voice-Settings
render_overlay.py        Overlay-Renderer (Captions/Callouts/Counters)
whisper_transcribe.py    Whisper-Worker (im .venv_whisper, nur Subprozess)
start.sh                 Convenience-Starter (kill+restart+open-browser)
tests/
  test_cinematic_e2e.py  E2E-Tests für die Render-Pipeline
channels/                Laufzeitdaten (gitignored)
docs/                    Pläne, Schwachstellen-Tracker, Style-Guide
```

## Tests

```bash
.venv_whisper/bin/python -m pytest tests/ -v   # wenn venv vorhanden
python3 -m pytest tests/ -v                    # sonst mit System-Python
```

## Bekannte Grenzen

- **Visual-Continuity ~70% zuverlässig** — KIE variiert trotz Char-Refs.
  Manuell nachkorrigieren wenn nötig.
- **`dashboard.py` ist ~4000 Z.** — der HTTP-Handler sollte langfristig in
  `routes/` extrahiert werden. Riskanter Refactor, deshalb bisher nicht
  angefasst (siehe ARCHITECTURE.md §"Offene Wunden").
- **Kein Production-Deployment** — kein Dockerfile, keine HTTPS, kein Auth.
  Tool läuft lokal auf `localhost`.

## Wenn du ein konkretes Feature suchst

- Was noch offen ist → `docs/80-schwachstellen-tracker.md`
- Großer Detail-Plan mit Hebel/Aufwand → `CINEMATIC_UPGRADE_PLAN.md`
- Wie das System intern funktioniert → `ARCHITECTURE.md`