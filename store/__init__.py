"""store — geteilte SQLite-Datenschicht für Shorts/Upload/Control.

Bewusst getrennt von der bestehenden channels/<cid>/-JSON-Welt (plan.json,
charsheets, meta.json — nichts davon wird angefasst). SQLite kommt nur für
Zustand zum Einsatz, der (a) über Entitäten hinweg abfragbar sein muss oder
(b) Zustandsübergänge mit Neustart-Festigkeit braucht (OAuth-Tokens,
Upload-Queue, Quota-Ledger) — siehe store/db.py.
"""
from __future__ import annotations
