"""`--discover-api`: record what connectMLS asks its own API for.

The portal is a single-page app, so every field on a listing page comes
from a JSON call. Probing from outside showed `/api/listing/{id}` and
`/api/listings/{id}` answering 401 rather than 404 -- they exist and need
a session -- but the exact path, the id to use (MLS number, or the
internal `Id` GUID the payload also carries), and the auth header are all
unknown. Guessing at them would produce a scraper that silently returns
nothing.

So instead of guessing: open a real browser on the profile that is
already logged in, let the operator click to any listing, and write down
exactly which JSON endpoints fired and what fields came back. Then those
endpoints get wired into connectmls_api.py from evidence.

SECRETS ARE NOT WRITTEN DOWN. Authorization and Cookie headers are
recorded as presence-and-scheme only ("Bearer <redacted, 812 chars>"),
never their values, because this report is meant to be pasteable. Field
NAMES are recorded; field VALUES are not, apart from the small set of
identifiers needed to tell which id form the endpoint wanted.
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

from .browser import CONNECTMLS_BASE_URL, launch_session_context

log = logging.getLogger(__name__)

SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token"}
IGNORE_HOSTS = ("googleapis.com", "google-analytics", "doubleclick", "cloud.es.io", "gstatic")


def _redact(name: str, value: str) -> str:
    if name.lower() not in SENSITIVE_HEADERS:
        return value
    scheme = value.split(" ", 1)[0] if " " in value else "(raw)"
    return f"{scheme} <redacted, {len(value)} chars>"


def _field_names(obj, prefix="", depth=0, out=None):
    out = [] if out is None else out
    if depth > 3:
        return out
    if isinstance(obj, dict):
        for key, value in obj.items():
            out.append(prefix + key)
            _field_names(value, prefix + key + ".", depth + 1, out)
    elif isinstance(obj, list) and obj:
        _field_names(obj[0], prefix + "[].", depth + 1, out)
    return out


async def discover(profile_dir: Path | None, output_path: Path, start_url: str = "") -> Path:
    """Open a headed browser and record JSON traffic until it is closed."""
    records: list[dict] = []
    seen: set[str] = set()

    async with async_playwright() as playwright:
        context = await launch_session_context(
            playwright, profile_dir=profile_dir, headless=False
        )

        async def on_response(response):
            url = response.url
            if any(host in url for host in IGNORE_HOSTS):
                return
            content_type = (response.headers or {}).get("content-type", "")
            if "json" not in content_type.lower():
                return
            # Collapse repeats of the same endpoint shape.
            shape = re.sub(r"/[0-9A-Fa-f-]{8,}", "/{id}", url.split("?")[0])
            if shape in seen:
                return
            seen.add(shape)
            try:
                body = await response.text()
                data = json.loads(body)
            except Exception:
                return
            request = response.request
            records.append({
                "url": url,
                "endpoint_shape": shape,
                "method": request.method,
                "status": response.status,
                "request_headers": {
                    k: _redact(k, v) for k, v in (request.headers or {}).items()
                    if k.lower() in SENSITIVE_HEADERS or k.lower().startswith("x-")
                },
                "response_bytes": len(body),
                "field_names": _field_names(data)[:220],
            })
            log.info("captured %s (%d fields)", shape, len(records[-1]["field_names"]))

        page = await context.new_page()
        page.on("response", on_response)
        await page.goto(start_url or CONNECTMLS_BASE_URL, wait_until="domcontentloaded")

        print(
            "\nA browser window is open on your logged-in connectMLS profile.\n"
            "  1. Open ANY listing's full detail view (ideally an expired one).\n"
            "  2. Open its price/listing history if there is a tab for it.\n"
            "  3. Close the window when done.\n"
            f"Endpoints and FIELD NAMES will be written to {output_path}.\n"
            "Auth headers are recorded as redacted placeholders, never values.\n",
            flush=True,
        )
        await context.wait_for_event("close", timeout=0)

    report = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "endpoint_count": len(records),
        "endpoints": records,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=1))
    return output_path


def summarize(report_path: Path) -> str:
    """Human-readable digest, for pasting back into a conversation."""
    report = json.loads(Path(report_path).read_text())
    lines = [f"{report['endpoint_count']} JSON endpoints captured", ""]
    wanted = ("dom", "day", "date", "price", "expir", "remark", "agent",
              "office", "history", "status", "bed", "bath", "sqft", "mls")
    for record in report["endpoints"]:
        lines.append(f"{record['status']} {record['method']} {record['endpoint_shape']}")
        hits = [f for f in record["field_names"] if any(w in f.lower() for w in wanted)]
        if hits:
            lines.append(f"    relevant fields: {', '.join(hits[:30])}")
        if record["request_headers"]:
            lines.append(f"    auth headers: {list(record['request_headers'])}")
        lines.append("")
    return "\n".join(lines)
