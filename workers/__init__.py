"""workers/ — Hintergrund-Worker, aus dashboard.py extrahiert (Refactor Phase 3).

Jeder Worker lazy-importiert `dashboard` für die verbliebenen God-Modul-Helfer
(Job-Dicts+Locks, load_v_meta/save_v_meta, get_video_image_model, ...) --
dieselbe Konvention, die shorts/api.py, youtube/upload.py und control/api.py
bereits erfolgreich nutzen (siehe deren Modul-Docstrings). dashboard.py
importiert die Worker-Funktion umgekehrt direkt von hier; der bestehende
threading.Thread(target=...)-Aufruf an der Route bleibt unverändert.
"""
