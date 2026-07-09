# Runbook: Pipeline-Kontrolle & Troubleshooting

**Stand:** Juli 2026
**Zweck:** Schritt-für-Schritt-Anleitung für den normalen Render-Flow
**und** für die häufigsten Fehlerbilder (Buttons hängen, Bilder fehlen,
Voiceover wird nicht abgespielt, Charaktere sehen anders aus).

Zielgruppe: Du (Noel), alleine am Rechner — kein Pair-Programming.

---

## 0. Quick-Health-Check (30 Sekunden, vor jedem Run)

```bash
# 1) Server läuft?
ps aux | grep "dashboard\.py" | grep -v grep | head -1

# 2) Wie viele Bilder pro Video?
for v in $(ls channels/default/videos/); do
  echo "$v: $(ls channels/default/videos/$v/generated/*.jpg 2>/dev/null | wc -l) Bilder"
done

# 3) Welche Videos haben Voiceover?
for v in $(ls channels/default/videos/); do
  [ -f "channels/default/videos/$v/uploads/voiceover.mp3" ] && \
    echo "$v: Voiceover vorhanden" || echo "$v: KEIN Voiceover"
done

# 4) Was sind die aktuellen Generation-Läufe?
curl -s "http://localhost:8000/api/voiceover_status?channel=default&video=19_yearold_girl"
curl -s "http://localhost:8000/api/plan_status?channel=default&video=19_yearold_girl"
curl -s "http://localhost:8000/api/generate_all_status?channel=default&video=19_yearold_girl"
```

Erwartete Werte:
- `voiceover_status`: `running: false` (oder `stage: "elevenlabs-generate"` während Gen)
- `plan_status`: `running: false`
- `generate_all_status`: `running: false` und `done >= total` wenn fertig

**Wenn `running: true` ohne dass ein Job läuft:** Server-Memory-State stale.
→ Server-Neustart (siehe §6).

---

## 1. Normal-Flow: Neues Video von Skript bis Render

### Schritt 1: Skript einfügen
- Frontend: Skript-Tab öffnen, Text reinkopieren
- Wird debounced (2.5s) in `channels/<cid>/videos/<vid>/script.json` persistiert
- **Verify:** `cat channels/default/videos/<vid>/script.json | python3 -m json.tool | grep text`

### Schritt 2: Voice-Settings
- Voice auswählen (Select-Dropdown) → speichert in `voice_settings.json`
- Slider setzen: Stability, Similarity, Style, **Speed**, Speaker-Boost
- **Verify:** `cat channels/default/videos/<vid>/../voice_settings.json`

### Schritt 3: "Voice testen" (optional, ~5s)
- Sendet hardcoded `'Hallo, das ist ein Stimm-Sample.'` an `/api/voiceover_preview`
- Inline-Preview-Player zeigt 2-Sek-Sample
- **Wenn du die ECHTE 7-Min-Audio hören willst:** nach Schritt 4 unten

### Schritt 4: "Voiceover generieren"
- Wenn Skript > 4800 Zeichen: Auto-Chunking (Satzgrenzen)
- v3: 2 Chunks à ~2900 Zeichen, ffmpeg-concat zu 1 MP3
- v2 (oder multilingual): kann `previous_request_ids` für Continuity
- **Verify:**
  ```bash
  ls -la channels/<cid>/videos/<vid>/uploads/voiceover.mp3
  ffprobe -v error -show_format channels/<cid>/videos/<vid>/uploads/voiceover.mp3
  cat channels/<cid>/videos/<vid>/uploads/audio_meta.json | python3 -m json.tool | head -20
  ```
  Erwartet: MP3 vorhanden, `voiceover_chars == len(skript_text)`, ~7min für 5500 chars

