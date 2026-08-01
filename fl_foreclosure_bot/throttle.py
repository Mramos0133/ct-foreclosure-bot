"""Conservative rate limiting for requests to realforeclose.com.

robots.txt for this site could not be checked during development -- every
request from the dev sandbox (including a plain GET to /robots.txt) was
rejected with a 403 at the network edge (an AWS WAF blocking that
sandbox's outbound IP range, confirmed across curl, a headless browser,
and a URL-fetch tool -- not something a different request shape fixes).
Running from a normal residential/office network should not hit this.
Check the site's actual robots.txt yourself before scaling up frequency;
the default here is a conservative single-sequential-request pace, same
posture as the CT bot took toward its own site.
"""

from __future__ import annotations

import asyncio
import random
import time


class Throttle:
    def __init__(self, min_delay: float = 1.5, max_delay: float = 2.5):
        if min_delay <= 0 or max_delay < min_delay:
            raise ValueError("require 0 < min_delay <= max_delay")
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._last_request_at: float | None = None

    async def wait(self) -> None:
        now = time.monotonic()
        if self._last_request_at is not None:
            elapsed = now - self._last_request_at
            delay = random.uniform(self.min_delay, self.max_delay)
            remaining = delay - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._last_request_at = time.monotonic()
