# Prompt-Pipeline: Skript → Prompts → Bilder

Stand: Juli 2026. Dokumentiert die Kette von "Skript einfügen" bis "fertiges Bild bei
KIE angekommen" und alle Fixes, die an einem Tag daran vorgenommen wurden, nachdem
mehrere zusammenhängende Bugs zu Stilbrüchen und einem ungewollt un-anonymisierten
Klarnamen im Bild-Prompt geführt hatten.

## 1. Überblick: die vier Stufen

```
① Skript-Text
      │  POST /api/generate_script (Gemini, synchron)
      ▼
② Szenen + Bild-Prompts  ──  /api/plan → _plan_generate_worker (Worker-Thread)
      │
      │  Stufe 1: analyze_script(units)         → EINE Charakter-/Orts-/Symbol-Liste
      │           (characters, locations, recurring_symbols, emotional_arc, pacing, …)
      │
      │  Stufe 2: visual_prompts(scenes, analysis) → pro Szenen-Chunk (20 Szenen/Call)
      │           ein Bild-Prompt + concrete_entity, referenziert auf Stufe-1-IDs
      ▼
③ Bild-Generierung (pro Szene)
      │  Einzeln: POST /api/generate_one
      │  Alle:    POST /api/generate_all_start → _batch_generate_worker
      │           (ThreadPoolExecutor, bis zu 8 parallel, IMAGE_GEN_SEMAPHORE)
      ▼
④ KIE (nano-banana-2): Submit → Poll → Download → generated/NNN.jpg
```

Jede Stufe läuft serverseitig in einem Worker-Thread mit Status-Poll-Endpoint —
nichts blockiert mehr den HTTP-Request-Thread (siehe Abschnitt 6).

## 2. Wie ein Bild-Prompt zusammengesetzt wird

`_build_image_prompt()` in `engine/prompts.py` baut den finalen String, der an KIE
geht, in dieser Reihenfolge:

```
scene_prompt                                    # von visual_prompts(), Stufe 2
  + char_hint                                    # NUR bei entity-Match, siehe §3
  + phase_hint                                   # STYLE (OPENING/RISING_ACTION/CLIMAX/RESOLUTION)
  + hook_hint                                     # bei is_hook=True
  + "\n\n" + master                              # Kanal-Master-Prompt, siehe §4
```

Zusätzlich wird — abhängig vom Kontinuitäts-Fall — eine der beiden Texte angehängt
(siehe §5):

- `CONTINUITY (STRICT)` bei Sequenz-Fortsetzung (gleicher Shot)
- `CHARACTER CONTINUITY` bei wiederkehrendem Charakter (anderer Shot)

## 3. Referenzbilder: was heute grundlegend neu gebaut wurde

### Vorher (Bug-Zustand)

- Charsheets (eine ältere, im aktuellen UI nicht mehr erreichbare Multi-Charakter-
  Bibliothek — `switchTopTab()` wird nirgends mehr aufgerufen) wurden **kanalweit**
  geladen und **ungefiltert** in JEDEN Prompt injiziert — auch Leichen aus einem
  völlig anderen, abgeschlossenen Video im selben Kanal.
- Das eine globale Settings-Referenzbild (`char_ref_url`) wurde zusätzlich als
  **erzwungene Text-Charakterbeschreibung** behandelt ("this exact design wins") —
  seine Gemini-Vision-Beschreibung überstimmte damit die korrekte, szenen-eigene
  Beschreibung (Symptom: Prompt sagt "blonde Haare", generiertes Bild zeigt einen
  Mann mit braunen Haaren und Pullover — weil das Referenzbild zufällig einen
  komplett anderen Charakter zeigte).
- `/api/generate_one` (Einzelbild) hängte nur `char_ref_url` an, nie die
  Charsheet-Bilder — anders als der Batch-Worker. Einzeln und "Alle generieren"
  verhielten sich dadurch unterschiedlich.
- Referenzbild wurde nur an Szenen mit `concrete_entity.startswith("char_")`
  angehängt — Landschafts-/Symbol-Szenen hatten dadurch GAR keinen visuellen
  Stil-Anker und drifteten ab (u. a. Richtung Hyperrealismus).
- Bei wiederkehrenden Charakteren wurden **zwei** Referenzbilder gleichzeitig
  geschickt (das schon generierte Bild der Figur UND das globale Referenzbild) —
  KIE musste selbst gewichten, welchem es folgt → zufällige Inkonsistenz zwischen
  Generierungen derselben Szene.

### Jetzt

