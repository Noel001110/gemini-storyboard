"""TikTok OAuth2 Integration"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import store.db as db

# Juli 2026 (Secret-Hygiene-Fix): client_key/client_secret waren zuvor hier hart
# codiert und landeten dadurch versehentlich im öffentlichen Git-Repo. Gleiches
# Muster wie youtube/oauth.py (OAUTH_CLIENT_FILE/client_configured()) -- eine
# gitignored Datei im Home-Verzeichnis, nie Teil des Repos.
OAUTH_CLIENT_FILE = os.path.expanduser("~/.tiktok_oauth_client.json")
# War zuvor "https://example.com/" -- TikToks eigener Doku-Platzhalter, nie
# funktionsfähig. TikToks Login Kit für die Plattform "Desktop" erlaubt
# localhost/127.0.0.1-Redirect-URIs (anders als "Web", das eine verifizierte
# HTTPS-Domain verlangt) -- App im TikTok-Developer-Portal entsprechend als
# "Desktop" registrieren, dann zeigt diese URI auf die bereits existierende
# lokale Callback-Route (tiktok/api.py).
REDIRECT_URI = "http://127.0.0.1:8010/tiktok/oauth/callback"

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
SCOPES = "video.publish,user.info.basic"


def client_configured() -> bool:
    return os.path.exists(OAUTH_CLIENT_FILE)


def _client() -> dict:
    return json.load(open(OAUTH_CLIENT_FILE))


def build_auth_url(state: str, code_challenge: str) -> str:
    params = {
        "client_key": _client()["client_key"],
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256"
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

def exchange_code(cid: str, code: str, code_verifier: str) -> None:
    client = _client()
    data = urllib.parse.urlencode({
        "client_key": client["client_key"],
        "client_secret": client["client_secret"],
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
        "code_verifier": code_verifier
    }).encode("utf-8")

    req = urllib.request.Request(
        TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"}
    )
    
    with urllib.request.urlopen(req) as resp:
        res = json.loads(resp.read().decode("utf-8"))

    access_token = res.get("access_token")
    if not access_token:
        # TikToks OAuth-Token-Endpoint nutzt ein anderes Fehlerformat als die
        # Content-Posting-API (flaches error/error_description statt
        # error.code) -- ungeprüft durchgereicht landete ein fehlendes
        # access_token bisher als NOT-NULL-Constraint-Fehler in der DB, ohne
        # den eigentlichen TikTok-Fehler (z.B. invalid_grant) sichtbar zu
        # machen. Juli 2026, gefunden beim ersten echten Reconnect-Versuch.
        raise ValueError(
            f"TikTok token exchange failed: "
            f"{res.get('error_description') or res.get('error') or res}"
        )
    refresh_token = res.get("refresh_token")
    expires_in = res.get("expires_in", 86400)
    refresh_expires_in = res.get("refresh_expires_in", 31536000)
    open_id = res.get("open_id")

    expires_at = time.time() + expires_in
    refresh_expires_at = time.time() + refresh_expires_in

    # Store tokens in DB
    conn = db.get_connection()
    with db.WRITE_LOCK:
        conn.execute(
            """INSERT OR REPLACE INTO tiktok_oauth
               (cid, access_token, refresh_token, expires_at, refresh_expires_at, open_id, obtained_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (cid, access_token, refresh_token, expires_at, refresh_expires_at, open_id, time.time())
        )
        conn.commit()

def get_tokens(cid: str) -> dict | None:
    conn = db.get_connection()
    row = conn.execute("SELECT * FROM tiktok_oauth WHERE cid = ?", (cid,)).fetchone()
    if not row:
        return None
    return dict(row)

def update_access_token(cid: str, access_token: str, expires_at: float, refresh_token: str, refresh_expires_at: float) -> None:
    conn = db.get_connection()
    with db.WRITE_LOCK:
        conn.execute(
            """UPDATE tiktok_oauth 
               SET access_token = ?, expires_at = ?, refresh_token = ?, refresh_expires_at = ?
               WHERE cid = ?""",
            (access_token, expires_at, refresh_token, refresh_expires_at, cid)
        )
        conn.commit()

def refresh_if_needed(cid: str) -> str:
    tokens = get_tokens(cid)
    if not tokens:
        raise ValueError(f"No TikTok tokens for {cid}")
        
    if time.time() < tokens["expires_at"] - 120:
        return tokens["access_token"]
        
    # Refresh
    client = _client()
    data = urllib.parse.urlencode({
        "client_key": client["client_key"],
        "client_secret": client["client_secret"],
        "refresh_token": tokens["refresh_token"],
        "grant_type": "refresh_token",
    }).encode("utf-8")
    
    req = urllib.request.Request(
        TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"}
    )
    
    with urllib.request.urlopen(req) as resp:
        res = json.loads(resp.read().decode("utf-8"))

    access_token = res.get("access_token")
    if not access_token:
        raise ValueError(
            f"TikTok token refresh failed: "
            f"{res.get('error_description') or res.get('error') or res}"
        )
    refresh_token = res.get("refresh_token")
    expires_in = res.get("expires_in", 86400)
    refresh_expires_in = res.get("refresh_expires_in", 31536000)

    expires_at = time.time() + expires_in
    refresh_expires_at = time.time() + refresh_expires_in

    update_access_token(cid, access_token, expires_at, refresh_token, refresh_expires_at)
    return access_token
