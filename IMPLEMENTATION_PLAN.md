# Implementierungsplan: Bild-Sequenzen & Auto-Rendering
### Storyboard Generator — Übergabedokument für Claude Code

Stand: Juli 2026. Dieses Dokument ist **selbsterklärend** — es setzt keine vorherige Konversation voraus. Es beschreibt (1) den verifizierten Ist-Zustand von `dashboard.py`/`dashboard.html`, (2) zwei neue Fähigkeiten, die eingebaut werden sollen, und (3) einen phasenweisen, einzeln testbaren Bauplan.

**Projektpfad:** `/Users/noel/Documents/Claude/N8NWorkflows/yeonmi_storyboard/`
**Start:** `python3 dashboard.py --port 8765`

---

**Errata (nachträglich, Stand siehe `ARCHITECTURE.md` Abschnitt 16):** Überall in diesem Dokument, wo **„ElevenLabs Scribe"** als Quelle für Wort-Timestamps in Phase 3 genannt wird, gilt das als **überholt**. Live-Test von `elevenlabs/speech-to-text` über KIE (echter Task, echte Credits) blieb dauerhaft im Status `"waiting"` hängen, ohne je ein Ergebnis zu liefern — das Modell ist über KIE nicht nutzbar. Gemini wurde als Alternative geprüft und verworfen, da es bei präzisen Timestamps halluziniert (für reinen Transkript-Text ist es gut, für "wann genau wurde welches Wort gesagt" nicht verlässlich). **Ersetzt durch: lokales `faster-whisper`** (Modell "small", `word_timestamps=True`) — läuft offline, ohne Rate-Limit, ohne laufende Kosten, mit ~500 MB einmaligem Modell-Download und ~1 GB RAM-Bedarf während einer kurzen Transkriptions-Burst-Phase. Jede Erwähnung von „Scribe"/`transcribe_words_scribe` unten ist im Sinne dieser Ersetzung zu lesen.

---

## 0. Anleitung für Claude Code

1. **Lies `dashboard.py` und `dashboard.html` vollständig, bevor du irgendetwas änderst.** Die Zeilenangaben in diesem Dokument stammen aus einem Repo-Snapshot (GitHub `main`, Juli 2026) und können vom lokalen Stand leicht abweichen — nutze sie als *Orientierung*, verifiziere Funktionsnamen als *Anker* (siehe Abschnitt 1).
2. **Zero-Framework ist keine Präferenz, sondern eine harte Anforderung.** Kein Flask/FastAPI, kein React/Vue, kein Node.js, kein Remotion, kein Editly, kein MoviePy. Nur: Python-Stdlib (`http.server`, `threading`, `json`, `subprocess`, `urllib`) + Vanilla JS/HTML/CSS + das externe `ffmpeg`-Binary via `subprocess`.
3. **Der Veo-/Grok-Video-Pfad wird nicht angefasst** (`gen_veo`, `extend_veo`, `gen_video`, `gen_t2v`, `make_t2v_prompt`, alles rund um `/api/generate_t2v`, `/api/generate_video`). Das ist ein separates Feature (KI-generierte Bewegung), das mit dem neuen Renderer nichts zu tun hat.
4. **Jede neue langlaufende Aktion folgt exakt dem bestehenden Muster**: Daemon-Thread + In-Memory-Status-Dict mit eigenem `threading.Lock()` + `POST /api/..._start` + `GET /api/..._status`-Polling. Kopiere den Stil von `_batch_generate_worker`/`BATCH_JOBS`, nicht neu erfinden.
5. **Arbeite die Phasen in Abschnitt 8 der Reihe nach ab.** Jede Phase hat ein „Definition of Done" — nicht weitermachen, bevor die vorige testbar funktioniert.
6. **Bei den in Abschnitt 9 gelisteten offenen Fragen: nachfragen, nicht raten**, bevor der jeweils betroffene Codeteil geschrieben wird.
7. **Dieser Plan ist nicht die gesamte Roadmap.** Abschnitt 11 listet bewusst zurückgestellte Erweiterungen und bekannte Lücken (Code-Wartbarkeit, weitere Phasen, nicht diskutierte Themen). Lies Abschnitt 11, bevor du eine Design-Entscheidung triffst, die spätere Phasen erschweren würde (z. B. Datenmodell-Felder so eng bauen, dass Scribe-Timing in Phase 3 nicht mehr reinpasst).

---

## 1. Verifizierter Ist-Zustand (Ground Truth)

Es gibt im Projekt eine `ARCHITECTURE.md`, die an mehreren Stellen einen **Wunschzustand** beschreibt, der im tatsächlichen Code (verifiziert per Repo-Clone) **nicht existiert**. Bitte dieser Tabelle vertrauen, nicht der alten Doku, wo sie widersprechen:

| `ARCHITECTURE.md` behauptet | Code-Realität |
|---|---|
| `segment_by_pacing()`, `pacing`-Labels pro Einheit, `MAX_SCENE_SEC` | existiert **nicht** — nur `segment(text, wpm, sec_per_img)`, Timing rein aus Wortzahl/wpm geschätzt |
| `analyze_script` liefert `pacing` | liefert nur `locations`, `characters`, `recurring_symbols`, `emotional_arc`, `callbacks` |
| `MAX_CONCURRENT_IMAGE_GENS = 8`, `ThreadPoolExecutor(max_workers=8)` im Batch-Worker | `MAX_CONCURRENT_IMAGE_GENS = 2`; `_batch_generate_worker` ist eine **reine sequenzielle for-Schleife**, kein Thread-Pool — eine Szene läuft vollständig durch, bevor die nächste startet |
| `_kie_submit_image` hat den `image_input`/`image_urls`-Feld-Bug | **bereits gefixt** — die Funktion hat einen `ref_urls`-Parameter, der automatisch das richtige Feld je Modell wählt |

**Wichtige Konsequenz aus der Sequenzialität:** Weil `_batch_generate_worker` schon streng sequenziell ist, braucht die Bild-Ketten-Logik (Abschnitt 2) **keine zusätzliche Synchronisation**. Wenn Szene N+1 an der Reihe ist, ist Szene N garantiert fertig und ihre `source_url` garantiert in `plan.json` persistiert.

### 1.1 Relevante bestehende Funktionen (Anker, mit Zeilennummer aus dem Referenz-Snapshot)

```
dashboard.py
  49-77    Pfad-Helper (ch_dir, v_plan, v_out, get_channel_char_ref, ...)
  538-557  segment(text, wpm, sec_per_img)           -- Timing-Schätzung
  564-596  analyze_script(beats)                      -- EIN LLM-Call übers ganze Skript
  598-716  IMAGE_PROMPT_CHUNK_SIZE, _image_prompt_chunk, _validate_image_prompt_entry
  717-770  visual_prompts(scenes, analysis)
  1039-1047 _build_image_prompt(scene_prompt, master, char_refs)
  1058-1089 _kie_submit_image(full_prompt, model, ref_urls)   -- Referenz-Mechanik bereits korrekt
  1098-1177 _image_job_worker(...) / _image_job_worker_inner(...) / _mark_scene_error(...)
  1180-1280 _batch_generate_worker(cid, vid)          -- SEQUENZIELL, kein ThreadPoolExecutor
  1283-1350 _veo_job_worker(...)                       -- Referenz-Beispiel für FFmpeg-Audio-Mux-Aufruf
  1406-...  upload_image_public(local_path)            -- lokales Bild -> öffentliche URL (Fallback-Baustein)
  1747-1769 transcribe_and_segment(local_path, mime_type, sec_per_img)
  1773+     class H(BaseHTTPRequestHandler): do_GET / do_POST -- flache "if p == '/api/...':"-Blöcke

dashboard.html
  173-283  #view-videolist (Tabs: Videos / Stil / Skript-Generator)
  285-394  #view-editor -- Modus-Toggle (293-294), Titel/Thumbnail, Audio, Toolbar (386-394)
  386-394  Toolbar: genAll()-Button, stopGenAll(), downloadZip(), <div id="scenes">
  564      async function openVideo(vid, name)         -- Reload-Wiederaufnahme-Logik
  736      function startBatchPoll()                    -- Polling-Vorbild
  761      function backToVideoList()
  794      async function setMode(mode)
  828      async function makePlan()                     -- Plan-Erstellung, hier Sequenz-Annotation einhängen
  940      function renderScenes(source)                 -- Szenenkarten-Template
  1270     async function genAll()
```

