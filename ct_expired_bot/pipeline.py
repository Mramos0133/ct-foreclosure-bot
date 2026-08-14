"""End-to-end orchestration of Steps 1-6.

Ordering is dictated by cost, not by the step numbers. Dedupe against the
Leads tab happens before a single page is opened, so an alert re-sending
last week's listings costs nothing. The portal registry is consulted
before discovery, so a town is looked up once ever. And the MLS pull runs
before the assessor lookup because the assessor step needs MLS year built
and sqft to disambiguate parcels -- a listing whose MLS pull failed still
gets an assessor attempt, but with nothing to verify against, which is
precisely when the spec wants NEEDS_MANUAL_REVIEW rather than a guess.

Nothing in here raises on a single bad listing. Each of the three network
steps is independently failure-tolerant, and a listing that fails all
three still produces a row -- marked, and routed to the review file.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from playwright.async_api import async_playwright

from . import alerts as alerts_mod
from . import portals as portals_mod
from . import workbook as wb_mod
from .assessor import lookup_owner
from .browser import is_logged_in, launch_session_context
from .mls import fetch_detail, load_label_overrides
from .models import MLS_PULL_FAILED, AlertListing, Lead, MlsDetail, OwnerRecord, TownPortal
from .scoring import score_lead
from .skiptrace import resolve_headers, write_review_csv, write_skiptrace_csv

log = logging.getLogger(__name__)

FIRST_RUN_LOOKBACK_DAYS = 7


@dataclass
class RunConfig:
    master_path: Path
    alerts_dir: Path | None = None
    imap_host: str = ""
    imap_user: str = ""
    imap_password: str = ""
    graph_client_id: str = ""
    graph_tenant: str = ""
    graph_token_cache: Path | None = None
    sender_filter: str | None = None
    profile_dir: Path | None = None
    headless: bool = True
    label_overrides: Path | None = None
    listing_url_template: str = ""
    graph_folder: str = ""
    subject_filter: str = ""
    csv_headers: str | None = None
    csv_header_map: Path | None = None
    output_dir: Path = Path(".")
    limit: int | None = None
    delay_seconds: float = 2.0
    discover_portals: bool = True


@dataclass
class RunReport:
    new_found: int = 0
    enriched: int = 0
    needs_review: int = 0
    mls_failed: int = 0
    high_leads: list[tuple[str, str]] = field(default_factory=list)  # (address, town)
    skiptrace_path: str = ""
    review_path: str = ""
    warnings: list[str] = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        lines = [
            f"New found:      {self.new_found}",
            f"Enriched:       {self.enriched}",
            f"Needing review: {self.needs_review}",
            f"Failed:         {self.mls_failed}",
        ]
        if self.high_leads:
            lines.append("")
            lines.append("High-score leads:")
            lines += [f"  {addr}{f', {town}' if town else ''}" for addr, town in self.high_leads]
        return lines


def _since_datetime(last_run: str) -> datetime:
    if last_run:
        try:
            parsed = datetime.fromisoformat(last_run)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            log.warning("unparseable last_run %r; falling back to %d days", last_run, FIRST_RUN_LOOKBACK_DAYS)
    return datetime.now(timezone.utc) - timedelta(days=FIRST_RUN_LOOKBACK_DAYS)


def collect_alerts(config: RunConfig, since: datetime) -> list[AlertListing]:
    """Step 1, from whichever source is configured."""
    found: list[AlertListing] = []
    if config.alerts_dir:
        found += alerts_mod.load_from_dir(config.alerts_dir, since=since)
    if config.graph_client_id and config.graph_tenant:
        from .graph_mail import load_from_graph

        found += load_from_graph(
            config.graph_client_id,
            config.graph_tenant,
            since=since,
            sender_filter=config.sender_filter,
            subject_filter=config.subject_filter,
            folder=config.graph_folder,
            cache_path=config.graph_token_cache,
        )
    if config.imap_host and config.imap_user and config.imap_password:
        found += alerts_mod.load_from_imap(
            config.imap_host,
            config.imap_user,
            config.imap_password,
            since=since,
            sender_filter=config.sender_filter,
        )
    return alerts_mod.dedupe(found)


async def _resolve_portal(
    context, town: str, registry: dict[str, TownPortal], discovered: list[TownPortal],
    allow_discovery: bool,
) -> TownPortal:
    portal = registry.get(town)
    if portal and portal.platform != "Unknown" and portal.portal_url:
        return portal
    seed = portals_mod.seed_portal(town)
    if seed.platform != "Unknown":
        registry[town] = seed
        discovered.append(seed)
        return seed
    if not allow_discovery:
        registry[town] = seed
        discovered.append(seed)
        return seed
    found = await portals_mod.discover_portal(context, town)
    registry[town] = found
    discovered.append(found)
    return found


async def run(config: RunConfig, today: date | None = None) -> RunReport:
    report = RunReport()
    master = Path(config.master_path)

    created = wb_mod.ensure_workbook(master)
    if created:
        log.info("first run: created %s", master)

    headers = resolve_headers(config.csv_headers, config.csv_header_map)
    labels = load_label_overrides(config.label_overrides)

    since = _since_datetime(wb_mod.read_last_run(master))
    already_seen = wb_mod.existing_mls_numbers(master)
    registry = wb_mod.read_portals(master)

    candidates = collect_alerts(config, since)
    new_listings = [l for l in candidates if l.mls_no not in already_seen]
    if config.limit:
        new_listings = new_listings[: config.limit]
    report.new_found = len(new_listings)

    if not new_listings:
        wb_mod.write_last_run(master)
        return report

    leads: list[Lead] = []
    discovered_portals: list[TownPortal] = []

    async with async_playwright() as playwright:
        context = await launch_session_context(
            playwright, profile_dir=config.profile_dir, headless=config.headless
        )
        try:
            if not await is_logged_in(context):
                report.warnings.append(
                    "SmartMLS session did not look logged in -- MLS pulls will likely "
                    "fail. Run with --login to refresh the profile's session."
                )

            for index, listing in enumerate(new_listings, start=1):
                log.info("[%d/%d] MLS %s %s", index, len(new_listings), listing.mls_no, listing.street_address)

                detail = await fetch_detail(
                    context, listing.mls_no, labels=labels,
                    url_template=config.listing_url_template,
                )
                if detail.pull_status == MLS_PULL_FAILED:
                    report.mls_failed += 1

                town = detail.town if detail.town not in ("N/A", "") else listing.town
                if town:
                    portal = await _resolve_portal(
                        context, town, registry, discovered_portals, config.discover_portals
                    )
                    owner = await lookup_owner(context, portal, listing.street_address, detail)
                else:
                    owner = OwnerRecord(
                        status="NEEDS_MANUAL_REVIEW",
                        owner_name="NEEDS_MANUAL_REVIEW",
                        notes=["No town captured from the alert or the MLS page."],
                    )

                lead = score_lead(Lead(alert=listing, mls=detail, owner=owner), today=today)
                leads.append(lead)
        finally:
            await context.close()

    for lead in leads:
        if lead.needs_review:
            report.needs_review += 1
        else:
            report.enriched += 1
        if lead.lead_score == "High":
            town = lead.mls.town if lead.mls.town != "N/A" else lead.alert.town
            report.high_leads.append((lead.alert.street_address or lead.mls_no, town or ""))

    wb_mod.append_leads(master, leads)
    if discovered_portals:
        wb_mod.upsert_portals(master, discovered_portals)

    stamp = (today or date.today()).isoformat()
    output_dir = Path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    skiptrace_path = output_dir / f"skiptrace-{stamp}.csv"
    review_path = output_dir / f"review-{stamp}.csv"
    write_skiptrace_csv(skiptrace_path, leads, headers=headers)
    write_review_csv(review_path, leads)
    report.skiptrace_path = str(skiptrace_path)
    report.review_path = str(review_path)

    wb_mod.write_last_run(master)
    return report


async def seed_portal_registry(config: RunConfig, verify: bool = False) -> int:
    """Populate the Town Portals tab for all 169 towns without crawling."""
    from ct_foreclosure_bot.towns import CT_TOWNS

    master = Path(config.master_path)
    wb_mod.ensure_workbook(master)
    registry = portals_mod.seed_registry(list(CT_TOWNS))

    if verify:
        async with async_playwright() as playwright:
            context = await launch_session_context(
                playwright, profile_dir=config.profile_dir, headless=True
            )
            try:
                for town, portal in registry.items():
                    if portal.platform == "Unknown":
                        registry[town] = await portals_mod.discover_portal(context, town)
            finally:
                await context.close()

    return wb_mod.upsert_portals(master, list(registry.values()))