- **Charsheet-Text-Injection komplett entfernt.** Jede Bild-Anfrage besteht nur
  noch aus: Szenen-Prompt + Master-Prompt + **genau ein** Referenzbild.
- Das globale Referenzbild ist ein reiner **Stil-Anker** (Linienführung, Palette,
  Rendering-Technik), keine erzwungene Charakter-Identität mehr.
- **Cross-Szenen-Charakter-Kontinuität** (`_resolve_entity_ref` in
  `engine/scenes.py`): die erste generierte Szene eines Charakters wird bei jeder
  späteren Szene desselben Charakters als Anker mitgegeben — strikt pro Video
  isoliert, nie kanalübergreifend (verifiziert per Test).
- **Entweder/Oder-Regel** (nie zwei Referenzbilder gleichzeitig):
  1. Gibt es schon eine spezifischere eigene Referenz (`chain_refs` = gleicher
     Shot in einer Sequenz, `entity_refs` = erste Erscheinung desselben
     Charakters) → **nur** diese wird angehängt.
  2. Sonst, falls ein globales Referenzbild gesetzt ist → **das** wird angehängt.
  3. Referenzbild jetzt an **jede** Szene (nicht mehr auf `char_`-Entities
     beschränkt) — auch Landschafts-/Symbol-Szenen bekommen den Stil-Anker.
- `/api/generate_one` und `_batch_generate_worker` verwenden identische Logik.

Code: `dashboard.py` (`_batch_generate_worker`, `/api/generate_one`),
`engine/scenes.py` (`_resolve_entity_ref`, `_wait_for_entity_anchor_scene`).

## 4. Der Master-Prompt (Kanal-Stil)

**Vorher:** eine feste Stil-Bibel mit harten, konkreten Vorgaben (dicke
dunkelbraune Outlines, gedämpfte/entsättigte Palette, minimalistische
Punkt-Augen). Das stand im direkten Widerspruch zu jedem Referenzbild, das
selbst einen anderen Look hat (z. B. ein buntes, outline-loses Referenzbild) —
das Modell bekam zwei widersprüchliche Anweisungen gleichzeitig und entschied
bei jeder Generierung neu, zufällig, welcher es folgt. Genau das erzeugte die
beobachteten Stilbrüche zwischen einzelnen Versuchen derselben Szene.

**Jetzt:** zweistufig, in `channels/<kanal>/master_prompt.txt`:

1. **STYLE — driven by the attached reference image:** Linienstärke, Schattierung,
   Farbpalette und Charakterdesign sollen exakt dem Referenzbild folgen. Das
   Referenzbild ist die "single source of truth" fürs Aussehen.
2. **FALLBACK:** nur für Szenen ganz ohne Referenzbild (aktuell nicht mehr der
   Fall, siehe §3 — jede Szene bekommt jetzt eins, aber der Fallback bleibt als
   Absicherung, z. B. falls kein Referenzbild in den Settings gesetzt ist).
3. **STRUCTURE:** unabhängig vom Referenzbild — 16:9-Framing, Lichtführung,
   kein Text/Wasserzeichen im Bild, symbolische statt explizite Darstellung
   heikler Inhalte. Diese Regeln kann kein Bild transportieren, deshalb bleiben
   sie hart im Text.

⚠️ `channels/*/master_prompt.txt` ist Nutzerdaten (git-ignored), keine
Code-Änderung — der obige Text ist der aktuelle Stand für den Kanal `default`,
kein globaler Zwang.

## 5. Charakter-Identität vs. Skript-Text: der "Elizabeth Holmes"-Bug

**Befund:** `analyze_script()` (Stufe 1) lieferte für ein bestimmtes Skript eine
komplett leere `characters`-Liste zurück — bisher gab es dafür KEINEN Retry
(nur bei Exceptions/Parse-Fehlern). Zwei Folgeschäden:

1. **Inkonsistente Entity-IDs:** ohne die Stufe-1-Liste als gemeinsamer Anker
   erfand jeder Chunk-Aufruf (Stufe 2, 20 Szenen pro Call) unabhängig eine
   eigene ID für dieselbe Person — im konkreten Fall `char_elizabeth` in einem
   Chunk, `char_elizabeth_holmes` in einem anderen. Die neue Kontinuitäts-Logik
   (§3) matcht exakt auf den String und behandelte das als zwei unverbundene
   Charaktere → Stilbruch mitten im Batch-Run.
2. **Kein Anonymisierungs-Schutz:** die Regel "echte, identifizierbare Personen
   nie beim Klarnamen nennen, nur symbolisch/fiktionalisiert darstellen" hängt
   an der `characters`-Liste (`anonymize: true`). Leere Liste = Regel greift nie
   → der reale Name landete unfiltriert im Bild-Prompt.
