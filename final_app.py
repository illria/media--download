from __future__ import annotations

import os
from pathlib import Path

import runtime_app
import subtitle_feature
import youtube_hotfix
import youtube_reliability
from starlette.responses import Response


youtube_hotfix.install(runtime_app.core, youtube_reliability)
subtitle_feature.install(runtime_app.core)


@runtime_app.core.app.get("/api/tasks/{task_id}/file-status")
def task_file_status(task_id: str):
    task = runtime_app.core.row(task_id)
   