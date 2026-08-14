"""CLI entrypoint for the expired-listing bot.

First-time setup (once per machine):
    python -m ct_expired_bot --login
    python -m ct_expired_bot --seed-portals --master "CT-Expired-Master.xlsx"

Normal run:
    python -m ct_expired_bot \
        --master "~/Documents/Expired Leads/CT-Expired-Master.xlsx" \
        --alerts-dir ~/Downloads/smartmls-alerts \
        --csv-headers "First,Last,Address,City,State,Zip,Mail Addr,Mail City,Mail St,Mail Zip"

Confirming the MLS labels against your board's display (see mls.py):
    python -m ct_expired_bot --mls 24012345 --dump-html /tmp/detail.html

The IMAP password is read from CT_EXPIRED_IMAP_PASSWORD and is never
written to disk or into the workbook.
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from .browser import run_login_flow
from .pipeline import RunConfig, run, seed_portal_registry
from .skiptrace import CANONICAL_FIELDS, HeaderSpecError


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ct_expired_bot",
        description=(
            "Turn new expired/withdrawn/canceled SmartMLS listings into a "
            "skip-trace-ready lead file. Uses an existing logged-in browser "
            "profile; never stores MLS credentials. Check your SmartMLS "
            "participant agreement on automated retrieval before scheduling."
        ),
    )
    p.add_argument("--master", default="CT-Expired-Master.xlsx", help="Master workbook path (Leads / Town Portals / Meta). Created on first run.")
    p.add_argument("--alerts-dir", default=None, help="Directory of saved SmartMLS alert emails (.eml/.html).")
    p.add_argument("--imap-host", default="", help="IMAP host for fetching alerts (e.g. outlook.office365.com, imap.gmail.com).")
    p.add_argument("--imap-user", default="", help="IMAP username. Password comes from CT_EXPIRED_IMAP_PASSWORD. NOTE: Microsoft disabled basic-auth IMAP on Exchange Online -- use the --graph-* options for an M365 mailbox.")
    p.add_argument("--graph-client-id", default=os.environ.get("CT_EXPIRED_GRAPH_CLIENT_ID", ""), help="Entra ID application (client) ID for reading mail over Microsoft Graph. Defaults to CT_EXPIRED_GRAPH_CLIENT_ID.")
    p.add_argument("--graph-tenant", default=os.environ.get("CT_EXPIRED_GRAPH_TENANT", ""), help="Entra ID directory (tenant) ID. Defaults to CT_EXPIRED_GRAPH_TENANT.")
    p.add_argument("--graph-token-cache", default=None, help="Where the Graph refresh token is cached (default: ~/.ct_expired_bot/graph-token.json, mode 0600).")
    p.add_argument("--graph-login", action="store_true", help="Sign into Microsoft Graph once via device code, cache the token, print the mailbox, then exit.")
    p.add_argument("--sender", default=None, help="Restrict the alert search to this From: address.")
    p.add_argument("--output-dir", default=".", help="Where skiptrace-YYYY-MM-DD.csv and review-YYYY-MM-DD.csv are written.")
    p.add_argument("--csv-headers", default=None, help=f"Your vendor's exact headers, comma-separated, in this order: {', '.join(CANONICAL_FIELDS)}")
    p.add_argument("--csv-header-map", default=None, help='JSON {"Your Header": "canonical_field"} for a vendor whose column set differs in shape.')
    p.add_argument("--listing-url-template", default=os.environ.get("CT_EXPIRED_LISTING_URL", ""), help="connectMLS detail-page URL with {mls_no} where the MLS number goes, e.g. 'https://host/listing?mls={mls_no}'. Required for Step 2; without it every row is marked MLS_PULL_FAILED. Defaults to CT_EXPIRED_LISTING_URL.")
    p.add_argument("--graph-folder", default="", help="Mail folder to read instead of the whole mailbox, e.g. 'CT-Expired Listings'. Searched recursively by display name.")
    p.add_argument("--subject-filter", default="", help="Only parse messages whose subject contains this (e.g. 'Updated Matches'). Empty means no subject filtering -- correct when --graph-folder already isolates the alerts.")
    p.add_argument("--label-overrides", default=None, help='JSON {"beds": ["Bedrooms"]} overriding the MLS detail labels in mls.py.')
    p.add_argument("--profile-dir", default=None, help="Chrome profile directory holding the SmartMLS session (default: ~/.ct_expired_bot/chrome-profile).")
    p.add_argument("--limit", type=int, default=None, help="Process at most this many new listings (useful for a first test batch).")
    p.add_argument("--delay", type=float, default=2.0, help="Seconds between MLS page loads.")
    p.add_argument("--no-discover-portals", action="store_true", help="Do not web-search for unknown town portals; mark them PORTAL_UNAVAILABLE instead.")
    p.add_argument("--headed", action="store_true", help="Run the browser headed (debugging).")

    p.add_argument("--login", action="store_true", help="Open a browser window to sign into SmartMLS once, then exit.")
    p.add_argument("--seed-portals", action="store_true", help="Fill the Town Portals tab for all 169 CT towns from the confirmed Vision list, then exit.")
    p.add_argument("--verify-portals", action="store_true", help="With --seed-portals, also web-discover the towns that are not on Vision (slow).")
    p.add_argument("--mls", default=None, help="Fetch one MLS number and print what was captured, then exit. For confirming labels.")
    p.add_argument("--dump-html", default=None, help="With --mls, write the raw page HTML here.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _config_from_args(args: argparse.Namespace) -> RunConfig:
    return RunConfig(
        master_path=Path(args.master).expanduser(),
        alerts_dir=Path(args.alerts_dir).expanduser() if args.alerts_dir else None,
        imap_host=args.imap_host,
        imap_user=args.imap_user,
        imap_password=os.environ.get("CT_EXPIRED_IMAP_PASSWORD", ""),
        graph_client_id=args.graph_client_id,
        graph_tenant=args.graph_tenant,
        graph_token_cache=Path(args.graph_token_cache).expanduser() if args.graph_token_cache else None,
        sender_filter=args.sender,
        profile_dir=Path(args.profile_dir).expanduser() if args.profile_dir else None,
        headless=not args.headed,
        label_overrides=Path(args.label_overrides).expanduser() if args.label_overrides else None,
        listing_url_template=args.listing_url_template,
        graph_folder=args.graph_folder,
        subject_filter=args.subject_filter,
        csv_headers=args.csv_headers,
        csv_header_map=Path(args.csv_header_map).expanduser() if args.csv_header_map else None,
        output_dir=Path(args.output_dir).expanduser(),
        limit=args.limit,
        delay_seconds=args.delay,
        discover_portals=not args.no_discover_portals,
    )


async def _single_mls(args: argparse.Namespace, config: RunConfig) -> int:
    from dataclasses import asdict

    from .browser import launch_session_context
    from .mls import fetch_detail, load_label_overrides

    labels = load_label_overrides(config.label_overrides)
    async with async_playwright() as playwright:
        context = await launch_session_context(
            playwright, profile_dir=config.profile_dir, headless=config.headless
        )
        try:
            detail = await fetch_detail(
                context, args.mls, labels=labels, dump_html_to=args.dump_html,
                url_template=config.listing_url_template,
            )
        finally:
            await context.close()

    for key, value in asdict(detail).items():
        print(f"{key:24s} = {value!r}")
    return 0 if detail.pull_status == "ok" else 1


async def _main_async(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = _config_from_args(args)

    if args.login:
        async with async_playwright() as playwright:
            await run_login_flow(playwright, profile_dir=config.profile_dir)
        return 0

    if args.graph_login:
        from .graph_mail import GraphAuthError, acquire_token, describe_mailbox

        if not (config.graph_client_id and config.graph_tenant):
            print(
                "--graph-login needs --graph-client-id and --graph-tenant (see the "
                "app-registration steps in ct_expired_bot/graph_mail.py).",
                file=sys.stderr,
            )
            return 2
        try:
            acquire_token(
                config.graph_client_id, config.graph_tenant,
                cache_path=config.graph_token_cache, interactive=True,
            )
            mailbox = describe_mailbox(
                config.graph_client_id, config.graph_tenant, cache_path=config.graph_token_cache
            )
        except GraphAuthError as exc:
            print(f"Graph sign-in failed: {exc}", file=sys.stderr)
            return 1
        print(f"Signed in. Reading mail as: {mailbox}")
        return 0

    if args.seed_portals:
        written = await seed_portal_registry(config, verify=args.verify_portals)
        print(f"Town Portals: {written} rows written to {config.master_path}")
        return 0

    if args.mls:
        return await _single_mls(args, config)

    has_graph = bool(config.graph_client_id and config.graph_tenant)
    has_imap = bool(config.imap_host and config.imap_user)
    if not config.alerts_dir and not has_graph and not has_imap:
        print(
            "Nothing to read. Pass one of:\n"
            "  --graph-client-id/--graph-tenant  (Microsoft 365 mailbox -- run --graph-login first)\n"
            "  --alerts-dir DIR                  (saved .eml/.html alert files)\n"
            "  --imap-host/--imap-user           (IMAP; not available on Exchange Online)",
            file=sys.stderr,
        )
        return 2
    if config.imap_host and config.imap_user and not config.imap_password:
        print("CT_EXPIRED_IMAP_PASSWORD is not set.", file=sys.stderr)
        return 2

    report = await run(config)
    print()
    for line in report.summary_lines():
        print(line)
    if report.skiptrace_path:
        print(f"\nSkip trace CSV: {report.skiptrace_path}")
        print(f"Review CSV:     {report.review_path}")
    for warning in report.warnings:
        print(f"\nWARNING: {warning}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        return asyncio.run(_main_async(args))
    except HeaderSpecError as exc:
        print(f"CSV header problem: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
