# ARCHITECTURE — Storyboard Generator (Stand 2026-07-12)

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
   └─ Beim Tippen läuft ein 2.5s Debounce-Speicher. Beim Klick auf "Planen" wird ein synchroner harter Save-Befehl gefeuert, damit kein Text verloren geht.

2. ANALYZE (LLM)
   └─ Gemini (via KIE.ai) zerlegt Skript in:
      - Szenen mit start_aligned/end_aligned (Whisper-getimed)
      - Phase (OPENING/RISING_ACTION/CLIMAX/RESOLUTION)
      - Hook (Cold-Open) + Throughline-Question
      - Pacing (calm/normal/punchy)
      - Visual Sequences (für Style-Konsistenz über Szenen)
      - Charsheets (Char-Referenz-Bilder). Hier greift neuerdings eine Ausnahme zur "invent nothing"-Regel: Fehlt eine optische Beschreibung im Text, MUSS die KI einen Basis-Look (z.B. "young man") erfinden, um den Charakter nicht versehentlich ganz wegzuwerfen.

3. VOICE (TTS) — die einzige Audio-Quelle
   └─ ElevenLabs TTS → MP3 mit Narration. Default-Modell eleven_multilingual_v2
      (nicht v3 — v3 ist ausdrucksstark, aber die Kadenz ist langsam/dramatisch und
      der speed-Parameter wirkt dort kaum; v2 respektiert speed zuverlässig und
      unterstützt Request-Stitching-Continuity, Audit Juli 2026)
   └─ Default-Voice-Preset (recherche-basiert, Doku-Narration): stability 0.4,
      similarity_boost 0.75, style 0.0, speed 1.1
   └─ Whisper → Wort-Level-Timing
   └─ Pause-Trim (Szenen-Grenzen landen auf Satzenden)

4. PROMPT-BAU pro Szene
   └─ Master-Preset + Phase-Cue + Hook-Cue + Charsheet-Beschreibung + Charsheet-PNG
   └─ Müll-Filter (verhindert "Stick-Figure"-Test-Müll in Prompts)
   └─ Charsheet-PNGs als data:image/png;base64 (Style-Anker für KIE)
   └─ Charsheets erben jetzt den Kanal-Master-Prompt (kein hardcodierter Stil mehr,
      siehe engine/prompts.py:gen_charsheet — Audit Juli 2026)

5. BILDERZEUGUNG (engine/imagegen.py — Provider-Interface)
   └─ generate_image(prompt, ref_urls, provider="kie") — einheitliches Interface,
      ein zweiter Provider (z.B. FLUX Kontext) braucht nur einen neuen Registry-Eintrag
   └─ Pro Szene: POST an KIE.ai (16:9, 2K, nano-banana-2)
   └─ Referenzen (bis zu 14 Bilder, Reihenfolge = Gewichtung): Chain-Anker + Chain-
      Vorgänger + Entity-Anchor (+ Charsheet, wenn vorhanden) + bis zu 3 Style-
      Referenzen (Settings, IMMER angehängt — auch bei Charakter-Szenen)
   └─ "CONTINUITY (STRICT)" Prompt zwingt zur visuellen Konsistenz
   └─ Rate-Limit + Circuit Breaker (Hardening aus Schwachstellenbericht)
   └─ Referenz-Hosting: KIE File-Upload-API (24h-TTL, kein Self-Block), catbox/
      litterbox nur noch Fallback-Kette bei KIE-Ausfall
   └─ Kein Seed-Parameter — empirisch verifiziert (echte KIE + Google-Gemini-Calls,
      Juli 2026): nano-banana-2 hat keinen wirksamen seed, egal über welchen Provider

