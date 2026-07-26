"""youtube/quota.py — Tages-Kontingent-Prüfung fürs Hochladen. Reine Geschäftsregel
über store/db.py's Ledger (record_usage/get_uploads_today) -- die eigentliche Zähl-
und Reset-Logik (Mitternacht Pacific) lebt dort, dieses Modul kennt nur den
bindenden Schwellenwert.

100 videos.insert/Tag ist Googles Standard-Zuteilung, eine separate, benannte
Obergrenze -- NICHT über die allgemeine 10.000-Einheiten/Tag-Punkterechnung
zu verwechseln (siehe Plan, Abschnitt "Verifizierte Ausgangslage").
"""
from __future__ import annotations

import store.db as db

DAILY_UPLOAD_LIMIT = 100


def uploads_remaining_today() -> int:
    return max(0, DAILY_UPLOAD_LIMIT - db.get_uploads_today())


def assert_quota_available() -> None:
    if uploads_remaining_today() <= 0:
        raise RuntimeError(
            f"YouTube-Tageskontingent erschöpft ({DAILY_UPLOAD_LIMIT} videos.insert/Tag, "
            "Reset um Mitternacht Pacific) -- Upload bleibt in der Warteschlange und wird "
            "automatisch erneut versucht, sobald wieder Kontingent verfügbar ist."
        )