3. Nebenbefund: in einer Szene stand sogar die rohe interne ID
   (`char_elizabeth_holmes`, mit Unterstrichen) wortwörtlich im sichtbaren
   Prompt-Text statt einer Beschreibung — ein bedeutungsloser Code-Schnipsel
   für das Bildmodell.

**Fixes (`dashboard.py`, `engine/prompts.py`):**

- `analyze_script()` wiederholt sich jetzt einmal, wenn `characters` bei einem
  nicht-trivialen Skript (≥5 Beats) leer zurückkommt.
- `_validate_image_prompt_entry()` erkennt jetzt, wenn die rohe Entity-ID
  (mit Unterstrichen) wörtlich im `image_prompt` auftaucht, und wirft den
  Eintrag in den bestehenden Single-Retry-Pfad.
- Bestehende Daten des betroffenen Videos wurden von Hand nachbereinigt (Name
  entfernt, Entity-IDs vereinheitlicht) — bereits generierte Bilder mit dem
  alten, fehlerhaften Prompt (Szenen 13/18/19) sollten neu generiert werden.

## 6. Nebenbei behoben: Async-Umbau + UX

- **`/api/generate_thumbnail` war komplett synchron** (KIE Submit+Poll+Download
  im Request-Thread, 30–60s) → Browser fror ein, ohne Feedback. Jetzt
  Worker-Thread + `/api/thumbnail_status`-Poll, gleiches Muster wie `/api/plan`.
- **Fortschrittsbalken bei Bildgenerierung** stand immer bei "0%" und sprang
  direkt auf fertig — KIE liefert für Bild-Jobs nie einen echten
  Zwischen-Progress-Wert. Jetzt eine plausible, monoton steigende Fake-Kurve
  clientseitig (basierend auf verstrichener Zeit), springt bei echtem Abschluss
  sofort auf 100%.

## 6b. Prompt-Generierung: retryen statt Notprompt

**Befund:** in einem ~85-Szenen-Video hatten 5 Szenen (~6%) einen barebones
Fallback-Text statt eines echten Bild-Prompts — z. B. Szene 7:
`"Scene illustrating: Chapter 1: The Promise The best scams usually don't
start like scams.. Simple, clear composition."` Das ist keine visuelle
Beschreibung (keine Einstellung, kein Setting, kein Subjekt), sondern nur der
rohe Skript-Text + eine generische Floskel — sichtbar als Stilbruch, weil das
Bildmodell praktisch freie Hand hatte.

**Root Cause:** `_image_prompt_single_retry()` und der Chunk-Fallback in
`visual_prompts()` (`_fetch_image_chunk`) hatten je nur **einen** Versuch —
jeder Fehler (auch ein simpler, transienter JSON-Parse-Fehler von Gemini)
landete sofort beim Fallback, ohne erneuten Versuch, und ohne jede Markierung
— nicht von einer normalen Szene unterscheidbar.

**Fix (`engine/prompts.py`):**

- Beide Stellen retryen jetzt 3× vor dem Fallback (statt 1×).
- Bleibt es trotzdem bei einem Fehler: die Szene wird explizit mit
  `prompt_error: true` markiert (persistiert in `plan.json`), statt
  unauffällig als normale Szene durchzugehen.
- `_batch_generate_worker` überspringt Szenen mit `prompt_error: true`
  komplett (keine KIE-Generierung auf Basis eines Nicht-Prompts) und markiert
  sie mit Status `"fehler"` — **entweder ein echter Prompt + echtes Bild, oder
  gar keine Generierung**, kein stiller Mittelweg mit verschwendeter KIE-Zeit.
- Bereits betroffene Szenen im laufenden Video (`the_19_year_old_who_scamed_18_`,
  Szenen 7/14/22/33/80) wurden nachträglich mit `prompt_error: true` markiert.
  Szenen 7/14/22 hatten dazu schon ein generiertes Bild — das sollte manuell
  neu generiert werden, nachdem der Prompt-Text überarbeitet wurde.

## 7. Bekannte Grenzen / offene Punkte

- Das globale Referenzbild ist **kanalweit**, nicht pro Video — bewusst so
  belassen (Nutzer hat es genau dafür in den Settings hinterlegt), aber wer
  in einem Kanal mehrere stilistisch komplett unterschiedliche Projekte macht,
  muss das Referenzbild manuell zwischen Videos wechseln.
