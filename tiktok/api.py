"""tiktok/api.py"""
from __future__ import annotations

import json
import urllib.parse
from urllib.error import URLError

import store.db as db
from tiktok.oauth import build_auth_url, client_configured, exchange_code

# Simple local state for CSRF like youtube
_PENDING_STATES: dict = {}
import secrets
import time
import base64
import hashlib
import os

def _new_state(cid: str) -> tuple[str, str, str]:
    state = secrets.token_urlsafe(24)
    code_verifier = base64.urlsafe_b64encode(os.urandom(32)).decode('utf-8').rstrip('=')
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode('utf-8')).digest()).decode('utf-8').rstrip('=')
    _PENDING_STATES[state] = (cid, code_verifier, time.time())
    return state, code_verifier, code_challenge

def _pop_state(state: str) -> tuple[str, str] | None:
    entry = _PENDING_STATES.pop(state, None)
    if not entry:
        return None
    cid, code_verifier, ts = entry
    if time.time() - ts > 600:
        return None
    return cid, code_verifier

def handle(method, path, handler, qs, cid, vid, body):
    if method == "GET" and path == "/tiktok/oauth/login":
        if not client_configured():
            handler._send(400, {"error": "~/.tiktok_oauth_client.json fehlt — OAuth-Client "
                                          "noch nicht eingerichtet."})
            return True, None
        state, code_verifier, code_challenge = _new_state(cid)
        url = build_auth_url(state, code_challenge)
        handler.send_response(302)
        handler.send_header("Location", url)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return True, None

    if method == "GET" and path == "/tiktok/oauth/callback":
        error = (qs.get("error") or [""])[0]
        if error:
            handler._send(400, {"error": error})
            return True, None
            
        code = (qs.get("code") or [""])[0]
        state = (qs.get("state") or [""])[0]
        
        state_data = _pop_state(state)
        if not state_data:
            handler._send(400, {"error": "Invalid or expired state"})
            return True, None
            
        cid_from_state, code_verifier = state_data
            
        try:
            exchange_code(cid_from_state, code, code_verifier)
        except Exception as e:
            handler._send(500, {"error": f"Failed to exchange token: {e}"})
            return True, None
            
        handler.send_response(302)
        handler.send_header("Location", "/")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return True, None

    return False, None
