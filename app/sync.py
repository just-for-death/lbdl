#!/usr/bin/env python3
"""
sync.py — Cron-triggered sync.
Reads config/playlists.json, checks each playlist for new tracks,
downloads missing ones into organized Artist/Year - Album/ folders,
and regenerates the M3U file.
"""

import json
import logging
import os
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

CONFIG_DIR = Path(os.getenv("LBDL_CONFIG_DIR", "/app/config"))
LB_TOKEN   = os.getenv("LBDL_LB_TOKEN", "")

PLAYLISTS_FILE = CONFIG_DIR / "playlists.json"
LOG_FILE       = Path("/var/log/lbdl-sync.log")


# ── Logging setup ─────────────────────────────────────────────────────────────
def setup_logging() -> logging.Logger:
    """Configure root logger: stdout (for cron redirect) + dedicated log file."""
    fmt = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # stdout — cron already redirects this to /var/log/lbdl-sync.log
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # Dedicated file handler as a safety net
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(LOG_FILE)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as e:
        root.warning("Could not open log file %s: %s", LOG_FILE, e)

    return logging.getLogger("lbdl.sync")


logger = setup_logging()


def fetch_playlist(playlist_id: str) -> tuple[str, list[dict]]:
    headers = {"Authorization": f"Token {LB_TOKEN}"} if LB_TOKEN else {}
    req = urllib.request.Request(
        f"https://api.listenbrainz.org/1/playlist/{playlist_id}",
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    playlist = data.get("playlist", {})
    return playlist.get("title", "?"), playlist.get("track", [])


def main():
    logger.info("=== lbdl sync started ===")

    if not PLAYLISTS_FILE.exists():
        logger.info("No playlists.json found — nothing to sync")
        return

    playlists = json.load(open(PLAYLISTS_FILE))
    if not playlists:
        logger.info("playlists.json is empty — nothing to sync")
        return

    try:
        from ytmusicapi import YTMusic
        from app.organizer import download_track, already_exists, generate_m3u
        ytm = YTMusic()
    except ImportError as e:
        logger.critical("Missing dependency: %s", e)
        sys.exit(1)

    for pl in playlists:
        url        = pl.get("url", "")
        saved_name = pl.get("name", url)
        match      = re.search(r"/playlist/([a-f0-9-]{36})", url)
        if not match:
            logger.warning("Skipping invalid URL: %s", url)
            continue

        playlist_id = match.group(1)
        logger.info("── Syncing: %s", saved_name)

        try:
            name, tracks = fetch_playlist(playlist_id)
        except Exception as e:
            logger.error("Failed to fetch playlist %s: %s", saved_name, e)
            continue

        logger.info("  %d tracks in playlist", len(tracks))
        downloaded_paths = []
        new_count = 0

        for track in tracks:
            title  = track.get("title", "Unknown")
            artist = track.get("creator", "")
            query  = f"{artist} {title}".strip()

            if already_exists(artist, title):
                logger.debug("  ✓ exists:  %s", query)
                continue

            logger.info("  ↓ new:     %s", query)

            results = ytm.search(query, filter="songs", limit=3)
            if not results:
                logger.warning("    ✗ not found on YouTube Music: %s", query)
                continue

            video_id = results[0]["videoId"]
            ok, final_path, output = download_track(
                video_id, artist, title,
                log_fn=lambda msg: logger.info("    %s", msg),
            )

            if ok and final_path:
                rel = str(final_path.relative_to(Path(os.getenv("LBDL_DATA_DIR", "/app/music"))))
                logger.info("    ✓ %s", rel)
                downloaded_paths.append(final_path)
                new_count += 1
            else:
                logger.error("    ✗ download failed for %s", query)
                for line in (output or "").splitlines()[-5:]:
                    if line.strip():
                        logger.debug("      %s", line.strip())

        # Regenerate M3U with newly downloaded tracks
        if new_count > 0:
            m3u = generate_m3u(name, downloaded_paths)
            logger.info("  ♫ M3U updated: _Playlists/%s", m3u.name)

        logger.info("  Done — %d new tracks", new_count)

    logger.info("=== Sync complete ===")


if __name__ == "__main__":
    main()