6. RENDERING (ffmpeg, engine/render.py)
   └─ Pro Szene: zoompan (Ken Burns) — entweder reiner Zoom ODER reiner Pan/Tilt,
      nie kombiniert; Geschwindigkeit skaliert mit der echten Szenendauer (konstante
      %/s statt fixer Distanz), Zoom-Fokus/Clamp-sicher; Anti-Monotonie verhindert
      exakte Wiederholung der Vorszenen-Bewegung; sehr kurze Szenen (<2s) bleiben
      bewusst als ruhiges Standbild
   └─ Übergänge: xfade-Rotation über die Vorkommen-Position (nicht den Szenenindex),
      4 Sub-Typen pro Familie gegen Monotonie
   └─ Color-Grading pro Phase (colorbalance + CLIMAX-Vignette)
   └─ Overlays: CapCut-Style 1-Wort-Captions (word_caption_seq), callout, counter
      (animiert) — Akt-Einspieler/Titelkarten entfernt, Szenen rendern durchgehend
      mit ihrem echten Bild
   └─ Audio: nur die rohe (pausen-gekürzte) Sprecherspur wird gemuxt (KEINE Musik,
      KEINE SFX — Sound-Design-Kette aus engine/audio.py bleibt dormant im Code)
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
│       │   ├── audio.py     ── Voice→Sync (dormant:    │
│       │   │                    Musik/SFX-Mix-Kette)   │
│       │   ├── prompts.py   ── Prompt-Bau              │
│       │   ├── presets.py   ── 5 Stil-Presets         │
│       │   └── imagegen.py  ── Bild-Provider-Interface │
│       │                        (KIE-Submit/Poll/      │
│       │                        Upload, generate_image)│
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
    style_ref_url.txt    ── bis zu 3 Style-Referenz-URLs (1 pro Zeile)
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
3. **Style-Referenzen** (Settings-Tab, bis zu 3 Slots): definieren den globalen
   Grafik-Stil, werden an JEDE Bild-Generierung angehängt (auch Charakter-Szenen)
4. **Char-Referenzen** (Video-Tab, pro Video): Charsheet-PNGs hochladen/generieren
5. **Master-Prompt** (Settings-Tab): Stil-Beschreibung editieren
6. **Plan-Generierung**: Button → LLM generiert Szenen
7. **Bilderzeugung pro Szene**: Button "Generieren" → KIE rendert
8. **Video-Render**: Button → ffmpeg assembliert (final.mp4 mit Voice + Bildern)

## Was du NICHT im Frontend steuerst

- Musik-Auswahl (kein Dropdown)
- Sound-Effekt-Library (keine Trigger)
- Hintergrund-Audio (keine Auswahl)
- Finale Audio-Synchronisation (Voice ist alles, was das System macht)

## Schwachstellen-Status

- **Out-of-scope (vom User explizit ausgeschlossen):**
  - Komplexe Audio-Stem-Pipeline (Phase K MUSIC_BEDS, Segment-Kette) — Code bleibt
    dormant in `engine/audio.py`, wird von `_render_worker` nicht mehr aufgerufen
  - Sound-Effekt-Library / Auto-SFX-Trigger — Sound legst du selbst in Logic/DaVinci
- **Verbleibende Quick-Wins:** FastAPI-Migration (nicht empfohlen, siehe Umbau-
  Evaluation Juli 2026 — Bottleneck ist Bildgenerierung/ffmpeg, nicht Request-
  Routing), Connection-Pool

## Was du als nächstes machen solltest

Du suchst ein konkretes Feature? Schau in:
- `engine/*.py` — die 6 Engine-Module (je 200-1000 Z., sauber gekapselt)
- `engine/presets.py` — die 5 Stil-Presets als Copy-Paste-fertige Prompts
- `docs/PROMPT_PIPELINE.md` / `docs/RUNBOOK.md` — Pipeline-Details + Troubleshooting

## Offene Wunden (ehrlich)

1. **dashboard.py ist ~4260 Z.** — der HTTP-Handler sollte in `routes/` raus. Riskanter Refactor. Empfehlung: kleine, isolierte Commits.
2. **Visual-Continuity ist ~70-80% zuverlässig** — KIE variiert trotz Char-/Style-Refs. Manuell nachkorrigieren.
3. **Tests sind teilweise Source-Grep** statt echte E2E.
4. **Kein Production-Deployment** — kein Dockerfile, keine HTTPS, kein Auth.
