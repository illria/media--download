FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_PORT=19190 \
    DATA_ROOT=/data \
    HOME=/data/home

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg nodejs tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY app.py ./
COPY static ./static

RUN mkdir -p /data/database /data/downloads /data/temp /data/cookies /data/home

EXPOSE 19190
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "app.py"]
