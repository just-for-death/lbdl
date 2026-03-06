import asyncio
import json
import logging
logger = logging.getLogger("lbdl.main")
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
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from rapidfuzz import fuzz as _fuzz
from ytmusicapi import YTMusic
from app.organizer import (
    download_track as dl_track,
    already_exists,
    find_existing_path,
    generate_m3u,
    cleanup_part_files,
)

# ── Config (env defaults) ─────────────────────────────────────────────────────
OUTPUT_DIR         = Path(os.getenv("LBDL_DATA_DIR",          "/app/music"))
CONFIG_DIR         = Path(os.getenv("LBDL_CONFIG_DIR",        "/app/config"))
YTDLP_DIR          = Path(os.getenv("LBDL_YTDLP_DIR",         "/app/config"))
_ENV_AUDIO_FORMAT  = os.getenv("LBDL_AUDIO_FORMAT",           "opus")
_ENV_AUDIO_QUALITY = os.getenv("LBDL_AUDIO_QUALITY",          "0")
_ENV_LB_TOKEN      = os.getenv("LBDL_LB_TOKEN",               "")
_ENV_INVIDIOUS     = os.getenv("LBDL_INVIDIOUS_INSTANCE",     "https://inv.nadeko.net")
_ENV_CRON          = os.getenv("LBDL_SCHEDULER_CRON",         "0 */2 * * *")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

PLAYLISTS_FILE = CONFIG_DIR / "playlists.json"
SETTINGS_FILE  = CONFIG_DIR / "settings.json"

_ytm: YTMusic | None = None

def get_ytm() -> YTMusic:
    global _ytm
    if _ytm is None:
        try:
            _ytm = YTMusic()
        except Exception as e:
            logging.getLogger("lbdl").error("YTMusic init failed: %s", e)
            _ytm = YTMusic()   # retry once — may succeed without headers
    return _ytm

# ── Settings persistence ──────────────────────────────────────────────────────

DEFAULT_SETTINGS = {
    "lb_token":          _ENV_LB_TOKEN,
    "invidious_instance": _ENV_INVIDIOUS,
    "audio_format":      _ENV_AUDIO_FORMAT,
    "audio_quality":     _ENV_AUDIO_QUALITY,
    "sync_cron":         _ENV_CRON,
    "gotify_url":        "",
    "gotify_token":      "",
    "gotify_priority":   5,
    "acoustid_key":      os.getenv("LBDL_ACOUSTID_KEY", ""),
    "fetch_lyrics":      True,
    # ── New feature settings ───────────────────────────────────────────────
    # Untagged view: only show files added in the last N days (0 = show all)
    "untagged_new_days": 30,
    # Duplicate cleanup: cron expression + enable toggle
    "dedup_enabled":     False,
    "dedup_cron":        "0 4 * * *",   # daily at 04:00 by default
}

def load_settings() -> dict:
    if not SETTINGS_FILE.exists():
        return dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE) as f:
            stored = json.load(f)
        merged = dict(DEFAULT_SETTINGS)
        merged.update(stored)
        return merged
    except Exception:
        return dict(DEFAULT_SETTINGS)


def save_settings(data: dict) -> dict:
    current = load_settings()
    current.update(data)
    # Strip unknown keys
    clean = {k: current[k] for k in DEFAULT_SETTINGS if k in current}
    with open(SETTINGS_FILE, "w") as f:
        json.dump(clean, f, indent=2)
    return clean


# ── Runtime config helpers ────────────────────────────────────────────────────

def cfg() -> dict:
    return load_settings()


# ── Gotify notifications ──────────────────────────────────────────────────────

def notify_gotify(title: str, message: str, priority: int | None = None):
    s = cfg()
    url   = s.get("gotify_url", "").strip().rstrip("/")
    token = s.get("gotify_token", "").strip()
    if not url or not token:
        return
    prio = priority if priority is not None else int(s.get("gotify_priority", 5))
    try:
        requests.post(
            f"{url}/message",
            json={"title": title, "message": message, "priority": prio},
            headers={"X-Gotify-Key": token},
            timeout=8,
        )
    except Exception as e:
        logging.getLogger("lbdl").warning("Gotify notification failed: %s", e)


# ── Playlist persistence ──────────────────────────────────────────────────────

