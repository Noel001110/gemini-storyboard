# Storyboard Generator — Architektur-Überblick

Stand: Juli 2026. Referenz-Dokument, damit man nach längerer Pause wieder reinfindet, ohne den ganzen Code neu lesen zu müssen. Alle Zeilenangaben beziehen sich auf den aktuellen Stand von `dashboard.py` / `dashboard.html` — bei größeren Änderungen können sie sich verschieben, die Funktionsnamen bleiben der verlässlichere Anker.

## 1. Was das Programm macht

Lokales Tool (läuft auf `localhost:8765`), das aus einem Sprecher-Skript automatisch ein komplettes Storyboard baut: Skript → Szenen mit Timing → Bild-Prompts (oder Video-Prompts) → generierte Bilder/Videos über KIE.ai → optional Titel + Thumbnail für YouTube. Ein **Kanal** = ein visueller Stil/Charakter (z.B. "Ink Explainer"). Jeder Kanal kann beliebig viele **Videos** enthalten, jedes Video hat sein eigenes Skript/Szenen-Plan.

## 2. Tech-Stack — bewusst minimal

- **Backend**: `dashboard.py`, ein einziges File, nur Python-Stdlib (`http.server.ThreadingHTTPServer`, kein Flask/FastAPI). ~3745 Zeilen (Stand nach Aufräumen toter Pfade, Juli 2026 — wächst mit jeder Phase, Zeilenangabe hier bewusst nur eine grobe Orientierung, kein exakter Wert zum Nachhalten).
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
   ▼ split_units()                              [570]
Atomare Sätze/Teilsätze
   │
   ▼ analyze_script(units)                       [771]   ── EIN LLM-Call, liest das GANZE Skript
Analyse: locations, characters, recurring_symbols, emotional_arc,
callbacks, pacing (pro Einheit), visual_sequences, callouts
   │
   ▼ segment_by_pacing(units, pacing, wpm, sec, sequences, callouts) [591]
Szenen mit variabler Dauer (calm bis 6s, punchy <1.5s), seq_id/seq_reason/callout durchgereicht
   │
   ▼ visual_prompts(scenes, analysis)            [952]   ── einziger Prompt-Pfad, Bild-Modus wie Video-Modus
Bild-Prompt pro Szene (chunked, validiert, retry)
   │
   ▼ _build_image_prompt(prompt, master, char_refs) [1080]
Voller Prompt = Szenen-Text + Charakter-Beschreibungen + Master-Stil
   │
   ▼ _kie_submit_image() → poll → download        [1125]
Fertiges Bild in generated/NNN.jpg, plan.json aktualisiert
```

Für Videos (T2V/Veo) ist der Pfad separat und **on-demand pro Szene**, nicht Teil der Plan-Erstellung — siehe Abschnitt 7. (Eine früher parallel existierende, eigene Video-Prompt-Batch-Pipeline für den Video-Modus — `video_prompts_batch()`/`_video_prompt_chunk()` — war nie an einen HTTP-Endpunkt angeschlossen und wurde beim Aufräumen toter Pfade [Juli 2026] komplett entfernt, siehe Abschnitt 11.)

## 5. Die zwei Betriebsmodi (`mode`: `"image"` | `"video"`)

Pro Video (`videos.json` → `mode`-Feld) einstellbar, im Frontend über den Segmented-Control oben im Editor (`setMode()`, dashboard.html:930).

- **Bild-Modus** (Standard): Skript → Szenen → **Bilder** (nano-banana-2). Optional pro Szene per I2V ("Animieren"-Button) zu einem kurzen Grok-Video animiert (`gen_video()`, dashboard.py:2649 — **nicht** Veo, sondern `grok-imagine/image-to-video`).
- **Video-Modus** (T2V): Skript → Szenen → **direkt Videos**, kein Bild-Zwischenschritt, über **Veo 3.1** (`gen_veo`/`extend_veo`, dashboard.py:2533/2564, verdrahtet über `/api/generate_t2v`, dashboard.py:3251), inkl. Chain-Extend (Abschnitt 7.2). Ein früher zusätzlich vorhandener, nie verdrahteter zweiter Pfad über Grok T2V (`gen_t2v`, Modell `grok-imagine/text-to-video`) wurde beim Aufräumen toter Pfade (Juli 2026) entfernt, siehe Abschnitt 11.

`renderScenes()` im Frontend (dashboard.html:1131) rendert je nach `CURRENT_MODE` komplett unterschiedliches HTML pro Szene (zwei Spalten Bild+Video im Bild-Modus vs. eine Video-Spalte + Prompt-Textarea im Video-Modus).

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
| `PRODUCE_JOBS` | Status des Ein-Knopf-Orchestrators (Plan→Bilder→Rendern verkettet, Abschnitt 17) pro Video | `_PRODUCE_JOBS_LOCK` |
| `VOICE_JOBS` | ElevenLabs-spezifischer Status (Phase 1, §23) pro Video — führt ElevenLabs-Task, Settings, Resume-Marker; Polling-Kanal für `/api/voiceover_status` | `_VOICE_JOBS_LOCK` |

**Wichtiges Muster, das sich wiederholt**: Alle langlaufenden Aktionen (Plan erstellen, alle Bilder generieren, Transkription, Rendern, der Ein-Knopf-Orchestrator, ElevenLabs-Generierung in §23) laufen als **Server-seitiger Background-Thread**, nicht als eine einzige lange HTTP-Anfrage. Grund (mehrfach live aufgetreten): eine blockierende HTTP-Anfrage stirbt, wenn der Tab geschlossen/neu geladen wird — das Frontend denkt dann "nichts passiert" und der Nutzer klickt erneut, was einen zweiten, komplett unabhängigen LLM-Lauf auf demselben Skript startet (doppelte Kosten). Die Lösung überall gleich: Endpunkt startet nur einen `threading.Thread(daemon=True)`, merkt sich `running=True` **atomisch mit der "läuft schon?"-Prüfung** (nicht danach!), und das Frontend pollt einen `_status`-Endpunkt. `PRODUCE_JOBS` (Abschnitt 17) ist dabei kein neues Muster, sondern derselbe Mechanismus nochmal — der Orchestrator ruft die drei anderen Worker-Funktionen nur nacheinander im selben Thread auf, statt jeweils einen eigenen Thread zu spawnen.

### 6.2 Nebenläufigkeit bei der Bildgenerierung

- `IMAGE_GEN_SEMAPHORE = threading.Semaphore(8)` (dashboard.py:44, Kapazität aus `MAX_CONCURRENT_IMAGE_GENS`) — globales Limit, wie viele KIE-Bild-Tasks gleichzeitig laufen dürfen, unabhängig davon ob sie vom Batch-Worker oder einem einzelnen Klick kommen.
- `_batch_generate_worker()` (dashboard.py:1311) nutzt `concurrent.futures.ThreadPoolExecutor(max_workers=8)`, um bis zu 8 Szenen parallel zu bearbeiten (`process_scene()`, verschachtelte Funktion darin).
- `_kie_rate_limit_wait()` (dashboard.py:1113) — zusätzlicher Schutz: max. 12 Submits pro 10 Sekunden prozessweit (KIEs echtes Limit: 20/10s), damit die 8 parallelen Worker nicht gleichzeitig eine Burst-Rate-Limit-Fehlermeldung auslösen. Bei "call frequency too high" wird automatisch mit Backoff wiederholt (`_kie_submit_image`, dashboard.py:1161 ff.), bei "insufficient credits" wird der **ganze Batch sofort gestoppt** (nicht jede Szene einzeln durchprobiert).
- `_PLAN_WRITE_LOCK` (dashboard.py:35) — schützt jedes Lesen-Ändern-Schreiben von `plan.json`. **Historischer Bug**: ohne diesen Lock konnten zwei Szenen, die fast gleichzeitig fertig wurden, sich gegenseitig überschreiben (Thread B liest eine Momentaufnahme, bevor Thread A geschrieben hat → Thread B's Schreibvorgang macht A's Update rückgängig). Das erklärte zufällig "verschwindende" fertige Bilder.

### 6.3 KIE.ai-Anbindung — drei verschiedene API-Formate

| Funktion | Endpunkt | Zweck |
|---|---|---|
| `post_kie_text()` (298) | `/gemini-2.5-flash/v1/chat/completions` | OpenAI-kompatibel, für Transkription + Charakter-Bildanalyse |
| `post_gemini_native()` (317) | `/gemini/v1/models/{model}:generateContent` | Natives Gemini-Format (contents/parts), nutzt `gemini-3-5-flash` mit `thinkingLevel: high` — verhindert "faules"/generisches Verhalten bei späteren Items in einem Batch. **Wird für fast alle Analyse-/Prompt-Generierungs-Calls genutzt** (analyze_script, Bild-/Video-Prompts, Titel, Thumbnail-Prompt, Skript-Generator). |
| `_kie_submit_image()` (1125) | `/api/v1/jobs/createTask`, Modell `nano-banana-2`/`-lite` | Bildgenerierung |
| `gen_veo()`/`extend_veo()` (2533/2564) | `/api/v1/veo/generate`, `/extend` | Veo 3.1 Videos |
| `gen_video()` (2649) | `/api/v1/jobs/createTask`, Modell `grok-imagine/image-to-video` | Bild→Video-Animation im Bild-Modus |

**Wichtiger, einmal live gefundener Bug**: `nano-banana-2` erwartet Referenzbilder im Feld `image_input`, `nano-banana-2-lite` im Feld `image_urls` — das falsche Feld wird von KIE stillschweigend akzeptiert (HTTP 200), hat aber **keinerlei Effekt**. Siehe `_kie_submit_image()`, dashboard.py:1143 (`ref_field = "image_input" if model == "nano-banana-2" else "image_urls"`).

## 7. Die Prompt-Pipeline im Detail (Kernstück)

### 7.1 Bild-Modus: Skript → fertiger Bild-Prompt

**Schritt 1 — `split_units(text)`** (570): zerlegt in Sätze (Regex `[^.!?]+[.!?]?`), Sätze >22 Wörter werden zusätzlich an Kommas/Semikola gesplittet. Reiner Text-Preprocessing-Schritt, kein LLM.

**Schritt 2 — `analyze_script(units)`** (771): **Ein** LLM-Call (Gemini 3.5, `json_mode=True`) über das **gesamte** Skript. Liefert:
- `locations`, `characters` (mit `visual_description` + `anonymize`-Flag für echte, identifizierbare Personen — die werden später nie beim Namen genannt/realistisch gezeigt, nur als Silhouette/Symbol),
- `recurring_symbols` + `callbacks` (damit wiederkehrende visuelle Elemente konsistent bleiben),
- `emotional_arc` (Opening/Midpoint/Resolution als je ein Wort),
- `pacing`, pro Einheit ein Label `calm`/`normal`/`punchy`, explizit im selben Call wie der `emotional_arc` bestimmt, damit das Pacing nicht unabhängig vom Spannungsbogen "driftet" (siehe Prompt-Text dashboard.py:805 — die Einstufung soll die Position im Bogen berücksichtigen, nicht nur die Satzformulierung isoliert),
- `visual_sequences` (Feature A, Abschnitt 12) und `callouts` (Phase 4.4, Abschnitt 18.3) — beide additiv später hinzugekommen, gleicher Call, kein Mehraufwand.

Dieses `analysis`-Dict wird **überall weitergereicht** — an die Segmentierung (`pacing`, `visual_sequences`, `callouts`), an die Bild-Prompt-Chunks, an die Anonymisierungs-Prüfung. Es wird pro Plan-Erstellung nur **einmal** berechnet (`visual_prompts()` überspringt einen zweiten `analyze_script()`-Call, wenn `analysis` schon übergeben wurde).

**Schritt 3 — `segment_by_pacing(units, pacing, wpm, sec, sequences, callouts)`** (591): gruppiert die Einheiten zu Szenen. `calm` darf bis `MAX_SCENE_SEC=6.0s` halten, `punchy` wird auf ~1.1s komprimiert (bei langen Sätzen sogar in zwei Bilder gesplittet für den "Gut-Punch"-Effekt), `normal` folgt dem Nutzer-Wert (`sec`-Feld im Frontend). Harter Deckel bei 6s wird **immer** durchgesetzt, auch wenn eine einzelne Einheit für sich schon zu lang ist — dieselbe Bug-Klasse wie einst bei der alten festen `segment()`, hier neu gefixt. Sicherheitsnetz: warnt im Log, wenn >30% als "punchy" eingestuft werden (`PACING_WARN_THRESHOLD`). Trägt zusätzlich `seq_id`/`seq_reason` (Feature A/Kapitel-Titel) und `callout` durch Merge/Split hindurch — siehe Abschnitt 12 und 18.3 für die Details dieses Trackings.

**Schritt 4 — `visual_prompts(scenes, analysis)`** (952): generiert den eigentlichen Bild-Prompt-Text pro Szene. Läuft **gechunkt** (`IMAGE_PROMPT_CHUNK_SIZE=20` Szenen pro LLM-Call — Grund: die Analyse+Few-Shot-Beispiele werden bei jedem Chunk-Call komplett mitgeschickt, größere Chunks = weniger Wiederholung = günstiger). Jeder Chunk-Call (`_image_prompt_chunk()`, 880) zwingt das Modell zu Zwischenfeldern, bevor der finale Prompt geschrieben wird:
```
scene → core_statement → concrete_entity → callback_check → character_consistency → image_prompt
```
Das verhindert vage, generische Prompts ("dark ominous scene") — das Modell muss zuerst explizit benennen, WAS die Zeile eigentlich behauptet und WELCHE konkrete Entität aus der Analyse gemeint ist, bevor es den Bildtext schreibt. `_validate_image_prompt_entry()` (868) prüft danach: mindestens `IMAGE_PROMPT_MIN_LEN=220` Zeichen, und die genannte `concrete_entity` muss tatsächlich im Prompt-Text vorkommen (außer bei anonymisierten Personen — dort wäre das ja gerade falsch). Bei Fehlschlag: `_image_prompt_single_retry()` (940), ein fokussierter Einzel-Call nur für diese eine Szene.

**Fehlerresistenz beim Chunking**: `_fetch_image_chunk()` (976, verschachtelt in `visual_prompts()`) — wenn ein Chunk-Call fehlschlägt (z.B. abgeschnittenes JSON bei großen Antworten), wird der Chunk **halbiert und beide Hälften einzeln neu versucht**, rekursiv, statt gleich zum generischen Fallback-Text zu greifen. Ein Timeout kostet so nur die halbe Chunk-Größe, nicht den ganzen Chunk.

**Schritt 5 — `_build_image_prompt(scene_prompt, master, char_refs)`** (1080): baut den **finalen** an KIE gesendeten Text zusammen: `Szenen-Prompt + Charakter-Design-Hinweise (aus charsheets/) + Master-Prompt (Stil/Farben/Linienführung)`. Der Master-Prompt wird hier **wörtlich angehängt**, nicht nur dem LLM als Kontext gegeben — die Stil-Durchsetzung passiert also durch reine String-Konkatenation direkt vor dem Absenden, nicht durch "Vertrauen" ins Sprachmodell.

### 7.2 Video-Modus: T2V über Veo 3.1

Anders als Bilder wird der Video-Prompt **nicht** beim Plan-Erstellen fertig generiert, sondern **on-demand pro Szene**, wenn der Nutzer auf "Generieren" klickt (`/api/generate_t2v`, dashboard.py:3251 → `make_t2v_prompt()`, 2469). Grund vermutlich: Video-Generierung ist teuer/langsam, man will nicht 170 Video-Prompts vorab bezahlen, wenn nur ein paar Szenen wirklich als Video gebraucht werden.

`make_t2v_prompt()` bekommt: den Szenentext, die Story-Phase (`OPENING`/`RISING ACTION`/`CLIMAX`/`RESOLUTION`, berechnet aus der Position im Skript — `story_phase()`, 1013), die letzten 2 vorherigen Video-Prompts (für visuelle Kontinuität), und das **volle Skript** als Kontext (damit z.B. Eigennamen korrekt erkannt werden). Ergebnis muss ≥`VIDEO_PROMPT_MIN_LEN=280` Zeichen sein und vier Dinge explizit benennen: Hauptmotiv, Setting, Licht-Stimmung, Kamera-Winkel.

**Chain-Extend** (dashboard.py:3290 ff.): Wenn die vorherige Szene in derselben Story-Phase ist UND ihre Extend-Kette noch nicht zu lang ist (`MAX_CHAIN_LENGTH=4`, Zeile 2531), wird `extend_veo()` (2564) statt `gen_veo()` (2533) genutzt — das **setzt das letzte Frame des vorherigen Videos fort**, echte Bild-zu-Bild-Kontinuität statt nur gleicher Stil. Sonst wird ein frischer Anker-Shot via `REFERENCE_2_VIDEO` (mit dem Channel-Charakter-Referenzbild) oder `TEXT_2_VIDEO` generiert.

### 7.3 Skript-, Titel- und Thumbnail-Generierung

Drei weitere, unabhängige LLM-Aufrufe, die nichts mit der Szenen-Pipeline zu tun haben:

- **`generate_script()`** (407) — "Simplicissimus-Stil" Dokumentar-Skript aus Rohmaterial (Transkript/Notizen). System-Prompt `SCRIPT_SYSTEM` (385) definiert ein festes 6-Schritte-Schema (Hook → Build-up → Escalation → Broader Pattern → Human Cost → Closing) und Stilregeln (kurze/lange Satzwechsel, 150 WPM, 8-14 Kapitel).
- **`generate_titles()`** (453) — 5 Titel-Optionen nach CTR-Formeln (`TITLE_SYSTEM`, 433): Curiosity Gap, Zahlen-basiert, Loss-Aversion, 55-60 Zeichen, keine erfundenen Behauptungen.
- **`make_thumbnail_prompt()`** (505) — EIN Bild-Prompt fürs Thumbnail, andere Regeln als Storyboard-Szenen (`THUMBNAIL_PROMPT_SYSTEM`, 483): ein dominantes Motiv, starker Kontrast, übertriebener Ausdruck, Rule of Thirds — bewusst "am extremsten gestylte Frame des ganzen Videos, nicht ein typischer Frame".

### 7.4 Charakter-Referenzen — zwei unterschiedliche Konzepte, nicht verwechseln

1. **`char_ref_url.txt`** (kanal-weit, `get_channel_char_ref()`, 79) — EIN Bild, das für Veo `REFERENCE_2_VIDEO` und als `image_input`/`image_urls` bei jeder Bildgenerierung mitgeschickt wird, um das Charakterdesign visuell zu verankern. Wird über `/api/gen_char_ref` (dashboard.py:3684) aus dem Master-Prompt generiert oder manuell hochgeladen.
2. **`charsheets/<name>.json`** (`load_char_refs()`, 1025) — benannte Charaktere mit **Text**-Designbeschreibung (`visual_description`), die in `_build_image_prompt()` als zusätzlicher Text-Hinweis eingefügt werden ("CHARACTER DESIGN for 'Max': ..."). Kein Bild-Input an KIE, nur Text-Kontext.

Beide werden unabhängig voneinander genutzt und können auch beide gleichzeitig aktiv sein.

## 8. Bild-Modell-Auswahl (nano-banana-2 vs. -lite)

Pro **Video**, nicht pro Kanal (`get_video_image_model()`/`set_video_image_model()`, dashboard.py:103/110 — gespeichert in `meta.json` desselben Videos, das auch Titel/Thumbnail und die Text-Overlay-Toggles hält, Abschnitt 18.5). UI-Dropdown sitzt in der Toolbar über der Szenenliste im Editor, lädt/speichert bei `openVideo()` bzw. `saveImageModel()`.

## 9. Frontend-Architektur (`dashboard.html`)

### 9.1 Zwei Haupt-Views

- **`#view-videolist`** — drei Tabs: 🎬 Videos (Grid aller Videos im Kanal), 🎨 Stil-Einstellungen (Master-Prompts, Charakter-Referenzen — kanalweit), ✍️ Skript-Generator.
- **`#view-editor`** — der eigentliche Storyboard-Editor für EIN Video, seit der UI-Neuordnung (Juli 2026) als klar nummerierter, gated Workflow statt loser Kartensammlung.

