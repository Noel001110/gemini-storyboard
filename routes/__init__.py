"""Routes package — HTTP-Endpoints der Dashboard-App.

Layout (Stand M.1):
    frontend_routes.py  — statische Seiten (kommt in späterer Phase)
    dashboard_routes.py — JSON-API-Endpoints (kommt in späterer Phase)

Stand-Entscheidung M.1 (2026-07-07):
Der existierende `Handler` in dashboard.py (Klasse H, Zeile 3568–4643) referenziert
60+ globale Funktionen aus dashboard.py (load_channels, BATCH_JOBS, _BATCH_JOBS_LOCK,
read_master, get_video_image_model, …). Ein vollständiger Umzug des Handlers in
dieses Package würde einen massiven, riskanten Diff erzeugen (jeder Aufruf müsste
neu verdrahtet werden), ohne den XXL-Wachstumsschmerz von dashboard.py zu lösen
(das eigentliche Problem sind die 3.300 Zeilen Render-/Audio-/Szenen-Logik in
Z. 1300–2700, nicht die 1.100-Zeilen-Handler-Klasse).

M.1 beschränkt sich deshalb bewusst auf:
  1. Definition der Lazy-Import-Konvention für engine/* (siehe register_engine_paths)
  2. Saubere Schnittstelle für künftige Handler-Module ohne Zyklen
  3. Klare Markierung der Erweiterungspunkte (ENDPOINT_REGISTRY) für M.6+
     wenn dashboard.py zum Orchestrator geschrumpft ist

Wenn dashboard.py in M.6 hinreichend klein ist (< 1.000 Zeilen), wird M.6 den
Handler-Inhalt aus dashboard.py hierher ziehen — ohne dass an M.1 etwas geändert
werden muss (die Konvention bleibt stabil).
"""

from __future__ import annotations

# Lazy-Import-Konvention für engine-Module.
# Andere Module (insbesondere dashboard.py nach M.2+) dürfen engine-Module nicht
# oben importieren, sondern nur innerhalb von Funktionen über diese Helfer.
# Das verhindert Import-Zyklen zwischen engine.scenes ↔ engine.render ↔ engine.audio.
_ENGINE_PATHS_REGISTERED = False


def register_engine_paths() -> None:
    """Einmaliger Aufruf beim App-Start. Idempotent.

    Stellt sicher, dass `engine.*` als Top-Level-Pakete von jedem Modul aus
    importierbar sind (auch wenn dashboard.py per 'python3 dashboard.py' gestartet
    wird und der CWD nicht automatisch auf sys.path liegt).

    Wird in M.2+ noetig, sobald dashboard.py per `from engine.scenes import ...`
    zu importieren beginnt. Bis dahin no-op.
    """
    global _ENGINE_PATHS_REGISTERED
    if _ENGINE_PATHS_REGISTERED:
        return
    import os
    import sys
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if here not in sys.path:
        sys.path.insert(0, here)
    _ENGINE_PATHS_REGISTERED = True


# Endpoint-Registry (Erweiterungspunkt fuer spaetere Phasen).
# Heute leer; in M.6+ werden hier Endpoint-Decoratoren registriert,
# die dashboard.py beim Start an den Handler uebergibt.
ENDPOINT_REGISTRY: list = []


def register_endpoint(method: str, path: str, handler) -> None:
    """Mittelfristige Schnittstelle fuer deklarative Endpoint-Registrierung.

    Wird in spaeteren Phasen genutzt; heute nur Doku der Konvention.
    """
    ENDPOINT_REGISTRY.append({"method": method.upper(), "path": path, "handler": handler})