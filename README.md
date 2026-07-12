# Storyboard Generator

Lokales Tool, das aus einem Sprecher-Skript ein fertiges Erklärvideo baut:
**Skript → Szenen mit Timing → Bild-Prompts → generierte Bilder (KIE.ai) →
Ken-Burns-Clips mit Übergängen, Overlays und Voiceover → fertiges `final.mp4`**

Sound (Musik/SFX) kommt bewusst nicht aus dem System — das machst du danach
in Logic/DaVinci auf dem fertigen `final.mp4`. ARCHITECTURE.md §"Audio-Realität"
erklärt warum.

Details zu Architektur, Datenmodell und Phasen stehen in
[ARCHITECTURE.md](ARCHITECTURE.md). Pipeline-spezifische Notizen in
[docs/PROMPT_PIPELINE.md](docs/PROMPT_PIPELINE.md). **Praktische Anleitung
mit Troubleshooting** in [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Tech-Stack

- **Backend**: `dashboard.py` (Python-Stdlib `http.server`) + `engine/`-Module
- **Frontend**: `dashboard.html` (Vanilla JS, Alpine + Tailwind via CDN, kein Build)
- **Rendering**: ffmpeg (`zoompan` für Ken-Burns, `xfade` für Übergänge, `concat` für MP3-Stitching)
- **KI**: [KIE.ai](https://kie.ai) für Bilder/LLM, [ElevenLabs](https://elevenlabs.io) für Voiceover
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
`/api/produce_status`). Bei Problemen siehe RUNBOOK §6.

## Was du im UI steuerst

Pro **Kanal** (`channels/<cid>/`) beliebig viele **Videos** (`channels/<cid>/videos/<vid>/`).
Die Pipeline pro Video:

1. **Skript** — Text eintippen oder aus Titel/Thema generieren. Wird debounced
   in `script.json` persistiert (überlebt Browser-/Geräte-Wechsel).
2. **Voice-Setup** — Voice + 5 Slider wählen (Stability, Similarity, Style,
   **Speed** 0.7-1.2, Speaker-Boost). "Voice testen" für 2-Sek-Stimmprobe.
3. **Voiceover generieren** — bei Texten > 4800 Zeichen Auto-Chunking
   (Satzgrenzen + ffmpeg-concat). Ergebnis: 7-Min-MP3 + Word-Timestamps
   + Inline-Audio-Player im UI.
4. **Plan** — LLM zerlegt das Skript in Szenen mit getimetem Voiceover.
   Race-safe: bestehende gerenderte Bilder werden per Text-Match preserviert.
5. **Char-Sheets** — manuell hochgeladen oder pipeline-generiert. Bei Duplikaten
   die manuelle Variante behalten (Pipeline-Version in `_pipeline_duplicates/`).
6. **Bilder** — pro Szene KIE.ai-Generierung (über `engine/imagegen.py`, ein
   Provider-Interface — `generate_image(provider="kie")`), mit Style-Ankern
   (Settings-Tab, bis zu 3 Referenzbilder) + Character-Referenzen (3-Stufen-
   Fallback: Anchor-Szene → Datei-Match → Name-Match). Style- und Charakter-
   Referenzen werden **beide** angehängt, nie exklusiv gegeneinander getauscht.
7. **Render** — ffmpeg assembliert `final.mp4` (Voice + Bilder, ohne Sound).
   Ken-Burns-Bewegung: pro Szene reiner Zoom ODER reiner Pan/Tilt (nie kombiniert),
   Geschwindigkeit skaliert mit der echten Szenendauer, Anti-Monotonie-Regel
   verhindert Wiederholung der Vorszenen-Bewegung. Captions: CapCut-Style
   1-Wort-Einblendungen statt ganzer Sätze.

Optional pro Video: Tonauswahl (ElevenLabs / minimax; Default-Modell
`eleven_multilingual_v2` — schnellere, verlässlichere Kadenz als v3), Bild-Modell
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
  scenes.py              Text→Szenen-Segmentierung + Entity-Resolve mit Fallback
  render.py              Szenen→Video (ffmpeg-Orchestrierung, Ken-Burns-Motion-Lib)
  audio.py               Voice→Sync (Pause-Trim, Timing-Invariant); Musik/SFX-Mix
                         bleibt dormant im Code, wird nicht mehr aufgerufen
  prompts.py             Prompt-Bau (Style-Anker, Phase-Cues, Charsheets)
  presets.py             5 Stil-Presets (flat_cartoon_doc, editorial_minimal, …)
  imagegen.py            Bild-Provider-Interface (generate_image, KIE-Submit/
                         Poll/Upload) — ein zweiter Provider braucht nur einen
                         neuen Registry-Eintrag, keine Änderung an Aufrufern
engine_elevenlabs.py     ElevenLabs-TTS-Integration + Auto-Chunking + Voice-Settings
render_overlay.py        Overlay-Renderer (Captions/Callouts/Counters)
whisper_transcribe.py    Whisper-Worker (im .venv_whisper, nur Subprozess)
start.sh                 Convenience-Starter (kill+restart+open-browser)
tests/
  test_cinematic_e2e.py  E2E-Tests für die Render-Pipeline
  test_pipeline_fixes.py Regressionstests für Sync/Char-Refs/Motion/Bild-Provider
channels/                Laufzeitdaten (gitignored)
docs/
  PROMPT_PIPELINE.md     Pipeline-Details: Prompt-Bau, Char-Refs, Retry-Logik
  RUNBOOK.md             Schritt-für-Schritt-Anleitung + Troubleshooting-Guide
```

## Tests

```bash
python3 tests/test_pipeline_fixes.py   # Hand-rolled Runner, kein pytest-Discovery
python3 tests/test_cinematic_e2e.py    # dito — Funktionsnamen bewusst t_* statt test_*
```

## Bekannte Grenzen

- **`dashboard.py` ist ~4260 Z.** — der HTTP-Handler sollte langfristig in
  `routes/` extrahiert werden. Riskanter Refactor, deshalb bisher nicht
  angefasst (siehe ARCHITECTURE.md §"Offene Wunden").
- **Kein Production-Deployment** — kein Dockerfile, keine HTTPS, kein Auth.
  Tool läuft lokal auf `localhost`.

## Wenn du ein konkretes Feature suchst

- Wie das System intern funktioniert → `ARCHITECTURE.md`
- Praktische Anleitung + Troubleshooting → `docs/RUNBOOK.md`
- Pipeline-Details (Prompt-Bau, Char-Refs, Retry-Logik) → `docs/PROMPT_PIPELINE.md`