Umschalten über `openVideo()` / `backToVideoList()` / `showVideoListView()`.

#### 9.1.1 Schritt-für-Schritt-Reihenfolge im Editor

Der Editor war ursprünglich eine flache Kartensammlung (Titel&Thumbnail und Einstellungen standen ganz oben, noch bevor überhaupt ein Skript existierte — irreführend, weil diese Karten inhaltlich *nachgelagerte* Schritte sind). Neu strukturiert in fünf sichtbar nummerierte Schritte, jeder Folgeschritt erst sichtbar/aktiv, wenn sein Vorgänger einen Zustand geliefert hat, der ihn sinnvoll macht:

| Schritt | Karte | Sichtbarkeits-/Freischalt-Bedingung |
|---|---|---|
| ① Modus | Modus-Toggle (`image`/`video`) | immer sichtbar |
| ② Skript/Voice-Over | Ziel-Länge-Einstellung (`cardSettings`, je nach Modus `settingsImg`/`settingsVid`) + Audio-Upload (Option A, empfohlen) + manuelles Skript-Feld (Option B, geschätztes Timing) | immer sichtbar |
| ③ Bilder generieren | `planArea` (Szenenliste, Toolbar, Batch-Status) | `display:none` bis ein Plan existiert |
| ④ Titel & Thumbnail | `titleThumbCard` | `display:none` bis `SCENES.length > 0`; `genTitlesBtn`/`genThumbBtn` zusätzlich `disabled` |
| ⑤ Video rendern | `renderCard` | `display:none` bis Bilder existieren (bestehendes `updateRenderCardVisibility()`) |

Zwei parallele Sichtbarkeits-Funktionen mit identischem Muster:
- `updateTitleThumbCardVisibility()` — steuert Schritt ④, aufgerufen aus `renderScenes()` und aus dem Completion-Zweig von `startBatchPoll()`.
- `updateRenderCardVisibility()` — steuert Schritt ⑤ (bereits aus Feature B, unverändert wiederverwendet).

`openVideo()` setzt beim Öffnen eines Videos **beide** Karten explizit auf `display:none`, bevor der eigentliche Lade-Code läuft (zusätzlich zum bestehenden Reset von `planArea`) — sonst blieben `titleThumbCard`/`renderCard` sichtbar-stale, wenn man von einem Video mit Szenen zu einem leeren Video wechselt (in dieser Runde selbst gefunden und behoben, kein vom Nutzer gemeldeter Bug).

**Bewusst entfernt aus der alten Oben-Position:** Die „Wörter/Sekunde"-Einstellung stand vorher prominent über allem, obwohl sie durch die Pacing-Analyse (Abschnitt 7) für die meisten Fälle nur noch ein Richtwert ist, kein hartes Timing-Element mehr. Sie lebt jetzt platzsparend innerhalb von Schritt ②, direkt am Ort, wo Skript/Audio eingegeben werden — nicht mehr als eigene Karte davor.

### 9.2 Globaler State (Top of `<script>`, dashboard.html:486 ff.)

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

### 9.4 Polling-Pattern (wiederholt sich sechsmal, gleiche Grundidee)

Jede lange Aktion hat: **Start-Request** (nur "starte den Job") + **Poll-Loop** (fragt Status alle 1-4s ab, aktualisiert UI, stoppt sich selbst wenn fertig):

| Aktion | Start | Poll-Funktion | Intervall |
|---|---|---|---|
| Plan erstellen | `makePlan()` (994) → `/api/plan` | `startPlanPoll()` (1002) | 2s |
| Alle Bilder generieren | `genAll()` (1469) → `/api/generate_all_start` | `startBatchPoll()` (865) | 3s |
| Einzelnes Bild | `genOne()` (1245) → `/api/generate_one` | Inline-Loop in `genOne()` selbst | 2s, 130 Versuche |
| Transkription | `transcribeAudio()` (1082) → `/api/transcribe` | `startStatusPoll()` (1065) | 1.2s |
| Rendern | `renderVideo()` (1534) → `/api/render_start` | `startRenderPoll()` (1547) | 2s |
| Ein-Knopf-Orchestrator | `produceAll()` (1616) → `/api/produce_start` | `startProducePoll()` (1644, Abschnitt 17.3) | 2.5s |

Wichtig: `openVideo()` (646) prüft beim Öffnen eines Videos, ob im Hintergrund noch einer dieser sechs Jobs läuft (Server hat den Zustand, nicht der Browser) und **nimmt den Poll automatisch wieder auf** — das ist, was Reload-Sicherheit für den Nutzer tatsächlich bedeutet: nicht "der Job überlebt", sondern "die Anzeige findet den laufenden Job wieder".

### 9.5 Szenen-Rendering — Sync mit dem Server ohne komplettes Re-Render

`_applyFreshScene(fresh)` (783) ist die zentrale Stelle, die einzelne DOM-Elemente gezielt aktualisiert (Bild-Tag ersetzen, Status-Badge ändern), statt bei jedem Poll `renderScenes()` komplett neu zu bauen (würde Bild-Requests unnötig wiederholen, Formularinhalte in Video-Prompt-Textareas verlieren). Wird von drei Stellen genutzt: `_refreshScenesFromPlan()` (805), `_watchRunningScenes()` (817), `startBatchPoll()` (865).

## 10. HTTP-Routing — vollständige Tabelle

Alle Routen sind flache `if p == "/api/...":`-Blöcke in `do_GET`/`do_POST` (keine Router-Bibliothek). `cid`/`vid` werden am Anfang jeder Methode aus Query-Params (GET) bzw. JSON-Body (POST) gelesen.

**GET** (dashboard.py:2957 ff.):
| Route | Zweck |
|---|---|
| `/` | liefert `dashboard.html` |
| `/api/channels`, `/api/videos` | Listen |
| `/api/char_ref`, `/api/image_model` | aktuelle Werte lesen |
| `/api/get_mode`, `/api/master`, `/api/vid_master` | Kanal/Video-Konfiguration |
| `/api/plan` | aktuelles `plan.json` |
| `/api/plan_status`, `/api/generate_all_status`, `/api/transcribe_status`, `/api/job_status`, `/api/render_status`, `/api/produce_status` | Job-Polling |
| `/api/overlay_opts` | Text-Overlay-Toggles lesen (Abschnitt 18.5) |
| `/api/video_meta` | Titel/Thumbnail-Status |
| `/api/download` | ZIP aller Bilder |
| `/generated/<file>`, `/charsheets/<file>` | Datei-Ausgabe (Bilder/Videos) |
| `/api/charsheets` | Charakter-Liste |

**POST** (dashboard.py:3055 ff.):
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
| `/api/produce_start`, `/api/produce_stop` | Ein-Knopf-Orchestrator starten/stoppen (Abschnitt 17) |
| `/api/overlay_opts` | Text-Overlay-Toggles speichern (Abschnitt 18.5) |

## 11. Bekannte Stolperfallen / Dinge, an die man sich erinnern sollte

- **Toter Code aufgeräumt (Juli 2026)**: eine externe Architektur-Bewertung (auf Basis dieses Dokuments) stieß auf zwei bereits hier dokumentierte tote Pfade — Anlass für einen systematischen Scan (jede Top-Level-Funktion in `dashboard.py` darauf geprüft, ob sie irgendwo tatsächlich mit `(` aufgerufen wird, nicht nur in einem Kommentar erwähnt). Ergebnis: **vier** tote Funktionsgruppen, nicht zwei — `charsheet_path()` (griff zudem auf eine nirgends definierte Konstante `CHARSHEET_DIR` zu, hätte bei einem Aufruf sofort einen `NameError` geworfen) und `poll_kie_video()` waren bisher undokumentiert. Alle vier vollständig entfernt: `video_prompts_batch()`/`_video_prompt_chunk()`/`_video_prompt_single_retry()`/`_validate_video_prompt_entry()` (eigene Video-Prompt-Pipeline, nie an einen Endpunkt angeschlossen — der tatsächlich genutzte Pfad ist `make_t2v_prompt()`, Abschnitt 7.2), `gen_t2v()`/`T2V_MODEL` (unverdrahteter zweiter Video-Pfad über Grok, tatsächlich genutzt wird `gen_veo()`/`extend_veo()`), `charsheet_path()`, `poll_kie_video()` (veralteter KIE-Polling-Helfer, ersetzt durch inline Polling in `_veo_job_worker`). `dashboard.py` dadurch von 3995 auf 3744 Zeilen geschrumpft. **Lehre für künftige Sessions**: nach größeren Feature-Wellen diesen Scan wiederholen (Einzeiler, siehe Session-Notizen) statt Dead Code sich anzusammeln zu lassen — die Funktionsnamen-Ähnlichkeit zu aktivem Code (`gen_t2v` vs. `/api/generate_t2v`, `video_prompts_batch` vs. `visual_prompts`) macht manuelles Erkennen beim Lesen überraschend unzuverlässig.
- **`_migrate_legacy_video()`** (217) läuft bei **jedem Start** (`init_channels()`, Zeile 267, Modulebene — nicht nur beim allerersten Mal) und prüft für jeden Kanal, ob eine Migration vom alten Ein-Video-pro-Kanal-Layout nötig ist. Harmlos im Normalbetrieb (early-return sobald `videos.json` existiert), aber falls mal ein Kanal-Ordner von Hand angelegt wird, kann das überraschende Effekte haben.
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

**`_crossfade_clips(clip_a, clip_b, out_path, duration, transition_type="fade")`** — nimmt zwei bereits fertig gerenderte Clips, ermittelt `clip_a`s tatsächliche Dauer per `ffprobe` (`_clip_duration_sec`), berechnet den `xfade`-Offset (`dauer_a - duration`) und rendert den Übergang mit demselben Encoder wie die Einzel-Clips. In Teil 1 war `transition_type` fest `"fade"` (reine Überblendung, bewusste Scope-Entscheidung für den ersten Wurf) — seit Teil 2 (Abschnitt 15.1) variabel, siehe dort.

