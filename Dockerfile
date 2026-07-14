FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_PORT=19190 \
    DATA_ROOT=/data \
    HOME=/data/home \
    DENO_INSTALL=/opt/deno \
    PATH=/opt/deno/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip ffmpeg tini \
    && curl -fsSL https://deno.land/install.sh | sh \
    && deno --version \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip install --no-cache-dir -U --pre "yt-dlp[default,curl-cffi]" yt-dlp-ejs \
    && python -c "import importlib.metadata as m; print('yt-dlp',m.version('yt-dlp')); print('yt-dlp-ejs',m.version('yt-dlp-ejs'))"

COPY app.py ./
COPY runtime_app.py ./
COPY youtube_reliability.py ./
COPY youtube_execute.py ./
COPY youtube_hotfix.py ./
COPY subtitle_feature.py ./
COPY final_app.py ./
COPY static ./static
COPY strip_auth.py ./

RUN python strip_auth.py \
    && rm strip_auth.py \
    && mkdir -p /data/database /data/downloads /data/temp /data/cookies /data/home

EXPOSE 19190
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "final_app.py"]
