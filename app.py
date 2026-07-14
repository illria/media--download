from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field

APP_PORT = int(os.getenv("APP_PORT", "19190"))
DATA_ROOT = Path(os.getenv("DATA_ROOT", "/data")).resolve()
DB_PATH = DATA_ROOT / "database" / "media-hub.db"
DOWNLOAD_DIR = DATA_ROOT / "downloads"
TEMP_DIR = DATA_ROOT / "temp"
COOKIE_DIR = DATA_ROOT / "cookies"
SECRET_DIR = DATA_ROOT / "database"
STATIC_DIR = Path(__file__).parent / "static"

for directory in (SECRET_DIR, DOWNLOAD_DIR, TEMP_DIR, COOKIE_DIR):
    directory.mkdir(parents=True, exist_ok=True)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_or_create(path: Path, factory) -> str:
    if path.exists() and path.read_text(encoding="utf-8").strip():
        return path.read_text(encoding="utf-8").strip()
    value = factory()
    path.write_text(value, encoding="utf-8")
    os.chmod(path, 0o600)
    return value


SECRET_KEY = os.getenv("SECRET_KEY") or read_or_create(
    SECRET_DIR / "secret.key", lambda: secrets.token_urlsafe(48)
)
FERNET_KEY = os.getenv("COOKIE_ENCRYPTION_KEY") or read_or_create(
    SECRET_DIR / "cookie-encryption.key", lambda: Fernet.generate_key().decode()
)
FERNET = Fernet(FERNET_KEY.encode())
TOKENS = URLSafeTimedSerializer(SECRET_KEY, salt="media-download-hub")

DEFAULT_SETTINGS: dict[str, Any] = {
    "max_file_size_gb": 5,
    "max_video_minutes": 180,
    "max_live_minutes": 120,
    "retention_hours": 24,
    "min_free_gb": 5,
    "max_resolution": 1080,
    "proxy_url": "",
}

