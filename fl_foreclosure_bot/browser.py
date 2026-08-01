"""Playwright browser setup.

Simpler than the CT bot's: no ASP.NET postback dance here, and (per the
site's own AWS WAF behavior observed during development -- see
throttle.py) this is expected to run from a normal home/office network,
not a datacenter/proxy IP, so the TLS-1.2-forcing workaround that project
needed for its dev sandbox isn't included by default here. It's kept as
an opt-in flag in case a corporate/VPN network hits the same
TLS-interception issue.
"""

from __future__ import annotations

import os
from playwright.async_api import Browser, BrowserContext


def _default_launch_args(force_tls12: bool) -> list[str]:
    args = ["--no-sandbox"]
    if force_tls12:
        args.append("--ssl-version-max=tls1.2")
    return args


async def launch_browser(
    playwright,
    headless: bool = True,
    proxy_server: str | None = None,
    force_tls12: bool = False,
) -> Browser:
    proxy_server = proxy_server or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    launch_kwargs = dict(
        headless=headless,
        args=_default_launch_args(force_tls12),
    )
    if proxy_server:
        launch_kwargs["proxy"] = {"server": proxy_server}
    executable_path = os.environ.get("FL_BOT_CHROMIUM_PATH")
    if executable_path:
        launch_kwargs["executable_path"] = executable_path
    return await playwright.chromium.launch(**launch_kwargs)


# A real-run produced an empty result set end-to-end (pages fetched, zero
# cards recognized) while the same pages rendered fine in the user's own
# browser. The site sits behind AWS WAF, which commonly rejects requests
# whose User-Agent carries headless Chromium's "HeadlessChrome" token, so
# the context pins a normal desktop-Chrome UA instead. The tool still
# rate-limits conservatively and runs strictly sequentially -- behavior,
# not the UA string, is what keeps its footprint polite.
_DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


async def new_context(browser: Browser, ignore_https_errors: bool = False) -> BrowserContext:
    return await browser.new_context(
        ignore_https_errors=ignore_https_errors,
        user_agent=_DESKTOP_UA,
        viewport={"width": 1280, "height": 900},
    )