### 1.2 HTTP-Routing-Stil (für neue Routen 1:1 übernehmen)

```python
# GET-Dispatch (in do_GET):
if p == "/api/plan":
    ...
    self._send(200, plan_dict)
    return

# POST-Dispatch (in do_POST):
if p == "/api/generate_all_start":
    body = self._read()
    cid, vid = body["channel"], body["video"]
    ...
    threading.Thread(target=_batch_generate_worker, args=(cid, vid), daemon=True).start()
    self._send(200, {"ok": True})
    return
```
Alle neuen Routen folgen exakt diesem Muster: flacher `if p == "..."`-Block, `cid`/`vid` aus Query (GET) bzw. JSON-Body (POST).

---

## 2. Feature A — Zusammenhängende Bild-Sequenzen ("Ketten")

### 2.1 Ziel

Passagen, die erzählerisch über mehrere Sekunden **denselben Schauplatz/dasselbe Motiv** behandeln, werden nicht als unabhängige Einzelbilder generiert, sondern als **Sequenz**: Bild 0 ist der Anker, jedes Folgebild referenziert sowohl das unmittelbare Vorgängerbild als auch den Anker selbst. Ergebnis: Bilder, die wie Frames derselben Einstellung wirken — visuell und beim späteren Schnitt als durchlaufende Kamerafahrt nutzbar.

### 2.2 Warum Doppel-Anker (nicht nur "Bild N-1")

Recherche-Befund zur nano-banana-2-Referenz-Mechanik: Konsistenz entsteht durch **eine feste Foundation-Image, die jedes Mal mitgeschickt wird** — nicht durch eine Kette, in der jedes Bild nur sein Vorgänger kennt (das akkumuliert Drift, weil jede Generation eine neue Fehlerquelle ist). Deshalb:

- **`seq_pos == 0` (Anker):** keine Kettenreferenz, nur die normale Kanal-Charakter-Referenz.
- **`seq_pos >= 1` (Fortsetzung):** `ref_urls = [ankerbild_url, vorgängerbild_url, char_ref_url]`, dedupliziert (bei `seq_pos == 1` sind Anker und Vorgänger identisch → nur einmal senden). nano-banana-2 erlaubt bis zu 14 Referenzbilder — hier werden nie mehr als 3 gebraucht.

### 2.3 Datenmodell-Erweiterung (`plan.json`, additiv)

Jedes Szenen-Objekt bekommt optional:
```jsonc
{
  "i": 5, "start": 12.0, "dur": 3.1, "text": "...", "prompt": "...",
  "file": "005.jpg", "status": "fertig", "source_url": "https://...",

  "seq_id": 2,             // Szenen mit gleicher seq_id gehören zusammen; fehlt = Einzelbild
  "seq_pos": 1,            // 0 = Anker, 1,2,3... = Fortsetzung
  "chain_anchor_file": "003.jpg",  // welches Bild als Anker-Referenz diente (Nachvollziehbarkeit/Debug)
  "chain_prev_file": "004.jpg"     // welches Bild als Vorgänger-Referenz diente
}
```
Alle Felder optional — alte Pläne ohne diese Felder bleiben gültig (kein `seq_id` = wie bisher behandeln).

### 2.4 Änderung 1 — `analyze_script` um Sequenz-Erkennung erweitern

**Datei:** `dashboard.py`, Funktion `analyze_script` (Zeile ~564).

Den bestehenden JSON-Schema-Block im Prompt (Zeile ~574-584) um ein Feld ergänzen:
```python
'  "visual_sequences": [{"seq_id": N, "beats": [N, N, N], '
'"reason": string, "camera": "slow push-in" | "pan" | "static series"}],\n'
```
Und eine explizite Regel in den Instruktionstext einfügen (analog zum bestehenden `anonymize`-Regel-Satz):
```
Rule: only group beats into a visual_sequence when consecutive beats describe the SAME
concrete location/subject continuously (≥2 beats). When in doubt, do NOT form a sequence —
single independent images are the safe default. A sequence should feel like one continuous
shot, not a scene change.
```
Rückgabe bleibt `json.loads(txt)`, keine Signatur-Änderung nötig — das Ergebnis-Dict hat einfach ein zusätzliches Feld.

### 2.5 Änderung 2 — Sequenz-Zuordnung beim Plan-Erstellen

**Datei:** `dashboard.py`, im `POST /api/plan`-Handler (dort wo heute `segment()` + `analyze_script()` + `visual_prompts()` aufgerufen werden, ~Zeile 2015 ff.).

Neue kleine Hilfsfunktion, direkt nach `analyze_script`-Aufruf:
```python
def _apply_sequences(scenes: list, analysis: dict) -> None:
    """Annotiert scenes in-place mit seq_id/seq_pos, gemäß analysis['visual_sequences'].
    Beats werden 1:1 auf scenes[i] gemappt (gleiche Reihenfolge wie beim Segmentieren)."""
    for seq in analysis.get("visual_sequences", []):
        beat_indices = seq.get("beats", [])
        for pos, beat_i in enumerate(beat_indices):
            if 0 <= beat_i < len(scenes):
                scenes[beat_i]["seq_id"] = seq["seq_id"]
                scenes[beat_i]["seq_pos"] = pos
```
**Geklärt (verifiziert im echten Code):** `analyze_script` wird mit `[s["text"] for s in scenes]` aufgerufen (dashboard.py ~2030/~2222), also mit den **bereits segmentierten Szenen-Texten**, nicht mit den `units`. Damit ist **Beat-Index = Szenen-Index (1:1)** und der direkte Index-Zugriff oben ist korrekt. Der einzige Schutz ist der Bounds-Check `0 <= beat_i < len(scenes)` (falls das LLM einen ungültigen Index liefert). Kein Substring-/Token-Matching nötig — siehe Abschnitt 9.1.

### 2.6 Änderung 3 — Ketten-Referenz im Batch-Worker (der Kern-Eingriff)

**Datei:** `dashboard.py`, Funktion `_batch_generate_worker` (Zeile ~1180-1280), an der Stelle, wo heute `ref_urls` für `_kie_submit_image` gebaut wird (aktuell nur `[char_ref_url]`).

```python
def _resolve_chain_refs(fresh_plan: dict, scene: dict) -> tuple[list, dict]:
    """Liefert (ref_urls_ohne_char_ref, debug_info) für Sequenz-Fortsetzungen.
    Gibt ([], {}) zurück, wenn die Szene kein Fortsetzungs-Bild ist."""
    if scene.get("seq_id") is None or scene.get("seq_pos", 0) == 0:
        return [], {}
    seq_id, pos = scene["seq_id"], scene["seq_pos"]
    by_pos = {s.get("seq_pos"): s for s in fresh_plan["scenes"] if s.get("seq_id") == seq_id}
    anchor = by_pos.get(0)
    prev   = by_pos.get(pos - 1)
    refs, debug = [], {}
    if anchor and anchor.get("source_url"):
        refs.append(anchor["source_url"]); debug["chain_anchor_file"] = anchor.get("file")
    if prev and prev.get("source_url") and prev is not anchor:
        refs.append(prev["source_url"]); debug["chain_prev_file"] = prev.get("file")
    return refs, debug
```

