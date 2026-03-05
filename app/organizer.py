"""
organizer.py — Shared download logic for lbdl.

Bug fixes in this version:
  • find_existing_path()     — returns actual Path so M3U includes skipped tracks
  • _capture_path hook       — checks d.get("filepath") first (FFmpegExtractAudio
                               sets this key; info_dict.filepath is pre-conversion)
  • already_exists()         — now delegates to find_existing_path()

Output structure:
  music/
    Artist/
      2024 - Album/
        01 - Track.opus
        01 - Track.lrc        ← synced lyrics sidecar
        cover.jpg
    _Playlists/
      Home.m3u                ← separate from artist folders
"""

import logging
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import yt_dlp
from pathvalidate import sanitize_filename

AUDIO_FORMAT  = os.getenv("LBDL_AUDIO_FORMAT",  "opus")
AUDIO_QUALITY = os.getenv("LBDL_AUDIO_QUALITY", "0")
OUTPUT_DIR    = Path(os.getenv("LBDL_DATA_DIR",   "/app/music"))
YTDLP_DIR     = Path(os.getenv("LBDL_YTDLP_DIR",  "/app/config"))

logger = logging.getLogger(__name__)

YT_MUSIC_URL = "https://music.youtube.com/watch?v={video_id}"
MAX_RETRIES  = 3
RETRY_DELAY  = 1.0   # seconds; doubles each attempt (1 → 2 → 4)


# ── Filename helpers ──────────────────────────────────────────────────────────

def safe(s: str) -> str:
    result = sanitize_filename(str(s)).strip()
    return result or "Unknown"


def dedup_artists(raw: str) -> str:
    if not raw:
        return raw
    seen: set[str] = set()
    unique: list[str] = []
    for part in (p.strip() for p in raw.split(",")):
        key = part.lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(part)
    return ", ".join(unique)


# ── Thumbnail upscaling ───────────────────────────────────────────────────────

def upscale_thumbnail_url(url: str, size: int = 544) -> str:
    return re.sub(r"=w\d+-h\d+", f"=w{size}-h{size}", url)


# ── Thread-safe cover art cache ───────────────────────────────────────────────

class _CoverCache:
    def __init__(self) -> None:
        self._cache: dict[str, bytes] = {}
        self._lock  = threading.Lock()

    def fetch(self, url: str | None) -> bytes | None:
        if not url:
            return None
        with self._lock:
            if url in self._cache:
                return self._cache[url]
        data = self._download(upscale_thumbnail_url(url))
        if data:
            with self._lock:
                self._cache[url] = data
        return data

    def _download(self, url: str) -> bytes | None:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "lbdl/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()
            return data
        except Exception as exc:
            logger.warning("Cover fetch failed (%s): %s", url, exc)
            return None


_cover_cache = _CoverCache()


# ── .part file cleanup ────────────────────────────────────────────────────────

def cleanup_part_files() -> int:
    cleaned = 0
    try:
        for part in OUTPUT_DIR.rglob("*.part"):
            try:
                part.unlink(missing_ok=True)
                cleaned += 1
            except OSError:
                pass
    except OSError:
        pass
    if cleaned:
        logger.info("Removed %d stale .part file(s)", cleaned)
    return cleaned


# ── Metadata ──────────────────────────────────────────────────────────────────

def get_metadata(video_id: str) -> dict:
    url = YT_MUSIC_URL.format(video_id=video_id)
    opts: dict = {"quiet": True, "no_warnings": True, "color": "never"}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return dict(info) if info else {}
    except Exception as exc:
        logger.warning("Metadata fetch failed for %s: %s", video_id, exc)
        return {}


def resolve_path(meta: dict, fallback_artist: str, fallback_title: str) -> Path:
    raw_artist = meta.get("artist") or meta.get("uploader") or fallback_artist or "Unknown Artist"
    artist = safe(dedup_artists(raw_artist))
    album  = safe(meta.get("album") or "Unknown Album")
    title  = safe(meta.get("title") or fallback_title or "Unknown Title")

    year = (
        meta.get("release_year")
        or meta.get("release_date", "")[:4]
        or str(meta.get("upload_date", ""))[:4]
        or "Unknown Year"
    )

    track_num = meta.get("track_number") or meta.get("playlist_index")
    filename  = f"{int(track_num):02d} - {title}" if track_num else title

    return OUTPUT_DIR / artist / f"{year} - {album}" / filename


# ── Audio tagging via mediafile ───────────────────────────────────────────────

def _apply_tags(path: Path, meta: dict) -> None:
    try:
        from mediafile import MediaFile
        raw   = meta.get("artist") or meta.get("uploader") or ""
        dedup = dedup_artists(raw)
        parts = [a.strip() for a in dedup.split(",") if a.strip()]
        if not parts:
            return
        joined = " / ".join(parts)

        audio = MediaFile(path)
        audio.artist       = joined
        audio.albumartist  = joined
        audio.artists      = parts
        audio.albumartists = parts
        audio.save()
    except Exception as exc:
        logger.warning("Could not tag %s: %s", path.name, exc)


