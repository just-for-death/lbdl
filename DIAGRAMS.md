# LBDL Microservices - Visual Diagrams

## System Architecture

```
┌────────────────────────────────────────────────────────────────────────────┐
│                         LBDL MICROSERVICES SYSTEM                          │
└────────────────────────────────────────────────────────────────────────────┘

                         ┌──────────────────────┐
                         │   WEB BROWSER        │
                         │ localhost:8032       │
                         └──────────┬───────────┘
                                    │
                     ┌──────────────▼──────────────┐
                     │    lbdl-api (FastAPI)      │
                     │  • Web UI (Vue.js)         │
                     │  • REST API                │
                     │  • Library Scanning        │
                     │  • WebSocket (RT updates)  │
                     └──┬─────────────┬───────────┘
                        │             │
            ┌───────────┘             └──────────────┐
            │                                        │
    ┌───────▼─────────┐              ┌──────────────▼──────┐
    │   REDIS (6379)  │              │ RABBITMQ (5672)    │
    │                 │              │                     │
    │  State Store:   │              │  Queues:            │
    │  • track:*      │              │  • downloads        │
    │  • processed:*  │              │  • tagging          │
    │  • metadata     │              │  • metadata_fetch   │
    │                 │              │                     │
    └────────┬────────┘              └──────────────┬──────┘
             │                                      │
             │      ┌──────────────────────┬───────┘
             │      │                      │
             │      ▼                      ▼
             │   ┌──────────────────────────────────┐
             │   │  lbdl-downloader (3 workers)     │
             │   │                                   │
             │   │  1. Receives DownloadTask         │
             │   │  2. Downloads from YouTube Music │
             │   │  3. Converts to opus/mp3/flac    │
             │   │  4. Updates Redis state          │
             │   │  5. Auto-enqueues tagging task   │
             │   │                                   │
             │   └──────────────┬────────────────────┘
             │                  │ (File downloaded)
             │      ┌───────────┘
             │      │
             │      ▼
             │   ┌────────────────────────────────────┐
             └──▶│  lbdl-tagger (2 workers)           │
                 │                                     │
                 │  1. Receives TaggingTask            │
                 │  2. Fingerprints audio (AcoustID)  │
                 │  3. Fetches metadata:              │
                 │     • MusicBrainz (primary)        │
                 │     • iTunes (fallback)            │
                 │  4. Fetches album art (CAA/iTunes) │
                 │  5. Embeds cover in file           │
                 │  6. Tags with ID3/Vorbis           │
                 │  7. Reorganizes file structure     │
                 │  8. Updates Redis state            │
                 │                                     │
                 └────────┬──────────────────────────┘
                          │ (File tagged & organized)
                          │
                     ┌────▼────────────────────┐
                     │  /app/music/ (storage)  │
                     │                         │
                     │ Artist/Year - Album/    │
                     │  01 - Track Title.opus  │
                     │  02 - Track Title.opus  │
                     │  ...                    │
                     └─────────────────────────┘
```

## Data Flow Sequence

```
USER CLICKS "SYNC" PLAYLIST
│
└─→ lbdl-api receives POST /api/sync
    │
    └─→ Calls sync_service.py
        │
        ├─→ Fetch from ListenBrainz API
        │   └─→ Get playlist metadata + 5 tracks
        │
        ├─→ For each track:
        │   ├─→ Check Redis: is_duplicate()? → NO
        │   ├─→ Create DownloadTask
        │   ├─→ Enqueue to RABBITMQ (DOWNLOAD_QUEUE)
        │   └─→ Mark processed in Redis
        │
        └─→ Return {"status": "ok", "enqueued": 5}
            │
            └─→ User gets response in <2 seconds ✓


MEANWHILE (Parallel Processing)
│
├─→ lbdl-downloader has 3 workers ready
│   ├─→ Worker #1: Pick task 1 → Download (90s)
│   ├─→ Worker #2: Pick task 2 → Download (90s)
│   ├─→ Worker #3: Pick task 3 → Download (90s)
│   ├─→ Worker #1: Task 4 queued...
│   └─→ Worker #2: Task 5 queued...
│
│   When task 1 completes:
│   └─→ downloader.mark_downloaded(track_id, path)
│       ├─→ Updates Redis: download_status = "completed"
│       ├─→ Auto-enqueues TaggingTask
│       └─→ Updates Redis: tagging_status = "queued"
│
└─→ lbdl-tagger has 2 workers ready
    ├─→ Worker #1: Pick tagged task 1 → Tag (20s)
    ├─→ Worker #2: Pick tagged task 2 → Tag (20s)
    └─→ Workers #3-5 queued...
    
    When task 1 tagging completes:
    └─→ tagger.mark_tagged(track_id, new_path, metadata)
        ├─→ Updates Redis: tagging_status = "completed"
        ├─→ Updates Redis: tagged_path = "/app/music/.../..."
        └─→ WebSocket broadcast to UI
```

