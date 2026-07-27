"""TikTok OAuth2 Integration"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

import store.db as db

CLIENT_KEY = "sbawwfb9dpfdlratae"
CLIENT_SECRET = "LysTAVkPQdrNwKDdM23kDDtSHP4OhN4m"
REDIRECT_URI = "https://example.com/"

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
SCOPES = "video.publish,user.info.basic"

def build_auth_url(state: str, code_challenge: str) -> str:
    params = {
        "client_key": CLIENT_KEY,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256"
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

def exchange_code(cid: str, code: str, code_verifier: str) -> None:
    data = urllib.parse.urlencode({
        "client_key": CLIENT_KEY,
        "client_secret": CLIENT_SECRET,
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
    data = urllib.parse.urlencode({
        "client_key": CLIENT_KEY,
        "client_secret": CLIENT_SECRET,
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
    refresh_token = res.get("refresh_token")
    expires_in = res.get("expires_in", 86400)
    refresh_expires_in = res.get("refresh_expires_in", 31536000)
    
    expires_at = time.time() + expires_in
    refresh_expires_at = time.time() + refresh_expires_in
    
    update_access_token(cid, access_token, expires_at, refresh_token, refresh_expires_at)
    return access_token