**Verkettung im `_render_worker`**: nach dem Rendern aller Einzel-Clips werden sie in einer Schleife zu `merged_paths` zusammengeführt — trifft ein Index auf einen Übergangspunkt, wird **das letzte Element** von `merged_paths` (das selbst schon ein Merge-Ergebnis eines vorherigen Übergangs sein kann) mit dem aktuellen Clip verschmolzen und ersetzt. Das behandelt auch unmittelbar aufeinanderfolgende Übergänge korrekt, ohne auf einen bereits verbrauchten Clip-Pfad zu verweisen.

**Neue Fortschritts-Stufe** `"transitions"` zwischen `"clips"` und `"assemble"` in `RENDER_JOBS`/`RENDER_STAGE_ORDER` (dashboard.html) — zeigt bei mehreren Übergängen ebenfalls einen Fortschrittsbalken (`done`/`total`), analog zur `"clips"`-Stufe.

**End-to-End verifiziert** (Juli 2026): 4-Szenen-Testvideo (normal → Sequenz-1-Anker → Sequenz-1-Fortsetzung → Sequenz-2-Anker/punchy) mit 2 erwarteten Übergangspunkten über die echte HTTP-API gerendert — `final.mp4` exakt 8.0s (identisch zur synthetischen Voiceover-Länge; ohne korrekte Kompensation wäre es 7.0s gewesen, 2×0.5s kürzer), Video- und Audiospur vorhanden, `render_tmp/` korrekt aufgeräumt.

## 15.1 Phase 4 (Teil 2) — Übergangs-Bibliothek, gekoppelte SFX, frame-genaue Impact-Akzente

Nutzerwunsch: "richtig professioneller Schnitt" statt immer derselben Überblendung, plus passende Sounds pro Übergang — und explizit eine **Bibliothek**, auf die man zurückgreifen kann, statt Einzel-Hacks. Der eigentliche Fund dabei: **die Bibliothek existiert bereits** — ffmpegs `xfade`-Filter bringt von Haus aus 58 fertige Übergangstypen mit (`ffmpeg -h filter=xfade`), Teil 1 nutzte davon nur einen (`fade`). Kein neues Paket, keine eigene Formel — nur eine Auswahlregel, die den vorhandenen Typenkatalog tatsächlich ausnutzt.

**`TRANSITION_LIBRARY`** (dashboard.py, nahe `TRANSITION_DURATION_SEC`) — drei kuratierte Familien statt aller 58 Typen (Auswahl nach Stilgefühl fürs Ink/Stickman-Format, nicht alle 58 wirken professionell):
| Familie | ffmpeg-Typen (Richtung alterniert) | SFX |
|---|---|---|
| `fade` | `fade`, `dissolve` | keins — ein Whoosh würde eine ruhige `calm`-Szene stören |
| `wipe` | `wipeleft`, `wiperight` | `whoosh` — energischer, "harter" Look |
| `smooth` | `smoothleft`, `smoothright` | `whoosh` — moderner Standardfall, unauffälliger als ein Wipe |

**`_transition_for_scene(scene, idx)`** — regelbasiert, kein LLM-Call, kein Zufall (Zufall würde einen Resume-Render nach Reload optisch anders aussehen lassen als den ursprünglichen Lauf): Familie folgt dem bereits vorhandenen `pacing`-Feld der Szene (`calm`→`fade`, `punchy`→`wipe`, sonst `smooth`), Richtung (links/rechts) alterniert deterministisch über `scene["i"] % 2` — dasselbe Muster wie die Zoom-Richtung in `_motion_for_scene`. Gibt `(transition_type, sfx_or_None)` zurück.

**Bild und Ton sind durch dieselbe Funktion gekoppelt, nicht zwei unabhängige Regeln:** `_render_worker`s Merge-Schleife ruft `_transition_for_scene()` für den Video-Crossfade auf, `_build_sfx_events()` ruft **dieselbe Funktion** für das begleitende SFX auf. Eine `fade`-Familie liefert dadurch garantiert keinen Whoosh (statt vorher immer einen), ein `wipe`/`smooth`-Übergang garantiert einen. Der gewählte `transition_type` wird zusätzlich auf der Szene persistiert (`scene["transition_type"]`, sichtbar in `plan.json`) — dasselbe Debug-Sichtbarkeits-Prinzip wie `char_ref_applied` aus Feature A.

**Phase 4.2 — Frame-genauer Impact-Akzent auf harten Schnitten:** `_build_sfx_events()` erweitert um ein drittes Ereignis, das erst durch Phase 3 sinnvoll wurde. Eine `punchy`-Szene, die **kein** Übergangspunkt ist (also ein harter Schnitt bleibt, keine Überblendung), bekommt zusätzlich zum bestehenden `riser`-Ereignis ein `impact`-Ereignis exakt auf `start_aligned` (Whisper-Wortgrenze statt Schätzung) — ein scharfer, perkussiver Treffer genau auf dem Schnitt. Punchy-Szenen, die GLEICHZEITIG Übergangspunkte sind, bekommen bewusst KEIN Impact (der weiche Video-Crossfade + Whoosh würde mit einem harten Perkussions-Treffer kollidieren). Nebenbei: das `impact`-Sound-Asset (`SFX_FILES["impact"]`) existierte bereits seit Phase 2.5, wurde aber nie tatsächlich ausgelöst — jetzt hat es seine Funktion.

**End-to-End verifiziert** (Juli 2026): 6-Szenen-Testvideo (`calm`→`normal`→`punchy` innerhalb Sequenz 1, dann drei je eigene Sequenzen mit `punchy`/`normal`/`calm`) über die echte HTTP-API gerendert. `plan.json` zeigt korrekt `wiperight` (Sequenzwechsel auf punchy-Szene), `smoothleft` (Sequenzwechsel auf normal-Szene), `dissolve` (Sequenzwechsel auf calm-Szene) — drei verschiedene Übergangstypen im selben Video statt immer `fade`. Server-Log bestätigt "5 SFX-Ereignisse platziert", exakt die erwartete Kombination (Impact+Riser auf dem harten punchy-Schnitt, Whoosh+Riser auf dem punchy-Übergang, Whoosh auf dem normal-Übergang, nichts auf dem calm-Übergang). `final.mp4`: exakt 12.0s (= Sync-Invariante hält trotz variabler Übergangstypen), Video- und Audiospur vorhanden.

## 16. Phase 3 — Frame-genaues Timing: Pivot von ElevenLabs Scribe zu lokalem `faster-whisper`

Der externe `IMPLEMENTATION_PLAN.md` sah für Phase 3 ursprünglich „ElevenLabs Scribe über KIE" vor (`transcribe_words_scribe`). Diese Quelle wurde **verworfen, bevor auch nur eine Zeile Code dafür geschrieben wurde** — reine Recherche/Live-Test-Phase, kein Rollback nötig.

### 16.1 Warum ElevenLabs Scribe (über KIE) ausscheidet

Zwei von KIE-Marktplatz-Docs zitierte Modelle wurden zuerst geprüft und als grundlegend falsche Richtung erkannt:
- `elevenlabs/text-to-dialogue-v3` — **Text-zu-Sprache**, generiert neues Audio aus Text. Falsche Richtung: es soll ein bestehendes Voiceover transkribiert werden, nicht neues erzeugt.
- `elevenlabs/text-to-speech-multilingual-v2` — hat zwar einen `timestamps`-Parameter, aber nur für selbst generierte TTS-Ausgabe, nicht für eine hochgeladene Datei. Hätte das echte Nutzer-Voiceover durch KI-Sprache ersetzt — ein produktzerstörender Fehler, wäre er implementiert worden.

Der tatsächlich passende Modellname (`elevenlabs/speech-to-text`, über Web-Suche mit zwei unabhängigen Quellen als `audio_url`/`language_code`/`tag_audio_events`/`diarize`-Parametersatz bestätigt) wurde **live getestet**: deutsche TTS-Testdatei erzeugt (macOS `say`), hochgeladen, per KIE-API submitted — gültige `taskId`, `code:200`, aber der Task blieb über 10+ Minuten permanent im Status `"waiting"` hängen, ohne je Fortschritt zu zeigen, bei bereits verbrauchten `0.12` Credits. **KIE-Modell-Listings sind keine Garantie für tatsächliche Funktionsfähigkeit** — dieses Modell ist über KIE praktisch nicht nutzbar (Stand Juli 2026). Kein weiteres Polling/Credits-Verbrauchen, sofort abgebrochen und Testdateien aufgeräumt.

### 16.2 Warum nicht Gemini als Alternative

Gemini generiert im Rahmen dieses Projekts bereits den gesamten Text-Content (Skript-Analyse, Titel, etc.) und wäre naheliegend gewesen. Verworfen, weil Gemini bei **präzisen Timestamps** unzuverlässig ist — es liefert einen guten Transkript-Text, „weiß" aber nicht zuverlässig, an welcher exakten Sekunde/Millisekunde ein bestimmtes Wort gesprochen wurde (Halluzinationsrisiko genau bei der Information, auf die Phase 3 angewiesen ist).

### 16.3 Entscheidung: lokales `faster-whisper`

- Dedizierte ASR-Engine (Automatic Speech Recognition), kein Text-Generierungsmodell — löst genau das Problem, für das Scribe gedacht war.
- **Lokal, nicht API-basiert**: kein Rate-Limit, keine laufenden Kosten, kein Hänge-Risiko wie bei KIE, funktioniert offline.
- Ressourcenbedarf gering und **kein Dauerlast**: ~500 MB einmaliger Modell-Download ("small"), ~1 GB RAM während der kurzen Transkriptions-Burst-Phase pro Video, danach wieder frei.
- `word_timestamps=True` liefert exakt das, was Phase 3 braucht: Wort-für-Wort-Zeitstempel statt nur Satz-/Segment-Zeitstempel.
- Nicht `openai-whisper` (die Original-Referenzimplementierung), sondern `faster-whisper` (CTranslate2-basiert) — deutlich schneller bei gleicher Modellqualität, relevant weil die Transkription synchron im bestehenden Job-Pattern laufen soll (Daemon-Thread + Status-Polling, wie überall sonst in diesem Projekt).

### 16.4 Umsetzung

**Isolierte venv statt Import ins Hauptprozess:** `faster-whisper` (und seine Abhängigkeit `ctranslate2`) wird NICHT in `dashboard.py` importiert — das würde die Zero-Framework/Stdlib-only-Regel des Hauptprozesses verletzen und ist ohnehin unnötig, da Homebrew-Python hier "externally managed" ist (kein `pip install` direkt ins System-Python ohne `--break-system-packages`, was bewusst vermieden wurde). Stattdessen: eigene venv unter `.venv_whisper/` (in `.gitignore`, maschinenspezifisch, ~464 MB inkl. Modell-Cache unter `~/.cache/huggingface/`), ein eigenständiges Skript `whisper_transcribe.py` darin, aufgerufen per `subprocess.run([...])` — exakt dasselbe Muster wie der bestehende `ffmpeg`-Aufruf. `dashboard.py` bleibt dadurch so stdlib-rein wie vorher.

- **`transcribe_words_whisper(audio_path, language=None)`** (dashboard.py, nahe `transcribe_and_segment`) — startet `whisper_transcribe.py` in der venv, Modell "small", `word_timestamps=True`, Timeout 900s. Gibt `{"text","language","language_probability","words":[{"word","start","end"}]}` zurück.
- **`align_scenes_to_whisper(scenes, whisper_words)`** — ordnet jeder Szene `start_aligned`/`end_aligned` zu. Kein Fuzzy-Text-Matching: da Gemini (`transcribe_and_segment`) und Whisper dieselbe Audiodatei in derselben Reihenfolge transkribieren, genügt sequenzielles Vorrücken um `len(scene["text"].split())` Wörter durch die Whisper-Wortliste. Toleriert einzelne ASR-Abweichungen zwischen den beiden Engines (verifiziert: Whisper hörte "Wortszeitstempel", Gemini/Referenztext "Wort-Zeitstempel" — beides ein Token, Zählung bleibt korrekt), weil nur die WortANZAHL zählt, nie der exakte Wortlaut.
- **Wiring: in `_render_worker`, nicht in `/api/transcribe`** — siehe 16.5 für die Korrektur und Begründung.
- **Renderer/SFX/Crossfade bevorzugen `*_aligned`**: `_apply_sync_invariant()` (Abschnitt 13) berechnet die Szenendauer jetzt über eine kleine `scene_dur()`-Hilfsfunktion, die `end_aligned - start_aligned` nimmt, falls vorhanden, sonst das geschätzte `dur` — die zwei-Schritte-Sync-Invariante selbst (lineare Normierung + Integer-Frame-Rundung) läuft unverändert danach. `_build_sfx_events()` (Abschnitt 14) nutzt analog `start_aligned` statt `start` für die SFX-Zeitpunkte, falls vorhanden.

### 16.5 Korrektur: Alignment gehört an den Render-Zeitpunkt, nicht an den Transkriptions-Zeitpunkt

**Vom Nutzer selbst gefunden, nicht von mir:** Die ursprüngliche Umsetzung (16.4, erste Fassung) rief `align_scenes_to_whisper` direkt im `/api/transcribe`-Handler auf — das deckt nur den Audio-Transkriptions-Pfad (Option A) ab. Der Nutzer stellte die berechtigte Frage, ob das nicht unnötig doppelt/inkonsistent ist: **`_render_worker` verlangt so oder so IMMER ein hochgeladenes Voice-over** (`v_audio`), unabhängig davon, ob die Szenen-Texte aus der Audio-Transkription oder dem manuellen Skript-Pfad (`_plan_generate_worker`, Option B, rein WPM-geschätzte Timeline) stammen. Da `align_scenes_to_whisper` in Option B nie aufgerufen wurde, blieb der manuelle Pfad **dauerhaft** auf der groben WPM-Schätzung sitzen, selbst nachdem der Nutzer später ein echtes Voice-over hochlud und rendern ließ — genau die „Mitte driftet"-Schwäche, die Phase 3 eigentlich beheben sollte, griff für Option B nie.

**Fix:** Whisper-Aufruf + Alignment aus `/api/transcribe` entfernt (Handler wieder bei 4 statt 5 `TX_STATUS`-Stufen), stattdessen in `_render_worker` verschoben — direkt nach dem Laden von `audio_duration` per `ffprobe`, als neue Stage `"timing"` (zwischen `"prepare"` und `"motion"`, auch in `RENDER_STAGE_LABELS`/`RENDER_STAGE_ORDER` in dashboard.html ergänzt). Läuft dadurch für **jeden** Render, unabhängig vom Ursprung der Szenen-Texte — ein einziger Alignment-Punkt statt eines, der nur einen von zwei Pfaden abdeckt. Resume-sicher: überspringt den Whisper-Lauf, wenn alle Szenen schon `start_aligned` aus einem vorigen Render tragen (`if any(s.get("start_aligned") is None for s in scenes)`), damit ein wiederholter Render nicht unnötig erneut transkribiert. Graceful Degradation unverändert: schlägt Whisper fehl, behalten die Szenen ihre geschätzten `start`/`dur`-Werte, der Rest des Renders läuft normal weiter.