### Schritt 5: "Plan aus Skript erstellen"
- Erzeugt 75-100 Szenen mit `concrete_entity` (char_01, sym_02, loc_01, ...)
- Bestehende gerenderte Bilder werden per Text-Match preserved (Race-Fix)
- **Verify:** `python3 -c "import json; p=json.load(open('channels/<cid>/videos/<vid>/generated/plan.json')); print(f'Scenes: {len(p[\"scenes\"])}')"`
- **Wenn `plan.json` nur 1 Szene hat:** Server-Memory wurde von einem Test-Curl
  mit "Test."-Text überschrieben. → siehe §5 "Plan-Recovery"

### Schritt 6: Char-Sheets prüfen
```bash
# Welche Charaktere hat der Plan?
python3 -c "
import json
from collections import Counter
plan = json.load(open('channels/<cid>/videos/<vid>/generated/plan.json'))
print(Counter(s['concrete_entity'] for s in plan['scenes'] if s['concrete_entity'].startswith('char_')))
"
# Welche Charsheets existieren?
ls channels/<cid>/videos/<vid>/charsheets/ | grep -v "^_"
```

**Duplikate:** wenn z.B. `char_01.json` UND `elizabeth_holmes.json` für denselben
Charakter existieren → manuell die Pipeline-generierte Variante in
`_pipeline_duplicates/` verschieben (siehe §4).

### Schritt 7: "Generate All" (60-180s für ~100 Bilder)
- Pro Szene: Anchor-Szene (source_url) ODER Charsheet-PNG-Fallback + Style-Ref + Master
- KIE rendert, setzt `file`, `status="fertig"`, `source_url`
- **Verify während des Laufs:**
  ```bash
  curl -s "http://localhost:8000/api/generate_all_status?channel=default&video=<vid>" | python3 -m json.tool
  ```
  Beobachte `done`, `total`, `current_i`. Normal: `done` steigt alle paar Sekunden.

### Schritt 8: Audio abspielen im UI
- Nach Schritt 4 ist die `elVoiceoverBox` sichtbar mit dem `<audio>` Player
- Browser-Reload nötig wenn die Box nicht erscheint (UI läuft nicht automatisch Updates)

### Schritt 9: "Render Video"
- ffmpeg assembliert `final.mp4` (Voice + Bilder, ohne Sound)
- ~30s für 100 Szenen
- Output: `channels/<cid>/videos/<vid>/generated/final.mp4`

### Schritt 10: Sound in Logic/DaVinci
- `final.mp4` enthält NUR Voice + Bilder
- Musik + SFX manuell drunterlegen

---

## 2. Häufige Probleme

### Problem A: "Voiceover wird nicht angezeigt"

**Symptom:** Nach "Voiceover generieren" zeigt das UI nur "Voiceover wird angefragt …" dauerhaft, kein Player.

**Ursache 1: Server-Memory stale** (häufigster Fall)
- `voiceover_status.running = true` obwohl Job längst fertig
- Audio IST auf Disk vorhanden

**Diagnose:**
```bash
ls -la channels/<cid>/videos/<vid>/uploads/voiceover.mp3
curl -s "http://localhost:8000/api/voiceover_file?channel=default&video=<vid>" -I | head -3
```

**Fix:** Server-Restart (siehe §6). Browser-Reload (Cmd+Shift+R für Hard-Reload).
Player sollte erscheinen — entweder automatisch (durch `refreshVoiceoverPlayer()` in
`openVideo()`) oder nach Hard-Reload.

**Ursache 2: ElevenLabs-Generate ist gescheitert**
- `elStatus` zeigt "Fehler: ..."
- MP3 nicht auf Disk

**Fix:** Siehe §3.

---

### Problem B: "Charaktere sehen in jeder Szene anders aus"

**Symptom:** Elizabeth Holmes hat in Szene 1, 4, 7 jeweils komplett andere Frisur,
Pose, sogar Gesichtsform. Klingt wie "1:1 das selbe Bild" aber umgekehrt — kein
Anchor da.

