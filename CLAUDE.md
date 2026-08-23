# CT Foreclosure Bot

This repo now holds two separate bots:

- `ct_foreclosure_bot/` -- court-docket foreclosure scraper (the original;
  everything below the "Lead classification rules" heading is about this one)
- `ct_expired_bot/` -- expired/withdrawn MLS listing -> skip-trace pipeline
  (see "Expired listing bot" at the bottom)

## "Give me the updated leads" workflow

Whenever the user asks for updated/refreshed leads, run the `--update`
flow against the statewide checkpoint -- it already does exactly what
they want: discovers genuinely new matched cases, rechecks every
already-matched case for real changes (new motion, judgment granted,
bucket change, etc via a cheap docket-entry-count check first), and
exports the ranked workbook with **new-case rows highlighted green** and
**updated-case rows highlighted yellow** (see `NEW_ROW_FILL`/
`UPDATED_ROW_FILL` in `excel_export.py`). No new code needed for this --
just run it and hand back the file.

```
CT_BOT_CHROMIUM_PATH=/opt/pw-browsers/chromium python3 -m ct_foreclosure_bot \
  --update \
  --checkpoint-db statewide_checkpoint.sqlite3 \
  --output statewide_results.csv \
  --review-log statewide_review.csv \
  --export-xlsx statewide_leads.xlsx
```

This re-walks all 169 towns (cheap -- per-docket dedup skips everything
already seen) and rechecks every already-matched case, so it can take a
while (full statewide passes have taken on the order of an hour-plus in
this environment). Run it in the background and check back rather than
polling. After it finishes, send `statewide_leads.xlsx` to the user.

`CT_BOT_CHROMIUM_PATH=/opt/pw-browsers/chromium` is required in this
remote execution environment -- Playwright's own bundled browser isn't
present here; `browser.py` reads this env var as an `executable_path`
override.

## "Pull these specific cities" workflow

When the user asks for a list limited to particular towns, ALWAYS produce
it in the same format as the master statewide sheet -- same columns, same
HOT/WARM/COLD/POTENTIAL_SHORT_SALE/UNCLASSIFIED tabs, same green(new)/
yellow(updated) highlighting. Do not invent a bespoke layout; use
`scripts/city_pull.py`, which calls the same `export_to_xlsx()` the
master sheet uses, so the two never drift apart.

```
python3 scripts/city_pull.py Watertown Woodbury Middlebury Naugatuck Seymour \
  --output watertown_area_leads.xlsx \
  --door-knock \
  --live-auction /tmp/live_sales.json \
  --new-since <older_checkpoint.sqlite3>
```

`--door-knock` narrows to the field-visit subset (lender complaint within
`--complaint-days`, default 30, OR heading to auction). Omit it to export
every matched case in those towns.

Two staleness fixes the script applies, worth knowing about:

- `days_to_key_date` is frozen at scrape time in the checkpoint, so a row
  can claim "8 days" on a sale that already happened. The script always
  recomputes it against today.
- A stored `key_date` comes from the Order document at scrape time and is
  often missing for a case that was posted for sale later. Re-read the
  auction site and pass the fresh dates via `--live-auction`
  ({docket_no: "YYYY-MM-DD"} JSON) whenever the list is for real outreach
  -- on the last pull this filled in three sale dates that showed as
  blank, two of which were only days away.

## Lead classification rules

See the module docstring in `lead_ranking.py` for the full HOT/WARM/COLD
bucket logic, including the three independent extra HOT rules:
bankruptcy-then-reopened within 2-12 months, EMAP/loan-mod-then-failed
within 1-12 months, and recent-lender-complaint within 6 months (the
only rule that matches cases with no target motion at all -- complaint-
stage cases; their complaint PDF is OCR'd for principal owed and the
P&I-unpaid-since date, see `complaint_document.py`).

Note: the EMAP/loan-mod docket-entry phrasing patterns in `motions.py`
are a best-effort guess, not yet confirmed against real docket text like
the bankruptcy patterns were -- worth a spot-check against a real EMAP
case if one shows up in a future run.

## Live checkpoint files

- `statewide_checkpoint.sqlite3` -- the source of truth (tracked in git
  despite `.gitignore`'s general `*.sqlite3` rule -- see its own commit
  history). All other checkpoint/results/review files for this dataset
  are derived/regeneratable and gitignored.

## Expired listing bot (`ct_expired_bot/`)

Separate product from the foreclosure scraper: takes SmartMLS
expired/withdrawn/canceled alert emails, pulls listing detail, resolves
the owner from the town assessor's card, scores the lead, and writes a
skip-trace-ready CSV plus an append-only master workbook.

**It cannot run in this cloud environment**, and that is by design, not a
bug to fix. Step 2 needs a live authenticated SmartMLS session, which
lives in a browser profile on the operator's own machine; the bot
deliberately stores no credentials. Run it locally:

