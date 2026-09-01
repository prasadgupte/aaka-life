FROM python:3.11-slim

# System deps
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git sqlite3 gettext-base ca-certificates gnupg cron \
    tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# Sensor is Telegram-only and OpenClaw-free: no Node, no OpenClaw, no Baileys.
# WhatsApp (when used) runs on the Mac via the wa-sidecar, never in this container.

# Non-root user for the sensor process + gosu for privilege drop in entrypoint
RUN groupadd -r aaka && useradd -r -g aaka -d /home/aaka -m aaka
RUN apt-get update && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps (includes google libs for VPS-scoped token writes)
COPY requirements_sensor.txt .
RUN pip install --no-cache-dir -r requirements_sensor.txt

# Copy repo (secrets/config come from volume mount at runtime)
COPY . .

# Queue DB lives on a named volume so it survives container restarts
ENV QUEUE_DB=/data/butler.db
ENV AAKA_BASE=/app
ENV AAKA_CONFIG_DIR=/config
# Telegram-only in the container (OpenClaw-free). WhatsApp runs on the Mac sidecar.
ENV ENABLED_CHANNELS=telegram

EXPOSE 18789 18790

# Crontab: flush outbox every 3 minutes + scheduled summaries every hour
RUN printf "*/3 * * * * root cd /app && python3 sensor/flush_outbox.py >> /var/log/outbox_flush.log 2>&1\n0 * * * * root cd /app && python3 sensor/scheduled_summaries.py --check >> /var/log/summaries.log 2>&1\n" \
    > /etc/cron.d/aaka-cron && chmod 0644 /etc/cron.d/aaka-cron

# Entrypoint: set up cron + permissions, run the Telegram multi-bot supervisor
COPY sensor/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