**Diagnose:**
```bash
python3 << 'EOF'
import json
plan = json.load(open('channels/<cid>/videos/<vid>/generated/plan.json'))
scenes_with_url = [s for s in plan['scenes'] if s.get('source_url')]
char_scenes = [s for s in plan['scenes'] if s.get('concrete_entity','').startswith('char_')]
print(f'Scenes mit source_url: {len(scenes_with_url)}/{len(plan["scenes"])}')
print(f'Charakter-Szenen: {len(char_scenes)}')
if char_scenes:
    print(f'Erste char-Szene (i={char_scenes[0]["i"]}) hat source_url: {bool(char_scenes[0].get("source_url"))}')
EOF
```

**Fix-Pfade:**

1. **0 scenes mit source_url:** Race-Bug oder Recovery war kaputt. Lösung:
   - Race-Fix ist im Code (commit d3914c6) — sollte nicht mehr passieren
   - Manuell: lösche `generated/plan.json`, klicke "Plan aus Skript erstellen" erneut
     (Race-Fix wird gerenderte Bilder per Text-Match preservieren)
2. **Erste char-Szene hat source_url aber andere nicht:** Das ist OK. Folge-Szenen
   bekommen den Anchor via `_resolve_entity_ref`. Sollte funktionieren.
3. **Erste char-Szene hat KEINE source_url:** Charsheet-PNG-Fallback greift
   (engine/scenes.py `_resolve_entity_ref` Stufe A oder B). Sollte funktionieren
   wenn PNG vorhanden ist. Verify mit §4.

---

### Problem C: "Bilder weg" / "Plan plötzlich kleiner"

**Symptom:** `plan.json` hat plötzlich 1 Szene statt 75-100. Oder: alle Szenen
haben `file=None, status="geplant"` obwohl JPGs auf Disk liegen.

**Diagnose:**
```bash
# Plan-Inhalt
python3 -c "
import json
p = json.load(open('channels/<cid>/videos/<vid>/generated/plan.json'))
print(f'Scenes: {len(p[\"scenes\"])}, mit file: {sum(1 for s in p[\"scenes\"] if s.get(\"file\"))}')"

# JPGs auf Disk
ls channels/<cid>/videos/<vid>/generated/*.jpg | wc -l
```

**Ursache:** Race zwischen Plan-Worker und Render-Worker ODER manueller Test-POST.

**Fix (Recovery):**
```bash
python3 << 'EOF'
import json, os, re
plan_path = 'channels/default/videos/<vid>/generated/plan.json'
plan = json.load(open(plan_path))
gen_dir = os.path.dirname(plan_path)
existing = {int(re.match(r'^(\d{3})\.jpg$', f).group(1)): f
            for f in os.listdir(gen_dir)
            if re.match(r'^\d{3}\.jpg$', f)}
for s in plan['scenes']:
    i = s['i']
    if i in existing and (s.get('file') is None or s.get('status') != 'fertig'):
        s['file'] = existing[i]
        s['status'] = 'fertig'
print(f'Reparierte {sum(1 for s in plan[\"scenes\"] if s.get(\"file\"))} Szenen')
from dashboard import _atomic_write_json
_atomic_write_json(plan_path, plan, ensure_ascii=False, indent=1)
EOF
```

**Anschließend:** Waisen-Bilder (Idx > Anzahl Szenen) in `_stale/` verschieben.

---

### Problem D: "Doppelte Charaktere"

**Symptom:** Im Frontend siehst du "Elizabeth Holmes" zweimal (z.B. `char_01` und
`elizabeth_holmes` als zwei getrennte Charakter-Einträge).

**Ursache:** Plan-Generator hat `char_01` erzeugt, dann hast du manuell
`elizabeth_holmes` hochgeladen. Beide existieren parallel.

**Fix:** Pipeline-generierte Varianten (`char_XX`) in `_pipeline_duplicates/`
verschieben — die manuellen haben semantisch gleichen Namen und sind vom User
bewusst hochgeladen (typischerweise mit besseren Bildern).

```bash
cd channels/<cid>/videos/<vid>/charsheets
mkdir -p _pipeline_duplicates
mv char_0*.json char_0*.png _pipeline_duplicates/
```

Frontend zeigt danach nur noch die 2 manuellen Charaktere.

---

### Problem E: "ElevenLabs: HTTP 400 text_too_long"

