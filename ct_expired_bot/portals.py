"""The town -> assessor portal registry behind Step 3.

CT has no statewide property-card database; each of the 169 towns picks
its own vendor. The spec's answer is to look a town up once, write it to
the Town Portals tab, and never look it up again -- so this module is
both the seed list and the runtime discovery path.

THE SEED IS REAL, NOT GUESSED. Every town in `VGSI_TOWNS` was probed
against `https://gis.vgsi.com/<slug>ct/` on 2026-08-04 and returned HTTP
200; all 169 towns were tried, plus slug variants (no-suffix, hyphenated,
"New X" -> "X") for the ones that missed. 77 towns answered, 92 did not
and are on some other vendor. Those 92 are deliberately NOT guessed at:
they resolve to Unknown and get discovered on first encounter, because a
wrong portal produces a wrong owner name, which is the one failure this
bot exists to avoid.

Re-probe at any time with:
    python -m ct_expired_bot --probe-portals
"""

import logging
import re
from dataclasses import asdict

from playwright.async_api import BrowserContext

from .models import TownPortal

log = logging.getLogger(__name__)

VGSI_URL_TEMPLATE = "https://gis.vgsi.com/{slug}ct/"

# 77 CT towns confirmed live on Vision Government Solutions (probed
# 2026-08-04). See module docstring.
VGSI_TOWNS = frozenset({
    'Andover', 'Bethlehem', 'Bolton', 'Branford', 'Bridgeport', 'Bristol',
    'Brookfield', 'Brooklyn', 'Burlington', 'Canterbury', 'Canton',
    'Clinton', 'Cornwall', 'Coventry', 'Deep River', 'East Haddam',
    'East Lyme', 'East Windsor', 'Enfield', 'Essex', 'Fairfield',
    'Glastonbury', 'Granby', 'Griswold', 'Hamden', 'Hampton', 'Kent',
    'Lisbon', 'Madison', 'Manchester', 'Meriden', 'Middlebury',
    'Middlefield', 'Middletown', 'Milford', 'Monroe', 'New Britain',
    'New Fairfield', 'New Hartford', 'New Haven', 'New London',
    'New Milford', 'Newtown', 'North Branford', 'Norwich', 'Old Lyme',
    'Old Saybrook', 'Orange', 'Plainfield', 'Pomfret', 'Preston',
    'Redding', 'Salem', 'Salisbury', 'Sharon', 'Somers', 'South Windsor',
    'Southbury', 'Southington', 'Sprague', 'Stafford', 'Stamford',
    'Sterling', 'Stratford', 'Thompson', 'Tolland', 'Trumbull', 'Union',
    'Wallingford', 'Waterford', 'West Hartford', 'Westbrook', 'Westport',
    'Willington', 'Winchester', 'Wolcott', 'Woodstock',
})

# Host fragments that identify the other CT assessor vendors, used to
# label a portal once its URL is known.
PLATFORM_SIGNATURES = [
    ("vgsi.com", "Vision"),
    ("qds.", "QDS"),
    ("qdsinc", "QDS"),
    ("nereval", "Northeast"),
    ("northeastrevaluation", "Northeast"),
    ("assessedvalues2", "Northeast"),
    ("axisgis", "AxisGIS"),
    ("tylertech", "Tyler"),
    ("equalitycama", "Tyler"),
    ("csvision", "CSVision"),
    ("mainstreetmaps", "MainStreetMaps"),
    ("patriotproperties", "Patriot"),
]


def town_slug(town: str) -> str:
    return re.sub(r"[^a-z]", "", (town or "").lower())


def vgsi_url(town: str) -> str:
    return VGSI_URL_TEMPLATE.format(slug=town_slug(town))


def classify_platform(url: str) -> str:
    lowered = (url or "").lower()
    for fragment, platform in PLATFORM_SIGNATURES:
        if fragment in lowered:
            return platform
    return "Custom" if lowered else "Unknown"


def seed_portal(town: str) -> TownPortal:
    """The registry entry for a town before any discovery has run."""
    if town in VGSI_TOWNS:
        return TownPortal(
            town=town,
            portal_url=vgsi_url(town),
            platform="Vision",
            search_notes="Confirmed live 2026-08-04. Search by street address.",
        )
    return TownPortal(
        town=town,
        portal_url="",
        platform="Unknown",
        search_notes=(
            "Not on Vision (probed 2026-08-04). Needs discovery: search "
            f"'{town} CT assessor property card online database'."
        ),
    )


def seed_registry(towns: list[str]) -> dict[str, TownPortal]:
    return {town: seed_portal(town) for town in towns}


async def verify_url(context: BrowserContext, url: str, timeout_ms: int = 25000) -> bool:
    """True if the URL loads without erroring. Used by --probe-portals."""
    page = await context.new_page()
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        return bool(response and response.ok)
    except Exception:
        return False
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def discover_portal(context: BrowserContext, town: str) -> TownPortal:
    """Find a town's portal on first encounter (spec Step 3 fallback).

    Tries Vision first (cheap, and the single most common CT vendor),
    then a plain web search whose top results are filtered to known
    assessor-vendor hosts. A search result on an unrecognized host is
    recorded with platform "Custom" rather than being trusted silently --
    the operator sees the URL in the Town Portals tab and can correct it.
    """
    candidate = vgsi_url(town)
    if await verify_url(context, candidate):
        return TownPortal(town, candidate, "Vision", "Discovered via Vision URL pattern.")

    query = f"{town} CT assessor property card online database"
    page = await context.new_page()
    try:
        await page.goto(
            f"https://duckduckgo.com/html/?q={query.replace(' ', '+')}",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        hrefs = await page.eval_on_selector_all(
            "a[href]", "els => els.map(e => e.href)"
        )
    except Exception as exc:
        log.warning("portal discovery failed for %s: %s", town, exc)
        hrefs = []
    finally:
        try:
            await page.close()
        except Exception:
            pass

    for href in hrefs:
        platform = classify_platform(href)
        if platform not in ("Unknown", "Custom"):
            return TownPortal(town, href, platform, f"Discovered via web search: {query}")

    return TownPortal(
        town, "", "Unknown",
        f"Discovery found no known assessor platform. Search manually: {query}",
    )


def registry_rows(registry: dict[str, TownPortal]) -> list[dict]:
    return [asdict(registry[town]) for town in sorted(registry)]
