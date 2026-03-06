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
    autotag_track, apply_metadata_and_reorganize, read_track_meta,
    _gate_candidate,
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

def fetch_metadata(file_path: str, task: TaggingTask) -> tuple[dict | None, bytes | None]:
    """
    Fetch metadata for a track.
    
    Returns: (metadata_dict, cover_bytes) or (None, None) on failure
    """
    from app.library import (
        _mb_text_search,
        _parse_mb_rec,
        _cover_url_from_caa,
        _fetch_cover_from_url,
        _itunes_candidates,
    )

    try:
        # 1. Try AcoustID fingerprinting first (most reliable)
        acoustid_key = _get_acoustid_key()
        logger.debug("[%s] Attempting AcoustID fingerprint (key=%s)", task.track_id, "set" if acoustid_key else "missing")
        metadata, cover = autotag_track(file_path, acoustid_key=acoustid_key)
        if metadata:
            # AcoustID found metadata — use it even if cover is missing
            if not cover:
                # Try iTunes as cover fallback using the confirmed metadata
                logger.debug("[%s] AcoustID found metadata but no cover — trying iTunes cover", task.track_id)
                itunes = _itunes_candidates(
                    metadata.get("artist", task.artist),
                    metadata.get("title", task.title),
                    limit=1,
                )
                if itunes and itunes[0].get("cover_url"):
                    cover = _fetch_cover_from_url(itunes[0]["cover_url"])
            logger.info("[%s] Found via AcoustID: %s - %s", task.track_id, metadata.get("artist"), metadata.get("title"))
            return metadata, cover

        # 2. AcoustID returned nothing — fall back to MusicBrainz text search
        logger.debug("[%s] AcoustID found nothing, trying MusicBrainz text search", task.track_id)
        search_artist = task.artist
        search_title  = task.title
        
        mb_recs = _mb_text_search(search_artist, search_title, limit=6)
        if mb_recs:
            rec = mb_recs[0]
            parsed = _parse_mb_rec(rec, source="musicbrainz")
            if parsed:
                accepted, gate_reason = _gate_candidate(search_artist, search_title, parsed, label="MB/tagger")
                if accepted:
                    logger.info("[%s] Found via MusicBrainz [%s]: %s", task.track_id, gate_reason, parsed.get("title"))
                    cover = None
                    if parsed.get("mb_rel_id"):
                        caa_url = _cover_url_from_caa(parsed["mb_rel_id"])
                        cover = _fetch_cover_from_url(caa_url)
                    return parsed, cover
                else:
                    logger.info("[%s] MusicBrainz rejected [%s]: %s — %s", task.track_id, gate_reason, parsed.get("artist"), parsed.get("title"))

        # 3. Final fallback: iTunes search for cover art
        logger.debug("[%s] Trying iTunes for metadata", task.track_id)
        itunes = _itunes_candidates(search_artist, search_title, limit=1)
        if itunes:
            candidate = itunes[0]
            accepted, gate_reason = _gate_candidate(search_artist, search_title, candidate, label="iTunes/tagger")
            if accepted:
                cover = None
                if candidate.get("cover_url"):
                    cover = _fetch_cover_from_url(candidate["cover_url"])
                metadata = {
                    "artist": candidate.get("artist", search_artist),
                    "title": candidate.get("title", search_title),
                    "album": candidate.get("album", ""),
                    "year": candidate.get("year", ""),
                    "albumartist": candidate.get("artist", search_artist),
                }
                logger.info("[%s] Found via iTunes [%s]: %s", task.track_id, gate_reason, metadata.get("title"))
                return metadata, cover
            else:
                logger.info("[%s] iTunes rejected [%s]: %s — %s", task.track_id, gate_reason, candidate.get("artist"), candidate.get("title"))

        # If all fails, return what we have
        logger.warning("[%s] No metadata found, using provided info", task.track_id)
        return {
            "artist": task.artist,
            "title": task.title,
            "album": task.album,
            "album_artist": task.album_artist,
            "year": task.year,
        }, None

    except Exception as e:
        logger.error("[%s] Metadata fetch error: %s", task.track_id, e, exc_info=True)
        return None, None


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
        metadata, cover = await loop.run_in_executor(
            None, fetch_metadata, str(file_path), task
        )

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
