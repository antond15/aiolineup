FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=8080

# Non-root user. /data is created and owned here so a fresh named volume inherits the ownership.
RUN useradd --system --uid 10001 --user-group --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown app:app /data

WORKDIR /app
COPY --chown=root:root aiolineup ./aiolineup

USER app
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT', '8080')}/health\", timeout=4)"]

CMD ["python", "-m", "aiolineup"]