```
python -m ct_expired_bot --login              # once, sign into SmartMLS
python -m ct_expired_bot --graph-login \      # once, sign into M365 mail
  --graph-client-id <app id> --graph-tenant <tenant id>
python -m ct_expired_bot --seed-portals --master "CT-Expired-Master.xlsx"
python -m ct_expired_bot \
  --master "~/Documents/Expired Leads/CT-Expired-Master.xlsx" \
  --graph-client-id <app id> --graph-tenant <tenant id> \
  --graph-folder "CT-Expired Listings" \
  --listing-url-template "https://<connectmls host>/...?mls={mls_no}"
```

### BEST INPUT: the connectMLS export (`--mls-export`)

Exporting the saved search from connectMLS (**Export Listings** ->
choose a report -> All Matches) supplies Steps 1 AND 2 in one file: no
email, no OAuth, no MLS session, no scraping. Prefer it.

```
python -m ct_expired_bot --mls-export "connectMLS_Skip_trace_*.XLS" \
  --master "CT-Expired-Master.xlsx" --output-dir .
```

Confirmed against a real 103-row `.xls` (2026-08-23) -- see
`mls_export.py` for the column list. Four traps, all handled, all real:

- **ZIPs lose their leading zero** (06118 arrives as float `6118.0`).
  Every CT ZIP is affected; a 4-digit ZIP breaks the assessor lookup.
- **MLS numbers are floats too** (`24186595.0`), so dedupe never matches.
- **Prices carry a prefix**: `LP: $339,000`.
- **Rentals are mixed in with sales** (one row at `LP: $2,375`/mo).
  Flagged `likely_rental` and routed to review, never priced as a sale.

The export does NOT carry Original List Price, CDOM, list date, price
history or remarks. Adding **Original List Price** to the export
template is the single highest-value change -- without it there is no
price-reduction figure at all.

### Reading the alert emails

The operator's mail is on Exchange Online (`newerainvesting.com` MX ->
`*.mail.protection.outlook.com`), and Microsoft disabled basic-auth
IMAP there -- `alerts.load_from_imap` cannot authenticate against it no
matter what password it is given. Use `graph_mail.py` (Microsoft Graph,
OAuth2 device-code, public client, `Mail.Read` delegated). The Entra ID
app-registration steps are in that module's docstring.

`--alerts-dir` (saved `.eml`/`.html` files) still works and needs no
auth at all; it is the fallback when Graph is unavailable, at the cost
of being a manual step every run. The IMAP path is kept for a future
non-Microsoft mailbox, not because it works today.

### Step 2: use the JSON API, not HTML scraping

connectMLS is a single-page app -- the HTML is an empty shell and every
field arrives as JSON. So `connectmls_api.py`, not `mls.py`, is the
right path for listing detail.

**Verified 2026-08-13**, no authentication required:

```
GET https://smartmls-apiserver.connectmls.com/api/shared-link/{uuid}
```

returns `{"Listings":[{...83 fields...}]}`. Pinned in
`tests/fixtures_shared_link.json` (real payload for MLS 24119274).
Try it: `python -m ct_expired_bot --shared-link "<url>"`.

It carries ListingId, StreetAddress, City, Zip, MlsStatus, ListPrice,
OriginalListPrice, beds/baths, SquareFeet, Acres, LotSqft, YearBuilt,
property type and Remarks -- but returns **null** for ListingAgent and
SalesHistory, and has no DOM, list-date or expiration-date field. Four
Step 2 fields it cannot supply.

The anonymous response is a *stripped* view, not a different shape:
AllFields, SalesHistory, Rooms, ReportFields, Comments, MediaLinks and
OpenHouses are all present as keys and all null. The operator's
logged-in view shows a "Marketing" section with original list price,
most recent list price and each change date -- that is the SalesHistory
this response nulls out. Rendering the public page headlessly and
expanding everything reachable yields only the 124-char summary card and
no extra XHR, so it is behind the session, not behind a tab.

**The authenticated endpoints exist but are not yet mapped.**
`/api/listing/{id}` and `/api/listings/{id}` answer **401, not 404** --
they are real and need a session. The path, the id form (MLS number vs
the internal `Id` GUID) and the auth header are unknown, and are
deliberately not guessed. Capture them from a logged-in profile:

```
python -m ct_expired_bot --discover-api
```

That opens a headed browser on the saved profile, records which JSON
endpoints a listing page calls and their field names, and redacts
Authorization/Cookie values rather than writing them down.

### What is verified vs. what is a guess

Verified live on 2026-08-04, with tests pinned to the observed values:

- **Vision/VGSI assessor cards.** Element IDs in `assessor.py` were read
  off a real card and re-confirmed end-to-end (owner, mailing address,
  assessed value, sale date/price, year built, living area, beds/baths).