## Before vs After Timeline

### BEFORE (Monolithic - Sequential)
```
10:00:00 │ Sync started
10:00:05 │ Track 1 download (blocking) ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
10:01:45 │ Track 1 tag (blocking) ▓▓▓
10:01:55 │ Track 2 download (blocking) ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
10:03:35 │ Track 2 tag (blocking) ▓▓▓
10:03:45 │ ... 3 more tracks ...
10:18:00 │ ✓ All done (18 minutes!)
         │ User waited 18 minutes for sync + downloads + tags
```

### AFTER (Microservices - Parallel)
```
10:00:00 │ Sync started
10:00:02 │ Sync returns ✓ (user gets immediate response!)
         │
         │ Downloader:                Tagger:
         │ Track 1 ▓▓▓▓▓▓▓▓▓          Track 1 ▓▓
         │ Track 2 ▓▓▓▓▓▓▓▓▓          Track 2 ▓▓ (starts before #1 done)
         │ Track 3 ▓▓▓▓▓▓▓▓▓          Track 3 ▓▓
         │ Track 4 ▓▓▓▓▓▓▓▓▓          Track 4 ▓▓
         │ Track 5 ▓▓▓▓▓▓▓▓▓          Track 5 ▓▓
         │
10:02:30 │ ✓ All done (2.5 minutes! 86% faster!)
         │ User got response at 10:00:02
         │ All processing happens transparently in background
```

## Duplicate Prevention

```
SCENARIO: Track moves between albums

Timeline:

Sync #1 @ 10:00 (Album A)
│
├─→ Track: "The Beatles - Let It Be"
├─→ Check Redis: is_duplicate("playlist1", "Beatles", "Let It Be")? → NO
├─→ Enqueue download
└─→ Redis: sadd("processed_tracks:playlist1", "beatles||let it be")


Downloader processes...
│
└─→ Downloads to: /app/music/Beatles/1970 - Let It Be/01 - Let It Be.opus


User reorganizes in database...
│
└─→ Moves to: /app/music/Beatles/1962 - Rubber Soul/01 - Let It Be.opus


Sync #2 @ 12:00 (Album B - updated)
│
├─→ Track: "The Beatles - Let It Be"
├─→ Check Redis: is_duplicate("playlist1", "Beatles", "Let It Be")? → YES ✓
├─→ SKIP (don't download again!)
└─→ Log: "✓ exists: Beatles - Let It Be"


RESULT: No duplicate download! ✓

How it works:
• Redis remembers by (artist, title) not by file path
• Even if file moves, Redis knows it was already processed
• Solves the original race condition problem!
```

## State Transitions

```
Track State Machine:

┌──────────────────────┐
│   INITIAL STATE      │
│ (no download yet)    │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐       ┌──────────────┐
│ DOWNLOAD_QUEUED      ├──────▶│ DOWNLOAD_    │
│ (in RabbitMQ queue)  │       │ PROCESSING   │
└──────────────────────┘       │ (downloading)│
                               └──────┬───────┘
                                      │
                    ┌─────────────────┼──────────────────┐
                    │                 │                  │
                    ▼                 ▼                  ▼
            ┌──────────────┐  ┌───────────────┐  ┌────────────────┐
            │ DOWNLOAD_    │  │ DOWNLOAD_     │  │ TAGGING_QUEUED │
            │ COMPLETED    │  │ FAILED        │  │ (auto-enqueue) │
            │ file_path:✓  │  │ error_msg:✓   │  │ (in RabbitMQ)  │
            └──────┬───────┘  └───────────────┘  └────────┬───────┘
                   │                                       │
                   │                          ┌────────────┘
                   │                          │
                   ▼                          ▼
            (next phase)            ┌──────────────────┐
                                    │ TAGGING_          │
                                    │ PROCESSING        │
                                    │ (fetching tags)   │
                                    └────────┬──────────┘
                                             │
                          ┌──────────────────┼────────────────┐
                          │                  │                │
                          ▼                  ▼                ▼
                  ┌────────────────┐ ┌──────────────┐ ┌──────────┐
                  │ TAGGING_       │ │ TAGGING_     │ │ FINAL    │
                  │ COMPLETED      │ │ FAILED       │ │ STATE    │
                  │ tagged_path:✓  │ │ error_msg:✓  │ │ (ready)  │
                  │ metadata:✓     │ └──────────────┘ └──────────┘
                  └────────────────┘
                        │
                        └─→ Visible in library ✓

All state persisted in Redis:
key: "lbdl:track:{track_id}"
value: JSON with all fields + timestamps
```

