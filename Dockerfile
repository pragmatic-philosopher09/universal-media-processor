FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8000 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    NODE_VERSION=22.23.3

# ffmpeg for downloads/enhancement; Node.js >= 22 is yt-dlp's JavaScript runtime for YouTube.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl xz-utils \
    && ARCH="$(dpkg --print-architecture)" \
    && case "$ARCH" in amd64) NODE_ARCH=x64 ;; arm64) NODE_ARCH=arm64 ;; *) echo "unsupported arch $ARCH" && exit 1 ;; esac \
    && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz" -o /tmp/node.tar.xz \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner \
       "node-v${NODE_VERSION}-linux-${NODE_ARCH}/bin/node" \
    && rm -f /tmp/node.tar.xz \
    && apt-get purge -y --auto-remove curl xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && node --version && ffmpeg -version | head -1

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && chmod -R a+rX /opt/playwright

COPY app ./app
COPY static ./static

# Hugging Face Spaces run the container as this user; keep everything it needs writable.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /app
USER appuser
ENV HOME=/home/appuser
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD python -m app.healthcheck || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host ${HOST} --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
