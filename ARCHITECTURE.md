# Storyboard Generator — Architektur-Überblick

Stand: Juli 2026. Referenz-Dokument, damit man nach längerer Pause wieder reinfindet, ohne den ganzen Code neu lesen zu müssen. Alle Zeilenangaben beziehen sich auf den aktuellen Stand von `dashboard.py` / `dashboard.html` — bei größeren Änderungen können sie sich verschieben, die Funktionsnamen bleiben der verlässlichere Anker.

## 1. Was das Programm macht

Lokales Tool (läuft auf `localhost:8765`), das aus einem Sprecher-Skript automatisch ein komplettes Storyboard baut: Skript → Szenen mit Timing → Bild-Prompts (oder Video-Prompts) → generierte Bilder/Videos über KIE.ai → optional Titel + Thumbnail für YouTube. Ein **Kanal** = ein visueller Stil/Charakter (z.B. "Ink Explainer"). Jeder Kanal kann beliebig viele **Videos** enthalten, jedes Video hat sein eigenes Skript/Szenen-Plan.

## 2. Tech-Stack — bewusst minimal

- **Backend**: `dashboard.py`, ein einziges File, nur Python-Stdlib (`http.server.ThreadingHTTPServer`, kein Flask/FastAPI). ~2760 Zeilen.
- **Frontend**: `dashboard.html`, ein einziges File — Vanilla JS, kein Build-Step, kein Framework. Wird bei **jedem** Request frisch von der Platte gelesen (Zeile 2034: `open(... "dashboard.html").read()`), d.h. Frontend-Änderungen brauchen **keinen Server-Neustart**, Backend-Änderungen (`dashboard.py`) schon.
- **Externe Dienste**: alles über **KIE.ai** als zentralen API-Broker — Bildgenerierung (nano-banana-2/-lite), Textgenerierung (Gemini 2.5 Flash + Gemini 3.5 Flash native), Video (Veo 3.1, Grok Imagine T2V/I2V).
- **Datenhaltung**: keine Datenbank — alles als JSON-Dateien im Filesystem unter `channels/`.

## 3. Verzeichnisstruktur (Datenmodell)

```
channels/
  channels.json                       # Liste aller Kanäle [{id, name}]
  <cid>/                              # ein Kanal
    master_prompt.txt                 # Bild-Stil (Charakter/Farben/Linienführung)
    video_master_prompt.txt           # Video-Stil (für T2V-Modus)
    char_ref_url.txt                  # EIN globales Referenzbild fürs ganze Kanal (Veo-Konsistenz)
    char_ref.png                      # lokale Kopie davon
    charsheets/                       # benannte Charakter-Referenzen (mehrere möglich)
      <name>.png / <name>.json        # {name, description, safe, mime}
    videos.json                       # [{id, name, mode, created_ts}] — Liste der Videos in diesem Kanal
    videos/<vid>/
      generated/
        plan.json                     # DAS zentrale Szenen-Dokument (siehe unten)
        000.jpg, 001.jpg, ...         # generierte Bilder
        000.mp4, ...                  # generierte Videos (Bild-Modus: I2V-Animation)
        thumbnail.jpg
      uploads/
        voiceover.<ext>                # hochgeladene Audiodatei
        audio_meta.json                # {path, mime, name}
      meta.json                        # {titles: [...], selected_title, thumbnail_prompt, image_model}
```

`plan.json` — eine Liste von Szenen-Objekten, das Herzstück, das Backend und Frontend ständig synchron halten:
```json
{
  "scenes": [
    {"i": 0, "start": 0.0, "dur": 4.2, "text": "...", "t": "0:00",
     "prompt": "...", "file": "000.jpg", "status": "fertig",
     "source_url": "https://...", "source_url_ts": 173...,
     "video_prompt": "...", "video_file": "000.mp4", "phase": "OPENING",
     "concrete_entity": "char_02",
     "seq_id": 2, "seq_pos": 1, "chain_anchor_file": "003.jpg", "chain_prev_file": "004.jpg",
     "char_ref_applied": true,
     "motion": {"type": "zoom_in", "z_end": 1.12, "focus": [0.5, 0.45]}, "clip_file": "005.mp4"}
  ],
  "wpm": 150, "sec": 4, "characters": [...],
  "audio_duration": 187.4,
  "render": {"file": "final.mp4", "ts": 173..., "checks": {"duration_ok": true, "audio_ok": true, "frames_ok": true}}
}
```
`status` ∈ `geplant | läuft | fertig | fehler`. Jeder Schreibzugriff auf `plan.json` läuft über `_PLAN_WRITE_LOCK` (dashboard.py:35) — siehe Abschnitt 6.3, das war ein echter Bug. Alle Felder ab `concrete_entity` sind **additiv/optional** (Feature A/B, siehe Abschnitt 12/13) — alte Pläne ohne diese Felder bleiben ohne Änderung ladbar.

## 4. High-Level-Datenfluss

```
Skript (Text)
   │
   ▼ split_units()                              [557]
Atomare Sätze/Teilsätze
   │
   ▼ analyze_script(units)                       [655]   ── EIN LLM-Call, liest das GANZE Skript
Analyse: locations, characters, recurring_symbols,
emotional_arc, callbacks, pacing (pro Einheit)
   │
   ▼ segment_by_pacing(units, pacing, wpm, sec)  [563]
Szenen mit variabler Dauer (calm bis 6s, punchy <1.5s)
   │
   ▼ visual_prompts(scenes, analysis)            [820]   ── Bild-Modus
   │  ODER video_prompts_batch(...)              [1018]  ── Video-Modus (aktuell nicht im Haupt-Flow verdrahtet, s.u.)
Bild-Prompt pro Szene (chunked, validiert, retry)
   │
   ▼ _build_image_prompt(prompt, master, char_refs) [1142]
Voller Prompt = Szenen-Text + Charakter-Beschreibungen + Master-Stil
   │
   ▼ _kie_submit_image() → poll → download        [1187, 1267]
Fertiges Bild in generated/NNN.jpg, plan.json aktualisiert
```

Für Videos (T2V/Veo) ist der Pfad separat und **on-demand pro Szene**, nicht Teil der Plan-Erstellung — siehe Abschnitt 7.

## 5. Die zwei Betriebsmodi (`mode`: `"image"` | `"video"`)

