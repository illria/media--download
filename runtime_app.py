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


_original_error = core.err


def _base(url: str, cookie_path: Path | None = None, compat: bool = False) -> list[str]:
    settings = core.settings()
    args = [
        "yt-dlp",
        "--no-playlist",
        "--force-ipv4",
        "--socket-timeout",
        "8",
        "--