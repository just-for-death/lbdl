#!/bin/bash
set -e

# Set timezone
if [ -n "$LBDL_TZ" ]; then
    ln -snf /usr/share/zoneinfo/$LBDL_TZ /etc/localtime
    echo $LBDL_TZ > /etc/timezone
fi

mkdir -p /app/config /app/music

CRON="${LBDL_SCHEDULER_CRON:-0 */2 * * *}"
printf 'MAILTO=""\nSHELL=/bin/bash\nPATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin\nPYTHONPATH=/app\n%s root /usr/local/bin/python /app/app/sync.py >> /var/log/lbdl-sync.log 2>&1\n' "$CRON" > /etc/cron.d/lbdl-sync
chmod 0644 /etc/cron.d/lbdl-sync
touch /var/log/lbdl-sync.log
cron

LOG_LEVEL="${LBDL_LOG_LEVEL:-INFO}"
LOG_FORMAT="${LBDL_LOG_FORMAT:-json}"

echo "{\"time\":\"$(date -Iseconds)\",\"level\":\"INFO\",\"logger\":\"lbdl.entrypoint\",\"message\":\"Starting lbdl\",\"log_level\":\"${LOG_LEVEL}\",\"log_format\":\"${LOG_FORMAT}\"}"

exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --log-level "$(echo $LOG_LEVEL | tr '[:upper:]' '[:lower:]')" \
    --access-log \
    --no-server-header
