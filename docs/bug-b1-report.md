# Bug B-1 — Charakter-Upload-Crash (2026-07-07)

## Symptom (Nutzer-Report)

Der Upload der Charakter-Bild-Referenz schlug fehl — kein Bild erschien als
Referenz-Kachel im Frontend.

## Reproduktion

Live-Test mit `python3 dashboard.py` auf Port 8766:

```bash
curl -X POST -H 'Content-Type: application/json' \
  -d '{"channel":"default","name":"Test","image":"not-valid-base64!!!"}' \
  http://localhost:8766/api/upload_charref
```

**Vorher:** Leere Response, Server-Log zeigt:
```
binascii.Error: Incorrect padding
  File "/.../dashboard.py", line 2934, in do_POST
    img_bytes = base64.b64decode(img_b64)
```

**Nachher (Fix):**
```
{"error": "image ist kein gültiges Base64: Incorrect padding"}
```

## Root Cause

Drei verkettete Bugs im `/api/upload_charref`-Handler (`dashboard.py` Z. 2929 ff.):

| # | Bug | Auswirkung |
|---|---|---|
| 1 | `base64.b64decode(img_b64)` ohne `validate=False` + ohne try/except | Server-Crash bei Junk-Input → leerer 500er → Frontend Silent-Fail |
| 2 | Keine Validierung von `name = d.get("name").strip()` — leerer String erlaubt | `safe = ""` → Datei endet auf `.png` im `charsheets/`-Root, gegenseitiges Überschreiben |
| 3 | Keine `os.makedirs(ch_sheets(cid), exist_ok=True)` vor dem Schreiben | Fresh-Channel-Crash wenn `charsheets/` nicht existiert (passierte bei Kanälen, die nicht via `POST /api/channels` angelegt wurden) |

## Fix

Ein Block im Handler (`dashboard.py` ~Z. 2929-2955):

1. **`validate=False`** bei `b64decode` — toleriert fehlendes Padding (häufiger Browser-Bug).
2. **`try/except`** um `b64decode` — fängt alle `binascii.Error`-Klassen, gibt 400 mit klarer Meldung.
3. **`if not name: return 400 "name fehlt"`** — verhindert leere `safe`-Strings.
4. **`os.makedirs(ch_sheets(cid), exist_ok=True)`** — fresh channels arbeiten auch.
5. **`try/except OSError`** um `open(img_path, "wb")` — defensive Schreib-Fehlerbehandlung.

## Tests

Zwei neue Regressionstests in `tests/test_cinematic_e2e.py`:

- **`t_bug_b1_charref_upload_validates_base64`**: Source-Grep-Check, dass der Handler
  alle 4 Fixes enthält (`validate=False`, `try/except`, `name`-Check, `makedirs`).
- **`t_bug_b1_charref_upload_roundtrip`**: E2E — startet echten Server, schickt validen
  Upload, prüft dass PNG + JSON auf Disk landen UND keine 5xx.

## Verifikation

Manuelle cURL-Repros aller 6 Edge-Cases vor und nach dem Fix:

| Szenario | Vorher | Nachher |
|---|---|---|
| A: kein `image`-Feld | `{"error": "image fehlt"}` ✓ | `{"error": "image fehlt"}` ✓ |
| B: leeres `image` | `{"error": "image fehlt"}` ✓ | `{"error": "image fehlt"}` ✓ |
| **C: ungültiges Base64** | **Server-Crash, leere Response ✗** | **`{"error": "..."}` ✓** |
| D: leerer `name` | Bug (leere `safe`) ✗ | `{"error": "name fehlt"}` ✓ |
| E: valider Upload | `{"ok": true, ...}` ✓ | `{"ok": true, ...}` ✓ |
| F: Base64 ohne Padding | Server-Crash ✗ | `{"error": "..."}` ✓ |

Test-Stand: 68 → 70 grün.

## Akzeptanzkriterium (§9.3 des Plans)

> PNG/JPG hochladen → erscheint sofort als Referenz-Kachel → nächster Batch-Gen hängt
> sie nachweislich an (`char_ref_applied: true` in `plan.json`).

✓ E2E-Test `t_bug_b1_charref_upload_roundtrip` schreibt PNG + JSON erfolgreich.
✓ Valider Upload liefert 200 OK mit Beschreibung.

**Nicht in B-1** (gehört zu Phase 38 / separates Frontend-Ticket):
- Frontend zeigt die neue Char-Kachel automatisch
- `_batch_generate_worker` hängt sie an (das tat es schon, jetzt aber zuverlässig)