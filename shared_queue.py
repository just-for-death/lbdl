"""
shared_queue.py — Message queue layer for coordinating downloader and tagger services.

This module provides:
- Download queue: tasks for the downloader service
- Tagging queue: tasks for the tagger service
- State synchronization via Redis
- Event broadcasting for UI updates

Flow:
1. Download task created → stored in DOWNLOAD_QUEUE
2. Downloader processes → updates track state in Redis
3. Once downloaded → TAGGING_QUEUE task created
4. Tagger processes → tags metadata, reorganizes files
5. Completion broadcast to all subscribers
"""

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

import aio_pika
import redis.asyncio as aioredis

logger = logging.getLogger("lbdl.queue")

# ── Configuration ──────────────────────────────────────────────────────────────

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
QUEUE_URL = os.getenv("QUEUE_URL", "amqp://guest:guest@localhost:5672//")

# Queue names
DOWNLOAD_QUEUE = "lbdl.downloads"
TAGGING_QUEUE = "lbdl.tagging"
METADATA_QUEUE = "lbdl.metadata_fetch"

# Redis keys
DOWNLOAD_STATE_PREFIX = "lbdl:download:"      # download:{track_id} → JSON state
TAGGING_STATE_PREFIX = "lbdl:tagging:"        # tagging:{track_id} → JSON state
TRACK_STATE_PREFIX = "lbdl:track:"            # track:{track_id} → combined state
PROCESSED_TRACKS = "lbdl:processed_tracks"    # set of (playlist_id, artist, title) already handled


# ── Task Models ────────────────────────────────────────────────────────────────

class TaskStatus(str, Enum):
    """Status of a task in the pipeline."""
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class DownloadTask:
    """Task for the downloader service."""
    track_id: str                    # Unique ID for this track in the job
    playlist_id: str                 # Playlist being synced
    artist: str
    title: str
    video_id: str                    # YouTube Music video ID
    job_id: str | None = None        # Job ID from UI (optional, for UI-driven downloads)
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> "DownloadTask":
        return cls(**json.loads(data))


@dataclass
class TaggingTask:
    """Task for the tagger service."""
    track_id: str                    # Should match file ID in library_index
    file_path: str                   # Path to downloaded audio file
    artist: str
    title: str
    album: str = ""
    album_artist: str = ""
    year: str = ""
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> "TaggingTask":
        return cls(**json.loads(data))


@dataclass
class TrackState:
    """Combined state of a track through the entire pipeline."""
    track_id: str
    artist: str
    title: str
    
    # Download stage
    download_status: TaskStatus = TaskStatus.QUEUED
    download_error: str | None = None
    file_path: str | None = None
    download_timestamp: str | None = None
    
    # Tagging stage
    tagging_status: TaskStatus = TaskStatus.QUEUED
    tagging_error: str | None = None
    tagged_path: str | None = None  # Path after reorganization
    tagging_timestamp: str | None = None
    
    # Metadata
    metadata: dict = field(default_factory=dict)
    last_updated: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> "TrackState":
        return cls(**json.loads(data))


# ── Global Connection Pool ─────────────────────────────────────────────────────