Im bestehenden Submit-Block (wo heute `ref_urls=[char_ref_url] if char_ref_url else None` steht):
```python
chain_refs, chain_debug = _resolve_chain_refs(fresh_plan, scene)
refs = chain_refs + ([char_ref_url] if char_ref_url else [])
task_id = _kie_submit_image(full_prompt, model=image_model, ref_urls=refs or None)
```
Nach erfolgreichem Abschluss der Szene (dort wo `plan["scenes"]` mit `file`/`source_url` aktualisiert wird — heute in `_image_job_worker_inner`, Zeile ~1161-1168) zusätzlich `chain_debug` in das Szenen-Dict mergen, damit es persistiert wird.

**Fallback bei abgelaufener KIE-URL:** Falls der Submit mit `refs` einen HTTP-Fehler wirft (z. B. `source_url` nicht mehr erreichbar), einmal automatisch mit lokal frisch hochgeladenen Bildern (`upload_image_public(local_path)`, Zeile ~1406) statt der alten `source_url` retryen, bevor die Szene als Fehler markiert wird.

### 2.7 Änderung 4 — Prompt-Formulierung für Fortsetzungsbilder anpassen

**Datei:** `dashboard.py`, `_build_image_prompt` (Zeile 1039-1047) oder direkt im Prompt-Chunk-Aufbau.

Recherche-Befund: Wörter wie „different", „new" oder erneute Charakterbeschreibung laden zur Neuerfindung ein und verursachen Drift. **Ebenso wichtig — verneinte Anweisungen ("do NOT redesign") werden von instruktionsbefolgenden Bildmodellen schwächer gewichtet und teils als Fokus fehlinterpretiert ("Rosa-Elefant-Effekt").** Deshalb ausschließlich **positive, harte Constraints** formulieren, keine Verneinungen. Für Fortsetzungsszenen (`seq_pos >= 1`):
```python
if scene.get("seq_id") is not None and scene.get("seq_pos", 0) >= 1:
    scene_prompt = (scene_prompt.strip() +
        "\n\nCONTINUITY (STRICT): This is a continuation of the exact same shot as the "
        "reference image(s). You MUST perfectly match the identity, outfit, and background "
        "environment shown in the references. Change ONLY the camera angle/framing or the "
        "specific action described above.")
```

### 2.8 Frontend — Sequenz sichtbar machen

**Datei:** `dashboard.html`, `renderScenes()` (Zeile ~940), im Szenenkarten-Template.

Wenn `s.seq_id != null`: linken Rand der Karte farbig markieren (Farbe deterministisch aus `seq_id % N` Palette) und ein kleines Badge `⛓ Seq ${s.seq_id} · ${s.seq_pos+1}` einfügen. Rein visuell, kein neuer State, kein neuer API-Call.

---

## 3. Feature B — Automatisches Rendering (FFmpeg)

### 3.1 Nicht-Ziele (wichtig, um Missverständnisse zu vermeiden)

- **Kein KI-generiertes Bewegtbild.** Die Bilder bleiben statische Standbilder. Bewegung (Zoom/Pan) entsteht ausschließlich in FFmpeg.
- **Keine externe Render-Bibliothek.** Kein MoviePy (pip-Paket, lädt Frames als numpy-Arrays in RAM — bei einem 10-Minuten-Video unnötig speicherhungrig), kein Remotion/Editly (Node.js). FFmpeg wird ausschließlich per `subprocess.run([...])` aufgerufen — exakt wie es `_veo_job_worker` (Zeile 1319) heute schon für den Audio-Mux tut.

### 3.2 Architektur-Entscheidung: ein Clip pro Szene, dann Zusammenschnitt

Statt eines einzigen riesigen `filter_complex` über alle Bilder: **jede Szene wird zu einem eigenen kurzen Ken-Burns-Clip gerendert**, danach werden alle Clips zusammengefügt. Vorteil: Fortschritt pro Szene meldbar (Polling), ein fehlerhaftes Bild betrifft nur seinen Clip, debugbar. Das spiegelt exakt das Muster von `_batch_generate_worker` (ein Artefakt pro Szene, sequenziell).

### 3.3 Erprobtes FFmpeg-Rezept: Ken-Burns-Clip ohne Ruckeln

**Bekannter Fallstrick:** `zoompan` arbeitet framegenau auf dem Eingangsbild; ohne Vorvergrößerung entsteht sichtbares Ruckeln bei langsamem Zoom, weil die Zoomstufen auf ganze Pixel runden. **Lösung, durch mehrere unabhängige Quellen bestätigt:** das Bild vor `zoompan` hochskalieren (Supersampling).

```bash
# Szene: 005.jpg, Ziel-Dauer 3.1s, 30 fps, Ziel 1920x1080, leichter Push-in
ffmpeg -y -loop 1 -i 005.jpg -t 3.1 -r 30 \
  -filter_complex "\
    scale=3840:-2,\
    zoompan=z='min(zoom+0.0007,1.12)':d=93:\
      x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':\
      s=1920x1080:fps=30,\
    setsar=1" \
  -c:v libx264 -pix_fmt yuv420p -video_track_timescale 90000 -preset medium render_tmp/005.mp4
```
- `scale=3840:-2` = 4K-Supersampling gegen Jitter. **Warum 4K statt 8K:** bei moderatem Zoom (Deckel ≤ ~1.15) macht 4K das Ruckeln bereits unsichtbar, braucht aber nur ~¼ des Speichers pro FFmpeg-Prozess. Auf einem Mac ist selbst 8K unkritisch (sequenzieller Render, ein Prozess ≈ 100 MB), aber 4K ist der ressourcenschonende, deploy-taugliche Default. **`-2`** (nicht `-1`) erzwingt durch 2 teilbare Dimensionen — `yuv420p`/x264 bricht sonst mit „height not divisible by 2" ab.
- **`-video_track_timescale 90000`** = einheitliche Zeitbasis über alle Clips. Verhindert Mikroruckler/A-V-Drift an den Schnittkanten beim späteren `concat`-Demuxer, die durch unterschiedliche interne Zeitstempel-Rundungen entstehen.
- `d=93` = `round(dur_sekunden * fps)` = `round(3.1 * 30)`.
- `z`-Ausdruck + `min(...)`-Deckel steuern Zoom-Intensität (aus Motion-Rezept, Abschnitt 3.5). **Hinweis:** Bei sehr starkem Zoom (Deckel > ~1.3) reicht 4K u. U. nicht mehr — dann Supersampling adaptiv zur Zoom-Intensität wählen (z. B. `scale=5760:-2`). Für V1 mit sanftem Zoom nicht nötig.
- Für Pans: `x`/`y` als Funktion des Frame-Index `on` variieren statt fixem Zentrum.
- **Alle Clips müssen dieselbe `fps` UND `-video_track_timescale` haben** — sonst ruckeln `concat`/`xfade` an den Übergängen.
- Ausgabe in ein **separates** `render_tmp/`-Verzeichnis, nicht in `generated/` (siehe Abschnitt 9.4 zur Aufräum-Sicherheit).

### 3.4 Zusammenfügen — V1 bewusst nur harte Schnitte

Zwei Wege existieren: `concat`-Demuxer (schnell, verlustfrei, aber nur harte Schnitte) oder eine Kette von `xfade`-Filtern (weiche Übergänge, aber kompletter Re-Encode und nicht-trivialer Offset-Berechnung). **Empfehlung für die erste lauffähige Version: nur harte Schnitte.** Grund: einfacher, schneller, robuster — und innerhalb einer Bild-Sequenz (Feature A) wirkt ein harter Schnitt zwischen zwei sehr ähnlichen Bildern ohnehin fast wie ein Match-Cut. Crossfades sind eine spätere Politur-Phase (Abschnitt 8, Phase 4), keine V1-Anforderung.

```bash
# render_tmp/clips.txt:
# file 'render_tmp/000.mp4'
# file 'render_tmp/001.mp4'
# ...
ffmpeg -y -f concat -safe 0 -i render_tmp/clips.txt -c copy render_tmp/silent.mp4
```
Voraussetzung für `-c copy`: alle Clips identisch encodiert (gleiche Auflösung/fps/Codec/Pixel-Format) — durch das feste Rezept in 3.3 automatisch gegeben.

