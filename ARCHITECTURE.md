# LBDL Microservices Architecture

## Overview

This is a complete refactoring of LBDL into a decoupled, scalable microservices architecture. The monolithic downloader and tagger functions have been split into independent services that communicate via message queues, eliminating race conditions and enabling true parallelization.

## Problem Solved

**Before (Monolithic):**
- Download and tagging happened in sequence, blocking each other
- If a track moved albums between download and tagging, it could be downloaded twice
- No clear state tracking across the pipeline
- Scheduler blocked waiting for downloads
- Difficult to scale or restart individual components

**After (Microservices):**
- Download and tagging run concurrently in separate services
- State synchronized via Redis (single source of truth)
- Work queue persists even if services restart
- Scheduler enqueues tasks and returns immediately
- Each service can scale independently
- Complete observability of each track's status

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    REDIS (State Store)                       │
│  track:{id} → {status, path, metadata, timestamps}          │
│  processed_tracks:{playlist_id} → set of already-handled     │
└─────────────────────────────────────────────────────────────┘
                              ▲
                              │
                    ┌─────────┴─────────┐
                    │                   │
           ┌────────▼────────┐  ┌──────▼────────┐
           │  RabbitMQ       │  │  RabbitMQ     │
           │ (work queues)   │  │ (work queues) │
           └────────▲────────┘  └──────▲────────┘
                    │                   │
        ┌───────────┴──────────┬────────┴─────────┬──────────┐
        │                      │                  │          │
        ▼                      ▼                  ▼          ▼
    ┌────────────┐       ┌──────────────┐  ┌──────────┐ ┌────────┐
    │  API Server│       │ Downloader   │  │  Tagger  │ │  Sync  │
    │ (Port 8032)│       │  Service     │  │ Service  │ │Service │
    │            │       │              │  │          │ │(Cron)  │
    │ - Library  │       │ - Download   │  │ - Fetch  │ │        │
    │ - UI       │       │   audio      │  │   metadata
    │ - WebSocket│       │ - Queue work │  │ - Auto   │ │- Sync  │
    │ - Triggers │       │   from QB    │  │   tags   │ │  playlists
    └────────────┘       └──────────────┘  │ - Reorganize
                                            │ - Cover art
                                            └──────────┘
```

## Services

### 1. **lbdl-api** (Main API & UI)
**Port:** 8032

**Responsibilities:**
- Serve web UI (Vue.js)
- REST API endpoints
- Library scanning and indexing
- Job management (from UI)
- WebSocket for real-time updates
- Settings management

**Communicates with:**
- Redis (state queries)
- Downloader (via API: POST /trigger-download)
- Tagger (via API: POST /retag-track)

### 2. **lbdl-downloader** (Download Worker)
**Concurrency:** 3 (configurable via WORKER_CONCURRENCY)

**Responsibilities:**
- Listen on RabbitMQ DOWNLOAD_QUEUE
- Download audio from YouTube Music
- Handle yt-dlp conversion
- Update track state in Redis
- **Auto-enqueue to TAGGING_QUEUE** when done

**Flow:**
```
DOWNLOAD_QUEUE (message) 
  → download_track(video_id, artist, title)
  → Redis: set download_status = "completed", file_path = "/path/to/file"
  → Auto-enqueue TaggingTask to TAGGING_QUEUE
```

### 3. **lbdl-tagger** (Metadata & Tagging Worker)
**Concurrency:** 2 (configurable via WORKER_CONCURRENCY)

**Features:**
- ✅ AcoustID fingerprinting (primary)
- ✅ MusicBrainz text search (fallback)
- ✅ **Album fetcher** (integrated)
  - Fetches album art from MusicBrainz CAA
  - Falls back to iTunes
  - Embeds covers in file
- ✅ iTunes search (final fallback)
- ✅ Auto-tagging with confidence scoring
- ✅ File reorganization (Artist/Year - Album/Track)

**Flow:**
```
TAGGING_QUEUE (message)
  → fetch_metadata(file_path) [AcoustID → MB → iTunes]
  → apply_metadata_and_reorganize(file, metadata, cover, OUTPUT_DIR)
  → Redis: set tagging_status = "completed", tagged_path = "/new/path"
  → Broadcast WebSocket update
