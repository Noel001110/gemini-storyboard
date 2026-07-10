# ARCHITECTURE — Storyboard Generator (Stand 2026-07-08)

## Was das System tut (in einem Satz)

**Du gibst ein Script ein → das System generiert für jede Szene ein Bild mit cinematischen Effekten → die Bilder werden mit Ken-Burns-Effekten zu einem Video zusammengeschnitten, bei dem der Schnitt EXAKT auf dem Sprach-Timing (ElevenLabs TTS) liegt → du machst Sound im Nachgang in Logic/DaVinci.**



## Audio-Realität (genau das was du willst)

```
DAS SYSTEM GENERIERT NUR:
  └─ Voice (ElevenLabs TTS) — die einzige Audio-Spur im final.mp4
  └─ Word-Level-Timing (Whisper) — bestimmt wann Szenen wechseln
  └─ Scene-Cut = Wort-Grenze — Szenen enden wo Sätze enden

DAS SYSTEM GENERIERT NICHT:
  └─ Keine Musik
  └─ Keine SFX
  └─ Keine Hintergrund-Audio
  └─ Keine Sound-Effekte an Transitions

NACHGANG IN LOGIC/DAVINCI:
  └─ Du nimmst final.mp4 (enthält nur Voice + Bilder)
  └─ Du legst Musik drunter
  └─ Du fügst SFX hinzu wo du willst
  └─ Du machst Mix
```

## Pipeline (was das System macht)

```
1. SCRIPT-EINGABE
   └─ Du tippst ein Skript in den Kanal-Editor (DE/EN, mit Stil-Preset aus 5 Templates)

2. ANALYZE (LLM)
   └─ Gemini (via KIE.ai) zerlegt Skript in:
      - Szenen mit start_aligned/end_aligned (Whisper-getimed)
      - Phase (OPENING/RISING_ACTION/CLIMAX/RESOLUTION)
      - Hook (Cold-Open) + Throughline-Question
      - Pacing (calm/normal/punchy)
      - Visual Sequences (für Style-Konsistenz über Szenen)
      - Charsheets (Char-Referenz-Bilder)

3. VOICE (TTS) — die einzige Audio-Quelle
   └─ ElevenLabs TTS → MP3 mit Narration
   └─ Whisper → Wort-Level-Timing
   └─ Pause-Trim (Szenen-Grenzen landen auf Satzenden)

4. PROMPT-BAU pro Szene
   └─ Master-Preset + Phase-Cue + Hook-Cue + Charsheet-Beschreibung + Charsheet-PNG
   └─ Müll-Filter (verhindert "Stick-Figure"-Test-Müll in Prompts)
   └─ Charsheet-PNGs als data:image/png;base64 (Style-Anker für KIE)

5. BILDERZEUGUNG
   └─ Pro Szene: POST an KIE.ai (16:9, 2K, nano-banana-2)
   └─ Referenzen: Chain-Anker + Chain-Vorgänger + Charsheet-PNG
   └─ "CONTINUITY (STRICT)" Prompt zwingt zur visuellen Konsistenz
   └─ Rate-Limit + Circuit Breaker (Hardening aus Schwachstellenbericht)

6. RENDERING (ffmpeg)
   └─ Pro Szene: zoompan + Sequenz-Wiederholung (Ken Burns)
   └─ Color-Grading pro Phase (colorbalance + CLIMAX-Vignette)
   └─ Overlays: caption, callout, counter (animiert)
   └─ Audio: Voice-Spur wird gemuxt (KEINE Musik, KEINE SFX)
   └─ Frame-genau: _apply_sync_invariant (Whisper-Timing)

7. SOUND: MANUELL (NACHGANG IN LOGIC)
   └─ Du nimmst final.mp4 in Logic/DaVinci
   └─ Du legst Musik drunter
   └─ Du fügst SFX hinzu
   └─ Du machst Mix
```

## Komponenten

