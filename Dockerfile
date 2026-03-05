FROM python:3.12-slim

# Install system deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg cron curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY static/ ./static/
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

ENV LBDL_DATA_DIR=/app/music
ENV LBDL_CONFIG_DIR=/app/config
ENV LBDL_YTDLP_DIR=/app/config
ENV LBDL_AUDIO_FORMAT=opus
ENV LBDL_AUDIO_QUALITY=0
ENV LBDL_SCHEDULER_CRON="0 */2 * * *"
ENV LBDL_TZ=UTC
ENV LBDL_LOG_LEVEL=INFO
ENV LBDL_LOG_FORMAT=json

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