Pro Video (`videos.json` → `mode`-Feld) einstellbar, im Frontend über den Segmented-Control oben im Editor (`setMode()`, dashboard.html:820).

- **Bild-Modus** (Standard): Skript → Szenen → **Bilder** (nano-banana-2). Optional pro Szene per I2V ("Animieren"-Button) zu einem kurzen Grok-Video animiert (`gen_video()`, dashboard.py:1925 — **nicht** Veo, sondern `grok-imagine/image-to-video`).
- **Video-Modus** (T2V): Skript → Szenen → **direkt Videos**, kein Bild-Zwischenschritt. Zwei parallele Engines existieren im Code:
  - **Veo 3.1** (`gen_veo`/`extend_veo`, dashboard.py:1809/1840) — das ist der tatsächlich verdrahtete Pfad (`/api/generate_t2v`, dashboard.py:2308), inkl. Chain-Extend (Abschnitt 7.2).
  - **Grok T2V** (`gen_t2v`, dashboard.py:1758, Modell `grok-imagine/text-to-video`) — Funktion existiert, ist aber an keinem HTTP-Endpunkt angeschlossen (aktuell toter Code / Altlast aus einer früheren Iteration).

`renderScenes()` im Frontend (dashboard.html:1001) rendert je nach `CURRENT_MODE` komplett unterschiedliches HTML pro Szene (zwei Spalten Bild+Video im Bild-Modus vs. eine Video-Spalte + Prompt-Textarea im Video-Modus).

## 6. Backend-Kernkonzepte

### 6.1 Globale Zustands-Dictionaries (dashboard.py:17–63)

Kein Datenbank, kein Redis — alles In-Memory-Dicts im laufenden Python-Prozess, mit `threading.Lock()` geschützt:

| Dict | Zweck | Lock |
|---|---|---|
| `JOBS` | Einzelner Bild-/Video-Generierungs-Job → `{status, progress, file, error}` | (kein eigener, GIL reicht für einfache dict-writes) |
| `ACTIVE_SCENE_JOBS` | `(cid,vid,scene_i) → job_id`, verhindert doppelte Generierung derselben Szene | `_ACTIVE_SCENE_JOBS_LOCK` |
| `BATCH_JOBS` | Status von "Alle Bilder generieren" pro Video | `_BATCH_JOBS_LOCK` |
| `PLAN_JOBS` | Status von "Plan aus Skript erstellen" pro Video | `_PLAN_JOBS_LOCK` |
| `RENDER_JOBS` | Status von "Video zusammenschneiden" (Auto-Rendering, Abschnitt 13) pro Video | `_RENDER_JOBS_LOCK` |

**Wichtiges Muster, das sich wiederholt**: Alle drei "langlaufenden" Aktionen (Plan erstellen, alle Bilder generieren, Transkription) laufen als **Server-seitiger Background-Thread**, nicht als eine einzige lange HTTP-Anfrage. Grund (mehrfach live aufgetreten): eine blockierende HTTP-Anfrage stirbt, wenn der Tab geschlossen/neu geladen wird — das Frontend denkt dann "nichts passiert" und der Nutzer klickt erneut, was einen zweiten, komplett unabhängigen LLM-Lauf auf demselben Skript startet (doppelte Kosten). Die Lösung überall gleich: Endpunkt startet nur einen `threading.Thread(daemon=True)`, merkt sich `running=True` **atomisch mit der "läuft schon?"-Prüfung** (nicht danach!), und das Frontend pollt einen `_status`-Endpunkt.

### 6.2 Nebenläufigkeit bei der Bildgenerierung

- `IMAGE_GEN_SEMAPHORE = threading.Semaphore(8)` (dashboard.py:43) — globales Limit, wie viele KIE-Bild-Tasks gleichzeitig laufen dürfen, unabhängig davon ob sie vom Batch-Worker oder einem einzelnen Klick kommen.
- `_batch_generate_worker()` (dashboard.py:1324) nutzt `concurrent.futures.ThreadPoolExecutor(max_workers=8)`, um bis zu 8 Szenen parallel zu bearbeiten (`process_scene()`, verschachtelte Funktion ab Zeile 1359).
- `_kie_rate_limit_wait()` (dashboard.py:1175) — zusätzlicher Schutz: max. 12 Submits pro 10 Sekunden prozessweit (KIEs echtes Limit: 20/10s), damit die 8 parallelen Worker nicht gleichzeitig eine Burst-Rate-Limit-Fehlermeldung auslösen. Bei "call frequency too high" wird automatisch mit Backoff wiederholt (`_kie_submit_image`, Zeile 1210 ff.), bei "insufficient credits" wird der **ganze Batch sofort gestoppt** (nicht jede Szene einzeln durchprobiert).
- `_PLAN_WRITE_LOCK` (dashboard.py:35) — schützt jedes Lesen-Ändern-Schreiben von `plan.json`. **Historischer Bug**: ohne diesen Lock konnten zwei Szenen, die fast gleichzeitig fertig wurden, sich gegenseitig überschreiben (Thread B liest eine Momentaufnahme, bevor Thread A geschrieben hat → Thread B's Schreibvorgang macht A's Update rückgängig). Das erklärte zufällig "verschwindende" fertige Bilder.

### 6.3 KIE.ai-Anbindung — drei verschiedene API-Formate

| Funktion | Endpunkt | Zweck |
|---|---|---|
| `post_kie_text()` (270) | `/gemini-2.5-flash/v1/chat/completions` | OpenAI-kompatibel, für Transkription + Charakter-Bildanalyse |
| `post_gemini_native()` (289) | `/gemini/v1/models/{model}:generateContent` | Natives Gemini-Format (contents/parts), nutzt `gemini-3-5-flash` mit `thinkingLevel: high` — verhindert "faules"/generisches Verhalten bei späteren Items in einem Batch. **Wird für fast alle Analyse-/Prompt-Generierungs-Calls genutzt** (analyze_script, Bild-/Video-Prompts, Titel, Thumbnail-Prompt, Skript-Generator). |
| `_kie_submit_image()` (1187) | `/api/v1/jobs/createTask`, Modell `nano-banana-2`/`-lite` | Bildgenerierung |
| `gen_veo()`/`extend_veo()` (1809/1840) | `/api/v1/veo/generate`, `/extend` | Veo 3.1 Videos |
| `gen_video()` (1925) | `/api/v1/jobs/createTask`, Modell `grok-imagine/image-to-video` | Bild→Video-Animation im Bild-Modus |

