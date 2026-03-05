#!/bin/bash
set -e

# Set timezone
if [ -n "$LBDL_TZ" ]; then
    ln -snf /usr/share/zoneinfo/$LBDL_TZ /etc/localtime
    echo $LBDL_TZ > /etc/timezone
fi

# Create required dirs
mkdir -p /app/config /app/music

# Write cron job using the configured schedule
CRON="${LBDL_SCHEDULER_CRON:-0 */2 * * *}"
echo "Installing cron schedule: $CRON"

# The sync script checks saved playlists for new tracks
# Notes:
#   MAILTO=""        — suppress cron's attempt to email output (no MTA in container)
#   PATH=...         — cron runs with a stripped env; set explicitly so python is found
#   PYTHONPATH=/app  — required for "from app.organizer import ..." inside sync.py
#   trailing newline — /etc/cron.d files MUST end with a newline or cron ignores them
printf 'MAILTO=""
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
PYTHONPATH=/app
%s root /usr/local/bin/python /app/app/sync.py >> /var/log/lbdl-sync.log 2>&1
' "$CRON" > /etc/cron.d/lbdl-sync
chmod 0644 /etc/cron.d/lbdl-sync
touch /var/log/lbdl-sync.log

# Start cron in background
cron

echo "Starting lbdl server..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
