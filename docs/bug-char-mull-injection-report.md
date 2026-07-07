# Bug — Char-Müll-Injection in Bild-Prompts (2026-07-07)

## Symptom (User-Report)

Trotz Char-Referenz-Bild wurden ausschließlich Strichmännchen generiert; die
Referenzbild-Einstellung im UI wurde nicht korrekt mit der Charakter-Referenz
verknüpft.

## Root Cause

User-Fund: `channels/default/charsheets/validchar.json` enthält wörtlich diese
Beschreibung, die in JEDEN Bild-Prompt injiziert wurde:

> "Stick-figure construction… Torso is a single vertical line; limbs are single
>  lines with rounded joints. Face: Two simple black dots for eyes…
>  minimalist stick-figure aesthetic. No hands or feet; limbs terminate in rounded ends."

`load_char_refs` lud alle charsheets-JSONs ungefiltert → die Stick-Figure-Test-Spec
landete als Text-Anweisung in jedem Prompt und übersteuerte Master + Referenzbild.

## Weitere gefundene Müll-Charsheets

Im selben `channels/default/charsheets/`-Verzeichnis:

| Datei | Description | Status |
|---|---|---|
| `validchar.json` | Stick-Figure-Test-Spec | **Müll** |
| `testchar.json` (in `b1_repro`) | Stick-Figure-Test-Spec | **Müll** |
| `big.json` | leer | **Müll** |
| `müller___söhne__1_.json` | leer | **Müll** |
| `nochan.json` | leer | **Müll** |

Plus 5 PNG-Dateien ohne JSON-Pendant (user-uploads ohne Description):
- `human_rights_activist.png`, `jamal_khashoggi.png`, `mexican_journalist.png`,
  `the_activist.png`, `the_journalist.png`

Plus Test-Kanäle `b1_repro`, `b1_test_ch`, `p38test_*`, `defaultpresettest`,
`testpresetchannel`, `invalidpresettest` (Phase-38-Test-Reste) — komplett gelöscht.

## Fix

1. **Müll gelöscht**: alle 5 Müll-JSONs + zugehörige PNGs + Test-Kanäle
2. **Echte PNG-Charsheets bekommen Beschreibung** via `analyze_char_image` (LLM-Aufruf)
3. **`_is_valid_char_description()`** in `engine/prompts.py` — Müll-Injection-Filter:
   - description muss ≥ 30 Zeichen
   - blockiert Stick-Figure-Test-Patterns: "torso is a single vertical line",
     "minimalist stick-figure aesthetic", "single lines with rounded joints",
     "limbs terminate in rounded ends", "no hands or feet"
4. **`load_char_refs()`** validiert jetzt jedes Charsheet und überspringt Müll
5. **`_build_image_prompt()`** filtert Müll-Charsheets vor der Text-Injection

## Echtes Problem dahinter

Die 5 echten charsheet-PNGs sind Strichmännchen-Bilder, weil der User Strichmännchen
für seine journalistischen Charaktere hochgeladen hat. Das `flat_cartoon_doc`-Preset
verlangt aber "vereinfachte Cartoon-Figuren" — das ist ein **Use-Case-Konflikt**.

**Lösung**: Müll-Filter sorgt dafür, dass die widersprüchlichen Text-Specs NICHT mehr
injiziert werden → der Master-Prompt gewinnt. Aber für korrekte Stick-Figures sollte
der User entweder:
- das Preset auf `stick_minimal` wechseln (passend zum Material), ODER
- Cartoon-Char-PNGs hochladen (passend zum aktuellen Preset)

Diese UI-Entscheidung ist Teil von Phase 2 (out of scope dieses Bugfixes).

## Phase 2 — charsheet-PNGs als Bild-Referenz ans Modell

Aktueller Stand: die charsheet-PNG-Dateien werden nirgendwo als Bild-Referenz ans
Bildmodell geschickt — nur die Text-Beschreibungen werden injiziert. Das ist der
eigentliche Architektur-Bug.

Phase 2 (eigener PR):
- charsheets als Bild-Refs via `image_urls` an `_kie_submit_image` anhängen
- Dazu müssen die lokalen PNGs öffentlich erreichbar sein (Upload-CDN wie bei
  `char_ref_url.txt`)
- Im `_batch_generate_worker`: pro Szene mit `concrete_entity.startswith("char_")` den
  spezifischen charsheet-PNG anhängen statt nur den kanal-globalen

## Verifikation

Tests: 113/114 grün (1 flaky B-1 E2E wegen KIE-403, nicht durch diesen Fix)

5 neue Tests in `tests/test_cinematic_e2e.py`:
- `t_char_filter_mull_empty`
- `t_char_filter_mull_test_patterns`
- `t_char_filter_real_charsheets_pass`
- `t_char_filter_build_image_prompt`
- `t_char_filter_load_char_refs`