**Symptom:** Generate schlägt fehl mit Meldung über 5000-Zeichen-Limit.

**Ursache:** Skript > 5000 Zeichen. Sollte durch Auto-Chunking gefangen werden —
aber falls nicht:

**Diagnose:**
```bash
python3 -c "import json; print(len(json.load(open('channels/<cid>/videos/<vid>/script.json'))['text']))"
```

**Fix:** Auto-Chunking sollte greifen (Engine-Code, commit 60a9a0c). Wenn nicht:
- Server neu starten (Stale Code?)
- `engine_elevenlabs.py` Z.337 prüfen: muss `elevenlabs_generate()` (mit Auto-Chunk) aufrufen,
  nicht direkt die Single-Variante

---

### Problem F: Server hängt / Worker läuft nicht

**Diagnose:**
```bash
ps aux | grep "dashboard\.py" | grep -v grep
tail -30 /tmp/dashboard_restart7.log  # letzte Server-Log-Datei
```

**Fix:** Server neu starten (siehe §6).

---

## 3. ElevenLabs API — was du wissen musst

**Offizielle Docs:** https://elevenlabs.io/docs/api-reference/text-to-speech/convert-with-timestamps

**Character limits (Stand Juli 2026):**

| Model | Limit | ≈ Audio-Dauer |
|---|---|---|
| `eleven_v3` | 5.000 Zeichen | 5 min |
| `eleven_multilingual_v2` | 10.000 Zeichen | 10 min |
| `eleven_flash_v2_5` | 40.000 Zeichen | 40 min |

**Voice-Settings (`voice_settings`):**
- `stability` (0-1, default 0.5) — niedrig = variabler, hoch = monotoner
- `similarity_boost` (0-1, default 0.75) — hoch = näheres Original
- `style` (0-1, default 0) — Stil-Verstärkung (kostet Latenz)
- `use_speaker_boost` (bool, default true) — mehr Ähnlichkeit zum Original
- `speed` (0.7-1.3, default 1.0) — >1 schneller, <1 langsamer

**v3-spezifische Caveats:**
- `previous_request_ids` wird mit `unsupported_model` abgelehnt
- Andere Felder könnten ähnlich eingeschränkt sein — der Code testet das per
  try/except und meldet klare Fehlermeldungen

---

## 4. Disk-Recovery-Skripte

### 4.1 Charakter-Beziehungen anzeigen
```python
# Zeigt welche concrete_entity IDs im Plan sind, mit welchen Charsheets matchen
import json, os
plan = json.load(open('channels/<cid>/videos/<vid>/generated/plan.json'))
chars_dir = f'channels/<cid>/videos/<vid>/charsheets'
existing = {}
for fn in os.listdir(chars_dir):
    if fn.endswith('.json') and not fn.startswith('_'):
        meta = json.load(open(f'{chars_dir}/{fn}'))
        existing[meta.get('name', '').lower()] = meta.get('safe', '')

print('Charakter-IDs im Plan → erwarteter Name → Charsheet-Datei:')
seen = set()
for s in plan['scenes']:
    e = s.get('concrete_entity', '')
    if e.startswith('char_') and e not in seen:
        seen.add(e)
        # Suche in plan["characters"]
        name = ''
        for c in plan.get('characters', []):
            if c.get('id') == e:
                name = c.get('name_or_role', '')
                break
        png = f'{chars_dir}/{e}.png'
        if not os.path.exists(png):
            # Fallback via Name-Match
            png = f'{chars_dir}/{existing.get(name.lower(), "NOT FOUND")}.png'
        print(f'  {e:10s} → {name:25s} → {png} (exists: {os.path.exists(png)})')
```

### 4.2 Plan-Recovery (siehe §Problem C)

(Siehe oben im Problem-C-Abschnitt)

### 4.3 Render-Fortschritt überwachen
```bash
watch -n 5 'curl -s "http://localhost:8000/api/generate_all_status?channel=default&video=<vid>" | python3 -m json.tool'
```

---

## 5. Häufige Workflows

