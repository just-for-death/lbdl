"""
lbdl — main FastAPI application.

Bugs fixed in this revision:
  • asyncio.get_event_loop() replaced with asyncio.get_running_loop() everywhere
    (get_event_loop is deprecated in 3.10+ and raises DeprecationWarning in threads)
  • asyncio.ensure_future() replaced with asyncio.create_task() (prefer explicit API)
  • WSLogHandler now guards against RuntimeError + uses get_running_loop()
  • Worker task stored and restarted if it ever dies (was silently lost before)
  • delete_playlist: mkdir guard so write never fails on fresh installs
  • save_playlist now updates existing entries (thumbnail refresh on re-download)
  • Structured JSON log output for Dozzle / log aggregators
  • Log level filtering: DEBUG suppressed from WS stream by default
"""

import asyncio
import json
import logging
import os
import re
import time
import urllib.parse
import uuid
from asyncio import Queue
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import requests
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from ytmusicapi import YTMusic
from app.organizer import (
    download_track as dl_track,
    already_exists,
    find_existing_path,
    generate_m3u,
    cleanup_part_files,
)

# ── Config ────────────────────────────────────────────────────────────────────
OUTPUT_DIR         = Path(os.getenv("LBDL_DATA_DIR",          "/app/music"))
CONFIG_DIR         = Path(os.getenv("LBDL_CONFIG_DIR",        "/app/config"))
YTDLP_DIR          = Path(os.getenv("LBDL_YTDLP_DIR",         "/app/config"))
AUDIO_FORMAT       = os.getenv("LBDL_AUDIO_FORMAT",           "opus")
AUDIO_QUALITY      = os.getenv("LBDL_AUDIO_QUALITY",          "0")
LB_TOKEN           = os.getenv("LBDL_LB_TOKEN",               "")
INVIDIOUS_INSTANCE = os.getenv("LBDL_INVIDIOUS_INSTANCE",     "https://inv.nadeko.net")
LOG_LEVEL          = os.getenv("LBDL_LOG_LEVEL",              "INFO").upper()
LOG_FORMAT         = os.getenv("LBDL_LOG_FORMAT",             "json")   # "json" | "text"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
PLAYLISTS_FILE = CONFIG_DIR / "playlists.json"
SETTINGS_FILE  = CONFIG_DIR / "settings.json"
ytm = YTMusic()


# ── Structured logging ────────────────────────────────────────────────────────
# JSON formatter so Dozzle can parse fields and filter/search by level, module, etc.

