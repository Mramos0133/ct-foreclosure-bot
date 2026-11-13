# CT Foreclosure Bot

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
  recomputes it against today, through `lead_ranking.days_until` --
  `export_final.py` uses the same helper, so the master sheet and a city
  pull can no longer disagree about what day it is.
- A stored `key_date` comes from the Order document at scrape time and is
  often missing for a case that was posted for sale later. Re-read the
  auction site and pass the fresh dates via `--live-auction`
  ({docket_no: "YYYY-MM-DD"} JSON) whenever the list is for real outreach
  -- on the last pull this filled in three sale dates that showed as
  blank, two of which were only days away.

## Refreshing sale dates from the auction site

The checkpoint's `key_date` and `on_auction_site` are only ever as good as
the last docket read, so before any list that drives real outreach:

```
CT_BOT_CHROMIUM_PATH=/opt/pw-browsers/chromium \
  python3 scripts/fetch_live_sales.py --output /tmp/live_sales.json
python3 scripts/apply_live_auction.py --live-auction /tmp/live_sales.json --dry-run
python3 scripts/apply_live_auction.py --live-auction /tmp/live_sales.json
```

`fetch_live_sales.py` reads the listing (~90 throttled requests),
`apply_live_auction.py` persists it. On 2026-10-16 that corrected 173
cases: 87 sale dates the sheet had blank, 20 it had wrong, and 173
`on_auction_site` flags reading False on properties that were posted.
`export_final.py --live-auction` applies the same JSON at export time
without touching the checkpoint, if you only want it in one workbook.

**The listing parser failed silently for an unknown stretch before that.**
`auction_site` looked the grid up by the exact id `ctl00_cphBody_GridView1`;
the site dropped the ASP.NET prefix, the lookup matched nothing, and
`_parse_town_detail` returned `{}` for all 90 towns. Nothing errored --
the statewide listing simply read as zero pending sales, which is
indistinguishable from "no property in CT is for sale". It now matches on
the id suffix, falls back to finding the table by its "Sale Date" /
"Docket Number" header, and `fetch_statewide_auction_listing` raises
`AuctionListingError` when towns are listed but no sales parse. Never
reintroduce a code path where an empty listing is returned rather than
raised: callers treat `{}` as fact and clear `on_auction_site` wholesale.

## Standard operating procedure for every data pull

These apply to ALL pulls -- statewide updates and city pulls alike. They
are implemented in code, so following the normal commands is enough; this
section exists so the reasoning is not lost.

1. **Probate / heirs are identified up front.** Every case is checked for
   a deceased owner (`probate.py`) from both the caption and the docket
   entries -- the docket check matters because an owner who died
   mid-case leaves the caption looking perfectly normal. The sheet gets
   Probate (Y/N), the signal that triggered it, the deceased owner's
   name, and the fiduciary/heirs to contact, and the same appears as the
   FIRST line of Summary of Case, because it changes *who* is contacted
   before anyone knocks or calls.
   Heir names are best-effort: a caption reading "THE WIDOW, HEIRS ... OF
   JOHN SMITH" names nobody, so the cell is deliberately left empty with
   a note rather than filled with a guess.

2. **Equity outranks distress.** (debt + subsequent encumbrances) as a
   share of appraised value:
   - `> 85%` (`SHORT_SALE_RATIO`) -> **POTENTIAL_SHORT_SALE**
   - `> 85%` (`COLD_RATIO`) -> **COLD** -- band is EMPTY, see below
   Overrides every HOT/WARM distress signal -- no equity means no deal
   however motivated the owner. Applied only when debt AND appraised
   value are both known; a case is never penalized for a figure the OCR
   failed to read, and an "Updated debt" reading below
   `MIN_PLAUSIBLE_DEBT` ($1,000) is treated as unread rather than as fact.

   COLD_RATIO was raised 0.75 -> 0.85 on 2026-09-27 per explicit
   instruction. Since SHORT_SALE_RATIO is also 0.85 and is tested first,
   equity no longer forces COLD at all; COLD is now reachable only via the
   continuance rule. Raise SHORT_SALE_RATIO above 0.85 to reopen a
   distinct COLD band between the two.