**Erweiterung Phase 1 (ElevenLabs, §23):** Der Alignment-Pfad akzeptiert jetzt drei mögliche Quellen für Word-Timestamps, priorisiert nach `audio_meta.json["voiceover_source"]`:

| `voiceover_source` | Word-Quelle | Wer ruft auf | Sektion |
|---|---|---|---|
| `"elevenlabs"` | bereits im `audio_meta.json["voiceover_word_timestamps"]` | `elevenlabs_generate()` (Phase 1) hat sie direkt vom Provider geholt — **kein** Netzwerk-Call im `_render_worker` | §23 |
| `"user_upload"` oder fehlt | `transcribe_words_whisper()` | `_render_worker` Z. ~2318, identisch zur bisherigen Pipeline | §16.4/§16.5 |

Der Pause-Trim (`_compute_pause_trims` / `_trim_audio_pauses` / `_adjust_words_for_trims`) und das Alignment (`align_scenes_to_whisper`) laufen in beiden Fällen identisch — die Übergabe ist einheitlich `[{word, start, end}, ...]`. Die einzige Verzweigung findet **vor** `transcribe_words_whisper()` statt: ist `voiceover_word_timestamps` vorhanden, wird diese Liste direkt verwendet (mit `language="elevenlabs"`, `language_probability=1.0` als Audit-Marker im Log); sonst Whisper.

**End-to-End verifiziert** (Juli 2026), beide Pfade getrennt:
- **Option A** (Audio-Transkription): `/api/transcribe` liefert jetzt wieder reine geschätzte Szenen ohne `start_aligned` (4 Stufen, kein Whisper mehr an dieser Stelle) — korrektes Verhalten, die Ausrichtung folgt beim Rendern.
- **Option B** (manueller Skript-Pfad, hier simuliert: Szenen mit WPM-geschätztem `start`/`dur`, kein `source`-Feld, keine `start_aligned`-Felder) + echtes deutsches TTS-Voice-over hochgeladen + gerendert: Server-Log zeigt `[Whisper] 27 Wörter ausgerichtet (Sprache: de, p=0.984)`, `plan.json` zeigt danach `start_aligned`/`end_aligned` auf allen drei Szenen — und zwar spürbar abweichend von der WPM-Schätzung (Szene 0 geschätzt `0.0–4.0`, ausgerichtet `0.0–3.04`; die reale Aufnahme war insgesamt nur `9.84s` lang, nicht die geschätzten `12.0s`). Damit bekommt Option B jetzt exakt dieselbe Timing-Qualität wie Option A, sobald ein echtes Voice-over vorliegt.

## 17. Phase 4.5 — Ein-Knopf-Orchestrator

Ursprünglicher Nutzerwunsch, jetzt umgesetzt: „Skript oder Audio rein → ein Klick → fertiges Video." Kein neuer fachlicher Baustein — verkettet nur die drei bereits einzeln getesteten Jobs (Plan/Transkription → Bilder → Rendern) hintereinander in einem einzigen Hintergrund-Thread, exakt das etablierte Server-seitige Job-Muster (`PRODUCE_JOBS`/`_PRODUCE_JOBS_LOCK`, analog zu `BATCH_JOBS`/`RENDER_JOBS`/`PLAN_JOBS`).

### 17.1 Refactor als Voraussetzung: `_transcribe_generate_worker`

Vor dem Orchestrator war die Audio→Plan-Logik (Gemini-Transkription, Szenen-Bau, `analyze_script`, Bild-Prompts) als ~50 Zeilen **inline im `/api/transcribe`-HTTP-Handler** vergraben — als einzige der vier langlaufenden Aktionen NICHT als eigenständige Funktion, anders als `_plan_generate_worker`/`_batch_generate_worker`/`_render_worker`. Um sie im Orchestrator ohne Code-Duplikation wiederzuverwenden, wurde sie in `_transcribe_generate_worker(cid, vid, sec)` extrahiert; der HTTP-Handler ist jetzt ein dünner Wrapper, der nur noch Fehlerbehandlung/Response-Formatierung übernimmt.

### 17.2 `_produce_worker(cid, vid, text, wpm, sec)`

Drei Etappen, jede ruft dieselbe Worker-Funktion wie ihr eigener Einzel-Button:
1. **`"plan"`** — übersprungen, wenn `plan.json` schon Szenen enthält (Resume). Sonst: existiert ein hochgeladenes Voice-over → `_transcribe_generate_worker` (Option A); sonst nicht-leerer `text`-Parameter → `_plan_generate_worker` (Option B); sonst Fehler ("kein Voice-over, kein Skript").
2. **`"images"`** — `_batch_generate_worker(cid, vid)` direkt aufgerufen (blockierend, da `_produce_worker` selbst schon in einem eigenen Daemon-Thread läuft). Bereits generierte Szenen werden dank dessen eigenem `todo`-Filter automatisch übersprungen.
3. **`"render"`** — `_render_worker(cid, vid)` direkt aufgerufen, inklusive alles, was in den vorigen Abschnitten gebaut wurde (Whisper-Timing, Übergangs-Bibliothek, Sound-Design, Impact-Akzente).

Bricht bei Fehler in einer Etappe sofort ab (`fail(stage, msg)`), der Etappen-Name landet zusammen mit dem Fehlergrund in `PRODUCE_JOBS` — sichtbar im Frontend als „Fehlgeschlagen (Rendern): …" statt eines nichtssagenden generischen Fehlers.

**Stop-Propagation:** `_produce_worker` prüft sein eigenes `stop_requested` nur ZWISCHEN Etappen (ein Stop während einer laufenden Etappe würde sonst erst nach deren Ende greifen). `/api/produce_stop` setzt deshalb zusätzlich das `stop_requested`-Flag des GERADE aktiven Sub-Jobs (`BATCH_JOBS`/`RENDER_JOBS`), damit ein Stop-Klick auch mitten in der Bild-Generierung oder mitten im Rendern sofort wirkt.

### 17.3 Frontend: `produceCard`

Neue Karte direkt nach Schritt ② (Skript/Voice-Over), vor Schritt ③ — bewusst nicht als weiterer Punkt in der bestehenden Toolbar, sondern als eigenständiger, visuell hervorgehobener Block (`background:var(--acc-soft)`), der signalisiert: „das hier ersetzt alle folgenden Einzel-Schritte". Sichtbar nur im Bild-Modus (`CURRENT_MODE !== 'video'` — der Veo/Grok-Pfad hat eine eigene, unangetastete Logik) und erst, sobald Rohmaterial vorliegt: ein bereits bestehender Plan (`SCENES.length>0`), ein ausgewähltes Audio-File (`audioB64`), oder eingetippter Skript-Text — `updateProduceCardVisibility()`, aufgerufen von `applyMode()`, `audioSelected()`, `estimate()` und überall dort, wo auch `updateRenderCardVisibility()`/`updateTitleThumbCardVisibility()` laufen.

`produceAll()` lädt zuerst ein ggf. gewähltes, aber noch nicht hochgeladenes Audio-File hoch (derselbe Schritt wie in `transcribeAudio()`), dann `POST /api/produce_start` mit `text`/`wpm`/`sec`. `startProducePoll()` pollt `/api/produce_status` alle 2.5s, highlighted die aktuelle Etappe (`pstage-plan`/`pstage-images`/`pstage-render`, identisches `.done`/`.active`-Muster wie die Render-Karte), und ruft bei Erfolg **`refreshPlanAndStatus()`** auf — eine aus `openVideo()` extrahierte gemeinsame Funktion, die Szenen/Batch-Status/Render-Status neu vom Server lädt, exakt wie ein frischer Seitenaufruf. Reload-Sicherheit: `openVideo()` prüft `/api/produce_status` genauso wie die drei bestehenden Jobs und nimmt einen laufenden Orchestrator-Lauf nach Reload wieder auf.

### 17.4 Nebenbefund: `suggestCharsFromPlan()` erwartete das falsche Datenmodell

Beim End-to-End-Test (ein 1-Szenen-Testskript mit einer LLM-erkannten Figur) crashte `refreshPlanAndStatus()` mit `Cannot read properties of undefined (reading 'toLowerCase')`. Ursache: `analyze_script()` (dashboard.py) liefert Charaktere als `{id, name_or_role, visual_description, ...}`, aber `suggestCharsFromPlan()` (dashboard.html) griff auf `ch.name`/`ch.description` zu — ein Feld, das in dieser Datenstruktur nie existiert hat. Der Bug ist **nicht neu und nicht durch den Orchestrator verursacht**: dieselbe Funktion wird identisch aus `openVideo()` aufgerufen und hätte dort genauso gecrasht, sobald ein Plan mit nicht-leerem `characters`-Array neu geladen wird — vermutlich seit Einführung von `analyze_script`s Charakter-Erkennung unbemerkt kaputt, weil ein unbehandelter Fehler in einer async-Funktion ohne sichtbare Fehlermeldung einfach den Rest der aufrufenden Kette abbricht. **Fix:** `suggestCharsFromPlan()` liest jetzt `ch.name_or_role||ch.name` und `ch.visual_description||ch.description`, mit Kommentar zur Datenform. Behoben, weil beim Testen dieses Features gefunden — kein separater Auftrag, aber zu wichtig (bricht `openVideo()`s Reload-Resume lautlos), um es stehen zu lassen.

### 17.5 End-to-End verifiziert (Juli 2026)

