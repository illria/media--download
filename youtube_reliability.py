from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

POT_PROVIDER_URL = os.getenv('POT_PROVIDER_URL', 'http://youtube-pot-provider:4416').rstrip('/')
_POT_CACHE = {'checked': 0.0, 'available': False}

@dataclass(frozen=True)
class Strategy:
    key: str
    label: str
    client: str | None = None
    use_pot: bool = False
    timeout: int = 24

GENERIC = Strategy('generic', '通用解析', timeout=35)
DEFAULT = Strategy('default', '默认客户端', timeout=24)
MWEB_POT = Strategy('mweb-pot', 'mweb + PO Token', client='mweb', use_pot=True, timeout=30)
WEB_SAFARI = Strategy('web-safari', 'Safari/HLS', client='web_safari', timeout=22)
ANDROID_VR = Strategy('android-vr', 'Android VR', client='android_vr', timeout=20)
WEB_EMBEDDED = Strategy('web-embedded', '嵌入客户端', client='web_embedded', timeout=18)

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
    if re.fullmatch(r'[A-Za-z0-9_-]{6,}', video_id or ''):
        return f'https://www.youtube.com/watch?v={video_id}'
    return url

def pot_available(force: bool = False) -> bool:
    now = time.monotonic()
    if not force and now - float(_POT_CACHE['checked']) < 15:
        return bool(_POT_CACHE['available'])
    parsed = urlsplit(POT_PROVIDER_URL)
    try:
        with socket.create_connection((parsed.hostname or 'youtube-pot-provider', parsed.port or 4416), timeout=1.2):
            available = True
    except OSError:
        available = False
    _POT_CACHE.update(checked=now, available=available)
    return available

def strategies(core: Any, url: str, preferred: str | None = None) -> list[Strategy]:
    if not core.yt(url):
        return [GENERIC]
    items: list[Strategy] = []
    if pot_available():
        items.append(MWEB_POT)
    items.extend([DEFAULT, WEB_SAFARI, ANDROID_VR, WEB_EMBEDDED])
    if preferred:
        items.sort(key=lambda item: item.key != preferred)
    return items

def base(core: Any, url: str, cookie: Path | None, strategy: Strategy) -> list[str]:
    cfg = core.settings()
    args = ['yt-dlp','--no-playlist','--force-ipv4','--socket-timeout','10','--retries','3','--fragment-retries','5','--extractor-retries','2','--js-runtimes','deno','--remote-components','ejs:npm','--impersonate','chrome']
    proxy = str(cfg.get('proxy_url') or '').strip()
    if proxy:
        args += ['--proxy', proxy]
    if cookie:
        args += ['--cookies', str(cookie)]
    if core.yt(url) and strategy.client:
        args += ['--extractor-args', f'youtube:player_client={strategy.client}']
    if strategy.use_pot:
        args += ['--extractor-args', f'youtubepot-bgutilhttp:base_url={POT_PROVIDER_URL}']
    return args

def compatibility_base(core: Any, url: str, cookie: Path | None = None, compat: bool = False) -> list[str]:
    items = strategies(core, url)
    selected = next((item for item in items if item.key == 'web-safari'), items[-1]) if compat else items[0]
    return base(core, url, cookie, selected)

def retriable(text: str) -> bool:
    lower = text.lower()
    return any(marker in lower for marker in ('403','429','forbidden','timed out','timeout','not a bot','sign in to confirm','po token','requested format is not available','unable to download','connection reset','remote end closed','temporary failure','network is unreachable','no video formats'))

def friendly_error(original: Any, text: str, url: str = '') -> tuple[str, str]:
    lower = text.lower()
    if 'all_youtube_strategies_failed' in lower:
        if 'sign in to confirm' in lower or 'not a bot' in lower:
            return 'YOUTUBE_BOT_CHECK', 'YouTube 已拦截当前机房 IP。系统已尝试 PO Token 和多个客户端；请上传 Cookie，仍失败时配置住宅代理。'
        if 'timed out' in lower or 'timeout' in lower or '解析超时' in text:
            return 'YOUTUBE_PROBE_TIMEOUT', 'YouTube 多条解析路径均超时。请重试；仍失败时上传 Cookie 或设置代理。'
        return 'YOUTUBE_ALL_STRATEGIES_FAILED', 'YouTube 多条解析路径均失败。请上传 Cookie；仍失败时配置住宅代理。'
    if 'timed out' in lower or 'timeout' in lower:
        return 'PROBE_TIMEOUT', '平台解析超时，系统已停止等待；请重试或配置代理。'
    return original(text, url)

def probe(core: Any, url: str, cookie_id: str | None) -> dict[str, Any]:
    url = canonical_url(core, url)
    cookie = core.cpath(cookie_id)
    errors: list[str] = []
    raw: dict[str, Any] | None = None
    selected: Strategy | None = None
    try:
        for strategy in strategies(core, url):
            command = base(core, url, cookie, strategy) + ['--dump-single-json','--no-warnings',url]
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
                errors.append(f'{strategy.label}：没有可用格式')
                continue
            errors.append(f"{strategy.label}：{(result.stderr or result.stdout or '解析失败')[-1400:]}")
        if raw is None or selected is None:
            raise RuntimeError('ALL_YOUTUBE_STRATEGIES_FAILED\n' + '\n'.join(errors)[-5000:])
    finally:
        if cookie:
            cookie.unlink(missing_ok=True)
    videos, audios = core.simplify(raw.get('formats') or [])
    for item in videos + audios:
        item['strategy'] = selected.key
    return {'id':raw.get('id'),'title':raw.get('title'),'uploader':raw.get('uploader') or raw.get('channel'),'platform':raw.get('extractor_key') or raw.get('extractor'),'duration':raw.get('duration'),'thumbnail':raw.get('thumbnail'),'is_live':bool(raw.get('is_live') or raw.get('live_status') == 'is_live'),'drm':bool(raw.get('has_drm')),'video_options':videos,'audio_options':audios,'subtitles':sorted(set(raw.get('subtitles') or {}) | set(raw.get('automatic_captions') or {})),'webpage_url':raw.get('webpage_url') or url,'download_strategy':selected.key,'download_strategy_label':selected.label,'pot_provider':pot_available()}
