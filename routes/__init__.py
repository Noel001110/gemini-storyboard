"""Routes package — HTTP-Endpoints der Dashboard-App.

Layout:
    frontend_routes.py  — statische Seiten (@app.route("/"), serve_html, etc.)
    dashboard_routes.py — alle @app.route("/api/...") JSON-Endpoints

Beide Module erhalten eine Flask-Blueprint-artige register(app)-Funktion statt
@app.route direkt zu nutzen — Lazy-Imports gegen Zyklen mit engine/.
"""