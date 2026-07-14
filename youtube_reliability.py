from __future__ import annotations

import importlib.metadata
import json
import os
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests

POT_PROVIDER_URL = os.getenv('POT_PROVIDER_URL', 'http://youtube-pot-provider:4416').rstrip('/')
_CACHE = {'checked': 0.0, 'pot': False, 'preflight': None}


@dataclass(frozen=True)
class Strategy:
    key: str
    label: str
    client: str | None = None
    use_pot: bool = False
    timeout: int = 24


GENERIC = Strategy('generic', '通用解析', timeout=35)
MWEB_POT = Strategy('mweb-pot', 'mweb + PO Token', 'mweb', True, 28)
DEFAULT = Strategy('default', '默认客户端', timeout=24)
WEB_SAFARI = Strategy('web-safari', 'Safari/HLS', 'web_safari', False, 22)
ANDROID_VR = Strategy('android-vr', 'Android VR', 'android_vr', False, 20)
WEB_EMBEDDED = Strategy('web-embedded', '嵌入客户端', 'web_embedded', False, 18)


def canonical_url(core: Any, url: str) -> str:
    if not core.yt(url):
        return url
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    video_id = ''
    if host == 'youtu.be':
        video_id = parsed.path.strip('/').split('/')[0]
    elif parsed.path == '/watch':
        video_id = (parse_qs(parsed.query).get('v') or [''])[0]
    else:
        match = re.match(r'/(?:shorts|live|embed)/([A-Za-z0-9_-]{6,})', parsed.path)
        if match:
            video_id = match.group(1)
    return f'https://www.youtube.com/watch?v={video_id}' if re.fullmatch(r'[A-Za-z0-9_-]{6,}', video_id or '') else url


def pot_available(force: bool = False) -> bool:
    now = time.monotonic()
    if not force and now - _CACHE['checked'] < 15:
        return bool(_CACHE['pot'])
    parsed = urlsplit(POT_PROVIDER_URL)
    try:
        with socket.create_connection((parsed.hostname or 'youtube-pot-provider', parsed.port or 4416), timeout=1.2):
            value = True
    except OSError:
        value = False
    _CACHE.update(checked=now, pot=value)
    return value


