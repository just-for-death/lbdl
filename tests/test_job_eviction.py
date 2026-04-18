"""Tests for in-memory job eviction ordering."""

import importlib
import uuid

import pytest


@pytest.fixture
def main_module(tmp_path, monkeypatch):
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
    return main


def test_evict_old_jobs_removes_oldest_finished_first(main_module, monkeypatch):
    """When over capacity, oldest *finished* jobs (by created_at) are dropped."""
    monkeypatch.setattr(main_module, "_MAX_JOBS", 3)

    main_module.jobs.clear()
    main_module.subscribers.clear()

    for ts in [10.0, 20.0, 30.0, 40.0]:
        jid = str(uuid.uuid4())
        main_module.jobs[jid] = main_module.Job(
            id=jid,
            playlist_url="http://example.invalid/p",
            source="listenbrainz",
            status=main_module.JobStatus.DONE,
            created_at=ts,
        )

    assert len(main_module.jobs) == 4

    main_module._evict_old_jobs()

    assert len(main_module.jobs) == 3
    remaining_ts = sorted(j.created_at for j in main_module.jobs.values())
    assert remaining_ts == [20.0, 30.0, 40.0]


def test_evict_old_jobs_no_op_when_under_cap(main_module, monkeypatch):
    monkeypatch.setattr(main_module, "_MAX_JOBS", 50)
    main_module.jobs.clear()

    jid = str(uuid.uuid4())
    main_module.jobs[jid] = main_module.Job(
        id=jid,
        playlist_url="http://example.invalid/p",
        source="listenbrainz",
        status=main_module.JobStatus.DONE,
        created_at=1.0,
    )

    main_module._evict_old_jobs()
    assert jid in main_module.jobs
