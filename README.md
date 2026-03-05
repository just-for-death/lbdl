<div align="center">
  <h1>lbdl</h1>
  <p><strong>ListenBrainz &amp; Invidious playlist downloader</strong></p>
  <p>
    <img src="https://img.shields.io/badge/Python-3.12-blue?style=flat-square&logo=python" alt="Python"/>
    <img src="https://img.shields.io/badge/FastAPI-async-green?style=flat-square&logo=fastapi" alt="FastAPI"/>
    <img src="https://img.shields.io/badge/yt--dlp-latest-red?style=flat-square" alt="yt-dlp"/>
    <img src="https://img.shields.io/badge/PWA-ready-purple?style=flat-square" alt="PWA"/>
    <img src="https://img.shields.io/badge/license-MIT-lightgrey?style=flat-square" alt="License"/>
  </p>
</div>

---

Paste a **ListenBrainz** or **Invidious / YouTube** playlist URL and get a fully
organised music library — with cover art, synced lyrics, multi-value artist tags,
and an M3U playlist file — with one click.

## Quick Start

```bash
git clone https://github.com/YOUR_USERNAME/lbdl
cd lbdl
docker compose up -d --build
# Open http://localhost:8032
```

## Supported Sources

| Source | URL Format | How It Works |
|---|---|---|
| **ListenBrainz** | `listenbrainz.org/playlist/<uuid>` | Fetches track list via LB API, searches YouTube Music for each track |
| **Invidious** | `inv.example.com/playlist?list=PL…` | Uses Invidious API directly — video IDs are already known, no search needed |
| **YouTube** | `youtube.com/playlist?list=PL…` | Routed through your configured Invidious instance (`LBDL_INVIDIOUS_INSTANCE`) |

The UI auto-detects the source when you paste a URL.

## Output Structure

```
music/
  Artist/
    2024 - Album Name/
      01 - Track Title.opus
      01 - Track Title.lrc     ← synced LRC lyrics (timestamped)
      cover.jpg
  _Playlists/
    My Playlist.m3u            ← underscore prefix sorts first in file managers
```

## Configuration

Set these in `compose.yaml` → `environment`:

| Variable | Default | Description |
|---|---|---|
| `LBDL_AUDIO_FORMAT` | `opus` | `opus`, `mp3`, `flac` |
| `LBDL_AUDIO_QUALITY` | `0` | VBR quality 0–10 (0 = best) |
| `LBDL_LB_TOKEN` | _(empty)_ | ListenBrainz API token — required for private playlists |
| `LBDL_INVIDIOUS_INSTANCE` | `https://inv.nadeko.net` | Default Invidious instance used when YouTube URLs are submitted |
| `LBDL_SCHEDULER_CRON` | `0 */2 * * *` | Cron schedule for background auto-sync |
| `LBDL_TZ` | `UTC` | Container timezone |

## Features

- **Deezer-inspired UI** — dark, gradient-accented interface with real-time WebSocket progress
- **PWA** — installable as a desktop/mobile app, works offline (cached shell)
- **Auto-detect source** — paste any URL; the UI switches tabs automatically
- **Invidious pagination** — fetches all pages of large playlists automatically
- **Direct download for Invidious** — video IDs are known upfront, no YTMusic search needed
- **Synced lyrics** — `.lrc` sidecar + embedded tag via syncedlyrics (lrclib → Musixmatch → NetEase)
- **Multi-value tags** — `artists` / `albumartists` fields for Navidrome & Jellyfin
- **Cover art** — embedded thumbnail + `cover.jpg` sidecar, upscaled to 544px
- **Artist deduplication** — collapses `"A, A, A, B"` → `"A, B"` from yt-dlp metadata
- **Retry + back-off** — 3× retries with 1s/2s/4s delays for 403/429/5xx
- **Cookies support** — place `cookies.txt` in the config volume for premium audio
- **M3U with real durations** — correct seek bars in every player, includes pre-existing tracks
- **Auto-sync** — cron job re-downloads new tracks added to saved playlists

## Volumes

| Host path | Container path | Purpose |
|---|---|---|
| `./config` | `/app/config` | `playlists.json` + optional `cookies.txt` |
| `./music`  | `/app/music`  | Downloaded audio, art, lyrics, `_Playlists/` |

## Premium Audio / Cookies

Export YouTube cookies with a browser extension (e.g. *Get cookies.txt LOCALLY*)
and save as `./config/cookies.txt`. lbdl copies it to a temp file before each
download so yt-dlp never corrupts the original file.

## Running Without Docker

```bash
pip install -r requirements.txt
LBDL_DATA_DIR=./music LBDL_CONFIG_DIR=./config \
  uvicorn app.main:app --host 0.0.0.0 --port 8032 --reload
```

## API

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/jobs` | Start a download job (`playlist_url`, optional `invidious_instance`) |
| `GET`  | `/api/jobs/:id` | Get job status + tracks |
| `GET`  | `/api/playlists` | List saved playlists |
| `DELETE` | `/api/playlists` | Remove a saved playlist (`{ "url": "..." }`) |
| `GET`  | `/api/config` | Get runtime config (invidious instance, audio format) |
| `WS`   | `/ws/:job_id` | Real-time job events |
| `WS`   | `/ws/server-logs` | Real-time server log stream |

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).
