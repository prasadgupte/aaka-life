FROM python:3.11-slim

# System deps
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git sqlite3 gettext-base ca-certificates gnupg cron \
    tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# WhatsApp support is opt-in at build time. The default image is Telegram-only:
# no Node, no OpenClaw, no Baileys → much smaller, and OpenClaw-free.
#   docker build --build-arg WITH_WHATSAPP=true ...   # to include WhatsApp
ARG WITH_WHATSAPP=false
RUN if [ "$WITH_WHATSAPP" = "true" ]; then \
      curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
      && apt-get install -y --no-install-recommends nodejs \
      && npm install -g openclaw @whiskeysockets/baileys \
      && rm -rf /var/lib/apt/lists/*; \
    fi

# Non-root user for openclaw process + gosu for privilege drop in entrypoint
RUN groupadd -r aaka && useradd -r -g aaka -d /home/aaka -m aaka
RUN apt-get update && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps (includes google libs for VPS-scoped token writes)
COPY requirements_sensor.txt .
RUN pip install --no-cache-dir -r requirements_sensor.txt

# Copy repo (secrets/config come from volume mount at runtime)
COPY . .

# Openclaw config dir owned by aaka user (entrypoint writes here before gosu drop)
RUN mkdir -p /home/aaka/.openclaw && chown aaka:aaka /home/aaka/.openclaw

# Queue DB lives on a named volume so it survives container restarts
ENV QUEUE_DB=/data/butler.db
ENV AAKA_BASE=/app
ENV AAKA_CONFIG_DIR=/config
ENV GATEWAY_BACKEND=openclaw
# Default channel set — Telegram only (OpenClaw-free). Add whatsapp/slack to opt in.
ENV ENABLED_CHANNELS=telegram

EXPOSE 18789 18790

# Crontab: flush outbox every 3 minutes + scheduled summaries every hour
RUN printf "*/3 * * * * root cd /app && python3 sensor/flush_outbox.py >> /var/log/outbox_flush.log 2>&1\n0 * * * * root cd /app && python3 sensor/scheduled_summaries.py --check >> /var/log/summaries.log 2>&1\n" \
    > /etc/cron.d/aaka-cron && chmod 0644 /etc/cron.d/aaka-cron

# Entrypoint: template agent.yaml, start openclaw daemon
COPY sensor/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
