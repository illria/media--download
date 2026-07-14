from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import app as core
from starlette.responses import Response

_original_error = core.err
POT_PROVIDER_URL = os.getenv("POT_PROVIDER_URL", "http://youtube-pot-provider:4416").rstrip("/")
