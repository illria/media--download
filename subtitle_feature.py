from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any

import requests
from fastapi import HTTPException

ENDPOINT = "https://api.siliconflow.cn/v1/audio/transcriptions"
MODEL = "FunAudioLLM/SenseVoiceSmall"
META_KEY = "siliconflow_api_key"


def install(core: Any) -> None:
    original_execute = core.execute
    original_error = core.err

    def api_key() -> str:
        env = os.getenv("SILICONFLOW_API_KEY",