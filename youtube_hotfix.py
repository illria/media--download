from __future__ import annotations

from pathlib import Path
from typing import Any


def install(core: Any, yr: Any) -> None:
    original_strategies = yr.strategies

    def strategies(app: Any, url: str, preferred: str | None = None):
        items = original_strategies(app, url, preferred)
        if not app.yt(url):
            return items
        order = {"default": 0, "mweb-pot": 1, "web-safari": 2, "android-vr": 3, "web-embedded": 4}
        return sorted(items, key=lambda item: order.get(item.key, 99))

    def base(app: Any, url: str, cookie: Path | None, strategy: Any) -> list[str]:
        cfg = app.settings()
        args = [
            "yt-dlp", "--no-playlist", "--force-ipv4",
            "--socket-timeout", "10", "--retries", "1",
            "--fragment-retries", "2", "--extractor-retries", "0",
            "--js-runtimes", "deno", "--no-check-formats",
        ]
        proxy = str(cfg.get("proxy_url") or "").strip()
        if proxy:
            args += ["--proxy", proxy]
        if cookie:
            args += ["--cookies", str(cookie)]
        if app.yt(url) and strategy.client:
            args += ["--extractor-args", f"youtube:player_client={strategy.client}"]
        if app.yt(url):
            args += ["--extractor-args", f"youtubepot-bgutilhttp:base_url={yr.POT_PROVIDER_URL}"]
        return args

    yr.strategies = strategies
    yr.base = base
    core.base = lambda url, cookie=None, compat=False: yr.compatibility_base(core, url, cookie, compat)
    core.youtube_strategies = lambda url, preferred=None: yr.strategies(core, url, preferred)