**Spätere Erweiterung (Phase 4):** An Sequenz-Grenzen (`seq_id` wechselt) optional `xfade` statt hartem Schnitt. Das erfordert dann statt `-c copy` einen vollständigen `filter_complex`-Chain mit korrekt berechneten `offset`-Werten (`offset = kumulierte_zeit − fade_dauer` pro Übergang) — bewusst nicht Teil von V1.

### 3.5 Ken-Burns-Rezept-Generierung (regelbasiert, kein LLM-Call)

Neue Funktion `_motion_for_scene(scene, prev_scene, next_scene)`:
- **Sequenz-Fortsetzung** (`seq_pos >= 1`): gleiche Zoomrichtung wie die vorige Szene der Sequenz fortsetzen (wirkt wie eine durchlaufende Kamerafahrt über mehrere Bilder).
- **Einzelszenen:** alternierend `zoom_in`/`zoom_out` nach Szenenindex, damit nicht jedes Bild gleich „atmet".
- **Kurze Szenen** (`dur < 1.5s`): minimale/keine Bewegung (`z` bleibt nahe 1.0) — schnelle Schnitte vertragen keine sichtbare Kamerabewegung.
- **Intensität skaliert mit `dur`:** lange Szene = langsamerer, größerer Gesamtzoom; kurze Szene = fast statisch.

Ergebnis wird als `motion`-Dict in `plan.json` geschrieben (Format siehe 3.7), damit der Renderer deterministisch und reproduzierbar ist — zweimal rendern mit demselben Plan ergibt dasselbe Video.

### 3.6 Audio

**Nicht** pro Clip audio-mischen (das ist der `_veo_job_worker`-Weg für Einzelclips). Für das Gesamtvideo: **eine durchgehende Voiceover-Spur** über das komplette zusammengefügte Bild-Video legen:
```bash
ffmpeg -y -i silent.mp4 -i voiceover.mp3 \
  -map 0:v -map 1:a -c:v copy -c:a aac -b:a 192k -shortest final.mp4
```

**Sync-Invariante (kritisch):** Die Summe aller Szenen-`dur` muss der echten Audiolänge entsprechen, sonst driftet das Bild gegen den Ton. Vor dem Rendern: `audio_duration` per `ffprobe` ermitteln (`ffprobe -v error -show_entries format=duration -of csv=p=0 voiceover.mp3`), dann **alle `dur` linear normieren**: `faktor = audio_duration / sum(dur)`, jede `dur *= faktor`. Die Differenz *nur* auf die letzte Szene aufzuschlagen ist zu vermeiden — ein Bild, das plötzlich 2 s länger steht als der Rest, fällt sichtbar auf. Lineare Normierung verteilt die (typisch kleine) Korrektur unsichtbar über alle Clips. Diese Korrektur passiert einmalig direkt vor dem Rendern, nicht im gespeicherten Plan.

**Grenze der geschätzten Timeline (ehrlich benannt):** Lineare Normierung hält **Anfang und Ende** synchron, garantiert aber nicht die **Mitte** — weicht die wpm-Schätzung stellenweise stark von der echten Sprechgeschwindigkeit ab, kann der Schnitt innerhalb des Videos gegen die Stimme verrutschen. Das ist die inhärente Grenze der geschätzten Timeline und die eigentliche Motivation, später Wort-Timestamps (lokal via `faster-whisper`, siehe Errata oben) nachzurüsten (Abschnitt 9, Punkt 5). Für V1 ist die Normierung die beste verfügbare Methode.

### 3.7 Datenmodell-Erweiterung (`plan.json`, additiv, Fortsetzung von Abschnitt 2.3)

```jsonc
{
  // ... bestehende + Sequenz-Felder aus 2.3 ...
  "motion": {"type": "zoom_in", "z_end": 1.12, "focus": [0.5, 0.45]},
  "clip_file": "005.mp4"
},
// auf Plan-Ebene:
"audio_duration": 187.4,
"render": {"file": "final.mp4", "ts": 1730000000,
           "checks": {"duration_ok": true, "audio_ok": true, "frames_ok": true}}
```

### 3.8 Render-Job (fünfter Job nach bestehendem Muster)

**Neue globale Struktur**, direkt neben `BATCH_JOBS` (Zeile ~46):
```python
RENDER_JOBS = {}                      # (cid,vid) -> {running, stage, done, total, error, file}
_RENDER_JOBS_LOCK = threading.Lock()
```

**Neue Funktion** `_render_worker(cid, vid)`, strukturell wie `_batch_generate_worker`:
```
stage "prepare":  Plan laden, Sync-Invariante (3.6) anwenden, audio_duration per ffprobe
stage "motion":   pro Szene ohne motion-Feld eines generieren (3.5)
stage "clips":    pro Szene sequenziell den Ken-Burns-Clip rendern (3.3) -- done/total-Fortschritt
stage "assemble": concat-Demuxer (3.4)
stage "audio":    Voiceover muxen (3.6)
stage "review":   ffprobe-Selbstprüfung (3.9); bei Fehler -> Status "error" mit Grund
final:            plan["render"] schreiben, RENDER_JOBS[key]["running"]=False, file="final.mp4"
```
Läuft **sequenziell** (ein FFmpeg-Prozess nach dem anderen) — kein Thread-Pool über FFmpeg, sonst CPU-Überzeichnung. Kooperatives Abbrechen via `stop_requested`-Flag, wie bei `BATCH_JOBS`.

### 3.9 Post-Render-Selbstprüfung

Nach dem Render `ffprobe` laufen lassen und in `plan["render"]["checks"]` schreiben:
- **duration_ok:** `abs(video_dur - audio_duration) < 0.3s`
- **audio_ok:** Audiospur vorhanden, nicht stumm (`ffprobe`/`astats`-Mittelwert nicht `-inf`)
- **frames_ok:** kein Clip mit 0 Byte / fehlgeschlagener Erstellung

Bei Fehlschlag: Status `"error"` mit konkretem Grund statt stillem Erfolg — gleiche Philosophie wie `_validate_image_prompt_entry` + Retry, nur eine Ebene höher (am fertigen Video statt am Prompt).

### 3.10 Neue HTTP-Routen

| Route | Methode | Zweck |
|---|---|---|
| `/api/render_start` | POST | `_render_worker` als Daemon-Thread starten (atomare "läuft schon?"-Prüfung wie bei `generate_all_start`) |
| `/api/render_status` | GET | Polling: `{running, stage, done, total, error, file}` |
| `/api/render_stop` | POST | `stop_requested`-Flag setzen |
| `/generated/final.mp4` | GET | Auslieferung — läuft über den bereits existierenden `/generated/`-Datei-Handler, keine neue Route nötig |

### 3.11 Easing — weiche Kamerabewegung ohne externes Tool (Phase 2)

