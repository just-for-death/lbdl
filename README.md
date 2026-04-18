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
cp docker-compose.portainer.yaml docker-compose.yaml
# edit paths and tokens
docker compose pull
docker compose up -d
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `LBDL_DATA_DIR` | `/app/music` | Where audio files are stored |
| `LBDL_CONFIG_DIR` | `/app/config` | Where settings, playlists, cookies live |
| `LBDL_YTDLP_DIR` | `/app/config` | Where `cookies.txt` is read from |
| `LBDL_AUDIO_FORMAT` | `opus` | yt-dlp output format |
| `LBDL_AUDIO_QUALITY` | `0` | yt-dlp quality (0 = best) |
| `LBDL_SCHEDULER_CRON` | `0 */2 * * *` | Playlist sync schedule |
| `LBDL_LB_TOKEN` | *(empty)* | ListenBrainz user token |
| `LBDL_INVIDIOUS_INSTANCE` | `https://inv.nadeko.net` | Invidious instance to use |
| `LBDL_ACOUSTID_KEY` | *(empty)* | AcoustID API key for fingerprinting |
| `LBDL_TZ` | `UTC` | Timezone for cron |
| `LBDL_API_TOKEN` | *(empty)* | If set, **all** `/api/*` and `/ws/*` require authentication (see below) |
| `LBDL_COOKIE_SECURE` | *(empty)* | Set to `1` / `true` when the site is served over HTTPS so the login cookie is marked `Secure` |

### Authentication (`LBDL_API_TOKEN`)

When `LBDL_API_TOKEN` is **unset** or **empty**, the API and WebSockets remain open (same as older releases — suitable only on a trusted network).

When set to a long random string:

1. **Web UI** — Opening the app shows a sign-in screen. Paste the same value as the token; the server sets an HttpOnly session cookie so you stay logged in in that browser.
2. **Scripts / curl** — Send the token on every request:
   ```bash
   curl -sS -H "Authorization: Bearer YOUR_TOKEN" http://localhost:8032/api/status
   ```
3. **WebSockets** — Browsers send the session cookie automatically after login. Other clients can use:
   - Header `Authorization: Bearer YOUR_TOKEN` on the WebSocket handshake (if your client supports it), or
   - Query parameter: `wss://host/ws/library?token=YOUR_TOKEN` (avoid logging full URLs).

Health checks (`GET /health`) and static assets (HTML, icons, `/static/*`, PWA files) stay **unauthenticated** so containers and install prompts still work.

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
