"""
Smoke tests: auth, static resolution, merge path guard.
Run: pytest tests/test_smoke.py  (requires httpx: pip install httpx)
"""

import base64
import importlib
import os
from pathlib import Path

import pytest


@pytest.fixture
def fresh_app(tmp_path, monkeypatch):
    """Fresh app with isolated config/music dirs; reload modules so paths apply."""
    cfg = tmp_path / "config"
    music = tmp_path / "music"
    cfg.mkdir()
    music.mkdir()
    monkeypatch.setenv("LBDL_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("LBDL_DATA_DIR", str(music))

    import app.auth as auth
    import app.main as main

    importlib.reload(auth)
    importlib.reload(main)
    return main.app


def test_health_and_auth_public(fresh_app):
    from fastapi.testclient import TestClient

    c = TestClient(fresh_app)
    assert c.get("/health").status_code == 200
    assert c.get("/").status_code == 200
    assert c.get("/api/auth/status").status_code == 200


def test_api_requires_login(fresh_app):
    from fastapi.testclient import TestClient

    c = TestClient(fresh_app)
    assert c.get("/api/status").status_code == 401


def test_login_cookie_and_basic(fresh_app):
    from fastapi.testclient import TestClient

    c = TestClient(fresh_app)
    r = c.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 200
    assert r.json().get("ok") is True
    assert "lbdl_session" in r.cookies

    r2 = c.get("/api/status")
    assert r2.status_code == 200

    tok = base64.b64encode(b"admin:admin").decode()
    r3 = c.get("/api/status", headers={"Authorization": f"Basic {tok}"})
    assert r3.status_code == 200


def test_change_password(fresh_app):
    from fastapi.testclient import TestClient

    c = TestClient(fresh_app)
    c.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    r = c.post(
        "/api/auth/change-password",
        json={"current_password": "admin", "new_password": "newsecret"},
    )
    assert r.status_code == 200
    assert r.json().get("ok") is True

    assert (
        c.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin"},
        ).status_code
        == 401
    )
    r_ok = c.post(
        "/api/auth/login",
        json={"username": "admin", "password": "newsecret"},
    )
    assert r_ok.status_code == 200


def test_merge_rejects_path_outside_library(fresh_app):
    from fastapi.testclient import TestClient

    c = TestClient(fresh_app)
    c.post("/api/auth/login", json={"username": "admin", "password": "admin"})

    outside = Path("/tmp/lbdl_merge_probe_outside")
    try:
        outside.mkdir(exist_ok=True)
        r = c.post(
            "/api/library/merge-artists",
            json={"target_name": "Safe", "source_folders": [str(outside)]},
        )
        assert r.status_code == 200
        body = r.json()
        assert body.get("errors")
        assert any("outside" in e.lower() or "rejected" in e.lower() for e in body["errors"])
    finally:
        outside.rmdir() if outside.exists() else None


def test_static_dir_points_at_repo(fresh_app):
    import app.main as main

    assert (main.STATIC_DIR / "index.html").is_file()