**Wichtiger, einmal live gefundener Bug**: `nano-banana-2` erwartet Referenzbilder im Feld `image_input`, `nano-banana-2-lite` im Feld `image_urls` — das falsche Feld wird von KIE stillschweigend akzeptiert (HTTP 200), hat aber **keinerlei Effekt**. Siehe `_kie_submit_image()` Zeile 1204.

## 7. Die Prompt-Pipeline im Detail (Kernstück)

### 7.1 Bild-Modus: Skript → fertiger Bild-Prompt

**Schritt 1 — `split_units(text)`** (542): zerlegt in Sätze (Regex `[^.!?]+[.!?]?`), Sätze >22 Wörter werden zusätzlich an Kommas/Semikola gesplittet. Reiner Text-Preprocessing-Schritt, kein LLM.

**Schritt 2 — `analyze_script(units)`** (655): **Ein** LLM-Call (Gemini 3.5, `json_mode=True`) über das **gesamte** Skript. Liefert:
- `locations`, `characters` (mit `visual_description` + `anonymize`-Flag für echte, identifizierbare Personen — die werden später nie beim Namen genannt/realistisch gezeigt, nur als Silhouette/Symbol),
- `recurring_symbols` + `callbacks` (damit wiederkehrende visuelle Elemente konsistent bleiben),
- `emotional_arc` (Opening/Midpoint/Resolution als je ein Wort),
- `pacing` — **neu**, pro Einheit ein Label `calm`/`normal`/`punchy`, explizit im selben Call wie der `emotional_arc` bestimmt, damit das Pacing nicht unabhängig vom Spannungsbogen "driftet" (siehe Prompt-Text Zeile 680 ff. — die Einstufung soll die Position im Bogen berücksichtigen, nicht nur die Satzformulierung isoliert).

Dieses `analysis`-Dict wird **überall weitergereicht** — an die Segmentierung (`pacing`), an die Bild-Prompt-Chunks, an die Video-Prompt-Chunks, an die Anonymisierungs-Prüfung. Es wird pro Plan-Erstellung nur **einmal** berechnet (`visual_prompts()` überspringt einen zweiten `analyze_script()`-Call, wenn `analysis` schon übergeben wurde — Zeile 834).

**Schritt 3 — `segment_by_pacing(units, pacing, wpm, sec)`** (563): gruppiert die Einheiten zu Szenen. `calm` darf bis `MAX_SCENE_SEC=6.0s` halten, `punchy` wird auf ~1.1s komprimiert (bei langen Sätzen sogar in zwei Bilder gesplittet für den "Gut-Punch"-Effekt), `normal` folgt dem Nutzer-Wert (`sec`-Feld im Frontend). Harter Deckel bei 6s wird **immer** durchgesetzt, auch wenn eine einzelne Einheit für sich schon zu lang ist (Zeile 620 ff. — dieselbe Bug-Klasse wie einst bei der alten festen `segment()`, hier neu gefixt). Sicherheitsnetz: warnt im Log, wenn >30% als "punchy" eingestuft werden (`PACING_WARN_THRESHOLD`).

**Schritt 4 — `visual_prompts(scenes, analysis)`** (820): generiert den eigentlichen Bild-Prompt-Text pro Szene. Läuft **gechunkt** (`IMAGE_PROMPT_CHUNK_SIZE=20` Szenen pro LLM-Call — Grund: die Analyse+Few-Shot-Beispiele werden bei jedem Chunk-Call komplett mitgeschickt, größere Chunks = weniger Wiederholung = günstiger). Jeder Chunk-Call (`_image_prompt_chunk()`, 748) zwingt das Modell zu Zwischenfeldern, bevor der finale Prompt geschrieben wird:
```
scene → core_statement → concrete_entity → callback_check → character_consistency → image_prompt
```
Das verhindert vage, generische Prompts ("dark ominous scene") — das Modell muss zuerst explizit benennen, WAS die Zeile eigentlich behauptet und WELCHE konkrete Entität aus der Analyse gemeint ist, bevor es den Bildtext schreibt. `_validate_image_prompt_entry()` (736) prüft danach: mindestens `IMAGE_PROMPT_MIN_LEN=220` Zeichen, und die genannte `concrete_entity` muss tatsächlich im Prompt-Text vorkommen (außer bei anonymisierten Personen — dort wäre das ja gerade falsch). Bei Fehlschlag: `_image_prompt_single_retry()` (808), ein fokussierter Einzel-Call nur für diese eine Szene.

**Fehlerresistenz beim Chunking**: `_fetch_image_chunk()` (840) — wenn ein Chunk-Call fehlschlägt (z.B. abgeschnittenes JSON bei großen Antworten), wird der Chunk **halbiert und beide Hälften einzeln neu versucht**, rekursiv, statt gleich zum generischen Fallback-Text zu greifen. Ein Timeout kostet so nur die halbe Chunk-Größe, nicht den ganzen Chunk.

**Schritt 5 — `_build_image_prompt(scene_prompt, master, char_refs)`** (1142): baut den **finalen** an KIE gesendeten Text zusammen: `Szenen-Prompt + Charakter-Design-Hinweise (aus charsheets/) + Master-Prompt (Stil/Farben/Linienführung)`. Der Master-Prompt wird hier **wörtlich angehängt**, nicht nur dem LLM als Kontext gegeben — die Stil-Durchsetzung passiert also durch reine String-Konkatenation direkt vor dem Absenden, nicht durch "Vertrauen" ins Sprachmodell.

### 7.2 Video-Modus: T2V über Veo 3.1

Anders als Bilder wird der Video-Prompt **nicht** beim Plan-Erstellen fertig generiert, sondern **on-demand pro Szene**, wenn der Nutzer auf "Generieren" klickt (`/api/generate_t2v`, dashboard.py:2308 → `make_t2v_prompt()`, 1695). Grund vermutlich: Video-Generierung ist teuer/langsam, man will nicht 170 Video-Prompts vorab bezahlen, wenn nur ein paar Szenen wirklich als Video gebraucht werden.

