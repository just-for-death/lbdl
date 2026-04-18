"""
API authentication for lbdl.

Set LBDL_API_TOKEN to require every client to present either:
  • Header: Authorization: Bearer <token>
  • Cookie: lbdl_session (set via POST /api/auth/login from the web UI)

When LBDL_API_TOKEN is unset or empty, all routes remain open (legacy behaviour).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Optional

from starlette.requests import Request
from starlette.websockets import WebSocket

AUTH_COOKIE = "lbdl_session"
TOKEN_ENV = "LBDL_API_TOKEN"


def _pepper() -> bytes:
    return b"lbdl-session-cookie-v1"


def session_cookie_value(api_token: str) -> str:
    """Opaque cookie value derived from the API token (not the raw secret)."""
    return hmac.new(api_token.encode("utf-8"), _pepper(), hashlib.sha256).hexdigest()


def get_api_token() -> Optional[str]:
    t = (os.getenv(TOKEN_ENV) or "").strip()
    return t or None


def auth_enabled() -> bool:
    return get_api_token() is not None


def verify_token_string(presented: str) -> bool:
    expected = get_api_token()
    if not expected:
        return True
    try:
        return secrets.compare_digest(presented.strip(), expected)
    except Exception:
        return False


def verify_request(request: Request) -> bool:
    if not auth_enabled():
        return True

    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        if verify_token_string(auth[7:].strip()):
            return True

    cookie = request.cookies.get(AUTH_COOKIE)
    expected = get_api_token()
    if cookie and expected:
        try:
            if hmac.compare_digest(cookie, session_cookie_value(expected)):
                return True
        except Exception:
            pass
    return False


def verify_websocket(websocket: WebSocket) -> bool:
    if not auth_enabled():
        return True

    auth = websocket.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        if verify_token_string(auth[7:].strip()):
            return True

    q = websocket.query_params.get("token")
    if q and verify_token_string(q):
        return True

    cookie = websocket.cookies.get(AUTH_COOKIE)
    expected = get_api_token()
    if cookie and expected:
        try:
            if hmac.compare_digest(cookie, session_cookie_value(expected)):
                return True
        except Exception:
            pass
    return False


def is_public_path(path: str) -> bool:
    """Paths that never require authentication (static assets + login + health)."""
    if path == "/health":
        return True
    if path == "/":
        return True
    if path.startswith("/static/"):
        return True
    if path in (
        "/sw.js",
        "/manifest.json",
        "/favicon.ico",
    ):
        return True
    if path.startswith("/apple-touch-icon"):
        return True
    if path.startswith("/api/auth/"):
        return True
    return False


def cookie_secure_flag() -> bool:
    return (os.getenv("LBDL_COOKIE_SECURE") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