3. **A recently-closed assistance window is HOT on its own.** When
   mediation expired/terminated or EMAP was denied/expired within
   `ASSISTANCE_ELAPSED_HOT_MAX_MONTHS` (6), the case is HOT regardless of
   whether a complaint is on record -- the owner's rescue options are
   spent and the foreclosure resumes. Widened 3 -> 6 on 2026-08-29 after
   profiling UNCLASSIFIED: of 482 cases there with a closed window, none
   had elapsed within 3 months (already swept into HOT) but 52 sat in the
   3-6 month band. The cap still excludes the stale tail -- 154 elapsed
   6-12 months ago and 276 over a year ago, and a lapse from last year
   with nothing since is a closed file, not a live lead.
   `scripts/reclassify_offline.py` applies this to the whole checkpoint
   with no network, since both inputs are persisted.

4. **Complaint-stage leads split on the loan-assistance clock.** A recent
   lender complaint is no longer HOT on its own:
   - complaint + assistance **elapsed or absent** -> **HOT** (options
     spent, owner reachable and out of alternatives)
   - complaint + assistance **still open** -> **WARM** (owner is working
     a rescue and won't discuss selling; converts to HOT by itself once
     the window closes)
   The clock counts mediation AND EMAP -- whichever appears on the docket
   -- and only reads "elapsed" once every avenue found has closed. An
   avenue opened after the last closure puts the case back to "open".

## Running long jobs in this environment (IMPORTANT)

**Background processes do not survive here. Never use them for a long run.**

This container suspends roughly five minutes after the agent's turn ends,
and it also periodically reverts the working tree to an older commit.
Either way every running process dies. `setsid nohup ... &` does not help:
measured twice on 2026-08-23, a supervisor launched at 21:21 stopped at
21:27 and one launched at 21:52 stopped at 21:59, after which the run sat
dead for 1h53m. Background supervision cannot fix this, because the
supervisor is just one more process that dies with everything else.

What keeps the container alive is an **in-flight foreground tool call**.
So the work has to happen inside a turn:

```
python3 scripts/selftest_ops.py             # must exit 0 first
python3 scripts/run_leg.py --budget 540     # blocking; repeat until exit 0
```

`scripts/run_leg.py` is the unit of work. Each call reconciles the local
checkpoint against the remote, scrapes for its budget, then commits and
pushes, so an interruption at any moment costs at most the current case.
Keep `--budget` UNDER the caller's own timeout (540s against a 600s Bash
timeout) so the leg exits cleanly and commits instead of being killed
mid-write. Exit code 0 means the whole update is finished, 2 means call
it again. About five calls fit in one turn.

A rebuilt container comes back WITHOUT the pip packages or the tesseract
binary, and that failure is silent from outside -- legs just make no
progress. The self-test catches it; when it flags missing deps:

```
python3 -m pip install -r requirements.txt   # NOT bare `pip`, see below
apt-get update -qq && apt-get install -y tesseract-ocr
```

Three traps in that, all hit on 2026-10-26:

- **Use `python3 -m pip`, never bare `pip`.** In the rebuilt container
  `pip` is Python 3.13's (`/usr/bin/pip`) while `python3` is 3.11
  (`/usr/local/bin/python3`). A bare `pip install` reports success and
  puts everything in 3.13's dist-packages, where the interpreter that
  runs this project cannot see it -- so the self-test still fails with
  `ModuleNotFoundError` right after an apparently clean install.
- `apt-get install tesseract-ocr` fails with "Unable to locate package"
  until `apt-get update` has run.
- Pillow needs `python3 -m pip install --force-reinstall Pillow` on top.
  A stale 3.13 copy at `/usr/lib/python3/dist-packages/PIL` shadows it,
  and that copy's compiled `_imaging` will not load under 3.11:
  `import PIL` succeeds while `from PIL import Image` raises. The ops
  self-test's dep check only does the former, so it reports PASS for a
  Pillow that cannot actually decode an image -- `selftest_ranking_rules.py`
  is what surfaces it.

Supporting pieces:

- `recheck_progress` (checkpoint.py) -- per-case marker making phase 2
  resumable. Without it, phase 2 restarts from zero on every interruption
  and can never finish in an environment that resets hourly.
- `scripts/resume_update.py` -- the two update phases WITHOUT clearing
  `completed_towns`. `run_leg.py` drives this; plain `--update` clears
  town progress and is only correct for a fresh run.
- `scripts/checkpoint_autosave.py` -- `commit_snapshot()` writes a
  consistent sqlite snapshot via the backup API and stages it with git
  plumbing. Use `--once` for a single commit; NEVER pass a sentinel
  `--watch-process` value, because `pgrep -f` matches the autosaver's own
  command line and it will wait on itself forever.
