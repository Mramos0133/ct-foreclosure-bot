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
