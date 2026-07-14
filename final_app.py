from __future__ import annotations

import os

import runtime_app
import subtitle_feature
import youtube_hotfix
import youtube_reliability
from starlette.responses import Response


youtube_hotfix.install(runtime_app.core, youtube_reliability)
subtitle_feature.install(runtime_app.core)


@runtime_app.core.app.middleware("http")
async def inject_subtitle_script(request, call_next):
    response = await call_next(request)
    if request.url.path not in {"/", "/index.html"}:
        return response
    if "text/html" not in response.headers.get("content-type", ""):
        return response
    body = b""
    async for chunk in response.body_iterator:
        body += chunk
    html = body.decode("utf-8", errors="replace")
    script = "/assets/subtitle_auto.js"
    if script not in html:
        html = html.replace("</body>", f'<script src="{script}"></script></body>')
    headers = dict(response.headers)
    headers.pop("content-length", None)
    return Response(content=html, status_code=response.status_code, headers=headers, media_type="text/html")


app = runtime_app.core.app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("APP_PORT", "19190")), workers=1)