def load_saved_playlists() -> list[dict]:
    if not PLAYLISTS_FILE.exists():
        return []
    try:
        with open(PLAYLISTS_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def save_playlist(url: str, name: str, source: str = "listenbrainz"):
    playlists = load_saved_playlists()
    if not any(p["url"] == url for p in playlists):
        playlists.append({"url": url, "name": name, "source": source})
        with open(PLAYLISTS_FILE, "w") as f:
            json.dump(playlists, f, indent=2)


# ── Logging broadcast ─────────────────────────────────────────────────────────
server_log_history: list[str] = []
server_log_subscribers: list[WebSocket] = []


class WSLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        msg = self.format(record)
        server_log_history.append(msg)
        if len(server_log_history) > 500:
            server_log_history.pop(0)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(_broadcast_server_log(msg))
        except RuntimeError:
            pass


async def _broadcast_server_log(msg: str):
    dead = []
    for ws in server_log_subscribers:
        try:
            await ws.send_text(json.dumps({"type": "server_log", "msg": msg}))
        except Exception:
            dead.append(ws)
    for ws in dead:
        server_log_subscribers.remove(ws)


_ws_handler = WSLogHandler()
_ws_handler.setFormatter(logging.Formatter("%(levelname)s:\t %(message)s"))
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


@dataclass
class Job:
    id:                 str
    playlist_url:       str
    source:             str = "listenbrainz"
    invidious_instance: str = ""
    playlist_name:      str = ""
    status:             JobStatus = JobStatus.QUEUED
    tracks:             list[Track] = field(default_factory=list)
    logs:               list[str]   = field(default_factory=list)


jobs:        dict[str, Job]             = {}
job_queue:   Queue                      = Queue()
subscribers: dict[str, list[WebSocket]] = {}

# ── Library state ─────────────────────────────────────────────────────────────

library_cache:   list[dict]    = []
library_index:   dict[str, dict] = {}   # track_id → meta dict
lib_scan_status: dict          = {"running": False, "scanned": 0, "total": 0, "done": False}
lib_subscribers: list[WebSocket] = []


# ── URL Detection ─────────────────────────────────────────────────────────────

def detect_source(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower().lstrip("www.")
    if "listenbrainz.org" in host:
        return "listenbrainz"
    params = urllib.parse.parse_qs(parsed.query)
    if "list" in params:
        return "invidious"
    return "unknown"


def get_lb_playlist_id(url: str) -> str | None:
    match = re.search(r"/playlist/([a-f0-9-]{36})", url)
    return match.group(1) if match else None


def get_yt_playlist_id(url: str) -> str | None:
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    list_id = params.get("list", [None])[0]
    if list_id and re.match(r"^[A-Za-z0-9_-]{10,}$", list_id):
        return list_id
    return None


def resolve_invidious_instance(url: str) -> str:
    s = cfg()
    default = s.get("invidious_instance", _ENV_INVIDIOUS).rstrip("/")
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    if host in ("www.youtube.com", "youtube.com", "youtu.be", "m.youtube.com"):
        return default
    return f"{parsed.scheme}://{parsed.netloc}"


# ── ListenBrainz fetch ────────────────────────────────────────────────────────

def fetch_lb_playlist(playlist_id: str) -> tuple[str, list[dict]]:
    token = cfg().get("lb_token", "")
    headers = {"Authorization": f"Token {token}"} if token else {}
    resp = requests.get(
        f"https://api.listenbrainz.org/1/playlist/{playlist_id}",
        headers=headers, timeout=15,
    )
    if resp.status_code == 401:
        raise ValueError("Playlist is private — set a ListenBrainz token in Settings")
    resp.raise_for_status()
    data = resp.json()
    playlist = data.get("playlist", {})
    return playlist.get("title", "Unnamed Playlist"), playlist.get("track", [])


def parse_lb_track(raw: dict) -> Track:
    return Track(
        title=raw.get("title", "Unknown Title"),
        artist=raw.get("creator", ""),
    )


# ── Invidious fetch ───────────────────────────────────────────────────────────

def fetch_invidious_playlist(playlist_id: str, instance: str) -> tuple[str, list[dict]]:
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
    while len(videos) < total:
        r = requests.get(base, params={"page": page}, timeout=20)
        if not r.ok:
            break
        page_vids = r.json().get("videos", [])
        if not page_vids:
            break
        videos.extend(page_vids)
        page += 1
    return title, videos


def parse_invidious_track(raw: dict) -> Track:
    return Track(
        title=raw.get("title", "Unknown Title"),
        artist=raw.get("author", ""),
        video_id=raw.get("videoId"),
        status="found",
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
    job.status = JobStatus.RUNNING

    async def log(msg: str):
        job.logs.append(msg)
        await broadcast(job.id, {"type": "log", "msg": msg})

    try:
        if job.source == "invidious":
            await _process_invidious_job(job, log)
        else:
            await _process_lb_job(job, log)

        done_count   = sum(1 for t in job.tracks if t.status == "done")
        failed_count = sum(1 for t in job.tracks if t.status == "failed")

        # Generate M3U from downloaded paths
        track_paths = [Path(t.final_path) for t in job.tracks if t.final_path]
        if track_paths:
            m3u = generate_m3u(job.playlist_name, track_paths)
            await log(f"  ♫ M3U written: _Playlists/{m3u.name}")

        job.status = JobStatus.DONE
        await log(f"Finished — {done_count} downloaded, {failed_count} failed")
        await broadcast(job.id, {"type": "job_done", "done": done_count, "failed": failed_count})

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, notify_gotify,
            f"lbdl — {job.playlist_name or 'Playlist'} done",
            f"{done_count} downloaded · {failed_count} failed",
        )

    except Exception as e:
        import traceback
        job.status = JobStatus.ERROR
        for line in traceback.format_exc().splitlines():
            await log(f"  {line}")
        await log(f"Error: {e}")
        await broadcast(job.id, {"type": "error", "msg": str(e)})
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, notify_gotify,
            "lbdl — Error",
            f"Job failed: {e}",
        )


async def _process_lb_job(job: Job, log):
    playlist_id = get_lb_playlist_id(job.playlist_url)
    if not playlist_id:
        raise ValueError("Invalid ListenBrainz playlist URL")

    await log("Fetching playlist from ListenBrainz…")
    loop = asyncio.get_running_loop()
    name, raw_tracks = await loop.run_in_executor(None, fetch_lb_playlist, playlist_id)
    job.playlist_name = name
    job.tracks = [parse_lb_track(t) for t in raw_tracks]
    await broadcast(job.id, {"type": "playlist_info", "name": name, "total": len(job.tracks), "source": "listenbrainz"})
    await log(f"Found {len(job.tracks)} tracks in \"{name}\"")
    save_playlist(job.playlist_url, name, "listenbrainz")

    for i, track in enumerate(job.tracks):
        await broadcast(job.id, {"type": "track_start", "index": i, "title": track.title, "artist": track.artist})
        query = f"{track.artist} {track.title}".strip()
        await log(f"[{i+1}/{len(job.tracks)}] Searching: {query}")
        results = await loop.run_in_executor(
            None, lambda q=query: get_ytm().search(q, filter="songs", limit=3)
        )
        if not results:
            track.status = "failed"
            track.error  = "No results on YouTube Music"
            await log("  ✗ Not found on YouTube Music")
            await broadcast(job.id, {"type": "track_done", "index": i, "status": "failed", "error": track.error})
            continue

        track.video_id = results[0].get("videoId")
        track.status   = "found"
        await log(f"  ✓ Found: {results[0].get('title','')} — {(results[0].get('artists') or [{}])[0].get('name','')}")
        await _download_track(job, i, track, log)


async def _process_invidious_job(job: Job, log):
    playlist_id = get_yt_playlist_id(job.playlist_url)
    if not playlist_id:
        raise ValueError("Could not find a playlist ID (?list=…) in the URL")

    instance = job.invidious_instance or resolve_invidious_instance(job.playlist_url)
    await log(f"Fetching playlist from Invidious ({instance})…")
    loop = asyncio.get_running_loop()
    name, raw_tracks = await loop.run_in_executor(
        None, fetch_invidious_playlist, playlist_id, instance
    )
    job.playlist_name = name
    job.tracks = [parse_invidious_track(t) for t in raw_tracks]
    await broadcast(job.id, {"type": "playlist_info", "name": name, "total": len(job.tracks), "source": "invidious"})
    await log(f"Found {len(job.tracks)} tracks in \"{name}\" (video IDs ready — skipping search)")
    save_playlist(job.playlist_url, name, "invidious")

    for i, track in enumerate(job.tracks):
        await broadcast(job.id, {"type": "track_start", "index": i, "title": track.title, "artist": track.artist})
        if not track.video_id:
            track.status = "failed"
            track.error  = "No videoId in Invidious response"
            await broadcast(job.id, {"type": "track_done", "index": i, "status": "failed", "error": track.error})
            continue
        await log(f"[{i+1}/{len(job.tracks)}] {track.artist} — {track.title}")
        await _download_track(job, i, track, log)


def _cache_has_track(artist: str, title: str) -> tuple[bool, dict | None]:
    """
    Check library_cache for a track using fuzzy tag matching.

    The query title comes from a YouTube playlist and is typically noisy
    (pipe-separated channel labels, "with lyrics", etc.).
    _norm_yt_title() strips that noise before comparison so a raw YouTube
    title matches the clean tag stored on disk.

    Returns (found, meta_or_None).
    """
    if not library_cache:
        return False, None

    # Use aggressive YouTube normalisation on the query side;
    # use standard dedup normalisation on the clean cached tags.
    norm_q_title  = _norm_yt_title(title)
    norm_q_artist = _norm_dedup(artist)

    _channel_re = re.compile(
        r"\b(music|records?|official|entertainment|vevo|films?|studios?|channel"
        r"|ghazals?|tips)\b",
        re.I,
    )
    q_artist_is_channel = bool(_channel_re.search(norm_q_artist))

    best_score: float = 0.0
    best_meta:  dict | None = None

    for m in library_cache:
        norm_c_title  = _norm_dedup(m.get("title",  "") or "")
        norm_c_artist = _norm_dedup(
            m.get("artist", "") or m.get("albumartist", "") or ""
        )

        # 1. Exact normalised title match
        if norm_q_title and norm_q_title == norm_c_title:
            logger.debug("_cache_has_track: exact title match for %r → %s", title, m.get("path"))
            return True, m

        # 2. Fuzzy title (lower pre-filter now that the query is clean)
        t_score = _fuzz.token_sort_ratio(norm_q_title, norm_c_title)
        if t_score < 82:
            continue

        # Combine with artist when both sides look like real artists
        if norm_c_artist and norm_q_artist and not q_artist_is_channel:
            a_score  = _fuzz.token_sort_ratio(norm_q_artist, norm_c_artist)
            combined = t_score * 0.65 + a_score * 0.35
        else:
            # Channel/unknown artist — title-only, slightly raised bar
            if t_score < 88:
                continue
            combined = float(t_score)

        if combined > best_score:
            best_score = combined
            best_meta  = m

    if best_meta is not None and best_score >= 82:
        logger.info(
            "_cache_has_track: fuzzy hit (%.0f%%) for %r / %r → %s",
            best_score, artist, title, best_meta.get("path", ""),
        )
        return True, best_meta

    return False, None


async def _download_track(job: Job, i: int, track: Track, log):
    # ── Tier 1: check library cache (tags, fuzzy) ──────────────────────────
    # Must run before the filesystem check because autotagging renames files —
    # the filename no longer matches the raw YouTube/playlist title after tagging.
    cache_hit, cache_meta = _cache_has_track(track.artist, track.title)
    if cache_hit:
        track.status = "done"
        if cache_meta:
            track.final_path = cache_meta.get("path")
        await log("  ✓ Already in library (tag match), skipping")
        await broadcast(job.id, {"type": "track_done", "index": i, "status": "done"})
        return

    # ── Tier 2: filesystem exact/fuzzy check (catches unscanned files) ─────
    if already_exists(track.artist, track.title):
        track.status = "done"
        existing = find_existing_path(track.artist, track.title)
        if existing:
            track.final_path = str(existing)
        await log("  ✓ Already exists, skipping")
        await broadcast(job.id, {"type": "track_done", "index": i, "status": "done"})
        return

    await log(f"  ↓ Downloading…")
    await broadcast(job.id, {"type": "track_downloading", "index": i})

    # Always download inline in the API process so the job status is always
    # updated in real-time. The RabbitMQ queue path has no feedback mechanism
    # back to the in-memory job state, which caused tracks to be stuck as
    # "queued" forever.
    def _run(vid=track.video_id, art=track.artist, ttl=track.title):
        collected: list[str] = []
        result = dl_track(vid, art, ttl, log_fn=collected.append)
        return result, collected

    (ok, final_path, output), dl_logs = await asyncio.get_running_loop().run_in_executor(None, _run)
    for dl_line in dl_logs:
        await log(dl_line)
    track.final_path = str(final_path) if final_path else None

    if not ok:
        output = output or "(no output)"
        track.status = "failed"
        track.error  = output[:600]
        await log("  ✗ Download failed")
        await broadcast(job.id, {"type": "track_done", "index": i, "status": "failed", "error": track.error})
    else:
        track.status = "done"
        rel = str(final_path.relative_to(OUTPUT_DIR)) if final_path else ""
        await log(f"  ✓ {rel}")
        await broadcast(job.id, {"type": "track_done", "index": i, "status": "done"})


# ── Library helpers ───────────────────────────────────────────────────────────

async def lib_broadcast(event: dict):
    msg  = json.dumps(event)
    dead = []
    for ws in lib_subscribers:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        lib_subscribers.remove(ws)


def _run_scan(loop: asyncio.AbstractEventLoop):
    """Blocking scan — called from a thread executor."""
    from app.library import scan_library
    global library_cache, library_index, lib_scan_status

    lib_scan_status.update({"running": True, "done": False, "scanned": 0, "total": 0})

    def progress(scanned: int, total: int):
        lib_scan_status["scanned"] = scanned
        lib_scan_status["total"]   = total
        asyncio.run_coroutine_threadsafe(
            lib_broadcast({"type": "scan_progress", "scanned": scanned, "total": total}),
            loop,
        )

    tracks = scan_library(OUTPUT_DIR, progress_cb=progress)
    library_cache  = tracks
    library_index  = {t["id"]: t for t in tracks}
    lib_scan_status.update({"running": False, "done": True})
    return len(tracks)


async def trigger_scan():
    loop  = asyncio.get_running_loop()
    count = await loop.run_in_executor(None, _run_scan, loop)
    await lib_broadcast({"type": "scan_done", "count": count})


# ── Worker ────────────────────────────────────────────────────────────────────

async def worker():
    while True:
        job = await job_queue.get()
        await process_job(job)
        job_queue.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    removed = cleanup_part_files()
    if removed:
        logging.getLogger(__name__).info("Startup: removed %d stale .part file(s)", removed)
    asyncio.create_task(worker())
    asyncio.create_task(_dedup_scheduler())
    yield


app = FastAPI(title="lbdl", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="/app/static"), name="static")


