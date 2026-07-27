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
"""

from __future__ import annotations

import os
import subprocess

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