- Charakter-Kontinuität (`_resolve_entity_ref`) ist strikt pro Video isoliert
  (verifiziert) — bewusste wiederkehrende Figuren über mehrere Videos hinweg
  (Serien-Format) werden aktuell NICHT automatisch erkannt.
- Die alte Multi-Charakter-Charsheet-Bibliothek (`/api/gen_charsheet`,
  `/api/upload_charref` mit beliebigem `name`) existiert backend-seitig
  weiterhin, ist aber UI-seitig tot (`switchTopTab()` wird nirgends
  aufgerufen). Nicht gelöscht, nur nicht mehr in die Bild-Generierung
  eingebunden — falls das Feature reaktiviert werden soll, braucht es zuerst
  wieder einen UI-Einstiegspunkt.
- `analyze_script()`s Retry-bei-leerer-characters-Liste deckt nur den
  manuellen Skript-Pfad (`_plan_generate_worker`) ab in vollem Umfang, da
  der Fix direkt in `analyze_script()` selbst sitzt und von beiden Aufrufern
  (`_plan_generate_worker` und dem Audio-Transkriptions-Pfad) genutzt wird —
  beide profitieren automatisch, aber nur der manuelle Pfad wurde heute mit
  echten Daten verifiziert.

---

## 9. ElevenLabs Auto-Chunking (Juli 2026)

**Befund:** ElevenLabs `/with-timestamps` lehnt Texte > 5000 Zeichen ab mit
HTTP 400 `text_too_long`. Theranos-Skript (5788 Zeichen) liegt knapp über
der Grenze. Manuelles Trimmen oder Studio-Tier-Upgrade waren die bisherigen
Optionen — beides unbefriedigend.

**Fix (`engine_elevenlabs.py`):**

- Neue Konstanten:
  - `EL_CHUNK_CHAR_LIMIT = 4800` (Sicherheitsabstand zur 5000er API-Grenze)
  - `EL_CHUNK_OVERLAP_CHARS = 0` (kein Overlap nötig bei Satzgrenzen-Split)
  - `EL_CONTINUITY_WINDOW = 1` (wie viele `previous_request_ids` pro Chunk)
- `_chunk_text_by_sentences()` — Regex-Split an Satzgrenzen
  (`(?<=[.!?])\s+`), greedy-Pack in ≤4800-char Chunks. Garantie: ganze
  Sätze, nie Wort-Bruchstücke.
- `_concat_mp3_files()` — verlustfreie MP3-Konkatenation via `ffmpeg -c copy`.
  Kein Re-Encode → keine Qualitätsverluste an den Boundaries. Single-Chunk-
  Fall ist nur ein `move()` (kein ffmpeg nötig).
- `elevenlabs_generate()` Dispatch:
  - Text ≤ Limit → `_elevenlabs_generate_single()` (alter Pfad)
  - Text > Limit → sequenzielle Calls mit `previous_request_ids` für
    Continuity (außer v3, das das ablehnt — siehe unten), ffmpeg-concat
    der Audio-Bytes, kumulative Timestamp-Verschiebung (`start += prev_chunk.duration`)

**ElevenLabs v3 Caveat:** v3 lehnt `previous_request_ids` mit
`unsupported_model` ab. Wir prüfen `model_id.startswith("eleven_v3")` und
lassen das Feld in dem Fall weg. Resultat: minimale Voice-Boundary-
Unterschiede zwischen Chunks (akzeptabel), dafür sauberer v3-Support.

**Caveat: Auto-Chunking allein rettet die Pipeline nicht** — KIE.ai
benötigt weiterhin gerenderte Bilder als Anker für visuelle Konsistenz
(siehe §3). Erst Voice → Plan → Render mit dem Char-Ref-Fallback
(§10) → Character-Konsistenz im fertigen Video.

**Korrektur (Juli 2026, Produktionsreife-Audit — siehe §12):** die erste
Fassung dieses Fixes hatte selbst zwei Bugs, beide inzwischen behoben:

1. **Chunk-Offset war falsch.** Der kumulative Timestamp-Offset für Chunk N+1
   wurde aus `chunk_N.words[-1].end` berechnet (letztes Wort-Ende) — aber
   ElevenLabs hängt an jeden Chunk etwas Stille an, die in keinem Wort-
   Timestamp auftaucht. Am Theranos-Skript (2 Chunks) betrug die reale
   Differenz ~0,56s zwischen Wort-Ende und echtem MP3-Ende — nach dem Fix
   (ffprobe-gemessene Dauer statt Wort-Ende) liegt sie bei ~0,03s. Ohne Fix
   lag am Ende eines 2-Chunk-Voiceovers der Schnitt bis zu 0,74s vor dem
   tatsächlich gesprochenen Wort; bei mehr Chunks akkumuliert sich das weiter.
