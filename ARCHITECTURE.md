# ARCHITECTURE — Storyboard Generator (Stand 2026-07-24)

## Was das System tut (in einem Satz)

**Du gibst ein Script ein → das System generiert für jede Szene ein Bild mit cinematischen Effekten → die Bilder werden mit Ken-Burns-Effekten zu einem Video zusammengeschnitten, bei dem der Schnitt EXAKT auf dem Sprach-Timing (ElevenLabs TTS) liegt → du machst Sound im Nachgang in Logic/DaVinci.** Zusätzlich generiert das System aus demselben Video automatisiert 5 eigenständige Hook-Shorts (eigenes Skript, eigener Hook, eigenes Voiceover — wiederverwendet nur die Longform-Bilder) und kann fertige Videos/Shorts über eine eigene Queue direkt auf YouTube hochladen (immer privat + geplant, nie automatisch öffentlich).



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
      - Pacing (calm/normal/punchy) — Zieldauern pro Format über `PacingProfile`/
        `PACING_PROFILES` (`engine/scenes.py`, getrennte Profile für longform/short);
        die zugrunde liegende Sprechrate wird real aus fertigen Videos des Kanals
        gemessen (`dashboard._measure_channel_wpm`, `GET /api/measure_wpm`) statt
        angenommen — Audit Juli 2026 zeigte reale 164-188 wpm statt der vorher
        angenommenen 120-150 wpm, was den Schnittrhythmus verzerrte
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

8. SHORT-SKRIPTE + HOOK-SHORTS (aus demselben Video, eigenes Kapitel im Stepper)
   └─ generate_short_scripts (engine/prompts.py) — EIN LLM-Call liefert 5
      eigenständige Short-Skripte (eigener Hook, eigener Winkel, eigenes CTA),
      70-110 Wörter, Hook in Sek 1-2 — bewusst KEIN Audio-Schnitt aus dem Longform
   └─ assign_short_scene_images (engine/prompts.py) — EIN batched LLM-Call ordnet
      allen Short-Szenen bereits vorhandene Longform-Bilder zu (Fallback: Keyword-
      Overlap, danach gen_image) → Regelfall 0 neue KIE-Bildgenerierungen
   └─ Segmentierung mit PACING_PROFILES["short"] (deutlich schnellerer Schnitt,
      ~1.5-2s/Bild statt Longforms ~5-7s)
   └─ Eigenes ElevenLabs-Voiceover pro Short, eigener Whisper-Align
   └─ Render über RENDER_TARGETS[f"short_hook_{n}"] (9:16, Blur-Pillarbox,
      engine/render.py) — Worker: shorts/api.py:_hooks_worker
   └─ Frontend-Gate: Stepper-Schritt ⑥ zeigt alle 5 Skripte zur Review/Korrektur,
      BEVOR TTS + 5 Renders laufen (dasselbe Muster wie die Titel-Auswahl)

9. VERÖFFENTLICHEN (optional, separat von der Video-Erzeugung)
   └─ control/ — kanalübergreifende Queue-UI unter GET /control (Queue-Status,
      OAuth-Connect, Quota, Playlist/Schedule)
   └─ youtube/ — eigener OAuth-Flow, Resumable-Upload, Quota-Deckel (100/Tag)
   └─ Harte Regel, kein Codepfad kann sie umgehen: privacyStatus=private + echtes
      publishAt — nichts wird automatisch öffentlich
   └─ Job-Queue liegt in store/pipeline.db (SQLite), nicht im channels/-JSON-Baum