- **Which CT towns are on Vision.** All 169 probed; 77 answered. The list
  in `portals.VGSI_TOWNS` is that probe result, not a guess. The other 92
  towns resolve to Unknown and get discovered on first encounter.
- **Owner name ordering.** Assessor cards print `LAST FIRST`, *not*
  `FIRST LAST` -- 'CARTWRIGHT ANGELA', 'MELECIO JAMIE K'. Reading them
  the other way reverses every name sent to the vendor. `names.py` takes
  an explicit `order` for this reason; do not "simplify" it away.
- **The connectMLS shared-link API** (see above) -- real payload pinned
  as a test fixture.
- **"Unknown" is not "zero" for price drops.** The shared-link API has
  no SalesHistory, so a 520k -> 480k listing would otherwise record as
  "0 price drops" and read as a seller who never moved on price.
  `count_price_drops` returns None when no history was supplied, and
  scoring falls back to original-vs-final, which IS known: enough for
  Medium, never enough to claim the 2+ that means High.
- **Vision search needs the street type REMOVED.** Vision matches the
  address literally against whatever the town stores, and towns
  disagree: Bridgeport holds `575 BURNSFORD AV`, Hamden `75 WASHINGTON
  AVE`. Searching "575 Burnsford Avenue" returns zero hits in both;
  "575 Burnsford" returns the right parcel in both. `search_query()`
  strips the suffix, and `normalize_address` folds AV/AVE/AVENUE so the
  verification still compares full addresses. Fixing this took a real
  103-row run from 19 usable rows to 35.
- **Condos need the unit number.** A condo building lists one parcel per
  unit with identical year built and sqft, so those two cannot separate
  them -- only the unit can. The export carries it; `strip_unit`/
  `extract_unit` split it out and `lookup_owner` matches on it. 35 -> 40.
- **The TLS 1.2 workaround** in `browser.py` is required here for the
  same proxy reason as `ct_foreclosure_bot/browser.py` -- without it,
  every gis.vgsi.com load fails ERR_CONNECTION_RESET.

Still unconfirmed, and marked as such in the module docstrings:

- `alerts.py` -- partially confirmed. The parser is now pinned to a real
  alert's observed text (see `TestRealAlertLayout`): anchored on the
  "MLS#:" label, line-oriented, reading `Town, CT ZIP`. The exact HTML
  tags are still unseen -- only a screenshot was available -- so the
  parser deliberately keys on text and line structure rather than markup.
- `mls.py` field labels are now CONFIRMED against a real connectMLS
  "Listing" tab (302 Dover Street, Bridgeport, 2026-08-14). The
  "Marketing History" block prints `List Price`, `Previous List Price`,
  `Original List Price`, `Price Last Updated`, `Entered in MLS`,
  `Start Marketing Date`, `Listing Last Updated`, `Expiration Date`;
  agent/office are `List Agent` and `List Office`. Note that labels are
  matched ANCHORED to a line start or column gap -- "List Price" is a
  substring of both "Original List Price" and "Previous List Price", and
  an unanchored search hands back the wrong figure.
  The agent-side results grid prints the status as `EXPD`, not
  `Expired`; both are in `EXPIRED_STATUSES`.
  Days-on-market was NOT on the Listing tab and is still unfound.
- `mls.py` (the HTML-scraping path) -- the detail-page URL. SmartMLS
  runs on **connectMLS (dynaConnections), not Matrix** (confirmed
  2026-08-13 from an alert link resolving to
  smartmls-portal.connectmls.com). There is deliberately no default URL:
  `--listing-url-template` is required, and without it every row is
  marked MLS_PULL_FAILED rather than silently 404ing against a guess.
  Get the template by opening a listing in connectMLS and copying the
  address bar with the MLS number replaced by `{mls_no}`. Then confirm
  labels with `--mls <number> --dump-html out.html`.
- Only Vision is implemented for Step 3. Towns on QDS / Northeast /
  Tyler / custom sites return `PORTAL_UNAVAILABLE` and land in the review
  file rather than getting a guessed-at scrape.

### Rules that must not be relaxed

- A row whose owner could not be confirmed is `NEEDS_MANUAL_REVIEW` and
  goes to `review-YYYY-MM-DD.csv`, never to the vendor CSV. Never pick
  the closest-matching parcel.
- The seven operator columns on the Leads tab (`Skip Trace Sent`,
  `Phone 1`, `Phone 2`, `Email`, `Call Attempts`, `Outcome`, `My Notes`)
  are never written by the bot. Appends are header-addressed so a
  reordered sheet does not shuffle them.
- `N/A` means the source did not print it. Nothing is estimated.

Tests: `python -m unittest discover -s tests -v` (81 tests, no network).
