"""core/ — neutrales Fundament (Refactor Phase 1).

Leitregel gegen Zyklen: core/ importiert NICHTS aus app/, routes/, workers/,
engine/, dashboard.py -- alles darf core/ importieren, core/ importiert nichts
davon zurück. Das löst den bestehenden `import dashboard`-Knoten (lazy
Re-Imports in shorts/api.py, youtube/upload.py, control/api.py), ohne einen
neuen Zyklus einzuführen.
"""
