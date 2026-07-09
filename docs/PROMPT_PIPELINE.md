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

Match wird als `data:image/png;base64,...` URL zurückgegeben (KIE.ai
akzeptiert data-URLs direkt — kein Catbox-Upload, kein 403-Risiko, kein
TTL-Problem). Debug-Dict enthält `source`-Feld (`anchor-scene` /
`charsheet-png` / `charsheet-name-match`) für Diagnose.

**Regex-Bug mit gefixt:** cid/vid-Extraktion aus plan_path anchored
ursprünglich mit `/channels/` (leading slash), matchte aber relative
Pfade nicht. Jetzt `(?:^|/)channels/` für beide Fälle.

---

## 11. Speed-Parameter (Juli 2026)

**Offizielle ElevenLabs API:** `voice_settings.speed` mit Default 1.0,
Range praxisüblich 0.7–1.3. Werte >1.0 sprechen schneller, <1.0 langsamer.

**Fix:** UI-Slider (0.7-1.3, step 0.05), Backend-Persist in
`voice_settings.json`, Engine-Code sendet speed an alle 5
voice_settings-builder Sites (single + chunked Generate, voiceTestPreview,
settings save, reset).

## 8. Relevante Commits (main)

- `c7cb1cb` — Thumbnail async + Charsheet-Kontamination behoben
- `22a7a09` — nie zwei widersprüchliche Referenzbilder gleichzeitig
- `93f5f76` — Referenzbild fehlte bei Nicht-Charakter-Szenen + Progress-Fake
- `99756b1` — leere Charakter-Analyse → inkonsistente IDs + Klarname im Prompt
- `2ec5ea4` — Prompt-Generierung retryt jetzt 3x, markiert Fehler statt Notprompt