```

### 4. **lbdl-sync** (Scheduler)
**Schedule:** Cron (default: every 2 hours, `0 */2 * * *`)

**Responsibilities:**
- Fetch latest tracks from ListenBrainz playlists
- Check for duplicates using Redis
- Enqueue new downloads to DOWNLOAD_QUEUE
- Return immediately (non-blocking)

**Flow:**
```
Cron trigger
  → Load playlists.json
  → For each playlist:
      - Fetch from ListenBrainz API
      - For each track:
          - Check Redis duplicate set
          - If new: create DownloadTask
          - Enqueue to DOWNLOAD_QUEUE
          - Add to processed_tracks set
  → Done (return immediately)
```

## Key Design Patterns

### 1. **State Synchronization via Redis**

Each track's state lives in Redis:
```python
# Key: "lbdl:track:{track_id}"
# Value: JSON with full state
{
  "track_id": "playlist123:artist:title",
  "artist": "The Beatles",
  "title": "Let It Be",
  
  # Download phase
  "download_status": "completed",  # queued → processing → completed/failed
  "file_path": "/app/music/downloads/tmp_xyz.opus",
  "download_error": null,
  
  # Tagging phase
  "tagging_status": "completed",   # queued → processing → completed/failed
  "tagged_path": "/app/music/The Beatles/1970 - Let It Be/01 Let It Be.opus",
  "metadata": {
    "artist": "The Beatles",
    "title": "Let It Be",
    "album": "Let It Be",
    "year": "1970"
  },
  
  "last_updated": "2024-03-06T10:30:45.123456"
}
```

**Advantages:**
- Single source of truth
- Atomic updates
- Survives service restarts
- Query from any service
- TTL-based cleanup (30 days)

### 2. **Duplicate Prevention**

Redis set prevents re-downloading when tracks move albums:
```python
# Key: "lbdl:processed_tracks:{playlist_id}"
# Value: Set of "artist||title"

# During sync, check before enqueuing:
if not await queue_manager.is_duplicate(playlist_id, artist, title):
    await queue_manager.enqueue_download(task)
    await queue_manager.mark_processed(playlist_id, artist, title)
```

**Benefits:**
- Survives service restarts
- No duplicate downloads if file moves
- Force-resync available (clear set per playlist)

### 3. **Auto-Tagging Pipeline**

When download completes, tagger automatically runs:
```python
# In downloader_service.py:
await queue_manager.mark_downloaded(track_id, file_path)
  # ↓ Internally calls:
  # await self.enqueue_tagging(tagging_task)
  # Updates state: tagging_status = "queued"

# Tagger picks it up:
await process_tagging_task(queue_manager, task)
  # → Fetch metadata
  # → Apply tags + embed cover
  # → Reorganize file
  # → Update state: tagging_status = "completed"
```

### 4. **Graceful Fallbacks**

Metadata fetching has 4-tier fallback strategy:
```
1. AcoustID fingerprinting (most accurate)
   ↓ (if fails)
2. MusicBrainz text search (artist + title)
   ↓ (if fails)
3. iTunes search (for cover art at least)
   ↓ (if fails)
4. Use provided metadata (artist, title from track info)
```

## Deployment

### 1. Update Docker Compose

Replace old `compose.yaml` with new one:
```bash
cp docker-compose.yaml /path/to/project/docker-compose.yaml
```

### 2. Build Images

```bash
docker compose build
```

This will build:
- `lbdl-api:latest`
- `lbdl-downloader:latest`
- `lbdl-tagger:latest`
- `lbdl-sync:latest`

And pull:
- `redis:7-alpine`
- `rabbitmq:3.13-alpine`

### 3. Start Services

```bash
docker compose up -d
```

This starts all 6 services:
- API (port 8032)
- Downloader (3 workers)
- Tagger (2 workers)
- Sync (cron daemon)
- Redis
- RabbitMQ

### 4. Monitor Logs

```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f lbdl-downloader
docker compose logs -f lbdl-tagger
docker compose logs -f lbdl-sync
```

## Configuration

### Environment Variables

**API Service:**
```env
LBDL_DATA_DIR=/app/music
LBDL_CONFIG_DIR=/app/config
LBDL_AUDIO_FORMAT=opus
LBDL_AUDIO_QUALITY=0
LBDL_SCHEDULER_CRON="0 */2 * * *"
LBDL_ACOUSTID_KEY=<your-key>
LBDL_LB_TOKEN=<your-token>
REDIS_URL=redis://lbdl-redis:6379/0
QUEUE_URL=amqp://guest:guest@lbdl-rabbitmq:5672//
```

**Downloader Service:**
```env
WORKER_CONCURRENCY=3  # Number of parallel downloads
```

**Tagger Service:**
```env
WORKER_CONCURRENCY=2  # Number of parallel tagging operations
LBDL_ACOUSTID_KEY=<your-key>
```

## API Changes (for Frontend/Users)

### New Endpoints

**POST /api/trigger-download**
```json
{
  "artist": "The Beatles",
  "title": "Let It Be",
  "video_id": "xxx"
}

