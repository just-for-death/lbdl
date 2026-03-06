"""
sync_service.py — Scheduled playlist synchronization.

Downloads new tracks directly (inline) instead of going through RabbitMQ,
which had no feedback path and left tracks stuck as "queued" forever.
"""

import json
import logging
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import requests
from ytmusicapi import YTMusic

from app.organizer import already_exists, download_track as dl_track
from app.library import full_autotag_track, apply_metadata_and_reorganize, read_track_meta

logger = logging.getLogger("lbdl.sync")

CONFIG_DIR         = Path(os.getenv("LBDL_CONFIG_DIR",        "/app/config"))
DATA_DIR           = Path(os.getenv("LBDL_DATA_DIR",          "/app/music"))
LB_TOKEN           = os.getenv("LBDL_LB_TOKEN",               "")
_DEFAULT_INVIDIOUS = os.getenv("LBDL_INVIDIOUS_INSTANCE",     "https://inv.nadeko.net")

PLAYLISTS_FILE = CONFIG_DIR / "playlists.json"
SETTINGS_FILE  = CONFIG_DIR / "settings.json"
PROCESSED_FILE = CONFIG_DIR / "processed.json"


def _read_settings() -> dict:
    try:
        if SETTINGS_FILE.exists():
            with open(SETTINGS_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _get_invidious_instance() -> str:
    return _read_settings().get("invidious_instance", "").strip() or _DEFAULT_INVIDIOUS

def _get_lb_token() -> str:
    return _read_settings().get("lb_token", "").strip() or LB_TOKEN

def _get_acoustid_key() -> str:
    return _read_settings().get("acoustid_key", "").strip()

def _fetch_lyrics_enabled() -> bool:
    return _read_settings().get("fetch_lyrics", True)


# ── Processed-tracks log (flat JSON, replaces Redis) ─────────────────────────

def _load_processed() -> dict:
    try:
        if PROCESSED_FILE.exists():
            with open(PROCESSED_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_processed(data: dict) -> None:
    try:
        with open(PROCESSED_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning("Could not save processed.json: %s", e)


def _is_processed(processed: dict, playlist_id: str, artist: str, title: str) -> bool:
    key = f"{artist}||{title}".lower()
    return key in processed.get(playlist_id, [])


def _mark_processed(processed: dict, playlist_id: str, artist: str, title: str) -> None:
    key = f"{artist}||{title}".lower()
    processed.setdefault(playlist_id, [])
    if key not in processed[playlist_id]:
        processed[playlist_id].append(key)


# ── Playlist fetchers ─────────────────────────────────────────────────────────

def fetch_listenbrainz_playlist(playlist_id: str) -> tuple[str, list]:
    token = _get_lb_token()
    headers = {"Authorization": f"Token {token}"} if token else {}
    req = urllib.request.Request(
        f"https://api.listenbrainz.org/1/playlist/{playlist_id}",
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    pl = data.get("playlist", {})
    return pl.get("title", "?"), pl.get("track", [])


def fetch_invidious_playlist(playlist_id: str, instance: str) -> tuple[str, list]:
    base = f"{instance.rstrip('/')}/api/v1/playlists/{playlist_id}"
    r = requests.get(base, timeout=20)
    r.raise_for_status()
    data   = r.json()
    title  = data.get("title", "Unnamed Playlist")
    videos = list(data.get("videos", []))
    total  = data.get("videoCount", len(videos))
    page   = 2
    while len(videos) < total:
        r2 = requests.get(base, params={"page": page}, timeout=20)
        if not r2.ok:
            break
        pv = r2.json().get("videos", [])
        if not pv:
            break
        videos.extend(pv)
        page += 1
    return title, videos


# ── Download + inline autotag ─────────────────────────────────────────────────

def _download_and_tag(video_id: str, artist: str, title: str) -> tuple[bool, str]:
    logger.info("    ↓ %s — %s", artist, title)
    ok, final_path, output = dl_track(
        video_id, artist, title,
        log_fn=lambda m: logger.debug("      [dl] %s", m),
    )
    if not ok or not final_path:
        return False, output or "download failed"

    logger.info("    ✓ downloaded → %s", final_path)
    try:
        meta         = read_track_meta(final_path) or {"artist": artist, "title": title}
        acoustid_key = _get_acoustid_key()
        do_lyrics    = _fetch_lyrics_enabled()
        new_meta, cover, logs = full_autotag_track(
            final_path, meta, acoustid_key=acoustid_key, fetch_lyrics=do_lyrics
        )
        for line in logs:
            logger.debug("      [tag] %s", line)
        if new_meta:
            new_path = apply_metadata_and_reorganize(final_path, new_meta, cover, DATA_DIR)
            logger.info("    ✓ tagged   → %s", new_path)
            return True, str(new_path)
    except Exception as e:
        logger.warning("    ⚠ autotag failed: %s", e)
    return True, str(final_path)


# ── Per-playlist sync ─────────────────────────────────────────────────────────

def sync_lb_playlist(ytm: YTMusic, processed: dict, playlist_id: str, saved_name: str) -> dict:
    logger.info("── Syncing LB: %s", saved_name)
    try:
        _, tracks = fetch_listenbrainz_playlist(playlist_id)
    except Exception as e:
        logger.error("Fetch failed %s: %s", saved_name, e)
        return {"error": str(e), "name": saved_name, "processed": 0}

    logger.info("  %d tracks", len(tracks))
    new_count = skipped = failed = 0

    for track in tracks:
        title  = track.get("title", "Unknown")
        artist = track.get("creator", "")
        query  = f"{artist} {title}".strip()

        if _is_processed(processed, playlist_id, artist, title) or already_exists(artist, title):
            skipped += 1
            continue

        try:
            results = ytm.search(query, filter="songs", limit=3)
            if not results:
                logger.warning("  ✗ not found: %s", query)
                failed += 1
                continue
            video_id = results[0]["videoId"]
            ok, _ = _download_and_tag(video_id, artist, title)
            if ok:
                _mark_processed(processed, playlist_id, artist, title)
                new_count += 1
            else:
                failed += 1
        except Exception as e:
            logger.error("  ✗ %s: %s", query, e)
            failed += 1

    logger.info("  Done — %d new, %d skipped, %d failed", new_count, skipped, failed)
    return {"name": saved_name, "processed": new_count, "skipped": skipped, "failed": failed}


def sync_invidious_playlist(processed: dict, playlist_id: str, instance: str, saved_name: str) -> dict:
    logger.info("── Syncing YT: %s", saved_name)
    try:
        _, videos = fetch_invidious_playlist(playlist_id, instance)
    except Exception as e:
        logger.error("Fetch failed %s: %s", saved_name, e)
        return {"error": str(e), "name": saved_name, "processed": 0}

    logger.info("  %d videos", len(videos))
    new_count = skipped = failed = 0

    for video in videos:
        video_id = video.get("videoId", "")
        title    = video.get("title", "Unknown")
        artist   = video.get("author", "")
        if not video_id:
            failed += 1
            continue
        if _is_processed(processed, playlist_id, artist, title) or already_exists(artist, title):
            skipped += 1
            continue
        ok, _ = _download_and_tag(video_id, artist, title)
        if ok:
            _mark_processed(processed, playlist_id, artist, title)
            new_count += 1
        else:
            failed += 1

    logger.info("  Done — %d new, %d skipped, %d failed", new_count, skipped, failed)
    return {"name": saved_name, "processed": new_count, "skipped": skipped, "failed": failed}


# ── Entry point ───────────────────────────────────────────────────────────────

def run_sync():
    logger.info("=== Sync started ===")
    if not PLAYLISTS_FILE.exists():
        logger.info("No playlists.json — nothing to sync")
        return

    with open(PLAYLISTS_FILE) as f:
        playlists = json.load(f)
    if not playlists:
        logger.info("playlists.json is empty — nothing to sync")
        return

    processed = _load_processed()
    ytm = None
    results = []

    for pl in playlists:
        url        = pl.get("url", "")
        saved_name = pl.get("name", url)
        source     = pl.get("source", "listenbrainz")
        try:
            if source == "invidious":
                params      = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                playlist_id = params.get("list", [None])[0]
                if not playlist_id:
                    continue
                parsed = urllib.parse.urlparse(url)
                host   = parsed.netloc.lower()
                if host in ("www.youtube.com", "youtube.com", "youtu.be", "m.youtube.com"):
                    instance = _get_invidious_instance()
                else:
                    instance = f"{parsed.scheme}://{host}"
                result = sync_invidious_playlist(processed, playlist_id, instance, saved_name)
            else:
                match = re.search(r"/playlist/([a-f0-9-]{36})", url)
                if not match:
                    continue
                playlist_id = match.group(1)
                if ytm is None:
                    ytm = YTMusic()
                result = sync_lb_playlist(ytm, processed, playlist_id, saved_name)
            results.append(result)
        except Exception as e:
            logger.error("Error syncing %s: %s", saved_name, e, exc_info=True)
            results.append({"name": saved_name, "error": str(e)})

    _save_processed(processed)
    total = sum(r.get("processed", 0) for r in results)
    logger.info("=== Sync done: %d new tracks ===", total)


def setup_logging():
    fmt = logging.Formatter("%(asctime)s [%(levelname)-8s] %(name)s — %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)


if __name__ == "__main__":
    setup_logging()
    run_sync()
