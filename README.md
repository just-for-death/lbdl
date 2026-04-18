# lbdl — Music Library Downloader

A self-hosted music library manager. Paste a ListenBrainz or YouTube/Invidious playlist URL, download every track as high-quality audio, auto-tag with iTunes/MusicBrainz/AcoustID metadata, fetch synced lyrics, and browse your library from any device — including as a PWA on Android and iOS.

---

## Features

### Downloading
- Paste any **ListenBrainz** or **YouTube / Invidious playlist** URL and download all tracks in one click
- Downloads via **yt-dlp** — supports opus, mp3, flac, m4a, aac, wav (configurable)
- **8-variant smart skip logic** — tracks already in the library are never re-downloaded, even when the YouTube title is heavily noisy. Eight title-normalisation variants are tried in order:

  | Variant | Pattern handled | Example |
  |---|---|---|
  | V1 Full norm | Baseline | Always tried |
  | V2 Pre-dash | `Song - Movie \| …` | `Pardesiya - Param Sundari` → `pardesiya` |
  | V3 Ft-strip | `Song Ft. Collab …` | `Saiyaara Ft. Kishore Kumar` → `saiyaara` |
  | V4 Post-dash | Noise prefix before dash | `New Hindi Song - Tere Bina` → `tere bina` |
  | V5 Artist-strip | Artist name in title | `SOFTLY KARAN AUJLA` → `softly` |
  | V6 Lib-artist-strip | Per-entry: strip library artist | `Channa Mereya Arijit Singh` → `channa mereya` |
  | V7 Non-Latin fallback | Non-Latin script first segment | `कल चौदहवीं \| Kal Chaudhvin Ki Raat Thi` → latin segment |
  | V8 First-3-words | Very long residual titles | `Lag Ja Gale Lata Mangeshkar Woh Kaun Thi` → `lag ja gale` |

- Noise stripping covers: `unplugged`, `acoustic`, `live`, `version`, `remix`, `cover`, `mashup`, `remastered`, `new`, `latest`, `punjabi`, `hindi`, `bollywood`, `dj`, `club`, `season N`, `vol N`, and all standard YouTube suffixes
- **Channel artist detection** — labels like `"Saregama Music"` or `"Tips Official"` are excluded from artist matching
- Real-time download progress via WebSocket — track-by-track status in the browser
- M3U playlist written automatically on job completion
- Cookie support — place `cookies.txt` in config dir for age-restricted content
- **Job memory cap** — oldest completed jobs are evicted after 200 entries to prevent memory growth

### Auto-Tagging
- **Three-source pipeline**: iTunes (fastest, no key) → MusicBrainz text search → AcoustID fingerprint
- **Synced LRC lyrics** fetched automatically during tagging from LRCLIB, NetEase, JioSaavn, Lyrics.ovh, and Genius — `.lrc` sidecar saved alongside each file
- One-click re-tag any track, or bulk tag all untagged tracks
- Manual candidate selection — browse ranked matches from all three sources and apply the one you want
- **Auto-tag after sync** *(new)* — optionally trigger the untagged tagger automatically after every playlist download completes (see Settings)
- **Concurrency guard** — rapid button clicks no longer spawn parallel workers that compete for the same files
- After tagging, the library cache is updated immediately without requiring a rescan — untagged count reflects the result straight away

### Library
- Full library scan with live progress
- **Untagged filter** — shows only tracks missing artist, album, or with raw YouTube filenames, optionally limited to files added in the last N days (configurable). False-positives on properly-tagged files are eliminated: `Artist/Title.opus` is never wrongly flagged as untagged
- **Duplicate detection and cleanup** — fuzzy artist+title matching across the entire library, keeps the oldest copy, deletes newer duplicates
  - Scheduled via cron expression (e.g. `0 4 * * *`)
  - Manual trigger with dry-run preview
  - Duplicate scan runs off the event loop so it never blocks the UI on large libraries
- **Artist merge** — merge multiple artist folders into one, with path traversal protection
- Cover art browser — view and replace embedded cover art per track
- Lyrics status indicator per track

### Playlist Sync
- Save playlists to auto-sync on a schedule (cron-configurable)
- Supports both ListenBrainz and Invidious/YouTube playlist sources
- `processed.json` written atomically with `fcntl` file locking — safe against concurrent cron runs corrupting the file
- Inline download + autotag in the sync container — no separate queue needed

### Settings
- Audio format and quality
- Invidious instance URL
- ListenBrainz token
- AcoustID API key (free at acoustid.org/api-key)
- Gotify push notifications (URL + token + priority + test button)
- Sync cron schedule with preset buttons and expression validation
- **Auto-tag after sync** *(new)* — toggle to automatically run the untagged tagger after every playlist download
- **Untagged new days** — filter untagged view to recently added files (0 = all time)
- **Dedup enabled / dedup cron** — scheduled automatic duplicate removal

