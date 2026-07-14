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
_POT_CACHE = {'checked': 0.0, 'available': False}
_PREFLIGHT_CACHE: dict[str, Any] = {'checked': 0.0, 'result': None}


@dataclass(frozen=True)
class Strategy:
    key: str
    label: str
    client: str | None = None
    use_pot: bool = False
    timeout: int = 26


GENERIC = Strategy('generic', '通用解析', timeout=35)
MWEB_POT = Strategy('mweb-pot', 'mweb + PO Token', client='mweb', use_pot=True, timeout=30)
DEFAULT = Strategy('default', '默认客户端', timeout=26)
WEB_SAFARI = Strategy('web-safari', 'Safari/HLS', client='web_safari', timeout=24)
ANDROID_VR = Strategy('android-vr', 'Android VR', client='android_vr', timeout=22)
WEB_EMBEDDED = Strategy('web-embedded', '嵌入客户端', client='web_embedded', timeout=20)


def canonical_url(core: Any, url: str) -> str:
    if not core.yt(url):
        return url
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    video_id = ''
    if host == 'youtu.be':
        video_id = parsed.path.strip('/').split('/')[0]
    elif parsed.path == '/watch':
        video_id = (parse