- `scripts/export_final.py` -- the finishing step. Diffs the checkpoint
  against a baseline commit and exports the workbook with green (new) /
  yellow (updated) rows via `export_to_xlsx()`.

To run unattended, create an hourly Routine that does the loop above IN
THE FOREGROUND (five `run_leg.py` calls per firing, ~45 min of real work
per hour). A Routine that merely relaunches a background supervisor is
useless here -- that was tried and yielded ~6 minutes of work per hour.
**Delete the Routine once the run finishes**; it does not stop on its own,
and one was left firing for five hours after a completed run.

Never use `pgrep -f <pattern>` to check whether these scripts are alive:
it matches the checking command's own command line and reports RUNNING for
a process that died hours ago. Use a self-excluding check:

```
ps -eo pid,etime,args | awk '/python3? .*(run_leg|resume_update)\.py/ && !/awk/'
```

`scripts/supervise_update.py` is kept only for reference. Do not start it.

## Judgment / law day / title columns (PERMANENT, added 2026-11-13)

Six columns requested on 2026-11-13 and carried on **every** pull from
then on -- statewide updates and city pulls alike, since both route
through `export_to_xlsx()`. Do not drop them from `COLUMNS`:

| Column | Source |
|---|---|
| Judgment Type | Order text, else key date label, else the motion moved on |
| Judgment Date | file date of the Order docket entry |
| Current Law Day | `key_date` on strict foreclosures only |
| Title Vested Status | derived from type + key date + today |
| Last Material Court Event | most recent material docket entry |
| Last Material Event Date | its file date |

All of it comes from material the pipeline already fetches, so the
columns cost **no extra requests per case**. Logic and the full caveats
live in `ct_foreclosure_bot/judgment.py`; `scripts/selftest_judgment.py`
guards them and runs inside `selftest_ops.py`.

**Title Vested Status is an inference from docket dates, not a title
search.** It reads "the Law Day on record has passed" and says so. It
cannot see a redemption that happened or a Law Day reset the docket has
not yet shown, so a case with activity recorded AFTER its Law Day is
reported as *likely reset*, not vested, and a passed Sale Date is
reported as *title passes on confirmation, verify* rather than as a lost
owner. Confirm on the land records before writing an owner off.

Judgment Type reads the motion as a third source for a reason: statewide,
1898 cases carry a strict-foreclosure motion while only 11 ever yielded a
"Law Day" date label, because that line is frequently not extracted from
the Order. Deriving from the date label alone reported Connecticut as
overwhelmingly foreclosure-by-sale, which is backwards.

Both paths persist these -- `pipeline.py` for a fresh scrape and
`update_run.py` for a recheck. Keep it that way; a field written on one
path only is the exact shape of the `assistance_elapsed_hot` bug.
`title_vested_status` is additionally recomputed in `export_final.py`,
because like `days_to_key_date` it reads today's clock and would
otherwise claim a redemption period was running on a deadline that
lapsed weeks ago.

`update_run.MEANINGFUL_FIELDS` carries the four factual ones so a new
judgment or a moved Law Day turns a row yellow, but **not**
`title_vested_status`, which changes on its own as dates pass.
`BACKFILLED_FIELDS` then suppresses the empty-to-populated transition:
without it, adding these columns turned 2279 of 2945 rows yellow and
buried the 906 that reflected real docket activity.

`scripts/backfill_judgment.py` fills Judgment Type / Current Law Day /
Title Vested on existing cases with no network. Judgment Date and Last
Material Event CANNOT be derived offline -- raw docket entries are not
persisted -- so they stay blank until the next full update reads the
dockets. Blank is the honest state there.

### Key dates come out of the Order PDF, and the phrasing varies (2026-11-13)

`KEY_DATE_PATTERNS` in `order_document.py` carries an explicit list of
the words courts put between the label and the date. Every entry was
read off a real filing via `scripts/sample_order_text.py`, which dumps
an Order's OCR text for a list of docket numbers. Sample before widening
it again -- the first version of these patterns was a reasonable guess
and it missed most of the book:

    LAW DAY SET FOR Monday, November 16, 2026, for the owner...
    The clerk will set the new Sale Date of March 6, 2027.
    A new sale date of 8/29/2026 is ordered.

It allowed a bare colon, an "is", and one comma-terminated word, so
every "SET FOR" / "to" / "of" phrasing fell through **silently** and
Current Law Day sat at 11 of 2945 cases. On the 20-Order sample the
widened list takes extraction from 5 to 13.