`make_t2v_prompt()` bekommt: den Szenentext, die Story-Phase (`OPENING`/`RISING ACTION`/`CLIMAX`/`RESOLUTION`, berechnet aus der Position im Skript — `story_phase()`, 874), die letzten 2 vorherigen Video-Prompts (für visuelle Kontinuität), und das **volle Skript** als Kontext (damit z.B. Eigennamen korrekt erkannt werden). Ergebnis muss ≥`VIDEO_PROMPT_MIN_LEN=280` Zeichen sein und vier Dinge explizit benennen: Hauptmotiv, Setting, Licht-Stimmung, Kamera-Winkel.

**Chain-Extend** (dashboard.py:2344 ff.): Wenn die vorherige Szene in derselben Story-Phase ist UND ihre Extend-Kette noch nicht zu lang ist (`MAX_CHAIN_LENGTH=4`), wird `extend_veo()` (1840) statt `gen_veo()` genutzt — das **setzt das letzte Frame des vorherigen Videos fort**, echte Bild-zu-Bild-Kontinuität statt nur gleicher Stil. Sonst wird ein frischer Anker-Shot via `REFERENCE_2_VIDEO` (mit dem Channel-Charakter-Referenzbild) oder `TEXT_2_VIDEO` generiert.

Es gibt eine **eigene, unabhängige** Video-Prompt-Chunk-Pipeline (`video_prompts_batch()`/`_video_prompt_chunk()`, 1018/918) mit fast identischer Struktur wie die Bild-Pipeline (Story-Phase → `shot_framing` als Zwischenfeld statt `character_consistency`), die aber **an keinem aktuell erreichbaren Endpunkt hängt** — vermutlich eine frühere Iteration, bevor auf das On-Demand-pro-Szene-Muster umgestellt wurde. Nicht verwirren lassen: `make_t2v_prompt()` ist der tatsächlich genutzte Pfad.

### 7.3 Skript-, Titel- und Thumbnail-Generierung

Drei weitere, unabhängige LLM-Aufrufe, die nichts mit der Szenen-Pipeline zu tun haben:

- **`generate_script()`** (379) — "Simplicissimus-Stil" Dokumentar-Skript aus Rohmaterial (Transkript/Notizen). System-Prompt `SCRIPT_SYSTEM` (357) definiert ein festes 6-Schritte-Schema (Hook → Build-up → Escalation → Broader Pattern → Human Cost → Closing) und Stilregeln (kurze/lange Satzwechsel, 150 WPM, 8-14 Kapitel).
- **`generate_titles()`** (425) — 5 Titel-Optionen nach CTR-Formeln (`TITLE_SYSTEM`, 405): Curiosity Gap, Zahlen-basiert, Loss-Aversion, 55-60 Zeichen, keine erfundenen Behauptungen.
- **`make_thumbnail_prompt()`** (477) — EIN Bild-Prompt fürs Thumbnail, andere Regeln als Storyboard-Szenen (`THUMBNAIL_PROMPT_SYSTEM`, 455): ein dominantes Motiv, starker Kontrast, übertriebener Ausdruck, Rule of Thirds — bewusst "am extremsten gestylte Frame des ganzen Videos, nicht ein typischer Frame".

### 7.4 Charakter-Referenzen — zwei unterschiedliche Konzepte, nicht verwechseln

1. **`char_ref_url.txt`** (kanal-weit, `get_channel_char_ref()`, 71) — EIN Bild, das für Veo `REFERENCE_2_VIDEO` und als `image_input`/`image_urls` bei jeder Bildgenerierung mitgeschickt wird, um das Charakterdesign visuell zu verankern. Wird über `/api/gen_char_ref` (2701) aus dem Master-Prompt generiert oder manuell hochgeladen.
2. **`charsheets/<name>.json`** (`load_char_refs()`, 1087) — benannte Charaktere mit **Text**-Designbeschreibung (`visual_description`), die in `_build_image_prompt()` als zusätzlicher Text-Hinweis eingefügt werden ("CHARACTER DESIGN for 'Max': ..."). Kein Bild-Input an KIE, nur Text-Kontext.

Beide werden unabhängig voneinander genutzt und können auch beide gleichzeitig aktiv sein.

## 8. Bild-Modell-Auswahl (nano-banana-2 vs. -lite)

Pro **Video**, nicht pro Kanal (`get_video_image_model()`/`set_video_image_model()`, dashboard.py:91/98 — gespeichert in `meta.json` desselben Videos, das auch Titel/Thumbnail hält). UI-Dropdown sitzt in der Toolbar über der Szenenliste im Editor (dashboard.html:391 ff.), lädt/speichert bei `openVideo()` bzw. `saveImageModel()`.

## 9. Frontend-Architektur (`dashboard.html`)

### 9.1 Zwei Haupt-Views

- **`#view-videolist`** — drei Tabs: 🎬 Videos (Grid aller Videos im Kanal), 🎨 Stil-Einstellungen (Master-Prompts, Charakter-Referenzen — kanalweit), ✍️ Skript-Generator.
- **`#view-editor`** — der eigentliche Storyboard-Editor für EIN Video: Modus-Toggle, Titel&Thumbnail-Karte, Einstellungen, Audio-Upload, manuelles Skript-Feld, Szenenliste.

Umschalten über `openVideo()` / `backToVideoList()` / `showVideoListView()`.

### 9.2 Globaler State (Top of `<script>`, dashboard.html:411)

```js
SCENES         // aktuell geladene Szenen-Liste (Spiegel von plan.json)
ACTIVE_CHANNEL, CHANNEL_NAME, ACTIVE_VIDEO   // welcher Kanal/Video gerade offen ist
VIDEOS         // Video-Liste im aktiven Kanal
CURRENT_MODE   // 'image' | 'video'
VIDEO_META     // Titel/Thumbnail-Status
CHARS          // Charakter-Referenzen-Liste
```

### 9.3 API-Helper-Pattern

```js
ch_api(url, body)  // POST, hängt automatisch {channel: ACTIVE_CHANNEL, video: ACTIVE_VIDEO} an
ch_get(url)        // GET mit denselben Query-Params
api(url, method, body)  // roher fetch-Wrapper ohne automatisches Channel/Video
```
Fast der gesamte Code nutzt `ch_api`/`ch_get` — `api()` direkt wird nur für kanal-übergreifende Dinge (z.B. `/api/channels`) oder Spezialfälle mit anderem Video-Parameter genutzt.

