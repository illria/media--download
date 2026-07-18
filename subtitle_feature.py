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
        if "translation_fallback_models" in data:
            source_items = data.get("translation_fallback_models")
            if not isinstance(source_items, list):
                source_items = []
        else:
            source_items = list(DEFAULT_FALLBACK_MODELS)
        fallbacks = []
        for item in source_items:
            name = str(item or "").strip()
            if name in ALLOWED_TRANSLATION_MODELS and name != model and name not in fallbacks:
                fallbacks.append(name)
            if len(fallbacks) >= 3:
                break
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
            items = payload.get("translation_fallback_models")
            if items is None:
                items = []
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
            "platform_auto_translated_subtitle_used": ("PLATFORM_AUTO_TRANSLATED_SUBTITLE_USED", "已使用平台自动翻译字幕，质量可能不稳定。"),
            "subtitle_translation_quality_failed": ("SUBTITLE_TRANSLATION_QUALITY_FAILED", "翻译质量未达标，已保留原字幕。"),
            "subtitle_translation_structure_failed": ("SUBTITLE_TRANSLATION_STRUCTURE_FAILED", "翻译结果结构异常，已保留原字幕。"),
            "subtitle_source_quality_failed": ("SUBTITLE_SOURCE_QUALITY_FAILED", "平台字幕质量异常，未继续翻译。"),
            "subtitle_source_language_uncertain": ("SUBTITLE_SOURCE_LANGUAGE_UNCERTAIN", "无法可靠识别源语言，未继续翻译。"),
            "asr_quality_failed": ("ASR_QUALITY_FAILED", "语音识别结果质量过低，未生成字幕。"),
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
            "bn-bd": "bn", "bn-in": "bn", "bangla": "bn", "bengali": "bn",
        }
        lowered = value.lower()
        if value in languages:
            return value
        if lowered in aliases:
            return aliases[lowered]
        lowered_map = {key.lower(): key for key in languages}
        if lowered in lowered_map:
            return lowered_map[lowered]
        base = lowered.split("-", 1)[0]
        for key in languages:
            if key.lower() == lowered or key.lower().startswith(base + "-") or key.lower() == base:
                return key
        return value

    def task_subtitle_options(task: dict[str, Any]) -> dict[str, str]:
        options = task.get("options") if isinstance(task.get("options"), dict) else {}
        has_new_fields = any(key in options for key in (
            "subtitle_output_mode", "subtitle_source_language", "subtitle_target_language",
        ))
        source = str(options.get("subtitle_source_language") or "auto")
        target = normalize_lang(options.get("subtitle_target_language") or "zh-CN") or "zh-CN"
        if has_new_fields:
            mode = str(options.get("subtitle_output_mode") or "translated").lower()
        else:
            mode = "original"
        if mode not in {"original", "translated", "bilingual"}:
            mode = "translated" if has_new_fields else "original"
        if source != "auto":
            source = normalize_lang(source) or "auto"
        return {
            "subtitle_source_language": source,
            "subtitle_target_language": target if target in languages else "zh-CN",
            "subtitle_output_mode": mode,
        }

    def fetch_media_subtitle_catalog(task: dict[str, Any], cookie: Path | None) -> dict[str, Any]:
        empty = {"media_language": "", "manual": [], "auto": [], "tracks": [], "raw": {}}
        command = core.task_base(task, cookie, False) + ["--dump-single-json", "--skip-download", "--no-warnings", task["url"]]
        try:
            result = run_task(task, core.TMP / task["id"], command, 90)
        except Exception:
            return empty
        if result.returncode:
            if core.denied(result.stderr or result.stdout):
                retry = core.task_base(task, cookie, True) + command[len(core.task_base(task, cookie, False)):]
                try:
                    result = run_task(task, core.TMP / task["id"], retry, 90)
                except Exception:
                    return empty
            if result.returncode:
                return empty
        try:
            raw = json.loads(result.stdout or "{}")
        except Exception:
            return empty

        def parse_tracks(bucket: dict[str, Any] | None, manual: bool) -> list[dict[str, Any]]:
            tracks: list[dict[str, Any]] = []
            for code, entries in (bucket or {}).items():
                raw_code = str(code or "")
                lowered = raw_code.lower()
                names: list[str] = []
                if isinstance(entries, list):
                    for entry in entries:
                        if isinstance(entry, dict):
                            for key in ("name", "ext", "format"):
                                if entry.get(key):
                                    names.append(str(entry.get(key)))
                joined = " ".join(names)
                is_orig_code = lowered.endswith("-orig") or lowered.endswith(".orig") or "-orig-" in lowered
                is_orig_name = bool(re.search(r"\boriginal\b|原始|原文", joined, re.I))
                normalized = normalize_lang(raw_code.replace("-orig", "").replace(".orig", ""))
                tracks.append({
                    "normalized_language": normalized if normalized and normalized != "und" else raw_code,
                    "raw_language_code": raw_code,
                    "track_name": joined[:200],
                    "manual": manual,
                    "original": bool(is_orig_code or is_orig_name),
                    "auto_translated": (not manual) and not (is_orig_code or is_orig_name),
                })
            return tracks

        manual_tracks = parse_tracks(raw.get("subtitles") or {}, True)
        auto_tracks = parse_tracks(raw.get("automatic_captions") or {}, False)
        tracks = manual_tracks + auto_tracks
        media_language = normalize_lang(raw.get("language") or raw.get("original_language") or "")
        if not media_language or media_language == "und":
            original_track = next((item for item in tracks if item.get("original")), None)
            media_language = (original_track or {}).get("normalized_language") or ""
        if media_language == "und":
            media_language = ""
        for item in tracks:
            lang = item.get("normalized_language") or ""
            if media_language and lang == media_language:
                item["original"] = True
                item["auto_translated"] = False
            elif not item.get("original") and not item.get("manual") and media_language and lang and lang != media_language:
                item["auto_translated"] = True
        return {
            "media_language": media_language,
            "manual": sorted({item["normalized_language"] for item in manual_tracks if item.get("normalized_language")}),
            "auto": sorted({item["normalized_language"] for item in auto_tracks if item.get("normalized_language")}),
            "tracks": tracks,
            "raw": {"id": raw.get("id"), "title": raw.get("title"), "duration": raw.get("duration")},
        }

    def language_candidates(source: str, target: str, media_language: str = "", catalog: dict[str, Any] | None = None) -> list[str]:
        catalog = catalog or {}
        tracks = list(catalog.get("tracks") or [])

        def add(code: str, bucket: list[str]) -> None:
            code = str(code or "").strip()
            if not code or code == "auto" or code in bucket:
                return
            bucket.append(code)

        priority: list[str] = []
        # 1) exact original raw codes
        for item in tracks:
            if item.get("original"):
                add(item.get("raw_language_code"), priority)
                add(item.get("normalized_language"), priority)
        # 2) media language
        add(media_language, priority)
        # 3) explicit source language
        if source and source != "auto":
            for item in tracks:
                item_lang = normalize_lang(item.get("normalized_language"))
                item_raw = str(item.get("raw_language_code") or "")
                if item_lang == normalize_lang(source) or item_raw.lower() == str(source).lower() or item_raw.lower().startswith(str(source).lower() + "-"):
                    add(item_raw, priority)
                    add(item_lang, priority)
            add(source, priority)
        # 4/5) target manual then target auto
        for item in tracks:
            if normalize_lang(item.get("normalized_language")) == normalize_lang(target) and item.get("manual"):
                add(item.get("raw_language_code"), priority)
                add(item.get("normalized_language"), priority)
        for item in tracks:
            if normalize_lang(item.get("normalized_language")) == normalize_lang(target) and not item.get("manual"):
                add(item.get("raw_language_code"), priority)
                add(item.get("normalized_language"), priority)
        add(target, priority)
        # 6/7) english manual then auto
        for item in tracks:
            if normalize_lang(item.get("normalized_language")) == "en" and item.get("manual"):
                add(item.get("raw_language_code"), priority)
                add(item.get("normalized_language"), priority)
        for item in tracks:
            if normalize_lang(item.get("normalized_language")) == "en" and not item.get("manual"):
                add(item.get("raw_language_code"), priority)
                add(item.get("normalized_language"), priority)
        add("en", priority)
        # 8) one real manual fallback from catalog (not hard-coded empty-list branch)
        fallback_candidates = []
        for item in tracks:
            if not item.get("manual"):
                continue
            if item.get("original"):
                continue
            lang = normalize_lang(item.get("normalized_language"))
            if not lang or lang in {normalize_lang(target), "en"}:
                continue
            fallback_candidates.append(item)
        fallback_candidates.sort(key=lambda item: (
            0 if normalize_lang(item.get("normalized_language")) in languages else 1,
            str(item.get("raw_language_code") or ""),
        ))
        if fallback_candidates:
            item = fallback_candidates[0]
            add(item.get("raw_language_code"), priority)
            add(item.get("normalized_language"), priority)
        # Hard cap.
        priority = priority[:8]
        return priority or ["en", "zh-CN", "bn"]

    def detect_subtitle_meta(path: Path, auto: bool = False, media_language: str = "", catalog: dict[str, Any] | None = None) -> dict[str, Any]:
        name = path.name
        lang = "und"
        raw_code = ""
        match = re.search(r"\.([A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?)(?:\.auto)?\.srt$", name, re.I)
        if match:
            raw_code = match.group(1)
            lang = normalize_lang(raw_code.replace("-orig", "")) or raw_code
        else:
            match = re.search(r"\[([A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?)\]", name)
            if match:
                raw_code = match.group(1)
                lang = normalize_lang(raw_code.replace("-orig", "")) or raw_code
        original = bool(media_language and lang == media_language) or bool(re.search(r"-orig|\boriginal\b|原始", name, re.I))
        auto_translated = False
        matched = None
        # Phase 1: exact raw code match.
        for item in (catalog or {}).get("tracks") or []:
            item_raw = str(item.get("raw_language_code") or "")
            if raw_code and item_raw.lower() == raw_code.lower():
                matched = item
                break
        # Phase 2: normalized language only when no exact raw match.
        if matched is None:
            candidates = []
            for item in (catalog or {}).get("tracks") or []:
                item_lang = normalize_lang(item.get("normalized_language"))
                if item_lang == lang:
                    candidates.append(item)
            if candidates:
                candidates.sort(key=lambda item: (
                    0 if item.get("original") else 1,
                    0 if item.get("manual") else 1,
                    1 if item.get("auto_translated") else 0,
                ))
                matched = candidates[0]
        if matched is not None:
            original = bool(matched.get("original") or original)
            auto_translated = bool(matched.get("auto_translated"))
            raw_code = matched.get("raw_language_code") or raw_code
        if auto and media_language and lang and lang != media_language and not original:
            auto_translated = True
        if not media_language and auto and not original:
            # Unknown media language: never invent an original track.
            auto_translated = not bool(re.search(r"-orig|\boriginal\b|原始", name, re.I))
            original = False
        return {
            "path": path,
            "language": lang,
            "raw_language_code": raw_code,
            "auto": bool(auto),
            "manual": not bool(auto),
            "original": bool(original),
            "auto_translated": bool(auto_translated and not original),
            "size": path.stat().st_size if path.is_file() else 0,
        }

    def language_priority(lang: str, source: str, target: str, media_language: str = "") -> int:
        order: list[str] = []
        for item in [media_language, target, source if source and source != "auto" else "", "en", "bn"]:
            code = str(item or "").strip()
            if code and code not in order:
                order.append(code)
        for code in languages:
            if code not in order:
                order.append(code)
        if lang in order:
            return order.index(lang)
        return len(order) + 1

    def score_subtitle(meta: dict[str, Any], source: str, target: str, prefer_target: bool, media_language: str = "") -> tuple:
        lang = normalize_lang(meta.get("language")) or "und"
        auto = bool(meta.get("auto"))
        original = bool(meta.get("original") or (media_language and lang == media_language))
        auto_translated = bool(meta.get("auto_translated"))
        size_rank = -int(meta.get("size") or 0)
        supported = lang in languages
        is_en = lang == "en"
        is_target = bool(target and lang == target)
        is_source = bool(source and source != "auto" and lang == source)

        # Heavy penalty for platform auto-translated non-original tracks.
        translated_penalty = 20 if auto_translated and not original else 0

        if prefer_target:
            if is_target and not auto:
                quality_rank = 0
            elif is_target and auto and original:
                quality_rank = 1
            elif is_target and auto:
                quality_rank = 12  # platform auto-translated target is low priority
            elif original and not auto:
                quality_rank = 2
            elif original and auto:
                quality_rank = 3
            elif is_source and not auto:
                quality_rank = 4
            elif is_source and auto:
                quality_rank = 5
            elif is_en and not auto:
                quality_rank = 6
            elif supported and not auto:
                quality_rank = 7
            elif is_en and auto:
                quality_rank = 8
            elif supported and auto:
                quality_rank = 9
            elif not auto:
                quality_rank = 10
            else:
                quality_rank = 11
        elif source and source != "auto":
            if is_source and not auto:
                quality_rank = 0
            elif is_source and auto:
                quality_rank = 1
            elif original and not auto:
                quality_rank = 2
            elif original and auto:
                quality_rank = 3
            elif is_en and not auto:
                quality_rank = 4
            elif supported and not auto:
                quality_rank = 5
            elif is_en and auto:
                quality_rank = 6
            elif supported and auto:
                quality_rank = 7
            elif not auto:
                quality_rank = 8
            else:
                quality_rank = 9
        else:
            # source=auto: original language first, then English, then others. Never prefer random auto-translated tracks.
            if original and not auto:
                quality_rank = 0
            elif original and auto:
                quality_rank = 1
            elif is_en and not auto:
                quality_rank = 2
            elif supported and not auto:
                quality_rank = 3
            elif is_en and auto:
                quality_rank = 4
            elif supported and auto and not auto_translated:
                quality_rank = 5
            elif supported and auto:
                quality_rank = 8
            elif not auto:
                quality_rank = 6
            else:
                quality_rank = 7

        return (quality_rank + translated_penalty, language_priority(lang, source, target, media_language), size_rank)

    def download_platform_subtitles(task: dict[str, Any], work: Path, cookie: Path | None, source: str, target: str, media_language: str = "", catalog: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        candidates = language_candidates(source, target, media_language, catalog=catalog)
        sub_langs = ",".join(candidates)
        files: list[dict[str, Any]] = []

        def run_kind(kind: str, write_flag: str, auto: bool) -> None:
            target_dir = work / kind
            target_dir.mkdir(parents=True, exist_ok=True)
            command = core.task_base(task, cookie, False) + [
                "--skip-download", write_flag,
                "--sub-langs", sub_langs,
                "--sub-format", "srt/best", "--convert-subs", "srt",
                "--output", str(target_dir / "%(title).100B.%(language)s.%(ext)s"), task["url"],
            ]
            result = run_task(task, work, command, 180)
            found = [detect_subtitle_meta(path, auto=auto, media_language=media_language, catalog=catalog) for path in target_dir.rglob("*.srt") if path.is_file()]
            if found:
                files.extend(found)
                return
            if result.returncode and core.denied(result.stderr or result.stdout):
                retry = core.task_base(task, cookie, True) + command[len(core.task_base(task, cookie, False)):]
                run_task(task, work, retry, 180)
                files.extend(detect_subtitle_meta(path, auto=auto, media_language=media_language, catalog=catalog) for path in target_dir.rglob("*.srt") if path.is_file())

        core.patch(task["id"], status="processing", progress=8, eta="正在检查原始平台字幕")
        run_kind("platform-manual", "--write-subs", False)
        run_kind("platform-auto", "--write-auto-subs", True)
        return files

    def choose_source_subtitle(files: list[dict[str, Any]], source: str, target: str, prefer_target: bool, media_language: str = "") -> dict[str, Any] | None:
        if not files:
            return None
        ranked = sorted(files, key=lambda item: score_subtitle(item, source, target, prefer_target, media_language))
        return ranked[0]

    def cue_duration_seconds(cue: dict[str, str]) -> float:
        def parse_ts(value: str) -> float:
            text = value.strip().replace(",", ".")
            parts = text.split(":")
            if len(parts) != 3:
                return 0.0
            try:
                hours = float(parts[0]); minutes = float(parts[1]); seconds = float(parts[2])
                return hours * 3600 + minutes * 60 + seconds
            except ValueError:
                return 0.0
        return max(0.0, parse_ts(cue.get("end", "")) - parse_ts(cue.get("start", "")))

    def text_metrics(text: str) -> dict[str, float]:
        value = text or ""
        compact = value.strip()
        total = max(len(value), 1)
        letters = 0
        digits = 0
        emoji = 0
        spaces = 0
        for ch in value:
            code = ord(ch)
            if ch.isspace():
                spaces += 1
            elif ch.isdigit() or ("０" <= ch <= "９"):
                digits += 1
            elif (
                ch.isalpha()
                or ("一" <= ch <= "鿿")
                or ("ঀ" <= ch <= "৿")
                or ("؀" <= ch <= "ۿ")
                or ("Ѐ" <= ch <= "ӿ")
                or ("가" <= ch <= "힣")
            ):
                letters += 1
            elif code > 0x1F300:
                emoji += 1
        meaningful = letters + digits

        def meaningful_char(ch: str) -> bool:
            return (
                ch.isalnum()
                or ("一" <= ch <= "鿿")
                or ("ঀ" <= ch <= "৿")
                or ("؀" <= ch <= "ۿ")
                or ("Ѐ" <= ch <= "ӿ")
                or ("가" <= ch <= "힣")
            )

        is_punct_only = bool(compact) and all((not meaningful_char(ch)) and ord(ch) <= 0x1F300 for ch in compact)
        is_emoji_only = bool(compact) and letters == 0 and digits == 0 and any(ord(ch) > 0x1F300 for ch in compact) and all((ord(ch) > 0x1F300 or not meaningful_char(ch)) for ch in compact)
        return {
            "length": float(len(value)),
            "letter_ratio": meaningful / total,
            "emoji_ratio": emoji / total,
            "punct_ratio": max(0.0, 1.0 - meaningful / total - spaces / total),
            "space_ratio": spaces / total,
            "is_emoji_only": is_emoji_only,
            "is_punct_only": is_punct_only,
            "is_single_char": len(compact) <= 1,
            "has_kana": any("ぁ" <= ch <= "ヿ" for ch in value),
            "has_cjk": any("一" <= ch <= "鿿" for ch in value),
            "has_latin": any(("a" <= ch.lower() <= "z") for ch in value if ch.isalpha()),
            "has_bengali": any("ঀ" <= ch <= "৿" for ch in value),
            "has_arabic": any("؀" <= ch <= "ۿ" for ch in value),
            "has_cyrillic": any("Ѐ" <= ch <= "ӿ" for ch in value),
            "has_hangul": any("가" <= ch <= "힣" for ch in value),
            "has_digit": digits > 0,
        }

    def script_match_ratio(metrics_list: list[dict[str, float]], expected_language: str) -> float:
        if not metrics_list:
            return 0.0
        lang = (expected_language or "").lower()

        def ok(item: dict[str, float]) -> bool:
            if lang.startswith("zh"):
                return bool(item.get("has_cjk"))
            if lang.startswith("ja"):
                return bool(item.get("has_cjk") or item.get("has_kana"))
            if lang.startswith("ko"):
                return bool(item.get("has_hangul") or item.get("has_cjk"))
            if lang.startswith("bn"):
                return bool(item.get("has_bengali"))
            if lang.startswith("ar"):
                return bool(item.get("has_arabic"))
            if lang.startswith("ru"):
                return bool(item.get("has_cyrillic"))
            if lang.startswith(("en", "fr", "de", "es", "pt", "id", "tr", "it", "vi")):
                return bool(item.get("has_latin") or item.get("has_digit"))
            return True

        candidates = [item for item in metrics_list if item.get("length", 0) >= 2]
        if len(candidates) < 3:
            return 1.0
        return sum(1 for item in candidates if ok(item)) / len(candidates)

    def evaluate_cues_quality(cues: list[dict[str, str]], video_duration: float = 0.0, expected_language: str = "", role: str = "source") -> dict[str, Any]:
        if not cues:
            return {"passed": False, "reason": "empty", "metrics": {"cue_count": 0}}
        durations = [cue_duration_seconds(cue) for cue in cues]
        avg_duration = sum(durations) / max(len(durations), 1)
        max_duration = max(durations) if durations else 0.0
        metrics_list = [text_metrics(str(cue.get("text") or "")) for cue in cues]
        emoji_only = sum(1 for item in metrics_list if item["is_emoji_only"]) / len(metrics_list)
        punct_only = sum(1 for item in metrics_list if item["is_punct_only"]) / len(metrics_list)
        single_char = sum(1 for item in metrics_list if item["is_single_char"]) / len(metrics_list)
        letter_ratio = sum(item["letter_ratio"] for item in metrics_list) / len(metrics_list)
        kana_ratio = sum(1 for item in metrics_list if item["has_kana"]) / len(metrics_list)
        cjk_ratio = sum(1 for item in metrics_list if item["has_cjk"]) / len(metrics_list)
        texts = [str(cue.get("text") or "").strip() for cue in cues]
        unique_ratio = len(set(texts)) / max(len(texts), 1)
        total_chars = sum(len(text) for text in texts)
        script_ratio = script_match_ratio(metrics_list, expected_language)
        metrics = {
            "cue_count": len(cues),
            "video_duration": video_duration,
            "average_cue_duration": avg_duration,
            "maximum_cue_duration": max_duration,
            "emoji_only_ratio": emoji_only,
            "punctuation_only_ratio": punct_only,
            "single_character_ratio": single_char,
            "letter_ratio": letter_ratio,
            "kana_ratio": kana_ratio,
            "cjk_ratio": cjk_ratio,
            "unique_text_ratio": unique_ratio,
            "total_chars": total_chars,
            "script_match_ratio": script_ratio,
            "expected_language": expected_language,
            "role": role,
        }
        failed = False
        reason = ""
        hard = role != "original_download"
        if hard and video_duration >= 300 and len(cues) < 20:
            failed, reason = True, "too_few_cues"
        elif hard and avg_duration > 15:
            failed, reason = True, "average_cue_too_long"
        elif role == "source" and max_duration > 60:
            failed, reason = True, "maximum_cue_too_long"
        elif emoji_only > 0.10:
            failed, reason = True, "emoji_only"
        elif punct_only > 0.10:
            failed, reason = True, "punctuation_only"
        elif single_char > 0.25:
            failed, reason = True, "single_character"
        elif hard and letter_ratio < 0.20:
            failed, reason = True, "low_letter_ratio"
        elif unique_ratio < 0.35:
            failed, reason = True, "duplicate_text"
        elif hard and total_chars < max(20, len(cues)):
            failed, reason = True, "too_little_text"
        elif role in {"source", "translation"} and expected_language and script_ratio < 0.35:
            failed, reason = True, "script_mismatch"
        elif role == "translation" and expected_language.startswith("zh") and cjk_ratio < 0.35:
            failed, reason = True, "not_enough_chinese"
        elif role == "translation" and expected_language.startswith("zh") and kana_ratio > 0.15:
            failed, reason = True, "kana_in_chinese"
        return {"passed": not failed, "reason": reason, "metrics": metrics}

    def assert_source_quality(cues: list[dict[str, str]], video_duration: float = 0.0, language: str = "") -> dict[str, Any]:
        report = evaluate_cues_quality(cues, video_duration=video_duration, expected_language=language, role="source")
        if not report["passed"]:
            raise RuntimeError(json.dumps({
                "code": "SUBTITLE_SOURCE_QUALITY_FAILED",
                "message": "平台字幕质量异常，未继续翻译。",
                "detail": report.get("reason"),
            }, ensure_ascii=False))
        return report

    def assert_translation_quality(source_cues: list[dict[str, str]], translated_cues: list[dict[str, str]], target: str, source_type: str) -> dict[str, Any]:
        if source_type.startswith("platform") and len(translated_cues) != len(source_cues):
            raise RuntimeError(json.dumps({
                "code": "SUBTITLE_TRANSLATION_STRUCTURE_FAILED",
                "message": "翻译结果结构异常，已保留原字幕",
            }, ensure_ascii=False))
        source_ids = [cue["id"] for cue in source_cues]
        translated_ids = [cue["id"] for cue in translated_cues]
        if source_ids != translated_ids:
            raise RuntimeError(json.dumps({
                "code": "SUBTITLE_TRANSLATION_STRUCTURE_FAILED",
                "message": "翻译结果结构异常，已保留原字幕",
            }, ensure_ascii=False))
        for source_cue, translated_cue in zip(source_cues, translated_cues):
            if source_cue.get("start") != translated_cue.get("start") or source_cue.get("end") != translated_cue.get("end"):
                raise RuntimeError(json.dumps({
                    "code": "SUBTITLE_TRANSLATION_STRUCTURE_FAILED",
                    "message": "翻译结果结构异常，已保留原字幕",
                }, ensure_ascii=False))
        texts = [{"text": str(cue.get("translated") or cue.get("text") or "")} for cue in translated_cues]
        # reuse evaluate with translated text
        pseudo = [{"id": cue["id"], "start": cue["start"], "end": cue["end"], "text": str(cue.get("translated") or cue.get("text") or "")} for cue in translated_cues]
        report = evaluate_cues_quality(pseudo, expected_language=target, role="translation")
        if not report["passed"]:
            raise RuntimeError(json.dumps({
                "code": "SUBTITLE_TRANSLATION_QUALITY_FAILED",
                "message": "翻译质量未达标，已保留原字幕",
                "detail": report.get("reason"),
                "translation_quality_metrics": (report.get("metrics") or {}),
            }, ensure_ascii=False))
        return report

    def parse_srt(text: str) -> list[dict[str, str]]:
        normalized = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").strip()
        blocks = re.split(r"\n\s*\n", normalized)
        cues: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        generated = 0
        for block in blocks:
            lines = block.split("\n")
            if len(lines) < 2:
                continue
            idx = 0
            cue_id = None
            first = lines[0].strip()
            if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", first) and "-->" not in first:
                cue_id = first
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
            if not cue_id:
                generated += 1
                cue_id = str(generated)
            if cue_id in seen_ids:
                raise RuntimeError(json.dumps({"code": "SUBTITLE_PARSE_FAILED", "message": "字幕编号重复"}, ensure_ascii=False))
            seen_ids.add(cue_id)
            cues.append({
                "id": cue_id,
                "start": match.group(1).strip(),
                "end": match.group(2).strip(),
                "text": body,
            })
        if not cues:
            raise RuntimeError(json.dumps({"code": "SUBTITLE_PARSE_FAILED", "message": "字幕解析失败"}, ensure_ascii=False))
        return cues

    def write_srt(path: Path, cues: list[dict[str, str]], text_key: str = "text") -> None:
        lines: list[str] = []
        for cue in cues:
            lines.extend([
                str(cue.get("id") or ""),
                f"{cue['start']} --> {cue['end']}",
                str(cue.get(text_key) or cue.get("text") or "").strip(),
                "",
            ])
        path.write_text("\n".join(lines), encoding="utf-8")

    def write_bilingual_srt(path: Path, cues: list[dict[str, str]]) -> None:
        lines: list[str] = []
        for cue in cues:
            original = str(cue.get("text") or "").strip()
            translated = str(cue.get("translated") or "").strip()
            body = original if not translated or translated == original else f"{original}\n{translated}"
            lines.extend([str(cue.get("id") or ""), f"{cue['start']} --> {cue['end']}", body, ""])
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

    def validate_translations(batch: list[dict[str, str]], payload: dict[str, Any], target_language: str = "") -> dict[str, str]:
        items = payload.get("translations")
        if not isinstance(items, list):
            raise RuntimeError(json.dumps({
                "code": "SUBTITLE_TRANSLATION_RESPONSE_INVALID",
                "failure_stage": "batch_validation",
            }, ensure_ascii=False))
        mapping: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                raise RuntimeError(json.dumps({
                    "code": "SUBTITLE_TRANSLATION_RESPONSE_INVALID",
                    "failure_stage": "batch_validation",
                }, ensure_ascii=False))
            item_id = str(item.get("id") or "").strip()
            text = str(item.get("text") or "").strip()
            if not item_id or not text:
                raise RuntimeError(json.dumps({
                    "code": "SUBTITLE_TRANSLATION_EMPTY",
                    "failure_stage": "batch_validation",
                }, ensure_ascii=False))
            if item_id in mapping:
                raise RuntimeError(json.dumps({
                    "code": "SUBTITLE_TRANSLATION_ID_MISMATCH",
                    "failure_stage": "batch_validation",
                }, ensure_ascii=False))
            metrics = text_metrics(text)
            if metrics["is_emoji_only"] or metrics["is_punct_only"]:
                raise RuntimeError(json.dumps({
                    "code": "SUBTITLE_TRANSLATION_QUALITY_FAILED",
                    "failure_stage": "batch_validation",
                }, ensure_ascii=False))
            if target_language.startswith("zh") and metrics["has_kana"] and not metrics["has_cjk"]:
                raise RuntimeError(json.dumps({
                    "code": "SUBTITLE_TRANSLATION_QUALITY_FAILED",
                    "failure_stage": "batch_validation",
                }, ensure_ascii=False))
            mapping[item_id] = text
        expected = {cue["id"] for cue in batch}
        if set(mapping) != expected:
            raise RuntimeError(json.dumps({
                "code": "SUBTITLE_TRANSLATION_ID_MISMATCH",
                "failure_stage": "batch_validation",
            }, ensure_ascii=False))
        return mapping

    def call_translation_model(model: str, key: str, source_language: str, target_language: str, batch: list[dict[str, str]]) -> dict[str, str]:
        source_label = languages.get(source_language, source_language or "auto")
        target_label = languages.get(target_language, target_language)
        payload_items = [{"id": cue["id"], "text": cue["text"]} for cue in batch]
        system_prompt = (
            "You are a subtitle translator. Translate subtitle text only. "
            "Keep natural spoken language. Do not explain. Do not summarize. "
            "Do not add content. Preserve IDs exactly. Return pure JSON only. "
            "Preserve subtitle markup and markers such as <i>...</i>, <b>...</b>, {\\an8}, music symbols, and speaker prefixes. "
            "Do not translate URLs, code snippets, or non-translatable identifiers. "
            "Never modify cue IDs or invent timestamps. "
            "Never replace a sentence with only emoji or punctuation."
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
        try:
            data = response.json()
        except Exception as exc:
            raise RuntimeError(json.dumps({
                "code": "SUBTITLE_TRANSLATION_RESPONSE_INVALID",
                "failure_stage": "response_parse",
            }, ensure_ascii=False)) from exc
        content = ""
        try:
            content = data["choices"][0]["message"]["content"]
        except Exception as exc:
            raise RuntimeError(json.dumps({
                "code": "SUBTITLE_TRANSLATION_RESPONSE_INVALID",
                "failure_stage": "response_parse",
            }, ensure_ascii=False)) from exc
        try:
            parsed = extract_json_payload(str(content or ""))
        except Exception as exc:
            raise RuntimeError(json.dumps({
                "code": "SUBTITLE_TRANSLATION_RESPONSE_INVALID",
                "failure_stage": "response_parse",
            }, ensure_ascii=False)) from exc
        return validate_translations(batch, parsed, target_language=target_language)

    def classify_translation_failure_stage(error: str, *, quality_gate_invoked: bool = False) -> str:
        text = str(error or "")
        # If error is a structured JSON with explicit failure_stage, use it.
        try:
            parsed = json.loads(text)
            explicit = parsed.get("failure_stage")
            if explicit in {"request", "response_parse", "batch_validation", "structure_gate", "full_quality_gate"}:
                return explicit
            code = str(parsed.get("code") or "")
            if code == "SUBTITLE_TRANSLATION_STRUCTURE_FAILED":
                return "structure_gate"
            if code == "SUBTITLE_TRANSLATION_QUALITY_FAILED":
                return "full_quality_gate" if quality_gate_invoked else "batch_validation"
        except Exception:
            pass
        if quality_gate_invoked and "STRUCTURE" in text:
            return "structure_gate"
        if quality_gate_invoked and "QUALITY" in text:
            return "full_quality_gate"
        if any(token in text for token in (
            "SILICONFLOW_TRANSLATION_TIMEOUT",
            "SILICONFLOW_TRANSLATION_UNAUTHORIZED",
            "SILICONFLOW_TRANSLATION_RATE_LIMITED",
            "SILICONFLOW_TRANSLATION_HTTP_",
            "SILICONFLOW_TRANSLATION_FAILED",
        )):
            return "request"
        if "JSONDecodeError" in text:
            return "response_parse"
        if "SUBTITLE_TRANSLATION_RESPONSE_INVALID" in text:
            return "response_parse"
        if any(token in text for token in (
            "SUBTITLE_TRANSLATION_EMPTY",
            "SUBTITLE_TRANSLATION_ID_MISMATCH",
            "SUBTITLE_TRANSLATION_QUALITY_FAILED",
        )):
            return "batch_validation"
        return "request"

    def translate_cues(task: dict[str, Any], cues: list[dict[str, str]], source_language: str, target_language: str, key: str, source_type: str = "") -> dict[str, Any]:
        stage_rank = {
            None: -1,
            "request": 0,
            "response_parse": 1,
            "batch_validation": 2,
            "structure_gate": 3,
            "full_quality_gate": 4,
        }

        def raise_stage_error(code: str, message: str, *, attempted: bool, quality_checked: bool, stage: str | None, detail: str = "", metrics: dict[str, Any] | None = None) -> None:
            raise RuntimeError(json.dumps({
                "code": code,
                "message": message,
                "detail": (detail or "")[:200],
                "translation_attempted": bool(attempted),
                "translation_quality_checked": bool(quality_checked),
                "translation_quality_passed": False,
                "translation_quality_metrics": metrics or {},
                "failure_stage": stage,
            }, ensure_ascii=False))

        if not source_language or source_language in {"auto", "und"}:
            raise_stage_error(
                "SUBTITLE_SOURCE_LANGUAGE_UNCERTAIN",
                "无法可靠识别源语言，未继续翻译。",
                attempted=False,
                quality_checked=False,
                stage=None,
            )
        settings = load_translation_settings()
        if not settings["translation_enabled"]:
            raise_stage_error(
                "SUBTITLE_TRANSLATION_DISABLED",
                "字幕翻译未启用",
                attempted=False,
                quality_checked=False,
                stage=None,
            )
        models = [settings["translation_model"], *settings["translation_fallback_models"]]
        models = [item for index, item in enumerate(models) if item in ALLOWED_TRANSLATION_MODELS and item not in models[:index]]
        # Prefer general models first when source language is uncommon.
        if source_language not in {"zh-CN", "zh-TW", "en", "ja", "ko"}:
            preferred = [item for item in models if item != "tencent/Hunyuan-MT-7B"] + [item for item in models if item == "tencent/Hunyuan-MT-7B"]
            models = preferred or models
        batches = batch_cues(cues)
        used_models: list[str] = []
        index_map = {cue["id"]: i for i, cue in enumerate(cues)}
        translation_attempted = False
        translation_quality_checked = False
        translation_quality_metrics: dict[str, Any] = {}
        failure_stage: str | None = None
        last_error = "SUBTITLE_TRANSLATION_FAILED"

        def record_failure(error: str, *, quality_gate_invoked: bool, metrics: dict[str, Any] | None = None) -> None:
            nonlocal last_error, failure_stage, translation_quality_checked, translation_quality_metrics
            last_error = str(error)
            stage = classify_translation_failure_stage(last_error, quality_gate_invoked=quality_gate_invoked)
            if stage_rank.get(stage, -1) >= stage_rank.get(failure_stage, -1):
                failure_stage = stage
            if quality_gate_invoked:
                # Full model output reached assert_translation_quality().
                translation_quality_checked = True
                if metrics is not None:
                    translation_quality_metrics = metrics

        for model in models:
            candidate = [dict(cue) for cue in cues]
            model_ok = True
            for batch_index, batch in enumerate(batches, 1):
                ensure_not_cancelled(task["id"])
                core.patch(task["id"], status="processing", progress=45 + batch_index / max(len(batches), 1) * 45, eta=f"正在翻译字幕 {batch_index}/{len(batches)}")
                batch_ok = False
                for _attempt in range(2):
                    try:
                        # Mark attempted only immediately before a real model request.
                        translation_attempted = True
                        mapping = call_translation_model(model, key, source_language, target_language, batch)
                        for cue in batch:
                            candidate[index_map[cue["id"]]]["translated"] = mapping[cue["id"]]
                        batch_ok = True
                        break
                    except Exception as exc:
                        record_failure(str(exc), quality_gate_invoked=False)
                        continue
                if not batch_ok:
                    model_ok = False
                    break
            if not model_ok:
                continue
            # All batches completed for this model; full output quality gate runs now.
            try:
                quality = assert_translation_quality(cues, candidate, target_language, source_type)
            except Exception as exc:
                metrics: dict[str, Any] = {}
                try:
                    parsed_quality = json.loads(str(exc))
                    quality_metrics = parsed_quality.get("translation_quality_metrics") or {}
                    if isinstance(quality_metrics, dict) and quality_metrics:
                        metrics = quality_metrics
                except Exception:
                    metrics = {}
                record_failure(str(exc), quality_gate_invoked=True, metrics=metrics)
                continue
            if model not in used_models:
                used_models.append(model)
            return {
                "cues": candidate,
                "models_used": used_models,
                "translated": True,
                "quality": quality,
                "translation_attempted": True,
                "translation_quality_checked": True,
                "translation_quality_passed": bool(quality and quality.get("passed")),
                "translation_quality_metrics": (quality or {}).get("metrics") or {},
                "failure_stage": None,
            }
        if failure_stage == "structure_gate":
            code, message = "SUBTITLE_TRANSLATION_STRUCTURE_FAILED", "翻译结果结构异常，已保留原字幕"
        elif failure_stage in {"full_quality_gate", "batch_validation"}:
            code, message = "SUBTITLE_TRANSLATION_QUALITY_FAILED", "翻译质量未达标，已保留原字幕"
        elif "STRUCTURE" in last_error:
            code, message = "SUBTITLE_TRANSLATION_STRUCTURE_FAILED", "翻译结果结构异常，已保留原字幕"
            failure_stage = failure_stage or "structure_gate"
        elif "QUALITY" in last_error:
            code, message = "SUBTITLE_TRANSLATION_QUALITY_FAILED", "翻译质量未达标，已保留原字幕"
            failure_stage = failure_stage or ("full_quality_gate" if translation_quality_checked else "batch_validation")
        else:
            code, message = "SUBTITLE_TRANSLATION_FAILED", "翻译失败，已保留原字幕"
            if failure_stage is None and translation_attempted:
                failure_stage = "request"
        raise_stage_error(
            code,
            message,
            attempted=translation_attempted,
            quality_checked=translation_quality_checked,
            stage=failure_stage,
            detail=last_error,
            metrics=translation_quality_metrics,
        )
        raise RuntimeError("unreachable")  # pragma: no cover

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
        metrics = text_metrics(text)
        if metrics["is_emoji_only"] or metrics["is_punct_only"] or metrics["letter_ratio"] < 0.2:
            raise RuntimeError(json.dumps({
                "code": "ASR_QUALITY_FAILED",
                "message": "语音识别结果质量过低，未生成字幕。",
            }, ensure_ascii=False))
        return text

    def srt_time(seconds: float) -> str:
        ms = max(0, round(seconds * 1000))
        hours, ms = divmod(ms, 3600000)
        minutes, ms = divmod(ms, 60000)
        secs, ms = divmod(ms, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"

    def pieces(text: str) -> list[str]:
        cleaned = re.sub(r"\s+", " ", text or "").strip()
        items = [item.strip() for item in re.split(r"(?<=[。！？!?；;])\s*", cleaned) if item.strip()]
        output: list[str] = []
        for item in items or ([cleaned] if cleaned else []):
            # Prefer short subtitle lines: 8-24 CJK chars or ~12 latin words-ish by char count.
            while len(item) > 24:
                cut = 20
                output.append(item[:cut].strip())
                item = item[cut:].strip()
            if item:
                output.append(item)
        return output

    def create_asr_srt(task: dict[str, Any], work: Path, chunks: list[Path], total: float, key: str) -> Path:
        cues: list[dict[str, str]] = []
        for index, chunk in enumerate(chunks):
            ensure_not_cancelled(task["id"])
            core.patch(task["id"], status="processing", progress=20 + index / max(len(chunks), 1) * 20, eta=f"平台没有可用字幕，正在尝试 AI 识别 {index + 1}/{len(chunks)}")
            text = transcribe(chunk, key)
            chunk_start = index * 60.0
            chunk_end = min(total, chunk_start + duration(chunk))
            parts = pieces(text)
            if not parts:
                raise RuntimeError(json.dumps({
                    "code": "ASR_QUALITY_FAILED",
                    "message": "语音识别结果质量过低，未生成字幕。",
                }, ensure_ascii=False))
            metrics = text_metrics(text)
            if metrics["is_emoji_only"] or metrics["is_punct_only"] or metrics["letter_ratio"] < 0.18:
                raise RuntimeError(json.dumps({
                    "code": "ASR_QUALITY_FAILED",
                    "message": "语音识别结果质量过低，未生成字幕。",
                }, ensure_ascii=False))
            span = max(chunk_end - chunk_start, 0.1)
            max_cues = max(1, int(span // 2))
            while len(parts) > max_cues and len(parts) > 1:
                merged = []
                i = 0
                while i < len(parts):
                    if i + 1 < len(parts):
                        merged.append((parts[i] + " " + parts[i + 1]).strip())
                        i += 2
                    else:
                        merged.append(parts[i])
                        i += 1
                if len(merged) >= len(parts):
                    break
                parts = merged
            piece_duration = min(10.0, max(2.0, min(7.0, span / max(len(parts), 1))))
            cursor = chunk_start
            for part in parts:
                cue_end = min(chunk_end, cursor + piece_duration)
                if cue_end <= cursor:
                    cue_end = min(chunk_end, cursor + 0.5)
                cues.append({
                    "id": str(len(cues) + 1),
                    "start": srt_time(cursor),
                    "end": srt_time(cue_end),
                    "text": part,
                })
                cursor = cue_end
        quality = evaluate_cues_quality(cues, video_duration=total, role="source")
        if not quality["passed"] or any(cue_duration_seconds(cue) > 10.5 for cue in cues):
            raise RuntimeError(json.dumps({
                "code": "ASR_QUALITY_FAILED",
                "message": "语音识别结果质量过低，未生成字幕。",
                "detail": quality.get("reason") or "long_cues",
            }, ensure_ascii=False))
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

    def build_outputs(task: dict[str, Any], work: Path, source_path: Path, source_language: str, source_type: str, key: str | None, catalog: dict[str, Any] | None = None) -> tuple[Path, list[Path], dict[str, str] | None]:
        opts = task_subtitle_options(task)
        target = opts["subtitle_target_language"]
        mode = opts["subtitle_output_mode"]
        title = safe_name(task.get("title"))
        cues = parse_srt(source_path.read_text(encoding="utf-8", errors="replace"))
        duration_seconds = 0.0
        try:
            duration_seconds = float((task.get("options") or {}).get("media_duration_seconds") or 0)
        except (TypeError, ValueError):
            duration_seconds = 0.0
        same_language = bool(source_language and source_language not in {"auto", "und"} and normalize_lang(source_language) == target)
        direct_target = source_type == "platform_target_manual"
        platform_auto_target = source_type == "platform_auto_translated_target"
        source_quality = evaluate_cues_quality(
            cues,
            video_duration=duration_seconds,
            expected_language=source_language if source_language not in {"auto", "und", ""} else "",
            role="original_download" if mode == "original" else "source",
        )
        if mode == "original":
            # Advisory only for pure original downloads.
            pass
        elif source_type == "sensevoice" and not source_quality.get("passed"):
            raise RuntimeError(json.dumps({
                "code": "ASR_QUALITY_FAILED",
                "message": "语音识别结果质量过低，未生成字幕。",
                "detail": source_quality.get("reason"),
            }, ensure_ascii=False))
        elif (
            source_type.startswith("platform")
            and mode in {"translated", "bilingual"}
            and not source_quality.get("passed")
            and not direct_target
            and not platform_auto_target
        ):
            # Non-direct platform sources that fail quality cannot be used for project translation.
            if source_language and float((source_quality.get("metrics") or {}).get("script_match_ratio") or 1) < 0.35:
                raise RuntimeError(json.dumps({
                    "code": "SUBTITLE_SOURCE_LANGUAGE_UNCERTAIN",
                    "message": "无法可靠识别源语言，未继续翻译。",
                }, ensure_ascii=False))
            raise RuntimeError(json.dumps({
                "code": "SUBTITLE_SOURCE_QUALITY_FAILED",
                "message": "平台字幕质量异常，未继续翻译。",
                "detail": source_quality.get("reason"),
            }, ensure_ascii=False))
        original = work / f"{title}.original.{source_language or 'und'}.srt"
        write_srt(original, cues)
        files = [original]
        primary = original
        warning = None
        models_used: list[str] = []
        translated_flag = False
        translation_quality = None
        translation_attempted = False
        translation_quality_checked = False
        translation_failure_stage: str | None = None
        platform_target_direct_used = False
        need_translation = mode in {"translated", "bilingual"} and not same_language and not direct_target and not platform_auto_target
        if need_translation and core.is_guest_task(task):
            policy = core.task_policy(task)
            if duration_seconds and duration_seconds > int(policy.get("subtitle_translation_max_duration_minutes", 60)) * 60:
                raise RuntimeError(json.dumps({
                    "code": "GUEST_TRANSLATION_DURATION_LIMIT",
                    "message": f"该视频超过游客字幕翻译时长限制（{int(policy['subtitle_translation_max_duration_minutes'])} 分钟）",
                }, ensure_ascii=False))
        if need_translation:
            settings = load_translation_settings()
            translation_slot = False
            try:
                if not key or not settings["translation_enabled"]:
                    # No real model request was made.
                    warning = {"code": "SUBTITLE_TRANSLATION_FAILED", "message": "翻译失败，已保留原字幕"}
                    primary = original
                    translation_attempted = False
                    translation_quality_checked = False
                    translation_failure_stage = None
                else:
                    if core.is_guest_task(task):
                        policy = core.task_policy(task)
                        if not policy.get("allow_subtitle_translation", True):
                            warning = {"code": "SUBTITLE_TRANSLATION_FAILED", "message": "翻译失败，已保留原字幕"}
                            primary = original
                            translation_attempted = False
                            translation_quality_checked = False
                            translation_failure_stage = None
                        else:
                            translation_slot = core.start_guest_translation(task)
                            result = translate_cues(
                                task, cues, source_language, target, key, source_type=source_type
                            )
                            translated_cues = result["cues"]
                            models_used = list(result.get("models_used") or [])
                            translated_flag = bool(result.get("translated"))
                            translation_quality = result.get("quality")
                            translation_attempted = bool(result.get("translation_attempted"))
                            translation_quality_checked = bool(result.get("translation_quality_checked"))
                            translation_failure_stage = result.get("failure_stage")
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
                    else:
                        result = translate_cues(
                            task, cues, source_language, target, key, source_type=source_type
                        )
                        translated_cues = result["cues"]
                        models_used = list(result.get("models_used") or [])
                        translated_flag = bool(result.get("translated"))
                        translation_quality = result.get("quality")
                        translation_attempted = bool(result.get("translation_attempted"))
                        translation_quality_checked = bool(result.get("translation_quality_checked"))
                        translation_failure_stage = result.get("failure_stage")
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
                    code = parsed.get("code") or "SUBTITLE_TRANSLATION_FAILED"
                    message = parsed.get("message") or "翻译失败，已保留原字幕"
                except Exception:
                    parsed = {}
                    code, message = friendly_error(str(exc), task.get("url") or "")
                    if not code.startswith("GUEST_") and not code.startswith("SUBTITLE_") and not code.startswith("SILICONFLOW_"):
                        code, message = "SUBTITLE_TRANSLATION_FAILED", "翻译失败，已保留原字幕"
                # Temporary guest limits remain retryable failures.
                if code in {"GUEST_TRANSLATION_HOURLY_LIMIT", "GUEST_TRANSLATION_BUSY", "GUEST_TRANSLATION_DURATION_LIMIT"}:
                    raise RuntimeError(json.dumps({"code": code, "message": message}, ensure_ascii=False))
                # ASR itself bad must fail; qualified ASR/platform source is retained.
                if code == "ASR_QUALITY_FAILED":
                    raise RuntimeError(json.dumps({"code": code, "message": message}, ensure_ascii=False))
                if code == "SUBTITLE_SOURCE_QUALITY_FAILED" and source_type != "sensevoice":
                    raise RuntimeError(json.dumps({"code": code, "message": message}, ensure_ascii=False))
                if code == "SUBTITLE_SOURCE_LANGUAGE_UNCERTAIN":
                    message = "无法确定源语言，已保留原字幕"
                warning = {
                    "code": code if (code.startswith("SUBTITLE_") or code.startswith("SILICONFLOW_") or code.startswith("PLATFORM_")) else "SUBTITLE_TRANSLATION_FAILED",
                    "message": message if (code.startswith("SUBTITLE_") or code.startswith("SILICONFLOW_") or code.startswith("PLATFORM_")) else "翻译失败，已保留原字幕",
                }
                primary = original
                translated_flag = False
                models_used = []
                # Propagate stage flags from translate_cues; never infer from error code alone.
                if isinstance(parsed, dict) and (
                    "translation_attempted" in parsed
                    or "translation_quality_checked" in parsed
                    or "failure_stage" in parsed
                ):
                    translation_attempted = bool(parsed.get("translation_attempted"))
                    translation_quality_checked = bool(parsed.get("translation_quality_checked"))
                    translation_failure_stage = parsed.get("failure_stage")
                    metrics = parsed.get("translation_quality_metrics") or {}
                    translation_quality = {"passed": False, "metrics": metrics} if translation_quality_checked else None
                else:
                    translation_attempted = False
                    translation_quality_checked = False
                    translation_failure_stage = None
                    translation_quality = None
            finally:
                if translation_slot:
                    core.release_guest_translation_slot(task["id"])
        elif mode == "bilingual" and same_language:
            primary = original
        elif direct_target and mode == "translated":
            # Quality already evaluated above; do not hard-fail, keep original when bad.
            if source_quality.get("passed"):
                translated_path = work / f"{title}.translated.{target}.srt"
                write_srt(translated_path, cues)
                files.append(translated_path)
                primary = translated_path
                platform_target_direct_used = True
            else:
                warning = {
                    "code": "SUBTITLE_SOURCE_QUALITY_FAILED",
                    "message": "平台字幕质量异常，已提供原字幕",
                }
                primary = original
        elif platform_auto_target and mode in {"translated", "bilingual"}:
            translated_path = work / f"{title}.translated.{target}.srt"
            write_srt(translated_path, cues)
            files.append(translated_path)
            primary = translated_path
            warning = {
                "code": "PLATFORM_AUTO_TRANSLATED_SUBTITLE_USED",
                "message": "已使用平台自动翻译字幕，质量可能不稳定",
            }
        catalog = catalog or {}
        meta = {
            "source_language": source_language or "und",
            "target_language": target,
            "output_mode": mode,
            "primary_model": (models_used[0] if models_used else None),
            "models_used": models_used,
            "cue_count": len(cues),
            "source_cue_count": len(cues),
            "translated_cue_count": len(cues) if translated_flag else 0,
            "output_cue_count": len(cues),
            "translated": bool(translated_flag),  # project-model translation only
            "platform_target_direct_used": bool(platform_target_direct_used),
            "source_type": source_type,
            "platform_original_language": catalog.get("media_language") or "",
            "requested_source_language": opts["subtitle_source_language"],
            "detected_source_language": source_language or "und",
            "source_quality_passed": bool(source_quality and source_quality.get("passed")),
            "translation_attempted": bool(translation_attempted),
            "translation_quality_checked": bool(translation_quality_checked),
            "translation_quality_passed": bool(translation_quality and translation_quality.get("passed")) if translation_quality_checked else False,
            "translation_failure_stage": translation_failure_stage,
            "source_quality_metrics": (source_quality or {}).get("metrics") or {},
            "translation_quality_metrics": (translation_quality or {}).get("metrics") or {},
            "timing_estimated": source_type == "sensevoice",
            "quality_warning": bool((source_quality and not source_quality.get("passed")) or platform_auto_target),
            "translation_skipped_same_language": bool(same_language and mode != "original"),
            "fallback_to_original": bool(warning is not None and primary == original),
            "fallback_to_platform_auto_translation": bool(platform_auto_target),
            "fallback_reason": (warning or {}).get("code"),
            "available_manual_subtitles": catalog.get("manual") or [],
            "available_auto_subtitles": catalog.get("auto") or [],
        }
        meta_path = work / f"{title}.translation.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        # Always keep metadata for all subtitle modes, including original.
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
            core.patch(task_id, status="processing", progress=5, eta="正在检查原始平台字幕", error_code=None, error_message=None)
            catalog = fetch_media_subtitle_catalog(task, cookie)
            media_language = catalog.get("media_language") or ""
            if media_language:
                core.patch(task_id, status="processing", progress=7, eta=f"正在下载{languages.get(media_language, media_language)}字幕")
            platform_files = download_platform_subtitles(task, work, cookie, source, target, media_language=media_language, catalog=catalog)
            source_meta = None
            source_type = "none"
            source_path = None
            if platform_files:
                # Prefer original-language track for auto source.
                ranked = sorted(platform_files, key=lambda item: score_subtitle(item, source, target, prefer_target=False, media_language=media_language))
                original_hit = next((item for item in ranked if item.get("original")), None)
                if mode == "translated":
                    # Priority (any reliable manual/original beats platform auto-translated target):
                    # 1 target manual, 2 original manual, 3 original auto,
                    # 4 explicit manual, 5 explicit original auto, 6 other manual,
                    # 7 target platform auto-translated, 8 ASR (later).
                    manual_target = next((
                        item for item in platform_files
                        if normalize_lang(item.get("language")) == target and not item.get("auto")
                    ), None)
                    if manual_target:
                        source_meta = manual_target
                        source_type = "platform_target_manual"
                    if source_meta is None:
                        original_manual = next((
                            item for item in platform_files
                            if item.get("original") and not item.get("auto")
                        ), None)
                        if original_manual:
                            source_meta = original_manual
                            source_type = "platform_manual_original"
                    if source_meta is None:
                        original_auto = next((
                            item for item in platform_files
                            if item.get("original") and item.get("auto")
                        ), None)
                        if original_auto:
                            source_meta = original_auto
                            source_type = "platform_auto_original"
                    if source_meta is None and source != "auto":
                        explicit_manual = next((
                            item for item in platform_files
                            if normalize_lang(item.get("language")) == normalize_lang(source) and not item.get("auto")
                        ), None)
                        if explicit_manual:
                            source_meta = explicit_manual
                            source_type = "platform_manual"
                    if source_meta is None and source != "auto":
                        explicit_original_auto = next((
                            item for item in platform_files
                            if (
                                normalize_lang(item.get("language")) == normalize_lang(source)
                                and item.get("auto")
                                and item.get("original")
                            )
                        ), None)
                        if explicit_original_auto:
                            source_meta = explicit_original_auto
                            source_type = "platform_auto_original"
                    if source_meta is None:
                        manual_sources = [item for item in platform_files if not item.get("auto")]
                        source_meta = choose_source_subtitle(
                            manual_sources, source, target, prefer_target=False, media_language=media_language
                        )
                        if source_meta:
                            source_type = "platform_manual"
                    if source_meta is None:
                        # Last platform resort only: target-language auto-translated track.
                        target_auto = next((
                            item for item in platform_files
                            if normalize_lang(item.get("language")) == target and item.get("auto")
                        ), None)
                        if target_auto:
                            source_meta = target_auto
                            source_type = "platform_auto_translated_target"
                else:
                    source_meta = original_hit or choose_source_subtitle(platform_files, source, target, prefer_target=False, media_language=media_language)
                    if source_meta:
                        if source_meta.get("original"):
                            source_type = "platform_auto_original" if source_meta.get("auto") else "platform_manual_original"
                        else:
                            source_type = "platform_auto" if source_meta.get("auto") else "platform_manual"
                if source_meta:
                    source_path = source_meta["path"]
            if source_path is None:
                key = get_key()
                if not key:
                    raise RuntimeError("SILICONFLOW_KEY_MISSING")
                if core.is_guest_task(task):
                    ai_slot = core.start_guest_ai(task)
                core.patch(task_id, status="downloading", progress=15, eta="平台没有可用字幕，正在尝试 AI 识别")
                audio = download_audio(task, work, cookie)
                total = duration(audio)
                core.patch(task_id, status="processing", progress=30, eta="正在准备 AI 识别音频")
                chunks = make_chunks(task, audio, work, total)
                source_path = create_asr_srt(task, work, chunks, total, key)
                source_type = "sensevoice"
                source_language = media_language or "und"
            else:
                source_language = normalize_lang(source_meta.get("language") if source_meta else media_language or "und") or "und"
                key = get_key()
            # All platform sources go through build_outputs for quality/metadata consistency.
            primary, files, warning = build_outputs(
                task, work, source_path, source_language if source_path else "auto", source_type, get_key(), catalog=catalog
            )
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
                elif code in {"ASR_QUALITY_FAILED"}:
                    message = "语音识别结果质量过低，未生成字幕。"
                elif code in {"SUBTITLE_SOURCE_QUALITY_FAILED"}:
                    message = "平台字幕质量异常，未继续翻译。"
                elif code == "SUBTITLE_SOURCE_LANGUAGE_UNCERTAIN":
                    message = "无法确定源语言，已保留原字幕。"
                elif code in {"SUBTITLE_TRANSLATION_QUALITY_FAILED", "SUBTITLE_TRANSLATION_STRUCTURE_FAILED"}:
                    message = "翻译质量未达标，已保留原字幕。"
                elif not code.startswith("GUEST_") and not code.startswith("guest_") and not code.startswith("SUBTITLE_") and not code.startswith("ASR_"):
                    message = "游客字幕处理失败，请确认链接为公开可访问的媒体后重试。"
            # ASR garbage must fail, not complete with junk.
            status = "failed"
            core.patch(task_id, status=status, error_code=code, error_message=message, finished=core.now())
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
