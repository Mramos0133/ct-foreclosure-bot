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
  recomputes it against today.
- A stored `key_date` comes from the Order document at scrape time and is
  often missing for a case that was posted for sale later. Re-read the
  auction site and pass the fresh dates via `--live-auction`
  ({docket_no: "YYYY-MM-DD"} JSON) whenever the list is for real outreach
  -- on the last pull this filled in three sale dates that showed as
  blank, two of which were only days away.

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
   - `> 75%` (`COLD_RATIO`) -> **COLD**
   - `> 85%` (`SHORT_SALE_RATIO`) -> **POTENTIAL_SHORT_SALE**
   Both override every HOT/WARM distress signal -- no equity means no
   deal however motivated the owner. Applied only when debt AND appraised
   value are both known; a case is never penalized for a figure the OCR
   failed to read.

3. **A recently-closed assistance window is HOT on its own.** When
   mediation expired/terminated or EMAP was denied/expired within
   `ASSISTANCE_ELAPSED_HOT_MAX_MONTHS` (3), the case is HOT regardless of
   whether a complaint is on record -- the owner's rescue options are
   spent and the foreclosure resumes. Capped at 3 months because the
   older tail is large and stale (268 of 589 statewide had elapsed 1+
   years ago) and a lapse from last year is not a live lead.
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