class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record — Dozzle picks these up automatically."""
    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        return json.dumps({
            "time":    self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level":   record.levelname,
            "logger":  record.name,
            "module":  record.module,
            "message": msg,
        }, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-readable coloured text — useful when tailing directly."""
    COLOURS = {
        "DEBUG":    "\033[90m",
        "INFO":     "\033[36m",
        "WARNING":  "\033[33m",
        "ERROR":    "\033[31m",
        "CRITICAL": "\033[35m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        ts    = self.formatTime(record, "%H:%M:%S")
        col   = self.COLOURS.get(record.levelname, "")
        level = f"{col}{record.levelname:<8}{self.RESET}"
        msg   = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        return f"{ts}  {level}  {record.name:<30}  {msg}"


def _build_formatter() -> logging.Formatter:
    return JsonFormatter() if LOG_FORMAT == "json" else TextFormatter()


def _configure_root_logging():
    """Replace all handlers on the root logger with a single stdout handler."""
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(_build_formatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    # Quiet noisy third-party loggers
    for noisy in ("httpx", "httpcore", "urllib3", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # yt-dlp emits a lot of DEBUG noise; keep it at WARNING unless user wants DEBUG
    if LOG_LEVEL != "DEBUG":
        logging.getLogger("yt_dlp").setLevel(logging.WARNING)


_configure_root_logging()
logger = logging.getLogger("lbdl")


# ── Settings ──────────────────────────────────────────────────────────────────

DEFAULT_SETTINGS = {
    "audio_format":       AUDIO_FORMAT,
    "invidious_instance": INVIDIOUS_INSTANCE,
    "gotify_url":         "",
    "gotify_token":       "",
}


def load_settings() -> dict:
    if not SETTINGS_FILE.exists():
        return dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE) as f:
            saved = json.load(f)
        merged = dict(DEFAULT_SETTINGS)
        merged.update(saved)
        return merged
    except Exception:
        return dict(DEFAULT_SETTINGS)


def save_settings(data: dict) -> dict:
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(merged, f, indent=2)
    return merged


def current_audio_format() -> str:
    return load_settings().get("audio_format", AUDIO_FORMAT)


def current_invidious_instance() -> str:
    return load_settings().get("invidious_instance", INVIDIOUS_INSTANCE)


# ── Gotify ────────────────────────────────────────────────────────────────────

def notify_gotify(title: str, message: str, priority: int = 5):
    cfg   = load_settings()
    url   = cfg.get("gotify_url", "").strip().rstrip("/")
    token = cfg.get("gotify_token", "").strip()
    if not url or not token:
        return
    try:
        resp = requests.post(
            f"{url}/message",
            json={"title": title, "message": message, "priority": priority},
            headers={"X-Gotify-Key": token},
            timeout=8,
        )
        if not resp.ok:
            logger.warning("gotify: HTTP %s — %s", resp.status_code, resp.text[:120])
        else:
            logger.debug("gotify: notification sent — %s", title)
    except Exception as exc:
        logger.warning("gotify: request failed — %s", exc)


# ── Playlist persistence ──────────────────────────────────────────────────────

def load_saved_playlists() -> list[dict]:
    if not PLAYLISTS_FILE.exists():
        return []
    try:
        with open(PLAYLISTS_FILE) as f:
            return json.load(f)
    except Exception:
        logger.warning("playlists.json corrupt or unreadable — returning empty list")
        return []


def save_playlist(url: str, name: str, source: str = "listenbrainz", thumbnail: str = ""):
    playlists = load_saved_playlists()
    existing = next((p for p in playlists if p["url"] == url), None)
    if existing:
        existing["name"] = name
        if thumbnail:
            existing["thumbnail"] = thumbnail
    else:
        entry: dict = {"url": url, "name": name, "source": source}
        if thumbnail:
            entry["thumbnail"] = thumbnail
        playlists.append(entry)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(PLAYLISTS_FILE, "w") as f:
        json.dump(playlists, f, indent=2)


# ── WS log broadcast ──────────────────────────────────────────────────────────
# Sends structured log events (with level + module) to connected browser clients.
# Only INFO and above reach the browser — DEBUG stays in container stdout only.

server_log_history: list[dict] = []
server_log_subscribers: list[WebSocket] = []


class WSLogHandler(logging.Handler):
    """Forwards log records to all connected /ws/server-logs subscribers.

    Bug fix: use asyncio.get_running_loop() instead of get_event_loop(),
    and wrap in try/except RuntimeError for calls from non-async threads.
    """
    def __init__(self):
        super().__init__(level=logging.INFO)   # DEBUG never goes to browser

    def emit(self, record: logging.LogRecord):
        # Skip noisy uvicorn access log lines for WS ping frames
        msg = record.getMessage()
        if "GET / HTTP" in msg or "websocket" in msg.lower() and "connected" not in msg.lower():
            return

        payload = {
            "time":    self.formatTime(record, "%H:%M:%S"),
            "level":   record.levelname,
            "logger":  record.name,
            "message": msg,
        }
        server_log_history.append(payload)
        if len(server_log_history) > 500:
            server_log_history.pop(0)

        try:
            loop = asyncio.get_running_loop()       # BUG FIX: was get_event_loop()
            if loop.is_running():
                asyncio.create_task(_broadcast_server_log(payload))  # BUG FIX: was ensure_future
        except RuntimeError:
            pass  # Called from a non-async thread — silently skip WS broadcast


async def _broadcast_server_log(payload: dict):
    msg  = json.dumps({"type": "server_log", **payload})
    dead = []
    for ws in server_log_subscribers:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        server_log_subscribers.remove(ws)


_ws_handler = WSLogHandler()
_ws_handler.setFormatter(logging.Formatter("%(message)s"))  # message already formatted
for _name in ("uvicorn", "uvicorn.access", "uvicorn.error", "fastapi", "lbdl", ""):
    logging.getLogger(_name).addHandler(_ws_handler)


# ── Models ────────────────────────────────────────────────────────────────────

class JobStatus(str, Enum):
    QUEUED  = "queued"
    RUNNING = "running"
    DONE    = "done"
    ERROR   = "error"


@dataclass
class Track:
    title:      str
    artist:     str
    status:     str = "pending"
    video_id:   str | None = None
    error:      str | None = None
    final_path: str | None = None
    thumbnail:  str | None = None


@dataclass
class Job:
    id:                 str
    playlist_url:       str
    source:             str = "listenbrainz"
    invidious_instance: str = ""
    playlist_name:      str = ""
    playlist_thumbnail: str = ""
    status:             JobStatus = JobStatus.QUEUED
    tracks:             list[Track] = field(default_factory=list)
    logs:               list[str]   = field(default_factory=list)
    started_at:         float = field(default_factory=time.time)
    finished_at:        float | None = None


jobs:        dict[str, Job]             = {}
job_queue:   Queue                      = Queue()
subscribers: dict[str, list[WebSocket]] = {}
_worker_task: asyncio.Task | None       = None   # BUG FIX: track task to restart if dead


# ── URL Detection ─────────────────────────────────────────────────────────────

def detect_source(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host   = parsed.netloc.lower().lstrip("www.")
    if "listenbrainz.org" in host:
        return "listenbrainz"
    if host in ("youtube.com", "youtu.be", "m.youtube.com"):
        return "youtube"
    params = urllib.parse.parse_qs(parsed.query)
    if "list" in params:
        return "invidious"
    return "unknown"


def get_lb_playlist_id(url: str) -> str | None:
    match = re.search(r"/playlist/([a-f0-9-]{36})", url)
    return match.group(1) if match else None


def get_yt_playlist_id(url: str) -> str | None:
    params  = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    list_id = params.get("list", [None])[0]
    if list_id and re.match(r"^[A-Za-z0-9_-]{10,}$", list_id):
        return list_id
    return None


def resolve_invidious_instance(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host   = parsed.netloc.lower()
    if host in ("www.youtube.com", "youtube.com", "youtu.be", "m.youtube.com"):
        return current_invidious_instance().rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}"


# ── Thumbnail helpers ─────────────────────────────────────────────────────────

def _best_thumbnail(thumbnails: list[dict], min_size: int = 200) -> str:
    if not thumbnails:
        return ""
    candidates = [t for t in thumbnails if t.get("width", 0) >= min_size]
    chosen = candidates[0] if candidates else thumbnails[-1]
    return chosen.get("url", "")


def fetch_lb_thumbnail(playlist_id: str, raw_tracks: list[dict]) -> str:
    for track in raw_tracks[:5]:
        identifiers = track.get("identifier", [])
        if isinstance(identifiers, str):
            identifiers = [identifiers]
        for ident in identifiers:
            mbid_match = re.search(r"recording/([0-9a-f-]{36})", str(ident))
            if mbid_match:
                mbid = mbid_match.group(1)
                try:
                    r = requests.get(
                        f"https://coverartarchive.org/recording/{mbid}",
                        timeout=6, allow_redirects=True,
                    )
                    if r.ok:
                        for img in r.json().get("images", []):
                            url = img.get("thumbnails", {}).get("small") or img.get("image", "")
                            if url:
                                logger.debug("lb thumbnail resolved for %s: %s", mbid, url)
                                return url
                except Exception as exc:
                    logger.debug("lb thumbnail fetch failed for %s: %s", mbid, exc)
    return ""


# ── ListenBrainz fetch ────────────────────────────────────────────────────────

def fetch_lb_playlist(playlist_id: str) -> tuple[str, list[dict], str]:
    headers = {"Authorization": f"Token {LB_TOKEN}"} if LB_TOKEN else {}
    resp    = requests.get(
        f"https://api.listenbrainz.org/1/playlist/{playlist_id}",
        headers=headers, timeout=15,
    )
    if resp.status_code == 401:
        raise ValueError("Playlist is private — set LBDL_LB_TOKEN in your environment")
    if resp.status_code == 404:
        raise ValueError(f"Playlist {playlist_id} not found on ListenBrainz")
    resp.raise_for_status()
    data      = resp.json()
    playlist  = data.get("playlist", {})
    name      = playlist.get("title", "Unnamed Playlist")
    tracks    = playlist.get("track", [])
    thumbnail = fetch_lb_thumbnail(playlist_id, tracks)
    return name, tracks, thumbnail


def parse_lb_track(raw: dict) -> Track:
    return Track(
        title=raw.get("title", "Unknown Title"),
        artist=raw.get("creator", ""),
    )


# ── Invidious fetch ───────────────────────────────────────────────────────────

def fetch_invidious_playlist(playlist_id: str, instance: str) -> tuple[str, list[dict], str]:
    base = f"{instance.rstrip('/')}/api/v1/playlists/{playlist_id}"
    resp = requests.get(base, timeout=20)
    if resp.status_code == 404:
        raise ValueError(f"Playlist '{playlist_id}' not found on {instance}")
    resp.raise_for_status()
    data   = resp.json()
    title  = data.get("title", "Unnamed Playlist")
    videos = list(data.get("videos", []))
    total  = data.get("videoCount", len(videos))
    page   = 2

    thumbnail = data.get("playlistThumbnail", "")
    if not thumbnail and videos:
        thumbs    = videos[0].get("videoThumbnails", [])
        thumbnail = _best_thumbnail(thumbs)
    if thumbnail and thumbnail.startswith("/"):
        thumbnail = f"{instance.rstrip('/')}{thumbnail}"

    while len(videos) < total:
        r = requests.get(base, params={"page": page}, timeout=20)
        if not r.ok:
            break
        page_vids = r.json().get("videos", [])
        if not page_vids:
            break
        videos.extend(page_vids)
        page += 1

    logger.info("invidious: fetched %d/%d tracks from '%s'", len(videos), total, title)
    return title, videos, thumbnail


def parse_invidious_track(raw: dict) -> Track:
    thumbs = raw.get("videoThumbnails", [])
    return Track(
        title=raw.get("title", "Unknown Title"),
        artist=raw.get("author", ""),
        video_id=raw.get("videoId"),
        status="found",
        thumbnail=_best_thumbnail(thumbs, min_size=120),
    )


# ── Broadcast helper ──────────────────────────────────────────────────────────

async def broadcast(job_id: str, event: dict):
    msg  = json.dumps(event)
    dead = []
    for ws in subscribers.get(job_id, []):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        subscribers.get(job_id, []).remove(ws)


# ── Job processor ─────────────────────────────────────────────────────────────

async def process_job(job: Job):
    job.status     = JobStatus.RUNNING
    job.started_at = time.time()
    logger.info("job started | id=%s source=%s url=%s", job.id, job.source, job.playlist_url)

    async def log(msg: str):
        job.logs.append(msg)
        await broadcast(job.id, {"type": "log", "msg": msg})

    try:
        if job.source in ("invidious", "youtube"):
            await _process_invidious_job(job, log)
        else:
            await _process_lb_job(job, log)

        done   = sum(1 for t in job.tracks if t.status == "done")
        failed = sum(1 for t in job.tracks if t.status == "failed")

        if done > 0:
            track_paths = [Path(t.final_path) for t in job.tracks if t.final_path]
            m3u = generate_m3u(job.playlist_name, track_paths)
            await log(f"  ♫ M3U written: _Playlists/{m3u.name}")

        job.status      = JobStatus.DONE
        job.finished_at = time.time()
        elapsed         = job.finished_at - job.started_at
        await log(f"Finished — {done} downloaded, {failed} failed ({elapsed:.0f}s)")
        await broadcast(job.id, {
            "type": "job_done", "done": done, "failed": failed,
            "thumbnail": job.playlist_thumbnail, "elapsed": int(elapsed),
        })
        logger.info(
            "job done | id=%s name=%r done=%d failed=%d elapsed=%.0fs",
            job.id, job.playlist_name, done, failed, elapsed,
        )
        notify_gotify(
            title=f"lbdl ✓ {job.playlist_name}",
            message=f"{done} downloaded" + (f", {failed} failed" if failed else "") + f" ({elapsed:.0f}s)",
            priority=5 if not failed else 7,
        )

    except Exception as e:
        import traceback
        job.status      = JobStatus.ERROR
        job.finished_at = time.time()
        tb = traceback.format_exc()
        logger.error("job error | id=%s — %s\n%s", job.id, e, tb)
        for line in tb.splitlines():
            await log(f"  {line}")
        await log(f"Error: {e}")
        await broadcast(job.id, {"type": "error", "msg": str(e)})
        notify_gotify(
            title=f"lbdl ✗ {job.playlist_name or job.playlist_url}",
            message=str(e),
            priority=9,
        )


async def _process_lb_job(job: Job, log):
    playlist_id = get_lb_playlist_id(job.playlist_url)
    if not playlist_id:
        raise ValueError("Invalid ListenBrainz playlist URL")

    await log("Fetching playlist from ListenBrainz…")
    name, raw_tracks, thumbnail = fetch_lb_playlist(playlist_id)
    job.playlist_name      = name
    job.playlist_thumbnail = thumbnail
    job.tracks = [parse_lb_track(t) for t in raw_tracks]
    await broadcast(job.id, {
        "type": "playlist_info", "name": name,
        "total": len(job.tracks), "source": "listenbrainz",
        "thumbnail": thumbnail,
    })
    await log(f"Found {len(job.tracks)} tracks in \"{name}\"")
    save_playlist(job.playlist_url, name, "listenbrainz", thumbnail)

    for i, track in enumerate(job.tracks):
        await broadcast(job.id, {
            "type": "track_start", "index": i,
            "title": track.title, "artist": track.artist,
        })
        query = f"{track.artist} {track.title}".strip()
        await log(f"[{i+1}/{len(job.tracks)}] Searching: {query}")

        # BUG FIX: use get_running_loop() instead of deprecated get_event_loop()
        loop    = asyncio.get_running_loop()
        results = await loop.run_in_executor(
            None, lambda q=query: ytm.search(q, filter="songs", limit=3)
        )

        if not results:
            track.status = "failed"
            track.error  = "No results on YouTube Music"
            await log("  ✗ Not found on YouTube Music")
            await broadcast(job.id, {
                "type": "track_done", "index": i,
                "status": "failed", "error": track.error,
            })
            continue

        track.video_id  = results[0].get("videoId")
        track.status    = "found"
        ytm_thumbs      = results[0].get("thumbnails", [])
        if ytm_thumbs:
            track.thumbnail = ytm_thumbs[-1].get("url", "")
        found_title  = results[0].get("title", "")
        found_artist = (results[0].get("artists") or [{}])[0].get("name", "")
        await log(f"  ✓ Found: {found_title} — {found_artist}")
        await broadcast(job.id, {
            "type": "track_found", "index": i,
            "thumbnail": track.thumbnail or "",
        })
        await _download_track(job, i, track, log)


async def _process_invidious_job(job: Job, log):
    playlist_id = get_yt_playlist_id(job.playlist_url)
    if not playlist_id:
        raise ValueError("Could not find a playlist ID (?list=…) in the URL")

    instance = job.invidious_instance or resolve_invidious_instance(job.playlist_url)
    await log(f"Fetching playlist from {instance}…")
    name, raw_tracks, thumbnail = fetch_invidious_playlist(playlist_id, instance)
    job.playlist_name      = name
    job.playlist_thumbnail = thumbnail
    job.tracks = [parse_invidious_track(t) for t in raw_tracks]
    await broadcast(job.id, {
        "type": "playlist_info", "name": name,
        "total": len(job.tracks), "source": job.source,
        "thumbnail": thumbnail,
    })
    await log(f"Found {len(job.tracks)} tracks in \"{name}\"")
    save_playlist(job.playlist_url, name, job.source, thumbnail)

    for i, track in enumerate(job.tracks):
        await broadcast(job.id, {
            "type": "track_start", "index": i,
            "title": track.title, "artist": track.artist,
            "thumbnail": track.thumbnail or "",
        })
        if not track.video_id:
            track.status = "failed"
            track.error  = "No videoId in response"
            await broadcast(job.id, {
                "type": "track_done", "index": i,
                "status": "failed", "error": track.error,
            })
            continue
        await log(f"[{i+1}/{len(job.tracks)}] {track.artist} — {track.title}")
        await _download_track(job, i, track, log)


async def _download_track(job: Job, i: int, track: Track, log):
    if already_exists(track.artist, track.title):
        track.status = "done"
        existing = find_existing_path(track.artist, track.title)
        if existing:
            track.final_path = str(existing)
        await log("  ✓ Already exists, skipping")
        await broadcast(job.id, {"type": "track_done", "index": i, "status": "done"})
        return

    fmt = current_audio_format()
    await log(f"  ↓ Downloading ({fmt})…")
    await broadcast(job.id, {"type": "track_downloading", "index": i})

    def _run(vid=track.video_id, art=track.artist, ttl=track.title):
        collected: list[str] = []
        result = dl_track(vid, art, ttl, log_fn=collected.append)
        return result, collected

    loop = asyncio.get_running_loop()   # BUG FIX
    (ok, final_path, output), dl_logs = await loop.run_in_executor(None, _run)
    for dl_line in dl_logs:
        await log(dl_line)
    track.final_path = str(final_path) if final_path else None

    if not ok:
        output = output or "(no output)"
        track.status = "failed"
        track.error  = output[:600]
        await log("  ✗ Download failed")
        await broadcast(job.id, {
            "type": "track_done", "index": i,
            "status": "failed", "error": track.error,
        })
    else:
        track.status = "done"
        rel = str(final_path.relative_to(OUTPUT_DIR)) if final_path else ""
        await log(f"  ✓ {rel}")
        await broadcast(job.id, {"type": "track_done", "index": i, "status": "done"})


# ── Worker with auto-restart ──────────────────────────────────────────────────

async def worker():
    logger.info("lbdl worker started")
    while True:
        job = await job_queue.get()
        try:
            await process_job(job)
        except Exception as exc:
            logger.error("Unhandled error in worker for job %s: %s", job.id, exc)
        finally:
            job_queue.task_done()


async def _ensure_worker():
    """Start the worker task; restart it if it has died unexpectedly."""
    global _worker_task
    if _worker_task is None or _worker_task.done():
        if _worker_task and _worker_task.done():
            exc = _worker_task.exception()
            if exc:
                logger.error("Worker task died unexpectedly: %s — restarting", exc)
        _worker_task = asyncio.create_task(worker(), name="lbdl-worker")


@asynccontextmanager
async def lifespan(app: FastAPI):
    removed = cleanup_part_files()
    if removed:
        logger.info("Startup: removed %d stale .part file(s)", removed)
    await _ensure_worker()
    # Periodic worker health check — restarts it if it ever crashes
    async def _watchdog():
        while True:
            await asyncio.sleep(30)
            await _ensure_worker()
    asyncio.create_task(_watchdog(), name="lbdl-watchdog")
    logger.info(
        "lbdl started | format=%s log_format=%s log_level=%s",
        AUDIO_FORMAT, LOG_FORMAT, LOG_LEVEL,
    )
    yield
    logger.info("lbdl shutting down")


app = FastAPI(title="lbdl", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="/app/static"), name="static")


# ── API ───────────────────────────────────────────────────────────────────────

@app.post("/api/jobs")
async def create_job(body: dict):
    url = body.get("playlist_url", "").strip()
    if not url:
        return {"error": "playlist_url required"}

    source = detect_source(url)

    if source == "listenbrainz":
        if not get_lb_playlist_id(url):
            return {"error": "Invalid ListenBrainz URL — expected: listenbrainz.org/playlist/<uuid>"}
    elif source in ("youtube", "invidious"):
        if not get_yt_playlist_id(url):
            return {"error": "No playlist ID found — expected ?list=PLxxxxxx"}
    else:
        return {"error": "Unrecognised URL — paste a ListenBrainz, YouTube, or Invidious playlist URL"}

    job = Job(
        id=str(uuid.uuid4()),
        playlist_url=url,
        source=source,
        invidious_instance=body.get("invidious_instance", "").strip(),
    )
    jobs[job.id] = job
    await job_queue.put(job)
    logger.info("job queued | id=%s source=%s", job.id, source)
    return {"job_id": job.id, "source": source}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return {"error": "not found"}
    return {
        "id":                 job.id,
        "status":             job.status,
        "source":             job.source,
        "playlist_name":      job.playlist_name,
        "playlist_thumbnail": job.playlist_thumbnail,
        "tracks": [
            {
                "title": t.title, "artist": t.artist,
                "status": t.status, "error": t.error,
                "thumbnail": t.thumbnail or "",
            }
            for t in job.tracks
        ],
        "logs": job.logs,
    }


@app.websocket("/ws/{job_id}")
async def job_websocket(websocket: WebSocket, job_id: str):
    await websocket.accept()
    logger.debug("ws connected | job=%s", job_id)
    if job_id not in subscribers:
        subscribers[job_id] = []
    subscribers[job_id].append(websocket)

    job = jobs.get(job_id)
    if job:
        await websocket.send_text(json.dumps({
            "type":               "state",
            "status":             job.status,
            "source":             job.source,
            "playlist_name":      job.playlist_name,
            "playlist_thumbnail": job.playlist_thumbnail,
            "logs":               job.logs,
            "tracks": [
                {
                    "title": t.title, "artist": t.artist,
                    "status": t.status, "thumbnail": t.thumbnail or "",
                }
                for t in job.tracks
            ],
        }))

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        logger.debug("ws disconnected | job=%s", job_id)
        if job_id in subscribers and websocket in subscribers[job_id]:
            subscribers[job_id].remove(websocket)


@app.websocket("/ws/server-logs")
async def server_logs_websocket(websocket: WebSocket):
    await websocket.accept()
    server_log_subscribers.append(websocket)
    # Replay recent history with full structured payload
    for entry in server_log_history[-200:]:
        try:
            await websocket.send_text(json.dumps({"type": "server_log", **entry}))
        except Exception:
            break
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in server_log_subscribers:
            server_log_subscribers.remove(websocket)


@app.get("/api/playlists")
async def list_playlists():
    return load_saved_playlists()


@app.delete("/api/playlists")
async def delete_playlist(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"error": "invalid JSON body"}
    url = body.get("url", "").strip()
    if not url:
        return {"error": "url required"}
    playlists = [p for p in load_saved_playlists() if p["url"] != url]
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)  # BUG FIX: guard for fresh installs
    with open(PLAYLISTS_FILE, "w") as f:
        json.dump(playlists, f, indent=2)
    return {"ok": True}


@app.get("/api/config")
async def get_config():
    cfg = load_settings()
    return {
        "invidious_instance": cfg["invidious_instance"],
        "audio_format":       cfg["audio_format"],
    }


@app.get("/api/settings")
async def get_settings():
    return load_settings()


@app.post("/api/settings")
async def update_settings(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"error": "invalid JSON body"}
    saved = save_settings(body)
    logger.info("settings updated — audio_format=%s invidious=%s gotify=%s",
                saved["audio_format"],
                saved["invidious_instance"],
                "configured" if saved.get("gotify_url") else "off")
    return {"ok": True, "settings": saved}


@app.get("/api/health")
async def health():
    """Simple health endpoint — useful for Dozzle labels / Docker healthcheck."""
    return {
        "status": "ok",
        "jobs":   len(jobs),
        "queued": job_queue.qsize(),
        "worker": "running" if _worker_task and not _worker_task.done() else "dead",
    }


@app.get("/")
async def index():
    return FileResponse("/app/static/index.html")