2. **`previous_request_ids` waren erfunden.** Statt der echten, von der API
   im `request-id`-Response-Header zurückgegebenen ID (offizieller
   Stitching-Mechanismus, siehe ElevenLabs-Docs "Request stitching") baute
   der Code eine lokale Fake-ID (`el_{voice}_{idx}_{time}`). Bei v3 war das
   irrelevant (Feld wird eh weggelassen), bei v2-Modellen hätte die API das
   Feld entweder ignoriert oder mit einem Fehler abgelehnt — Continuity war
   faktisch nie aktiv. `_elevenlabs_call_with_retry` gibt jetzt
   `(response, request_id)` zurück, der echte Header-Wert wird verkettet.

Zusätzlich ist das Chunk-Zeichenlimit jetzt modellabhängig
(`_chunk_limit_for_model`): eleven_v3 ≈ 4800, multilingual_v2 ≈ 9500,
flash/turbo ≈ 28000 (ElevenLabs-Docs, Stand 2026) — vorher chunkte ein
8000-Zeichen-Skript mit multilingual_v2 unnötig in zwei Calls, obwohl das
Modell es in einem verarbeiten könnte.

---

## 10. Character-Reference-Fallback (Juli 2026)

**Befund:** `_resolve_entity_ref` (engine/scenes.py) lieferte nur dann eine
Referenz-URL, wenn eine **bereits gerenderte Anker-Szene** mit gültiger
`source_url` existierte. Bei folgenden Szenarien gab es **gar keine
Charakter-Referenz** → KIE rendert jede Szene "aus dem Nichts" → Elizabeth
sieht in jedem Frame anders aus:

1. **Race-Bug**: Mein Recovery-Script (für Plan-Generate-Race-Fix) hat
   `source_url` aus dem plan.json entfernt, weil die CDN-URLs von KIE.ai
   TTL-begrenzt sind und der Original-Plan durch einen parallelen
   Worker überschrieben wurde.
2. **ID-Mismatch**: Plan-Generator vergibt generische IDs (`char_01`),
   aber manuell hochgeladene Charsheets liegen unter sprechenden Namen
   (`elizabeth_holmes.png`). Dateiname-basierter Fallback findet sie nicht.

**Fix (`engine/scenes.py`):**

Drei-Stufen-Fallback-Kette in `_resolve_entity_ref`:

1. **Anchor-Szene mit `source_url`** (Original-Verhalten): sucht im Plan
   nach früheren Szenen mit gleicher `concrete_entity`, nimmt die erste.
   Gibt `entity_refs=[source_url]` zurück.
2. **Datei-Match** (Stufe A): sucht im per-video-Pool, dann channel-pool,
   nach `<entity>.png` (z.B. `char_01.png`).
3. **Name-Match via `plan["characters"]`** (Stufe B): nutzt
   `analyze_script()`-Output (`[{id: "char_01", name_or_role: "Elizabeth Holmes"}]`),
   mappt `entity` → `name`, sucht nach Charsheet-JSON mit gleichem `name`.

**Achtung (Update Juli 2026):** Ursprünglich gab der Code hier eine
`data:image/png;base64,...` URL zurück in der Annahme, dass KIE.ai diese nativ
versteht. In der Praxis lehnte die API (`nano-banana-2`) diese jedoch mit 
`{"error": "KIE: File type not supported"}` ab.
Der Code gibt nun den **lokalen Dateipfad** mit dem Flag `"is_local": True` zurück. 
Das Dashboard fängt diesen Pfad ab und lädt das Charsheet sicherheitshalber 
**in einen In-Memory-Cache** (via `upload_image_public`). So wird Rate-Limiting und
paralleler Upload-Spam bei Batch-Generierungen komplett vermieden.
**Wichtig:** Als Upload-Host muss `catbox.moe` verwendet werden! Temporäre Hoster
wie `tmpfiles.org` oder `litterbox` liegen mittlerweile hinter starken Cloudflare- 
oder BunkerWeb-WAFs (Web Application Firewalls) und liefern bei einem Download-
Versuch oft HTML-Anti-Bot-Seiten statt der reinen `image/png`-Datei zurück,
was die KIE.ai-API zum Absturz bringt (`internal error, please try again later`).
`catbox.moe` liefert saubere Raw-Images, weshalb das Dashboard es nun primär nutzt.

