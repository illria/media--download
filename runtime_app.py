from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import app as core


_original_error = core.err
_original_probe = core.probe_sync


def _base(url: str, cookie_path: Path | None = None, compat: bool = False) -> list[str]:
    """Build a bounded yt-dlp command without stacking multiple YouTube clients."""
    settings = core.settings()
    args = [
        "yt-dlp",
        "--no-playlist",
        "--force-ipv4",
        "--socket-timeout",
        "8",
        "--retries",
        "2",
        "--fragment-retries",
        "3",
        "--extractor-retries",
        "1",
        "--js-runtimes",
        "deno",
    ]
    proxy = str(settings.get("proxy_url") or "").strip()
    if proxy:
        args += ["--proxy", proxy]
    if cookie_path:
        args += ["--cookies", str(cookie_path)]
    if core.yt(url) and compat:
        args += ["--extractor-args", "youtube:player_client=web_safari"]
    return args


def _friendly_error(text: str, url: str = "") -> tuple[str, str]:
    lower = text.lower()
    if "probe_timeout" in lower or "timed out" in lower or "timeout" in lower:
        return (
            "PROBE_TIMEOUT",
            "YouTube 解析超时。系统已停止等待；请重试，或上传 Cookie / 设置代理后再解析。",
        )
    return _original_error(text, url)


def _probe(url: str, cookie_id: str | None) -> dict[str, Any]:
    cookie_path = core.cpath(cookie_id)
    attempts: list[tuple[str, bool]] = [("默认客户端", False)]
    if core.yt(url):
        attempts.append(("Safari 兼容客户端", True))

    errors: list[str] = []
    raw: dict[str, Any] | None = None
    try:
        for label, compat in attempts:
            command = _base(url, cookie_path, compat) + [
                "--dump-single-json",
                "--no-warnings",
                url,
            ]
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=22,
                )
            except subprocess.TimeoutExpired:
                errors.append(f"{label}解析超时")
                continue

            if completed.returncode == 0:
                try:
                    raw = json.loads(completed.stdout)
                    break
                except json.JSONDecodeError:
                    errors.append(f"{label}返回了无效数据")
                    continue

            message = (completed.stderr or completed.stdout or "解析失败")[-1200:]
            errors.append(f"{label}失败：{message}")

        if raw is None:
            if errors and all("超时" in item for item in errors):
                raise RuntimeError("PROBE_TIMEOUT: YouTube probe attempts timed out")
            raise RuntimeError("\n".join(errors)[-2400:] or "解析失败")
    finally:
        if cookie_path:
            cookie_path.unlink(missing_ok=True)

    videos, audios = core.simplify(raw.get("formats") or [])
    return {
        "id": raw.get("id"),
        "title": raw.get("title"),
        "uploader": raw.get("uploader") or raw.get("channel"),
        "platform": raw.get("extractor_key") or raw.get("extractor"),
        "duration": raw.get("duration"),
        "thumbnail": raw.get("thumbnail"),
        "is_live": bool(raw.get("is_live") or raw.get("live_status") == "is_live"),
        "drm": bool(raw.get("has_drm")),
        "video_options": videos,
        "audio_options": audios,
        "subtitles": sorted(
            set(raw.get("subtitles") or {})
            | set(raw.get("automatic_captions") or {})
        ),
        "webpage_url": raw.get("webpage_url") or url,
    }


core.base = _base
core.err = _friendly_error
core.probe_sync = _probe
app = core.app


if __name__ == "__main__":
    import os
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("APP_PORT", "19190")), workers=1)
