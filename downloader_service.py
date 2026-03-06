"""
downloader_service.py — Worker service for downloading tracks.

This service:
1. Listens on DOWNLOAD_QUEUE for download tasks
2. Downloads audio from YouTube Music
3. Updates track state via shared_queue
4. Automatically enqueues tagging tasks

Runs concurrently with up to WORKER_CONCURRENCY downloads at a time.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

import aio_pika
from app.organizer import download_track as dl_track
from shared_queue import (
    DownloadTask,
    QueueManager,
    close_queue_manager,
    get_queue_manager,
)

logger = logging.getLogger("lbdl.downloader")

# ── Configuration ──────────────────────────────────────────────────────────────

OUTPUT_DIR = Path(os.getenv("LBDL_DATA_DIR", "/app/music"))
WORKER_CONCURRENCY = int(os.getenv("WORKER_CONCURRENCY", "3"))


# ── Download Worker ────────────────────────────────────────────────────────────

async def process_download_task(
    queue_manager: QueueManager, task: DownloadTask
) -> None:
    """Process a single download task."""
    logger.info("Processing download: %s - %s (video_id=%s)", task.artist, task.title, task.video_id)

    try:
        # Perform synchronous download in thread pool
        loop = asyncio.get_running_loop()
        ok, final_path, output = await loop.run_in_executor(
            None,
            dl_track,
            task.video_id,
            task.artist,
            task.title,
            lambda msg: logger.debug("  [dl] %s", msg),  # Log callback
        )

        if ok and final_path:
            logger.info("✓ Downloaded: %s → %s", f"{task.artist} - {task.title}", final_path)
            await queue_manager.mark_downloaded(task.track_id, str(final_path))
            # Track is now queued for tagging automatically
        else:
            error_msg = f"Download failed: {output or 'unknown error'}"
            logger.error("✗ Download failed: %s", task.artist)
            await queue_manager.mark_downloaded(task.track_id, "", error=error_msg)

    except Exception as e:
        logger.error("Exception during download: %s", e, exc_info=True)
        await queue_manager.mark_downloaded(
            task.track_id, "", error=f"Exception: {str(e)}"
        )


async def download_worker(queue_manager: QueueManager, download_queue: aio_pika.Queue) -> None:
    """Main worker loop - processes messages from download queue."""
    
    async with download_queue.iterator() as queue_iter:
        logger.info("Download worker started")
        async for message in queue_iter:
            async with message.process():
                try:
                    task = DownloadTask.from_json(message.body.decode())
                    await process_download_task(queue_manager, task)
                except Exception as e:
                    logger.error("Failed to process download message: %s", e, exc_info=True)


async def run_downloader() -> None:
    """Start the downloader service."""
    logger.info("=== Downloader Service Starting ===")
    logger.info("Concurrency: %d workers", WORKER_CONCURRENCY)

    try:
        queue_manager = await get_queue_manager()
        
        # Start worker tasks
        workers = [
            asyncio.create_task(
                download_worker(queue_manager, queue_manager.download_queue)
            )
            for _ in range(WORKER_CONCURRENCY)
        ]

        logger.info("Starting %d download workers", WORKER_CONCURRENCY)
        await asyncio.gather(*workers)

    except KeyboardInterrupt:
        logger.info("Received interrupt signal")
    except Exception as e:
        logger.error("Fatal error: %s", e, exc_info=True)
        sys.exit(1)
    finally:
        await close_queue_manager()
        logger.info("=== Downloader Service Stopped ===")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    asyncio.run(run_downloader())