## Message Queue Structure

```
RABBITMQ QUEUES:

┌─────────────────────────────────┐
│ lbdl.downloads (DOWNLOAD_QUEUE) │
├─────────────────────────────────┤
│ Message format:                 │
│ {                               │
│   "track_id": "abc:Beatles:Let" │
│   "playlist_id": "abc...",      │
│   "artist": "The Beatles",      │
│   "title": "Let It Be",         │
│   "video_id": "xyz123",         │
│   "timestamp": "2024-03-06..."  │
│ }                               │
│                                 │
│ Durable: YES (survives restart) │
│ Workers: 3 (configurable)       │
└─────────────────────────────────┘

        Messages flow from API/sync to downloader

┌─────────────────────────────────┐
│ lbdl.tagging (TAGGING_QUEUE)    │
├─────────────────────────────────┤
│ Message format:                 │
│ {                               │
│   "track_id": "abc:Beatles:Let" │
│   "file_path": "/app/music/..", │
│   "artist": "The Beatles",      │
│   "title": "Let It Be",         │
│   "timestamp": "2024-03-06..."  │
│ }                               │
│                                 │
│ Durable: YES (survives restart) │
│ Workers: 2 (configurable)       │
│ Auto-enqueued by downloader     │
└─────────────────────────────────┘

        Messages flow from downloader to tagger
```

## Concurrency Model

```
DOWNLOADER SERVICE (3 workers by default):

Worker 1: ▓▓▓▓▓▓▓▓▓▓▓ Track A (downloading)
Worker 2: ▓▓▓▓▓▓▓▓▓   Track B (downloading) 
Worker 3: ▓▓▓▓▓▓▓▓    Track C (downloading)

         Queue:  [D] [E] [F] ...  (waiting)

Result: 3 tracks downloading simultaneously
Speed: ~3x faster than sequential


TAGGER SERVICE (2 workers by default):

Worker 1: ▓▓▓ Track A (tagging)
Worker 2: ▓▓▓ Track B (tagging)

         Queue:  [C] [D] [E] ...  (waiting)

Result: 2 tracks tagging simultaneously
Speed: ~2x faster than sequential


COMBINED EFFECT:

Download Queue:           Tag Queue:
[A][B][C][D][E]...    [A'][B'][C'][D'][E']...
  ↓  ↓  ↓                 ↓  ↓  ↓
 DL1 DL2 DL3            TG1 TG2
 (3 in progress)        (2 in progress)

Result: Downloads don't block tagging, and vice versa!
```

## Error Recovery

```
FAILURE SCENARIO:

Track A starts downloading...
│
├─→ [Network error after 5 minutes]
│   │
│   └─→ downloader.mark_downloaded(track_a, "", error="Network timeout")
│       ├─→ Redis: download_status = "FAILED"
│       ├─→ Redis: download_error = "Network timeout"
│       └─→ NO tagging enqueued
│
├─→ Worker moves to next task (Track B)
│
└─→ Track A remains in Redis with error for:
    • Manual retry via UI
    • Review by admin
    • Potential retry logic (future feature)


RECOVERY OPTIONS:

1. Manual retry:
   POST /api/retry-download/{track_id}
   → Enqueue DownloadTask again
   
2. Check error:
   GET /api/track/{track_id}/state
   → Returns: {"download_status": "FAILED", "download_error": "..."}

3. Clear and resync:
   DELETE /api/processed/{playlist_id}
   → Clear processed_tracks set
   → Next sync will try again

Key: Work queue is durable, so nothing is lost
```

---

## Summary Diagrams

### Success Path (Happy Flow)
```
User Sync
  └─→ Enqueue tasks
      └─→ Downloader processes (3 parallel)
          └─→ Auto-enqueue tagging
              └─→ Tagger processes (2 parallel)
                  └─→ Update Redis
                      └─→ WebSocket broadcast
                          └─→ UI updates
```

### State Persistence
```
Downloads        ↘                     Tagging
              Redis Store            /
        (single source of truth)
              ↙                      ↘
        Tagger respects              Download respects
      what Downloader did          what Tagger did
```

### Scaling
```
User load ↑        More downloads?      More tagging?
          │        Increase workers     Increase workers
          │        docker-compose.yaml docker-compose.yaml
          │        WORKER_CONCURRENCY   WORKER_CONCURRENCY
          └─→      docker compose up -d
                   (non-blocking restart)
```

---

This architecture is designed for **reliability, scalability, and clarity**. Each service has a single responsibility, making debugging and monitoring straightforward.