`zoompan` fährt mit einem linearen `z`-Ausdruck (`zoom+0.001`) mit konstanter Geschwindigkeit — das wirkt mechanisch („Roboter-Kamera"). Abhilfe **ohne** externe Bibliothek: den `z`-Ausdruck als **nicht-lineare Funktion des Frame-Index `on`** formulieren. Eine Smoothstep-Kurve (`3t²−2t³`, `t = on/frames`) erzeugt sanftes Ein- und Ausblenden der Bewegung:

```python
frames = round(dur * FPS)          # z.B. 93
z0, z1 = 1.0, 1.0 + intensity      # z.B. 1.0 -> 1.12
# absoluter Ausdruck NUR aus on/frames (nicht die interne zoom-Variable nutzen -> deterministisch)
z_expr = f"{z0}+({z1-z0})*(3*pow(on/{frames},2)-2*pow(on/{frames},3))"
# im Filter:  zoompan=z='{z_expr}':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1920x1080:fps={FPS}
```
- **Wichtig:** den Ausdruck aus `on`/`frames` bauen, **nicht** aus der internen `zoom`-Variable (die hält den geclampten Vorframe-Wert → Rundungsdrift). So ist die Bewegung pro Frame deterministisch.
- Für Zoom-out `z0`/`z1` tauschen; für Pans dieselbe Smoothstep-Hüllkurve auf `x`/`y` anwenden.
- Kosten: **null** außer der Formel. Ertrag: der mechanische Look verschwindet. Deshalb Teil von Phase 2, nicht später.
- Referenz-Projekt (nur zur Orientierung, **nicht** einbinden): `scriptituk/xfade-easing` generiert genau solche Easing-Ausdrücke inkl. CSS-Bezier — wir erzeugen die Formel selbst in Python, um Zero-Framework zu bleiben.

### 3.12 Sound-Design & Musik (Phase 2.5)

**Alles reines FFmpeg** — `sidechaincompress`, `adelay`, `amix`, `acrossfade`, `loudnorm`. Kein neues Paket.

**Ducking (Musikbett unter Voiceover):**
```bash
ffmpeg -i voiceover.mp3 -i music_bed.mp3 -filter_complex "\
  [0:a]asplit=2[voice][sc];\
  [1:a][sc]sidechaincompress=threshold=0.02:ratio=10:attack=50:release=500[ducked];\
  [voice][ducked]amix=inputs=2:duration=first[a]" -map "[a]" mixed_narration.mp3
```

**SFX exakt auf einem Schnitt platzieren** (Woosh bei Sekunde `start` einer Sequenz-Grenze), mehrere zusammen:
```bash
# narration = bereits gedückt (oben); sfx zeitlich per adelay ans Szenen-`start` gesetzt
ffmpeg -i mixed_narration.mp3 -i whoosh_01.wav -i impact_01.wav -filter_complex "\
  [1:a]adelay=12000|12000,volume=0.7[s1];\
  [2:a]adelay=48000|48000,volume=0.8[s2];\
  [0:a][s1][s2]amix=inputs=3:duration=first,loudnorm[a]" -map "[a]" final_audio.mp3
```
Dieses `final_audio.mp3` ersetzt dann im Mux-Schritt (3.6) die rohe `voiceover.mp3`.

**Platzierungslogik (der eigentliche Denk-Teil, regelbasiert):**
- Woosh/Transition-SFX: an jeder Szene mit `seq_pos == 0` **und** Vorgänger aus anderer `seq_id` (echter Sequenz-/Szenenwechsel) — die Info liefert Feature A bereits.
- Riser/Impact: an Kapitelgrenzen (falls Kapitel-Info im Skript/Plan vorhanden) oder an den stärksten `emotional_arc`-Wechseln aus `analyze_script`.
- Zeitpunkt = `scene["start"]` (bzw. `start_aligned` ab Phase 3) × 1000 ms für `adelay`.
- **Grob schon in Phase 2.5 möglich; frame-genau erst mit Phase 3** (Whisper-Wort-Timestamps). Sound-Design und Whisper-Timing verstärken sich — deshalb Phase 2.5 grob, Phase 4 tight.

**Trade-off:** Ein zusätzlicher Audio-Pass vor dem finalen Video-Mux. Vernachlässigbar (Audio-Only-FFmpeg ist schnell). Vorteil: der Video-Mux (3.6) bleibt unverändert, er bekommt nur eine andere Audio-Datei.

---

## 4. Frontend-Integration (`dashboard.html`)

Alle Änderungen additiv, im bestehenden Vanilla-JS-Stil (`ch_api`/`ch_get`-Helper nutzen, kein neues Build-Tooling).

1. **Toolbar erweitern** (Zeile ~386-394, neben `genAll()`/`downloadZip()`):
   ```html
   <button onclick="renderVideo()" id="renderBtn">🎬 Video rendern</button>
   <button onclick="stopRender()" id="stopRenderBtn" class="ghost" style="display:none">⏹ Stoppen</button>
   ```
2. **Neue Poll-Funktion** `startRenderPoll()`, 1:1 nach Vorbild von `startBatchPoll()` (Zeile 736): pollt `/api/render_status` alle 2s, aktualisiert einen Fortschrittsbalken (`stage` + `done`/`total`), stoppt sich bei `running:false`.
3. **`renderVideo()`**: POST an `/api/render_start`, dann `startRenderPoll()` aufrufen — Muster identisch zu `genAll()` (Zeile 1270).
4. **Video-Vorschau** nach erfolgreichem Render: unterhalb der Toolbar ein
   ```html
   <video controls src="/generated/final.mp4?t=${Date.now()}"></video>
   ```
   (Cache-Buster-Timestamp wie bei den Bild-Vorschauen üblich.)
5. **Reload-Sicherheit:** `openVideo()` (Zeile 564) erweitern — beim Öffnen eines Videos zusätzlich `/api/render_status` prüfen und bei laufendem Job `startRenderPoll()` automatisch wieder aufnehmen, konsistent mit dem bestehenden Verhalten für Plan-/Batch-Jobs.
6. **Sequenz-Badges** in `renderScenes()` (Zeile 940), siehe Abschnitt 2.8.
7. **Optional, nicht V1:** Fortschrittsanzeige mehrstufig (Bilder → Motion → Clips → Zusammenschnitt → Audio → Prüfung) statt nur eines Balkens, sobald `stage`-Feld im Status genutzt wird.

---

## 5. Neue/geänderte Backend-Funktionen — Kompaktübersicht

| Funktion | Datei-Bereich | Status |
|---|---|---|
| `analyze_script` | ~564 | **ändern** — `visual_sequences`-Feld ergänzen |
| `_apply_sequences(scenes, analysis)` | neu, nahe Plan-Erstellung | **neu** |
| `_resolve_chain_refs(fresh_plan, scene)` | nahe `_batch_generate_worker` | **neu** |
| `_batch_generate_worker` | ~1180 | **ändern** — Ketten-Refs einbauen (2.6) |
| `_build_image_prompt` bzw. Chunk-Aufbau | ~1039 | **ändern** — Continuity-Hinweis (2.7) |
| `_motion_for_scene(scene, prev, next)` | neu | **neu** |
| `_render_clip(scene, out_path)` | neu | **neu** — FFmpeg-Ken-Burns-Aufruf (3.3) |
| `_assemble_clips(clip_paths, out_path)` | neu | **neu** — concat-Demuxer (3.4) |
| `_mux_audio(silent_path, audio_path, out_path)` | neu | **neu** (3.6) |
| `_render_selfcheck(final_path, plan)` | neu | **neu** — ffprobe-Checks (3.9) |
| `_render_worker(cid, vid)` | neu | **neu** — Orchestriert 3.8 |
| `RENDER_JOBS`, `_RENDER_JOBS_LOCK` | neu, nahe `BATCH_JOBS` | **neu** |
| Routen `/api/render_start`, `_status`, `_stop` | in `do_POST`/`do_GET` | **neu** |

---

## 6. Explizite Guardrails

- Kein neues pip-Paket außer ggf. für Testzwecke — Kernpfad läuft mit Python-Stdlib + `subprocess`.
- `ffmpeg` wird als externes Binary vorausgesetzt, kein Vendoring, kein Download-Automatismus.
- Alle neuen Felder in `plan.json` sind **optional/additiv** — bestehende Pläne ohne diese Felder müssen weiterhin fehlerfrei ladbar sein (`scene.get("seq_id")`, nicht `scene["seq_id"]`).
- Kein Eingriff in `gen_veo`/`extend_veo`/`gen_video`/`gen_t2v`/`make_t2v_prompt` und die zugehörigen Routen.
- Rendering läuft **sequenziell** (ein FFmpeg-Prozess gleichzeitig pro Render-Job) — keine parallele Prozess-Flut ohne vorherige Messung.

### 6.1 Warum kein Remotion/Editly/Agent-Orchestrierung (Begründung, nicht nur Verbot)

Diese Guardrails sind das Ergebnis einer bewussten Evaluation, nicht nur eine Stilvorliebe — wichtig, damit an schwierigen Implementierungsstellen nicht versehentlich einer dieser Wege wieder vorgeschlagen wird:

- **Kanäle lösen Stiltrennung bereits.** Das Projekt hat schon ein Mehr-Stil-Konzept (`channels/<cid>/`, je eigener `master_prompt.txt` + Charakter-Referenz). Ein neues System für „mehrere visuelle Stile" ist nicht nötig.
- **Agent-orchestrierte Video-Pipelines (z. B. OpenMontage) haben ein belegtes Konsistenz-Problem.** Aktuelle Forschung zu agentischer Video-Produktion beschreibt „catastrophic semantic forgetting" und „cascading failures" bei Agent-Orchestrierung über mehrere Szenen — Charakter-/Stil-Drift, schwer bis zum verursachenden Schritt zurückverfolgbar. Das bestehende System (feste Analyse → Validierung → Retry, siehe `_validate_image_prompt_entry`) ist bereits die Architektur, die gegen genau dieses Problem robust macht: probabilistische Generierung in einen deterministischen Prozess eingehegt, nicht ein Agent, der jeden Schritt neu entscheidet.
- **Remotion/Editly sind Node.js/React.** Selbst als „nur Render-Engine" gedacht, brechen sie die Ein-Sprache/Zero-Framework-Regel genau an der Stelle, wo Determinismus am wichtigsten ist (Rendering). Reines FFmpeg via `subprocess` erreicht dieselben visuellen Ergebnisse (Ken Burns, Übergänge, Text-Overlays) nachweislich auch bei etablierten Konkurrenzprojekten (z. B. MoneyPrinterTurbo, Autotube) — keines davon braucht Node für den Kern-Schnitt.
- **Konsequenz für zukünftige Entscheidungen:** Bei jeder neuen Anforderung, die „schwer in FFmpeg/Stdlib zu bauen" scheint, ist die richtige Reaktion, nach dem *nächsten reinen FFmpeg-Filter/Stdlib-Baustein* zu suchen (wie in 3.11 für Easing geschehen), nicht nach einer externen Bibliothek. Nur wenn das nachweislich nicht geht, mit dem Nutzer eine bewusste Ausnahme besprechen — nie stillschweigend ein Framework einführen.

---

## 7. Manuelle Verifikationsschritte (vor bzw. während der Umsetzung)

```bash
# FFmpeg vorhanden und Version?
ffmpeg -version

# Ken-Burns-Rezept isoliert testen (mit einem echten generierten Bild aus channels/.../generated/):
ffmpeg -y -loop 1 -i test.jpg -t 3 -r 30 \
  -filter_complex "scale=3840:-2,zoompan=z='min(zoom+0.001,1.1)':d=90:s=1920x1080:fps=30" \
  -c:v libx264 -pix_fmt yuv420p test_out.mp4
# -> test_out.mp4 abspielen, auf Ruckeln prüfen

# ffprobe-Dauer-Check:
ffprobe -v error -show_entries format=duration -of csv=p=0 voiceover.mp3
```

---

## 8. Bauplan — Phasen (der Reihe nach, je einzeln testbar)

### Phase 1 — Bild-Sequenzen (Feature A)
1.1 `analyze_script`-Prompt um `visual_sequences` erweitern.
1.2 `_apply_sequences` schreiben (direkter Index-Zugriff `scenes[beat_i]` mit Bounds-Check — Mapping ist geklärt, siehe 9.1) und in die Plan-Erstellung (~Zeile 2030/2222) einhängen, direkt nach dem `analyze_script`-Aufruf.
1.3 `_resolve_chain_refs` schreiben, in `_batch_generate_worker` einbauen inkl. URL-Ablauf-Fallback.
1.4 Continuity-Prompt-Zusatz für Fortsetzungsszenen.
1.5 Frontend: Sequenz-Badges in `renderScenes()`.
**Definition of Done:** Ein Skript mit einer erkennbaren "langen Passage über einen Ort" erzeugt beim Plan eine `seq_id`-Gruppe; beim Generieren zeigen Fortsetzungsbilder sichtbar denselben Look/Charakter/Setting wie der Anker.

### Phase 2 — Basis-Renderer mit geschätzter Timeline (Feature B, harte Schnitte)
2.1 `_motion_for_scene` (regelbasiert).
2.2 `_render_clip` (Ken-Burns pro Szene, 3.3) — **inkl. Easing-Ausdruck (3.11)**. Easing ist quasi kostenlos (nur die `z`-Formel) und behebt den mechanischen „Roboter-Kamera"-Look sofort, deshalb direkt in V1 statt später.
2.3 `_assemble_clips` (concat, 3.4) + `_mux_audio` (3.6) + Sync-Invariante.
2.4 `RENDER_JOBS`/`_render_worker`/Routen (3.8/3.10).
2.5 `_render_selfcheck` (3.9).
2.6 Frontend: Render-Button, Poll, Vorschau (4.1-4.4).
**Definition of Done:** Ein Klick auf „🎬 Video rendern" erzeugt `final.mp4` mit **weich einsetzender** Ken-Burns-Bewegung, hartem Schnitt pro Szene und synchronem Voiceover; `ffprobe`-Checks sind grün.

### Cinematic-Roadmap — vom Gerüst zur „Magie" (nach Kosten/Nutzen gestaffelt)

V1 (Phase 2) liefert eine saubere, timing-korrekte Dokumentar-Diashow mit weichem Easing — aber noch nicht den vollen hochdynamischen Schnitt-Look. Die folgenden Phasen holen das „Cinematic" nach, **sortiert nach Aufwand/Ertrag** (nicht nach optischer Wichtigkeit) — so kommt der größte sichtbare Sprung pro investierter Stunde zuerst. Jede Phase setzt auf einem stabilen Vorgänger auf.

**Phase 2.5 — Sound-Design-Layer (mittel, sehr hoher wahrgenommener Sprung)**
Größter „klingt-professionell"-Effekt nach dem Easing. Setzt nur ein lauffähiges V1 voraus, **nicht** Scribe.
- 2.5.1 Kuratierter lokaler Asset-Pool `assets/sfx/`, `assets/music/` + `assets/CREDITS.txt` (Quellen/Lizenzen — siehe Abschnitt 10).
- 2.5.2 Musikbett unter das ganze Video, mit `sidechaincompress`-Ducking unter das Voiceover (3.12).
- 2.5.3 Grobe SFX-Platzierung regelbasiert: Woosh bei jedem Sequenz-Wechsel (`seq_id` ändert sich — die Info liefert Feature A bereits), Riser/Impact an Kapitelgrenzen. Platzierung via `adelay` an den Szenen-`start` (3.12).
- 2.5.4 `loudnorm`-Normalisierung des finalen Mixes (einheitliche Lautheit).
**Definition of Done:** `final.mp4` hat ein gedücktes Musikbett und hörbare Übergangs-SFX an Sequenzgrenzen.

**Phase 3 — Frame-genaues Timing (mittel, hoher Nutzen, Voraussetzung für tighten Schnitt)**
Behebt die „Mitte driftet"-Grenze der geschätzten Timeline (3.6) und macht wort-/beatgenaue Schnitte und SFX überhaupt erst möglich.

**Update (siehe Errata oben):** Ursprünglich als „ElevenLabs Scribe über KIE" geplant — live getestet und verworfen (Task blieb dauerhaft auf `"waiting"` hängen, 0.12 Credits verbraucht, kein Ergebnis). Gemini als Alternative verworfen (halluziniert bei Timestamps, für reinen Transkript-Text aber ok). Stattdessen: **lokales `faster-whisper`**, Modell "small", `word_timestamps=True` — offline, kostenlos nach Setup, kein Rate-Limit, ~500 MB Modell-Download, ~1 GB RAM während der kurzen Transkriptions-Burst-Phase (kein Dauerlast).

- 3.1 `faster-whisper` als Abhängigkeit installieren, lokale Transkriptionsfunktion mit `word_timestamps=True` bauen (nimmt die bereits hochgeladene Voiceover-Datei, kein neuer Upload-Schritt nötig — die Datei liegt durch den bestehenden `/api/transcribe`-Flow schon lokal vor).
- 3.2 Alignment Skript↔Transkript: Whisper liefert Wort-für-Wort-Timestamps für das, was tatsächlich gesagt wurde; das bestehende Skript/die Szenen-Texte werden pro Szene den passenden Whisper-Wörtern zugeordnet (sequenzielles Fenster-Matching, da Reihenfolge identisch ist — kein Fuzzy-Matching nötig, nur Wortanzahl-Verschiebung durch evtl. Whisper-Transkriptionsfehler abfangen). Ergebnis: `start_aligned`/`end_aligned` in `plan.json`.
- 3.3 Renderer bevorzugt `*_aligned`-Werte, falls vorhanden (sonst Fallback auf die geschätzte Timeline aus Phase 2); SFX aus Phase 2.5 rasten jetzt auf echte Wortgrenzen/Betonungen statt auf geschätzte Zeiten.
**Definition of Done:** Ein echtes Voiceover wird lokal per Whisper transkribiert, `plan.json` enthält `start_aligned`/`end_aligned` pro Szene, und Schnitte sitzen hörbar auf Satzenden/Betonungen statt „ungefähr".

**Phase 4 — Micro-Dynamics & Übergänge (gemischt, meist billig)**
- 4.1 ✅ **erledigt** (siehe `ARCHITECTURE.md` Abschnitt 15.1) — `xfade`-Übergänge an Sequenz-/Szenengrenzen statt überall hartem Schnitt, ABER nicht als eigene Easing-Formel: ffmpegs `xfade`-Filter bringt bereits 58 fertige Übergangstypen mit, kuratiert zu drei Familien (`fade`/`wipe`/`smooth`), Auswahl regelbasiert über das vorhandene `pacing`-Feld (`TRANSITION_LIBRARY`/`_transition_for_scene` in dashboard.py).
- 4.2 ✅ **erledigt** — Impact-Akzent auf harten (nicht-übergangs-) Schnitten bei `pacing=="punchy"`, frame-genau auf `start_aligned` (Whisper) statt geschätzt. Nutzt das seit Phase 2.5 ungenutzte `impact`-SFX-Asset. Zusätzlich: Übergangs-SFX (`whoosh`/keins) ist jetzt an dieselbe Auswahlfunktion gekoppelt wie der visuelle Übergangstyp, nicht mehr unabhängig fest verdrahtet.
- 4.3 Reload-Wiederaufnahme für Render-Jobs in `openVideo()` (4.5), mehrstufige Fortschrittsanzeige (4.7).
- 4.4 ✅ **erledigt** (siehe `ARCHITECTURE.md` Abschnitt 18) — Text-Overlays (Untertitel, Zahlen-Callouts, Kapitel-Titel) mit Ein-/Ausblenden, alle drei standardmäßig aus. Abweichung vom Plan: nicht `drawtext` (der installierte ffmpeg-Build hat kein freetype/fontconfig kompiliert), sondern Pillow-gerenderte PNGs + ffmpegs `overlay`/`fade`-Filter, isolierte venv wie Phase 3.

**Phase 4.5 — "Ein-Knopf"-Orchestrator — ✅ erledigt (siehe `ARCHITECTURE.md` Abschnitt 17)**
Ursprünglicher Nutzerwunsch: „Skript oder Audio rein → auf einen Klick alle Bilder generieren, zusammenschneiden, Sound drüber, fertiges Video." Umgesetzt exakt wie hier skizziert, mit einer Abweichung: keine eigene `"timing"`-Stufe im Orchestrator — Whisper-Alignment läuft bereits automatisch INNERHALB der `"render"`-Stufe (siehe Abschnitt 16.5-Korrektur: Alignment gehört an den Render-Zeitpunkt, nicht davor, damit sowohl Audio-Transkription als auch manueller Skript-Pfad gleichermaßen profitieren). Tatsächliche Etappen: `"plan"` (Resume-fähig — übersprungen, wenn schon ein Plan existiert; sonst Audio-Transkription oder manueller Skript-Text, je nachdem was vorliegt) → `"images"` (`_batch_generate_worker`, Resume-fähig) → `"render"` (`_render_worker`, inkl. Whisper-Timing/Übergänge/Sound-Design/Impact-Akzente). Voraussetzung war die Extraktion von `_transcribe_generate_worker` aus dem bis dahin inline im HTTP-Handler vergrabenen `/api/transcribe`-Code. Frontend: „🚀 Alles auf einmal"-Karte direkt nach Schritt ② (nicht neben dem Render-Button — sie ersetzt mehrere nachfolgende Schritte auf einmal, das verdient einen eigenen, hervorgehobenen Platz weiter oben), mit dreistufiger Fortschrittsanzeige (Plan → Bilder → Rendern), Stop-Propagation in laufende Sub-Jobs, Reload-Sicherheit.
**Definition of Done — erfüllt:** End-to-End über die echte Browser-UI getestet (nicht nur die API): Skript eingetippt, Klick, korrekter Abbruch in der Render-Etappe ohne Voice-over, danach Audio nachgereicht, zweiter Klick übersprang Plan+Bilder (Resume bestätigt) und lieferte ein echtes, abspielbares `final.mp4`. Nebenbefund beim Testen behoben: `suggestCharsFromPlan()` im Frontend erwartete ein falsches Datenmodell (`ch.name` statt `ch.name_or_role`) und crashte lautlos bei jedem Plan mit erkannten Charakteren — betraf auch das bestehende `openVideo()`, nicht nur den neuen Orchestrator.

**Phase 5 — Parallax / 2.5D (teuer, fragil, zurückgestellt)**
Bewusst ans Ende: echter Parallax braucht eine Tiefenschätzung (Depth-Map via ML-Modell — tangiert die Zero-Framework-Regel, da lokales ML) oder manuell freigestellte Vordergrund-Layer, dann getrennte Ebenen-Bewegung. Höchster Aufwand, unsicherster Ertrag — besonders bei flächigem Ink/Stickman-Stil schwächer sichtbar als bei fotorealistischen Essays. **Erst angehen, wenn Phasen 2.5–4 den Look bereits tragen** und ein konkreter Bedarf bleibt. Kein V1-Thema.

---

## 9. Offene Fragen — Status

1. **Beat→Szene-Mapping in `segment()` — GEKLÄRT (im echten Code verifiziert).** `analyze_script` wird mit `[s["text"] for s in scenes]` aufgerufen (dashboard.py ~Zeile 2030 und ~2222), also **bereits mit den segmentierten Szenen-Texten**, nicht mit den feingranularen `units`. Damit gilt **Beat-Index = Szenen-Index, 1:1** — `_apply_sequences` kann direkten Index-Zugriff (`scenes[beat_i]`) nutzen, abgesichert nur durch den Bounds-Check `0 <= beat_i < len(scenes)`. **Kein Substring-/Token-Matching nötig.** (Der `visual_sequences`-Prompt muss dem LLM lediglich klarmachen, dass die `beats`-Indizes 0-basiert der übergebenen Szenen-Liste entsprechen.)
2. **FFmpeg-Installation:** `ffmpeg -version` auf dem Zielrechner verifizieren (der bestehende Code in `_veo_job_worker` setzt es implizit voraus).
3. **Ziel-Framerate/Auflösung — ENTSCHIEDEN:** In V1 **fix 1080p/30fps als globale Backend-Konstante**. Uneinheitliche Frameraten killen den `concat`-Demuxer. Pro-Kanal-Konfigurierbarkeit erst nach V1, falls überhaupt nötig.
4. **Clip-Aufbewahrung — ENTSCHIEDEN:** In Phase 2 die Clips nach erfolgreichem Muxing **und** bestandenem `_render_selfcheck` löschen (`shutil.rmtree(render_tmp_dir)`). **Sicherheitsauflage (kritisch):** Die Clips liegen zwingend in einem **separaten** Verzeichnis (`videos/<vid>/render_tmp/`), **niemals** in `generated/` — sonst löscht `rmtree` die generierten Bilder mit. **Trade-off-Notiz:** Ohne Clip-Cache muss jeder erneute Render alle Ken-Burns-Clips neu erzeugen (auch bei nur einer geänderten Szene). Ein Hash-basierter Cache (Clip nur neu rendern, wenn sich Bild/motion/dur geändert haben) lohnt erst später und braucht dann ein `clip_hash`-Feld in `plan.json`.
5. **Präzises Wort-Timing (Phase 3) — Quelle geklärt (siehe Errata oben):** bewusst nicht Teil von Phase 1/2 (die nutzen die geschätzte Timeline aus `segment()`/`transcribe_and_segment` + lineare Normierung aus 3.6). Späterer Baustein zur Behebung der in 3.6 benannten „Mitte-driftet"-Grenze: Wort-Timestamps via **lokales `faster-whisper`** (nicht mehr ElevenLabs Scribe — über KIE live getestet und als nicht funktionsfähig verworfen), Alignment-Schritt Skript↔Transkript. Ausgeklammert aus Phase 1/2, um diese klein und schnell testbar zu halten.

---

## 11. Was noch kommt — bekannte Lücken über diesen Plan hinaus

Dieser Plan deckt zwei konkrete Features (Bild-Sequenzen, Rendering) plus deren cinematische Vertiefung ab. **Er ist nicht das Ende der Roadmap.** Folgendes ist bewusst *nicht* Teil dieses Dokuments, aber bekannt und relevant für zukünftige Arbeit an derselben Codebasis — Claude Code sollte diese Punkte kennen, um keine Entscheidungen zu treffen, die sie erschweren:

### 11.1 Code-Qualität / Wartbarkeit der wachsenden Monolith-Dateien
Dieser Plan **vergrößert** `dashboard.py` (ca. 15 neue Funktionen) und `dashboard.html` weiter, ohne die Zero-Framework-Regel zu verletzen — adressiert aber nicht die Wartbarkeit der wachsenden Einzeldatei. Zwei konkret im bestehenden Code sichtbare Kandidaten für spätere Aufmerksamkeit (nicht Teil dieses Plans):
- **`JOBS`-Dict wächst unbegrenzt.** Jeder Bild-Job (`job_id = f"{cid}_{vid}_{i}_{int(time.time())}"`) bleibt für die Lebensdauer des Prozesses im Speicher — langsames Memory-Leak bei langer Serverlaufzeit. Ein einfacher periodischer Cleanup (Einträge älter als N Stunden entfernen) wäre eine spätere, in sich geschlossene Verbesserung.
- **`MAX_CONCURRENT_IMAGE_GENS = 2`** koordiniert nicht mit dem neuen Render-Job — wenn Bildgenerierung (Video A) und Rendering (Video B) gleichzeitig laufen, konkurrieren beide um CPU ohne gemeinsames Limit. Für den aktuellen Ein-Nutzer-Kontext unkritisch, aber ein bekannter blinder Fleck.
- **Eine Aufteilung von `dashboard.py` in mehrere Dateien** (z. B. `kie_client.py`, `render.py`, `scenes.py`, importiert von einem schlanken `dashboard.py`) wäre ohne jedes Framework möglich (reine Python-Module) und würde die Wartbarkeit deutlich erhöhen — war aber nie Teil der bisherigen Anforderungen und ist hier bewusst nicht angegangen, um den Fokus auf die zwei Features zu halten.

### 11.2 Architektur-Entscheidung dokumentiert, nicht nur das Ergebnis
Siehe Abschnitt 6.1 (neu) — die Begründung, warum Remotion/Editly/Agent-Orchestrierung bewusst abgelehnt wurden, ist jetzt im Dokument, nicht nur das nackte Verbot.

### 11.3 Phasen, die in diesem Dokument stehen, aber noch nicht umgesetzt sind
Zur Erinnerung, damit nichts als „schon erledigt" missverstanden wird: **Phase 1, 2 und 2.5 sind umgesetzt und end-to-end verifiziert** (siehe `ARCHITECTURE.md`). Phase 3 (Whisper-Timing, nicht mehr Scribe — siehe Errata oben), 4 (Micro-Dynamics, Crossfades bereits teilweise umgesetzt), 4.5 (Orchestrator) und 5 (Parallax) sind konzeptionell fertig geplant, aber Phase 3 ist die aktuell laufende Arbeit und 4.5/5 folgen erst danach (siehe Cinematic-Roadmap, Abschnitt 8). Reihenfolge nicht vertauschen — jede Phase baut auf einem getesteten Vorgänger auf.

### 11.4 Nicht diskutiert, potenziell relevant für später (unvollständige Liste, kein Auftrag)
- Mehrsprachigkeit über Deutsch/Englisch hinaus (aktuell nur `SCRIPT_SYSTEM`/`setLang('de'|'en')`).
- Backup/Versionierung von `plan.json` (aktuell wird bei jedem Schreibvorgang überschrieben, kein Verlauf).
- Mobile-taugliches Frontend-Layout (aktuell nicht thematisiert, `dashboard.html` scheint Desktop-orientiert).

---

## 10. Sound- & Musik-Assets — Bezug, Lizenz, Ablage (für Phase 2.5)

**Prinzip:** Kein Live-API-Abruf zur Laufzeit, sondern ein **einmal kuratierter, lokaler Pool**. Deterministisch, offline, keine Rate-Limits, keine Lizenz-Überraschung zur Renderzeit.

**Empfohlene Quellen (kostenlos, kommerziell nutzbar, dauerhaft):**

| Quelle | Lizenz | Rolle |
|---|---|---|
| **Pixabay** (Musik + SFX) | Pixabay-Lizenz — keine Attribution, kommerziell erlaubt | Erste Wahl für Musik-Betten + Standard-SFX (Whoosh, Impact). Sauberste Bedingungen. |
| **Freesound** (auf **CC0** filtern) | CC0 = Public Domain, keine Auflagen | Größte SFX-Auswahl (Whoosh, Shatter, Glitch, Riser). **Zwingend CC0-Filter** — andere Sounds sind CC-BY (Attribution nötig) und damit für einen automatisierten Pool unpraktisch. |
| **Mixkit** | eigene Lizenz — kommerziell ok, Attribution optional | Ergänzung. |
| Epidemic Sound / Artlist | Abo-pflichtig | **nicht** verwenden (widerspricht „kostenlos/dauerhaft"). |

**Rechtliche Sorgfalt (auch bei CC0):** Für jedes heruntergeladene File Herkunft + Lizenz in `assets/CREDITS.txt` festhalten (Dateiname, Quelle-URL, Lizenz, Autor falls angegeben). CC0 verlangt keine Attribution, aber die Dokumentation der Provenienz schützt bei späteren Rückfragen. Keine „kostenlos"-Sounds aus unklaren Sammlungen (YouTube-Rips o. Ä.) — nur die oben genannten, klar lizenzierten Quellen.

**Ablage-Struktur (projektweit, nicht pro Kanal — Sounds sind stiluniversell):**
```
assets/
  music/     tension_bed.mp3, calm_bed.mp3, neutral_bed.mp3
  sfx/       whoosh_01.wav, impact_01.wav, riser_01.wav, glitch_01.wav, shatter_01.wav
  CREDITS.txt
```
Der Renderer referenziert diese Dateien über feste Namen bzw. Kategorien. Optional pro Kanal in `master_prompt`-Nähe ein „Sound-Profil" (welches Musikbett, welche SFX-Intensität) — analog zum visuellen Master-Prompt, aber erst wenn Phase 2.5 steht.

**Bewusst kein dynamischer Freesound-API-Abruf in V1:** brächte Lizenz-Tracking pro Request, Rate-Limits und Nichtdeterminismus. Ein fester Pool ist für einen wiederkehrenden Kanal-Stil die robustere Wahl.
