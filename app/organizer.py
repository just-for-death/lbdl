"""
app/organizer.py — Download orchestration and file management.

Handles:
- Downloading audio via yt-dlp
- Checking for existing tracks
- File path conventions
- M3U playlist generation
- Cleanup of stale .part files
"""

import json
import logging
import os
import re
import subprocess
import uuid
from pathlib import Path

from pathvalidate import sanitize_filename
from rapidfuzz import fuzz

logger = logging.getLogger("lbdl.organizer")

OUTPUT_DIR = Path(os.getenv("LBDL_DATA_DIR", "/app/music"))
YTDLP_DIR  = Path(os.getenv("LBDL_YTDLP_DIR", "/app/config"))
CONFIG_DIR = Path(os.getenv("LBDL_CONFIG_DIR", "/app/config"))

# Env-var defaults (used when settings.json is absent)
_DEFAULT_FMT  = os.getenv("LBDL_AUDIO_FORMAT",  "opus")
_DEFAULT_QUAL = os.getenv("LBDL_AUDIO_QUALITY", "0")

AUDIO_EXTENSIONS = {".opus", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".wav"}


def _live_fmt() -> tuple[str, str]:
    """Read audio_format and audio_quality from settings.json if available."""
    settings_path = CONFIG_DIR / "settings.json"
    try:
        if settings_path.exists():
            with open(settings_path) as f:
                s = json.load(f)
            return s.get("audio_format", _DEFAULT_FMT), str(s.get("audio_quality", _DEFAULT_QUAL))
    except Exception:
        pass
    return _DEFAULT_FMT, _DEFAULT_QUAL


# ── Helpers ────────────────────────────────────────────────────────────────────

def _safe(name: str) -> str:
    """Sanitize a string for use as a filename component."""
    return sanitize_filename(name, replacement_text="_").strip() or "Unknown"


def _artist_dir(artist: str) -> Path:
    return OUTPUT_DIR / _safe(artist)


def _expected_path(artist: str, title: str, ext: str | None = None) -> Path:
    fmt = ext or _live_fmt()[0]
    return _artist_dir(artist) / f"{_safe(title)}.{fmt}"


# ── Existence checks ───────────────────────────────────────────────────────────

def already_exists(artist: str, title: str) -> bool:
    """Return True if any audio file matching artist/title exists (exact or fuzzy)."""
    return find_existing_path(artist, title) is not None


# Fuzzy-match thresholds
_TITLE_THRESHOLD  = 88   # % similarity required on title stem
_ARTIST_THRESHOLD = 75   # % similarity required on artist when both are non-empty


