"""engine.upscale — Lokales 4K-Upscaling generierter Bilder.

Ruft die in Upscayl.app gebündelte Real-ESRGAN-ncnn-Vulkan-Binary per subprocess
auf (gleiches Muster wie die ffmpeg-Aufrufe in engine/render.py) — kein neuer
Python-Package-Dependency, passt zur Zero-Framework-Regel aus CLAUDE.md.

Modellwahl (Testlauf 2026-07-27, siehe Chat): "upscayl-lite-4x" statt der
mitgelieferten "upscayl-standard-4x"/"high-fidelity-4x" — ~15-35s/Bild statt
~4:30min/Bild, bei visuell identischem Ergebnis für den hier verwendeten
Flat-Illustration-Stil (harte Outlines, flache Farbflächen, keine feine
Foto-Textur, an der die größeren Modelle ihre zusätzliche Kapazität ausspielen
könnten). Bei 100+ Szenen pro Longform-Video ist das der Unterschied zwischen
"automatisch im Hintergrund" und "keine Nacht reicht".

Läuft automatisch direkt nach jedem Bild-Download (siehe Aufrufer in
dashboard.py:_image_job_worker_inner und engine/imagegen.py:_kie_poll_and_download)
— kein Button, kein Extra-Trigger im Frontend.

Batch-Modus (Testlauf 2026-07-27): ein einzelner upscayl-bin-Aufruf pro Bild
zahlt jedes Mal den vollen Prozess-Start/Modell-Load-Overhead (~15-35s/Bild
gemessen). upscayl-bin akzeptiert `-i`/`-o` auch als Verzeichnis und
verarbeitet dann alle enthaltenen Bilder in EINEM Prozess-Lauf — Overhead
amortisiert sich über die Bilder, real gemessen ~7s/Bild statt 15-35s/Bild
(2-5x schneller). Das ist KEINE echte GPU-Parallelität (dieser Mac hat eine
einzelne integrierte GPU — mehrere gleichzeitige Prozesse konkurrieren nur um
dieselbe Ressource und wurden real LANGSAMER gemessen, nicht schneller),
sondern reine Overhead-Amortisierung. Deshalb nutzt `workers/batch.py` (viele
Bilder pro Lauf) `upscale_images_batch()`, während Einzelbild-Pfade
(`/api/generate_one`, Thumbnail-Generierung — je nur 1 Bild, kein
Batch-Vorteil) bei `upscale_image_local()` bleiben.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

from core.logging import log_event as _log

UPSCAYL_BIN = "/Applications/Upscayl.app/Contents/Resources/bin/upscayl-bin"
UPSCAYL_MODELS_DIR = "/Applications/Upscayl.app/Contents/Resources/models"
UPSCAYL_MODEL = "upscayl-lite-4x"
# Match RENDER_WIDTH (engine/render.py) — kein Grund, breiter zu upscalen als
# der finale Video-Output ohnehin verwendet.
UPSCALE_TARGET_WIDTH = 3840


def upscale_image_local(in_path: str, target_width: int = UPSCALE_TARGET_WIDTH) -> None:
    """Skaliert das Bild unter `in_path` lokal per Real-ESRGAN hoch und ersetzt
    es in-place. Wirft RuntimeError bei fehlender Binary oder fehlgeschlagenem
    Aufruf — die Aufrufer fangen das ab und behalten im Fehlerfall das
    unskalierte Original, statt die ganze Szenen-Generierung scheitern zu
    lassen (ein Upscale-Hänger ist kein Grund, ein sonst fertiges Bild wegzuwerfen)."""
    if not os.path.exists(UPSCAYL_BIN):
        raise RuntimeError(
            f"Real-ESRGAN-Binary nicht gefunden unter {UPSCAYL_BIN} — Upscayl.app installiert?"
        )
    tmp_out = f"{in_path}.upscaled.jpg"
    result = subprocess.run(
        [UPSCAYL_BIN, "-i", in_path, "-o", tmp_out,
         "-n", UPSCAYL_MODEL, "-m", UPSCAYL_MODELS_DIR,
         "-w", str(target_width), "-f", "jpg"],
        capture_output=True, text=True, timeout=180,
    )
    if result.returncode != 0 or not os.path.exists(tmp_out):
        raise RuntimeError(f"Real-ESRGAN-Upscale fehlgeschlagen: {result.stderr[-500:]}")
    os.replace(tmp_out, in_path)
    _log("INFO", "upscale_done", path=in_path, target_width=target_width)


def upscale_image_local_safe(in_path: str) -> bool:
    """Wrapper für die Download-Callsites: loggt und schluckt Fehler statt sie
    zu propagieren, damit ein Upscale-Problem nie ein sonst erfolgreiches
    Bild-Ergebnis in einen fehlgeschlagenen Job verwandelt. Rückgabe: True bei
    Erfolg, False wenn das unskalierte Bild unverändert blieb."""
    try:
        upscale_image_local(in_path)
        return True
    except Exception as e:
        _log("WARN", "upscale_failed", path=in_path, error=str(e))
        return False


def upscale_images_batch(paths: list[str], target_width: int = UPSCALE_TARGET_WIDTH) -> int:
    """Skaliert mehrere Bilder in EINEM upscayl-bin-Lauf hoch (Verzeichnis-Modus)
    statt N einzelnen Prozess-Starts — amortisiert den Modell-Load-Overhead über
    alle Bilder (real gemessen ~7s/Bild statt 15-35s/Bild einzeln, siehe
    Modul-Docstring). Für `workers/batch.py`, wo viele Bilder pro Lauf anfallen;
    Einzelbild-Pfade bleiben bei `upscale_image_local()`.

    Kopiert erst in ein Temp-Verzeichnis (nicht move) statt direkt in `paths`
    hochzuskalieren -- bei einem Abbruch/Fehler mitten im Batch-Lauf bleiben so
    ALLE Originaldateien unverändert erhalten (gleiches Fallback-Prinzip wie
    `upscale_image_local`: ein Upscale-Problem darf nie ein sonst fertiges Bild
    kaputt machen). Nur Bilder, für die tatsächlich ein Output entstanden ist,
    werden am Ende per `os.replace` über ihr Original gelegt.

    Rückgabe: Anzahl tatsächlich hochskalierter Bilder (für Logging/Sweep-Zähler
    im Aufrufer)."""
    if not paths:
        return 0
    if not os.path.exists(UPSCAYL_BIN):
        _log("WARN", "upscale_batch_failed", count=len(paths),
             error=f"Binary nicht gefunden unter {UPSCAYL_BIN}")
        return 0

    tmp_in = tempfile.mkdtemp(prefix="upscale_batch_in_")
    tmp_out = tempfile.mkdtemp(prefix="upscale_batch_out_")
    try:
        # Eindeutige Namen (Index-Präfix) statt Original-Dateinamen -- die
        # Aufrufer übergeben Pfade aus womöglich verschiedenen Videos/Kanälen,
        # deren Dateinamen (000.jpg, 001.jpg, ...) sich sonst kollidieren würden.
        name_map = {}
        for idx, src in enumerate(paths):
            tmp_name = f"{idx:04d}.jpg"
            shutil.copy2(src, os.path.join(tmp_in, tmp_name))
            name_map[tmp_name] = src

        result = subprocess.run(
            [UPSCAYL_BIN, "-i", tmp_in, "-o", tmp_out,
             "-n", UPSCAYL_MODEL, "-m", UPSCAYL_MODELS_DIR,
             "-w", str(target_width), "-f", "jpg"],
            capture_output=True, text=True, timeout=max(180, len(paths) * 20),
        )
        if result.returncode != 0:
            _log("WARN", "upscale_batch_failed", count=len(paths), error=result.stderr[-500:])
            return 0

        done = 0
        for tmp_name, src in name_map.items():
            out_file = os.path.join(tmp_out, tmp_name)
            if os.path.exists(out_file):
                os.replace(out_file, src)
                done += 1
            else:
                _log("WARN", "upscale_batch_missing_output", path=src)
        _log("INFO", "upscale_batch_done", requested=len(paths), done=done, target_width=target_width)
        return done
    finally:
        shutil.rmtree(tmp_in, ignore_errors=True)
        shutil.rmtree(tmp_out, ignore_errors=True)
