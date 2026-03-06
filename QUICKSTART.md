# Quick Start Guide - LBDL Microservices

## What Changed?

✅ **Download and Tagging are now separate services**
✅ **No more race conditions** (track doesn't get re-downloaded if moved)
✅ **3 parallel downloads + 2 parallel tagging** (configurable)
✅ **Sync is instant** (enqueues and returns immediately)
✅ **Album fetcher integrated** into tagger service
✅ **Full state tracking** via Redis

## Prerequisites

- Docker & Docker Compose
- Same volumes as before
- Optional: AcoustID API key (for fingerprinting)
- Optional: ListenBrainz token (for playlist syncing)

## Installation

### 1. Backup Current Setup (Optional)

```bash
# If you're upgrading from the old single-container version
cd /path/to/lbdl
docker compose down
# Keep your music and config directories!
```

### 2. Update Files

Copy these files to your project directory:

```
docker-compose.yaml           ← New file (replaces old compose)
Dockerfile.api                ← New (split from old Dockerfile)
Dockerfile.downloader         ← New
Dockerfile.tagger             ← New
Dockerfile.sync               ← New
shared_queue.py               ← New (message queue layer)
downloader_service.py         ← New (downloader worker)
tagger_service.py             ← New (tagger worker)
sync_service.py               ← New (async sync)
docker-entrypoint-api.sh      ← New
docker-entrypoint-downloader.sh ← New
docker-entrypoint-tagger.sh   ← New
docker-entrypoint-sync.sh     ← New
requirements-updated.txt      ← Rename to requirements.txt
```

Keep your existing:
```
app/                          ← No changes needed!
static/                       ← No changes needed!
```

### 3. Update requirements.txt

```bash
cp requirements-updated.txt requirements.txt
```

The new additions are:
- `aio-pika>=13.0.0` (RabbitMQ client)
- `redis>=5.0.0` (Redis client)

### 4. Build Images

```bash
docker compose build
```

This will:
- Build lbdl-api (API server)
- Build lbdl-downloader (3 workers)
- Build lbdl-tagger (2 workers)
- Build lbdl-sync (cron service)
- Pull redis:7-alpine
- Pull rabbitmq:3.13-alpine

### 5. Update Compose File (IMPORTANT!)

Edit `docker-compose.yaml` and update the volume paths:

```yaml
services:
  lbdl-api:
    volumes:
      - /mnt/Nazi/lbdl/music:/app/music      # ← Change this path
      - /mnt/Nazi/lbdl/config:/app/config    # ← And this
```

Replace `/mnt/Nazi/lbdl` with your actual path.

Do the same for:
- `lbdl-downloader`
- `lbdl-tagger`
- `lbdl-sync`

### 6. Start Services

```bash
# Start all 6 services in background
docker compose up -d

# Check they're running
docker compose ps

# Should show:
# NAME                    STATUS
# lbdl-api               Up (healthy)
# lbdl-downloader        Up
# lbdl-tagger            Up
# lbdl-sync              Up
# lbdl-redis             Up (healthy)
# lbdl-rabbitmq          Up (healthy)
```

### 7. Open Web UI

Navigate to: **http://localhost:8032**

You should see the library as before.

## First Run - What to Expect

### 1. View Library

- Browse your existing music library
- Everything works as before

### 2. Add a Playlist

1. Click "Add Playlist"
2. Paste ListenBrainz playlist URL
3. Click "Add"

### 3. Trigger Sync

1. Click "Sync All" in the UI
2. **Sync returns immediately** ✨
3. Check the queue:
   ```bash
   docker compose logs -f lbdl-downloader
   ```

### 4. Watch Downloads

```bash
docker compose logs -f lbdl-downloader
# Output:
# Processing download: The Beatles - Let It Be (video_id=xyz)
# ✓ Downloaded: The Beatles - Let It Be → /app/music/.../file.opus
```

### 5. Watch Tagging

```bash
docker compose logs -f lbdl-tagger
# Output:
# [track_id] Attempting AcoustID fingerprint
# [track_id] Found via AcoustID: The Beatles - Let It Be
# [track_id] ✓ Tagged and organized: /app/music/The Beatles/1970 - Let It Be/01 Let It Be.opus
```

### 6. Check Library

- Library auto-scans when files are moved
- New tracks appear in "Recently Added" section

## Configuration

### Adjust Download Concurrency

In `docker-compose.yaml`, change:

```yaml
lbdl-downloader:
  environment:
    WORKER_CONCURRENCY: "5"  # Default: 3
```

Then restart:
```bash
docker compose up -d lbdl-downloader
```

### Adjust Tagging Concurrency

```yaml
lbdl-tagger:
  environment:
    WORKER_CONCURRENCY: "4"  # Default: 2
```

### Set AcoustID API Key

1. Get key from: https://acoustid.org/
2. Add to `docker-compose.yaml`:

```yaml
lbdl-api:
  environment:
    LBDL_ACOUSTID_KEY: "your_key_here"

lbdl-tagger:
  environment:
    LBDL_ACOUSTID_KEY: "your_key_here"
```

Then restart:
```bash
docker compose up -d
```

### Change Sync Schedule

Default is every 2 hours (`0 */2 * * *`). To sync every 4 hours:

```yaml
lbdl-api:
  environment:
    LBDL_SCHEDULER_CRON: "0 */4 * * *"
```

Cron format: `minute hour day month weekday`

Common examples:
- `0 */6 * * *` → Every 6 hours
- `0 3 * * *` → Daily at 3 AM
- `*/30 * * * *` → Every 30 minutes

## Monitoring

### Check Service Status

```bash
docker compose ps
```

### View Logs

```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f lbdl-downloader
docker compose logs -f lbdl-tagger
docker compose logs -f lbdl-api

# Last 100 lines
docker compose logs --tail=100 lbdl-tagger
```

### Check Queue Depth

```bash
# RabbitMQ queue sizes
docker compose exec lbdl-rabbitmq rabbitmqctl list_queues
```

### Check Redis State

```bash
# List all track states
docker compose exec lbdl-redis redis-cli
> KEYS "lbdl:track:*"
> GET "lbdl:track:{track_id}"  # See full JSON
> DBSIZE                        # Total keys
```

## Troubleshooting

### Service won't start

Check logs:
```bash
docker compose logs lbdl-api
docker compose logs lbdl-downloader
docker compose logs lbdl-tagger
```

Common issues:
- Port 8032 already in use → Change in docker-compose.yaml
- Volume paths wrong → Update paths in compose file
- Missing dependencies → `docker compose build` again

### Downloader not running

```bash
# Is RabbitMQ healthy?
docker compose logs lbdl-rabbitmq

# Is Redis healthy?
docker compose exec lbdl-redis ping
# Should return: PONG

# Check downloader logs
docker compose logs lbdl-downloader
```

### Stuck in "processing" state

The track is probably hanging. Kill and restart:

```bash
docker compose restart lbdl-downloader lbdl-tagger
```

Or reset Redis (dangerous!):
```bash
docker compose exec lbdl-redis redis-cli FLUSHDB
```

### Out of memory

Reduce concurrency:
```yaml
lbdl-downloader:
  environment:
    WORKER_CONCURRENCY: "1"
```

Or check what's taking space:
```bash
docker compose exec lbdl-redis redis-cli
> INFO memory
> KEYS "lbdl:*" | wc -l  # Count of keys
```

## Next Steps

1. ✅ Add your playlists
2. ✅ Configure AcoustID key (optional but recommended)
3. ✅ Set up automatic sync schedule
4. ✅ Monitor first few syncs in logs
5. ✅ Adjust concurrency based on your hardware

## Getting Help

If something breaks:

1. **Check logs first:**
   ```bash
   docker compose logs lbdl-downloader
   docker compose logs lbdl-tagger
   docker compose logs lbdl-api
   ```

2. **Check service health:**
   ```bash
   docker compose ps
   docker compose exec lbdl-redis ping
   docker compose exec lbdl-rabbitmq rabbitmqctl status
   ```

3. **Hard reset (as last resort):**
   ```bash
   docker compose down
   docker compose up -d --force-recreate
   ```

## Architecture Diagram

```
User Opens http://localhost:8032
              ↓
         lbdl-api (8032)
              ↓
    ┌─────────┼─────────┐
    ↓         ↓         ↓
 Redis    RabbitMQ   Downloader
                     ↓
                   Tagger
                     ↓
              /app/music (output)
```

Data flow:
1. UI sends "Sync" → API enqueues to RabbitMQ
2. Downloader picks up → Downloads audio
3. Downloader updates Redis → Auto-enqueues to tagger
4. Tagger picks up → Tags + reorganizes
5. Tagger updates Redis
6. API sees Redis change → WebSocket to UI
7. UI refreshes with new track

## Performance Tips

- **More downloads?** Increase `WORKER_CONCURRENCY` for downloader
- **Faster metadata?** Set AcoustID API key
- **Better tags?** Increase tagger concurrency
- **Faster syncing?** Adjust cron schedule or run manual sync

Each downloader uses ~100MB RAM, each tagger uses ~150MB.

For a typical 4GB system:
- Downloader: 3-4 workers
- Tagger: 2-3 workers
- Total: ~1GB usage

---

**Happy tagging!** 🎵