def preflight(core: Any, force: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    cached = _CACHE.get('preflight')
    if not force and cached and now - float(cached.get('checked', 0)) < 30:
        return cached
    result: dict[str, Any] = {'checked': now, 'dns': False, 'https': False, 'deno': bool(shutil.which('deno')), 'ejs': False, 'pot': pot_available(force)}
    try:
        socket.getaddrinfo('www.youtube.com', 443, type=socket.SOCK_STREAM)
        result['dns'] = True
    except OSError:
        pass
    try:
        response = requests.get('https://www.youtube.com/generate_204', timeout=(4, 7), allow_redirects=False)
        result['https'] = response.status_code in {200, 204, 301, 302, 303, 307, 308}
        result['http_status'] = response.status_code
    except requests.RequestException as exc:
        result['https_error'] = type(exc).__name__
    try:
        importlib.metadata.version('yt-dlp-ejs')
        result['ejs'] = True
    except importlib.metadata.PackageNotFoundError:
        pass
    _CACHE['preflight'] = result
    return result


def strategies(core: Any, url: str, preferred: str | None = None) -> list[Strategy]:
    if not core.yt(url):
        return [GENERIC]
    items = ([MWEB_POT] if pot_available() else []) + [DEFAULT, WEB_SAFARI, ANDROID_VR, WEB_EMBEDDED]
    if preferred:
        items.sort(key=lambda item: item.key != preferred)
    return items


def base(core: Any, url: str, cookie: Path | None, strategy: Strategy) -> list[str]:
    cfg = core.settings()
    args = ['yt-dlp', '--no-playlist', '--force-ipv4', '--socket-timeout', '10', '--retries', '2', '--fragment-retries', '3', '--extractor-retries', '1', '--js-runtimes', 'deno', '--impersonate', 'chrome']
    proxy = str(cfg.get('proxy_url') or '').strip()
    if proxy:
        args += ['--proxy', proxy]
    if cookie:
        args += ['--cookies', str(cookie)]
    extractor_args = []
    if core.yt(url) and strategy.client:
        extractor_args.append(f'player_client={strategy.client}')
    if extractor_args:
        args += ['--extractor-args', 'youtube:' + ';'.join(extractor_args)]
    if strategy.use_pot:
        args += ['--extractor-args', f'youtubepot-bgutilhttp:base_url={POT_PROVIDER_URL}']
    return args


def compatibility_base(core: Any, url: str, cookie: Path | None = None, compat: bool = False) -> list[str]:
    items = strategies(core, url)
    chosen = next((item for item in items if item.key == 'web-safari'), items[-1]) if compat else items[0]
    return base(core, url, cookie, chosen)


def retriable(text: str) -> bool:
    lower = text.lower()
    return any(value in lower for value in ('403', '429', 'forbidden', 'timed out', 'timeout', 'not a bot', 'sign in to confirm', 'po token', 'requested format is not available', 'connection reset', 'temporary failure', 'no video formats'))


def friendly_error(original: Any, text: str, url: str = '') -> tuple[str, str]:
    lower = text.lower()
    if 'youtube_dns_failed' in lower:
        return 'YOUTUBE_DNS_FAILED', '容器无法解析 YouTube 域名，不是播放器客户端问题。请检查 Docker DNS。'
    if 'youtube_https_failed' in lower:
        return 'YOUTUBE_HTTPS_FAILED', '容器无法连接 YouTube HTTPS，不是多客户端同时失效。请检查服务器网络、代理或防火墙。'
    if 'youtube_runtime_missing' in lower:
        return 'YOUTUBE_RUNTIME_MISSING', 'YouTube 解析运行环境不完整：Deno 或本地 yt-dlp-ejs 缺失。请重新构建镜像。'
    if 'all_youtube_strategies_failed' in lower:
        if 'sign in to confirm' in lower or 'not a bot' in lower:
            return 'YOUTUBE_BOT_CHECK', 'YouTube 已拦截当前机房 IP。请上传 Cookie，仍失败时配置住宅代理。'
        return 'YOUTUBE_ALL_STRATEGIES_FAILED', '网络与运行环境正常，但 YouTube 客户端路径均失败。请上传 Cookie；仍失败时配置代理。'
    if 'timed out' in lower or 'timeout' in lower:
        return 'PROBE_TIMEOUT', '平台请求超时，请重试或检查网络。'
    return original(text, url)


def probe(core: Any, url: str, cookie_id: str | None) -> dict[str, Any]:
    url = canonical_url(core, url)
    if core.yt(url):
        check = preflight(core)
        if not check['dns']:
            raise RuntimeError('YOUTUBE_DNS_FAILED')
        if not check['https']:
            raise RuntimeError('YOUTUBE_HTTPS_FAILED')
        if not check['deno'] or not check['ejs']:
            raise RuntimeError('YOUTUBE_RUNTIME_MISSING')
    cookie = core.cpath(cookie_id)
    errors: list[str] = []
    raw = None
    selected = None
    try:
        for strategy in strategies(core, url):
            command = base(core, url, cookie, strategy) + ['--dump-single-json', '--no-warnings', url]
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=strategy.timeout)
            except subprocess.TimeoutExpired:
                errors.append(f'{strategy.label}：解析超时')
                continue
            if result.returncode == 0:
                try:
                    candidate = json.loads(result.stdout)
                except json.JSONDecodeError:
                    errors.append(f'{strategy.label}：返回数据异常')
                    continue
                if candidate.get('formats') or candidate.get('url'):
                    raw, selected = candidate, strategy
                    break
            errors.append(f"{strategy.label}：{(result.stderr or result.stdout or '解析失败')[-1200:]}")
        if raw is None or selected is None:
            raise RuntimeError('ALL_YOUTUBE_STRATEGIES_FAILED\n' + '\n'.join(errors)[-4500:])
    finally:
        if cookie:
            cookie.unlink(missing_ok=True)
    videos, audios = core.simplify(raw.get('formats') or [])
    for item in videos + audios:
        item['strategy'] = selected.key
    return {'id': raw.get('id'), 'title': raw.get('title'), 'uploader': raw.get('uploader') or raw.get('channel'), 'platform': raw.get('extractor_key') or raw.get('extractor'), 'duration': raw.get('duration'), 'thumbnail': raw.get('thumbnail'), 'is_live': bool(raw.get('is_live') or raw.get('live_status') == 'is_live'), 'drm': bool(raw.get('has_drm')), 'video_options': videos, 'audio_options': audios, 'subtitles': sorted(set(raw.get('subtitles') or {}) | set(raw.get('automatic_captions') or {})), 'webpage_url': raw.get('webpage_url') or url, 'download_strategy': selected.key, 'download_strategy_label': selected.label, 'pot_provider': pot_available(), 'preflight': preflight(core)}