```
┌─────────────────────────────────────────────────────┐
│                  DASHBOARD (1 Prozess)               │
│                                                      │
│  dashboard.py  ────  HTTP-Server (ThreadingHTTPS)  │
│       │                                                │
│       ├── engine/                                    │
│       │   ├── scenes.py    ── Text→Szenen            │
│       │   ├── render.py    ── Szenen→Video (ffmpeg)  │
│       │   ├── audio.py     ── Voice→Sync              │
│       │   ├── prompts.py   ── Prompt-Bau              │
│       │   └── presets.py   ── 5 Stil-Presets         │
│       │                                                │
│       └── engine_elevenlabs.py  ── TTS-Integration    │
│                                                      │
│  dashboard.html  ───  Frontend (Alpine + Tailwind)    │
│                                                      │
└─────────────────────────────────────────────────────┘
        │
        ├── KIE.ai (REST)  ── Bilder + LLM + Whisper
        └── ElevenLabs (REST)  ── TTS
```

## Daten-Persistenz

```
channels/
  <cid>/
    channels.json        ── Kanal-Liste
    master_prompt.txt    ── Bild-Master (dein Stil)
    charsheets/          ── Char-Referenz-PNGs + JSON-Descs
    videos/
      <vid>/
        plan.json        ── Strukturierte Szenen
        generated/       ── Finale Bilder
        render_tmp/      ── Working-Dir
        final.mp4        ── Output (nur Voice + Bilder, KEIN Sound)
```

## Was du im Frontend steuerst

1. **Skript-Preset** (Kanal-Anlage): flat_cartoon_doc (default), editorial_minimal, ink_documentary, charcoal_noir, stick_minimal
2. **Skript** (Skript-Tab): Text eingeben, Sprache wählen
3. **Char-Referenzen** (Stil-Tab): Charsheet-PNGs hochladen
4. **Master-Prompt** (Stil-Tab): Stil-Beschreibung editieren
5. **Plan-Generierung**: Button → LLM generiert Szenen
6. **Bilderzeugung pro Szene**: Button "Generieren" → KIE rendert
7. **Video-Render**: Button → ffmpeg assembliert (final.mp4 mit Voice + Bildern)

## Was du NICHT im Frontend steuerst

- Musik-Auswahl (kein Dropdown)
- Sound-Effekt-Library (keine Trigger)
- Hintergrund-Audio (keine Auswahl)
- Finale Audio-Synchronisation (Voice ist alles, was das System macht)

## Schwachstellen-Status

- **Tracker:** `docs/80-schwachstellen-tracker.md`
- **Stand 2026-07-08:** 17/80 abgehakt (21%), 7 partial, 56 open
- **Out-of-scope (vom User explizit ausgeschlossen):**
  - Komplexe Audio-Stem-Pipeline (Phase K MUSIC_BEDS, Segment-Kette)
  - Sound-Effekt-Library
  - Auto-SFX-Trigger
- **Detail-Plan:** `CINEMATIC_UPGRADE_PLAN.md` (Schwachstellenbericht V2)
- **Verbleibende Quick-Wins:** FastAPI-Migration, Connection-Pool, Visual-Continuity-Verbesserung

## Was du als nächstes machen solltest

Du suchst ein konkretes Feature? Schau in:
- `docs/80-schwachstellen-tracker.md` — was noch offen ist
- `CINEMATIC_UPGRADE_PLAN.md` — der Detail-Plan mit Hebel/Aufwand

Du willst das System verstehen? Schau in:
- `engine/*.py` — die 5 Engine-Module (je 200-500 Z., sauber gekapselt)
- `engine/presets.py` — die 5 Stil-Presets als Copy-Paste-fertige Prompts

## Offene Wunden (ehrlich)

1. **dashboard.py ist 3300 Z.** — der HTTP-Handler sollte in `routes/` raus. Riskanter Refactor. Empfehlung: kleine, isolierte Commits.
2. **Visual-Continuity ist ~70% zuverlässig** — KIE variiert trotz Char-Refs. Manuell nachkorrigieren.
3. **Tests sind teilweise Source-Grep** statt echte E2E.
4. **Kein Production-Deployment** — kein Dockerfile, keine HTTPS, kein Auth.
