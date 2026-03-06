# lbdl — Music Library Downloader

A self-hosted music library manager. Paste a ListenBrainz or YouTube/Invidious playlist URL, download every track as high-quality audio, auto-tag with MusicBrainz metadata, and browse your library from any device — including as a PWA on Android and iOS.

---

## Features

### Downloading
- Paste any **ListenBrainz** or **YouTube / Invidious playlist** URL and download all tracks in one click
- Downloads via **yt-dlp** — supports opus, mp3, flac, m4a, aac, wav (configurable)
- **Smart skip logic** — three-tier duplicate detection stops re-downloading tracks that already exist:
  1. In-memory library cache matched by **fuzzy tag comparison** (catches re-tagged/renamed files)
  2. Filesystem exact stem match (fast path)
  3. Filesystem fuzzy stem match (handles YouTube title noise)
- **YouTube noise stripping** — raw titles like `"Kal Chaudhvin Ki Raat Thi with lyrics | कल चौदहवीं की रात थी | Jagjit Singh | Romantic Ghazal"` are normalised to `"kal chaudhvin ki raat thi"` before comparison
- **Channel artist detection** — labels like `"Saregama Music"` or `"Tips Official"` are flagged as unreliable and excluded from artist matching to avoid false misses
- Real-time download progress via WebSocket — track-by-track status in the browser
- M3U playlist written automatically on job completion
- Cookie support — place `cookies.txt` in config dir for age-restricted content

### Auto-Tagging
- **MusicBrainz** lookup with AcoustID fingerprinting for audio-based identification
- **iTunes** cover art candidates
- **JioSaavn** and **Gaana** cover sources for Indian music
- Synced **LRC lyrics** via LRCLIB, with plain lyrics fallback
- One-click re-tag any track, or bulk tag all untagged tracks
- Manual candidate selection — browse ranked matches and apply the one you want
- Artist tag and folder name correction after tagging

### Library
- Full library scan with live progress
- **Untagged filter** — shows only tracks missing artist, album, or clean title, optionally limited to files added in the last N days (configurable)
- **Duplicate detection and cleanup** — fuzzy artist+title matching across the entire library, keeps the oldest copy, deletes newer duplicates
  - Scheduled via cron expression (e.g. `0 4 * * *`)
  - Manual trigger with dry-run preview endpoint
- **Artist merge** — merge multiple artist folders into one with a target name
- Cover art browser — view and replace embedded cover art per track
- Lyrics status indicator per track

### Playlist Sync
- Save playlists to auto-sync on a schedule (cron-configurable)
- Supports both ListenBrainz and Invidious/YouTube playlist sources
- Processed-tracks log (`processed.json`) prevents re-processing known tracks
- Inline download + autotag in the sync container — no separate queue needed

### Settings
- Audio format and quality
- Invidious instance URL
- ListenBrainz token
- AcoustID API key
- Gotify push notifications (URL + token + priority)
- Sync cron schedule
- **Untagged new days** — filter untagged view to only recently added files (0 = all time)
- **Dedup enabled / dedup cron** — scheduled automatic duplicate removal
- Test notification button

### PWA (Progressive Web App)
- Installable on **Android** (Firefox, Chrome) via `beforeinstallprompt` banner
- Installable on **iOS / iPadOS** via "Add to Home Screen" hint (Firefox/Safari)
- Standalone window — no browser chrome when launched from home screen
- Safe-area CSS insets — notch and gesture-bar aware
- Service worker caches the app shell for fast load; API calls always go live
- Apple touch icons served at all root probe paths (`/apple-touch-icon.png`, `/apple-touch-icon-180x180.png`, etc.)
- Maskable icon variants for Android adaptive icon shapes

---

## Quick Start (Docker Compose)

```bash
# 1. Copy the Portainer stack compose file
cp docker-compose.portainer.yaml docker-compose.yaml

# 2. Edit paths and tokens
nano docker-compose.yaml

# 3. Pull and start
docker compose pull
docker compose up -d
```

Open **http://your-host:8032**

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

## Building from Source

```bash
# Build both images
docker buildx build -f Dockerfile.api  -t ustxforxdocker/lbdl-api:latest  --push .
docker buildx build -f Dockerfile.sync -t ustxforxdocker/lbdl-sync:latest --push .
```

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
| `POST` | `/api/settings` | Update settings |

---

## License

MIT