```

## Ältere Short-Pfade (Code vorhanden, teils UI-ausgeblendet)

- **Climax-Highlight-Short** (`shorts/clip_select.py:select_scenes`) — wählt eine
  bestehende Longform-Passage aus, hat kein eigenes Skript/Voiceover. Juli 2026
  (User-Report "sauberer Flow für die neue Short-Engine"): Karte trug dieselbe
  Stufennummer wie Hook-Shorts und war beide nur nach Longform-Render sichtbar —
  jetzt wie Parts per `display:none` ausgeblendet (`updateShortsCardVisibility()`),
  Backend bleibt erreichbar.
- **Parts-Pipeline** (`_parts_worker`, `split_into_parts`, `shorts/cta.py`,
  `/api/shorts/split_parts`) — **deprecated**: sequenzielle „Part N"-Schnitte ohne
  eigenen Hook waren die Analytics-verifizierte Hauptursache für 4-50 Views bei 10
  Shorts (vs. 3-14 Views Longform). Code bleibt erhalten (6 Videos liegen bereits
  live bei YouTube), die UI dazu ist in `dashboard.html` bewusst mit
  `display:none` ausgeblendet statt gelöscht.

## Komponenten

Weiterhin ein Ein-Prozess-Dashboard, aber `dashboard.py` ist nicht mehr die einzige
Codebasis: `routes/` ist ein bewusst minimaler **Prefix-Dispatch-Shim** (kein
vollständiger Routing-Umzug — siehe „Offene Wunden"), der drei Endpunkt-Familien in
eigene Pakete auslagert. Der große Rest (Channels, Videos, Plan, Render, Transcribe,
Voiceover, …) bleibt weiterhin direkt in `dashboard.py`.

```
┌───────────────────────────────────────────────────────────────┐
│              DASHBOARD (1 Prozess, weiterhin größter Teil)     │
│                                                                 │
│  dashboard.py (~4660 Z.) ── HTTP-Server (ThreadingHTTPS)       │
│       │  ~75 Routen weiterhin direkt im if/elif-Dispatch        │
│       │  + routes.dispatch() als Vorstufe (Prefix-Mounts unten) │
│       │                                                         │
│       ├── engine/                                               │
│       │   ├── scenes.py   ── Text→Szenen; PacingProfile         │
│       │   │                   (longform/short getrennt kalibriert)│
│       │   ├── render.py   ── Szenen→Video (ffmpeg); RenderTarget │
│       │   │                   (longform/short_vertical/          │
│       │   │                   short_hook_N — 9:16 Blur-Pillarbox)│
│       │   ├── audio.py    ── Voice→Sync (dormant: Musik/SFX-     │
│       │   │                   Mix-Kette)                         │
│       │   ├── prompts.py  ── Prompt-Bau; Short-Skript-Generierung│
│       │   │                   + Longform→Short Bild-Zuordnung    │
│       │   ├── presets.py  ── 5 Stil-Presets                     │
│       │   └── imagegen.py ── Bild-Provider-Interface             │
│       │                                                         │
│       └── engine_elevenlabs.py  ── TTS-Integration               │
│                                                                 │
│  dashboard.html ── Frontend, 8-Schritte-Stepper                 │
│  control.html   ── separate Upload-Kontroll-UI (GET /control)    │
└───────────────────────────────────────────────────────────────┘
        │                     │                       │
   mount("/api/shorts/")  mount("/api/youtube/")  mount("/api/control/")
        │                     │                       │
   shorts/api.py          youtube/                 control/api.py
   Hook-Shorts (aktiv) +   {oauth,upload,quota,     Queue/Quota/
   Climax-Short (aktiv) +  metadata,api}.py         Schedule-UI
   Parts (deprecated,                                (siehe control.html)
   UI ausgeblendet)
        │                     │                       │
        └─────────────────────┴───────────────────────┘
                              │
                store/db.py (SQLite, store/pipeline.db)
                youtube_oauth · quota_usage · upload_queue
        │
        ├── KIE.ai (REST)        ── Bilder + LLM + Whisper
        ├── ElevenLabs (REST)    ── TTS
        └── YouTube Data API v3  ── Upload (immer privat + geplant)
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

store/
  pipeline.db           ── SQLite (-wal/-shm daneben), bewusst getrennt von
                            channels/-JSON — Cross-Video/Restart-fester State
    youtube_oauth        ── OAuth-Tokens pro Kanal
    quota_usage          ── Tages-Ledger Upload-Quota (Pacific-Zeit)
    upload_queue          ── Job-Queue geplanter YT-Uploads
                             (CHECK privacy_status='private')