class QueueManager:
    """Manages connections to Redis and RabbitMQ."""
    
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self.mq_connection: aio_pika.Connection | None = None
        self.channel: aio_pika.Channel | None = None
        self.download_queue: aio_pika.Queue | None = None
        self.tagging_queue: aio_pika.Queue | None = None
        self.metadata_queue: aio_pika.Queue | None = None

    async def connect(self) -> None:
        """Initialize connections to Redis and RabbitMQ."""
        try:
            self.redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
            logger.info("Connected to Redis at %s", REDIS_URL)
        except Exception as e:
            logger.error("Failed to connect to Redis: %s", e)
            raise

        try:
            self.mq_connection = await aio_pika.connect_robust(QUEUE_URL)
            self.channel = await self.mq_connection.channel()
            logger.info("Connected to RabbitMQ at %s", QUEUE_URL)
        except Exception as e:
            logger.error("Failed to connect to RabbitMQ: %s", e)
            raise

        # Declare queues (idempotent if already exist)
        self.download_queue = await self.channel.declare_queue(
            DOWNLOAD_QUEUE, durable=True
        )
        self.tagging_queue = await self.channel.declare_queue(
            TAGGING_QUEUE, durable=True
        )
        self.metadata_queue = await self.channel.declare_queue(
            METADATA_QUEUE, durable=True
        )
        logger.info("Declared queues: %s, %s, %s", DOWNLOAD_QUEUE, TAGGING_QUEUE, METADATA_QUEUE)

    async def disconnect(self) -> None:
        """Close connections."""
        if self.mq_connection:
            await self.mq_connection.close()
        if self.redis:
            await self.redis.close()
        logger.info("Disconnected from queue services")

    async def enqueue_download(self, task: DownloadTask) -> None:
        """Add a download task to the queue."""
        if not self.download_queue:
            raise RuntimeError("Queue manager not connected")
        
        message = aio_pika.Message(
            body=task.to_json().encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        )
        await self.channel.default_exchange.publish(message, routing_key=DOWNLOAD_QUEUE)
        
        # Store initial state
        await self.set_track_state(
            task.track_id,
            TrackState(
                track_id=task.track_id,
                artist=task.artist,
                title=task.title,
                download_status=TaskStatus.QUEUED,
            ),
        )
        logger.debug("Enqueued download: %s - %s", task.artist, task.title)

    async def enqueue_tagging(self, task: TaggingTask) -> None:
        """Add a tagging task to the queue."""
        if not self.tagging_queue:
            raise RuntimeError("Queue manager not connected")
        
        message = aio_pika.Message(
            body=task.to_json().encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        )
        await self.channel.default_exchange.publish(message, routing_key=TAGGING_QUEUE)
        logger.debug("Enqueued tagging: %s", task.track_id)

    async def enqueue_metadata_fetch(self, task: TaggingTask) -> None:
        """Add a metadata fetch task (used within tagger)."""
        if not self.metadata_queue:
            raise RuntimeError("Queue manager not connected")
        
        message = aio_pika.Message(
            body=task.to_json().encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        )
        await self.channel.default_exchange.publish(message, routing_key=METADATA_QUEUE)
        logger.debug("Enqueued metadata fetch: %s", task.track_id)

    # ── State Management ───────────────────────────────────────────────────────

    async def get_track_state(self, track_id: str) -> TrackState | None:
        """Retrieve combined state of a track."""
        if not self.redis:
            raise RuntimeError("Queue manager not connected")
        
        data = await self.redis.get(f"{TRACK_STATE_PREFIX}{track_id}")
        if data:
            return TrackState.from_json(data)
        return None

    async def set_track_state(self, track_id: str, state: TrackState) -> None:
        """Store combined state of a track."""
        if not self.redis:
            raise RuntimeError("Queue manager not connected")
        
        state.last_updated = datetime.utcnow().isoformat()
        await self.redis.set(
            f"{TRACK_STATE_PREFIX}{track_id}",
            state.to_json(),
            ex=86400 * 30,  # Expire after 30 days
        )

    async def mark_downloaded(
        self, track_id: str, file_path: str, error: str | None = None
    ) -> None:
        """Mark a track as downloaded and auto-enqueue for tagging."""
        if not self.redis:
            raise RuntimeError("Queue manager not connected")
        
        state = await self.get_track_state(track_id)
        if not state:
            logger.warning("No state found for track %s", track_id)
            return

        if error:
            state.download_status = TaskStatus.FAILED
            state.download_error = error
            logger.error("Download failed for %s: %s", track_id, error)
        else:
            state.download_status = TaskStatus.COMPLETED
            state.file_path = file_path
            state.download_timestamp = datetime.utcnow().isoformat()
            logger.info("Download completed for %s: %s", track_id, file_path)

            # Auto-enqueue for tagging
            tagging_task = TaggingTask(
                track_id=track_id,
                file_path=file_path,
                artist=state.artist,
                title=state.title,
            )
            state.tagging_status = TaskStatus.QUEUED
            await self.enqueue_tagging(tagging_task)

        await self.set_track_state(track_id, state)

    async def mark_tagged(
        self, track_id: str, tagged_path: str, metadata: dict, error: str | None = None
    ) -> None:
        """Mark a track as tagged and reorganized."""
        if not self.redis:
            raise RuntimeError("Queue manager not connected")
        
        state = await self.get_track_state(track_id)
        if not state:
            logger.warning("No state found for track %s", track_id)
            return

        if error:
            state.tagging_status = TaskStatus.FAILED
            state.tagging_error = error
            logger.error("Tagging failed for %s: %s", track_id, error)
        else:
            state.tagging_status = TaskStatus.COMPLETED
            state.tagged_path = tagged_path
            state.metadata = metadata
            state.tagging_timestamp = datetime.utcnow().isoformat()
            logger.info("Tagging completed for %s: %s", track_id, tagged_path)

        await self.set_track_state(track_id, state)

    async def is_duplicate(
        self, playlist_id: str, artist: str, title: str
    ) -> bool:
        """Check if a track (artist+title) was already processed in this playlist."""
        if not self.redis:
            raise RuntimeError("Queue manager not connected")
        
        key = f"{PROCESSED_TRACKS}:{playlist_id}"
        track_key = f"{artist}||{title}".lower()
        exists = await self.redis.sismember(key, track_key)
        return bool(exists)

    async def mark_processed(
        self, playlist_id: str, artist: str, title: str
    ) -> None:
        """Mark a track as processed to prevent re-downloads."""
        if not self.redis:
            raise RuntimeError("Queue manager not connected")
        
        key = f"{PROCESSED_TRACKS}:{playlist_id}"
        track_key = f"{artist}||{title}".lower()
        await self.redis.sadd(key, track_key)
        # Expire the set after 90 days
        await self.redis.expire(key, 86400 * 90)

    async def clear_processed(self, playlist_id: str) -> None:
        """Clear processed tracks for a playlist (useful for force-resync)."""
        if not self.redis:
            raise RuntimeError("Queue manager not connected")
        
        key = f"{PROCESSED_TRACKS}:{playlist_id}"
        await self.redis.delete(key)


# ── Singleton Instance ─────────────────────────────────────────────────────────

_queue_manager: QueueManager | None = None
_queue_manager_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    """Lazily create the lock inside the running event loop."""
    global _queue_manager_lock
    if _queue_manager_lock is None:
        _queue_manager_lock = asyncio.Lock()
    return _queue_manager_lock


async def get_queue_manager() -> QueueManager:
    """Get or initialize the global queue manager (thread-safe)."""
    global _queue_manager
    async with _get_lock():
        if _queue_manager is None:
            _queue_manager = QueueManager()
            await _queue_manager.connect()
        return _queue_manager


async def close_queue_manager() -> None:
    """Close the global queue manager."""
    global _queue_manager
    if _queue_manager:
        await _queue_manager.disconnect()
        _queue_manager = None
