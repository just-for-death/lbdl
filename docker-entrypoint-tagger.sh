#!/bin/bash
set -e

echo "[Tagger] Starting lbdl tagger service..."
echo "[Tagger] Concurrency: ${WORKER_CONCURRENCY:-2}"
echo "[Tagger] AcoustID Key: ${LBDL_ACOUSTID_KEY:-(not set)}"
exec python tagger_service.py
