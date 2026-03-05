import asyncio
import json
import logging
import os
import re
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
OUTPUT_DIR          = Path(os.getenv("LBDL_DATA_DIR",          "/app/music"))
CONFIG_DIR          = Path(os.getenv("LBDL_CONFIG_DIR",        "/app/config"))
YTDLP_DIR           = Path(os.getenv("LBDL_YTDLP_DIR",         "/app/config"))
AUDIO_FORMAT        = os.getenv("LBDL_AUDIO_FORMAT",           "opus")
AUDIO_QUALITY       = os.getenv("LBDL_AUDIO_QUALITY",          "0")
LB_TOKEN            = os.getenv("LBDL_LB_TOKEN",               "")
INVIDIOUS_INSTANCE  = os.getenv("LBDL_INVIDIOUS_INSTANCE",     "https://inv.nadeko.net")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
PLAYLISTS_FILE = CONFIG_DIR / "playlists.json"
ytm = YTMusic()


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
    id:                str
    playlist_url:      str
    source:            str = "listenbrainz"   # "listenbrainz" | "invidious"
    invidious_instance: str = ""
    playlist_name:     str = ""
    status:            JobStatus = JobStatus.QUEUED
    tracks:            list[Track] = field(default_factory=list)
    logs:              list[str]   = field(default_factory=list)


jobs:        dict[str, Job]             = {}
job_queue:   Queue                      = Queue()
subscribers: dict[str, list[WebSocket]] = {}


# ── URL Detection ─────────────────────────────────────────────────────────────

def detect_source(url: str) -> str:
    """Return 'listenbrainz', 'youtube', or 'invidious' based on URL shape."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower().lstrip("www.")
    if "listenbrainz.org" in host:
        return "listenbrainz"
    if host in ("youtube.com", "youtu.be", "m.youtube.com"):
        return "youtube"
    # Any other domain with ?list= is an Invidious instance
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
    """Return the Invidious instance base URL to use for API calls.

    If the URL is from youtube.com, fall back to the configured default instance.
    Otherwise use the URL's own origin so the user's chosen instance is respected.
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    if host in ("www.youtube.com", "youtube.com", "youtu.be", "m.youtube.com"):
        return INVIDIOUS_INSTANCE.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}"


# ── ListenBrainz fetch ────────────────────────────────────────────────────────

def fetch_lb_playlist(playlist_id: str) -> tuple[str, list[dict]]:
    headers = {"Authorization": f"Token {LB_TOKEN}"} if LB_TOKEN else {}
    resp = requests.get(
        f"https://api.listenbrainz.org/1/playlist/{playlist_id}",
        headers=headers, timeout=15,
    )
    if resp.status_code == 401:
        raise ValueError("Playlist is private — set LBDL_LB_TOKEN in your environment")
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
    """Fetch all videos from an Invidious (or YouTube) playlist via the Invidious API.

    Handles pagination automatically — the API returns up to 100 videos per page.
    Each returned dict has at minimum: videoId, title, author.
    """
    base = f"{instance.rstrip('/')}/api/v1/playlists/{playlist_id}"

    # First page
    resp = requests.get(base, timeout=20)
    if resp.status_code == 404:
        raise ValueError(f"Playlist '{playlist_id}' not found on {instance}")
    resp.raise_for_status()
    data = resp.json()

    title      = data.get("title", "Unnamed Playlist")
    videos     = list(data.get("videos", []))
    total      = data.get("videoCount", len(videos))
    page       = 2

    # Paginate until we have all tracks
    while len(videos) < total:
        r = requests.get(base, params={"page": page}, timeout=20)
        if not r.ok:
            break
        page_data = r.json()
        page_vids = page_data.get("videos", [])
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
        status="found",  # video_id already known — no search needed
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

        job.status = JobStatus.DONE
        await log(f"Finished — {done} downloaded, {failed} failed")
        await broadcast(job.id, {"type": "job_done", "done": done, "failed": failed})

    except Exception as e:
        import traceback
        job.status = JobStatus.ERROR
        for line in traceback.format_exc().splitlines():
            await log(f"  {line}")
        await log(f"Error: {e}")
        await broadcast(job.id, {"type": "error", "msg": str(e)})


async def _process_lb_job(job: Job, log):
    playlist_id = get_lb_playlist_id(job.playlist_url)
    if not playlist_id:
        raise ValueError("Invalid ListenBrainz playlist URL")

    await log("Fetching playlist from ListenBrainz…")
    name, raw_tracks = fetch_lb_playlist(playlist_id)
    job.playlist_name = name
    job.tracks = [parse_lb_track(t) for t in raw_tracks]
    await broadcast(job.id, {"type": "playlist_info", "name": name, "total": len(job.tracks), "source": "listenbrainz"})
    await log(f"Found {len(job.tracks)} tracks in \"{name}\"")
    save_playlist(job.playlist_url, name, "listenbrainz")

    for i, track in enumerate(job.tracks):
        await broadcast(job.id, {"type": "track_start", "index": i, "title": track.title, "artist": track.artist})

        query = f"{track.artist} {track.title}".strip()
        await log(f"[{i+1}/{len(job.tracks)}] Searching: {query}")
        results = await asyncio.get_event_loop().run_in_executor(
            None, lambda q=query: ytm.search(q, filter="songs", limit=3)
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
    name, raw_tracks = fetch_invidious_playlist(playlist_id, instance)
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


async def _download_track(job: Job, i: int, track: Track, log):
    """Shared download step used by both LB and Invidious flows."""
    if already_exists(track.artist, track.title):
        track.status = "done"
        existing = find_existing_path(track.artist, track.title)
        if existing:
            track.final_path = str(existing)
        await log("  ✓ Already exists, skipping")
        await broadcast(job.id, {"type": "track_done", "index": i, "status": "done"})
        return

    await log(f"  ↓ Downloading ({AUDIO_FORMAT})…")
    await broadcast(job.id, {"type": "track_downloading", "index": i})

    def _run(vid=track.video_id, art=track.artist, ttl=track.title):
        collected: list[str] = []
        result = dl_track(vid, art, ttl, log_fn=collected.append)
        return result, collected

    (ok, final_path, output), dl_logs = await asyncio.get_event_loop().run_in_executor(None, _run)
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
    yield


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
            return {"error": "Invalid ListenBrainz playlist URL — expected format: https://listenbrainz.org/playlist/<uuid>"}

    elif source in ("youtube", "invidious"):
        if not get_yt_playlist_id(url):
            return {"error": "No playlist ID found in URL — expected ?list=PLxxxxxx"}

    else:
        return {"error": "Unrecognised URL — paste a ListenBrainz, YouTube, or Invidious playlist URL"}

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
    """Expose runtime config so the UI can pre-fill the Invidious instance field."""
    return {
        "invidious_instance": INVIDIOUS_INSTANCE,
        "audio_format":       AUDIO_FORMAT,
    }


@app.get("/")
async def index():
    return FileResponse("/app/static/index.html")
