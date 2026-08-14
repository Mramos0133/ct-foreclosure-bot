"""Playwright setup that reuses the operator's real SmartMLS login.

This is the part of the bot that cannot run in a cloud container, and the
reason is worth stating plainly: SmartMLS (connectMLS) is behind an
authenticated session, the spec forbids storing credentials, and a
session cookie lives in a browser profile on a particular machine. So
this module never logs in. It attaches to a Chrome/Chromium *profile
directory* that already has a live SmartMLS session, via
`launch_persistent_context` -- the same mechanism Chrome itself uses, so
nothing is copied, scraped out of, or decrypted from the cookie store.

Two supported ways to point it at a session:

  --chrome-profile /path/to/Chrome/User Data/Default
      Your everyday profile. Chrome must be fully quit first: Chromium
      takes an exclusive lock on a profile directory, and a second
      process attaching to a live one fails (or, worse, corrupts it).

  (default) ~/.ct_expired_bot/chrome-profile
      A dedicated profile this bot owns. Run `--login` once, sign into
      SmartMLS in the window that opens, close it, and the session
      persists here for subsequent headless runs. This is the
      recommended setup -- it never contends with your daily browser.

Headless is the default for real runs, but note that a persistent
context started headless still carries the profile's cookies; headless
only affects whether a window is drawn.
"""

import os
from pathlib import Path

from playwright.async_api import BrowserContext

# SmartMLS runs on connectMLS (dynaConnections), not Matrix. Confirmed
# 2026-08-13 from an alert email's "View All Listings" link, which points
# at smartmls-portal.connectmls.com and redirects to a connectMLS login;
# matrix.smartmls.com is not the system this account uses. The agent-side
# host is set with --mls-base-url if it differs from the portal host.
CONNECTMLS_BASE_URL = "https://smartmls-portal.connectmls.com"
DEFAULT_PROFILE_DIR = Path.home() / ".ct_expired_bot" / "chrome-profile"

# Kept for parity with ct_foreclosure_bot.browser: this tool does not
# disguise itself as manual browsing. Check your SmartMLS participant
# agreement on automated retrieval before scheduling it.
USER_AGENT_NOTE = "Chromium default UA; no spoofing."


def _launch_args(disable_tls12_workaround: bool = False) -> list[str]:
    """--ssl-version-max=tls1.2 is the same workaround ct_foreclosure_bot's
    browser.py carries, and it is needed here for the same reason: some
    TLS-intercepting proxies reset the connection on Chromium's TLS 1.3
    ClientHello. Confirmed necessary against gis.vgsi.com on 2026-08-04 --
    without it every assessor page load failed ERR_CONNECTION_RESET; with
    it, the same lookups succeed. Harmless against a normal TLS 1.2-capable
    server, so it stays on by default.
    """
    args = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
    if not disable_tls12_workaround:
        args.append("--ssl-version-max=tls1.2")
    return args


async def launch_session_context(
    playwright,
    profile_dir: str | os.PathLike | None = None,
    headless: bool = True,
    channel: str | None = "chrome",
    proxy_server: str | None = None,
    disable_tls12_workaround: bool = False,
) -> BrowserContext:
    """Open a persistent context on a profile that already holds the login.

    `channel="chrome"` uses the installed Google Chrome rather than
    Playwright's bundled Chromium, because the profile you already log
    into is a Chrome profile and the two are not interchangeable. Pass
    channel=None to force bundled Chromium (only useful with the
    bot-owned profile dir, which it creates itself).
    """
    profile_path = Path(profile_dir) if profile_dir else DEFAULT_PROFILE_DIR
    profile_path.mkdir(parents=True, exist_ok=True)

    kwargs: dict = {
        "user_data_dir": str(profile_path),
        "headless": headless,
        "args": _launch_args(disable_tls12_workaround),
        "accept_downloads": True,
    }
    executable_path = os.environ.get("CT_BOT_CHROMIUM_PATH")
    if executable_path:
        # Same env-var override ct_foreclosure_bot uses. An explicit
        # executable and a release channel are mutually exclusive.
        kwargs["executable_path"] = executable_path
    elif channel:
        kwargs["channel"] = channel

    proxy_server = proxy_server or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy_server:
        kwargs["proxy"] = {"server": proxy_server}

    return await playwright.chromium.launch_persistent_context(**kwargs)


async def is_logged_in(context: BrowserContext, timeout_ms: int = 20000) -> bool:
    """Best-effort check that the profile's SmartMLS session is still live.

    connectMLS bounces an unauthenticated request to /login, so a landing
    URL containing "login" is the signal (confirmed 2026-08-13). This is
    intentionally loose --
    it is a pre-flight warning, not a gate, and a false negative here
    costs one printed warning rather than a failed run.
    """
    page = await context.new_page()
    try:
        await page.goto(CONNECTMLS_BASE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        landed = (page.url or "").lower()
        return "login" not in landed and "signin" not in landed
    except Exception:
        return False
    finally:
        await page.close()


async def run_login_flow(playwright, profile_dir: str | os.PathLike | None = None) -> None:
    """Open a headed window so the operator can sign in once.

    Blocks until the window is closed. Nothing is captured from the
    session -- the cookie is written by Chrome into the profile dir.
    """
    context = await launch_session_context(playwright, profile_dir=profile_dir, headless=False)
    page = await context.new_page()
    await page.goto(CONNECTMLS_BASE_URL, wait_until="domcontentloaded")
    print(
        "Sign into SmartMLS in the browser window, then close it.\n"
        f"The session will persist in {profile_dir or DEFAULT_PROFILE_DIR}."
    )
    # Resolves when the operator closes the window.
    await context.wait_for_event("close", timeout=0)
