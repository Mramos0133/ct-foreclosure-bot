# FL Foreclosure Auction Bot (Miami-Dade / Broward)

Scrapes a `realforeclose.com` auction-date preview page for Miami-Dade
and/or Broward County and produces a CSV + ranked Excel workbook of that
date's foreclosure auction listings (case #, final judgment amount,
assessed value, property address, plaintiff max bid, sale status).

## Important: run this from your own network, not a cloud VM

During development, every request to `realforeclose.com` from the dev
sandbox this bot was written in -- `curl`, a headless browser, and a
URL-fetch tool alike -- was rejected with a 403 at the network edge (an
AWS WAF, based on response headers), before any page content was ever
returned. This looks like an IP-based block on datacenter/proxy traffic,
not a solvable browser-fingerprint issue. A normal home or office network
should not hit this. If you do see a 403 running this yourself, that's
useful information -- let me know and we can dig further, but it likely
means an even broader IP range is being blocked than expected.

## Setup

```
pip install -r requirements.txt   # playwright, beautifulsoup4, openpyxl -- already in the repo's requirements.txt
python -m playwright install chromium
```

## Usage

```
python -m fl_foreclosure_bot --county miami-dade --auction-date 07/08/2026
python -m fl_foreclosure_bot --county broward --auction-date 07/08/2026
python -m fl_foreclosure_bot --county both --auction-date 07/08/2026 --export-xlsx leads.xlsx
```

`--auction-date` must be `MM/DD/YYYY`, matching the site's own URL
format. Run it weekly with whatever date you want previewed; each run
appends to `--output` (CSV) and rewrites `--export-xlsx` from everything
scraped so far in that CSV run.

To automate the weekly run, add a cron entry (Mac/Linux) or Task
Scheduler task (Windows) that runs the command above -- see the repo's
top-level notes or ask for help setting one up.

## Known limitations / how this was built

Unlike the CT bot, this one was **not** built against confirmed raw HTML
-- the WAF block above meant the only way to see real page content during
development was screenshots the user took manually. The parser
(`auction_page.py`) therefore matches against the page's own visible
label text ("Case #:", "Final Judgment Amount:", etc., confirmed present
on real screenshots from both counties) rather than CSS classes/IDs,
which should make it fairly resilient to markup details being slightly
off, but two things are explicitly **unverified**:

- **Pagination.** A "page N of M" control with "PRE"/"NEXT" was seen in
  screenshots, but the actual clickable element was never confirmed.
  `goto_next_page()` tries a few reasonable selectors; if none work on
  the real site, only page 1 of a multi-page date gets scraped and a
  warning is logged -- not a silent hang or crash.
- **Field variations across counties/case types.** Only a handful of real
  cards were seen (a few Miami-Dade, a few Broward, both "Sold" and
  various "Canceled per ..." statuses). Some fields (e.g. "Assessed
  Value") were confirmed present on some cards and absent on others --
  handled as optional -- but there may be other card layouts (e.g. tax
  deed sales, if either county lists them here) not yet seen.

If something errors or looks wrong on a real run, the most useful thing
to send back is the exact error/log output plus a screenshot of the page
that tripped it up -- that's exactly how this was built in the first
place.
