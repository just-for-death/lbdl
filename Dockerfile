FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y ffmpeg cron && \
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

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
