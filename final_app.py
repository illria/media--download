from __future__ import annotations

import os

import runtime_app
import subtitle_feature
import youtube_hotfix
import youtube_reliability


youtube_hotfix.install(runtime_app.core, youtube_reliability)
subtitle_feature.install(runtime_app.core)

app = runtime_app.core.app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("APP_PORT", "19190")), workers=1)