Response: {
  "ok": true,
  "track_id": "job123:artist:title",
  "status": "queued"
}
```

**GET /api/track/{track_id}/state**
```json
{
  "track_id": "job123:artist:title",
  "artist": "The Beatles",
  "title": "Let It Be",
  "download_status": "completed",
  "tagging_status": "processing",
  "file_path": "/app/music/...",
  "last_updated": "2024-03-06T10:30:45.123456"
}
```

**POST /api/track/{track_id}/retag**
```json
{
  "ok": true,
  "queued": true,
  "message": "Retagging enqueued"
}
```

### Modified Endpoints

**GET /api/library/autotag-all**
- No longer blocks
- Returns immediately with job ID
- Use WebSocket to track progress

**POST /api/library/sync** (if exposed)
- Non-blocking
- Returns with enqueued count

## Troubleshooting

### Track stuck in "processing"

1. Check if service is running:
   ```bash
   docker compose ps
   ```

2. Check logs:
   ```bash
   docker compose logs lbdl-downloader
   docker compose logs lbdl-tagger
   ```

3. Reset state (if needed):
   ```bash
   docker compose exec lbdl-redis redis-cli DEL "lbdl:track:*"
   ```

### Duplicate downloads happening

1. This shouldn't happen with new architecture, but if it does:
   ```bash
   docker compose exec lbdl-redis redis-cli
   > KEYS "lbdl:processed_tracks:*"
   > DEL "lbdl:processed_tracks:{playlist_id}"
   ```

### RabbitMQ queue growing

- Normal during bulk operations
- Monitor with:
  ```bash
  docker compose exec lbdl-rabbitmq rabbitmqctl list_queues
  ```

### Redis memory usage

- Auto-expires old tracks after 30 days
- Manual cleanup:
  ```bash
  docker compose exec lbdl-redis redis-cli
  > KEYS "lbdl:track:*"  # See what's stored
  > FLUSHDB              # Nuclear option (clears all)
  ```

## Migration from Monolithic

1. **Backup** your current setup
2. **Update** `docker-compose.yaml`
3. **Copy** new files (shared_queue.py, *_service.py, Dockerfile.*)
4. **Run** `docker compose build && docker compose up -d`
5. **Test** with one playlist first
6. **Monitor** logs for any issues
7. **Old sync.py** is replaced by sync_service.py (async version)

## Performance Expectations

With default concurrency:

| Operation | Time | Notes |
|-----------|------|-------|
| Download | 30-120s | Per track, depends on YouTube Music |
| Tagging | 10-30s | Per track, includes metadata fetch + embed cover |
| Both | ~60-150s | Parallel: 1 download + 1 tag at same time |
| Sync | <5s | Just enqueues, returns immediately |

**Scaling:**
- Increase `WORKER_CONCURRENCY` for more parallelism (CPU/network permitting)
- Each downloader worker: ~100MB RAM
- Each tagger worker: ~150MB RAM

## Future Improvements

- [ ] Webhook notifications on completion
- [ ] Manual retry UI for failed tracks
- [ ] Batch retag with selected candidates
- [ ] Album-level operations (move entire album)
- [ ] Cover art management UI
- [ ] PostgreSQL for audit logs
- [ ] Prometheus metrics export
