# Diagnose: Charakter-Konsistenz bei Bild-Generierung

**Datum**: 2026-07-09
**Status**: Offen — Hypothese bestätigt durch Daten, Fix noch nicht umgesetzt
**Reporter**: Noel

## Symptom

Bei Video 2 ("19 year old fooled the world") rendert KIE alle Szenen mit
Charakter 1 mit nahezu identischer Mimik, Gestik und Komposition. Video 1
("Year old faked a company") hat das Problem nicht.

## Hypothese (Noel)

`thinking_level="high"` für die Prompt-Generierung produziert konservativere,
deterministischere Charakter-Beschreibungen die KIE dann 1:1 übernimmt.
Bei `"low"` hätte Gemini mehr Freiheitsgrade im Prompt gelassen → variablere Bilder.

## Daten-Sammlung

Verglichen wurden die ersten 6 Szenen-Prompts beider Videos plus die ersten
8 generierten Bilder (perceptual distance über 8x8-RGB-Grid).

### Quantitative Befunde

**Detail-Dichte in Prompts** (Regex-Match auf typische Charakter-Adjektive):

| Detail-Typ | Video 1 | Video 2 | Delta |
|---|---|---|---|
| Augen-Detail (blue/wide/intense/etc.) | 4% | 11% | **+175%** |
| Hair-Detail | 69% | 66% | ≈ gleich |
| Expression-Detail | 7% | 12% | **+71%** |
| Outfit-Detail | 24% | 24% | gleich |
| **Durchschnitt** | **1.04** | **1.14** | +10% |

**Beispiel-Prompts Side-by-Side (Szene 4, identischer Skript-Text):**

- Video 1: *"A medium shot of the blonde woman, now looking older but maintaining her serious expression and black turtleneck, standing on a dark stage."*
- Video 2: *"A blonde woman with a serious, intense expression, **wide unblinking blue eyes**, and her hair in a messy bun, wearing a black turtleneck, stands in a sleek, multi-story glass corporate lobby."*

In Video 2 taucht "wide unblinking blue eyes" + "messy bun" in **Szene 0, 4, 5**
fast wortgleich auf — KIE bekommt 3× die gleiche exakte Beschreibung und
rendert sie entsprechend identisch.

**Generierte Bilder (perceptual distance, benachbarte Paare):**

- Video 1: avg 202, range 108–302 — hohe Varianz
- Video 2: avg 134, range 108–193 — niedrigere Varianz, viele Szenen mit
  perceptual distance ~110 was auf gleiche Komposition hindeutet

### Was gleich geblieben ist

- Charsheets (Beschreibung + Bilder): identisch zwischen V1 und V2
- Master-Prompt: identisch (channel-level, kein per-video Override)
- Style-Ref-Image (`channels/default/style_ref.png`): identisch, gleicher Timestamp
- `nano-banana-2` Bild-Modell für beide Videos

### Was unterschiedlich ist

- **Einziger konfigurierbarer Unterschied**: `thinking_level` für die Prompt-
  Generierung in `engine/prompts.py` Z.225 — war `"low"` als V1 erzeugt wurde,
  ist `"high"` jetzt. Die Code-Kommentare (dashboard.py Z.613-615 und
  engine/prompts.py Z.226-229) dokumentieren diese Hin-und-Her-Schaltung
  explizit und beschreiben genau dieses Symptom (konservativere Outputs bei
  `"high"`).

## Hypothese-Bewertung

**Wahrscheinlich korrekt.** Die quantitative Evidenz (175% mehr Augen-Details
in V2-Prompts bei sonst gleicher Pipeline) passt zum Muster das `"high"`-
Thinking-Reasoning produziert. Charsheet-Identität schließt andere
Hauptursachen aus.

**Restunsicherheit**: ein kontrollierter Test (gleiches Skript, einmal mit
`"high"` und einmal mit `"low"` Prompt-Phase, dann Bilder vergleichen) wäre
die einzige 100%ige Bestätigung. Aufwand: 1-3€ KIE-Kosten + 5-15 min
Renderzeit.

## Vorschlag für Fix (wenn bestätigt)

`engine/prompts.py` Z.225 zurück auf `thinking_level="low"` setzen. Das
war der Default vor Juli 2026 (siehe Kommentar dashboard.py Z.613).

**Trade-off** (aus Code-Kommentaren dokumentiert):

- `"low"` → mehr Variabilität, aber ggf. **schlechtere Prompt-Qualität**
  (Reasoning fehlt für kreative Bild-Prompts)
- `"high"` → konsistentere Prompt-Qualität, aber **zu enge Bindung an
  Referenzbilder**

**Alternative ohne thinking_level**: pro Charakter-Detail-Pattern eine
sanfte Variation einbauen (z.B. "wide blue eyes" → "bright blue eyes" in
alternierenden Szenen), damit die wortgleiche Wiederholung aufgelöst wird.
Aufwand: ~1-2 Tage, fragiles Prompt-Hacking.

## Nicht weiter verfolgt

- **Chain-Anker (previous scene als ref)**: war eine Hypothese, aber das ist
  gewolltes Verhalten für visuelle Continuity und wirkt in V1 genauso.
- **Style-Ref-Anwendung**: Code-Logik prüft `not has_prior_ref` →
  Style-Ref wird nur ohne Chain-Refs angewendet, in den meisten Szenen also
  gar nicht. Nicht die Ursache.

## Reproduktion

Falls du selbst testen willst:

```bash
# Backup der plan.json
cp channels/default/videos/19_year_old_fooled_the_world/generated/plan.json /tmp/plan_v2_backup.json

# Temporär thinking_level ändern
# engine/prompts.py Z.225: thinking_level="high" → "low"

# 3-5 Szenen-Prompts neu generieren (z.B. via "Plan neu erstellen" Button
# im UI, dann stoppen nach ~30s sobald erste Prompts da sind)

# Side-by-side Diff: altes plan.json vs. neu
diff /tmp/plan_v2_backup.json channels/default/videos/19_year_old_fooled_the_world/generated/plan.json
```

Wenn die neu generierten Prompts **variabler** sind (weniger wortgleiche
Wiederholung von Charakter-Adjektiven), ist die Hypothese bestätigt.