### Voiceover neu generieren (z.B. nach Speed-Änderung)
1. Im UI: stelle Speed-Slider ein
2. Klick "Voiceover generieren" — das hängt am Resume-Pfad wenn MP3 existiert
3. **Workaround:** zuerst Delete-Button in der Voiceover-Box klicken, dann Generate
4. ODER API: `curl -X POST "http://localhost:8000/api/voiceover_delete?channel=default&video=<vid>"`
5. Dann erneut Generate

### Render mit anderen Chars
1. Verschiebe ungewollte Charsheets in `_pipeline_duplicates/`
2. Browser-Reload
3. Plan-Generate (Race-Fix preserviert gerenderte Bilder)
4. Wenn Charsheets geändert wurden: "Generate All" nochmal für Szenen mit Charakter

### Bestimmte Szenen neu rendern
- Im UI: auf der jeweiligen Szene "Neu generieren" klicken (single-image-Pfad)
- ODER API: `curl -X POST ".../api/generate_image?channel=...&video=...&i=N&..."

---

## 6. Server-Steuerung

### Starten
```bash
cd /Users/noel/gemini-storyboard
./start.sh 8000            # Standard-Port 8000
# oder
./start.sh 9000            # anderer Port wenn 8000 belegt
```

### Stoppen
```bash
pkill -9 -f "dashboard\.py"
```

### Neustart (bei Code-Änderungen)
```bash
pkill -9 -f "dashboard\.py"
sleep 1
./start.sh 8000
```

### Logs
Server-Logs werden nach `/tmp/dashboard_restartN.log` geschrieben (N = Anzahl
Restarts). Letzten Log lesen:
```bash
tail -50 $(ls -t /tmp/dashboard_restart*.log | head -1)
```

---

## 7. Performance-Benchmarks (Theranos-Test, Stand Juli 2026)

| Stage | Dauer | Kosten (ca.) |
|---|---|---|
| 11 Labs v3 Generate (5513 chars, 2 Chunks) | 60-90s | ~$0.05 |
| Plan-Generate (96 Szenen) | 30-60s | ~$0.10 |
| Generate All (96 Bilder @ nano-banana-2) | 4-6 min | ~$1.00 |
| Render final.mp4 (96 Szenen) | 30-60s | lokal (ffmpeg) |
| **Total für ein 7-Min-Video** | **~6-10 min** | **~$1.15** |

nano-banana-2-lite ist ~50% günstiger, etwas niedrigere Qualität.

---

## 8. Notfall-Kommandos

```bash
# Server-Status
ps aux | grep "dashboard\.py" | grep -v grep
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/

# Komplettes Health-Check
bash -c '
echo "=== Server ==="
ps aux | grep "dashboard\.py" | grep -v grep | head -1
echo ""
echo "=== Videos ==="
for v in $(ls channels/default/videos/); do
  d=channels/default/videos/$v
  imgs=$(ls $d/generated/*.jpg 2>/dev/null | wc -l)
  mp3=$([ -f "$d/uploads/voiceover.mp3" ] && echo "Y" || echo "N")
  plan=$([ -f "$d/generated/plan.json" ] && echo "Y" || echo "N")
  echo "  $v: imgs=$imgs plan=$plan mp3=$mp3"
done
echo ""
echo "=== Disk-Nutzung ==="
du -sh channels/default/videos/ 2>/dev/null
'
```

---

## 9. Wenn gar nichts mehr hilft

1. Server komplett neustarten
2. Browser: Hard-Reload (Cmd+Shift+R) UND DevTools öffnen → Console prüfen
3. Network-Tab: prüfe ob POSTs 200 zurückgeben oder 4xx/5xx
4. Wenn Server-Fehler: Stacktrace aus `/tmp/dashboard_restartN.log` lesen
5. Wenn alles nichts hilft: Git-Log durchgehen welche letzten Commits liefen,
   ggf. letzten Commit revertieren (`git revert HEAD`)

Bei anhaltenden Problemen: Plan-Snapshot sichern + Issue auf GitHub mit
Reproduktions-Steps + Logs.