**Keep the gap tight -- whitespace and commas only.** One real Order
states the sale date and then the committee's fee deadline twenty words
later ("...Sale Date of February 27, 2027. ... incur no additional fees
and costs prior to 01/13/27"); a loose gap reads the fee deadline as the
auction. `selftest_judgment.py` pins that case and nine more by docket
number.

`scripts/backfill_law_day.py` applies the fix to cases already on
record. **It has to exist as its own pass**: `recheck_matched_case`
short-circuits on docket entry count and never re-reads the Order of a
case whose docket has not grown, so a quiet case keeps its blank Law Day
through any number of `--update` runs. Strictly additive -- only cases
with no `key_date` are touched, so a stored date is never overwritten.
Resumable per case (`law_day_backfilled`) and `--budget`-bounded like
`run_leg.py`; about 4.5s and two requests per case.

On the first run it read all 270 cases that had a judgment but no date:
68 dates recovered, **172 Orders genuinely state no date** (they rule on
a motion to open, refer the owner to mediation, or grant a caseflow
request) and 30 have no Order entry at all. Together with the 915
strict-foreclosure cases where no judgment has been granted yet, that is
why Current Law Day reads 42 and not four figures. 42 is the real number.

Two known gaps left deliberately, both needing more requests per case:

- An Order sometimes delegates: "ORDER: GRANTED / See Order #133.50".
  The date is in that other document and is not followed.
- When the latest matched motion is still pending (no Order entry), the
  *prior* judgment's date is still operative but is not read. Note the
  converse is NOT safe: when the latest Order exists and simply states
  no date, falling back to an older date would resurrect a deadline a
  granted motion to open has just vacated. Blank is correct there.

`is_order_granted()` reads the court's explicit `ORDER: <disposition>`
line before scanning the prose. DBD-CV-25-6056466-S says `ORDER: OFF`
followed by "Petition to participate in mediation granted by the
Court." -- the motion came off the calendar and the owner went to
mediation, the opposite of a judgment, but the stray "granted" recorded
it as `judgment_granted`. That feeds HOT rule E, so it manufactures a
lead rather than just mis-filling a column; the backfill corrected 16
such cases and `apply_rule_fixes` then retired 22 HOT leads.

## Excel output shape

Every export carries TEN sheets, and that is permanent:

- the five buckets -- HOT, WARM, COLD, POTENTIAL_SHORT_SALE, UNCLASSIFIED
- five HOT breakdown sheets, one per rule, titled from `HOT_RULES` in
  `lead_ranking.py`: `A. Bankruptcy Reopened`, `B. EMAP-Loan Mod Failed`,
  `C. Recent Lender Complaint`, `D. Assistance Window Closed`,
  `E. Judgment + Non-Appearing`

`hot_rule_for()` follows `decide_bucket`'s precedence, so each HOT case
lands on exactly ONE breakdown sheet and the five sum to the HOT sheet --
verify that when changing either. A case whose bucket is HOT but matches
no rule (a window that aged out between updates) is omitted from the
breakdown sheets but still appears on HOT, so no lead is ever lost; if
that count is non-zero the buckets are stale and want a re-derivation.

That check earned its keep on 2026-10-16: repairing the auction
cross-reference dropped 64 cases off the breakdown while leaving them on
HOT, and the two causes needed opposite treatment -- 51 were posted for
sale with a future date (rule E widened to keep them, below) and 13 had
already been sold (retired via `apply_rule_fixes.py`). Run
`apply_rule_fixes.py` when the total does not reconcile; it retires a HOT
case whose sale has actually been held, which is decidable from persisted
fields alone because rule E is the only HOT rule with no flag of its own.
It also retires a HOT case matching NO rule at all (`hot_without_a_rule`,
added 2026-11-13): the post-sale rule only reaches cases with a sale
date, so a case whose flag was corrected to False and which never had a
key date sat in HOT permanently and kept the breakdown from summing.

**Run `apply_rule_fixes.py` until it reports zero changes, not once.**
Correcting a stale flag can expose a case to the retire rule, so a first
pass of 10 flag corrections + 7 retirements was followed by a second
finding 2 more. It converges in two or three passes; a single pass can
leave the breakdown short.

Rule E no longer excludes cases already on the auction site (changed
2026-10-16 per explicit instruction). A posted sale shortens the window
rather than closing it, and the old condition discarded the most urgent
cases in the book. `key_date_in_past` is what ends the rule now. Both
`decide_bucket` and `hot_rule_for` must agree on this or the breakdown
total silently breaks -- `selftest_ranking_rules.py` asserts it.

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