### PWA (Progressive Web App)
- Installable on **Android** (Firefox, Chrome) via `beforeinstallprompt` banner
- Installable on **iOS / iPadOS** via "Add to Home Screen" hint
- Standalone window — no browser chrome when launched from home screen
- Service worker caches the app shell for fast load; API calls always go live
- Maskable icon variants for Android adaptive icon shapes

---

## Quick Start (Portainer)

1. In Portainer: **Stacks → Add Stack → paste `docker-compose.portainer.yaml`**
2. Replace `/your/music/path` and `/your/config/path` with absolute paths on your host
3. Optionally fill in `LBDL_LB_TOKEN` and `LBDL_ACOUSTID_KEY`
4. Click **Deploy**
5. Open **http://your-host:8032**

## Quick Start (Docker Compose)

```bash
git clone https://github.com/justxforxdocker/lbdl
cd lbdl
# Optional: edit environment: in docker-compose.yaml (tokens, TZ, HTTPS cookie, etc.)
docker compose build && docker compose up -d
```

The repo includes a minimal **`.env`** that sets **`COMPOSE_PARALLEL_LIMIT=1`** so Compose does not build services in parallel (avoids interleaved build logs). It does not inject app config into containers; that stays in **`docker-compose.yaml`**. To disable, remove the file or set `COMPOSE_DISABLE_ENV_FILE=1`.

Use `docker compose pull` instead of `build` if you use prebuilt images from a registry.

---

## Environment Variables

Defaults for Docker are set in **`docker-compose.yaml`** under each service’s `environment:` block. The project **`.env`** only tunes the Compose CLI (see Quick Start); it is not used for service `environment:` substitution.

| Variable | Default | Description |
|---|---|---|
| `LBDL_DATA_DIR` | `/app/music` | Where audio files are stored |
| `LBDL_CONFIG_DIR` | `/app/config` | Where settings, playlists, cookies, `auth.json` live |
| `LBDL_STATIC_DIR` | *(auto)* | Override path to the `static/` web assets (defaults to repo `static/` in dev, `/app/static` in Docker) |
| `LBDL_YTDLP_DIR` | `/app/config` | Where `cookies.txt` is read from |
| `LBDL_AUDIO_FORMAT` | `opus` | yt-dlp output format |
| `LBDL_AUDIO_QUALITY` | `0` | yt-dlp quality (0 = best) |
| `LBDL_SCHEDULER_CRON` | `0 */2 * * *` | Playlist sync schedule |
| `LBDL_LB_TOKEN` | *(empty)* | ListenBrainz user token |
| `LBDL_INVIDIOUS_INSTANCE` | `https://inv.nadeko.net` | Invidious instance to use |
| `LBDL_ACOUSTID_KEY` | *(empty)* | AcoustID API key for fingerprinting |
| `LBDL_TZ` | `UTC` | Timezone for cron |
| `LBDL_COOKIE_SECURE` | *(empty)* | Set to `1` / `true` when the site is served over HTTPS so the login cookie is marked `Secure` |

### Authentication (username / password)

On first startup the API creates **`config/auth.json`** with default login **`admin`** / **`admin`**. Change the password under **Settings → Account** in the web UI (recommended before exposing the service).

1. **Web UI** — Sign-in page; the server sets an HttpOnly session cookie after successful login.
2. **Scripts / curl** — HTTP Basic auth (same username and password as the UI):
   ```bash
   curl -sS -u admin:admin http://localhost:8032/api/status
   ```
   Or explicitly:
   ```bash
   curl -sS -H "Authorization: Basic $(printf 'admin:admin' | base64)" http://localhost:8032/api/status
   ```
3. **WebSockets** — Browsers send the session cookie automatically after login. Other clients should send `Authorization: Basic …` on the WebSocket handshake if supported.

Credentials are stored **hashed** in `auth.json` inside your config volume. Health checks (`GET /health`) and static assets (HTML, icons, `/static/*`, PWA files) stay **unauthenticated** so containers and install prompts still work.

---

## Volumes

| Container path | Purpose |
|---|---|
| `/app/music` | Your music library (bind-mount your NAS/drive here) |
| `/app/config` | Settings, playlists, cookies, processed log |

---

## Services

| Service | Description |
|---|---|
| `lbdl-api` | FastAPI backend + web UI on port 8032 |
| `lbdl-sync` | Scheduled playlist sync via cron |

Optional worker services (started with `--profile workers`):