**Regex-Bug mit gefixt:** cid/vid-Extraktion aus plan_path anchored
ursprünglich mit `/channels/` (leading slash), matchte aber relative
pfade nicht. Jetzt `(?:^|/)channels/` für beide Fälle.

---

## 11. Speed-Parameter (Juli 2026)

**Offizielle ElevenLabs API:** `voice_settings.speed` mit Default 1.0,
Range praxisüblich 0.7–1.3. Werte >1.0 sprechen schneller, <1.0 langsamer.

**Fix:** UI-Slider (0.7-1.3, step 0.05), Backend-Persist in
`voice_settings.json`, Engine-Code sendet speed an alle 5
voice_settings-builder Sites (single + chunked Generate, voiceTestPreview,
settings save, reset).

**Korrektur (Juli 2026, §12):** 0.7–1.3 war zu großzügig — die offizielle
ElevenLabs-Range ist 0.7–1.2 (elevenlabs.io/docs/eleven-agents/customization/
voice/speed-control). Slider-Max jetzt 1.2, `save_voice_settings` clampt
serverseitig zusätzlich (Werte außerhalb hätten HTTP 400 mitten im teuren
Chunked-Call riskiert).

---

## 12. Produktionsreife-Audit: Sync-Drift + Char-Ref-Reliability + Frontend-Kontrakt (Juli 2026)

Vollständiger Audit + Fix-Pass nach User-Report "ElevenLabs und Bild-
Generierung sind wieder ein bisschen kaputt". Deckt drei Bereiche ab, alle
mit echten Test-Daten verifiziert (aktives Video `19_yearold_girl`, 96
Szenen, Theranos-Skript 5788 Zeichen enriched → 2 Chunks):

**1. Sync-Drift (§9-Korrektur oben):** Am Original-Voiceover lag der letzte
Wort-Timestamp 0,52s vor dem echten Sprechende (462,08s vs. real ~462,6s
gemessen per `ffmpeg silencedetect`); nach Fix nur noch 0,03s. Nachweis über
echten ffprobe/silencedetect-Vergleich, nicht nur Unit-Test.

