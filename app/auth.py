"""
HTTP Basic auth + signed session cookie for lbdl.

Credentials live in CONFIG_DIR/auth.json (created on first run with username admin
and password admin — change the password under Settings in the web UI).

Scripts may use: Authorization: Basic base64(username:password)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from starlette.requests import Request
from starlette.websockets import WebSocket

AUTH_FILENAME = "auth.json"
AUTH_COOKIE = "lbdl_session"
PBKDF2_ITERATIONS = 200_000

_auth_cache: Optional[dict[str, Any]] = None
_auth_mtime: float = 0.0


def _config_dir() -> Path:
    return Path(os.getenv("LBDL_CONFIG_DIR", "/app/config"))


def auth_json_path() -> Path:
    return _config_dir() / AUTH_FILENAME


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def ensure_auth_file() -> None:
    """Create auth.json with default admin / admin if missing."""
    path = auth_json_path()
    if path.exists():
        return
    salt = secrets.token_hex(16)
    data = {
        "username": "admin",
        "password_hash": _hash_password("admin", salt),
        "salt": salt,
        "session_secret": secrets.token_hex(32),
    }
    _atomic_write_json(path, data)


def _hash_password(password: str, salt_hex: str) -> str:
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return dk.hex()


def invalidate_cache() -> None:
    global _auth_cache, _auth_mtime
    _auth_cache = None
    _auth_mtime = 0.0


def load_auth() -> dict[str, Any]:
    global _auth_cache, _auth_mtime
    path = auth_json_path()
    if not path.exists():
        ensure_auth_file()
    m = path.stat().st_mtime
    if _auth_cache is not None and m == _auth_mtime:
        return _auth_cache
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    for key in ("username", "password_hash", "salt", "session_secret"):
        if key not in data or not isinstance(data[key], str):
            raise RuntimeError(
                f"{path} is invalid (missing {key!r}). Remove or fix the file, or delete it to recreate defaults."
            )
    _auth_cache = data
    _auth_mtime = m
    return _auth_cache


def verify_credentials(username: str, password: str) -> bool:
    auth = load_auth()
    try:
        exp_u = auth["username"]
        if len(username) != len(exp_u):
            return False
        if not secrets.compare_digest(username, exp_u):
            return False
        ph = _hash_password(password, auth["salt"])
        return hmac.compare_digest(ph, auth["password_hash"])
    except Exception:
        return False


def _sign_session(username: str, exp: int, session_secret_hex: str) -> str:
    secret = bytes.fromhex(session_secret_hex)
    msg = f"{username}:{exp}".encode()
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def make_session_cookie_value(username: str) -> str:
    auth = load_auth()
    exp = int(time.time()) + 60 * 60 * 24 * 365
    sig = _sign_session(username, exp, auth["session_secret"])
    payload = json.dumps({"u": username, "exp": exp, "sig": sig})
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _urlsafe_b64decode(s: str) -> bytes:
    """Decode base64url; tolerate missing padding."""
    pad = (-len(s)) % 4
    if pad:
        s += "=" * pad
    return base64.urlsafe_b64decode(s.encode())


def verify_session_cookie(cookie_val: str) -> bool:
    auth = load_auth()
    try:
        raw = _urlsafe_b64decode(cookie_val).decode()
        data = json.loads(raw)
        u, exp, sig = data["u"], int(data["exp"]), data["sig"]
        if time.time() > exp:
            return False
        if u != auth["username"]:
            return False
        expected = _sign_session(u, exp, auth["session_secret"])
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False


def _parse_basic(header: Optional[str]) -> Optional[tuple[str, str]]:
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(header[6:].strip()).decode("utf-8")
        u, _, p = raw.partition(":")
        return (u, p)
    except Exception:
        return None


def verify_request(request: Request) -> bool:
    # If Basic is present: accept when valid. If invalid, still allow a valid session
    # cookie (avoids stale/wrong Basic blocking the browser cookie).
    b = _parse_basic(request.headers.get("authorization"))
    if b and verify_credentials(b[0], b[1]):
        return True
    cookie = request.cookies.get(AUTH_COOKIE)
    if cookie and verify_session_cookie(cookie):
        return True
    return False


def verify_websocket(websocket: WebSocket) -> bool:
    b = _parse_basic(websocket.headers.get("authorization"))
    if b and verify_credentials(b[0], b[1]):
        return True
    cookie = websocket.cookies.get(AUTH_COOKIE)
    if cookie and verify_session_cookie(cookie):
        return True
    return False


def auth_enabled() -> bool:
    """UI hint: authentication is always required."""
    return True


def is_public_path(path: str) -> bool:
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
    if path in ("/api/auth/status", "/api/auth/login", "/api/auth/logout"):
        return True
    return False


def cookie_secure_flag() -> bool:
    return (os.getenv("LBDL_COOKIE_SECURE") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def change_password(current_password: str, new_password: str) -> tuple[bool, str]:
    auth = load_auth()
    if not verify_credentials(auth["username"], current_password):
        return False, "Current password is incorrect"
    np = (new_password or "").strip()
    if len(np) < 4:
        return False, "New password must be at least 4 characters"
    new_salt = secrets.token_hex(16)
    auth["salt"] = new_salt
    auth["password_hash"] = _hash_password(np, new_salt)
    auth["session_secret"] = secrets.token_hex(32)
    _atomic_write_json(auth_json_path(), auth)
    invalidate_cache()
    return True, ""
