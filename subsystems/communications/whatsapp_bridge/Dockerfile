# WhatsApp send bridge image.
#
# whatsapp-web.js drives a real WhatsApp Web session via Puppeteer, so the
# container needs Chromium plus its shared-library deps. We install distro
# chromium and tell Puppeteer to use it instead of downloading its own copy
# (faster builds, smaller image, easier to pin).

FROM node:20-bookworm-slim

ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium \
    NODE_ENV=production \
    PORT=3025 \
    HOST=0.0.0.0

RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        ca-certificates \
        fonts-liberation \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libc6 \
        libcairo2 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libexpat1 \
        libgbm1 \
        libglib2.0-0 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libx11-6 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        wget \
        xdg-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev && npm cache clean --force

COPY bridge.js ./

# Persistent session — mount a Docker volume here so QR is one-time.
VOLUME ["/app/.wa_session"]

EXPOSE 3025

CMD ["node", "bridge.js"]
