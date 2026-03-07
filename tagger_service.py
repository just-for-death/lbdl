"""
tagger_service.py — Worker service for tagging downloaded tracks.

This service:
1. Listens on TAGGING_QUEUE for tagging tasks
2. Fetches metadata (AcoustID, MusicBrainz, iTunes, album art)
3. Applies tags and reorganizes files
4. Updates track state via shared_queue

Features:
- Concurrent metadata fetching
- Album art fetching (art, playlist covers)
- Automatic file reorganization
- Error recovery and retries

Runs concurrently with up to WORKER_CONCURRENCY tags at a time.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

import aio_pika
from app.library import (
    full_autotag_track, apply_metadata_and_reorganize, read_track_meta,
)
from shared_queue import (
    QueueManager,
    TaggingTask,
    close_queue_manager,
    get_queue_manager,
)

logger = logging.getLogger("lbdl.tagger")

# ── Configuration ──────────────────────────────────────────────────────────────

OUTPUT_DIR = Path(os.getenv("LBDL_DATA_DIR", "/app/music"))
WORKER_CONCURRENCY = int(os.getenv("WORKER_CONCURRENCY", "2"))
# Env var is a fallback; settings.json (written by the UI) takes priority at task time
_ENV_ACOUSTID_KEY = os.getenv("LBDL_ACOUSTID_KEY", "")
CONFIG_DIR = Path(os.getenv("LBDL_CONFIG_DIR", "/app/config"))


def _get_acoustid_key() -> str:
    """Read AcoustID key: prefer settings.json, fall back to env var."""
    settings_path = CONFIG_DIR / "settings.json"
    try:
        if settings_path.exists():
            import json as _json
            with open(settings_path) as f:
                s = _json.load(f)
            key = s.get("acoustid_key", "").strip()
            if key:
                return key
    except Exception:
        pass
    return _ENV_ACOUSTID_KEY


# ── Metadata Fetcher (runs in thread pool) ──────────────────────────────────────

def fetch_metadata(file_path: str, task: TaggingTask) -> tuple[dict | None, bytes | None, list[str]]:
    """
    Fetch metadata for a track using the full pipeline (iTunes → MusicBrainz → AcoustID).

    Uses full_autotag_track which tries all sources in priority order and fetches
    lyrics when enabled — identical pipeline to the API's inline autotag path.

    Returns: (metadata_dict, cover_bytes, log_lines) or (None, None, logs) on failure
    """
    import json as _json
    try:
        settings_path = CONFIG_DIR / "settings.json"
        acoustid_key = _get_acoustid_key()
        do_lyrics = True
        try:
            if settings_path.exists():
                with open(settings_path) as f:
                    s = _json.load(f)
                do_lyrics = bool(s.get("fetch_lyrics", True))
        except Exception:
            pass

        seed_meta = {
            "artist": task.artist,
            "title":  task.title,
            "album":  task.album,
            "year":   task.year,
        }
        new_meta, cover, logs = full_autotag_track(
            file_path, seed_meta,
            acoustid_key=acoustid_key,
            fetch_lyrics=do_lyrics,
        )
        if new_meta:
            logger.info("[%s] Tagged: %s — %s", task.track_id, new_meta.get("artist"), new_meta.get("title"))
            return new_meta, cover, logs

        # full_autotag_track returned nothing — fall back to task metadata
        logger.warning("[%s] No metadata found via full pipeline, using task info", task.track_id)
        return {
            "artist":      task.artist,
            "title":       task.title,
            "album":       task.album,
            "albumartist": task.album_artist,
            "year":        task.year,
        }, None, logs

    except Exception as e:
        logger.error("[%s] Metadata fetch error: %s", task.track_id, e, exc_info=True)
        return None, None, [f"Exception: {e}"]


# ── Tagging Worker ────────────────────────────────────────────────────────────

async def process_tagging_task(
    queue_manager: QueueManager, task: TaggingTask
) -> None:
    """Process a single tagging task."""
    logger.info("[%s] Processing tagging: %s - %s", task.track_id, task.artist, task.title)

    file_path = Path(task.file_path)
    if not file_path.exists():
        error_msg = f"File not found: {task.file_path}"
        logger.error("[%s] %s", task.track_id, error_msg)
        await queue_manager.mark_tagged(task.track_id, "", {}, error=error_msg)
        return

    try:
        # Fetch metadata in thread pool (blocking operations)
        loop = asyncio.get_running_loop()
        metadata, cover, tag_logs = await loop.run_in_executor(
            None, fetch_metadata, str(file_path), task
        )
        for line in (tag_logs or []):
            logger.debug("[%s] %s", task.track_id, line)

        if not metadata:
            error_msg = "Failed to fetch metadata"
            logger.error("[%s] %s", task.track_id, error_msg)
            await queue_manager.mark_tagged(task.track_id, str(file_path), {}, error=error_msg)
            return

        # Apply metadata and reorganize file
        logger.debug("[%s] Applying metadata and reorganizing", task.track_id)
        new_path = await loop.run_in_executor(
            None,
            apply_metadata_and_reorganize,
            file_path,
            metadata,
            cover,
            OUTPUT_DIR,
        )

        # Re-read to confirm what was written
        final_meta = await loop.run_in_executor(None, read_track_meta, new_path)
        if not final_meta:
            final_meta = metadata

        logger.info("[%s] ✓ Tagged and organized: %s", task.track_id, new_path)
        await queue_manager.mark_tagged(task.track_id, str(new_path), final_meta)

    except Exception as e:
        logger.error("[%s] Exception during tagging: %s", task.track_id, e, exc_info=True)
        await queue_manager.mark_tagged(
            task.track_id, str(file_path), {}, error=f"Exception: {str(e)}"
        )


async def tagging_worker(queue_manager: QueueManager, tagging_queue: aio_pika.Queue) -> None:
    """Main worker loop - processes messages from tagging queue."""
    
    async with tagging_queue.iterator() as queue_iter:
        logger.info("Tagging worker started")
        async for message in queue_iter:
            async with message.process():
                try:
                    task = TaggingTask.from_json(message.body.decode())
                    await process_tagging_task(queue_manager, task)
                except Exception as e:
                    logger.error("Failed to process tagging message: %s", e, exc_info=True)


async def run_tagger() -> None:
    """Start the tagger service."""
    logger.info("=== Tagger Service Starting ===")
    logger.info("Concurrency: %d workers", WORKER_CONCURRENCY)
    logger.info("AcoustID Key: %s (reads settings.json at task time)", "env-configured" if _ENV_ACOUSTID_KEY else "not set in env")

    try:
        queue_manager = await get_queue_manager()
        
        # Start worker tasks
        workers = [
            asyncio.create_task(
                tagging_worker(queue_manager, queue_manager.tagging_queue)
            )
            for _ in range(WORKER_CONCURRENCY)
        ]

        logger.info("Starting %d tagging workers", WORKER_CONCURRENCY)
        await asyncio.gather(*workers)

    except KeyboardInterrupt:
        logger.info("Received interrupt signal")
    except Exception as e:
        logger.error("Fatal error: %s", e, exc_info=True)
        sys.exit(1)
    finally:
        await close_queue_manager()
        logger.info("=== Tagger Service Stopped ===")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    asyncio.run(run_tagger())
