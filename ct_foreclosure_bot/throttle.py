"""Conservative rate limiting for all requests to civilinquiry.jud.ct.gov.

The site's robots.txt disallows automated access entirely (Disallow: /).
This tool is used deliberately and carefully despite that, so the rate
limit here is treated as a hard requirement: every single network call
that hits the site -- search, pagination, docket fetch, document fetch --
must go through `Throttle.wait()` first. There is exactly one Throttle
instance per run, and the pipeline is single-threaded/sequential by
design, so this also guarantees no parallel requests to the host.
"""

import asyncio
import random
import time


class Throttle:
    def __init__(self, min_delay: float = 2.0, max_delay: float = 3.0):
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