### 9.4 Polling-Pattern (wiederholt sich viermal, gleiche Grundidee)

Jede lange Aktion hat: **Start-Request** (nur "starte den Job") + **Poll-Loop** (fragt Status alle 2-4s ab, aktualisiert UI, stoppt sich selbst wenn fertig):

| Aktion | Start | Poll-Funktion | Intervall |
|---|---|---|---|
| Plan erstellen | `makePlan()` → `/api/plan` | `startPlanPoll()` (875) | 2s |
| Alle Bilder generieren | `genAll()` → `/api/generate_all_start` | `startBatchPoll()` (759) | 3s |
| Einzelnes Bild | `genOne()` → `/api/generate_one` | Inline-Loop in `genOne()` selbst (1107) | 2s, 130 Versuche |
| Transkription | `transcribeAudio()` → `/api/transcribe` | `startStatusPoll()` (937) | 1.2s |

Wichtig: `openVideo()` (571) prüft beim Öffnen eines Videos, ob im Hintergrund noch ein Plan- oder Batch-Job läuft (Server hat den Zustand, nicht der Browser) und **nimmt den Poll automatisch wieder auf** — das ist, was Reload-Sicherheit für den Nutzer tatsächlich bedeutet: nicht "der Job überlebt", sondern "die Anzeige findet den laufenden Job wieder".

### 9.5 Szenen-Rendering — Sync mit dem Server ohne komplettes Re-Render

`_applyFreshScene(fresh)` (677) ist die zentrale Stelle, die einzelne DOM-Elemente gezielt aktualisiert (Bild-Tag ersetzen, Status-Badge ändern), statt bei jedem Poll `renderScenes()` komplett neu zu bauen (würde Bild-Requests unnötig wiederholen, Formularinhalte in Video-Prompt-Textareas verlieren). Wird von drei Stellen genutzt: `_refreshScenesFromPlan()`, `_watchRunningScenes()`, `startBatchPoll()`.

## 10. HTTP-Routing — vollständige Tabelle

Alle Routen sind flache `if p == "/api/...":`-Blöcke in `do_GET`/`do_POST` (keine Router-Bibliothek). `cid`/`vid` werden am Anfang jeder Methode aus Query-Params (GET) bzw. JSON-Body (POST) gelesen.

**GET** (dashboard.py:2028 ff.):
| Route | Zweck |
|---|---|
| `/` | liefert `dashboard.html` |
| `/api/channels`, `/api/videos` | Listen |
| `/api/char_ref`, `/api/image_model` | aktuelle Werte lesen |
| `/api/get_mode`, `/api/master`, `/api/vid_master` | Kanal/Video-Konfiguration |
| `/api/plan` | aktuelles `plan.json` |
| `/api/plan_status`, `/api/generate_all_status`, `/api/transcribe_status`, `/api/job_status`, `/api/render_status` | Job-Polling |
| `/api/video_meta` | Titel/Thumbnail-Status |
| `/api/download` | ZIP aller Bilder |
| `/generated/<file>`, `/charsheets/<file>` | Datei-Ausgabe (Bilder/Videos) |
| `/api/charsheets` | Charakter-Liste |

**POST** (dashboard.py:2116 ff.):
| Route | Zweck |
|---|---|
| `/api/videos`, `/videos/delete`, `/videos/rename` | Video-CRUD |
| `/api/channels`, `/channels/delete`, `/channels/rename` | Kanal-CRUD |
| `/api/master`, `/api/vid_master`, `/api/image_model`, `/api/set_mode` | Konfiguration setzen |
| `/api/generate_script` | Skript-Generator |
| `/api/generate_titles`, `/api/select_title`, `/api/generate_thumbnail` | Titel/Thumbnail |
| `/api/plan`, `/api/plan_status_reset` | Plan-Erstellung starten/zurücksetzen |
| `/api/preview_t2v_prompt`, `/api/generate_t2v` | Veo-Video pro Szene |
| `/api/upload_audio`, `/api/transcribe` | Audio-Upload + Transkription |
| `/api/upload_charref`, `/api/gen_charsheet` | Charakter-Referenzen |
| `/api/generate_all_start`, `/api/generate_all_stop`, `/api/generate_one` | Bild-Generierung |
| `/api/generate_video` | I2V-Animation (Bild-Modus, Grok) |
| `/api/set_char_ref`, `/api/gen_char_ref` | Kanal-Charakter-Referenzbild |
| `/api/render_start`, `/api/render_stop` | Auto-Rendering starten/stoppen (Abschnitt 13) |

## 11. Bekannte Stolperfallen / Dinge, an die man sich erinnern sollte

- **Zwei tote Code-Pfade**: `video_prompts_batch()`/`_video_prompt_chunk()` (1018/918) und `gen_t2v()`/`T2V_MODEL` (1758/1756) sind vollständig implementiert, aber an keinem HTTP-Endpunkt angeschlossen. Der tatsächlich genutzte Video-Pfad ist `make_t2v_prompt()` + `gen_veo()`/`extend_veo()`.
- **`_migrate_legacy_video()`** (189) läuft bei **jedem Start** (`init_channels()`, Zeile 247, Modulebene — nicht nur beim allerersten Mal) und prüft für jeden Kanal, ob eine Migration vom alten Ein-Video-pro-Kanal-Layout nötig ist. Harmlos im Normalbetrieb (early-return sobald `videos.json` existiert), aber falls mal ein Kanal-Ordner von Hand angelegt wird, kann das überraschende Effekte haben.
- **Server-Neustart-Regel**: vor jedem `pkill`/Neustart von `dashboard.py` erst `/api/generate_all_status` und `/api/plan_status` für das gerade aktive Video prüfen — ein laufender Batch-/Plan-Job wird beim harten Kill nicht sauber beendet, sondern verwaist (Szenen bleiben auf `läuft` hängen).
- **`dashboard.html`-Änderungen brauchen keinen Neustart**, `dashboard.py`-Änderungen schon (neuer Python-Prozess).

## 12. Feature A — Bild-Sequenzen (Doppel-Anker-Referenzierung)

Löst ein konkretes Problem: Passagen, die mehrere Sekunden denselben Ort/dasselbe Motiv behandeln, wurden bisher als unabhängige Einzelbilder generiert — jedes mit eigenem Zufalls-Ergebnis, keine visuelle Kontinuität untereinander. Sequenzen lösen das, indem Bild 0 einer Gruppe als Anker gilt und jedes Folgebild sowohl den Anker als auch sein unmittelbares Vorgängerbild als Referenz mitbekommt.

