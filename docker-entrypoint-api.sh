#!/bin/bash
set -e

echo "[API] Starting lbdl API server..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8032 --log-level info