Realer Testlauf über die Browser-UI (nicht nur die API direkt): Skript-Text eingetippt → „🚀 Alles auf einmal" geklickt → Karte zeigt live „Plan erstellen …" → „Bilder generieren …" mit Etappen-Hervorhebung. Ohne hochgeladenes Voice-over bricht der Lauf korrekt in der Render-Etappe ab (Plan + 1 echtes KIE-generiertes Bild bleiben erhalten, kein Datenverlust), Fehlermeldung „Fehlgeschlagen (Rendern): Kein hochgeladenes Voice-over gefunden …", UI kehrt in einen normal bedienbaren Zustand zurück (Schritt ⑤ zeigt den manuellen „Video rendern"-Button wieder aktiv). Nach nachträglichem Audio-Upload: zweiter Klick auf „Alles auf einmal" überspringt Plan+Bilder (Resume bestätigt — kein erneuter KIE-Bildaufruf, keine erneute Transkription) und rendert direkt ein echtes `final.mp4` (3.9s, Video- und Audiospur, `start_aligned`/`end_aligned` korrekt gesetzt, alle Selbstprüfungs-Checks grün) — im Browser sichtbar in der Render-Karte nach automatischem Refresh, inklusive funktionierendem Download-Button.

## 18. Phase 4.4 — Text-Overlays (Untertitel, Zahlen-Callouts, Kapitel-Titel)

Nutzerwunsch (alle drei zugleich gewählt, siehe Rückfrage vor Umsetzung): automatische Untertitel, Zahlen-/Statistik-Callouts, Kapitel-Titel. Alle drei standardmäßig AUS — der Plan markiert 4.4 explizit als optional, ein Video darf sich nie ungefragt im Look ändern.

### 18.1 Blocker: `drawtext` nicht verfügbar, PNG-Overlay statt Filter-Text

Der installierte ffmpeg-Build (Homebrew-Standardformel) hat kein `freetype`/`fontconfig` kompiliert — `ffmpeg -h filter=drawtext` meldet „Unknown filter". Die Alternative `ffmpeg-full` hätte 47 zusätzliche Abhängigkeiten bedeutet und ein Risiko für die bereits getestete Encoder-/Sync-Pipeline (andere Standard-Parameter, anderer Build) — bewusst nicht gewählt. Stattdessen: Text wird als transparentes PNG per **Pillow** gerendert (`render_overlay.py`, isolierte `.venv_whisper`-venv, dieselbe wie Whisper — jetzt mit Pillow ergänzt statt einer dritten venv), dann per ffmpegs `overlay`+`fade`-Filtern aufs Ken-Burns-Bild gelegt. Beide Filter sind in jedem Standard-ffmpeg-Build enthalten, kein Compile-Flag nötig. In der isolierten Test-Reihenfolge zuerst als eigenständiges Multi-Input-ffmpeg-Kommando verifiziert (`-loop 1 -i base.jpg -loop 1 -i overlay.png -t 3 -r 30 -filter_complex "..."`), bevor `_render_clip` angefasst wurde — exakte Frame-Anzahl/Auflösung bestätigt, bevor das Risiko für die bestehende Pipeline eingegangen wurde.

### 18.2 `render_overlay.py` — drei Stile

- **`caption`**: unten verankert, weißer Fettdruck mit schwarzem Textrand, halbtransparente Box, Zeilenumbruch via `draw.textlength`-Messung (max. 3 Zeilen, danach „…"). Zeigt `scene["text"]` für die GESAMTE Clip-Dauer.
- **`callout`**: groß, gelb, oberer Bildbereich, kein Kasten — für kurze Zahlen/Daten, ca. 1–1.5s sichtbar.
- **`chapter`**: mittig, weiß, kleiner als ein Callout, kein Kasten — kurzes Szenerie-Label, ca. 2s sichtbar bei einem Sequenz-Anker.

Alle drei nutzen `/System/Library/Fonts/Supplemental/Arial Bold.ttf` (auf diesem Mac vorhanden), Text wird base64-kodiert per `argv` übergeben (kein Shell-Escaping für beliebige Satzzeichen/Unicode nötig).

### 18.3 Datenherkunft — zwei Felder, die vorher berechnet und dann verworfen wurden

- **`seq_reason`** (Kapitel-Titel): `analyze_script()`s `visual_sequences`-Schema hatte von Anfang an ein `"reason"`-Feld (Feature A, Abschnitt 12) — wurde bisher nur für die Sequenz-Gruppierung selbst genutzt, der Text danach verworfen. Jetzt in `_apply_visual_sequences_direct` (Audio-Pfad) und `segment_by_pacing` (manueller Pfad, via neues `reason_by_sid`-Mapping) auf `scene["seq_reason"]` persistiert.
- **`callout`** (Zahlen-Callout): neues Feld `"callouts": [{"beat": N, "text": "1969"}]` im `analyze_script`-Schema — **kein zusätzlicher LLM-Call**, derselbe Analyse-Pass, der auch Pacing/Sequenzen liefert. Striktes Prompt-Wording: nur bei explizit im Text genannten konkreten Zahlen/Daten, nichts erfinden, die meisten Beats haben keinen Callout. Im manuellen Pfad durch `segment_by_pacing` mit einem eigenen `callout_by_i`/`cur_callout`-Tracking durch Merge/Split hindurchgereicht (analog zu `seq_by_i`), im Audio-Pfad direkte Beat-Index-Zuordnung wie Pacing.

### 18.4 `_render_clip` — von Single-Input zu Multi-Input-Filtergraph

`_overlay_specs_for_scene(scene, clip_dur, overlay_opts)` entscheidet pro Szene, welche Overlays (falls per Toggle aktiviert) greifen und ihr Zeitfenster: Kapitel-Titel nur bei `seq_pos==0` mit vorhandenem `seq_reason`, Callout nur bei vorhandenem `scene["callout"]`, Caption immer wenn `scene["text"]` existiert (praktisch immer). `_render_clip` selbst baut jetzt einen `-filter_complex`-Graphen mit einem `-loop 1 -i`-Input pro aktivem Overlay zusätzlich zum Basisbild — jedes Overlay-PNG bekommt `format=rgba,fade=in,fade=out` für weiches Ein-/Ausblenden, dann `overlay=enable='between(t,t0,t1)'` verkettet auf das vorherige Zwischenergebnis. Temporäre Overlay-PNGs werden nach dem Rendern (auch im Fehlerfall, `finally`-Block) wieder gelöscht.

### 18.5 Persistenz & Steuerung — pro Video, nicht pro Render-Klick

`get_video_overlay_opts`/`set_video_overlay_opts` (neu, analog zu `get_video_image_model`) speichern die drei Toggles in `meta.json`, nicht im Request-Body — dadurch liest `_render_worker` sie selbstständig, unabhängig davon, ob der Render über den manuellen „🎬 Video rendern"-Button oder den Ein-Knopf-Orchestrator (`_produce_worker` → `_render_worker`, Abschnitt 17) ausgelöst wurde, ohne dass die Optionen durch jeden Aufruf-Pfad einzeln durchgereicht werden müssten. Neue Routen `GET`/`POST /api/overlay_opts`. Frontend: drei Checkboxen in der Render-Karte (`ovCaptions`/`ovCallouts`/`ovChapters`), alle initial unchecked, `loadOverlayOpts()` beim Öffnen eines Videos, `saveOverlayOpts()` bei jeder Änderung.

### 18.6 End-to-End verifiziert (Juli 2026)

Isolierter `_render_clip`-Test zuerst (alle drei Overlays auf einer synthetischen Testszene, `overlay_opts` alle `True`): Frame bei t=0.5s zeigt Callout+Kapitel-Titel+Caption gleichzeitig korrekt positioniert, Frame bei t=2.5s zeigt Callout/Kapitel-Titel korrekt ausgeblendet, nur die Caption bleibt (erwartetes Timing). Danach vollständiger Produktions-Testlauf über die echte HTTP-API (Ein-Knopf-Orchestrator, Skript über einen Mondlandungs-Text mit explizitem Jahr „1969"): reales Gemini-Ergebnis erkannte selbstständig `callout="1969"` auf der ersten Szene UND gruppierte die ersten drei Szenen zu einer Sequenz mit `seq_reason="Durchgehende Szenerie auf der staubigen Mondoberfläche mit den Astronauten."` — beides ohne jede Sonderbehandlung im Prompt für diesen Testfall, allein aus der bestehenden Schema-Erweiterung. Fertig gerendertes `final.mp4` (18.4s, alle Selbstprüfungs-Checks grün) zeigt auf einem echten KIE-generierten Mondlandungsbild Callout, Kapitel-Titel und Untertitel gleichzeitig, gut lesbar, korrekt positioniert.

## 19. Pausen-Kürzung (auf Nutzerwunsch, nach Phase 3)

Nutzer-Beobachtung: ein 8-Minuten-Voiceover hat naturgemäß Satzpausen dazwischen — stille Momente, die im geschnittenen Video wie totes Material wirken. Lösung nutzt eine Datenquelle, die durch Phase 3 (Whisper) bereits vorliegt: die Lücke zwischen dem Ende von Wort N und dem Start von Wort N+1 IST die Sprechpause, kein separater Erkennungs-Schritt nötig. Auf Nutzer-Entscheidung: jede Pause wird auf `MAX_PAUSE_SEC = 0.3` Sekunden gekappt (nicht komplett entfernt — ein kurzer Atem-Abstand bleibt, nur die toten, langen Stellen verschwinden).

### 19.1 Drei neue Funktionen (dashboard.py, direkt nach `align_scenes_to_whisper`)

- **`_compute_pause_trims(words, max_pause=0.3)`** — findet jede Wortlücke über `max_pause` und gibt das jeweils zu entfernende ÜBERSCHUSS-Intervall zurück (nicht die ganze Pause — die ersten `max_pause` Sekunden bleiben als natürlicher Atem-Abstand). Jedes Intervall liegt garantiert vollständig innerhalb einer Stille, kann also nie ein gesprochenes Wort anschneiden.
- **`_trim_audio_pauses(audio_path, trims, out_path)`** — schneidet die Intervalle per ffmpeg `atrim`+`concat` heraus (eine Filterkette aus „keep intervals" zwischen den Trim-Punkten, verlustfrei als WAV). Isoliert getestet: 3.5s Audio (0.5s Ton, 2.5s Stille, 0.5s Ton) mit Trim `(0.8, 3.0)` ergibt exakt 1.3s — `3.5 - (3.0-0.8) = 1.3`.
- **`_adjust_words_for_trims(words, trims)`** — verschiebt jeden Wort-Zeitstempel auf die NEUE, gekürzte Zeitachse: jedes Wort verliert die kumulierte Dauer aller Trim-Intervalle, die vor ihm liegen. Da ein Trim-Intervall nie innerhalb eines Wortes liegt (nur zwischen Wörtern), ist ein einziger kumulativer Versatz pro Wort exakt für Start UND Ende.

### 19.2 Einbindung in `_render_worker`

Läuft in derselben `"timing"`-Stage wie die Whisper-Ausrichtung (Abschnitt 16.5) — logisch zusammengehörig, da das Kürzen der Audiospur die Wort-Zeitstempel verschiebt und ohne verlässliche Wortgrenzen kein sicherer Trim-Punkt existiert. Die gekürzte Datei landet als `voiceover_trimmed.wav` NEBEN dem Original in `v_uploads()` (nicht in `render_tmp/`, das nach jedem Render gelöscht wird) — ihre Existenz ist selbst der Resume-Marker: schon getrimmt + Szenen schon ausgerichtet heißt kein erneuter Whisper-Lauf bei einem Wiederholungs-Render. Alles Nachgelagerte (Sync-Invariante, Sound-Design, finaler Mux) verwendet ab dann `voiceover_trimmed.wav` statt des Original-Uploads — `audio_duration` für die Sync-Invariante wird entsprechend von der GEKÜRZTEN Datei per `ffprobe` ermittelt, nicht vom Original.

**Invalidierung bei neuem Upload:** `/api/upload_audio` löscht ein vorhandenes `voiceover_trimmed.wav` und leert `start_aligned`/`end_aligned` auf allen Szenen, sobald eine NEUE Aufnahme hochgeladen wird — sonst würde ein zweiter Render nach einer Neuaufnahme lautlos die alte, zur neuen Datei nicht mehr passende getrimmte Spur/Zeitstempel weiterverwenden.

### 19.3 Nebenbefund beim Testen: `WhisperModel()` versucht bei JEDEM Aufruf ins Netz, auch wenn das Modell längst lokal gecacht ist

Während des End-to-End-Tests blieb der Render minutenlang in der `"timing"`-Stage hängen (kein Hang im eigentlichen Sinn — der Prozess lief, aber wartete auf einen Netzwerk-Timeout). Ursache: `faster_whisper.WhisperModel()` ruft beim Initialisieren `huggingface_hub` auf, das selbst bei einem bereits lokal vorhandenen Modell (`~/.cache/huggingface/hub/models--Systran--faster-whisper-small`, siehe Abschnitt 16.3) einen Online-Check versucht — auf diesem Rechner/Netz manchmal ein 60-Sekunden-Timeout statt eines schnellen Fehlschlags, reproduzierbar zweimal hintereinander beobachtet. **Fix:** `whisper_transcribe.py` setzt jetzt `HF_HUB_OFFLINE=1`/`TRANSFORMERS_OFFLINE=1`, bevor `faster_whisper` importiert wird — der lokale Cache wird dadurch als maßgeblich behandelt, kein Netzwerk-Aufruf mehr nötig. Nicht durch die Pausen-Kürzung verursacht, aber durch deren Test aufgedeckt: dieselbe Verzögerung hätte auch die normale Whisper-Ausrichtung (Abschnitt 16) bei jedem Render treffen können, abhängig von Netzwerkbedingungen.

### 19.4 End-to-End verifiziert (Juli 2026)

Synthetisches 3-Satz-Voiceover mit zwei fest eingefügten 2-Sekunden-Stille-Abschnitten zwischen den Sätzen (11.93s Gesamtlänge) über die echte Render-API verarbeitet: Whisper erkannte beide Pausen (~1.9s effektiv, TTS-Grenzeffekte eingerechnet) klar über der 0.3s-Schwelle, `voiceover_trimmed.wav` landet bei 8.74s (-3.19s), `final.mp4` exakt 8.73s (nicht die ursprünglichen 11.93s) — alle drei Szenen zeigen lückenlos aneinander anschließende `start_aligned`/`end_aligned`-Werte statt der ursprünglichen Lücken. Selbstprüfung grün.

## 20. Motion-Vokabular + variable Übergangsdauer (auf Nutzer-Feedback nach einem echten Produktions-Render)

Nutzer lieferte ein echtes Voiceover für `sidequerst` (53s ElevenLabs-Aufnahme, "Alex - Business Book Narrator") und ließ real rendern — danach konkrete Kritik am fertigen Video: Schnitt wirkt monoton (immer nur rein-/rauszoomen), keine sichtbaren Übergänge, keine hörbare Musik/SFX, Clip-Längen wirken unnatürlich kurz/unruhig.

### 20.1 Diagnose — vier Ursachen, alle am echten Video/Plan verifiziert, nicht vermutet

1. **Zoom-Monotonie**: `_motion_for_scene` kannte bis dahin NUR `zoom_in`/`zoom_out`/`static`, praktisch immer derselbe Fokuspunkt `[0.5,0.45]`, fast dieselbe Intensität (~1.09) — bestätigt durch Auslesen aller 31 `scene["motion"]`-Werte aus dem echten Plan.
2. **Keine Übergänge**: `sidequerst`s Plan wurde gebaut, bevor die Sequenz-Erkennung existierte — alle 31 Szenen hatten `seq_id: null`. `_has_transition_before` (Abschnitt 15) feuert nur an Sequenzgrenzen — ohne `seq_id` kann das nie passieren. Kein Bug, fehlende Datengrundlage.
3. **Keine hörbare Musik/SFX**: `assets/music/neutral_bed.mp3` + `assets/sfx/*.wav` sind, wie in `CREDITS.txt` selbst dokumentiert, synthetische Sinuston/Rauschen-Platzhalter — unter der Stimme geduckt praktisch unhörbar. Zusätzlich verhinderte Punkt 2 (kein `seq_id`/`pacing`), dass Whoosh/Riser/Impact überhaupt auslösen konnten.
4. **Clip-Längen "unruhig"**: 31 Szenen waren für ~4s/Bild geplant (~124s), das echte Voiceover ist aber nur 47s lang (schneller Business-Narrator-Sprechstil) — die Sync-Invariante komprimiert korrekt auf die echte Länge, Ergebnis Ø 1.5s/Szene, mehrere unter 1s (kürzeste: 0.22s). Bei <1.2s bleibt die Kamera bewusst fast statisch (siehe `_motion_for_scene`s Kommentar) — das erzeugt bei so vielen kurzen Szenen den hektischen Rhythmus.

### 20.2 Fix 1 — Motion-Vokabular (`MOTION_LIBRARY`, dashboard.py nahe `_motion_for_scene`)

Generalisierung statt neuer Spezialfälle: **ein** Motion-Eintrag ist ein Zoom-Verlauf (`z0`→`z1`) UND ein Fokuspunkt-Verlauf (`focus0`→`focus1`), beide über dieselbe Smoothstep-Kurve interpoliert, die schon fürs Ken-Burns-Easing existierte (Abschnitt 13). Ein reiner Pan ist einfach `z0==z1` mit wanderndem Fokuspunkt — kein neuer ffmpeg-Filter, nur ein verallgemeinerter `zoompan`-Ausdruck (`x`/`y` interpolieren jetzt genau wie `z` schon immer). ~10 Einträge: `zoom_in`/`zoom_out` (bestehend), `pan_left`/`pan_right`, `tilt_up`/`tilt_down`, `dolly_in`/`dolly_out`, `diagonal_glide`, `snap_zoom_in`, `static`. Pan/Tilt/Dolly/Diagonal brauchen alle einen leichten Zoom-Puffer (>1.0) über den ganzen Verlauf, sonst liefe der Crop-Ausschnitt beim Wandern über den Bildrand hinaus.

**Auswahl** (`_motion_for_scene`, weiterhin regelbasiert, kein Zufall): nach `pacing` (heute verfügbar) — `calm`→Pan/Tilt/Dolly-out-Kandidaten, `normal`→Zoom/Dolly-in/Pan, `punchy`→Snap-Zoom/Diagonal/Static. Bereits vorbereitet für die (noch nicht gebaute) Story-Phase-Engine: `scene.get("phase")` wird zuerst geprüft, `_PHASE_MOTION_CANDIDATES` greift automatisch, sobald dieses Feld existiert, ohne Codeänderung. Intensität skaliert weiterhin mit der Szenendauer (`_build_motion(name, intensity_scale)`, skaliert Zoom- UND Fokus-Delta ums eigene Mittel — `intensity_scale=1.0` reproduziert exakt das Basis-Rezept).

**Rückwärtskompatibilität**: `_normalize_motion()` akzeptiert sowohl die alte Form (`{"type","z_end","focus"}`, aus jedem bereits gerenderten Plan) als auch die neue (`{"name","z0","z1","focus0","focus1"}`) und gibt immer Letztere zurück — alte Pläne mit bereits gesetztem `scene["motion"]` laden ohne Migration weiter (ARCHITECTURE §11-Regel).

**Isoliert getestet** vor der Integration: Testbild + `pan_left`-Rezept gerendert, Start-/End-Frame verglichen — ein fester Bildpunkt wandert sichtbar von rechts nach links im Frame (Kamera schwenkt links, Inhalt driftet rechts — genau die reale Schwenk-Physik), keine Skalierungsänderung erkennbar.

### 20.3 Fix 2 — variable Übergangsdauer (`TRANSITION_LIBRARY`, Vorstufe zur Phase-Engine)

`TRANSITION_DURATION_SEC` war eine globale Konstante (0.5s für jeden Übergang). Jetzt pro Familie: `fade`→0.8s ("linger", ruhige Szene darf sich Zeit lassen), `wipe`→0.3s ("snappy", ein 0.8s-Wipe wirkt behäbig), `smooth`→0.5s (unverändert). `_transition_for_scene()` gibt jetzt `(transition_type, sfx, duration)` zurück statt nur zwei Werte — beide Aufrufstellen (Kompensations-Berechnung VOR dem Clip-Rendern, tatsächlicher `_crossfade_clips()`-Aufruf im Merge-Loop) nutzen dieselbe Funktion für dieselbe Entscheidung, damit Kompensation und tatsächlicher Crossfade nie auseinanderlaufen können.

### 20.4 Fix 3 — `sidequerst` nachträglich analysiert (kein Code, ein einmaliger Daten-Fix)

Statt eines neuen Features: `analyze_script()` einmalig auf den 31 bereits bestehenden, bereits mit Bildern verknüpften Szenen-Texten erneut aufgerufen (dieselbe Funktion, die auch beim ersten Plan-Erstellen läuft) — die 31 Szenen sind dabei die "Beats" für diesen Analyse-Durchlauf, also direkte Index-Zuordnung wie beim Audio-Pfad (`_apply_visual_sequences_direct`). Ergebnis: 3 `visual_sequences`, 3 `callouts` (`"2013"`, `"$9B"`, `"2003"`), `pacing` auf allen 31 Szenen — alles nachträglich in `plan.json` geschrieben, OHNE die Bilder oder die Szenen-Struktur anzufassen. `motion`/`clip_file`/`transition_type` explizit gelöscht, damit der nächste Render sie mit der neuen, pacing-bewussten Engine frisch berechnet; `start_aligned`/`end_aligned` (Whisper+Pausen-Kürzung) blieben unangetastet, da unabhängig von dieser Analyse gültig.

### 20.5 End-to-End verifiziert (Juli 2026) — echtes Produktions-Video, nicht nur Testdaten

Nach beiden Fixes + Nachanalyse erneut über die echte Render-API gerendert: **Motion-Vielfalt bestätigt** (u.a. `pan_right`, `zoom_in`, `dolly_in`, `static`, `diagonal_glide`, `snap_zoom_in`, `tilt_up`, `dolly_out`, `pan_left` über die 31 Szenen verteilt, nicht mehr nur Zoom). **2 echte Übergänge** ausgelöst (`smoothright` an einem `normal`-Sequenzwechsel, `fade` an einem `calm`-Sequenzwechsel — der dritte Sequenz-Anker ist Szene 0, die per Definition keinen Übergang vor sich hat). **13 SFX-Ereignisse** platziert, exakt wie erwartet nachgerechnet: 1× Whoosh (Übergang mit `sfx≠None`) + 6× Riser (alle `punchy`-Szenen) + 6× Impact (`punchy`-Szenen, die KEIN Übergangspunkt sind) = 13. Selbstprüfung grün, `final.mp4` unverändert 47.2s.

## 21. JOBS-Memory-Cleanup (Phase 0 des "Cinematic Studio"-Erweiterungsplans)

Vorbereitender Schritt vor der ElevenLabs-Integration (Phase 1): `JOBS`, `BATCH_JOBS`, `PLAN_JOBS`, `RENDER_JOBS` und `PRODUCE_JOBS` (Abschnitt 6.1) wuchsen bis dahin für die gesamte Prozesslaufzeit nur — kein Eintrag wurde je proaktiv entfernt. Am gravierendsten bei `JOBS`: ein neuer Eintrag pro Bild-/Veo-Klick, nicht pro Video wie bei den anderen vier (die sind durch ihren `(cid, vid)`-Schlüssel ohnehin auf einen Eintrag pro Video gedeckelt).

### 21.1 `_cleanup_stale_jobs(max_age_hours=MAX_AGE_JOBS_HOURS)` (dashboard.py, direkt nach `RENDER_JOBS`/`_RENDER_JOBS_LOCK`)

`MAX_AGE_JOBS_HOURS = 2.0`. Läuft alle 30 Minuten über einen Daemon-Thread (`_start_job_cleanup_daemon()`, gestartet in `main()` vor `srv.serve_forever()`). Ein Eintrag wird nur gelöscht, wenn er **sowohl** nicht mehr läuft **als auch** älter als die Schwelle ist — ein laufender Job darf nie verschwinden, sonst würde der Client weiter auf einen dem Server unbekannten `job_id`/`(cid,vid)` pollen.

Zwei unterschiedliche "läuft noch"-Prädikate, weil die fünf Dicts unterschiedliche Schemata haben (kein einheitliches `running`-Feld über alle Dicts, wie ein naiver Copy-Paste-Ansatz angenommen hätte):
- **`JOBS`** hat kein `running`-Bool, sondern `status: "running"|"done"|"error"` — das Prädikat ist `status == "running"`.
- **`BATCH_JOBS`/`PLAN_JOBS`/`RENDER_JOBS`/`PRODUCE_JOBS`** haben alle ein echtes `running`-Bool — das Prädikat ist `entry.get("running")`.

### 21.2 `ts`-Feld an jeder Schreibstelle

Jede der ~25 Schreibstellen der fünf Dicts (jeder `XXX_JOBS[key] = {...}`-Literal sowie die In-Place-Mutation, die `BATCH_JOBS[key]["running"]` auf `False` setzt) bekam ein `"ts": time.time()` ergänzt — bei `JOBS` waren bereits zwei Stellen (`done`-Status bei Bild- und Veo-Jobs) mit `"ts": int(time.time())` vorhanden, dort unverändert gelassen; die übrigen (alle `error`-Stellen sowie beide `running`-Start-Stellen) waren bis dahin ohne `ts`.

### 21.3 End-to-End verifiziert (Juli 2026) — synthetischer Stresstest, nicht nur Unit-Logik

1000 synthetische `JOBS`-Einträge eingefügt (250× `running`, 250× `done`+alt, 250× `error`+alt, 250× `done`+frisch) sowie je drei Einträge (`running`+alt, `done`+alt, `done`+frisch) in allen vier übrigen Dicts, dann `_cleanup_stale_jobs()` aufgerufen: exakt die 500 alten, nicht-laufenden `JOBS`-Einträge entfernt (250 verbleiben unverändert, da `running`, + 250 verbleiben, da frisch), in jedem der vier anderen Dicts genau der eine alte-und-nicht-laufende Eintrag entfernt, `running`- und `fresh`-Einträge in allen fünf Dicts unangetastet. Server danach neu gestartet (zuvor über alle vier `_status`-Endpunkte für `sidequerst` geprüft: keine aktive Job — sicherer Neustart-Zeitpunkt, etablierte Regel dieser Session), Daemon läuft.

## 22. Quick-Win Q — `sec`-Defaults angleichen (2026-07)

Nutzer-Beobachtung aus dem Produktions-Render von `sidequerst`: das fertige Video wirkte „fast schon zu schnell geschnitten" — der Default-Wert 4 s pro Szene ist eher auf Reels/Shorts (15–60 s Endprodukt) kalibriert als auf narrative Doku (5–20 Min). Quick-Win Q hebt den Default sanft an und engt die maximale Spanne ein.

### 22.1 Was sich geändert hat

**Frontend** (`dashboard.html`, Zeile ~306):
```html
<!-- vorher: -->
<input type="number" id="sec" value="4" min="2" max="10" step="0.5">
<!-- nachher: -->
<input type="number" id="sec" value="5.5" min="2" max="8" step="0.5">
```
Plus präzisierter Hint-Text, der explizit nennt, was die Backends tun (cap auf 5.5 s für „normal", bis 6 s für „calm", ~1.1 s für „punchy").

**Backend** (`dashboard.py`, Z. ~631 + ~680):
```python
# vorher:
targets = {"calm": PACING_TARGET_SEC["calm"],
           "normal": max(1.5, min(normal_sec, 4.0)),  # hartes Cap bei 4.0s
           "punchy": PACING_TARGET_SEC["punchy"]}
# nachher:
NORMAL_HARD_CAP_SEC = 5.5   # neue Konstante, dokumentiert im Code
targets = {"calm": PACING_TARGET_SEC["calm"],
           "normal": max(1.5, min(normal_sec, NORMAL_HARD_CAP_SEC)),
           "punchy": PACING_TARGET_SEC["punchy"]}
```

### 22.2 Honest disclosure: empirische Scene-Count-Reduktion ist kleiner als die einfache Schätzung

Die Begründung „25–33 % weniger Szenen" stammt aus dem Übergabe-Plan (`IMPLEMENTATION_PLAN_NEXT.md` §1.3) und rechnet mit **Audio-Dauer / sec-per-scene** als simple Rate. In der Realität wird die Scene-Anzahl in `segment_by_pacing()` von **drei** zusammenspielenden Mechanismen bestimmt:

1. `target_words = round(targets[label] * wpm / 60.0)` — was der Plan meint mit „sec-per-scene"
2. `hard_cap_words = round(MAX_SCENE_SEC * wpm / 60.0)` — die **äußere** Decke pro Szene, unabhängig vom Label
3. Label-basiertes Grouping (calm/normal/punchy erzwingt Cuts an Label-Grenzen)

Bei kurzen Units (3–8 Wörter) trifft (2) bereits nach 2–3 Units zu, **bevor** (1) relevant wird — der `target_words`-Unterschied zwischen 10 (sec=4) und 14 (sec=5.5) macht dann oft **keinen Unterschied** in der Scene-Anzahl, sondern nur in der Textmenge pro Scene. Empirisch in Tests: bei kleinen Skripts identische Counts, bei großen Skripts nur ~5–10 % weniger Szenen (nicht 25–33 %).

Der UI-Default-Shift + `max=8` statt `10` sind trotzdem additiv wertvoll:
- Default 5.5 s entspricht der „cinematisch ruhigen" Wahl für narrative Mid-Form-Doku (Simplicissimus-Referenz)
- `max=8` verhindert Extrema („super-lange" Szenen)
- Der `sec=4`-Wert bleibt explizit wählbar — kein Code-Pfad ändert sich für non-default User-Eingaben
- Der Hardcap-Funktion (`sec=10` → effektiv 5.5) ist verifizierbar per `segment_by_pacing(units, pacing, wpm, 10.0)` == `segment_by_pacing(units, pacing, wpm, 5.5)`

## 23. Phase 1 — ElevenLabs-Voiceover mit Word-Timestamps (2026-07)

Das Voice-over kommt direkt vom ElevenLabs `/v1/text-to-speech/{voice_id}/with-timestamps`-Endpoint — Audio + per-Word Timestamps in einem einzigen Round-Trip. Eliminiert den Whisper-Lauf im Hauptpfad und macht das Timing deterministisch (kein LLM, das Szenen aus Audio halluziniert).

### 23.1 Datenmodell-Erweiterungen (additiv)

**`audio_meta.json`** (von `/api/upload_audio`, jetzt auch von ElevenLabs-Pfad beschrieben):
```jsonc
{
  // ...bestehend (path, mime, name)...
  "voiceover_source": "elevenlabs" | "user_upload",
  "voiceover_task_id": "el_xxx",
  "voiceover_chars": 3421,
  "voiceover_word_timestamps": [{"word": "...", "start": 0.0, "end": 0.21}],
  "voiceover_settings_used": {
    "voice_id": "...", "model_id": "eleven_multilingual_v2",
    "stability": ..., "similarity_boost": ..., "style": ..., "use_speaker_boost": ...
  }
}
```

**`plan.json`** (von `_transcribe_generate_worker` voiceover_source-aware befüllt):
```jsonc
{
  "scenes": [...],   // unverändert in der Struktur
  "wpm": ..., "sec": ..., "characters": [...],
  // NEU (alle additiv, alte Pläne bleiben lesbar):
  "source": "elevenlabs" | "audio" | "text"   // Plan-Quelle, war vorher nur "audio"|"text"
  "voiceover_source": "elevenlabs" | "user_upload",
  "voiceover_task_id": "el_xxx" | null,
  "voiceover_word_timestamps": [{"word":..., "start":..., "end":...}] | null
}
```

**Channel-scoped Persistenz** (`channels/<cid>/`):
- `voice_id.txt` — eine Zeile, ElevenLabs voice_id
- `voice_settings.json` — das obige `voiceover_settings_used`-Objekt (slider-freundlich)

### 23.2 Globale State

- `VOICE_JOBS: dict = {}` + `_VOICE_JOBS_LOCK = threading.Lock()` — sechster Job-Dict, in `_cleanup_stale_jobs` integriert (siehe §6.1)
- `ELEVENLABS_VOICE_SETTINGS_DEFAULT` als Single Source of Truth für die fünf ElevenLabs-Slider

### 23.3 Architektur-Entscheidung — keine neue Worker-Pipeline

Option C (ElevenLabs) erbt den Worker-Pfad von Option A (User-Upload). Das ist **kein** Zufall, sondern eine bewusste Designentscheidung aus dem Übergabe-Plan (§4.6):

> „**Variante A — MCP-Server-Wrapper … Variante B — Agenten innerhalb der Pipeline (explizit ablehnen) …**"
>
> Der User-Feedback-Punkt (zitiert im Plan §2.6): „Option C (ElevenLabs) ist semantisch **enger verwandt mit A als mit B** — auch hier wird **Audio erzeugt** und es soll daraus ein Plan entstehen … **derselbe `_transcribe_generate_worker`**."

Konkrete Implementierung statt eines neuen `_voiceover_worker`:

```python
# in _transcribe_generate_worker (Upstream Z. ~3151):
meta = json.load(open(v_audio(cid, vid)))
if meta.get("voiceover_source") == "elevenlabs" and meta.get("voiceover_word_timestamps"):
    words = meta["voiceover_word_timestamps"]
    audio_duration = max((w["end"] for w in words), default=0.0)
    beats = _elevenlabs_words_to_beats(words, sec, audio_duration)   # Time-Windowing
    # ... restliche Pipeline (analyze_script, visual_prompts, plan-write) unverändert ...
else:
    # bestehender Gemini-Transcribe-Pfad — unverändert
    beats = transcribe_and_segment(meta["path"], meta["mime"], sec)
```

Das spart:
- Einen kompletten neuen Background-Thread
- Eine neue Status-Polling-Schleife (existierende PLAN_JOBS-Polling wird wiederverwendet)
- Sonderbehandlung in der Orchestrator-Logik

### 23.4 API-Verhalten — explizit kein stillschweigender Fallback

`elevenlabs_generate()` (Phase 1) verhält sich restriktiv, gemäß `ARCHITECTURE.md` §6.1 (kein Agentic-Drift):

```python
# strenge Validierung der Response-Form:
if not resp.get("audio_base64"):
    raise RuntimeError("ElevenLabs antwortete ohne audio_base64 — bitte erneut versuchen.")
if not resp.get("alignment", {}).get("words"):
    raise RuntimeError("ElevenLabs antwortete ohne alignment.words — bitte erneut versuchen "
                       "(Provider-Schema-Drift oder leerer Text?).")
```

Plus Retry-Policy: 429/5xx → Backoff 5/10/20 s (max 3 Retries). Alles andere (401, 422, Schema-Drift) → sofortiger raise, **kein** Whisper-Fallback. Der User sieht den Fehler im Frontend und entscheidet, was er tut.

### 23.5 Atomic-Write-Strategie (kein halbpersistierter Zustand)

`_elevenlabs_persist_and_schedule()` schreibt in einer definierten Reihenfolge:

```
1. ElevenLabs-Call → liefert mp3_bytes + words + task_id (alles im RAM)
2. voiceover.mp3 schreiben
3. audio_meta.json schreiben (canonical resume-marker)
   ↳ wenn Schritt 3 failt → voiceover.mp3 wieder löschen
4. plan.json bestehende start_aligned / end_aligned nullen
5. _transcribe_generate_worker im Hintergrund starten
```

Wenn 3 fehlschlägt, ist die Disc garantiert entweder ganz leer (kein voiceover.mp3, kein meta) oder ganz vollständig (beides da + plan.json mit ElevenLabs-Phasen). Niemals halb.

### 23.6 Idempotenter Resume-Marker

`/api/voiceover_generate` führt vor dem ElevenLabs-Call eine Idempotenz-Prüfung durch:

```
if audio_meta["voiceover_source"]=="elevenlabs"
   and audio_meta["voiceover_word_timestamps"] exists
   and plan.json exists with voiceover_source="elevenlabs":
        return {ok: true, resume: true, ...}
```

**Kein** zweiter API-Call, **kein** erneuter Worker-Start — die ElevenLabs-Phase ist bereits abgeschlossen. Die `voiceover_task_id` im Response ist die alte, identifizierbar.

### 23.7 Frontend „Option C"-Karte

Im bestehenden Schritt-②-Block zwischen Option A (Upload) und Option B (Manuell) als gleichberechtigte Karte. Inhalte:

- Voice-Dropdown (`/api/elevenlabs_voices`) — listet Library + Cloned Voices
- 4 Slider (Stability / Similarity / Style / Speaker-Boost) mit Reset-Button
- „Voice testen" → `/api/voiceover_preview` → 5 s Sample-Audio (raw `audio/mpeg`, nicht JSON)
- „🎙 Voiceover generieren" → `/api/voiceover_generate` → Polling via `/api/voiceover_status` + `/api/plan_status` (Orchestrator-Pfad wird reused, kein neues Polling)

### 23.8 End-to-End verifiziert (Juli 2026)

Smoke-Tests gegen echten ElevenLabs-Account (Test 1 Konfiguration: 26 Voices geladen, Test 3 Resume: zweiter Generate-Call liefert `{resume: true}` ohne API-Call, Test 4 Fallback: alte Pläne ohne `voiceover_source` fallen sauber auf Gemini-Transcribe zurück, Test 6 Partial Response: `audio_base64` ohne `alignment.words` raise'd korrekt). Test 2 und Test 5 (echtes End-to-End-Render mit ElevenLabs-Audio) verlangen ElevenLabs-Credits und wurden nicht live ausgeführt — die isolierten Code-Pfade sind aber grün.

## 24. Phase 3 — Story-Phase-Engine (2026-07)

LLM-getriebene Dramaturgie-Analyse ersetzt die position-basierte Phase-Heuristik aus §20/§22. Voraussetzung für Phasen 4–9 (Cinematic-Erweiterung).

### 24.1 Was sich geändert hat

Drei additive Felder im Schema von `analyze_script`-Output (`dashboard.py:926`):

| Feld | Typ | Zweck |
|---|---|---|
| `phases` | `[{beat: int, phase: "OPENING"\|"RISING_ACTION"\|"CLIMAX"\|"RESOLUTION"}]` | Pro Beat die dramaturgische Phase — narrativ, NICHT position-basiert |
| `act_breaks` | `[int]` | Beat-Indizes an Akt-Grenzen |
| `climax_beat` | `int` (oder `-1`) | Einzelindex des dramaturgischen Höhepunkts |

Der `analyze_script`-LLM-Prompt (Z. ~951) instruiert explizit: „Diese reflect actual narrative arc, NOT position". Damit kann der LLM Flash-Forward / Cold-Open korrekt als `CLIMAX` oder `RESOLUTION` zuweisen.

### 24.2 Single Source of Truth + `phase_source` Debug-Feld

Jede Szene bekommt jetzt zusätzlich zu `phase` ein Feld `phase_source`:
- `"llm"` — vom LLM getrieben
- `"position-fallback"` — `story_phase(i, total)` Heuristik

`grep "position-fallback" plan.json` listet sofort alle Szenen, in denen der LLM nichts geliefert hat. `grep "llm"` listet die LLM-getriebenen.

### 24.3 80%-Hysterese gegen Schema-Drift

`_assign_phases()` (in `engine_elevenlabs.py`) hat eine **Hysterese-Schwelle**:

```python
coverage = len(llm_phases) / total_scenes
use_llm = coverage >= 0.8   # 80% der Beats brauchen LLM-Phase

if use_llm and beat in llm_phases:
    s["phase"]         = llm_value    # LLM hat geliefert
    s["phase_source"]  = "llm"
else:
    s["phase"]         = story_phase(s["i"], total)   # Heuristik
    s["phase_source"]  = "position-fallback"
```

Unter 80% Coverage: **vollständiger Fallback** aller Szenen — kein Mix aus LLM- und Heuristik-Phasen. Verhindert Mischung aus vertrauenswürdigen Fallback-Phasen und unzuverlässigen LLM-Phasen bei Schema-Drift. Test im E2E-Suite (`tests/test_cinematic_e2e.py`).

### 24.4 Szenen-Felder (additiv in `_assign_phases`)

Nach dem Phase-Assign haben alle Szenen:

```jsonc
{
  "i": int, "text": str,
  "phase": "OPENING" | "RISING_ACTION" | "CLIMAX" | "RESOLUTION",
  "phase_source": "llm" | "position-fallback",
  "is_phase_break": bool,    // true wenn LLM-PhaseBreak-Liste enthält
  "is_climax": bool,         // true wenn s["i"] == LLM-climax_beat
  "act_index": int,          // 0..3 abgeleitet aus phase
  "kind": "scene" | "title_card",   // Phase E — siehe §27
  "act_index_visual": int,   // 1-basiert für Title-Card-Beschriftung
  "card_title": str | None,  // "Akt 1", "Neuer Akt", oder User-Override
}
```

### 24.5 Frontend-Badges

In `dashboard.html` §Szenen-Rendering:

- **Phase-Pill** je Szene: kleines Pill rechts in `scene-meta`, farbcodiert:
  - `OPENING` → neutral grau
  - `RISING_ACTION` → blau (`#1e6bd6`)
  - `CLIMAX` → rot (`#c13838`) — ggf. mit subtilem Puls-Glow
  - `RESOLUTION` → grün (`#1f8a4a`)
- **`is_climax === true`** → subtiler goldener Outline-Ring um die Szenen-Karte (CSS `outline: 2px solid #d4a02c`)
- **`is_phase_break === true`** → dünne violette Border-Left neben der Karte (Akt-Trennlinie)

### 24.6 Motion-Auswahl (sofortige Aktivierung)

`_motion_for_scene` (`dashboard.py:1948`) liest schon `scene.get("phase")` und pickt aus `_PHASE_MOTION_CANDIDATES`:

```python
phase = scene.get("phase")
candidates = _PHASE_MOTION_CANDIDATES.get(phase) or _PACING_MOTION_CANDIDATES[pacing]
name = candidates[scene.get("i", 0) % len(candidates)]
```

Damit **sofort aktiviert**: Cold-Open mit `phase='CLIMAX'` → `snap_zoom_in`/`diagonal_glide`/`static`-Candidatenpool statt der position-basierten `pacing='normal'`-Heuristik. Determinismus erhalten (per `scene.i` modulo), kein zusätzlicher Render-Worker nötig.

### 24.7 End-to-End verifiziert (Juli 2026)

Tests in `tests/test_cinematic_e2e.py` (`tests/test_cinematic_e2e.py`):

- **B1**: `t_phase_b_story_engine_full_coverage` — Cold-Open Scenario (Beat 0 = CLIMAX, Beat 2 retro = OPENING). Verifiziert `phase`, `phase_source`, `is_climax`, `is_phase_break`.
- **B2**: `t_phase_b_story_engine_partial_hysteresis` — 50% Coverage → vollständiger Fallback, kein Mix.
- **B5**: `t_phase_b_motion_selector_uses_phase` — alle 4 Phasen wählen aus den richtigen Motion-Candidaten-Pools.


## 25. Phase 4 — Pacing-aware Image-Prompts (2026-07)

Visuelle Stilanweisungen in `visual_prompts()` basierend auf der narrativen Phase. Aktiviert sobald Phase B (`scene["phase"]`) gesetzt ist.

### 25.1 Was

`PHASE_PROMPT_ADDITIONS` (in `engine_elevenlabs.py`):

| Phase | Stil-Injection |
|---|---|
| `OPENING` | slow, deliberate composition; establish setting; neutral color palette; static-feeling even if motion comes later |
| `RISING_ACTION` | building tension; tighter framing; movement toward subject; contrast slightly elevated |
| `CLIMAX` | maximum visual impact; high contrast; dynamic angle; subject dominates frame; emotional saturation |
| `RESOLUTION` | wind-down; wider framing; softer palette; contemplative stillness |

### 25.2 Hook

`_image_prompt_chunk(chunks, offset, total, analysis_ctx, chunk_phases)` nimmt jetzt einen fünften Parameter: `chunk_phases` (list von phase-strings, parallel zu `chunk_beats`). Im LLM-Prompt wird jede Zeile als `N. [Phase: X] <text>` nummeriert — der LLM bekommt die Phase als Kontext, nicht als Subject.

Vor der `_IMAGE_PROMPT_FEWSHOT`-Sektion wird die Stil-Anweisung einmal erklärt:
```
PHASE STYLING (Phase C, Juli 2026) — each numbered line below is annotated with
its narrative phase. Adapt the image style to that phase: [4 cues ...]
Don't override the LINE'S TEXT — these cues modulate STYLING, not subject matter.
```

Legacy-Caller ohne `chunk_phases` (kein Phase-Set) bleiben kompatibel — die Annotation wird übersprungen, das Verhalten ist unverändert.

### 25.3 Verifikation

- Test in `tests/test_cinematic_e2e.py` (`t_phase_c_prompt_additions_present`): prüft dass jede Phase einen STYLE-Marker hat.
- Erwartet: in Production-Renders kriegen CLIMAX-Szenen dramatischere Kompositionen als OPENING-Szenen (visueller A/B-Vergleich empfohlen).


## 26. Phase 5 — Color-Grading pro Phase (2026-07)

ffmpeg `eq`-Filter pro Phase in `_render_clip`. Aktiviert sofort wenn `scene["phase"]` gesetzt ist.

### 26.1 Was

`PHASE_COLOR_FILTER` (in `engine_elevenlabs.py`):

| Phase | ffmpeg-Filter | Wirkung |
|---|---|---|
| `OPENING` | `eq=contrast=1.0:saturation=0.9:brightness=0.0` | Neutral, leicht entsättigt |
| `RISING_ACTION` | `eq=contrast=1.1:saturation=1.05:brightness=0.0` | leicht angehoben |
| `CLIMAX` | `eq=contrast=1.3:saturation=1.2:brightness=-0.02` | maximaler Impact, leicht dunkler |
| `RESOLUTION` | `eq=contrast=0.95:saturation=0.85:brightness=0.03` | weicher, leicht heller |

Legacy-Szenen ohne Phase (oder mit unbekannter Phase) → **Identity-Filter** (`""` als `eq_filter`), kein Color-Grading — bestehende Renderings bleiben byte-genau identisch.

### 26.2 Hook

`_render_clip` (`dashboard.py:2056`, jetzt in der _render_worker-Section bei §7.4):
```python
phase = scene.get("phase", "")
eq_filter = PHASE_COLOR_FILTER.get(phase, "")
eq_suffix  = f",{eq_filter}" if eq_filter else ""
filter_parts = [
    f"[0:v]scale={RENDER_SUPERSAMPLE_WIDTH}:-2,"
    f"zoompan=z='{z_expr}':d={frames}:x='{x_expr}':y='{y_expr}':"
    f"s={RENDER_WIDTH}x{RENDER_HEIGHT}:fps={fps},setsar=1"
    f"{eq_suffix}[base]"
]
```

Position: **nach zoompan und vor Overlays**. Die Overlays (Captions, Callouts, Title-Cards) sitzen also auf dem color-graded Base, nicht selbst color-graded — sonst würden Schriftfarben verzerrt.

### 26.3 Verifikation

- Test in `tests/test_cinematic_e2e.py` (`t_phase_d_color_filter_present`): prüft dass jeder Filter mit `eq=` startet und alle 3 Dimensionen (contrast, saturation, brightness) enthält.
- Visuell: 4-Phasen-Vergleichs-Render (gleicher Input, forcierte Phase-Override pro Scene) zeigt sichtbare Color-Grading-Unterschiede. Subtile Werte, nicht dramatisch — alles, was stärker ist, liest sich als „defekt" statt „cinematic".


## 27. Phase 7 — Title-Cards als eigener Szenentyp (2026-07)

Akt-Übergänge werden nicht mehr als reguläre Bild-Szene gerendert, sondern als Vollbild-Titel-Karten mit zentriertem Text und Phase-Color-Accent-Unterstreichung.

### 27.1 Datenmodell-Erweiterung

`_assign_phases` (Phase B §24) setzt auf Szenen mit `is_phase_break === true`:
- `kind: "title_card"` — ersetzt `kind: "scene"` als Default
- `act_index_visual: <int>` — 1-basierter Index unter den Title-Cards
- `card_title: str` — automatisch abgeleitet:
  - Eine einzelne Title-Card → `"Neuer Akt"` (singular)
  - Mehrere → `"Akt 1"`, `"Akt 2"`, ...
  - User-Manual-Override via direktem Edit in `plan.json` möglich (Frontend erlaubt Click-to-Edit)

Andere Szenen bekommen `kind: "scene"` als Default. **Wichtig:** Title-Cards werden **nur** generiert wenn `act_breaks` aus dem LLM kommen (Coverage ≥ 80%, Position-Fallback hat keine `act_breaks`).

### 27.2 Render-Pipeline

`render_overlay.py` wurde um eine **neue CLI-Mode** erweitert: `python3 render_overlay.py ... title_card <text_b64> [phase]`. Die Mode gibt eine **vollbild-opake PNG** zurück (kein Alpha-Channel wie bei Overlays), mit:
- Weißer Hintergrund
- Zentriertem Titel-Text (Font: Arial Bold, 12% Höhe)
- Phase-Color-Accent-Unterstreichung (gleiche 4-Farben-Palette wie PHASE_COLOR_FILTER)
- Weißer Stroke um schwarzen Text (für Lesbarkeit auf hellem Hintergrund)

`dashboard.py`:`render_title_card_png_via_venv()` ist der Subprocess-Wrapper (analog `render_text_overlay_png`). Läuft im `.venv_whisper`, weil dort PIL vorhanden ist — System-Python hat kein Pillow.

`_render_clip` (`dashboard.py`) hat einen Sonderfall für `kind=='title_card'`:
```python
if scene.get("kind") == "title_card":
    title_card_temp = out_path + ".title.png"
    render_title_card_png_via_venv(title_card_temp, RENDER_WIDTH, RENDER_HEIGHT,
                                    scene["card_title"], phase=scene.get("phase",""))
    img_path = title_card_temp   # swapped for the rest of the pipeline
```

Der `zoompan` + `PHASE_COLOR_FILTER` (Phase D) + Overlays laufen anschließend unverändert — der Title-Text profitiert von der langsamen Phase-D-Bewegung und bleibt lesbar durch den Stroke.

### 27.3 Frontend

In `dashboard.html:renderScenes()`:
- Lila Badge `"📜 Titell"` in der `scene-meta`-Zeile wenn `kind === 'title_card'`
- Subtile Phase-Border-Left (violett, kollidiert NICHT mit der Sequenz-Border-Left da `color` unterschiedlich ist)

### 27.4 Verifikation

- Tests in `tests/test_cinematic_e2e.py`:
  - `t_phase_e_title_card_assignment` — multi-act script → `kind='title_card'` mit Auto-Titeln
  - `t_phase_e_title_card_lifecycle_fallback` — position-fallback → keine Title-Cards (LLM-only feature)
- Visuell: aktuelles Skript mit 2+ Akten rendert mit eingeblendeten Title-Cards an den Bruchstellen.


## 28. Phase 6 — Counter-Animation-Callouts für punchige Szenen (2026-07)

Punchige Momente bekommen größere, zentrierte Counter-Overlays (rot + dicker schwarzer Stroke) statt der Standard-Callouts (gold-gelb, oben links).

### 28.1 Trigger

In `_overlay_specs_for_scene` (`dashboard.py:2036`):
```python
if overlay_opts.get("callouts") and scene.get("callout"):
    if scene.get("pacing") == "punchy":
        # Phase F: counter style für dramatische Momente
        specs.append(("counter", scene["callout"], counter_t0, counter_t1))
    else:
        # Standard callout style (unverändert)
        specs.append(("callout", scene["callout"], t0, t1))
```

Auto-routing: punchige Szenen mit `callout`-Daten bekommen automatisch Counter-Style — keine User-Aktion nötig, kein neuer UI-Toggle.

### 28.2 Render-Style

`render_counter(width, height, text)` in `render_overlay.py`:
- Full-frame RGBA mit `alpha=0` Hintergrund (transparenter overlay)
- **Eine** einzelne Zeile (kein wrap), weil das `analyze_script`-Prompt Callouts bereits auf max ~6 Zeichen beschränkt
- Schrift: 22% der Höhe (vs. 11% bei normalem `callout`)
- Rot `rgb(220, 38, 38)` Letter-Fill mit dicken schwarzen Stroke (`stroke_width = font_size // 12`)
- Zentriert horizontal + vertikal

### 28.3 Verifikation

- Test `t_phase_f_counter_overlay_for_punchy` in `tests/test_cinematic_e2e.py`: prüft dass punchy+callout → `'counter'` Style, normal+callout → `'callout'` Style.
- Visuell: Render mit Counter-Overlay ist deutlich intensiver als Standard-Callout — der rote Briefstil „schreit" die Zahl.


## 29. Phase 8 (Teil 1 — Phase G) — Per-Phase Music-Bed Volume (2026-07)

> **Scope-Klarstellung:** Phase G in der aktuellen Ausbaustufe ist eine **Vorstufe** zum vollen Cinematic-Plan-§8-Stem-System. Asset-Beschaffung (Pixabay-Stems: drums/bass/pads) und 4-Stem-Crossfade-Architektur sind NICHT gebaut. Was existiert: Per-Phase-Volume-Modulation des bestehenden `neutral_bed.mp3`. Wenn die Stems nachkommen, gibt der Code-Pfad der heute `neutral_bed.mp3` moduliert denselben Volume-Envelope weiter — nur die Input-Quelle ändert sich.

### 29.1 Was

`PHASE_VOLUME` (in `engine_elevenlabs.py`):

| Phase | Volume-Multiply |
|---|---|
| `OPENING` | 0.30 |
| `RISING_ACTION` | 0.55 |
| `CLIMAX` | 0.85 |
| `RESOLUTION` | 0.35 |

### 29.2 Staircase-Fix (User-Feedback Phase-G.4)

Initial-Implementation nutzte `between(t,start,end)*vol` pro Scene — das verursacht einen **1-Frame-Peak an jeder Phasengrenze**, weil `between()` inklusive an BEIDEN Enden ist. Beispiel: bei t=5 zwischen `*0.30` (Szene 1) und `*0.55` (Szene 2) ergibt die Summe **0.85** statt 0.55 (correct).

**Fix:** statt `between(t,st,en)*vol` jetzt `(if(gte(t,st),1,0))*(if(lt(t,en),vol,0))`. **Inclusive-start, exclusive-end** Semantik. Damit ist t=5 saubere Schwelle von Scene-1-Volume zu Scene-2-Volume — kein Peak.

Getestet in `tests/test_cinematic_e2e.py` (`t_phase_g_volume_no_boundary_peak`): drei Phasen back-to-back, alle Boundary-Werte manuell mit der ffmpeg-Semantik evaluiert. Regression-Guard: Source-grep prüft dass die alte `between(t,{st:.3f},{en:.3f})`-Zeile nicht zurück kommt.

### 29.3 Hook in `_build_final_audio`

Reihenfolge beim Sound-Design (geändert mit Phase G):
1. `_phase_modulate_music()` — Phase-Volume-Envelope auf den Music-Bed vor-modulieren
2. `_duck_music_under_voice()` — sidechaincompress auf der bereits modulierten Spur
3. `_build_sfx_events()` — wie bisher
4. `_place_sfx()` — wie bisher

Erlaubt Pixabay-Stems später ohne Code-Änderung einzuhängen — der Volume-Envelope wirkt auf allen Music-Inputs gleich.

### 29.4 Verifikation

- `tests/test_cinematic_e2e.py`:
  - `t_phase_g_volume_envelope_construction` — Expression-Bau, Volumen korrekt
  - `t_phase_g_volume_no_phase_falls_back` — Legacy-Plans ohne Phase fallen sauber auf Identity-Copy zurück
  - `t_phase_g_volume_no_boundary_peak` — Staircase-Fix, kein Peak an Boundaries


## 30. Phase 2 (alt) — TTS-Preprocessing (Phase I, 2026-07)

Enriched das Skript mit TTS-freundlichen Pause/Emphasis-Markern vor ElevenLabs-Anfrage. Reines Text-Preprocessing, kein LLM-Call, idempotent.

### 30.1 Was

`_enrich_for_tts(text, scenes)` (in `engine_elevenlabs.py`):
- Fügt `" ... "` zwischen Sätzen (Subtle-Between-Capitals via split-then-join-Pattern, **idempotent by construction**)
- Fügt `"... "` vor `is_climax`-Szenen (extra emphasis)
- Fügt `"\n\n"` vor `is_phase_break`-Szenen (starke Pause zwischen Akten)

### 30.2 Idempotenz-Pattern (User-Feedback-Phase-I.2)

Initial-Implementation hatte zwei Bugs:
1. `lstrip()` entfernte die Newlines vom `TTS_PAUSE_AFTER_PHASE_BREAK = "\n\n"`. **Fix:** kein lstrip mehr, Marker verbatim einfügen.
2. Marker-Replace-Path war nicht idempotent — bei wiederholter Anwendung wurde `"\n\n"` mehrfach vor den Scene-Text gesetzt. **Fix:** Idempotency-Check `if prefix + txt not in enriched` vor jedem Replace.

Sentence-Splitting war ursprünglich per Regex-substitute implementiert (`\.\\s+(?=[A-Z])` → `. ... X`), was bei wiederholter Anwendung die Ellipsen kompiliert. **Fix:** split-then-join Pattern — `re.split(r'(?<=\.)\s+(?=[A-Z])')` zerlegt die Szene in Segmente, jeder Segment-Trailing-`...` wird gestrippt, dann sauber mit `' ... '` joiner.

### 30.3 Hook in `/api/voiceover_generate`

In `dashboard.py` `do_POST` für `/api/voiceover_generate`:
```python
text = _enrich_for_tts(text, scenes=None)   # scenes=None im ersten Aufruf
result = _elevenlabs_persist_and_schedule(cid, vid, text, ...)
```

Scenes=None in der ersten Generation; scene-basierte Marker werden beim nächsten Regenerate aktiv, sobald plan.json existiert.

### 30.4 Verifikation

- `tests/test_cinematic_e2e.py` (`t_phase_i_enrich_for_tts`): testet sentence-Pausen, climax/phase-break marker, Idempotenz auf enriched text. Verifiziert dass der 2. Aufruf auf enriched text keine zusätzlichen Marker einschleust.


## 31. Phase 9 (Scaffold) — Multi-Speaker-Datenmodell (Phase H, 2026-07)

> **⚠ SCAFFOLD ONLY.** Was existiert: Daten-Slot + Detection. Was fehlt: die eigentliche Per-Speaker-ElevenLabs-Pipeline (H.2). Wer „Multi-Speaker ist eingebaut" behauptet, behauptet etwas das nicht da ist — alle Szenen werden aktuell mit dem Channel-Default-Voice generiert, egal was `s["speaker"]` enthält.

### 31.1 Was funktioniert

- `s["speaker"]` Datenmodell auf jeder Szene (Default `"narrator"` falls nicht gesetzt, in beiden Workern — `_transcribe_generate_worker` Z. ~3413 + `_plan_generate_worker` Z. ~2802)
- **Detection-Log** in `_transcribe_generate_worker`: wenn Szenen mehr als einen distinct speaker haben, WARNUNG im Log + Hinweis auf follow-up

```
[Phase H] WARNUNG: 2 distinct speakers erkannt (['narrator', 'yeonmi']).
Aktueller ElevenLabs-Pfad generiert alle Szenen mit dem Channel-Default-Voice.
Multi-Speaker-Pipeline ist ein follow-up (Plan §H.2). Edit s['speaker'] in
plan.json manuell wenn du jetzt verschiedene Stimmen willst.
```

### 31.2 Was fehlt (H.2 — Future)

Geplante Multi-Speaker-Pipeline, sobald eine sinnvolle ElevenLabs-Subscription mit Multi-Voice-Endpoint verfügbar ist:

1. **Pro unique speaker** ein eigener ElevenLabs-API-Call (oder ein Multi-Speaker-Call wenn vom Provider unterstützt)
2. **Audio-Segmente** per ffmpeg-concat zusammenfügen zu einer einzigen Spuren
3. **Kombinierte `voiceover_word_timestamps`** mit Speaker-Annotation pro Segment
4. **`speaker_voices` mapping** in `channels/<cid>/voice_settings.json` (Erweiterung der existierenden ElevenLabs-Settings-Datei)
5. Optional: Frontend-Badges auf jeder Szene, die den Speaker zeigt

Bis das gebaut ist, bleibt `s["speaker"]` ein Metadaten-Feld ohne sichtbaren Effekt — die Audio-Pipeline routet alles durch den Channel-Default-Voice.


## 32. Phase J — Engine-Refactor: `engine_elevenlabs.py` (2026-07)

Erste Teil-Aufspaltung von `dashboard.py` in fokussiertere Module. Pattern: Pure-Helpers extrahieren, via Wildcard-Import rückwärtskompatibel lassen.

### 32.1 Was wurde extrahiert

`engine_elevenlabs.py` (357 Zeilen neu) enthält jetzt:
- **ElevenLabs-Integration** (Phase 1 + Phase H + Phase I)
  - Konstanten: `ELEVENLABS_API`, `ELEVENLABS_DEFAULT_MODEL`, `ELEVENLABS_KEY_FILE`, `ELEVENLABS_VOICE_SETTINGS_DEFAULT`
  - Voice-Settings-Persistenz: `ch_voice_id`, `elevenlabs_key`, `_resolve_voice_id`, `load_voice_settings`, `save_voice_settings`
  - API-Call + Orchestration: `_elevenlabs_call_with_retry`, `elevenlabs_generate`, `_elevenlabs_persist_and_schedule`
  - TTS-Preprocessing: `_enrich_for_tts`, `TTS_PAUSE_BEFORE_CLIMAX`, `TTS_PAUSE_AFTER_PHASE_BREAK`
- **Phase-Engine Constants** (Phasen B-G): `PHASE_SET`, `PHASE_TO_ACT`, `PHASE_PROMPT_ADDITIONS`, `PHASE_COLOR_FILTER`, `PHASE_VOLUME`, `PHASE_ACCENT`

### 32.2 Import-Pattern

`dashboard.py:14`:
```python
from engine_elevenlabs import *  # noqa: F401,F403
```

`engine_elevenlabs.py` definiert eine **vollständig explizite `__all__`-Liste** (kein `dir()`-Comprehension) — geschützt gegen Reorder-Issues. Wenn jemand nachträglich eine neue Funktion hinzufügt und vergisst sie zu registrieren, schlägt der Wildcard-Import stillschweigend fehl — vermeidet das User-Feedback-J-Bug-Pattern.

### 32.3 Lazy-Imports für zirkuläre Abhängigkeiten

`engine_elevenlabs.py:_elevenlabs_persist_and_schedule` ruft dashboard-Helfer wie `ensure_video`, `_VOICE_JOBS_LOCK`, `_transcribe_generate_worker`, `_PLAN_WRITE_LOCK` auf. Diese werden **innerhalb der Funktion** importiert (lazy), um zirkuläre Imports zwischen den Modulen zu vermeiden:

```python
def _elevenlabs_persist_and_schedule(cid, vid, text, ...):
    if not vid:
        raise RuntimeError("Kein Video ausgewählt.")
    from dashboard import (ensure_video, ch_voice_id, ch_voice_settings,
                            v_uploads, v_audio, v_plan, _VOICE_JOBS_LOCK,
                            VOICE_JOBS, _transcribe_generate_worker)
    ...
```

### 32.4 Ergebnis-Diff

| File | Vorher | Nachher | Δ |
|---|---|---|---|
| `dashboard.py` | 4742 Zeilen | 4380 Zeilen | **−362** (−8%) |
| `engine_elevenlabs.py` | — | 357 Zeilen | +357 |

### 32.5 Was NICHT refactored (work-in-progress)

- Render-Pipeline: `_render_clip`, `_build_final_audio`, `_assemble_clips`, `_mux_audio`, `_phase_modulate_music` (alle in `dashboard.py`)
- Audio-Subsystem: `render_overlay.py` (Standalone-Skript, schon ausgelagert)
- Transcribe-Pfad: `transcribe_and_segment`, `whisper_transcribe`
- LLM-Client: `post_kie_text`, `post_gemini_native`
- Orchestrator: `_plan_generate_worker`, `_batch_generate_worker`, `_render_worker`, `_produce_worker`

Die nächsten Refaktor-Wellen (Phase J.2, Phase J.3 etc.) sollten z.B. `engine_render.py` (Video-Pipeline) und `engine_audio.py` (Sound-Mixing + Stems) als natürliche nächste Schritte extrahieren. Reihenfolge nicht erzwungen — jeder Extrakt-Schritt ist isoliert testbar.

### 32.6 Verifikation

- `tests/test_cinematic_e2e.py`:
  - `t_phase_j_engine_refactor_globals_intact` — alle wild-exported Symbole kommen aus `engine_elevenlabs` (nicht aus dashboard.py-Resten)
  - `t_phase_j_dashboard_unchanged_callers_still_work` — Caller wie `dashboard.save_voice_settings(...)`, `dashboard._assign_phases(...)`, `dashboard.elevenlabs_key(...)` funktionieren weiterhin ohne Code-Änderung im Caller