**Zwei unterschiedliche Beat-Räume — wichtig, nicht zu verwechseln:**
- **Manueller Skript-Pfad** (`_plan_generate_worker`, ruft `analyze_script(units)` auf **rohen** Einheiten auf, bevor `segment_by_pacing()` sie zu Szenen gruppiert/splittet) — Beat-Index ≠ finaler Szenen-Index.
- **Audio-Transkriptions-Pfad** (`/api/transcribe`, ruft `analyze_script([s["text"] for s in scenes])` auf **bereits fertig segmentierten** Szenen auf) — Beat-Index = Szenen-Index, 1:1.

Deshalb zwei unterschiedliche Zuordnungs-Wege:
- **`segment_by_pacing(units, pacing, wpm, sec, sequences)`** (563) — bekommt `sequences` zusätzlich zu `pacing`, trägt `seq_id` durch die Gruppierung/Splittung: ein Sequenz-Wechsel erzwingt eine Szenen-Grenze (genau wie ein Pacing-Label-Wechsel es schon tut). `seq_pos` wird NACH der Gruppierung 0,1,2... neu vergeben (`_renumber_seq_pos()`), nicht der LLM-Rohwert übernommen, da eine "calm"-Gruppe mehrere Units zu einer Szene zusammenfassen oder eine "punchy"-Unit in zwei Szenen splitten kann.
- **`_apply_visual_sequences_direct(scenes, sequences)`** (audio-Pfad) — direkte Index-Zuweisung, da hier keine Gruppierung stattfindet.

**`analyze_script()`** liefert dafür zusätzlich `"visual_sequences": [{"seq_id", "beats": [...], "reason", "camera"}]` — Regel im Prompt: nur gruppieren, wenn ≥2 aufeinanderfolgende Einheiten denselben Ort/dasselbe Motiv fortlaufend behandeln, im Zweifel keine Sequenz.

**`_resolve_chain_refs(plan_path, scene)`** (in `_batch_generate_worker`) liefert die Referenz-URLs für eine Fortsetzungs-Szene: Anker (`seq_pos=0`) + unmittelbarer Vorgänger, dedupliziert. **Wichtiger Nebeneffekt der 8-fachen Nebenläufigkeit** (Abschnitt 6.2): eine Fortsetzungs-Szene kann im selben Batch-Fenster wie ihr Anker landen, bevor der fertig ist — `_wait_for_chain_scene()` pollt deshalb `plan.json`, bis Anker/Vorgänger ein `source_url` haben (oder ein Timeout/Fehlerstatus greift), statt naiv anzunehmen, sequenzielle Reihenfolge sei durch die alte Batch-Architektur garantiert (die gibt es seit dieser Session nicht mehr).

**Bedingte Charakter-Referenz**: `char_ref_url` wird nur angehängt, wenn `scene["concrete_entity"]` auf einen `char_*`-Eintrag aus `analysis["characters"]` zeigt (nicht mehr blind bei jeder Szene) — `concrete_entity` wird dafür von `visual_prompts()` jetzt zusätzlich zurückgegeben und persistiert (vorher berechnet, aber nach der Validierung verworfen). Sichtbar nachvollziehbar über `scene["char_ref_applied"]` in `plan.json` + eine Log-Zeile pro Szene.

**Continuity-Prompt** (nur für `seq_pos >= 1`, in `_build_image_prompt`-Aufrufstelle in `_batch_generate_worker`): ausschließlich positive Constraints ("MUST perfectly match..."), keine Verneinungen — negierte Anweisungen werden von instruktionsbefolgenden Bildmodellen schwächer gewichtet und teils als Fokus fehlinterpretiert.

**Frontend**: `renderScenes()` (dashboard.html) markiert Szenen mit `seq_id != null` mit farbigem linken Rand + Badge `⛓ Seq N · Pos` (Farbe aus `SEQ_COLORS`, indiziert über `seq_id % 3`), und zeigt `kein Charakter-Ref` als Hinweistext, wenn `char_ref_applied === false`.

## 13. Feature B — Auto-Rendering (reines FFmpeg)

Nimmt die im Bild-Modus generierten Standbilder + das hochgeladene Voiceover und baut daraus automatisch ein fertiges `final.mp4` — Ken-Burns-Bewegung, harte Schnitte, durchgehende Audiospur. Kein MoviePy/Remotion/Node — ausschließlich `subprocess.run(["ffmpeg", ...])`, wie es `_veo_job_worker` schon fürs Audio-Mixing tut. Bewegtbild-Erzeugung (Veo/Grok) bleibt komplett unangetastet, dieser Renderer arbeitet nur auf bereits fertigen Bildern.

**`_render_worker(cid, vid)`** orchestriert sequenziell (ein FFmpeg-Prozess gleichzeitig): `prepare → motion → clips → assemble → audio → review`, Status in `RENDER_JOBS` (Abschnitt 6.1), Fortschritt via `/api/render_status`-Polling.

**Sync-Invariante — zwei Schritte, keine Alternative** (`_apply_sync_invariant()`):
1. Lineare Sekunden-Normierung: `dur`-Werte so skaliert, dass ihre Summe der echten Audiolänge (`ffprobe`) entspricht.
2. Integer-Frame-Rundung: wandelt die normierten Sekunden in exakte Frame-Zahlen um, letzte Szene absorbiert die Rundungsdifferenz — `sum(frames) == round(audio_duration*fps)` **exakt**. Das verhindert die Bug-Klasse aus MoneyPrinterTurbo Issue #985 (ein bloßer Float-Vergleich `video_duration >= audio_duration` bricht bei minimalen FFmpeg-Rundungsabweichungen zu früh ab) — kein Code danach vergleicht je wieder Floats, nur noch diese Ganzzahl-Frames gelten als Wahrheit.

**`_motion_for_scene(scene, prev_scene)`** — regelbasiert, kein LLM-Call: sehr kurze Szenen (<1.5s) bleiben fast statisch, Sequenz-Fortsetzungen übernehmen dieselbe Zoom-Richtung wie die vorige Szene der Sequenz (wirkt wie eine durchlaufende Kamerafahrt), sonst alternierend zoom_in/zoom_out nach Index. Intensität skaliert mit der Szenendauer.

