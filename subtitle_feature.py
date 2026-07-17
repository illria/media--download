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

ASR_ENDPOINT = "https://api.siliconflow.cn/v1/audio/transcriptions"
CHAT_ENDPOINT = "https://api.siliconflow.cn/v1/chat/completions"
ASR_MODEL = "FunAudioLLM/SenseVoiceSmall"
DEFAULT_TRANSLATION_MODEL = "tencent/Hunyuan-MT-7B"
DEFAULT_FALLBACK_MODELS = [
    "Qwen/Qwen3.5-4B",
    "Qwen/Qwen2.5-7B-Instruct",
    "THUDM/GLM-4-9B-0414",
]
ALLOWED_TRANSLATION_MODELS = [
    "tencent/Hunyuan-MT-7B",
    "Qwen/Qwen3.5-4B",
    "Qwen/Qwen2.5-7B-Instruct",
    "THUDM/GLM-4-9B-0414",
]
META_KEY = "siliconflow_api_key"
TRANSLATION_META_KEY = "subtitle_translation_settings"
BATCH_MAX_CUES = 30
BATCH_MAX_CHARS = 3000


def install(core: Any) -> None:
    original_execute = core.execute
    original_error = core.err
    languages = getattr(core, "SUBTITLE_LANGUAGES", {
        "zh-CN": "简体中文", "zh-TW": "繁体中文", "en": "英语", "ja": "日语", "ko": "韩语",
        "vi": "越南语", "th": "泰语", "fr": "法语", "de": "德语", "es": "西班牙语",
        "pt": "葡萄牙语", "ru": "俄语", "ar": "阿拉伯语", "id": "印度尼西亚语",
        "tr": "土耳其语", "it": "意大利语",
    })

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

    def load_translation_settings() -> dict[str, Any]:
        raw = core.meta(TRANSLATION_META_KEY)
        data: dict[str, Any] = {}
        if raw:
            try:
                data = json.loads(raw)
            except Exception:
                data = {}
        model = str(data.get("translation_model") or DEFAULT_TRANSLATION_MODEL)
        if model not in ALLOWED_TRANSLATION_MODELS:
            model = DEFAULT_TRANSLATION_MODEL
        fallbacks = []
        for item in data.get("translation_fallback_models") or DEFAULT_FALLBACK_MODELS:
            name = str(item)
            if name in ALLOWED_TRANSLATION_MODELS and name != model and name not in fallbacks:
                fallbacks.append(name)
            if len(fallbacks) >= 3:
                break
        if not fallbacks:
            fallbacks = [item for item in DEFAULT_FALLBACK_MODELS if item != model][:3]
        return {
            "translation_enabled": bool(data.get("translation_enabled", True)),
            "translation_model": model,
            "translation_fallback_models": fallbacks,
        }

    def save_translation_settings(payload: dict[str, Any]) -> dict[str, Any]:
        current = load_translation_settings()
        if "translation_enabled" in payload:
            current["translation_enabled"] = bool(payload.get("translation_enabled"))
        if "translation_model" in payload:
            model = str(payload.get("translation_model") or "").strip()
            if model not in ALLOWED_TRANSLATION_MODELS:
                raise ValueError("翻译主模型不在白名单内")
            current["translation_model"] = model
        if "translation_fallback_models" in payload:
            items = payload.get("translation_fallback_models") or []
            if not isinstance(items, list):
                raise ValueError("备用模型格式不正确")
            cleaned = []
            for item in items:
                name = str(item or "").strip()
                if not name:
                    continue
                if name not in ALLOWED_TRANSLATION_MODELS:
                    raise ValueError("备用模型不在白名单内")
                if name == current["translation_model"] or name in cleaned:
                    continue
                cleaned.append(name)
                if len(cleaned) >= 3:
                    break
            current["translation_fallback_models"] = cleaned
        if current["translation_model"] in current["translation_fallback_models"]:
            current["translation_fallback_models"] = [item for item in current["translation_fallback_models"] if item != current["translation_model"]]
        core.setmeta(TRANSLATION_META_KEY, json.dumps(current, ensure_ascii=False))
        return current

    def friendly_error(text: str, url: str = "") -> tuple[str, str]:
        lower = text.lower()
        mapping = {
            "siliconflow_key_missing": ("SILICONFLOW_KEY_MISSING", "平台没有字幕，请先在设置中填写硅基流动 API Key。"),
            "siliconflow_unauthorized": ("SILICONFLOW_UNAUTHORIZED", "硅基流动 API Key 无效或没有权限。"),
            "siliconflow_rate_limited": ("SILICONFLOW_RATE_LIMITED", "硅基流动接口限流，请稍后重试。"),
            "siliconflow_translation_unauthorized": ("SILICONFLOW_TRANSLATION_UNAUTHORIZED", "字幕翻译鉴权失败。"),
            "siliconflow_translation_rate_limited": ("SILICONFLOW_TRANSLATION_RATE_LIMITED", "字幕翻译接口限流，请稍后重试。"),
            "siliconflow_translation_timeout": ("SILICONFLOW_TRANSLATION_TIMEOUT", "字幕翻译超时，请稍后重试。"),
            "audio_too_long": ("AUDIO_TOO_LONG", "AI 字幕目前只处理不超过 1 小时的音频。"),
            "guest_ai_duration_limit": ("GUEST_AI_DURATION_LIMIT", "游客 AI 字幕超过允许时长。"),
            "guest_ai_busy": ("GUEST_AI_BUSY", "游客 AI 字幕正在处理中，请稍后重试。"),
            "guest_ai_hourly_limit": ("GUEST_AI_HOURLY_LIMIT", "游客 AI 字幕已达到每小时次数上限。"),
            "guest_ai_disabled": ("GUEST_AI_DISABLED", "当前未启用游客 AI 字幕。"),
            "guest_translation_disabled": ("GUEST_TRANSLATION_DISABLED", "当前未启用游客字幕翻译。"),
            "guest_translation_hourly_limit": ("GUEST_TRANSLATION_HOURLY_LIMIT", "游客字幕翻译已达到每小时次数上限。"),
            "guest_translation_busy": ("GUEST_TRANSLATION_BUSY", "字幕翻译正在处理中，请稍后重试。"),
            "guest_translation_duration_limit": ("GUEST_TRANSLATION_DURATION_LIMIT", "该视频超过游客字幕翻译时长限制。"),
            "subtitle_translation_failed": ("SUBTITLE_TRANSLATION_FAILED", "翻译失败，已保留原字幕。"),
            "subtitle_parse_failed": ("SUBTITLE_PARSE_FAILED", "字幕解析失败。"),
            "subtitle_source_not_found": ("SUBTITLE_SOURCE_NOT_FOUND", "没有找到可用源字幕。"),
            "transcription_empty": ("TRANSCRIPTION_EMPTY", "语音识别没有返回有效文字。"),
        }
        for key, value in mapping.items():
            if key in lower:
                return value
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
                raise core.guest_limit_failure(task)
            if time.monotonic() >= deadline:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                raise subprocess.TimeoutExpired(command, timeout)

    def ensure_not_cancelled(task_id: str) -> None:
        if (core.row(task_id) or {}).get("status") == "cancelled":
            raise RuntimeError("TASK_CANCELLED")

    def normalize_lang(code: str | None) -> str:
        value = str(code or "").strip()
        aliases = {
            "zh": "zh-CN", "zh-hans": "zh-CN", "zh-cn": "zh-CN", "cn": "zh-CN",
            "zh-hant": "zh-TW", "zh-tw": "zh-TW", "jp": "ja", "kr": "ko",
        }
        lowered = value.lower()
        if value in languages:
            return value
        if lowered in aliases:
            return aliases[lowered]
        base = lowered.split("-", 1)[0]
        for key in languages:
            if key.lower() == lowered or key.lower().startswith(base + "-") or key.lower() == base:
                return key
        return value

    def task_subtitle_options(task: dict[str, Any]) -> dict[str, str]:
        options = task.get("options") if isinstance(task.get("options"), dict) else {}
        source = str(options.get("subtitle_source_language") or "auto")
        target = normalize_lang(options.get("subtitle_target_language") or "zh-CN") or "zh-CN"
        mode = str(options.get("subtitle_output_mode") or "translated").lower()
        if mode not in {"original", "translated", "bilingual"}:
            mode = "translated"
        if source != "auto":
            source = normalize_lang(source) or "auto"
        return {
            "subtitle_source_language": source,
            "subtitle_target_language": target if target in languages else "zh-CN",
            "subtitle_output_mode": mode,
        }

    def language_candidates(source: str, target: str) -> list[str]:
        order = []
        for item in [target, source if source != "auto" else "", "en", "zh-CN", "zh", "ja", "ko", "es", "fr", "de", "ru"]:
            code = str(item or "").strip()
            if code and code not in order:
                order.append(code)
        # include common auto-caption variants
        expanded = []
        for code in order:
            expanded.append(code)
            if "-" not in code:
                expanded.append(code + ".*")
        return expanded or ["en", "zh-CN"]

    def detect_subtitle_meta(path: Path) -> dict[str, Any]:
        name = path.name
        lower = name.lower()
        auto = ".auto." in lower or lower.endswith(".auto.srt") or re.search(r"\.(auto|automatic)[\.-]", lower) is not None
        lang = "und"
        match = re.search(r"\.([A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?)(?:\.auto)?\.srt$", name, re.I)
        if match:
            lang = normalize_lang(match.group(1)) or match.group(1)
        else:
            match = re.search(r"\[([A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?)\]", name)
            if match:
                lang = normalize_lang(match.group(1)) or match.group(1)
        return {"path": path, "language": lang, "auto": auto, "size": path.stat().st_size if path.is_file() else 0}

    def score_subtitle(meta: dict[str, Any], source: str, target: str, prefer_target: bool) -> tuple:
        lang = normalize_lang(meta.get("language"))
        auto = 1 if meta.get("auto") else 0
        if prefer_target and lang == target:
            return (0, auto, -int(meta.get("size") or 0))
        if source != "auto" and lang == source:
            return (1, auto, -int(meta.get("size") or 0))
        if lang == "en":
            return (2, auto, -int(meta.get("size") or 0))
        if lang in languages:
            return (3, auto, -int(meta.get("size") or 0))
        return (9, auto, -int(meta.get("size") or 0))

    def download_platform_subtitles(task: dict[str, Any], work: Path, cookie: Path | None, source: str, target: str) -> list[dict[str, Any]]:
        candidates = language_candidates(source, target)
        sub_langs = ",".join(candidates)
        command = core.task_base(task, cookie, False) + [
            "--skip-download", "--write-subs", "--write-auto-subs",
            "--sub-langs", sub_langs,
            "--sub-format", "srt/best", "--convert-subs", "srt",
            "--output", str(work / "%(title).120B.%(language)s.%(ext)s"), task["url"],
        ]
        result = run_task(task, work, command, 180)
        files = [detect_subtitle_meta(path) for path in work.rglob("*.srt") if path.is_file()]
        if files:
            return files
        if result.returncode and core.denied(result.stderr or result.stdout):
            retry = core.task_base(task, cookie, True) + command[len(core.task_base(task, cookie, False)):]
            run_task(task, work, retry, 180)
            files = [detect_subtitle_meta(path) for path in work.rglob("*.srt") if path.is_file()]
        return files

    def choose_source_subtitle(files: list[dict[str, Any]], source: str, target: str, prefer_target: bool) -> dict[str, Any] | None:
        if not files:
            return None
        ranked = sorted(files, key=lambda item: score_subtitle(item, source, target, prefer_target))
        return ranked[0]

    def parse_srt(text: str) -> list[dict[str, str]]:
        blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").strip())
        cues: list[dict[str, str]] = []
        for block in blocks:
            lines = [line for line in block.split("\n") if line.strip() != "" or True]
            lines = block.split("\n")
            if len(lines) < 2:
                continue
            idx = 0
            if re.fullmatch(r"\d+", lines[0].strip()):
                idx = 1
            if idx >= len(lines) or "-->" not in lines[idx]:
                continue
            timing = lines[idx].strip()
            match = re.match(r"(.+?)\s*-->\s*(.+)", timing)
            if not match:
                continue
            body = "\n".join(lines[idx + 1:]).strip()
            if not body:
                continue
            cues.append({
                "id": str(len(cues) + 1),
                "start": match.group(1).strip(),
                "end": match.group(2).strip(),
                "text": body,
            })
        if not cues:
            raise RuntimeError(json.dumps({"code": "SUBTITLE_PARSE_FAILED", "message": "字幕解析失败"}, ensure_ascii=False))
        return cues

    def write_srt(path: Path, cues: list[dict[str, str]], text_key: str = "text") -> None:
        lines: list[str] = []
        for index, cue in enumerate(cues, 1):
            lines.extend([str(index), f"{cue['start']} --> {cue['end']}", str(cue.get(text_key) or cue.get("text") or "").strip(), ""])
        path.write_text("\n".join(lines), encoding="utf-8")

    def write_bilingual_srt(path: Path, cues: list[dict[str, str]]) -> None:
        lines: list[str] = []
        for index, cue in enumerate(cues, 1):
            original = str(cue.get("text") or "").strip()
            translated = str(cue.get("translated") or "").strip()
            body = original if not translated or translated == original else f"{original}\n{translated}"
            lines.extend([str(index), f"{cue['start']} --> {cue['end']}", body, ""])
        path.write_text("\n".join(lines), encoding="utf-8")

    def batch_cues(cues: list[dict[str, str]]) -> list[list[dict[str, str]]]:
        batches: list[list[dict[str, str]]] = []
        current: list[dict[str, str]] = []
        chars = 0
        for cue in cues:
            text = cue["text"]
            if current and (len(current) >= BATCH_MAX_CUES or chars + len(text) > BATCH_MAX_CHARS):
                batches.append(current)
                current = []
                chars = 0
            current.append(cue)
            chars += len(text)
        if current:
            batches.append(current)
        return batches

    def extract_json_payload(content: str) -> dict[str, Any]:
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{[\s\S]*\}", text)
            if not match:
                raise
            return json.loads(match.group(0))

    def validate_translations(batch: list[dict[str, str]], payload: dict[str, Any]) -> dict[str, str]:
        items = payload.get("translations")
        if not isinstance(items, list):
            raise RuntimeError("SUBTITLE_TRANSLATION_RESPONSE_INVALID")
        mapping: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                raise RuntimeError("SUBTITLE_TRANSLATION_RESPONSE_INVALID")
            item_id = str(item.get("id") or "").strip()
            text = str(item.get("text") or "").strip()
            if not item_id or not text:
                raise RuntimeError("SUBTITLE_TRANSLATION_EMPTY")
            if item_id in mapping:
                raise RuntimeError("SUBTITLE_TRANSLATION_ID_MISMATCH")
            mapping[item_id] = text
        expected = {cue["id"] for cue in batch}
        if set(mapping) != expected:
            raise RuntimeError("SUBTITLE_TRANSLATION_ID_MISMATCH")
        return mapping

    def call_translation_model(model: str, key: str, source_language: str, target_language: str, batch: list[dict[str, str]]) -> dict[str, str]:
        source_label = languages.get(source_language, source_language or "auto")
        target_label = languages.get(target_language, target_language)
        payload_items = [{"id": cue["id"], "text": cue["text"]} for cue in batch]
        system_prompt = (
            "You are a subtitle translator. Translate subtitle text only. "
            "Keep natural spoken language. Do not explain. Do not summarize. "
            "Do not add content. Preserve IDs exactly. Return pure JSON only."
        )
        user_prompt = json.dumps({
            "source_language": source_language or "auto",
            "source_language_label": source_label,
            "target_language": target_language,
            "target_language_label": target_label,
            "items": payload_items,
            "response_schema": {"translations": [{"id": "1", "text": "..."}]},
        }, ensure_ascii=False)
        try:
            response = requests.post(
                CHAT_ENDPOINT,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "temperature": 0.1,
                    "stream": False,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                },
                timeout=(20, 120),
            )
        except requests.Timeout as exc:
            raise RuntimeError("SILICONFLOW_TRANSLATION_TIMEOUT") from exc
        except requests.RequestException as exc:
            raise RuntimeError(f"SILICONFLOW_TRANSLATION_FAILED:{type(exc).__name__}") from exc
        if response.status_code in {401, 403}:
            raise RuntimeError("SILICONFLOW_TRANSLATION_UNAUTHORIZED")
        if response.status_code == 429:
            raise RuntimeError("SILICONFLOW_TRANSLATION_RATE_LIMITED")
        if not response.ok:
            raise RuntimeError(f"SILICONFLOW_TRANSLATION_HTTP_{response.status_code}")
        data = response.json()
        content = ""
        try:
            content = data["choices"][0]["message"]["content"]
        except Exception as exc:
            raise RuntimeError("SUBTITLE_TRANSLATION_RESPONSE_INVALID") from exc
        parsed = extract_json_payload(str(content or ""))
        return validate_translations(batch, parsed)

    def translate_cues(task: dict[str, Any], cues: list[dict[str, str]], source_language: str, target_language: str, key: str) -> tuple[list[dict[str, str]], list[str], bool]:
        settings = load_translation_settings()
        if not settings["translation_enabled"]:
            raise RuntimeError(json.dumps({"code": "SUBTITLE_TRANSLATION_DISABLED", "message": "字幕翻译未启用"}, ensure_ascii=False))
        models = [settings["translation_model"], *settings["translation_fallback_models"]]
        models = [item for index, item in enumerate(models) if item in ALLOWED_TRANSLATION_MODELS and item not in models[:index]]
        batches = batch_cues(cues)
        used_models: list[str] = []
        translated = [dict(cue) for cue in cues]
        index_map = {cue["id"]: i for i, cue in enumerate(translated)}
        for batch_index, batch in enumerate(batches, 1):
            ensure_not_cancelled(task["id"])
            core.patch(task["id"], status="processing", progress=45 + batch_index / max(len(batches), 1) * 50, eta=f"正在翻译字幕 {batch_index}/{len(batches)}")
            success = None
            last_error = "SUBTITLE_TRANSLATION_FAILED"
            for model in models:
                for attempt in range(2):
                    try:
                        mapping = call_translation_model(model, key, source_language, target_language, batch)
                        for cue in batch:
                            translated[index_map[cue["id"]]]["translated"] = mapping[cue["id"]]
                        if model not in used_models:
                            used_models.append(model)
                        success = True
                        break
                    except Exception as exc:
                        last_error = str(exc)
                        continue
                if success:
                    break
            if not success:
                raise RuntimeError(json.dumps({"code": "SUBTITLE_TRANSLATION_FAILED", "message": "翻译失败，已保留原字幕", "detail": last_error[:200]}, ensure_ascii=False))
        return translated, used_models, True

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
        if core.is_guest_task(task):
            policy = core.task_policy(task)
            maximum = policy["ai_transcription_max_duration_minutes"] * 60
            if total > maximum:
                raise RuntimeError(json.dumps({
                    "code": "GUEST_AI_DURATION_LIMIT",
                    "message": f"游客 AI 字幕最长支持 {int(policy['ai_transcription_max_duration_minutes'])} 分钟。",
                }, ensure_ascii=False))
        elif total > 3600.5:
            raise RuntimeError("AUDIO_TOO_LONG")
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
                ASR_ENDPOINT,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (path.name, audio, "audio/mpeg"), "model": (None, ASR_MODEL)},
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

    def create_asr_srt(task: dict[str, Any], work: Path, chunks: list[Path], total: float, key: str) -> Path:
        cues: list[dict[str, str]] = []
        for index, chunk in enumerate(chunks):
            ensure_not_cancelled(task["id"])
            core.patch(task["id"], status="processing", progress=20 + index / max(len(chunks), 1) * 20, eta=f"AI 转写 {index + 1}/{len(chunks)}")
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
                cues.append({
                    "id": str(len(cues) + 1),
                    "start": srt_time(cursor),
                    "end": srt_time(cue_end),
                    "text": part,
                })
                cursor = cue_end
        path = work / f"{safe_name(task.get('title'))}.original.auto.srt"
        write_srt(path, cues)
        return path

    def finish(task: dict[str, Any], primary: Path, files: list[Path], warning: dict[str, str] | None = None) -> None:
        ensure_not_cancelled(task["id"])
        destination = core.DL / task["id"]
        destination.mkdir(parents=True, exist_ok=True)
        primary_target: Path | None = None
        total_size = 0
        for source in files:
            target = destination / source.name
            shutil.move(str(source), target)
            total_size += target.stat().st_size
            if source.name == primary.name:
                primary_target = target
        shutil.rmtree(core.TMP / task["id"], ignore_errors=True)
        if core.is_guest_task(task) and core.guest_task_size_exceeded(task, destination):
            shutil.rmtree(destination, ignore_errors=True)
            raise core.guest_limit_failure(task)
        patch = {
            "progress": 100,
            "eta": "",
            "output_path": str(primary_target),
            "output_size": total_size,
            "status": "completed",
            "finished": core.now(),
        }
        if warning:
            patch["error_code"] = warning.get("code")
            patch["error_message"] = warning.get("message")
        else:
            patch["error_code"] = None
            patch["error_message"] = None
        core.patch(task["id"], **patch)
        auto_save = getattr(core, "auto_save_subtitles_to_koofr", None)
        if auto_save and not core.is_guest_task(task):
            auto_save(task["id"])

    def build_outputs(task: dict[str, Any], work: Path, source_path: Path, source_language: str, source_type: str, key: str | None) -> tuple[Path, list[Path], dict[str, str] | None]:
        opts = task_subtitle_options(task)
        target = opts["subtitle_target_language"]
        mode = opts["subtitle_output_mode"]
        title = safe_name(task.get("title"))
        cues = parse_srt(source_path.read_text(encoding="utf-8", errors="replace"))
        original = work / f"{title}.original.{source_language or 'und'}.srt"
        write_srt(original, cues)
        files = [original]
        primary = original
        warning = None
        models_used: list[str] = []
        translated_flag = False
        same_language = source_language in languages and normalize_lang(source_language) == target
        need_translation = mode in {"translated", "bilingual"} and not same_language
        if mode == "translated" and normalize_lang(source_language) == target:
            primary = original
            need_translation = False
        if need_translation:
            if not key:
                raise RuntimeError("SILICONFLOW_KEY_MISSING")
            settings = load_translation_settings()
            if not settings["translation_enabled"]:
                raise RuntimeError(json.dumps({"code": "SUBTITLE_TRANSLATION_DISABLED", "message": "字幕翻译未启用"}, ensure_ascii=False))
            translation_slot = False
            try:
                if core.is_guest_task(task):
                    policy = core.task_policy(task)
                    if not policy.get("allow_subtitle_translation", True):
                        raise RuntimeError(json.dumps({"code": "GUEST_TRANSLATION_DISABLED", "message": "当前未启用游客字幕翻译"}, ensure_ascii=False))
                    translation_slot = core.start_guest_translation(task)
                translated_cues, models_used, translated_flag = translate_cues(task, cues, source_language, target, key)
                translated_path = work / f"{title}.translated.{target}.srt"
                write_srt(translated_path, [
                    {**cue, "text": cue.get("translated") or cue["text"]} for cue in translated_cues
                ])
                files.append(translated_path)
                primary = translated_path
                if mode == "bilingual":
                    bilingual_path = work / f"{title}.bilingual.{source_language or 'und'}-{target}.srt"
                    write_bilingual_srt(bilingual_path, translated_cues)
                    files.append(bilingual_path)
                    primary = bilingual_path
            except Exception as exc:
                try:
                    parsed = json.loads(str(exc))
                    code, message = parsed.get("code") or "SUBTITLE_TRANSLATION_FAILED", parsed.get("message") or "翻译失败，已保留原字幕"
                except Exception:
                    code, message = friendly_error(str(exc), task.get("url") or "")
                    if code not in {
                        "SUBTITLE_TRANSLATION_FAILED", "SUBTITLE_TRANSLATION_DISABLED",
                        "GUEST_TRANSLATION_DISABLED", "GUEST_TRANSLATION_HOURLY_LIMIT",
                        "GUEST_TRANSLATION_BUSY", "GUEST_TRANSLATION_DURATION_LIMIT",
                        "SILICONFLOW_KEY_MISSING", "SILICONFLOW_TRANSLATION_UNAUTHORIZED",
                        "SILICONFLOW_TRANSLATION_RATE_LIMITED", "SILICONFLOW_TRANSLATION_TIMEOUT",
                    }:
                        code, message = "SUBTITLE_TRANSLATION_FAILED", "翻译失败，已保留原字幕"
                if code in {"GUEST_TRANSLATION_DISABLED", "GUEST_TRANSLATION_HOURLY_LIMIT", "GUEST_TRANSLATION_BUSY", "GUEST_TRANSLATION_DURATION_LIMIT", "SILICONFLOW_KEY_MISSING"}:
                    raise RuntimeError(json.dumps({"code": code, "message": message}, ensure_ascii=False))
                warning = {"code": "SUBTITLE_TRANSLATION_FAILED", "message": "翻译失败，已保留原字幕"}
                primary = original
            finally:
                if translation_slot:
                    core.release_guest_translation_slot(task["id"])
        elif mode == "bilingual" and same_language:
            primary = original
        meta = {
            "source_language": source_language or "und",
            "target_language": target,
            "output_mode": mode,
            "primary_model": (models_used[0] if models_used else None),
            "models_used": models_used,
            "cue_count": len(cues),
            "translated": translated_flag,
            "source_type": source_type,
            "translation_skipped_same_language": bool(same_language and mode != "original"),
        }
        meta_path = work / f"{title}.translation.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        if mode != "original" or translated_flag:
            files.append(meta_path)
        return primary, files, warning

    def subtitle_execute(task_id: str) -> None:
        task = core.row(task_id)
        if not task or task.get("status") == "cancelled":
            return
        work = core.TMP / task_id
        work.mkdir(parents=True, exist_ok=True)
        cookie = None
        ai_slot = False
        try:
            opts = task_subtitle_options(task)
            source = opts["subtitle_source_language"]
            target = opts["subtitle_target_language"]
            mode = opts["subtitle_output_mode"]
            cookie = core.cpath(task["options"].get("cookie_id")) if not core.is_guest_task(task) else None
            core.patch(task_id, status="processing", progress=5, eta="正在检查平台字幕", error_code=None, error_message=None)
            platform_files = download_platform_subtitles(task, work, cookie, source, target)
            source_meta = None
            source_type = "none"
            source_path = None
            if platform_files:
                if mode == "translated":
                    target_hit = choose_source_subtitle(platform_files, source, target, prefer_target=True)
                    if target_hit and normalize_lang(target_hit.get("language")) == target:
                        source_meta = target_hit
                        source_type = "platform_target_auto" if target_hit.get("auto") else "platform_target_manual"
                    else:
                        source_meta = choose_source_subtitle(platform_files, source, target, prefer_target=False)
                        source_type = "platform_auto" if source_meta and source_meta.get("auto") else "platform_manual"
                else:
                    source_meta = choose_source_subtitle(platform_files, source, target, prefer_target=False)
                    source_type = "platform_auto" if source_meta and source_meta.get("auto") else "platform_manual"
                if source_meta:
                    source_path = source_meta["path"]
            if source_path is None:
                key = get_key()
                if not key:
                    raise RuntimeError("SILICONFLOW_KEY_MISSING")
                if core.is_guest_task(task):
                    ai_slot = core.start_guest_ai(task)
                core.patch(task_id, status="downloading", progress=15, eta="平台无字幕，正在下载音频")
                audio = download_audio(task, work, cookie)
                total = duration(audio)
                core.patch(task_id, status="processing", progress=30, eta="正在准备 AI 转写音频")
                chunks = make_chunks(task, audio, work, total)
                source_path = create_asr_srt(task, work, chunks, total, key)
                source_type = "sensevoice"
                source_language = "auto"
            else:
                source_language = normalize_lang(source_meta.get("language") if source_meta else "und") or "und"
                key = get_key()
            # direct target platform subtitle for translated mode without translation API
            if mode == "translated" and source_type.startswith("platform_target"):
                title = safe_name(task.get("title"))
                primary = work / f"{title}.translated.{target}.srt"
                primary.write_text(source_path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
                original = work / f"{title}.original.{source_language}.srt"
                original.write_text(source_path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
                meta_path = work / f"{title}.translation.json"
                meta_path.write_text(json.dumps({
                    "source_language": source_language,
                    "target_language": target,
                    "output_mode": mode,
                    "primary_model": None,
                    "models_used": [],
                    "cue_count": len(parse_srt(primary.read_text(encoding="utf-8", errors="replace"))),
                    "translated": False,
                    "source_type": source_type,
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                finish(task, primary, [original, primary, meta_path])
                return
            primary, files, warning = build_outputs(task, work, source_path, source_language if source_path else "auto", source_type, get_key())
            finish(task, primary, files, warning)
        except Exception as exc:
            if (core.row(task_id) or {}).get("status") == "cancelled":
                return
            try:
                parsed = json.loads(str(exc))
                code, message = parsed["code"], parsed["message"]
            except Exception:
                code, message = friendly_error(str(exc), task["url"])
            if core.is_guest_task(task):
                if code in {
                    "SILICONFLOW_KEY_MISSING", "SILICONFLOW_UNAUTHORIZED", "SILICONFLOW_RATE_LIMITED",
                    "SILICONFLOW_TRANSLATION_UNAUTHORIZED", "SILICONFLOW_TRANSLATION_RATE_LIMITED",
                    "SILICONFLOW_TRANSLATION_TIMEOUT",
                }:
                    message = "当前游客字幕处理暂不可用，请稍后重试。"
                elif not code.startswith("GUEST_") and not code.startswith("guest_") and not code.startswith("SUBTITLE_"):
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
        translation = load_translation_settings()
        return {
            "configured": configured,
            "model": ASR_MODEL,
            "asr_model": ASR_MODEL,
            "source": "environment" if os.getenv("SILICONFLOW_API_KEY", "").strip() else ("ui" if configured else "none"),
            "translation_enabled": translation["translation_enabled"],
            "translation_model": translation["translation_model"],
            "translation_fallback_models": translation["translation_fallback_models"],
            "supported_translation_models": list(ALLOWED_TRANSLATION_MODELS),
            "supported_languages": dict(languages),
        }

    def settings_put(payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("clear"):
            clear_key()
        value = str(payload.get("api_key") or "").strip()
        if value:
            if len(value) < 10:
                raise ValueError("API Key 格式不正确")
            set_key(value)
        if any(key in payload for key in ("translation_enabled", "translation_model", "translation_fallback_models")):
            save_translation_settings(payload)
        return settings_get()

    def add_route(path: str, endpoint: Any, methods: list[str]) -> None:
        core.app.add_api_route(path, endpoint, methods=methods, dependencies=[core.Depends(core.auth)])
        route = core.app.router.routes.pop()
        index = next((i for i, item in enumerate(core.app.router.routes) if getattr(item, "path", None) in {"/", "/index.html", "/admin", "/admin/", "/admin.html"}), len(core.app.router.routes))
        core.app.router.routes.insert(index, route)

    core.err = friendly_error
    core.execute = execute
    add_route("/api/admin/transcription/settings", settings_get, ["GET"])
    add_route("/api/admin/transcription/settings", settings_put, ["PUT"])
    add_route("/api/transcription/settings", settings_get, ["GET"])
    add_route("/api/transcription/settings", settings_put, ["PUT"])
