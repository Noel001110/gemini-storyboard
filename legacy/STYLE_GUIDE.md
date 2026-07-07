# ⚠️  LEGACY — NICHT MEHR AKTUELL  ⚠️
#
# Dieses Dokument beschreibt den Stil des allerersten Video-Versuchs (Yeonmi-Story,
# schwarz-weiße Tusche-Line-Art). Es ist **NICHT** der aktuelle Kanal-Stil des Dashboards.
#
# Aktueller Stil: flacher 2D-Cartoon (warm, dokumentarisch), siehe
#   engine/presets.py → PRESET_MASTERS["flat_cartoon_doc"]
# Plan-Kontext: CINEMATIC_UPGRADE_PLAN.md §10
#
# Die alte Pipeline (gen.py, scenes.tsv, run_batch.sh) und der alte Asset-Pool
# (yeonmi_storyboard/) sind aus dem Dashboard entkoppelt. Sie wurden am 2026-07-07 nach
# legacy/ verschoben (Phase Q.1 des Upgrade-Plans). Nur noch historisch referenziert —
# NICHT weiterpflegen, jeder neue Kanal startet mit den Presets in engine/presets.py.
# ───────────────────────────────────────────────────────────────────────────────────────────

# Yeonmi Storyboard — Stil-Bibel & Pipeline

Ziel: Jedes Bild sieht aus wie aus DEMSELBEN Video, gezeichnet von DERSELBEN Hand.
Scharfe, saubere Line-Art-Skizzen — KEIN Blur, KEIN Whiteboard-Rahmen, KEIN Grunge-Hintergrund.

## Finale Konfiguration (festgelegt)

- **Modell:** `gemini-3-pro-image` (Nano Banana Pro) über Google AI Studio API (Key in `~/.gemini_key`).
- **Auflösung:** `IMG_SIZE=2K` → 2752×1536 px, reinweißer Hintergrund.
  (4K = `gemini-3-pro-image` rendert dann Grunge/Rausch in leere Flächen → NICHT nutzen.
   Flash-Modelle = sauber, aber max. ~1376 px, kein 4K.)
- **KEIN Referenzbild.** Der Stil wird allein über den Master-Prompt in `gen.py` (Variable `MASTER`)
  getragen. Referenz würde Kompositionen aneinander angleichen → „immer dasselbe Bild".
  (Optional reaktivierbar mit `USE_REF=1`, aber bewusst aus.)

## Generieren

    # ein Bild
    GEN_MODEL=gemini-3-pro-image IMG_SIZE=2K python3 gen.py "0:34.png" "<Szenen-Prompt>"

    # Batch (alle / erste N) — Prompts aus scenes.tsv
    GEN_MODEL=gemini-3-pro-image IMG_SIZE=2K bash run_batch.sh          # alle 51
    GEN_MODEL=gemini-3-pro-image IMG_SIZE=2K LIMIT=10 bash run_batch.sh # nur erste 10

`run_batch.sh` überspringt bereits vorhandene PNGs → Resume-fähig. Prompt-Aufbau:
`[Szene aus scenes.tsv] + [MASTER-Block aus gen.py]`.

## Wichtige Erkenntnisse

- **Schärfe-Fix:** Wörter wie „whiteboard / pencil / storyboard quality" verursachen Smudges +
  echte Whiteboard-Rahmen → raus. Stattdessen „crisp, sharp, high-contrast, clean white, no grain".
- **Safety-Filter:** Kind-/Leid-/Tod-/Menschenhandel-Motive werden teils blockiert (`IMAGE_SAFETY`).
  Solche Frames symbolisch formulieren (Bsp. 0:27: „leere Schüssel + greifende Hände" statt
  „hungerndes Kind"). Betrifft im Skript v.a. 2:34 / 2:40 / 2:47 / 3:14 — vorab entschärfen.
- **Format:** Pro liefert die Bytes als JPEG (trotz `.png`-Endung). Für YouTube egal; bei Bedarf
  am Ende verlustfrei nach echtem PNG konvertieren.

## Charakter-Anker (im MASTER-Block fest verdrahtet)

Yeonmi: schlankes 13-jähriges nordkoreanisches Mädchen, schulterlanges schwarzes Haar im tiefen
Pferdeschwanz mit losen Strähnen, rundes weiches Gesicht, große Augen, dünner abgetragener Mantel
über einfacher Hose, leicht gebeugte fröstelnde Haltung. Mutter/Vater analog im MASTER ergänzbar.