**2. Char-Ref-Fallback-Lücken (`engine/scenes.py`, `dashboard.py`):**
- `_resolve_entity_ref`/`_wait_for_entity_anchor_scene` bekommen einen
  `wait`-Parameter. `/api/generate_one` (Einzel-Szene, z.B. "Neu
  generieren") hatte bisher eine eigene, abgespeckte Inline-Logik ohne den
  3-Stufen-Fallback aus dem Batch-Worker — jetzt identische Logik
  (`wait=False`, kein Grund auf einen parallelen Sibling zu warten).
- Neue Stufe 1b: eine Anker-Szene mit `file` aber ohne `source_url`
  (Recovery-Race-Fall) liefert jetzt sofort die lokale Bilddatei als
  `data:image/...;base64` statt 170s auf eine `source_url` zu warten, die
  nie wiederkommt (`_wait_for_entity_anchor_scene` behandelt `file` als
  finalen Zustand, nicht nur `source_url`/`status=="fehler"`).
- Stale-URL-Retry (Submit schlägt wegen abgelaufener Referenz fehl) lud
  bisher nur Chain-Refs frisch hoch, ließ den Entity-Anker beim Retry
  komplett weg — jetzt wird auch der Charakter-Anker (frisch hochgeladen
  bei `source: anchor-scene`, sonst direkt die stabile data-URL) mitgeschickt.

**3. Voiceover-Neugenerierung verwaiste Bilder (`dashboard.py`):**
`_transcribe_generate_worker` schrieb `plan.json` bei jedem ElevenLabs-Re-Run
komplett neu (`file: None` für jede Szene), obwohl die Bilddateien selbst
unangetastet blieben (siehe §-Fix weiter oben zu diesem Worker). Die
Text-Match-Preserve-Logik aus `_plan_generate_worker` ist jetzt als
`_preserve_rendered_scenes()`-Helper extrahiert und läuft in BEIDEN
Workern. Verifiziert am echten Video: vor und nach der Voiceover-
Neugenerierung exakt 20 erhaltene Szenen mit `file`+`source_url`.

**4. Frontend/Backend-Kontraktbrüche (`dashboard.html`, `dashboard.py`):**
- `elModelSel` existierte im Code referenziert, aber nicht im DOM — Modell-
  Wahl ging bei jedem Reload verloren. Dropdown ergänzt, Slider-Werte
  (Stability/Similarity/Style/Speed/Boost/Modell) werden beim Laden jetzt
  aus den persistierten Settings vorbelegt (vorher: immer HTML-Hardcoded-
  Defaults, unabhängig von `voice_settings.json`).
- `/api/voiceover_preview` ("Voice testen") baute die mitgeschickten Slider-
  Werte, verwarf sie dann komplett bis auf `voice_id` — Preview spielte
  immer die zuletzt gespeicherten Werte vor, nie die aktuell gezogenen.
- `save_voice_settings` verwarf `tts_provider`/`volume`/`pitch` (nicht in
  `ELEVENLABS_VOICE_SETTINGS_DEFAULT`) — ein Provider-Wechsel zu MiniMax
  persistierte nie über einen Server-Neustart hinaus.
- Resume-Precedence-Bug in `/api/voiceover_generate`: die Bedingung
  `A and B if meta else False and C and D` parst in Python als
  `(A and B) if meta else (...)`, NICHT als `meta and A and B and C and D`.
  Solange `audio_meta.json` existierte, wurden die Prüfungen auf
  `voiceover_word_timestamps` und `plan.json`-Existenz nie ausgeführt — ein
  Resume wurde fälschlich gemeldet, auch ohne brauchbaren Plan.
- `/api/gen_style_ref` + `/api/set_style_ref` (vom Frontend aufgerufen)
  existierten serverseitig nur unter den alten Namen `gen_char_ref`/
  `set_char_ref` → 404 auf die Stil-Referenz-Buttons. Beide Namen werden
  jetzt akzeptiert.
- `/api/render_start` startete stillschweigend auch bei nur teilweise
  generierten Bildern — `_apply_sync_invariant` streckt die vorhandenen
  Bilder dann über die VOLLE Audiolänge (z.B. 20/96 Bilder über 462s ≈
  23s/Bild). Jetzt: Response `{"partial": true, "rendered": n, "total": m}`
  wenn nicht `force`, Frontend zeigt `confirm()` bevor es mit `force: true`
  weiterläuft.

**Tests:** `tests/test_pipeline_fixes.py` — 15 neue Tests, echte ffmpeg/
ffprobe-Läufe für den Chunk-Offset-Test (kein reines Mocking), Rest sind
Verhaltens- oder Source-Grep-Tests nach demselben Muster wie
`test_cinematic_e2e.py`.

**Bekannte, NICHT gefixte Nebenerkenntnis:** `align_scenes_to_whisper`
matched Szenen-Text-Wortanzahl gegen die Voiceover-Wortliste. Bei einem
Voiceover-Re-Take (ElevenLabs v3 ist nicht deterministisch) kann die
Gemini-Szenensegmentierung geringfügig von der tatsächlich gesprochenen
Wortzahl abweichen (~7% beobachtet) — die letzten paar Szenen eines Videos
bekommen dann kein `start_aligned`/`end_aligned` und fallen auf die
geschätzte Dauer zurück (dokumentiertes, existierendes Graceful-Degradation-
Verhalten, siehe Docstring in `dashboard.py`). Kein Regressions-Risiko durch
diesen Audit, aber ein potenzieller Folge-Fix falls das bei echten Videos
spürbar wird.

## 13. Symbiose-Fix: Voiceover ↔ Plan ↔ Schnitt entkoppelt + Sync-Präzision (Juli 2026)

User-Report nach §12: "Voiceover generieren zerstört meinen geprüften Plan" +
Zweifel, ob Bild/Schnitt am Ende wirklich aufs gesprochene Wort passen. Drei
Ursachen gefunden, alle an echten Daten verifiziert (Skript 919 Roh-Wörter,
97-Szenen-Plan von `19yearold_who_faked_a_9_billio`).

**1. Voiceover-Rebuild zerstörte bereits geprüfte Pläne (`engine_elevenlabs.py`):**
`_elevenlabs_persist_and_schedule`/`_minimax_persist_and_schedule` lösten nach
JEDEM ElevenLabs/MiniMax-Call blind `_transcribe_generate_worker` aus — der baut
den kompletten Plan neu, mit einer ANDEREN Segmentierung (feste Zeitfenster
statt satz-/pacing-bewusst) und neuen Prompts. Sinnvoll nur im voice-first-Fall
(noch kein Plan), aber lief auch dann, wenn schon ein manuell erstellter,
geprüfter Plan existierte.

Fix: neuer Helper `_plan_has_usable_scenes(plan_path)` — True, wenn irgendeine
Szene ein nicht-leeres `prompt` hat. Rebuild läuft nur noch, wenn das **nicht**
der Fall ist. Der Render braucht die Zeitstempel ohnehin nicht aus `plan.json`,
sondern liest sie direkt aus `audio_meta.json` — die Entkopplung ist damit ohne
Funktionsverlust. Verifiziert am echten 97-Szenen-Plan: `_plan_has_usable_scenes`
erkennt ihn korrekt, ein Voiceover-Klick würde ihn jetzt nicht mehr anfassen.

**2. Enrichment-Tokens verschoben den Wort-Zähler beim Alignment (`dashboard.py`) —
die eigentliche Hauptursache für Sync-Drift, nicht der in §9 gefixte Chunk-Offset:**
`_enrich_for_tts` fügt vor dem ElevenLabs-Call `"..."`-Pausenmarker in den Text
ein (natürlichere Betonung). ElevenLabs liefert dafür eigene Zeitstempel zurück,
die in `voiceover_word_timestamps` landen. `align_scenes_to_whisper` zählt aber
`len(scene["text"].split())` aus dem ROH-Skript (ohne `"..."`) und konsumiert die
Wortliste sequenziell in dieser Zählung — jedes ungezählte `"..."`-Token
verschiebt den Lesekopf um eins. Am echten Theranos-Skript gemessen: 919
Roh-Wörter → 1000 nach Enrichment (81 Phantom-Tokens). Bei genug Tokens lief der
Zähler leer, bevor die letzten Szenen ihr `start_aligned` bekamen → Fallback auf
geschätztes Timing → Schnitt driftet.

Fix: `_strip_pause_tokens(words)` entfernt Tokens, die NUR aus Punkten bestehen
(`"..."`, `"…"`), direkt vor `_compute_pause_trims`/`align_scenes_to_whisper` im
`_render_worker`. Bewusst NICHT "jedes Token ohne alphanumerisches Zeichen" —
ein breiterer Filter hätte auch eigenständige Satzzeichen-Wörter aus dem
ORIGINAL-Skript getroffen (an echten Skripten beobachtet: ein freistehendes
`"—"` oder `"/"`, das dort schon als eigenes `.split()`-Wort zählt) und damit
exakt dasselbe Off-by-one-Problem an anderer Stelle reproduziert. Verifiziert:
mit dem präzisen Filter sind Roh-Skript und gestrippte Wortliste exakt
deckungsgleich (919 = 919); ein zu breiter erster Versuch kam nur auf 917.

**3. Sync-Invariant verteilte Pausen proportional statt punktgenau (`engine/render.py`):**
`_apply_sync_invariant` nahm pro Szene nur `end_aligned - start_aligned` (reine
Sprechzeit, OHNE die Pause danach) und skalierte alle Szenen mit
`factor = audio_duration / summe_sprechzeit`. Da die Summe aller
Zwischenpausen fehlte, war `factor > 1.0` — JEDE Szene wurde gestreckt, auch
wenn ihre eigene Pause winzig war. Der Schnittpunkt vor Szene N verschob sich
damit von der tatsächlichen Startzeit des N-ten Worts weg, akkumuliert über die
ganze Szenenliste.

Fix: wenn ALLE Szenen `start_aligned` haben, ist die Dauer einer Szene die Zeit
bis zum NÄCHSTEN `start_aligned` (schließt die Pause danach ein), letzte Szene
bis zum Audio-Ende. Die Summe telescopiert zu `audio_duration - scenes[0].start_aligned`
→ `factor ≈ 1.0` bei typisch kurzer Anfangs-Stille → kumulierte Position vor
Szene N ≈ `scenes[N].start_aligned`. Fällt zurück auf die alte, sprechzeit-basierte
Berechnung, wenn auch nur eine Szene kein `start_aligned` hat (Whisper-Teilabdeckung).

**Tests:** `tests/test_pipeline_fixes.py` um 8 Tests erweitert (Entkopplung
inkl. Verhaltenstest mit gemocktem `_transcribe_generate_worker`, Strip-Filter
inkl. Alignment-Regressionstest, Sync-Invariant inkl. Schnittpunkt-Berechnung
mit ungleich verteilter Pause). 24/24 grün, bestehende Suite unverändert
(147/149, die 2 Fehler sind vorbestehend/umgebungsbedingt).

## 8. Relevante Commits (main)

- `c7cb1cb` — Thumbnail async + Charsheet-Kontamination behoben
- `22a7a09` — nie zwei widersprüchliche Referenzbilder gleichzeitig
- `93f5f76` — Referenzbild fehlte bei Nicht-Charakter-Szenen + Progress-Fake
- `99756b1` — leere Charakter-Analyse → inkonsistente IDs + Klarname im Prompt
- `2ec5ea4` — Prompt-Generierung retryt jetzt 3x, markiert Fehler statt Notprompt
