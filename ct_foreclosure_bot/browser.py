"""Playwright browser setup.

Everything -- form search, GridView pagination, docket pages, and PDF
document downloads -- goes through one headless Chromium instance via
Playwright, rather than mixing in a plain `requests` session. That is a
deliberate simplification, not the minimal implementation: the site is a
postback-heavy ASP.NET WebForms app that could largely be driven with
`requests` + extracted __VIEWSTATE tokens, but doing so was unreliable in
this project's own development/sandbox network (a proxy-layer TLS 1.3
incompatibility -- see NOTES below), while Chromium was consistently
reliable once forced to TLS 1.2. Keeping one browser context for the whole
pipeline avoids maintaining two separate HTTP code paths for a site that
already works end-to-end through the browser.

NOTES on the TLS workaround: if you hit `net::ERR_CONNECTION_RESET` talking
to civilinquiry.jud.ct.gov from behind a corporate/dev proxy, it is very
likely the same issue seen during development of this tool: some TLS
intercepting proxies choke on Chromium's TLS 1.3 ClientHello (GREASE
values / post-quantum key share make it unusually large). Forcing
Chromium's max TLS version down to 1.2 fixed it there. On a normal
network you can likely drop --ssl-version-max=tls1.2 entirely; it is kept
as a default here because it is harmless against a normal TLS 1.2-capable
server (which jud.ct.gov is) and only helps in the broken-proxy case.
"""

import os
from playwright.async_api import async_playwright, Browser, BrowserContext

BASE_URL = "https://civilinquiry.jud.ct.gov"
USER_AGENT_NOTE = (
    "Uses Chromium's default headless UA. No attempt is made to disguise "
    "this as manual browsing -- the tool identifies as automated by "
    "virtue of the rate limiting and checkpointing behavior around it, "
    "not by spoofing."
)


def _default_launch_args(disable_tls12_workaround: bool) -> list[str]:
    args = ["--no-sandbox"]
    if not disable_tls12_workaround:
        args.append("--ssl-version-max=tls1.2")
    return args


async def launch_browser(
    playwright,
    headless: bool = True,
    proxy_server: str | None = None,
    disable_tls12_workaround: bool = False,
) -> Browser:
    proxy_server = proxy_server or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    launch_kwargs = dict(
        headless=headless,
        args=_default_launch_args(disable_tls12_workaround),
    )
    if proxy_server:
        launch_kwargs["proxy"] = {"server": proxy_server}
    executable_path = os.environ.get("CT_BOT_CHROMIUM_PATH")
    if executable_path:
        launch_kwargs["executable_path"] = executable_path
    return await playwright.chromium.launch(**launch_kwargs)


async def new_context(browser: Browser, ignore_https_errors: bool = False) -> BrowserContext:
    return await browser.new_context(ignore_https_errors=ignore_https_errors)
