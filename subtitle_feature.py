from __future__ import annotations

import json
import os
import re
import signal
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import requests

ENDPOINT = "https://api.siliconflow.cn/v1/audio/transcriptions"
MODEL = "FunAudioLLM/SenseVoiceSmall"
META_KEY = "siliconflow_api_key"


def install(core: Any) -> None:
    original_execute = core.execute
    original_error = core.err

    def get_key() -> str:
        env_key = os.getenv("SILICONFLOW_API_KEY", "").strip()
        if env_key:
            return env_key
        encrypted = core.meta(META_KEY)
        if not encrypted:
            return ""
        try:
            return core.F.decrypt(encrypted.encode()).decode().strip()
        except Exception:
            return ""

    def set_key(value: str) -> None:
        encrypted = core.F.encrypt(value.strip().encode()).decode()
        core.setmeta(META_KEY, encrypted)

    def clear_key() -> None:
        with core.con() as connection:
            connection.execute("DELETE FROM meta WHERE k=?", (META_KEY,))

    def friendly_error(text: str, url: str = "") -> tuple[str, str]:
        lower = text.lower()
        if "siliconflow_key_missing" in lower:
            return "SILICONFLOW_KEY_MISSING", "平台没有字幕，请先在设置中填写硅基流动 API Key。"
        if "siliconflow_unauthorized" in lower:
            return "SILICONFLOW_UNAUTHORIZED", "硅基流动 API Key 无效或没有权限。"
        if "siliconflow_rate_limited" in lower:
            return "SILICONFLOW_RATE_LIMITED", "硅基流动接口限流，请稍后重试。"
        if "audio_too_long" in lower:
            return "AUDIO_TOO_LONG", "AI 字幕目前只处理不超过 1 小时的音频。"
        if "guest_ai_duration_limit" in lower:
            return "GUEST_AI_DURATION_LIMIT", "游客 AI 字幕最长支持 20 分钟。"
        if "guest_ai_busy" in lower:
            return "GUEST_AI_BUSY", "游客 AI 字幕正在处理中，请稍后重试。"
        if "guest_ai_hourly_limit" in lower:
            return "GUEST_AI_HOURLY_LIMIT", "游客 AI 字幕每小时最多 3 次。"
        if "guest_ai_disabled" in lower:
            return "GUEST_AI_DISABLED", "当前游客策略未启用 AI 字幕。"
        if "transcription_empty" in lower:
            return "TRANSCRIPTION_EMPTY", "语音识别没有返回有效文字。"
        return original_error(text, url)

    def safe_name(value: str | None) -> str:
        value = re.sub(r'[\\/:*?"<>|\r\n]+', "_", value or "subtitle")
        return value.strip(" ._")[:120] or "subtitle"

    def run(command: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)

    def run_task(task: dict[str, Any], work: Path, command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        with core.LOCK:
            core.ACTIVE[task["id"]] = process
        deadline = time.monotonic() + timeout
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.25)
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                pass
            current = core.row(task["id"])
            if current and current["status"] == "cancelled":
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                raise RuntimeError("TASK_CANCELLED")
            if core.guest_task_size_exceeded(task, work):
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                raise core.guest_limit_failure()
            if time.monotonic() >= deadline:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                raise subprocess.TimeoutExpired(command, timeout)

    subtitle_suffixes = {".srt", ".vtt", ".ass", ".ssa", ".lrc", ".ttml", ".json", ".txt"}

    def subtitle_files(work: Path) -> list[Path]:
        return sorted(
            [path for path in work.iterdir() if path.is_file() and path.suffix.lower() in subtitle_suffixes],
            key=lambda path: path.name,
        )

    def existing_subtitles(task: dict[str, Any], work: Path, cookie: Path | None) -> list[Path]:
        command = core.task_base(task, cookie, False) + [
            "--skip-download", "--write-subs", "--write-auto-subs",
            "--sub-langs", "all",
            "--sub-format", "srt/best", "--convert-subs", "srt",
            "--output", str(work / "%(title).150B [%(id)s].%(ext)s"), task["url"],
        ]
        result = run_task(task, work, command, 120)
        files = subtitle_files(work)
        if files:
            return files
        if result.returncode and core.denied(result.stderr or result.stdout):
            retry = core.task_base(task, cookie, True) + command[len(core.task_base(task, cookie, False)):]
            run_task(task, work, retry, 120)
            return subtitle_files(work)
        return []

    def normalize_subtitles(work: Path, files: list[Path]) -> list[Path]:
        reserved = set(files)
        targets: list[Path] = []
        for source in files:
            stem, suffix = safe_name(source.stem), source.suffix.lower()
            target = work / f"{stem}{suffix}"
            index = 2
            while (target in reserved and target != source) or target in targets:
                target = work / f"{stem}-{index}{suffix}"
                index += 1
            if source != target:
                shutil.move(str(source), target)
            targets.append(target)
        return targets

    def download_audio(task: dict[str, Any], work: Path, cookie: Path | None) -> Path:
        command = core.task_base(task, cookie, False) + [
            "--format", "bestaudio/best", "--output", str(work / "source.%(ext)s"), task["url"],
        ]
        if core.is_guest_task(task):
            command[1:1] = ["--max-filesize", f"{core.task_policy(task)['max_file_size_gb']}G"]
        result = run_task(task, work, command, 900)
        if result.returncode:
            code, message = friendly_error(result.stderr or result.stdout, task["url"])
            raise RuntimeError(json.dumps({"code": code, "message": message}, ensure_ascii=False))
        files = [path for path in work.glob("source.*") if path.is_file() and path.suffix != ".part"]
        if not files:
            raise RuntimeError("没有找到下载后的音频文件")
        return max(files, key=lambda path: path.stat().st_size)

    def duration(path: Path) -> float:
        result = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)], 30)
        if result.returncode:
            raise RuntimeError("无法读取音频时长")
        return float(result.stdout.strip())

    def make_chunks(task: dict[str, Any], source: Path, work: Path, total: float) -> list[Path]:
        maximum = core.task_policy(task)['ai_transcription_max_duration_minutes'] * 60 if core.is_guest_task(task) else 3600.5
        if total > maximum:
            raise RuntimeError("GUEST_AI_DURATION_LIMIT" if core.is_guest_task(task) else "AUDIO_TOO_LONG")
        chunk_dir = work / "chunks"
        chunk_dir.mkdir(exist_ok=True)
        result = run_task(task, work, [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
            "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k",
            "-f", "segment", "-segment_time", "60", "-reset_timestamps", "1",
            str(chunk_dir / "chunk_%03d.mp3"),
        ], 600)
        if result.returncode:
            raise RuntimeError("FFmpeg 音频分段失败")
        chunks = sorted(chunk_dir.glob("chunk_*.mp3"))
        if not chunks:
            raise RuntimeError("没有生成转写音频分段")
        return chunks

    def transcribe(path: Path, key: str) -> str:
        with path.open("rb") as audio:
            response = requests.post(
                ENDPOINT,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (path.name, audio, "audio/mpeg"), "model": (None, MODEL)},
                timeout=(20, 240),
            )
        if response.status_code in {401, 403}:
            raise RuntimeError("SILICONFLOW_UNAUTHORIZED")
        if response.status_code == 429:
            raise RuntimeError("SILICONFLOW_RATE_LIMITED")
        if not response.ok:
            raise RuntimeError(f"硅基流动转写失败 HTTP {response.status_code}: {response.text[:300]}")
        text = str(response.json().get("text") or "").strip()
        if not text:
            raise RuntimeError("TRANSCRIPTION_EMPTY")
        return text

    def srt_time(seconds: float) -> str:
        ms = max(0, round(seconds * 1000))
        hours, ms = divmod(ms, 3600000)
        minutes, ms = divmod(ms, 60000)
        secs, ms = divmod(ms, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"

    def pieces(text: str) -> list[str]:
        items = [item.strip() for item in re.split(r"(?<=[。！？!?；;])\s*", re.sub(r"\s+", " ", text)) if item.strip()]
        output: list[str] = []
        for item in items or [text.strip()]:
            while len(item) > 32:
                output.append(item[:32])
                item = item[32:]
            if item:
                output.append(item)
        return output

    def create_outputs(task: dict[str, Any], work: Path, chunks: list[Path], total: float, key: str) -> tuple[Path, list[Path]]:
        cues: list[tuple[float, float, str]] = []
        raw: list[dict[str, Any]] = []
        texts: list[str] = []
        for index, chunk in enumerate(chunks):
            if (core.row(task["id"]) or {}).get("status") == "cancelled":
                raise RuntimeError("TASK_CANCELLED")
            core.patch(task["id"], status="processing", progress=40 + index / len(chunks) * 55, eta=f"AI 转写 {index + 1}/{len(chunks)}")
            text = transcribe(chunk, key)
            start = index * 60.0
            end = min(total, start + duration(chunk))
            parts = pieces(text)
            weights = [max(len(part), 1) for part in parts]
            cursor = start
            accumulated = 0
            for part_index, (part, weight) in enumerate(zip(parts, weights)):
                accumulated += weight
                cue_end = end if part_index == len(parts) - 1 else start + (end - start) * accumulated / sum(weights)
                cues.append((cursor, cue_end, part))
                cursor = cue_end
            texts.append(text)
            raw.append({"index": index, "start": start, "end": end, "text": text})
        name = safe_name(task.get("title"))
        srt = work / f"{name}.srt"
        txt = work / f"{name}.txt"
        raw_json = work / f"{name}.json"
        lines: list[str] = []
        for index, (start, end, text) in enumerate(cues, 1):
            lines.extend([str(index), f"{srt_time(start)} --> {srt_time(end)}", text, ""])
        srt.write_text("\n".join(lines), encoding="utf-8")
        txt.write_text("\n".join(texts), encoding="utf-8")
        raw_json.write_text(json.dumps({"source": "siliconflow_ai", "model": MODEL, "approximate_timestamps": True, "chunks": raw}, ensure_ascii=False, indent=2), encoding="utf-8")
        return srt, [srt, txt, raw_json]

    def finish(task: dict[str, Any], primary: Path, files: list[Path]) -> None:
        if (core.row(task["id"]) or {}).get("status") == "cancelled":
            raise RuntimeError("TASK_CANCELLED")
        destination = core.DL / task["id"]
        destination.mkdir(parents=True, exist_ok=True)
        primary_target: Path | None = None
        total_size = 0
        for source in files:
            target = destination / source.name
            shutil.move(str(source), target)
            total_size += target.stat().st_size
            if source == primary:
                primary_target = target
        shutil.rmtree(core.TMP / task["id"], ignore_errors=True)
        if core.is_guest_task(task) and core.guest_task_size_exceeded(task, destination):
            shutil.rmtree(destination, ignore_errors=True)
            raise core.guest_limit_failure()
        core.patch(task["id"], progress=100, eta="", output_path=str(primary_target), output_size=total_size)
        auto_save = getattr(core, "auto_save_subtitles_to_koofr", None)
        if auto_save and not core.is_guest_task(task):
            auto_save(task["id"])
        core.patch(task["id"], status="completed", progress=100, eta="", finished=core.now())

    def subtitle_execute(task_id: str) -> None:
        task = core.row(task_id)
        if not task or task.get("status") == "cancelled":
            return
        work = core.TMP / task_id
        work.mkdir(parents=True, exist_ok=True)
        cookie = None
        ai_slot = False
        try:
            cookie = core.cpath(task["options"].get("cookie_id")) if not core.is_guest_task(task) else None
            core.patch(task_id, status="processing", progress=5, eta="正在检查平台字幕", error_code=None, error_message=None)
            subtitles = existing_subtitles(task, work, cookie)
            if subtitles:
                outputs = normalize_subtitles(work, subtitles)
                finish(task, max(outputs, key=lambda path: path.stat().st_size), outputs)
                return
            key = get_key()
            if not key:
                raise RuntimeError("SILICONFLOW_KEY_MISSING")
            ai_slot = core.start_guest_ai(task)
            core.patch(task_id, status="downloading", progress=15, eta="平台无字幕，正在下载音频")
            audio = download_audio(task, work, cookie)
            total = duration(audio)
            core.patch(task_id, status="processing", progress=35, eta="正在准备 AI 转写音频")
            chunks = make_chunks(task, audio, work, total)
            primary, outputs = create_outputs(task, work, chunks, total, key)
            finish(task, primary, outputs)
        except Exception as exc:
            if (core.row(task_id) or {}).get("status") == "cancelled":
                return
            try:
                parsed = json.loads(str(exc))
                code, message = parsed["code"], parsed["message"]
            except Exception:
                code, message = friendly_error(str(exc), task["url"])
            if core.is_guest_task(task):
                if code in {"SILICONFLOW_KEY_MISSING", "SILICONFLOW_UNAUTHORIZED", "SILICONFLOW_RATE_LIMITED"}:
                    message = "当前游客字幕处理暂不可用，请稍后重试。"
                elif not code.startswith("GUEST_") and not code.startswith("guest_"):
                    message = "游客字幕处理失败，请确认链接为公开可访问的媒体后重试。"
            core.patch(task_id, status="failed", error_code=code, error_message=message, finished=core.now())
        finally:
            if cookie:
                cookie.unlink(missing_ok=True)
            if ai_slot:
                core.release_guest_ai_slot(task_id)
            if core.is_guest_task(task):
                shutil.rmtree(work, ignore_errors=True)
            with core.LOCK:
                core.ACTIVE.pop(task_id, None)

    def execute(task_id: str) -> None:
        task = core.row(task_id)
        if task and task["options"].get("mode") == "subtitles":
            subtitle_execute(task_id)
        else:
            original_execute(task_id)

    def settings_get() -> dict[str, Any]:
        configured = bool(get_key())
        return {"configured": configured, "model": MODEL, "source": "environment" if os.getenv("SILICONFLOW_API_KEY", "").strip() else ("ui" if configured else "none")}

    def settings_put(payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("clear"):
            clear_key()
        value = str(payload.get("api_key") or "").strip()
        if value:
            if len(value) < 10:
                raise ValueError("API Key 格式不正确")
            set_key(value)
        return settings_get()

    def add_route(path: str, endpoint: Any, methods: list[str]) -> None:
        core.app.add_api_route(path, endpoint, methods=methods, dependencies=[core.Depends(core.auth)])
        route = core.app.router.routes.pop()
        index = next((i for i, item in enumerate(core.app.router.routes) if getattr(item, "path", None) == "/{path:path}"), len(core.app.router.routes))
        core.app.router.routes.insert(index, route)

    core.err = friendly_error
    core.execute = execute
    add_route("/api/admin/transcription/settings", settings_get, ["GET"])
    add_route("/api/admin/transcription/settings", settings_put, ["PUT"])
    add_route("/api/transcription/settings", settings_get, ["GET"])
    add_route("/api/transcription/settings", settings_put, ["PUT"])
