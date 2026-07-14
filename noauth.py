from __future__ import annotations

import os
from typing import Any

from app import TOK, app as protected_app


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


AUTH_ENABLED = env_flag("AUTH_ENABLED", False)
BYPASS_TOKEN = TOK.dumps({"sub": "admin"})
NO_AUTH_STYLE = (
    "<style id=\"media-hub-no-auth\">"
    "#auth{display:none!important}"
    "button[onclick=\"logout()\"]{display:none!important}"
    "</style>"
)
NO_AUTH_SCRIPT = """<script id="media-hub-no-auth-script">
try {
  token = 'no-auth';
  localStorage.setItem('mediaToken', 'no-auth');
  document.getElementById('auth')?.classList.add('hidden');
  document.querySelector('button[onclick="logout()"]')?.remove();
  Promise.resolve().then(() => boot()).catch(console.error);
} catch (error) {
  console.error('No-auth bootstrap failed', error);
}
</script>"""


class OptionalAuthApp:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if AUTH_ENABLED or scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        forwarded_scope = dict(scope)
        headers = [
            (name, value)
            for name, value in scope.get("headers", [])
            if name.lower() != b"authorization"
        ]
        headers.append((b"authorization", f"Bearer {BYPASS_TOKEN}".encode("ascii")))
        forwarded_scope["headers"] = headers

        if scope.get("path") not in {"/", "/index.html"}:
            await self.app(forwarded_scope, receive, send)
            return

        start_message: dict[str, Any] | None = None
        body_parts: list[bytes] = []

        async def capture(message: dict[str, Any]) -> None:
            nonlocal start_message
            if message["type"] == "http.response.start":
                start_message = message
                return
            if message["type"] != "http.response.body":
                await send(message)
                return

            body_parts.append(message.get("body", b""))
            if message.get("more_body", False):
                return

            body = b"".join(body_parts)
            if start_message is None:
                await send(message)
                return

            response_headers = list(start_message.get("headers", []))
            content_type = next(
                (
                    value.decode("latin-1").lower()
                    for name, value in response_headers
                    if name.lower() == b"content-type"
                ),
                "",
            )
            if "text/html" in content_type:
                text = body.decode("utf-8", errors="replace")
                text = text.replace("<head>", f"<head>{NO_AUTH_STYLE}", 1)
                text = text.replace("</body>", f"{NO_AUTH_SCRIPT}</body>", 1)
                body = text.encode("utf-8")
                response_headers = [
                    (name, value)
                    for name, value in response_headers
                    if name.lower() not in {b"content-length", b"transfer-encoding"}
                ]
                response_headers.append((b"content-length", str(len(body)).encode("ascii")))

            await send({**start_message, "headers": response_headers})
            await send({"type": "http.response.body", "body": body, "more_body": False})

        await self.app(forwarded_scope, receive, capture)


app = OptionalAuthApp(protected_app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("APP_PORT", "19190")), workers=1)
