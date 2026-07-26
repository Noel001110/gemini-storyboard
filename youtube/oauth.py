"""youtube/oauth.py — Authorization-Code-Flow + Refresh, handgebaut (kein
google-auth/google-auth-oauthlib) -- Plan-Entscheidung: der OAuth-Bedarf ist schmal
und stabil (ein Grant-Type, zwei Google-Endpunkte), dieselbe Form von Problem, das
kie_key()/post_gemini_native() in dashboard.py schon mit reinem urllib.request lösen.

Redirect-URI ist fest auf denselben, permanent laufenden Dashboard-Server verdrahtet
(127.0.0.1:8010, siehe dashboard.py main()-Docstring zum festen Port) -- der Callback
ist eine weitere gemountete Route auf DEMSELBEN Server (youtube/api.py, GET
/api/youtube/oauth_callback), kein zweiter Ephemeral-Listener.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import store.db as db

OAUTH_CLIENT_FILE = os.path.expanduser("~/.youtube_oauth_client.json")
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
# youtube.force-ssl (Juli 2026 dazugekommen) wird NUR für captions.insert gebraucht --
# per WebFetch gegen die offizielle captions.insert-Doku verifiziert, youtube.upload
# allein reicht dafür nicht (wohl aber für videos.insert/thumbnails.set). Ein Kanal,
# der VOR diesem Scope-Zusatz schon verbunden war, muss einmalig über Control neu
# verbunden werden -- sein alter Refresh-Token deckt den neuen Scope nicht automatisch
# ab (Google-OAuth-Verhalten: Scopes sind an den Consent-Zeitpunkt gebunden).
SCOPES = ("https://www.googleapis.com/auth/youtube.upload "
          "https://www.googleapis.com/auth/youtube.readonly "
          "https://www.googleapis.com/auth/youtube.force-ssl")
REDIRECT_URI = "http://127.0.0.1:8010/api/youtube/oauth_callback"

# Proaktiv VOR Ablauf erneuern, nicht erst wenn ein Call mit 401 scheitert -- genug
# Sicherheitsabstand für einen langsamen Resumable-Upload-Chunk mittendrin.
REFRESH_MARGIN_SEC = 120


def client_configured() -> bool:
    """Gate für den Upload-Worker-Thread (dashboard.py main()) -- Phase 3 startet
    nur, wenn diese Datei existiert (Google-Cloud-OAuth-Client, vom Nutzer selbst
    einmalig angelegt und hier abgelegt)."""
    return os.path.exists(OAUTH_CLIENT_FILE)


def _client_config() -> dict:
    return json.load(open(OAUTH_CLIENT_FILE))


def build_auth_url(state: str) -> str:
    cfg = _client_config()
    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        # IMMER bei jedem Auth-Request, nicht nur beim allerersten -- sonst liefert
        # Google bei einer stillen Re-Autorisierung keinen refresh_token zurück
        # (offizielle Doku, siehe Plan).
        "prompt": "consent",
        "state": state,
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def _post_token(payload: dict) -> dict:
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST",
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # urllib gibt bei einem HTTPError nur "HTTP Error 403: Forbidden" aus, ohne
        # Googles eigentliche Fehlerbeschreibung (JSON-Body mit error/error_description)
        # -- ohne die ist ein OAuth-Fehlschlag praktisch nicht diagnostizierbar.
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {body}") from e


def _confirm_channel(access_token: str) -> tuple:
    """channels.list?mine=true (1 Einheit) -- bestätigt, WELCHER echte YouTube-Kanal
    gerade verbunden wurde, bevor irgendetwas gespeichert wird."""
    url = "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        # Gleicher Grund wie bei _post_token oben -- ohne den Response-Body ist ein
        # 403 hier nicht von "API nicht aktiviert" vs. "falsches Scope" vs. irgendwas
        # anderem zu unterscheiden.
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"channels.list HTTP {e.code} {e.reason}: {body}") from e
    db.record_usage(1, "channels.list", caller="oauth")
    items = resp.get("items") or []
    if not items:
        raise RuntimeError("channels.list lieferte keinen Kanal für dieses Google-Konto zurück.")
    ch = items[0]
    return ch["id"], ch["snippet"]["title"]


def exchange_code(cid: str, code: str) -> dict:
    """Tauscht den Authorization-Code gegen Access+Refresh-Token, bestätigt den
    verbundenen Kanal (_confirm_channel) und speichert alles unter `cid` ab."""
    cfg = _client_config()
    resp = _post_token({
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    })
    access_token = resp["access_token"]
    refresh_token = resp.get("refresh_token")
    if not refresh_token:
        # Sollte wegen access_type=offline + prompt=consent nicht passieren; ohne
        # refresh_token könnte dieses System nach Ablauf des Access-Tokens nie wieder
        # selbstständig hochladen.
        raise RuntimeError("Google hat kein refresh_token geliefert — bitte erneut "
                            "verbinden und die Berechtigung dabei explizit bestätigen.")
    expires_at = time.time() + resp.get("expires_in", 3600)

    channel_id, channel_title = _confirm_channel(access_token)
    db.save_tokens(cid, access_token, refresh_token, expires_at, SCOPES,
                   channel_id, channel_title)
    return {"channel_id": channel_id, "channel_title": channel_title}


def refresh_access_token(cid: str) -> str:
    tokens = db.get_tokens(cid)
    if not tokens:
        raise RuntimeError(f"Kein OAuth für Kanal '{cid}' verbunden.")
    cfg = _client_config()
    resp = _post_token({
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "refresh_token": tokens["refresh_token"],
        "grant_type": "refresh_token",
    })
    access_token = resp["access_token"]
    expires_at = time.time() + resp.get("expires_in", 3600)
    db.update_access_token(cid, access_token, expires_at)
    return access_token


def get_valid_access_token(cid: str) -> str | None:
    """Einzige Stelle, die je auf rohe Tokens zugreift -- refresht proaktiv
    REFRESH_MARGIN_SEC vor Ablauf. None wenn der Kanal nie verbunden wurde
    (kein Fehler -- youtube/metadata.py's Live-Kategorie-Abruf nutzt genau das als
    "noch kein OAuth" und fällt auf die dokumentierte Fallback-Liste zurück)."""
    tokens = db.get_tokens(cid)
    if not tokens:
        return None
    if tokens["expires_at"] - REFRESH_MARGIN_SEC > time.time():
        return tokens["access_token"]
    return refresh_access_token(cid)