ACTIVE: dict[str, subprocess.Popen[str]] = {}
ACTIVE_LOCK = threading.RLock()
STOP_EVENT = threading.Event()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cookies(
                id TEXT PRIMARY KEY, platform TEXT NOT NULL, label TEXT NOT NULL,
                encrypted_path TEXT NOT NULL, original_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks(
                id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT, platform TEXT,
                status TEXT NOT NULL, options_json TEXT NOT NULL,
                progress REAL NOT NULL DEFAULT 0, speed TEXT, eta TEXT,
                output_path TEXT, output_size INTEGER,
                error_code TEXT, error_message TEXT, log_tail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                started_at TEXT, finished_at TEXT
            );
            """
        )
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key,value_json) VALUES(?,?)",
                (key, json.dumps(value)),
            )
        conn.execute(
            "UPDATE tasks SET status='queued', error_code=NULL, error_message=NULL "
            "WHERE status IN ('probing','downloading','processing')"
        )


def get_meta(key: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()


def get_settings() -> dict[str, Any]:
    result = dict(DEFAULT_SETTINGS)
    with connect() as conn:
        for row in conn.execute("SELECT key,value_json FROM settings"):
            try:
                result[row["key"]] = json.loads(row["value_json"])
            except json.JSONDecodeError:
                pass
    return result


def update_settings(values: dict[str, Any]) -> dict[str, Any]:
    allowed = set(DEFAULT_SETTINGS)
    with connect() as conn:
        for key, value in values.items():
            if key in allowed:
                conn.execute(
                    "INSERT INTO settings(key,value_json) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                    (key, json.dumps(value)),
                )
        conn.commit()
    return get_settings()


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("密码至少需要 8 个字符")
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        salt_hex, digest_hex = encoded.split(":", 1)
        actual = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32
        )
        return hmac.compare_digest(actual.hex(), digest_hex)
    except Exception:
        return False


def create_token() -> str:
    return TOKENS.dumps({"sub": "admin"})


def require_auth(authorization: str | None = Header(default=None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "需要登录")
    try:
        payload = TOKENS.loads(authorization[7:], max_age=7 * 24 * 3600)
        if payload.get("sub") != "admin":
            raise HTTPException(401, "登录已失效")
    except (BadSignature, SignatureExpired):
        raise HTTPException(401, "登录已失效")


def validate_url_syntax(url: str) -> tuple[str, int]:
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(400, "只允许有效的 HTTP/HTTPS 链接")
    if parsed.username or parsed.password:
        raise HTTPException(400, "链接中不能包含账号密码")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".local"):
        raise HTTPException(400, "禁止访问本机或局域网地址")
    try:
        ip = ipaddress.ip_address(host)
        reject_ip(ip)
    except ValueError:
        pass
    return host, parsed.port or (443 if parsed.scheme == "https" else 80)


def reject_ip(ip: ipaddress._BaseAddress) -> None:
    if any((ip.is_private, ip.is_loopback, ip.is_link_local, ip.is_multicast, ip.is_reserved, ip.is_unspecified)):
        raise HTTPException(400, "禁止访问本机、内网、链路本地或保留地址")


async def validate_public_url(url: str) -> str:
    host, port = validate_url_syntax(url)
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.run_in_executor(None, lambda: socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
    except socket.gaierror:
        raise HTTPException(400, "域名无法解析")
    for info in infos:
        reject_ip(ipaddress.ip_address(info[4][0]))
    return url.strip()


def disk_status() -> dict[str, Any]:
    usage = shutil.disk_usage(DATA_ROOT)
    return {
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "percent": round(usage.used / usage.total * 100, 1),
    }


def cookie_temp_path(cookie_id: str | None) -> Path | None:
    if not cookie_id:
        return None
    with connect() as conn:
        row = conn.execute("SELECT encrypted_path FROM cookies WHERE id=?", (cookie_id,)).fetchone()
    if not row:
        raise RuntimeError("Cookie 账号不存在")
    encrypted = Path(row["encrypted_path"]).read_bytes()
    try:
        plain = FERNET.decrypt(encrypted)
    except InvalidToken as exc:
        raise RuntimeError("Cookie 解密失败") from exc
    path = TEMP_DIR / f"cookie-{cookie_id}-{uuid.uuid4().hex}.txt"
    path.write_bytes(plain)
    os.chmod(path, 0o600)
    return path


def proxy_args() -> list[str]:
    proxy = str(get_settings().get("proxy_url") or "").strip()
    return ["--proxy", proxy] if proxy else []


def probe_sync(url: str, cookie_id: str | None) -> dict[str, Any]:
    cookie_path = cookie_temp_path(cookie_id)
    cmd = ["yt-dlp", "--dump-single-json", "--no-playlist", "--no-warnings", *proxy_args()]
    if cookie_path:
        cmd += ["--cookies", str(cookie_path)]
    cmd.append(url)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout or "解析失败")[-1800:]
            raise RuntimeError(message)
        raw = json.loads(proc.stdout)
    finally:
        if cookie_path:
            cookie_path.unlink(missing_ok=True)

    formats = []
    for item in raw.get("formats") or []:
        if not item.get("url"):
            continue
        formats.append({
            "format_id": str(item.get("format_id") or ""),
            "ext": item.get("ext"),
            "resolution": item.get("resolution") or item.get("format_note"),
            "height": item.get("height"),
            "fps": item.get("fps"),
            "vcodec": item.get("vcodec"),
            "acodec": item.get("acodec"),
            "filesize": item.get("filesize") or item.get("filesize_approx"),
            "tbr": item.get("tbr"),
        })
    subtitles = sorted(set((raw.get("subtitles") or {}).keys()) | set((raw.get("automatic_captions") or {}).keys()))
    drm = bool(raw.get("has_drm"))
    return {
        "id": raw.get("id"),
        "title": raw.get("title"),
        "uploader": raw.get("uploader") or raw.get("channel"),
        "platform": raw.get("extractor_key") or raw.get("extractor"),
        "duration": raw.get("duration"),
        "thumbnail": raw.get("thumbnail"),
        "is_live": bool(raw.get("is_live") or raw.get("live_status") == "is_live"),
        "drm": drm,
        "formats": formats,
        "subtitles": subtitles,
        "webpage_url": raw.get("webpage_url") or url,
    }


def task_row(task_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        return None
    data = dict(row)
    data["options"] = json.loads(data.pop("options_json"))
    return data


def patch_task(task_id: str, **values: Any) -> None:
    if not values:
        return
    values["updated_at"] = utcnow()
    columns = ",".join(f"{key}=?" for key in values)
    with connect() as conn:
        conn.execute(f"UPDATE tasks SET {columns} WHERE id=?", (*values.values(), task_id))
        conn.commit()


def classify_error(text: str) -> tuple[str, str]:
    lower = text.lower()
    if "drm" in lower:
        return "DRM_PROTECTED", "检测到 DRM 加密，系统不会绕过或解密"
    if "sign in" in lower or "cookies" in lower or "login" in lower:
        return "LOGIN_REQUIRED", "内容需要登录，请上传有效 cookies.txt"
    if "unsupported url" in lower:
        return "UNSUPPORTED_URL", "当前解析器不支持该链接"
    if "geo" in lower or "country" in lower or "region" in lower:
        return "REGION_BLOCKED", "内容可能存在地区限制"
    if "403" in lower or "forbidden" in lower:
        return "ACCESS_DENIED", "平台拒绝访问，可尝试更新解析器、Cookie 或代理"
    if "no space" in lower:
        return "DISK_FULL", "服务器磁盘空间不足"
    return "DOWNLOAD_FAILED", text[-1200:] or "下载失败"


def build_command(task: dict[str, Any], cookie_path: Path | None) -> list[str]:
    options = task["options"]
    settings = get_settings()
    output = str(TEMP_DIR / task["id"] / "%(title).180B [%(id)s].%(ext)s")
    mode = options.get("mode", "video")
    engine = options.get("engine", "auto")

    if mode == "live" or engine == "streamlink":
        quality = re.sub(r"[^a-zA-Z0-9_+,.-]", "", options.get("stream_quality", "best")) or "best"
        destination = TEMP_DIR / task["id"] / "live.ts"
        cmd = ["streamlink", "--force", "--output", str(destination)]
        proxy = str(settings.get("proxy_url") or "").strip()
        if proxy:
            cmd += ["--http-proxy", proxy]
        cmd += [task["url"], quality]
        return cmd

    max_height = min(int(options.get("resolution", 1080)), int(settings.get("max_resolution", 1080)))
    cmd = [
        "yt-dlp", "--newline", "--no-playlist", "--restrict-filenames",
        "--paths", f"temp:{TEMP_DIR / task['id']}", "--output", output,
        "--max-filesize", f"{settings.get('max_file_size_gb', 5)}G",
        "--progress-template", "download:PROGRESS:%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
        *proxy_args(),
    ]
    if cookie_path:
        cmd += ["--cookies", str(cookie_path)]
    if options.get("format_id"):
        cmd += ["--format", str(options["format_id"])]
    elif mode == "audio":
        cmd += ["--format", "bestaudio/best"]
    else:
        cmd += ["--format", f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best"]
        cmd += ["--merge-output-format", options.get("video_container", "mp4")]
    if mode == "audio" and options.get("audio_format", "original") != "original":
        cmd += ["--extract-audio", "--audio-format", options["audio_format"]]
    languages = [x for x in options.get("subtitle_languages", []) if re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", x)]
    if languages:
        cmd += ["--write-subs", "--write-auto-subs", "--sub-langs", ",".join(languages)]
    if options.get("write_thumbnail", True):
        cmd += ["--write-thumbnail"]
    if options.get("embed_metadata", True):
        cmd += ["--embed-metadata"]
    if options.get("start_time") or options.get("end_time"):
        start = options.get("start_time") or "0"
        end = options.get("end_time") or "inf"
        cmd += ["--download-sections", f"*{start}-{end}"]
    cmd.append(task["url"])
    return cmd


def move_outputs(task_id: str) -> tuple[str, int]:
    source = TEMP_DIR / task_id
    files = [p for p in source.rglob("*") if p.is_file() and not p.name.startswith("cookie-")]
    if not files:
        raise RuntimeError("任务完成但没有找到输出文件")
    destination = DOWNLOAD_DIR / task_id
    destination.mkdir(parents=True, exist_ok=True)
    for file in files:
        target = destination / file.name
        if target.exists():
            target = destination / f"{file.stem}-{uuid.uuid4().hex[:6]}{file.suffix}"
        shutil.move(str(file), target)
    shutil.rmtree(source, ignore_errors=True)
    primary = max((p for p in destination.iterdir() if p.is_file()), key=lambda p: p.stat().st_size)
    total = sum(p.stat().st_size for p in destination.iterdir() if p.is_file())
    return str(primary), total


def execute_task(task_id: str) -> None:
    task = task_row(task_id)
    if not task:
        return
    settings = get_settings()
    disk = disk_status()
    if disk["free"] < int(settings.get("min_free_gb", 5)) * 1024**3:
        patch_task(task_id, status="failed", error_code="DISK_LOW", error_message="磁盘可用空间低于安全阈值", finished_at=utcnow())
        return
    cookie_path = None
    try:
        cookie_path = cookie_temp_path(task["options"].get("cookie_id"))
        work = TEMP_DIR / task_id
        work.mkdir(parents=True, exist_ok=True)
        patch_task(task_id, status="downloading", started_at=utcnow(), progress=0)
        cmd = build_command(task, cookie_path)
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1, start_new_session=True,
        )
        with ACTIVE_LOCK:
            ACTIVE[task_id] = proc
        log_lines: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            clean = line.strip()
            if clean:
                log_lines.append(clean)
                log_lines = log_lines[-80:]
            match = re.search(r"PROGRESS:\s*([0-9.]+)%\|([^|]*)\|([^|]*)", clean)
            if match:
                patch_task(task_id, progress=float(match.group(1)), speed=match.group(2), eta=match.group(3), log_tail="\n".join(log_lines))
            elif len(log_lines) % 8 == 0:
                patch_task(task_id, log_tail="\n".join(log_lines))
            current = task_row(task_id)
            if current and current["status"] == "cancelled":
                os.killpg(proc.pid, signal.SIGTERM)
                break
        return_code = proc.wait()
        current = task_row(task_id)
        if current and current["status"] == "cancelled":
            shutil.rmtree(TEMP_DIR / task_id, ignore_errors=True)
            patch_task(task_id, finished_at=utcnow(), log_tail="\n".join(log_lines))
            return
        if return_code != 0:
            code, message = classify_error("\n".join(log_lines))
            raise RuntimeError(json.dumps({"code": code, "message": message}, ensure_ascii=False))
        output_path, output_size = move_outputs(task_id)
        patch_task(task_id, status="completed", progress=100, output_path=output_path, output_size=output_size, finished_at=utcnow(), log_tail="\n".join(log_lines))
    except Exception as exc:
        try:
            parsed = json.loads(str(exc))
            code, message = parsed["code"], parsed["message"]
        except Exception:
            code, message = classify_error(str(exc))
        patch_task(task_id, status="failed", error_code=code, error_message=message, finished_at=utcnow())
    finally:
        if cookie_path:
            cookie_path.unlink(missing_ok=True)
        with ACTIVE_LOCK:
            ACTIVE.pop(task_id, None)


def worker_loop() -> None:
    while not STOP_EVENT.is_set():
        with ACTIVE_LOCK:
            busy = bool(ACTIVE)
        if not busy:
            with connect() as conn:
                row = conn.execute("SELECT id FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if row:
                execute_task(row["id"])
                continue
        STOP_EVENT.wait(1)


def cleanup_loop() -> None:
    while not STOP_EVENT.wait(900):
        retention = int(get_settings().get("retention_hours", 24))
        cutoff = datetime.now(timezone.utc) - timedelta(hours=retention)
        with connect() as conn:
            rows = conn.execute("SELECT id,output_path FROM tasks WHERE status='completed' AND finished_at IS NOT NULL").fetchall()
            for row in rows:
                try:
                    finished = datetime.fromisoformat(task_row(row["id"])["finished_at"])
                except Exception:
                    continue
                if finished < cutoff:
                    shutil.rmtree(DOWNLOAD_DIR / row["id"], ignore_errors=True)
                    conn.execute("UPDATE tasks SET status='expired',output_path=NULL,output_size=NULL WHERE id=?", (row["id"],))
            conn.commit()


class PasswordBody(BaseModel):
    password: str = Field(min_length=8, max_length=256)


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class ProbeBody(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    cookie_id: str | None = None


class TaskBody(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    title: str | None = Field(default=None, max_length=500)
    platform: str | None = Field(default=None, max_length=100)
    options: dict[str, Any] = Field(default_factory=dict)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    env_password = os.getenv("ADMIN_PASSWORD")
    if env_password and not get_meta("admin_password"):
        set_meta("admin_password", hash_password(env_password))
    STOP_EVENT.clear()
    threads = [
        threading.Thread(target=worker_loop, daemon=True, name="download-worker"),
        threading.Thread(target=cleanup_loop, daemon=True, name="cleanup-worker"),
    ]
    for thread in threads:
        thread.start()
    yield
    STOP_EVENT.set()
    with ACTIVE_LOCK:
        for proc in ACTIVE.values():
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


app = FastAPI(title="Media Download Hub", lifespan=lifespan)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "port": APP_PORT,
        "disk": disk_status(),
        "tools": {name: bool(shutil.which(name)) for name in ("yt-dlp", "streamlink", "ffmpeg")},
    }


@app.get("/api/auth/status")
def auth_status() -> dict[str, bool]:
    return {"setup_required": not bool(get_meta("admin_password"))}


@app.post("/api/auth/setup")
def auth_setup(body: PasswordBody) -> dict[str, str]:
    if get_meta("admin_password"):
        raise HTTPException(409, "管理员密码已经设置")
    set_meta("admin_password", hash_password(body.password))
    return {"token": create_token()}


@app.post("/api/auth/login")
def auth_login(body: LoginBody) -> dict[str, str]:
    saved = get_meta("admin_password") or ""
    if not verify_password(body.password, saved):
        time.sleep(0.5)
        raise HTTPException(401, "密码错误")
    return {"token": create_token()}


@app.post("/api/probe", dependencies=[Depends(require_auth)])
async def probe(body: ProbeBody) -> dict[str, Any]:
    url = await validate_public_url(body.url)
    try:
        result = await asyncio.to_thread(probe_sync, url, body.cookie_id)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "解析超时")
    except Exception as exc:
        code, message = classify_error(str(exc))
        raise HTTPException(400, detail={"code": code, "message": message})
    if result["drm"]:
        result["warning"] = "检测到 DRM，系统不会绕过或解密"
    return result


@app.post("/api/tasks", dependencies=[Depends(require_auth)])
async def create_task(body: TaskBody) -> dict[str, Any]:
    url = await validate_public_url(body.url)
    task_id = uuid.uuid4().hex
    now = utcnow()
    with connect() as conn:
        conn.execute(
            "INSERT INTO tasks(id,url,title,platform,status,options_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (task_id, url, body.title, body.platform, "queued", json.dumps(body.options), now, now),
        )
        conn.commit()
    return task_row(task_id) or {}


@app.get("/api/tasks", dependencies=[Depends(require_auth)])
def list_tasks() -> list[dict[str, Any]]:
    with connect() as conn:
        ids = [row["id"] for row in conn.execute("SELECT id FROM tasks ORDER BY created_at DESC LIMIT 200")]
    return [task_row(task_id) for task_id in ids if task_row(task_id)]


@app.post("/api/tasks/{task_id}/cancel", dependencies=[Depends(require_auth)])
def cancel_task(task_id: str) -> dict[str, Any]:
    task = task_row(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["status"] not in {"queued", "downloading", "processing"}:
        raise HTTPException(409, "当前状态不能取消")
    patch_task(task_id, status="cancelled", finished_at=utcnow())
    with ACTIVE_LOCK:
        proc = ACTIVE.get(task_id)
        if proc:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    return task_row(task_id) or {}


@app.post("/api/tasks/{task_id}/retry", dependencies=[Depends(require_auth)])
def retry_task(task_id: str) -> dict[str, Any]:
    task = task_row(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["status"] not in {"failed", "cancelled", "expired"}:
        raise HTTPException(409, "当前状态不能重试")
    patch_task(task_id, status="queued", progress=0, error_code=None, error_message=None, output_path=None, output_size=None, finished_at=None)
    return task_row(task_id) or {}


@app.delete("/api/tasks/{task_id}", dependencies=[Depends(require_auth)])
def delete_task(task_id: str) -> dict[str, bool]:
    task = task_row(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["status"] in {"queued", "downloading", "processing"}:
        raise HTTPException(409, "请先取消任务")
    shutil.rmtree(DOWNLOAD_DIR / task_id, ignore_errors=True)
    shutil.rmtree(TEMP_DIR / task_id, ignore_errors=True)
    with connect() as conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        conn.commit()
    return {"ok": True}


@app.get("/api/tasks/{task_id}/download", dependencies=[Depends(require_auth)])
def download_task(task_id: str) -> FileResponse:
    task = task_row(task_id)
    if not task or task["status"] != "completed" or not task["output_path"]:
        raise HTTPException(404, "文件不存在")
    path = Path(task["output_path"]).resolve()
    if DOWNLOAD_DIR not in path.parents or not path.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(path, filename=path.name)


@app.get("/api/settings", dependencies=[Depends(require_auth)])
def settings_get() -> dict[str, Any]:
    values = get_settings()
    proxy = str(values.get("proxy_url") or "")
    values["proxy_url"] = (proxy[:12] + "••••") if proxy else ""
    return values


@app.put("/api/settings", dependencies=[Depends(require_auth)])
def settings_put(values: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key in DEFAULT_SETTINGS:
        if key in values:
            cleaned[key] = values[key]
    for key in ("max_file_size_gb", "max_video_minutes", "max_live_minutes", "retention_hours", "min_free_gb", "max_resolution"):
        if key in cleaned:
            cleaned[key] = max(1, int(cleaned[key]))
    if cleaned.get("proxy_url") and "••••" in str(cleaned["proxy_url"]):
        cleaned.pop("proxy_url")
    return update_settings(cleaned)


@app.get("/api/cookies", dependencies=[Depends(require_auth)])
def list_cookies() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT id,platform,label,original_name,created_at FROM cookies ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]


@app.post("/api/cookies", dependencies=[Depends(require_auth)])
async def upload_cookie(platform: str, label: str, file: UploadFile = File(...)) -> dict[str, Any]:
    content = await file.read(2 * 1024 * 1024 + 1)
    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(413, "Cookie 文件不能超过 2MB")
    if b"\x00" in content:
        raise HTTPException(400, "Cookie 文件必须是文本")
    cookie_id = uuid.uuid4().hex
    path = COOKIE_DIR / f"{cookie_id}.enc"
    path.write_bytes(FERNET.encrypt(content))
    os.chmod(path, 0o600)
    with connect() as conn:
        conn.execute(
            "INSERT INTO cookies(id,platform,label,encrypted_path,original_name,created_at) VALUES(?,?,?,?,?,?)",
            (cookie_id, platform[:100], label[:100], str(path), (file.filename or "cookies.txt")[:255], utcnow()),
        )
        conn.commit()
    return {"id": cookie_id, "platform": platform, "label": label}


@app.delete("/api/cookies/{cookie_id}", dependencies=[Depends(require_auth)])
def delete_cookie(cookie_id: str) -> dict[str, bool]:
    with connect() as conn:
        row = conn.execute("SELECT encrypted_path FROM cookies WHERE id=?", (cookie_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Cookie 不存在")
        Path(row["encrypted_path"]).unlink(missing_ok=True)
        conn.execute("DELETE FROM cookies WHERE id=?", (cookie_id,))
        conn.commit()
    return {"ok": True}


if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@app.get("/{path:path}", response_class=HTMLResponse)
def frontend(path: str = "") -> HTMLResponse:
    index = STATIC_DIR / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Media Download Hub</h1><p>Frontend missing.</p>", status_code=503)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=APP_PORT, workers=1)
