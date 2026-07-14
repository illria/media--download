from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any

import app as core
from starlette.responses import Response

_original_error = core.err


def runtime_base(url: str, cookie_path: Path | None = None, compat: bool = False) -> list[str]:
    cfg = core.settings()
    args = [
        "yt-dlp", "--no-playlist", "--force-ipv4",
        "--socket-timeout", "8", "--retries", "2",
        "--fragment-retries", "3", "--extractor-retries", "1",
        "--js-runtimes", "deno",
    ]
    proxy = str(cfg.get("proxy_url") or "").strip()
    if proxy:
        args += ["--proxy", proxy]
    if cookie_path:
        args += ["--cookies", str(cookie_path)]
    if core.yt(url) and compat:
        args += ["--extractor-args", "youtube:player_client=web_safari"]
    return args


def friendly_error(text: str, url: str = "") -> tuple[str, str]:
    lower = text.lower()
    if "probe_timeout" in lower or "timed out" in lower or "timeout" in lower:
        return "PROBE_TIMEOUT", "YouTube 解析超时，请重试；仍失败时上传 Cookie 或设置代理。"
    return _original_error(text, url)


def runtime_probe(url: str, cookie_id: str | None) -> dict[str, Any]:
    cookie_path = core.cpath(cookie_id)
    attempts = [("默认客户端", False)]
    if core.yt(url):
        attempts.append(("Safari 兼容客户端", True))
    errors: list[str] = []
    raw: dict[str, Any] | None = None
    try:
        for label, compat in attempts:
            command = runtime_base(url, cookie_path, compat) + ["--dump-single-json", "--no-warnings", url]
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=22)
            except subprocess.TimeoutExpired:
                errors.append(f"{label}解析超时")
                continue
            if result.returncode == 0:
                try:
                    raw = json.loads(result.stdout)
                    break
                except json.JSONDecodeError:
                    errors.append(f"{label}返回数据异常")
                    continue
            detail = (result.stderr or result.stdout or "解析失败")[-1200:]
            errors.append(f"{label}失败：{detail}")
        if raw is None:
            if errors and all("超时" in item for item in errors):
                raise RuntimeError("PROBE_TIMEOUT")
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
        "subtitles": sorted(set(raw.get("subtitles") or {}) | set(raw.get("automatic_captions") or {})),
        "webpage_url": raw.get("webpage_url") or url,
    }


def runtime_execute(task_id: str) -> None:
    task = core.row(task_id)
    if not task:
        return
    cookie_path: Path | None = None
    logs: list[str] = []
    try:
        if shutil.disk_usage(core.ROOT).free < int(core.settings()["min_free_gb"]) * 1024**3:
            raise RuntimeError("no space")
        cookie_path = core.cpath(task["options"].get("cookie_id"))
        (core.TMP / task_id).mkdir(parents=True, exist_ok=True)
        core.patch(task_id, status="downloading", progress=0, error_code=None, error_message=None, log_tail="")
        return_code = 1
        attempts = [False, True] if core.yt(task["url"]) else [False]
        for attempt_index, compat in enumerate(attempts):
            if attempt_index:
                logs.append("[Media Hub] 正在切换 YouTube 兼容客户端重试")
            process = subprocess.Popen(
                core.cmd(task, cookie_path, compat),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            with core.LOCK:
                core.ACTIVE[task_id] = process
            assert process.stdout is not None
            for line in process.stdout:
                clean = line.strip()
                if clean:
                    logs = (logs + [clean])[-120:]
                match = re.search(r"PROGRESS:\s*([0-9.]+)%\|([^|]*)\|([^|]*)", clean)
                if match:
                    core.patch(
                        task_id,
                        status="downloading",
                        progress=min(float(match.group(1)), 98.0),
                        speed=match.group(2),
                        eta=match.group(3),
                        log_tail="\n".join(logs),
                    )
                if any(marker in clean for marker in (
                    "[Merger]", "[VideoRemuxer]", "[VideoConvertor]",
                    "[ExtractAudio]", "[Metadata]", "[ThumbnailsConvertor]",
                )):
                    core.patch(
                        task_id,
                        status="processing",
                        progress=99,
                        speed="",
                        eta="正在合并/处理",
                        log_tail="\n".join(logs),
                    )
                current = core.row(task_id)
                if current and current["status"] == "cancelled":
                    os.killpg(process.pid, signal.SIGTERM)
                    break
            return_code = process.wait()
            current = core.row(task_id)
            if current and current["status"] == "cancelled":
                shutil.rmtree(core.TMP / task_id, ignore_errors=True)
                return
            if return_code == 0 or not core.denied("\n".join(logs)):
                break
        if return_code:
            code, message = friendly_error("\n".join(logs), task["url"])
            raise RuntimeError(json.dumps({"code": code, "message": message}, ensure_ascii=False))
        core.patch(task_id, status="processing", progress=99, speed="", eta="正在整理文件", log_tail="\n".join(logs))
        output_path, output_size = core.move(task_id)
        core.patch(
            task_id,
            status="completed",
            progress=100,
            speed="",
            eta="",
            output_path=output_path,
            output_size=output_size,
            finished=core.now(),
            log_tail="\n".join(logs),
        )
    except Exception as exc:
        try:
            parsed = json.loads(str(exc))
            code, message = parsed["code"], parsed["message"]
        except Exception:
            code, message = friendly_error(str(exc), task["url"])
        core.patch(task_id, status="failed", error_code=code, error_message=message, finished=core.now(), log_tail="\n".join(logs))
    finally:
        if cookie_path:
            cookie_path.unlink(missing_ok=True)
        with core.LOCK:
            core.ACTIVE.pop(task_id, None)


core.base = runtime_base
core.err = friendly_error
core.probe_sync = runtime_probe
core.execute = runtime_execute

@core.app.middleware("http")
async def inject_auto_download(request, call_next):
    response = await call_next(request)
    if request.url.path not in {"/", "/index.html"}:
        return response
    content_type = response.headers.get("content-type", "")
    if "text/html" not in content_type:
        return response
    body = b""
    async for chunk in response.body_iterator:
        body += chunk
    html = body.decode("utf-8", errors="replace")
    if "/assets/auto_download.js" not in html:
        html = html.replace("</body>", '<script src="/assets/auto_download.js"></script></body>')
    headers = dict(response.headers)
    headers.pop("content-length", None)
    return Response(content=html, status_code=response.status_code, headers=headers, media_type="text/html")

app = core.app

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("APP_PORT", "19190")), workers=1)