**`_render_clip(img_path, scene, out_path)`** — Ken-Burns-Clip pro Szene: Supersampling (`scale=3840:-2`) gegen `zoompan`-Ruckeln, Smoothstep-Easing (`3t²-2t³`, aus `on`/`frames` gebaut, NICHT aus `zoompan`s interner `zoom`-Variable — die würde Rundungsdrift akkumulieren) behebt den mechanischen "Roboter-Kamera"-Look bei quasi keinen Zusatzkosten. **Resume-sicher**: überspringt komplett, wenn `out_path` schon existiert und nicht leer ist (identisches Muster zu `_batch_generate_worker`s `todo`-Liste) — ein abgebrochener Render macht beim erneuten Start nur die fehlenden Clips neu.

**`_probe_video_encoder()`** — einmalig gecachter Check, ob `h264_videotoolbox` (Apple-Silicon-Hardware-Encoder, ~4x schneller als `libx264`, belastet die CPU nur leicht — wichtig, da der Python-Server nebenher läuft) verfügbar ist; sonst Fallback auf `libx264 -preset medium -crf 20`. Braucht eine explizite Qualitätsangabe (`-q:v 65`), sonst liefert der Hardware-Encoder sichtbar weichere Bilder.

**`_assemble_clips()`** — concat-Demuxer, nur harte Schnitte (V1-Entscheidung, Crossfades wären ein voller Re-Encode). **`_mux_audio()`** — finaler Mux mit `-af apad=pad_dur=0.3` (Sicherheitspuffer ZUSÄTZLICH zur Sync-Invariante, nicht als Ersatz) und `-movflags +faststart` (MP4-Metadaten an den Anfang, sonst startet die `<video>`-Vorschau erst nach Komplett-Download). **`_render_selfcheck()`** — `ffprobe`-Checks (Dauer, Audiospur vorhanden, Datei nicht leer) nach dem Render, gleiche Philosophie wie `_validate_image_prompt_entry`, nur eine Ebene höher.

**Datenverzeichnis**: `videos/<vid>/render_tmp/` (`v_render_tmp()`), bewusst getrennt von `generated/` — wird nach erfolgreichem Render + bestandener Selbstprüfung per `shutil.rmtree()` gelöscht, darf deshalb niemals derselbe Pfad wie `generated/` sein.

**Frontend**: eigene Karte "🎬 Video zusammenschneiden" **unterhalb** der Szenenliste (nicht in der ohnehin vollen "Alle generieren"-Toolbar) — Rendern ist ein nachgelagerter Schritt, der erst Sinn ergibt, wenn Bilder existieren. Button disabled mit Hinweistext, bis mindestens ein Bild fertig ist (`updateRenderCardVisibility()`). Mehrstufiger Fortschritt über dieselbe `.steps`-Komponente wie der Audio-Upload-Flow (6 Stufen statt 3). `openVideo()` nimmt einen laufenden Render-Poll nach Reload automatisch wieder auf, identisches Muster zu Plan-/Batch-Jobs.

**End-to-End verifiziert** (Juli 2026): 2-Szenen-Testvideo mit echtem generierten Bild + synthetischer Audiospur über die echte HTTP-API gerendert — `final.mp4` mit exakter Audiodauer-Übereinstimmung, beide Selbstprüfungs-Checks grün, `render_tmp/` korrekt aufgeräumt, Original-Bilder unangetastet, Sequenz-Konsistenz (gleiche Zoom-Richtung für Anker+Fortsetzung) bestätigt.

## 14. Phase 2.5 — Sound-Design-Layer (Musikbett + SFX)

Legt sich als zusätzlicher Schritt VOR `_mux_audio()` in die `"audio"`-Stage von `_render_worker` (Abschnitt 13): statt des rohen Voiceovers wird ein gedücktes Musikbett + regelbasiert platzierte SFX gemuxt, wenn die nötigen Asset-Dateien vorhanden sind — sonst fällt der Renderer automatisch auf reines Voiceover zurück (Phase-2-Verhalten). Kein LLM-Call, kein neues pip-Paket, nur FFmpeg-Filter (`sidechaincompress`, `adelay`, `amix`, `loudnorm`).

**`_build_final_audio(voice_path, scenes, render_dir)`** — der einzige Aufrufpunkt aus `_render_worker`. Prüft zuerst, ob `assets/music/neutral_bed.mp3` existiert; fehlt sie, wird sofort (mit Log-Hinweis) der unveränderte `voice_path` zurückgegeben — **kein Absturz bei fehlenden Assets**, gleiche Resilienz-Philosophie wie überall sonst im Code (fehlende optionale Daten degradieren, statt den ganzen Vorgang scheitern zu lassen). Bei einem Fehler mitten in der Ducking/SFX-Kette (z.B. defekte SFX-Datei) greift derselbe Fallback.

**`_duck_music_under_voice(voice_path, music_path, out_path)`** — Musikbett per `sidechaincompress` unter die Stimme geduckt (Lautstärke sinkt automatisch, wenn die Stimme da ist, steigt in Sprechpausen wieder). `-stream_loop -1` auf dem Musik-Input loopt das (typischerweise viel kürzere) Bett-File für die komplette Videolänge; `amix=duration=first` schneidet danach exakt auf die Länge der Stimme zurecht.

**`_build_sfx_events(scenes)`** — regelbasiert, kein LLM-Call:
- **`whoosh`** an jeder Szene, die Anker (`seq_pos == 0`) einer Sequenz ist UND deren unmittelbarer Vorgänger einer anderen (oder keiner) Sequenz angehört — ein echter Szenen-/Sequenzwechsel, nicht nur die erste Szene im Video.
- **`riser`** an jeder Szene mit `pacing == "punchy"` — die verfügbare Näherung für "stärkste emotional_arc-Wechsel" aus dem Plan, da dieses Codebase-Datenmodell keine expliziten Kapitelgrenzen kennt. Voraussetzung: `pacing` wird jetzt auch auf dem finalen Szenen-Objekt persistiert (`segment_by_pacing()`/`_renumber_seq_pos()`-Nachbarschaft bzw. direkt im Audio-Pfad — vorher wurde das Label nur intern zur Gruppierung verwendet und verworfen).

