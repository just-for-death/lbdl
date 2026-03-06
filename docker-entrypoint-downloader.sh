#!/bin/bash
set -e

echo "[Downloader] Starting lbdl downloader service..."
echo "[Downloader] Concurrency: ${WORKER_CONCURRENCY:-3}"
exec python downloader_service.py