def _embed_lyrics_tag(lrc: str, path: Path) -> None:
    plain = re.sub(r"\[\d+:\d+\.\d+\]", "", lrc).strip()
    if not plain:
        return
    try:
        from mediafile import MediaFile
        audio = MediaFile(path)
        audio.lyrics = plain
        audio.save()
    except Exception as exc:
        logger.warning("Could not embed lyrics into %s: %s", path.name, exc)


# ── Lyrics ────────────────────────────────────────────────────────────────────

def fetch_lyrics(title: str, artist: str, log_fn=None) -> str | None:
    def _log(msg: str) -> None:
        logger.info(msg)
        if log_fn:
            log_fn(msg)

    try:
        import syncedlyrics
    except ImportError:
        _log("  ♪ LYRICS ERROR: syncedlyrics not installed")
        return None

    primary_artist = artist.split(",")[0].strip() if artist else ""

    queries: list[tuple[str, str]] = []
    if primary_artist:
        queries.append((f"{primary_artist} - {title}", f"primary artist: {primary_artist!r}"))
    queries.append((title, "title only"))

    for query, query_desc in queries:
        _log(f"  ♪ Searching lyrics [{query_desc}]: {query!r}")
        try:
            lrc = syncedlyrics.search(query)
            if lrc:
                is_synced = bool(re.search(r'\[\d+:\d+\.\d+\]', lrc))
                kind = "synced" if is_synced else "plain text"
                _log(f"  ♪ ✓ Lyrics found ({kind})")
                return lrc
            else:
                _log(f"  ♪ No match for query: {query!r}")
        except Exception as exc:
            _log(f"  ♪ Search error: {exc}")

    _log(f"  ♪ No lyrics found for {title!r}")
    return None


def save_lrc(lrc: str, audio_path: Path) -> Path:
    lrc_path = audio_path.with_suffix(".lrc")
    lrc_path.write_text(lrc, encoding="utf-8")
    return lrc_path


# ── Download ──────────────────────────────────────────────────────────────────

def _build_ytdlp_opts(dest_path: Path, folder: Path, temp_cookies: Path | None) -> dict:
    opts: dict = {
        "format": (
            f"bestaudio[ext={AUDIO_FORMAT}]"
            f"/bestaudio[acodec={AUDIO_FORMAT}]"
            "/bestaudio/best"
        ),
        "outtmpl": {
            "default":   str(dest_path) + ".%(ext)s",
            "thumbnail": str(folder / "cover") + ".%(ext)s",
        },
        "postprocessors": [
            {
                "key":             "FFmpegExtractAudio",
                "preferredcodec":  AUDIO_FORMAT,
                "preferredquality": str(AUDIO_QUALITY),
            },
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ],
        "writethumbnail":    True,
        "convertthumbnails": "jpg",
        "quiet":             True,
        "no_warnings":       True,
        "color":             "never",
        "retry_sleep_functions": {
            "http":     lambda n: min(2 ** n, 30),
            "fragment": lambda n: min(2 ** n, 30),
        },
        "remote_components": ["ejs:github"],
    }
    if temp_cookies and temp_cookies.exists():
        opts["cookiefile"] = str(temp_cookies)
    return opts


def _is_retryable(msg: str) -> bool:
    return any(p in msg for p in (
        "HTTP Error 403", "403 Forbidden",
        "HTTP Error 429",
        "HTTP Error 5",
    ))