**`_place_sfx(narration_path, sfx_events, out_path)`** — legt die SFX-Dateien per `adelay` an die jeweilige `scene["start"]`-Zeit (in ms), mischt alles mit `amix` zusammen und normalisiert die Gesamtlautheit mit `loudnorm`. Eine fehlende einzelne SFX-Datei wird übersprungen, nicht der ganze Render abgebrochen. Ohne jegliche Ereignisse wird nur `loudnorm` auf die Musik+Stimme-Mischung angewendet.

**Asset-Ablage** (`SOUND_ASSETS_DIR = assets/`, projektweit, nicht pro Kanal — Sounds sind stiluniversell):
```
assets/
  music/neutral_bed.mp3
  sfx/whoosh_01.wav, impact_01.wav, riser_01.wav
  CREDITS.txt   -- Herkunft/Lizenz pro Datei, Pflichtfeld für echte (nicht-Platzhalter) Assets
```
**Wichtig, Stand Juli 2026**: die aktuell dort liegenden Dateien sind **synthetische Platzhalter** (per `ffmpeg lavfi` erzeugt: `sine`/`anoisesrc`), nur um die Pipeline testbar zu machen — keine echten lizenzierten Sounds. `CREDITS.txt` dokumentiert das explizit und enthält die Vorlage für echte Einträge (Pixabay/Freesound-CC0/Mixkit), sobald der Nutzer reale Assets einpflegt. Bis dahin klingt jeder Render mit den Platzhaltern entsprechend synthetisch — das ist erwartet, kein Bug.

**End-to-End verifiziert** (Juli 2026): 3-Szenen-Testvideo (calm → Sequenz-Anker → punchy) über die echte HTTP-API mit synthetischer Voiceover-Datei gerendert — Log bestätigt "Musikbett gedückt + 2 SFX-Ereignisse platziert" (exakt 1× `whoosh` am Sequenz-Wechsel, 1× `riser` an der punchy-Szene, wie von der Regel erwartet), `final.mp4` mit Video- UND Audiospur, exakte Ziel-Dauer.

## 15. Phase 4 (Teil 1) — Crossfade-Übergänge an Sequenzgrenzen

Bewusst **eng geschnittener** erster Teil von Phase 4: Crossfades gibt es **nur** an echten Sequenz-/Szenenwechseln (identische Bedingung wie das `whoosh`-SFX-Ereignis aus Abschnitt 14) — jeder andere Schnitt im Video bleibt ein harter Schnitt über den verlustfreien `concat`-Demuxer. Entscheidung des Nutzers: kein pauschales "überall überblenden", sondern gezielt an den Stellen, die ohnehin schon als dramaturgisch bedeutsam markiert sind.

**`_has_transition_before(scenes, idx)`** — exakt dieselbe Regel wie `_build_sfx_events`s Whoosh-Bedingung (`seq_pos==0` UND Vorgänger gehört zu anderer/keiner Sequenz). Bewusst identisch gehalten: Bild-Übergang und Whoosh-Sound müssen auf demselben Schnitt sitzen, nicht zwei unabhängig berechnete Zeitpunkte sein, die zufällig auseinanderlaufen könnten.

**Das Sync-Invarianten-Problem bei Crossfades und seine Lösung:** Ein Crossfade überlappt zwei Clips zeitlich (Gesamtdauer = `dauer_a + dauer_b - crossfade_dauer`), was die von `_apply_sync_invariant()` (Abschnitt 13) exakt berechnete Bild-Ton-Synchronität sonst um genau diese Überlappung verkürzen würde. Lösung: **Kompensation vor dem Rendern**, nicht danach — die Szene UNMITTELBAR VOR einem Übergang bekommt zusätzliche `round(TRANSITION_DURATION_SEC * fps)` Frames an ihre `_frames` addiert, bevor `_render_clip()` sie rendert (mehr Ken-Burns-Bewegung derselben Szene, nicht mehr Bildinhalt). Der Crossfade "verbraucht" exakt diese Zusatz-Frames beim Überlappen, sodass die gemergte Clip-Dauer wieder exakt der ursprünglichen, unkompensierten Summe beider Plansdauern entspricht — **verifiziert**: zwei Clips mit geplant 3.0s/2.0s ergeben nach Kompensation+Crossfade exakt 5.0s gemergte Dauer, nicht 4.5s.

**`_crossfade_clips(clip_a, clip_b, out_path, duration)`** — nimmt zwei bereits fertig gerenderte Clips, ermittelt `clip_a`s tatsächliche Dauer per `ffprobe` (`_clip_duration_sec`), berechnet den `xfade`-Offset (`dauer_a - duration`) und rendert den Übergang (`transition=fade`, reine Überblendung — keine Wipe/Slide-Varianten, das war die bewusste Scope-Entscheidung) mit demselben Encoder wie die Einzel-Clips.

**Verkettung im `_render_worker`**: nach dem Rendern aller Einzel-Clips werden sie in einer Schleife zu `merged_paths` zusammengeführt — trifft ein Index auf einen Übergangspunkt, wird **das letzte Element** von `merged_paths` (das selbst schon ein Merge-Ergebnis eines vorherigen Übergangs sein kann) mit dem aktuellen Clip verschmolzen und ersetzt. Das behandelt auch unmittelbar aufeinanderfolgende Übergänge korrekt, ohne auf einen bereits verbrauchten Clip-Pfad zu verweisen.

**Neue Fortschritts-Stufe** `"transitions"` zwischen `"clips"` und `"assemble"` in `RENDER_JOBS`/`RENDER_STAGE_ORDER` (dashboard.html) — zeigt bei mehreren Übergängen ebenfalls einen Fortschrittsbalken (`done`/`total`), analog zur `"clips"`-Stufe.

**End-to-End verifiziert** (Juli 2026): 4-Szenen-Testvideo (normal → Sequenz-1-Anker → Sequenz-1-Fortsetzung → Sequenz-2-Anker/punchy) mit 2 erwarteten Übergangspunkten über die echte HTTP-API gerendert — `final.mp4` exakt 8.0s (identisch zur synthetischen Voiceover-Länge; ohne korrekte Kompensation wäre es 7.0s gewesen, 2×0.5s kürzer), Video- und Audiospur vorhanden, `render_tmp/` korrekt aufgeräumt.
