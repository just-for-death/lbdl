#!/bin/bash
set -e

echo "[Sync] Starting lbdl sync service (cron daemon)..."

SETTINGS_FILE="${LBDL_CONFIG_DIR:-/app/config}/settings.json"
ENV_CRON="${LBDL_SCHEDULER_CRON:-0 */2 * * *}"

# Prefer cron schedule from settings.json (written by Settings UI) over env var
if [ -f "$SETTINGS_FILE" ]; then
    JSON_CRON=$(python3 -c "
import json, sys
try:
    d = json.load(open('$SETTINGS_FILE'))
    v = d.get('sync_cron', '').strip()
    if v: print(v)
except: pass
" 2>/dev/null)
    CRON_SCHEDULE="${JSON_CRON:-$ENV_CRON}"
else
    CRON_SCHEDULE="$ENV_CRON"
fi

SYNC_LOG="/var/log/lbdl-sync.log"

if [ -z "$CRON_SCHEDULE" ]; then
    echo "[Sync] sync_cron is empty — cron disabled, container will idle"
    touch "$SYNC_LOG"
    exec tail -f "$SYNC_LOG"
fi

echo "${CRON_SCHEDULE} cd /app && python sync_service.py >> ${SYNC_LOG} 2>&1" | crontab -
echo "[Sync] Crontab set: ${CRON_SCHEDULE}"

touch "$SYNC_LOG"
exec cron -f