# ── PWA routes (served at root so service-worker scope covers the whole app) ──

@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    """Serve SW from root so its scope covers '/'."""
    return FileResponse(
        "/app/static/sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@app.get("/manifest.json", include_in_schema=False)
async def manifest():
    return FileResponse("/app/static/manifest.json", media_type="application/manifest+json")


# ── iOS / iPadOS icon probing ─────────────────────────────────────────────────
# WebKit ignores <link rel="apple-touch-icon"> and fetches these paths directly.
# Must be served at root with no redirect.

@app.get("/apple-touch-icon.png", include_in_schema=False)
@app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
async def apple_touch_icon():
    return FileResponse("/app/static/icon-180.png", media_type="image/png",
                        headers={"Cache-Control": "public, max-age=604800"})

@app.get("/apple-touch-icon-180x180.png", include_in_schema=False)
@app.get("/apple-touch-icon-180x180-precomposed.png", include_in_schema=False)
async def apple_touch_icon_180():
    return FileResponse("/app/static/icon-180.png", media_type="image/png",
                        headers={"Cache-Control": "public, max-age=604800"})

@app.get("/apple-touch-icon-167x167.png", include_in_schema=False)
@app.get("/apple-touch-icon-167x167-precomposed.png", include_in_schema=False)
async def apple_touch_icon_167():
    return FileResponse("/app/static/icon-167.png", media_type="image/png",
                        headers={"Cache-Control": "public, max-age=604800"})

@app.get("/apple-touch-icon-152x152.png", include_in_schema=False)
@app.get("/apple-touch-icon-152x152-precomposed.png", include_in_schema=False)
async def apple_touch_icon_152():
    return FileResponse("/app/static/icon-152.png", media_type="image/png",
                        headers={"Cache-Control": "public, max-age=604800"})

@app.get("/apple-touch-icon-120x120.png", include_in_schema=False)
@app.get("/apple-touch-icon-120x120-precomposed.png", include_in_schema=False)
async def apple_touch_icon_120():
    return FileResponse("/app/static/icon-120.png", media_type="image/png",
                        headers={"Cache-Control": "public, max-age=604800"})

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse("/app/static/favicon.ico", media_type="image/x-icon",
                        headers={"Cache-Control": "public, max-age=604800"})


# ── API ───────────────────────────────────────────────────────────────────────

@app.post("/api/jobs")
async def create_job(body: dict):
    url = body.get("playlist_url", "").strip()
    if not url:
        return {"error": "playlist_url required"}

    source = detect_source(url)
    if source == "listenbrainz":
        if not get_lb_playlist_id(url):
            return {"error": "Invalid ListenBrainz playlist URL — expected format: https://listenbrainz.org/playlist/<uuid>"}
    elif source == "invidious":
        if not get_yt_playlist_id(url):
            return {"error": "No playlist ID found in URL — expected ?list=PLxxxxxx"}
    else:
        return {"error": "Unrecognised URL — paste a ListenBrainz or YouTube/Invidious playlist URL"}

    invidious_instance = body.get("invidious_instance", "").strip()
    job = Job(
        id=str(uuid.uuid4()),
        playlist_url=url,
        source=source,
        invidious_instance=invidious_instance,
    )
    jobs[job.id] = job
    await job_queue.put(job)
    return {"job_id": job.id, "source": source}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return {"error": "not found"}
    return {
        "id":            job.id,
        "status":        job.status,
        "source":        job.source,
        "playlist_name": job.playlist_name,
        "tracks": [
            {"title": t.title, "artist": t.artist, "status": t.status, "error": t.error}
            for t in job.tracks
        ],
        "logs": job.logs,
    }


@app.websocket("/ws/library")
async def library_websocket(websocket: WebSocket):
    await websocket.accept()
    lib_subscribers.append(websocket)
    # Send current state immediately
    await websocket.send_text(json.dumps({
        "type":   "scan_status",
        "status": lib_scan_status,
        "count":  len(library_cache),
    }))
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in lib_subscribers:
            lib_subscribers.remove(websocket)


# NOTE: specific WS paths MUST be declared before /ws/{job_id} or FastAPI's
# parameterized route will swallow them.

@app.websocket("/ws/server-logs")
async def server_logs_websocket(websocket: WebSocket):
    await websocket.accept()
    server_log_subscribers.append(websocket)
    for msg in server_log_history[-200:]:
        await websocket.send_text(json.dumps({"type": "server_log", "msg": msg}))
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in server_log_subscribers:
            server_log_subscribers.remove(websocket)


@app.websocket("/ws/{job_id}")
async def job_websocket(websocket: WebSocket, job_id: str):
    await websocket.accept()
    if job_id not in subscribers:
        subscribers[job_id] = []
    subscribers[job_id].append(websocket)

    job = jobs.get(job_id)
    if job:
        await websocket.send_text(json.dumps({
            "type":          "state",
            "status":        job.status,
            "source":        job.source,
            "playlist_name": job.playlist_name,
            "logs":          job.logs,
            "tracks": [
                {"title": t.title, "artist": t.artist, "status": t.status}
                for t in job.tracks
            ],
        }))

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if job_id in subscribers and websocket in subscribers[job_id]:
            subscribers[job_id].remove(websocket)


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
    playlists = [p for p in load_saved_playlists() if p["url"] != url]
    with open(PLAYLISTS_FILE, "w") as f:
        json.dump(playlists, f, indent=2)
    return {"ok": True}


@app.get("/api/config")
async def get_config():
    """Legacy endpoint — returns runtime config for backwards compat."""
    s = cfg()
    return {
        "invidious_instance": s.get("invidious_instance", _ENV_INVIDIOUS),
        "audio_format":       s.get("audio_format", _ENV_AUDIO_FORMAT),
    }


@app.get("/api/settings")
async def get_settings():
    return load_settings()


@app.post("/api/settings")
async def update_settings(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"error": "invalid JSON"}
    updated = save_settings(body)
    return {"ok": True, "settings": updated}


@app.post("/api/settings/test-gotify")
async def test_gotify():
    s = cfg()
    url   = s.get("gotify_url", "").strip().rstrip("/")
    token = s.get("gotify_token", "").strip()
    if not url or not token:
        return {"error": "Gotify URL and token are required"}
    try:
        r = requests.post(
            f"{url}/message",
            json={"title": "lbdl", "message": "Test notification — settings saved correctly.", "priority": 5},
            headers={"X-Gotify-Key": token},
            timeout=8,
        )
        if r.ok:
            return {"ok": True}
        return {"error": f"Gotify returned {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/library/tracks")
async def library_tracks():
    return {
        "tracks": library_cache,
        "scan":   lib_scan_status,
    }


@app.post("/api/library/scan")
async def library_scan():
    if lib_scan_status["running"]:
        return {"error": "Scan already running"}
    asyncio.ensure_future(trigger_scan())
    return {"ok": True}


@app.get("/api/library/cover/{tid}")
async def library_cover(tid: str):
    meta = library_index.get(tid)
    if not meta:
        return Response(status_code=204)
    from app.library import get_cover_bytes
    data = get_cover_bytes(Path(meta["path"]))
    if not data:
        return Response(status_code=204)
    return Response(content=data, media_type="image/jpeg")


@app.post("/api/library/track/{tid}/autotag")
async def library_autotag_one(tid: str):
    meta = library_index.get(tid)
    if not meta:
        return {"error": "Track not found"}
    path = Path(meta["path"])
    if not path.exists():
        return {"error": "File not found on disk"}

    loop = asyncio.get_running_loop()

    def _do():
        from app.library import full_autotag_track, apply_metadata_and_reorganize, read_track_meta
        acoustid_key = cfg().get("acoustid_key", "")
        do_lyrics    = bool(cfg().get("fetch_lyrics", True))
        new_meta, cover, logs = full_autotag_track(path, meta, acoustid_key=acoustid_key, fetch_lyrics=do_lyrics)
        if not new_meta:
            return None, logs
        try:
            new_path = apply_metadata_and_reorganize(path, new_meta, cover, OUTPUT_DIR)
        except Exception as e:
            logs.append(f"  ✗ Write failed: {e}")
            return None, logs
        # Re-read then override with authoritative candidate values
        updated = read_track_meta(new_path) or {}
        for k in ("title", "artist", "albumartist", "album", "year", "track"):
            if new_meta.get(k):
                updated[k] = new_meta[k]
        updated["path"] = str(new_path)
        updated["id"]   = tid
        return updated, logs

    updated, logs = await loop.run_in_executor(None, _do)

    if updated is None:
        return {"ok": False, "error": "No match found", "logs": logs}

    library_index[tid] = updated
    for i, t in enumerate(library_cache):
        if t["id"] == tid:
            library_cache[i] = updated
            break

    await lib_broadcast({"type": "track_updated", "track": updated})
    return {"ok": True, "track": updated, "logs": logs}


@app.get("/api/library/track/{tid}/candidates")
async def library_candidates(tid: str, q: str = ""):
    """Return ranked tag candidates. Optional ?q= overrides the search query."""
    meta = library_index.get(tid)
    if not meta:
        return {"error": "Track not found"}
    path = Path(meta["path"])
    if not path.exists():
        return {"error": "File not found on disk"}

    loop = asyncio.get_running_loop()

    def _do():
        from app.library import fetch_candidates
        acoustid_key = cfg().get("acoustid_key", "")
        search_meta = dict(meta)
        if q.strip():
            # Override title with custom query, blank artist so search is clean
            search_meta["title"]  = q.strip()
            search_meta["artist"] = ""
            logger.info("[candidates] custom query override: %r", q)
        return fetch_candidates(path, search_meta, acoustid_key=acoustid_key)

    candidates = await loop.run_in_executor(None, _do)
    return {"candidates": candidates}


@app.post("/api/library/track/{tid}/apply-candidate")
async def library_apply_candidate(tid: str, request: Request):
    """Apply a user-chosen candidate dict to the track."""
    meta = library_index.get(tid)
    if not meta:
        return {"error": "Track not found"}
    path = Path(meta["path"])
    if not path.exists():
        return {"error": "File not found on disk"}
    try:
        candidate = await request.json()
    except Exception:
        return {"error": "invalid JSON"}

    loop = asyncio.get_running_loop()

    def _do():
        from app.library import apply_candidate, read_track_meta
        do_lyrics = bool(cfg().get("fetch_lyrics", True))
        new_path, cover = apply_candidate(path, candidate, OUTPUT_DIR, fetch_lyrics=do_lyrics)
        # Re-read from disk to get fresh file-level fields (format, duration, folder)
        updated = read_track_meta(new_path) or {}
        # Candidate values are ground-truth — always override whatever was re-read
        for k in ("title", "artist", "albumartist", "album", "year", "track"):
            if candidate.get(k):
                updated[k] = candidate[k]
        updated["path"] = str(new_path)
        updated["id"]   = tid   # Always preserve original id
        return updated

    try:
        updated = await loop.run_in_executor(None, _do)
    except Exception as exc:
        logger.error("[apply-candidate] %s", exc)
        return {"error": f"Failed to apply candidate: {exc}"}

    # Remove old index entry and insert under same tid
    library_index[tid] = updated
    for i, t in enumerate(library_cache):
        if t["id"] == tid:
            library_cache[i] = updated
            break

    await lib_broadcast({"type": "track_updated", "track": updated})
    return {"ok": True, "track": updated}


def _is_untagged(meta: dict) -> bool:
    """
    Return True when a track appears to have never been successfully auto-tagged.

    A track is considered untagged when ANY of:
      • artist  is blank/missing
      • album   is blank/missing
      • title   equals the bare filename stem (raw yt-dlp download name)
      • title   contains YouTube noise patterns ("| Official", " - Topic", etc.)

    This is intentionally broad — it is better to re-tag a track that was already
    partially tagged than to skip one that genuinely needs metadata.
    """
    artist = (meta.get("artist") or "").strip()
    album  = (meta.get("album")  or "").strip()
    title  = (meta.get("title")  or "").strip()
    path   = meta.get("path", "")

    if not artist or not album:
        return True

    # Title equals raw filename stem — yt-dlp sets title to filename when no tags exist
    stem = Path(path).stem if path else ""
    if stem and title.lower() == stem.lower():
        return True

    # YouTube noise patterns common in raw downloads
    yt_noise = (
        " | " in title or
        "- Topic" in artist or
        "VEVO" in artist.upper() or
        title.endswith("(Official Video)") or
        title.endswith("(Official Audio)") or
        title.endswith("(Lyric Video)") or
        title.endswith("(Official Music Video)")
    )
    if yt_noise:
        return True

    return False


def _untagged_tracks() -> list[dict]:
    """
    Return the subset of library_cache that is untagged, optionally limited
    to files added within the last `untagged_new_days` days (0 = all time).
    """
    new_days = int(cfg().get("untagged_new_days", 30) or 0)
    cutoff   = (time.time() - new_days * 86400) if new_days > 0 else 0.0

    result = []
    for m in library_cache:
        if not _is_untagged(m):
            continue
        if cutoff > 0:
            # Use mtime if available, otherwise include (conservative)
            mtime = float(m.get("mtime") or 0)
            if mtime > 0 and mtime < cutoff:
                continue
        result.append(m)
    return result


# ── Duplicate detection & cleanup ────────────────────────────────────────────

_DEDUP_TITLE_THRESHOLD  = 90   # % fuzzy similarity on normalised title
_DEDUP_ARTIST_THRESHOLD = 80   # % fuzzy similarity on normalised artist

_dedup_status: dict = {"running": False, "last_run": None, "deleted": 0, "errors": []}


def _norm_dedup(s: str) -> str:
    """Normalise a clean tag string for duplicate comparison."""
    s = (s or "").lower().strip()
    s = re.sub(r"\s*[\(\[].*?[\)\]]", "", s)   # drop (Official Video) etc
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# Suffixes that YouTube channels append to titles but are never in clean tags.
_YT_PIPE_RE    = re.compile(r"\s*[\|｜]\s*.*$")
_YT_SUFFIX_RE  = re.compile(
    r"\s+with\s+lyrics?\b.*$"
    r"|\s+lyri(c(al)?|cs)\s*(video)?\s*$"
    r"|\s+official\s+(video|audio|music\s+video)?\s*$"
    r"|\s+\d{4}\s*$"
    r"|\s+(hd|4k|full\s+song)\s*$",
    re.I,
)
_YT_NOISE_RE   = re.compile(
    r"\b(topic|vevo|official|records?|music|ghazals?|lyrical|lyrics?|video|audio"
    r"|romantic|sad|old|hits?|song|full|hd|4k|ft\.?|feat\.?)\b",
    re.I,
)


def _norm_yt_title(s: str) -> str:
    """
    Aggressively normalise a raw YouTube/playlist title to its core song name.

    1. Take only the part before the first pipe — channel/label noise follows.
    2. Strip trailing appended phrases (with lyrics, lyrical video, etc).
    3. Drop parenthetical / bracketed suffixes.
    4. Remove YouTube noise words that never appear in a clean song title.
    5. Lowercase and collapse whitespace.
    """
    s = (s or "").strip()
    s = _YT_PIPE_RE.sub("", s).strip()
    s = _YT_SUFFIX_RE.sub("", s).strip()
    s = re.sub(r"\s*[\(\[].*?[\)\]]", "", s)
    s = _YT_NOISE_RE.sub(" ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).lower().strip()


def _find_duplicates(tracks: list[dict]) -> list[list[dict]]:
    """
    Group tracks into duplicate clusters using fuzzy artist+title matching.

    Strategy: O(n²) with early-exit is fine for typical library sizes (<20k tracks).
    Each cluster = list of tracks sorted oldest→newest (keep first, delete rest).
    Returns only clusters with 2+ members.
    """
    assigned: list[bool] = [False] * len(tracks)
    clusters: list[list[dict]] = []

    for i, t in enumerate(tracks):
        if assigned[i]:
            continue
        norm_title_i  = _norm_dedup(t.get("title", ""))
        norm_artist_i = _norm_dedup(t.get("artist", "") or t.get("albumartist", ""))

        cluster = [t]
        assigned[i] = True

        for j in range(i + 1, len(tracks)):
            if assigned[j]:
                continue
            u = tracks[j]
            norm_title_j  = _norm_dedup(u.get("title", ""))
            norm_artist_j = _norm_dedup(u.get("artist", "") or u.get("albumartist", ""))

            title_score = _fuzz.token_sort_ratio(norm_title_i, norm_title_j)
            if title_score < _DEDUP_TITLE_THRESHOLD:
                continue

            # If both have artists, they must also be similar
            if norm_artist_i and norm_artist_j:
                artist_score = _fuzz.token_sort_ratio(norm_artist_i, norm_artist_j)
                if artist_score < _DEDUP_ARTIST_THRESHOLD:
                    continue

            cluster.append(u)
            assigned[j] = True

        if len(cluster) > 1:
            # Sort by mtime ascending — oldest first (the one we keep)
            cluster.sort(key=lambda x: float(x.get("mtime") or x.get("ctime") or 0))
            clusters.append(cluster)

    return clusters


def _run_dedup_blocking() -> dict:
    """
    Synchronous dedup worker — called from a thread executor.
    Deletes all but the oldest copy in each duplicate cluster.
    Returns a summary dict.
    """
    global _dedup_status, library_cache, library_index

    _dedup_status["running"] = True
    deleted_paths: list[str] = []
    errors: list[str] = []

    tracks = list(library_cache)
    clusters = _find_duplicates(tracks)

    logger.info("[dedup] Found %d duplicate cluster(s) across %d tracks", len(clusters), len(tracks))

    kept_ids:    set[str] = set()
    deleted_ids: set[str] = set()

    for cluster in clusters:
        keep = cluster[0]
        kept_ids.add(keep["id"])
        logger.info(
            "[dedup] Keeping oldest: %s — %s  (%s)",
            keep.get("artist", "?"), keep.get("title", "?"), keep.get("path", ""),
        )
        for dup in cluster[1:]:
            path_str = dup.get("path", "")
            try:
                p = Path(path_str)
                if p.exists():
                    p.unlink()
                    deleted_paths.append(path_str)
                    deleted_ids.add(dup["id"])
                    logger.info("[dedup] Deleted newer duplicate: %s", path_str)
                else:
                    logger.warning("[dedup] Already gone: %s", path_str)
                    deleted_ids.add(dup["id"])
            except Exception as e:
                msg = f"Could not delete {path_str}: {e}"
                errors.append(msg)
                logger.error("[dedup] %s", msg)

    # Purge deleted tracks from in-memory library
    if deleted_ids:
        library_cache  = [t for t in library_cache  if t["id"] not in deleted_ids]
        library_index  = {k: v for k, v in library_index.items() if k not in deleted_ids}

    summary = {
        "clusters":  len(clusters),
        "deleted":   len(deleted_paths),
        "kept":      len(kept_ids),
        "errors":    errors,
        "paths":     deleted_paths,
    }
    _dedup_status.update({
        "running":  False,
        "last_run": time.time(),
        "deleted":  len(deleted_paths),
        "errors":   errors,
    })
    return summary


async def _run_dedup():
    """Async wrapper for dedup worker."""
    global _dedup_status
    if _dedup_status["running"]:
        return {"error": "Dedup already running"}
    loop    = asyncio.get_running_loop()
    summary = await loop.run_in_executor(None, _run_dedup_blocking)
    await lib_broadcast({"type": "dedup_done", **summary})
    return summary


# ── Dedup scheduler ───────────────────────────────────────────────────────────

def _parse_cron_to_seconds(cron: str) -> int | None:
    """
    Very lightweight cron-to-next-seconds calculator.
    Only handles simple patterns like "0 4 * * *" (minute hour * * *).
    Returns seconds until the next scheduled fire, or None if pattern unsupported.
    """
    import datetime
    parts = cron.strip().split()
    if len(parts) != 5:
        return None
    minute_s, hour_s, dom_s, month_s, dow_s = parts
    # Only support simple "M H * * *" patterns for now
    if not all(p == "*" for p in (dom_s, month_s, dow_s)):
        return None
    try:
        minute = int(minute_s)
        hour   = int(hour_s)
    except ValueError:
        return None

    now  = datetime.datetime.now()
    next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_run <= now:
        next_run += datetime.timedelta(days=1)
    return int((next_run - now).total_seconds())


async def _dedup_scheduler():
    """Background task: fires dedup based on settings.dedup_cron."""
    while True:
        s       = cfg()
        enabled = bool(s.get("dedup_enabled", False))
        cron    = str(s.get("dedup_cron", "0 4 * * *")).strip()

        if not enabled:
            await asyncio.sleep(300)   # check again in 5 min
            continue

        wait = _parse_cron_to_seconds(cron)
        if wait is None:
            logger.warning("[dedup-scheduler] Unsupported cron pattern %r — retrying in 1 h", cron)
            await asyncio.sleep(3600)
            continue

        logger.info("[dedup-scheduler] Next dedup run in %.0f s (cron: %s)", wait, cron)
        await asyncio.sleep(wait)

        # Re-check enabled in case settings changed while we slept
        if not bool(cfg().get("dedup_enabled", False)):
            continue

        logger.info("[dedup-scheduler] Starting scheduled dedup…")
        try:
            summary = await _run_dedup()
            logger.info("[dedup-scheduler] Done — %s", summary)
        except Exception as e:
            logger.error("[dedup-scheduler] Error: %s", e)


async def _run_autotag_batch(track_list: list[dict]):
    """
    Shared autotag worker used by both autotag-all and autotag-untagged.
    Broadcasts autotag_start / autotag_progress / autotag_log / autotag_done events.
    """
    from app.library import full_autotag_track, apply_metadata_and_reorganize, read_track_meta
    loop         = asyncio.get_running_loop()
    acoustid_key = cfg().get("acoustid_key", "")
    do_lyrics    = bool(cfg().get("fetch_lyrics", True))
    total        = len(track_list)
    done = failed = skipped = 0

    await lib_broadcast({"type": "autotag_start", "total": total})

    for i, meta in enumerate(track_list):
        tid  = meta["id"]
        path = Path(meta["path"])

        await lib_broadcast({
            "type": "autotag_progress",
            "index": i, "total": total, "tid": tid,
            "title": meta.get("title", ""), "artist": meta.get("artist", ""),
        })

        if not path.exists():
            await lib_broadcast({"type": "autotag_log", "tid": tid,
                "logs": [f"  ✗ File missing: {path}"]})
            skipped += 1
            continue

        def _do(p=path, m=meta, ak=acoustid_key, fl=do_lyrics):
            new_meta, cover, logs = full_autotag_track(p, m, acoustid_key=ak, fetch_lyrics=fl)
            if not new_meta:
                return None, logs
            try:
                new_path = apply_metadata_and_reorganize(p, new_meta, cover, OUTPUT_DIR)
            except Exception as e:
                logs.append(f"  ✗ Write failed: {e}")
                return None, logs
            updated = read_track_meta(new_path) or {}
            for k in ("title", "artist", "albumartist", "album", "year", "track"):
                if new_meta.get(k):
                    updated[k] = new_meta[k]
            updated["path"] = str(new_path)
            updated["id"]   = m["id"]
            return updated, logs

        updated, logs = await loop.run_in_executor(None, _do)

        await lib_broadcast({"type": "autotag_log", "tid": tid, "logs": logs})

        if updated:
            library_index[tid] = updated
            for j, t in enumerate(library_cache):
                if t["id"] == tid:
                    library_cache[j] = updated
                    break
            await lib_broadcast({"type": "track_updated", "track": updated})
            done += 1
        else:
            failed += 1

    await lib_broadcast({
        "type": "autotag_done",
        "count": total, "done": done, "failed": failed, "skipped": skipped,
    })


@app.get("/api/library/untagged-count")
async def library_untagged_count():
    """Return how many tracks are untagged, respecting the untagged_new_days setting."""
    untagged  = _untagged_tracks()
    new_days  = int(cfg().get("untagged_new_days", 30) or 0)
    return {
        "total":          len(library_cache),
        "untagged":       len(untagged),
        "untagged_new_days": new_days,
    }


@app.get("/api/library/untagged")
async def library_untagged_list():
    """Return the list of untagged tracks (filtered by untagged_new_days)."""
    return {"tracks": _untagged_tracks()}


@app.post("/api/library/dedup")
async def library_dedup():
    """
    Scan the library for duplicates (fuzzy artist+title match) and delete
    all but the oldest copy of each duplicate group.
    """
    if _dedup_status["running"]:
        return {"error": "Dedup already running"}
    if lib_scan_status["running"]:
        return {"error": "Library scan running — wait for it to finish first"}
    asyncio.ensure_future(_run_dedup())
    return {"ok": True, "message": "Dedup started"}


@app.get("/api/library/dedup/status")
async def library_dedup_status():
    """Return the current / last dedup run status."""
    return _dedup_status


@app.get("/api/library/dedup/preview")
async def library_dedup_preview():
    """
    Dry-run duplicate detection — returns clusters without deleting anything.
    Useful for reviewing what would be removed.
    """
    loop     = asyncio.get_running_loop()
    tracks   = list(library_cache)
    clusters = await loop.run_in_executor(None, _find_duplicates, tracks)
    return {
        "clusters": [
            {
                "keep":   {"id": c[0]["id"], "artist": c[0].get("artist"), "title": c[0].get("title"), "path": c[0].get("path"), "mtime": c[0].get("mtime")},
                "delete": [{"id": t["id"], "artist": t.get("artist"), "title": t.get("title"), "path": t.get("path"), "mtime": t.get("mtime")} for t in c[1:]],
            }
            for c in clusters
        ],
        "total_to_delete": sum(len(c) - 1 for c in clusters),
    }


@app.post("/api/library/autotag-all")
async def library_autotag_all():
    """Auto-tag every track in the library."""
    if lib_scan_status["running"]:
        return {"error": "Scan running, wait for it to finish"}
    track_list = list(library_cache)
    asyncio.ensure_future(_run_autotag_batch(track_list))
    return {"ok": True, "total": len(track_list)}


@app.post("/api/library/autotag-untagged")
async def library_autotag_untagged():
    """
    Auto-tag only tracks that have never been successfully tagged.
    Respects the untagged_new_days setting — only new tracks are included
    unless untagged_new_days is 0 (meaning all time).
    """
    if lib_scan_status["running"]:
        return {"error": "Scan running, wait for it to finish"}
    untagged = _untagged_tracks()
    if not untagged:
        return {"ok": True, "total": 0, "message": "No untagged tracks found"}
    asyncio.ensure_future(_run_autotag_batch(untagged))
    return {"ok": True, "total": len(untagged)}


@app.post("/api/library/track/{tid}/fetch-lyrics")
async def library_fetch_lyrics(tid: str):
    """
    Fetch and embed lyrics for a single track.
    Tries synced LRC lyrics first (via LRCLIB), falls back to plain lyrics.
    Saves a .lrc sidecar file when synced lyrics are found.
    Returns { ok, status: 'synced'|'plain'|'none', message }.
    """
    meta = library_index.get(tid)
    if not meta:
        return {"error": "Track not found"}
    path = Path(meta["path"])
    if not path.exists():
        return {"error": "File not found on disk"}

    loop = asyncio.get_running_loop()

    def _do():
        from app.library import fetch_lyrics_for_track, embed_lyrics, lyrics_status
        artist   = meta.get("artist", "") or ""
        title    = meta.get("title",  "") or ""
        album    = meta.get("album",  "") or ""
        duration = int(meta.get("duration") or 0)

        lyr, synced = fetch_lyrics_for_track(artist, title, duration, album)
        if not lyr:
            return {"ok": False, "status": "none", "message": "No lyrics found"}

        ok = embed_lyrics(path, lyr, synced)
        kind = "synced" if synced else "plain"
        status = lyrics_status(path)
        return {
            "ok":      ok,
            "status":  status,
            "message": f"{'Synced LRC' if synced else 'Plain'} lyrics embedded",
        }

    result = await loop.run_in_executor(None, _do)

    # Update in-memory cache with new lyrics_status
    if result.get("ok"):
        meta["lyrics_status"] = result["status"]
        library_index[tid] = meta
        for i, t in enumerate(library_cache):
            if t["id"] == tid:
                library_cache[i] = meta
                break
        await lib_broadcast({"type": "track_updated", "track": meta})

    return result


@app.get("/api/library/track/{tid}/cover-candidates")
async def library_cover_candidates(tid: str, q: str = ""):
    """Return a list of cover art candidates. Optional ?q= overrides artist+title search."""
    meta = library_index.get(tid)
    if not meta:
        return {"error": "Track not found"}

    loop = asyncio.get_running_loop()

    def _do():
        from app.library import (
            _mb_text_search, _parse_mb_rec, _cover_url_from_caa,
            _itunes_candidates, _jiosaavn_candidates, _gaana_candidates,
            get_cover_bytes,
        )
        import re as _re

        # If custom query provided, use it as both artist and title search
        if q.strip():
            artist = ""
            title  = q.strip()
            album  = ""
            logger.info("[cover-candidates] custom query=%r", q)
        else:
            artist = meta.get("artist", "") or ""
            title  = meta.get("title", "") or ""
            album  = meta.get("album", "") or ""
            # Strip pipe-separated YouTube junk from title for search
            if " | " in title:
                title = title.split(" | ")[0].strip()
            logger.info("[cover-candidates] auto: artist=%r title=%r album=%r", artist, title, album)

        covers: list[dict] = []

        # 1. Existing embedded cover
        path = Path(meta["path"])
        existing = get_cover_bytes(path)
        if existing:
            covers.append({
                "source":      "embedded",
                "label":       "Current (already embedded)",
                "url":         f"/api/library/cover/{tid}",
                "is_embedded": True,
                "tid":         tid,
            })

        # 2. MusicBrainz CAA
        mb_recs = _mb_text_search(artist, title or album, limit=6)
        for rec in mb_recs:
            parsed = _parse_mb_rec(rec, source="musicbrainz")
            if parsed and parsed.get("mb_rel_id"):
                covers.append({
                    "source":    "musicbrainz",
                    "label":     f"{parsed.get('artist') or ''} — {parsed.get('album') or ''} ({parsed.get('year') or ''})",
                    "url":       _cover_url_from_caa(parsed["mb_rel_id"]).replace("front-250", "front-500"),
                    "mb_rel_id": parsed["mb_rel_id"],
                })

        # 3. iTunes
        itunes = _itunes_candidates(artist, title or album, limit=6)
        for r in itunes:
            if r.get("cover_url"):
                full = _re.sub(r"250x250bb", "600x600bb", r["cover_url"])
                covers.append({
                    "source": "itunes",
                    "label":  f"{r.get('artist') or ''} — {r.get('album') or ''} ({r.get('year') or ''})",
                    "url":    full,
                })

        # 4. JioSaavn
        jsaavn = _jiosaavn_candidates(artist, title or album, limit=6)
        for r in jsaavn:
            if r.get("cover_url"):
                covers.append({
                    "source": "jiosaavn",
                    "label":  f"{r.get('artist') or ''} — {r.get('album') or ''} ({r.get('year') or ''})",
                    "url":    r["cover_url"],
                })

        # 5. Gaana
        gaana = _gaana_candidates(artist, title or album, limit=4)
        for r in gaana:
            if r.get("cover_url"):
                covers.append({
                    "source": "gaana",
                    "label":  f"{r.get('artist') or ''} — {r.get('album') or ''} ({r.get('year') or ''})",
                    "url":    r["cover_url"],
                })

        # Deduplicate by URL
        seen: set[str] = set()
        deduped = []
        for c in covers:
            u = c.get("url", "")
            if u and u not in seen:
                seen.add(u)
                deduped.append(c)

        logger.info("[cover-candidates] found %d unique covers", len(deduped))
        return deduped

    covers = await loop.run_in_executor(None, _do)
    return {"covers": covers}


@app.post("/api/library/track/{tid}/apply-cover")
async def library_apply_cover(tid: str, request: Request):
    """Apply a user-chosen cover art URL to the track."""
    meta = library_index.get(tid)
    if not meta:
        return {"error": "Track not found"}
    path = Path(meta["path"])
    if not path.exists():
        return {"error": "File not found on disk"}

    try:
        body = await request.json()
    except Exception:
        return {"error": "invalid JSON body"}

    cover_url  = body.get("url", "")
    mb_rel_id  = body.get("mb_rel_id", "")
    is_embedded = body.get("is_embedded", False)

    if not cover_url and not mb_rel_id and not is_embedded:
        return {"error": "url, mb_rel_id, or is_embedded required"}

    loop = asyncio.get_running_loop()

    def _do():
        from app.library import _get, _cover_from_caa, _write_metadata, get_cover_bytes
        cover: bytes | None = None

        # Case 1: re-embed the existing cover (no-op fetch needed, just re-save)
        if is_embedded:
            cover = get_cover_bytes(path)
            if not cover:
                return None, "No embedded cover found in file"
            logger.info("[apply-cover] re-embedding existing cover (%d bytes)", len(cover))

        # Case 2: MusicBrainz CAA
        elif mb_rel_id:
            cover = _cover_from_caa(mb_rel_id)
            if not cover:
                return None, f"Could not fetch cover from MusicBrainz CAA (release {mb_rel_id})"

        # Case 3: external URL — must be absolute http/https
        elif cover_url:
            if not cover_url.startswith("http"):
                return None, f"Invalid cover URL (must be absolute): {cover_url[:80]}"
            cover = _get(cover_url, timeout=20, label="apply-cover")
            if not cover:
                return None, f"Could not download cover from: {cover_url[:80]}"

        if not cover or len(cover) < 1000:
            return None, "Cover image too small or empty"

        logger.info("[apply-cover] writing %d byte cover into %s", len(cover), path.name)
        try:
            _write_metadata(path, {}, cover)
        except Exception as exc:
            return None, f"Failed to write cover to file: {exc}"

        try:
            (path.parent / "cover.jpg").write_bytes(cover)
        except Exception:
            pass

        return len(cover), None

    result = await loop.run_in_executor(None, _do)
    nbytes, err = result
    if err:
        logger.error("[apply-cover] %s", err)
        return {"error": err}

    await lib_broadcast({"type": "track_updated", "track": meta})
    return {"ok": True, "bytes": nbytes}


@app.put("/api/library/track/{tid}")
async def library_update_track(tid: str, request: Request):
    meta = library_index.get(tid)
    if not meta:
        return {"error": "Track not found"}
    path = Path(meta["path"])
    if not path.exists():
        return {"error": "File not found on disk"}

    try:
        body = await request.json()
    except Exception:
        return {"error": "invalid JSON"}

    loop = asyncio.get_running_loop()

    def _do():
        from app.library import apply_metadata_and_reorganize, get_cover_bytes
        # Merge body into existing meta
        new_meta = {**meta, **{k: v for k, v in body.items() if k in
            ("title", "artist", "album", "albumartist", "year", "track")}}
        cover = get_cover_bytes(path) if body.get("fetch_cover") else None
        new_path = apply_metadata_and_reorganize(path, new_meta, cover, OUTPUT_DIR)
        new_meta["path"] = str(new_path)
        new_meta["id"]   = tid
        return new_meta

    try:
        updated = await loop.run_in_executor(None, _do)
    except Exception as exc:
        logger.error("[update-track] %s", exc)
        return {"error": f"Failed to save: {exc}"}
    library_index[tid] = updated
    for i, t in enumerate(library_cache):
        if t["id"] == tid:
            library_cache[i] = updated
            break

    await lib_broadcast({"type": "track_updated", "track": updated})
    return {"ok": True, "track": updated}


# ── Duplicate detection ───────────────────────────────────────────────────────

def _lev_ratio(a: str, b: str) -> float:
    """Levenshtein similarity ratio. Returns 0.0–1.0 without external deps."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    la, lb = len(a), len(b)
    if la > 200 or lb > 200:
        return 1.0 if a == b else 0.0
    prev = list(range(lb + 1))
    for i, ca in enumerate(a):
        curr = [i + 1] + [0] * lb
        for j, cb in enumerate(b):
            curr[j + 1] = min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (0 if ca == cb else 1))
        prev = curr
    return 1.0 - prev[lb] / max(la, lb)


@app.get("/api/library/duplicates")
async def library_duplicates():
    """
    Analyse the library for duplicate tracks.

    Two tracks are considered duplicates when they share a normalised title
    AND their durations are within 10 seconds of each other.  We also flag
    pairs that are within 85 % Levenshtein similarity on title when the
    duration difference is ≤ 5 s (catches re-tagged vs. original names).

    Returns a list of groups, each group being a list of track dicts with
    an extra ``dup_reason`` key explaining why they matched.
    """
    if not library_cache:
        return {"groups": []}

    def _norm(s: str) -> str:
        import re as _re, unicodedata as _ud
        s = _ud.normalize("NFC", s.lower())
        s = _re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", s)   # strip (feat. …) [live] …
        s = _re.sub(r"[^\w\s]", "", s)
        return " ".join(s.split())

    tracks = list(library_cache)
    groups: list[list[dict]] = []
    used: set[str] = set()

    for i, a in enumerate(tracks):
        if a["id"] in used:
            continue
        norm_a = _norm(a.get("title", ""))
        dur_a  = int(a.get("duration", 0) or 0)
        group  = []

        for j, b in enumerate(tracks):
            if i == j or b["id"] in used:
                continue
            norm_b = _norm(b.get("title", ""))
            dur_b  = int(b.get("duration", 0) or 0)
            dur_diff = abs(dur_a - dur_b)

            reason = None
            if norm_a == norm_b and dur_diff <= 10:
                reason = "exact title match"
            elif norm_a and norm_b and dur_diff <= 5:
                ratio = _lev_ratio(norm_a, norm_b)
                if ratio >= 0.85:
                    reason = f"similar title ({int(ratio*100)}%)"

            if reason:
                if not group:
                    group.append({**a, "dup_reason": "original"})
                    used.add(a["id"])
                group.append({**b, "dup_reason": reason})
                used.add(b["id"])

        if len(group) > 1:
            groups.append(group)

    return {"groups": groups}


@app.delete("/api/library/track/{tid}")
async def library_delete_track(tid: str):
    """Delete a track file from disk and remove it from the library cache."""
    meta = library_index.get(tid)
    if not meta:
        return {"error": "Track not found"}
    path = Path(meta["path"])
    try:
        if path.exists():
            path.unlink()
        # Remove empty artist directory if it's now empty
        try:
            if path.parent.exists() and not any(path.parent.iterdir()):
                path.parent.rmdir()
        except OSError:
            pass
    except Exception as exc:
        return {"error": f"Could not delete file: {exc}"}

    # Remove from in-memory caches
    library_index.pop(tid, None)
    for i, t in enumerate(library_cache):
        if t["id"] == tid:
            library_cache.pop(i)
            break

    await lib_broadcast({"type": "track_deleted", "tid": tid})
    return {"ok": True}


# ── Artist merge ──────────────────────────────────────────────────────────────

@app.get("/api/library/artist-groups")
async def library_artist_groups():
    """
    Detect artist folders that look like the same artist under different names.

    Uses normalised Levenshtein similarity (≥ 82 %) on artist name,
    or exact match after stripping common suffixes ("The ", "DJ ", etc.).
    Returns a list of groups: each group contains a list of artist dicts
    {name, folder, track_count, track_ids}.
    """
    if not library_cache:
        return {"groups": []}

    import re as _re, unicodedata as _ud

    def _norm_artist(s: str) -> str:
        s = _ud.normalize("NFC", s.lower())
        s = _re.sub(r"^(the |dj |mc |dj\.)\s*", "", s)
        s = _re.sub(r"[^\w\s]", "", s)
        return " ".join(s.split())

    # Build per-artist-folder index
    artist_map: dict[str, dict] = {}
    for t in library_cache:
        folder  = t.get("folder", "")
        artist  = t.get("artist") or t.get("albumartist") or Path(folder).name
        key     = folder
        if key not in artist_map:
            artist_map[key] = {"name": artist, "folder": folder, "track_ids": [], "track_count": 0}
        artist_map[key]["track_ids"].append(t["id"])
        artist_map[key]["track_count"] += 1

    artists = list(artist_map.values())
    groups: list[list[dict]] = []
    used: set[str] = set()

    for i, a in enumerate(artists):
        if a["folder"] in used:
            continue
        norm_a = _norm_artist(a["name"])
        group  = [a]
        used.add(a["folder"])

        for j, b in enumerate(artists):
            if i == j or b["folder"] in used:
                continue
            norm_b = _norm_artist(b["name"])
            if norm_a == norm_b:
                group.append(b)
                used.add(b["folder"])
            elif norm_a and norm_b and _lev_ratio(norm_a, norm_b) >= 0.82:
                group.append(b)
                used.add(b["folder"])

        if len(group) > 1:
            groups.append(group)

    return {"groups": groups}


@app.post("/api/library/merge-artists")
async def library_merge_artists(request: Request):
    """
    Merge one or more source artist folders into a target artist name/folder.

    Body: { target_name: str, source_folders: [str, ...] }

    Moves all audio files from each source folder into OUTPUT_DIR/<target_name>/,
    updates the artist tag in every moved file, and removes the now-empty
    source folders.
    """
    try:
        body = await request.json()
    except Exception:
        return {"error": "invalid JSON"}

    target_name    = (body.get("target_name") or "").strip()
    source_folders = body.get("source_folders") or []

    if not target_name:
        return {"error": "target_name required"}
    if not source_folders:
        return {"error": "source_folders required"}

    import shutil as _shutil
    from app.library import _write_metadata, _safe as _lib_safe

    safe_target = _lib_safe(target_name)
    target_dir  = OUTPUT_DIR / safe_target
    target_dir.mkdir(parents=True, exist_ok=True)

    moved: list[str] = []
    errors: list[str] = []
    AUDIO_EXT = {".opus", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".wav"}

    loop = asyncio.get_running_loop()

    def _do_merge():
        _moved_inner: list[str] = []
        _errs_inner:  list[str] = []
        for src_folder in source_folders:
            src_path = Path(src_folder)
            # Skip if source IS the target (same resolved path) — avoids self-move
            try:
                if src_path.resolve() == target_dir.resolve():
                    continue
            except Exception:
                pass
            if not src_path.exists():
                _errs_inner.append(f"Folder not found: {src_folder}")
                continue
            for f in list(src_path.iterdir()):
                if f.suffix.lower() not in AUDIO_EXT:
                    continue
                dest = target_dir / f.name
                # Avoid collisions
                if dest.exists() and dest != f:
                    stem, suf = f.stem, f.suffix
                    dest = target_dir / f"{stem}_merged{suf}"
                try:
                    if f != dest:
                        _shutil.move(str(f), str(dest))
                    # Update artist tag
                    try:
                        _write_metadata(dest, {"artist": target_name, "albumartist": target_name})
                    except Exception:
                        pass
                    _moved_inner.append(str(dest))
                except Exception as exc:
                    _errs_inner.append(f"{f.name}: {exc}")

            # Remove source dir if now empty
            try:
                remaining = [x for x in src_path.iterdir() if x.suffix.lower() in AUDIO_EXT]
                if not remaining:
                    src_path.rmdir()
            except OSError:
                pass
        return _moved_inner, _errs_inner

    moved, errors = await loop.run_in_executor(None, _do_merge)

    # Rebuild library cache for affected tracks
    def _rescan_target():
        from app.library import read_track_meta
        new_entries = []
        for dest in target_dir.iterdir():
            if dest.suffix.lower() not in AUDIO_EXT:
                continue
            m = read_track_meta(dest)
            if m:
                new_entries.append(m)
        return new_entries

    new_entries = await loop.run_in_executor(None, _rescan_target)

    # Purge old entries for source folders + old target entries, re-insert
    moved_set = {str(m) for m in moved}
    src_set   = set(source_folders)
    for i in range(len(library_cache) - 1, -1, -1):
        t = library_cache[i]
        if t.get("folder") in src_set or t.get("path") in moved_set:
            library_index.pop(t["id"], None)
            library_cache.pop(i)

    for entry in new_entries:
        library_index[entry["id"]] = entry
        library_cache.append(entry)

    await lib_broadcast({"type": "library_changed"})
    return {"ok": True, "moved": len(moved), "errors": errors, "target": safe_target}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/status")
async def api_status():
    s = load_settings()
    return {
        "status": "ok",
        "audio_format": s.get("audio_format", _ENV_AUDIO_FORMAT),
        "audio_quality": s.get("audio_quality", _ENV_AUDIO_QUALITY),
        "jobs_total": len(jobs),
        "jobs_running": sum(1 for j in jobs.values() if j.status.value == "running"),
        "library_tracks": len(library_cache),
        "library_scanning": lib_scan_status.get("running", False),
    }


@app.get("/")
async def index():
    return FileResponse("/app/static/index.html")