def download_track(
    video_id: str,
    fallback_artist: str = "",
    fallback_title:  str = "",
    log_fn=None,
) -> tuple[bool, Path | None, str]:
    def _log(msg: str) -> None:
        logger.info(msg)
        if log_fn:
            log_fn(msg)

    url = YT_MUSIC_URL.format(video_id=video_id)
    _log(f"Downloading {video_id} ({fallback_artist} – {fallback_title})")

    meta      = get_metadata(video_id)
    dest_path = resolve_path(meta, fallback_artist, fallback_title)
    folder    = dest_path.parent
    folder.mkdir(parents=True, exist_ok=True)
    _log(f"Target: {dest_path}.{AUDIO_FORMAT}")

    cookies_src  = YTDLP_DIR / "cookies.txt"
    temp_cookies: Path | None = None
    if cookies_src.exists():
        fd, tmp = tempfile.mkstemp(suffix=".txt", prefix="lbdl_cookies_")
        os.close(fd)
        temp_cookies = Path(tmp)
        shutil.copy2(cookies_src, temp_cookies)

    actual_path: Path | None = None
    error_msg   = ""
    success     = False

    def _capture_path(d: dict) -> None:
        """Postprocessor hook — captures final output path after FFmpegExtractAudio.

        BUG FIX: yt-dlp sets d["filepath"] (top-level) on the postprocessor
        finished event for FFmpegExtractAudio, pointing to the converted file.
        The original code only checked d["info_dict"]["filepath"] which may
        still reference the pre-conversion container (e.g. .webm).
        """
        nonlocal actual_path
        if d.get("status") == "finished":
            # Top-level filepath is set by FFmpegExtractAudio to the output file
            fp = d.get("filepath") or d.get("info_dict", {}).get("filepath")
            if fp:
                actual_path = Path(fp)

    opts = _build_ytdlp_opts(dest_path, folder, temp_cookies)
    opts["postprocessor_hooks"] = [_capture_path]

    try:
        for attempt in range(MAX_RETRIES + 1):
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
                success = True
                break

            except Exception as exc:
                error_msg = str(exc)

                if "Video unavailable" in error_msg:
                    logger.error("Video %s unavailable (region-locked?)", video_id)
                    break
                if "Sign in" in error_msg or "cookies" in error_msg.lower():
                    logger.error("Authentication required for %s — provide cookies.txt", video_id)
                    break

                if _is_retryable(error_msg) and attempt < MAX_RETRIES:
                    delay = RETRY_DELAY * (2 ** attempt)
                    logger.warning(
                        "Attempt %d/%d failed, retrying in %.1fs: %s",
                        attempt + 1, MAX_RETRIES + 1, delay, error_msg,
                    )
                    for part in folder.glob(f"{dest_path.name}*.part"):
                        part.unlink(missing_ok=True)
                    time.sleep(delay)
                    continue

                logger.error("Download failed for %s: %s", video_id, error_msg)
                break
    finally:
        if temp_cookies:
            temp_cookies.unlink(missing_ok=True)

    if not success:
        return False, None, error_msg

    final_path = actual_path
    if not final_path or not final_path.exists():
        final_path = Path(f"{dest_path}.{AUDIO_FORMAT}")
    if not final_path.exists():
        for p in folder.glob(f"{dest_path.name}.*"):
            if p.suffix.lstrip(".") in ("opus", "mp3", "flac", "ogg", "m4a", "webm"):
                final_path = p
                break

    if not final_path or not final_path.exists():
        return False, None, "Output file not found after download"

    logger.info("yt-dlp succeeded → %s", final_path.name)

    _apply_tags(final_path, meta)

    track_title  = meta.get("title") or fallback_title
    track_artist = dedup_artists(
        meta.get("artist") or meta.get("uploader") or fallback_artist
    )
    lrc_path = final_path.with_suffix(".lrc")
    if lrc_path.exists():
        logger.debug("Lyrics sidecar already present: %s", lrc_path.name)
    else:
        lrc = fetch_lyrics(track_title, track_artist, log_fn=_log)
        if lrc:
            save_lrc(lrc, final_path)
            _embed_lyrics_tag(lrc, final_path)
            _log(f"  ♪ Lyrics saved: {final_path.with_suffix('.lrc').name}")

    return True, final_path, ""


# ── Existence check ───────────────────────────────────────────────────────────

def find_existing_path(artist: str, title: str) -> Path | None:
    """Return path of an existing audio file for this track, or None.

    BUG FIX: the old already_exists() only returned bool, so tracks skipped
    as duplicates had track.final_path=None and were omitted from the M3U.
    Callers should use this to capture the path and include it in the playlist.
    """
    safe_title = safe(title)
    for ext in ("opus", "mp3", "flac", "ogg", "m4a", "webm"):
        matches = list(OUTPUT_DIR.glob(f"*/*/{safe_title}.{ext}"))
        if matches:
            return matches[0]
        flat = OUTPUT_DIR / f"{safe(artist)} - {safe_title}.{ext}"
        if flat.exists():
            return flat
    return None


def already_exists(artist: str, title: str) -> bool:
    """Return True if any audio file for this track already exists on disk."""
    return find_existing_path(artist, title) is not None


# ── M3U generation ────────────────────────────────────────────────────────────

def _audio_duration(path: Path) -> int:
    try:
        from mediafile import MediaFile
        length = MediaFile(path).length
        return int(length) if length else -1
    except Exception:
        return -1


def generate_m3u(playlist_name: str, track_paths: list[Path]) -> Path:
    playlists_dir = OUTPUT_DIR / "_Playlists"
    playlists_dir.mkdir(parents=True, exist_ok=True)

    m3u_path = playlists_dir / f"{safe(playlist_name)}.m3u"
    lines    = ["#EXTM3U", ""]

    for p in track_paths:
        if p and p.exists():
            rel      = os.path.relpath(p, playlists_dir)
            duration = _audio_duration(p)
            lines.append(f"#EXTINF:{duration},{p.stem}")
            lines.append(rel)

    m3u_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info(
        "M3U written: _Playlists/%s (%d track(s))",
        m3u_path.name,
        len([p for p in track_paths if p and p.exists()]),
    )
    return m3u_path