| Service | Description |
|---|---|
| `lbdl-redis` | Job state store |
| `lbdl-rabbitmq` | Download job queue |
| `lbdl-downloader` | Parallel download workers |
| `lbdl-tagger` | Parallel autotag workers |

---

## Development / testing

From the repository root (with dependencies installed):

```bash
pip install pytest httpx
pytest tests/ -v
```

Tests cover authentication, static files, merge path guards, **`POST /api/jobs`**, and job eviction order (`tests/test_job_eviction.py`).

---

## Building and Pushing Docker Images

```bash
# Build and push both images in one go
docker buildx build -f Dockerfile.api  -t justxforxdocker/lbdl-api:latest  --push .
docker buildx build -f Dockerfile.sync -t justxforxdocker/lbdl-sync:latest --push .
```

Or build locally first, then push:

```bash
docker build -f Dockerfile.api  -t justxforxdocker/lbdl-api:latest  .
docker build -f Dockerfile.sync -t justxforxdocker/lbdl-sync:latest .

docker login
docker push justxforxdocker/lbdl-api:latest
docker push justxforxdocker/lbdl-sync:latest
```

After pushing, redeploy in Portainer: **Stack → Editor → Update the stack** (or pull + restart containers).

---

## Playlist download API (scripts, mobile, other apps)

All `/api/*` routes require **HTTP Basic** auth (same username/password as the web UI). Use `-u user:pass` with `curl`, or `Authorization: Basic …` in your HTTP client.

### Start a download

`POST /api/jobs`  
`Content-Type: application/json`

| Field | Required | Description |
|--------|----------|-------------|
| `playlist_url` | **yes** | ListenBrainz playlist URL (`https://listenbrainz.org/playlist/<uuid>`), **or** a YouTube / Invidious URL that includes a playlist id: `?list=PL…` |
| `invidious_instance` | no | Only for YouTube-style playlists: override the Invidious base URL (e.g. `https://inv.nadeko.net`). If omitted, the server picks the instance from the playlist URL or from Settings. |

**Success — HTTP 200**

```json
{"job_id": "550e8400-e29b-41d4-a716-446655440000", "source": "listenbrainz"}
```

`source` is `"listenbrainz"` or `"invidious"`.

**Error — HTTP 400**

```json
{"error": "…"}
```

### Poll job status

`GET /api/jobs/{job_id}`

Returns JSON including:

- `status`: `"queued"` → `"running"` → `"done"` or `"error"`
- `playlist_name`, `source`
- `tracks`: per-track `status` (`pending`, `found`, `done`, `failed`, …) and optional `error`
- `logs`: text lines from the job

**HTTP 404** if the job id is unknown or was evicted (only the last ~200 jobs are kept in memory).

### Live progress (WebSocket)

Connect to:

`ws://<host>:<port>/ws/<job_id>` (or `wss://` behind HTTPS)

Use the **same authentication** as the API: session cookie (browser) or `Authorization: Basic` on the WebSocket handshake if your client supports it.

Message types include `state`, `playlist_info`, `track_start`, `track_downloading`, `track_done`, `log`, `job_done`, `error` — same as the web UI.

### Example: `curl`

```bash
BASE="http://localhost:8032"   # or your server URL
USER="admin"
PASS="admin"

# Start job (replace with a real playlist URL)
RESP=$(curl -sS -u "$USER:$PASS" -X POST "$BASE/api/jobs" \
  -H "Content-Type: application/json" \
  -d '{"playlist_url":"https://www.youtube.com/playlist?list=PLxxxxxxxxxx"}')
echo "$RESP"
JOB_ID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('job_id',''))")

# Poll until done
curl -sS -u "$USER:$PASS" "$BASE/api/jobs/$JOB_ID"
```

Files are written under `LBDL_DATA_DIR` (default `/app/music` in Docker). An M3U may be created under `_Playlists/` when the job finishes.

---

## API Endpoints (selected)

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/jobs` | Start a playlist download job |
| `GET` | `/api/jobs/{id}` | Job status |
| `GET` | `/api/library/tracks` | All scanned tracks |
| `POST` | `/api/library/scan` | Trigger library rescan |
| `GET` | `/api/library/untagged-count` | Untagged track count |
| `GET` | `/api/library/untagged` | Untagged track list (respects `untagged_new_days`) |
| `POST` | `/api/library/autotag-untagged` | Auto-tag all untagged tracks |
| `POST` | `/api/library/autotag-all` | Auto-tag every track |
| `GET` | `/api/library/dedup/preview` | Dry-run duplicate scan |
| `POST` | `/api/library/dedup` | Delete duplicate tracks (keep oldest) |
| `GET` | `/api/settings` | Read settings |
| `POST` | `/api/settings` | Update settings (validates cron expressions) |

---

## License

MIT