```

`channels/` ist pro Video/Kanal und JSON-basiert; `store/pipeline.db` ist
Cross-Entity und wird nur von `shorts/`, `youtube/`, `control/` angefasst —
`dashboard.py` selbst initialisiert die DB nur beim Start (`store_db.init_db()`).

`~/.youtube_oauth_client.json` liegt bewusst **außerhalb** des Repos (echte
OAuth-Zugangsdaten, chmod 600) — weder in `channels/` noch in `store/`.

## Stil / Identität / Inhalt — die drei Ebenen (Architekturregel)

Bildkonsistenz entsteht nur, wenn diese drei Ebenen **strikt getrennt** bleiben. Ein
neues Video im selben Kanal bringt neue Charaktere und neue Szenen mit, erbt aber den
Stil unverändert — deshalb darf nichts Video-Spezifisches in die Stil-Ebene rutschen.

| Ebene | Ort | Gültigkeit | Inhalt |
|---|---|---|---|
| **Stil** | `channels/<cid>/master_prompt.txt` (Startwert aus `PRESET_MASTERS`) + `style_ref_url.txt` | ganzer Kanal | **WIE** gezeichnet wird: Linienführung, Flatcolors, weißer Hintergrund, Body-Rule. Enthält **niemals** Charakternamen, Kleidungsfarben oder Szeneninhalte. |
| **Identität** | `videos/<vid>/charsheets/<name>.json` + `.png` | ein Video | **WER** dargestellt wird: rotes T-Shirt, braune Haare, konkreter Hautton. |
| **Inhalt** | `videos/<vid>/generated/plan.json` → `scenes[].prompt` | eine Szene | **WAS** passiert: Pose, Handlung, Kamera. |

Konkretes Beispiel für die Regel: Die Body-Rule im Master sagt *„Gliedmaßen als
schwarze Linien, Haut flach und umrandet ohne Schattierung — Hautton aus dem
Referenzbild"*. Sie schreibt **keinen** Hautton-Hex fest, denn das wäre Identität und
würde jeden künftigen Charakter zwangsvereinheitlichen. Den Ton liefert das Charsheet.

Damit die Identitäts-Ebene die Inhalts-Ebene überstimmen kann, übergibt die
Bild-Generierung `char_refs` + `entity` an `_build_image_prompt()` — daraus entsteht
der Steckbrief plus die Regel „bei Widerspruch gewinnt das Referenzbild". Fehlt das,
gewinnt der (chunkweise neu erfundene) Szenentext, und Charaktere driften.
Details + Fehleranalyse: `docs/PROMPT_PIPELINE.md` §14.

## Was du im Frontend steuerst

Der Stepper in `dashboard.html` führt Schritt für Schritt (① Thema → ⑧
Veröffentlichen) durch die volle Kette; die folgenden Punkte sind die Stellen, an
denen du tatsächlich eingreifst:

1. **Skript-Preset** (Kanal-Anlage): flat_cartoon_doc (default), editorial_minimal, ink_documentary, charcoal_noir, stick_minimal
2. **Skript** (Skript-Tab): Text eingeben, Sprache wählen
3. **Style-Referenzen** (Settings-Tab, bis zu 3 Slots): definieren den globalen
   Grafik-Stil, werden an JEDE Bild-Generierung angehängt (auch Charakter-Szenen)
4. **Char-Referenzen** (Video-Tab, pro Video): Charsheet-PNGs hochladen/generieren
5. **Master-Prompt** (Settings-Tab): Stil-Beschreibung editieren
6. **Plan-Generierung**: Button → LLM generiert Szenen
7. **Bilderzeugung pro Szene**: Button "Generieren" → KIE rendert
8. **Video-Render**: Button → ffmpeg assembliert (final.mp4 mit Voice + Bildern)
9. **Short-Skripte generieren + reviewen** (Stepper-Schritt ⑥): Button → 1
   LLM-Call liefert 5 unabhängige Short-Skripte (eigener Hook/Winkel/CTA); jedes
   einzeln editier-/verwerfbar, bevor TTS + Render laufen (Review-Gate vor dem
   teuren Teil, analog zur Titel-Auswahl)
10. **Shorts bauen** (Stepper-Schritt ⑦): Button → pro bestätigtem Short:
    Bild-Zuordnung, eigenes Voiceover, Whisper-Align, Render (9:16) — mit
    Fortschrittsanzeige pro Short
11. **Veröffentlichen** (Stepper-Schritt ⑧ + eigene Seite `/control`): Datei in
    die Upload-Queue geben, Zeitpunkt/Playlist setzen — Upload läuft immer
    privat + geplant

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

1. **dashboard.py ist ~4660 Z.** (gewachsen von ~4260) — der HTTP-Handler sollte in `routes/` raus. Riskanter Refactor. Empfehlung: kleine, isolierte Commits.
2. **`routes/` ist ein Dispatch-Shim, keine vollständige Extraktion** — nur `shorts/`, `youtube/`, `control/` sind ausgelagert (über `mount()`/`dispatch()`); der Rest (Channels, Videos, Plan, Render, Transcribe, Voiceover, …) bleibt vollständig in `dashboard.py`. Das Modul dokumentiert selbst, dass ein voller Umzug als zu riskant verworfen wurde.
3. **Visual-Continuity ist ~70-80% zuverlässig** — KIE variiert trotz Char-/Style-Refs. Manuell nachkorrigieren.
4. **Tests sind teilweise Source-Grep** statt echte E2E.
5. **Kein Production-Deployment** — kein Dockerfile, keine HTTPS, kein Auth.
