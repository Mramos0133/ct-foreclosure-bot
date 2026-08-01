#!/bin/bash
# Non-interactive runner used by the weekly schedule (see
# "FL Bot - Schedule Weekly.command"). No prompts: both counties,
# rolling 90-day horizon. The first run fetches all 90 days; later runs
# only fetch the days that newly came into range (plus a refresh of the
# near-term dates, whose status can change), then rebuild the workbook
# from everything accumulated in fl_auction_store.sqlite3.
# Everything it prints lands in fl_bot_weekly_log.txt.
cd "$(dirname "$0")"
exec > "fl_bot_weekly_log.txt" 2>&1

echo "=== weekly run started: $(date) ==="

if [ ! -d ".fl_bot_venv" ]; then
    echo "first run: setting up..."
    python3 -m venv .fl_bot_venv || exit 1
    ./.fl_bot_venv/bin/pip install --quiet --upgrade pip
    ./.fl_bot_venv/bin/pip install --quiet playwright beautifulsoup4 openpyxl
    ./.fl_bot_venv/bin/python -m playwright install chromium
fi

STAMP=$(date "+%m-%d-%Y")
./.fl_bot_venv/bin/python -m fl_foreclosure_bot \
    --county both \
    --horizon 90 \
    --output "fl_newly_fetched_${STAMP}.csv" \
    --export-xlsx "fl_leads_weekly_${STAMP}.xlsx"

echo "=== weekly run finished: $(date) ==="