def _norm(s: str) -> str:
    """Normalise for comparison: lowercase, strip punctuation noise."""
    s = s.lower().strip()
    # Drop common suffixes that vary between sources
    s = re.sub(r"\s*[\(\[].*?[\)\]]", "", s)          # (Official Video), [Remaster], …
    s = re.sub(r"\s*-\s*(official|lyrics?|audio|video|topic|vevo).*$", "", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def find_existing_path(artist: str, title: str) -> Path | None:
    """Return the Path to an existing track, or None.

    Three-tier search (stops at first hit):
    1. Exact stem match in the expected artist directory  (fast).
    2. Exact stem match anywhere under OUTPUT_DIR         (handles re-tagging).
    3. Fuzzy stem match anywhere under OUTPUT_DIR         (catches YouTube noise,
       slight title variations, artist misspellings).
    """
    safe_stem  = _safe(title).lower()
    norm_title = _norm(title)
    norm_artist = _norm(artist)

    # ── Tier 1: exact match in expected artist dir ─────────────────────────────
    artist_dir = _artist_dir(artist)
    if artist_dir.exists():
        for f in artist_dir.iterdir():
            if f.suffix in AUDIO_EXTENSIONS and f.stem.lower() == safe_stem:
                return f

    # ── Tier 2 & 3: walk all artist subdirs ───────────────────────────────────
    fuzzy_best: tuple[float, Path | None] = (0.0, None)

    if OUTPUT_DIR.exists():
        for subdir in OUTPUT_DIR.iterdir():
            if not subdir.is_dir():
                continue
            if subdir.name.startswith("_") or subdir.name.startswith("."):
                continue
            for f in subdir.iterdir():
                if f.suffix not in AUDIO_EXTENSIONS:
                    continue

                # Tier 2 — exact on sanitised stem
                if f.stem.lower() == safe_stem:
                    if subdir != artist_dir:
                        logger.debug(
                            "find_existing_path: exact match in wrong dir — %r in %s",
                            title, subdir.name,
                        )
                    return f

                # Tier 3 — fuzzy on normalised stem
                norm_stem = _norm(f.stem)
                title_score = fuzz.token_sort_ratio(norm_title, norm_stem)

                # If we have an artist folder name to cross-check, use it to
                # raise confidence (avoids false positives on common short titles)
                if norm_artist and title_score >= _TITLE_THRESHOLD - 10:
                    artist_score = fuzz.token_sort_ratio(norm_artist, _norm(subdir.name))
                    if artist_score >= _ARTIST_THRESHOLD:
                        combined = (title_score * 0.7) + (artist_score * 0.3)
                        if combined > fuzzy_best[0]:
                            fuzzy_best = (combined, f)
                elif title_score >= _TITLE_THRESHOLD:
                    if title_score > fuzzy_best[0]:
                        fuzzy_best = (title_score, f)

    if fuzzy_best[1] is not None:
        logger.info(
            "find_existing_path: fuzzy match (%.0f%%) for %r → %s",
            fuzzy_best[0], title, fuzzy_best[1],
        )
        return fuzzy_best[1]

    return None


# ── Download ───────────────────────────────────────────────────────────────────

def download_track(
    video_id: str,
    artist: str,
    title: str,
    log_fn=None,
) -> tuple[bool, Path | None, str]:
    """
    Download a track from YouTube Music using yt-dlp.

    Returns:
        (success, final_path_or_None, output_or_error_string)
    """
    if log_fn is None:
        log_fn = lambda msg: logger.debug(msg)

    dest_dir = _artist_dir(artist)
    dest_dir.mkdir(parents=True, exist_ok=True)

    audio_fmt, audio_qual = _live_fmt()
    safe_title = _safe(title)
    tmp_stem   = f"{safe_title}_{uuid.uuid4().hex[:6]}"
    tmp_out    = dest_dir / f"{tmp_stem}.%(ext)s"

    cookies_file = YTDLP_DIR / "cookies.txt"
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "-x",
        "--audio-format", audio_fmt,
        "--audio-quality", audio_qual,
        "-o", str(tmp_out),
        "--no-progress",
        "--quiet",
        "--print", "after_move:filepath",
    ]

    if cookies_file.exists():
        cmd += ["--cookies", str(cookies_file)]

    cmd.append(f"https://www.youtube.com/watch?v={video_id}")

    log_fn(f"  yt-dlp {video_id} → {dest_dir.name}/{safe_title}")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return False, None, "yt-dlp timed out after 300 s"
    except Exception as exc:
        return False, None, str(exc)

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        log_fn(f"  yt-dlp error: {err[:400]}")
        return False, None, err

    # yt-dlp prints the final path; use it if available
    printed = proc.stdout.strip().splitlines()
    final_path: Path | None = None
    if printed:
        candidate = Path(printed[-1])
        if candidate.exists():
            final_path = candidate

    # Fallback: look for the file we just created
    if final_path is None:
        for ext in AUDIO_EXTENSIONS:
            p = dest_dir / f"{tmp_stem}{ext}"
            if p.exists():
                final_path = p
                break

    if final_path is None:
        return False, None, "Downloaded file not found on disk"

    # Rename to canonical name
    canonical = dest_dir / f"{safe_title}{final_path.suffix}"
    if final_path != canonical:
        try:
            final_path.rename(canonical)
            final_path = canonical
        except OSError:
            pass  # keep tmp name if rename fails

    log_fn(f"  saved → {final_path.relative_to(OUTPUT_DIR)}")
    return True, final_path, ""


# ── M3U generation ─────────────────────────────────────────────────────────────

def generate_m3u(playlist_name: str, track_paths: list[Path]) -> Path:
    """Write an M3U playlist file and return its path."""
    playlist_dir = OUTPUT_DIR / "_Playlists"
    playlist_dir.mkdir(parents=True, exist_ok=True)

    safe_name = _safe(playlist_name) or "playlist"
    m3u_path  = playlist_dir / f"{safe_name}.m3u"

    with open(m3u_path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for p in track_paths:
            if p and p.exists():
                try:
                    rel = p.relative_to(OUTPUT_DIR)
                    f.write(f"../{rel}\n")
                except ValueError:
                    f.write(f"{p}\n")

    logger.info("M3U written: %s (%d tracks)", m3u_path, len(track_paths))
    return m3u_path


# ── Cleanup ────────────────────────────────────────────────────────────────────

def cleanup_part_files() -> int:
    """Remove stale yt-dlp .part files left by interrupted downloads."""
    removed = 0
    if not OUTPUT_DIR.exists():
        return 0
    for part in OUTPUT_DIR.rglob("*.part"):
        try:
            part.unlink()
            removed += 1
            logger.debug("Removed stale part file: %s", part)
        except OSError:
            pass
    return removed
