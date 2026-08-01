#!/bin/bash
# Double-clickable runner for the FL foreclosure auction bot (Mac).
# First run: sets everything up (Python packages + browser), then scrapes.
# Later runs: skips setup and goes straight to scraping.
set -e
cd "$(dirname "$0")"

# Everything shown on screen is also saved to this file, so a failed run
# can be diagnosed by sending the file -- no terminal scrollback needed.
exec > >(tee "fl_bot_run_log.txt") 2>&1

echo "======================================================"
echo "  FL Foreclosure Auction Bot (Miami-Dade + Broward)"
echo "======================================================"
echo

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 is not installed yet. A popup may appear asking to"
    echo "install 'command line developer tools' -- click Install, wait"
    echo "for it to finish, then double-click this file again."
    xcode-select --install >/dev/null 2>&1 || true
    read -p "Press Enter to close this window..."
    exit 1
fi

if [ ! -d ".fl_bot_venv" ]; then
    echo "First-time setup: installing what the bot needs."
    echo "This takes a few minutes and only happens once..."
    echo
    python3 -m venv .fl_bot_venv
    ./.fl_bot_venv/bin/pip install --quiet --upgrade pip
    ./.fl_bot_venv/bin/pip install --quiet playwright beautifulsoup4 openpyxl
    ./.fl_bot_venv/bin/python -m playwright install chromium
    echo
    echo "Setup done."
    echo
fi

echo "Start date? Type it like 08/03/2026, or just press Enter for today."
read -p "Start date (MM/DD/YYYY, Enter = today): " AUCTION_DATE
# Keep only digits and slashes, so stray spaces/words don't break the run.
AUCTION_DATE=$(echo "$AUCTION_DATE" | tr -cd '0-9/')
if [ -z "$AUCTION_DATE" ]; then
    AUCTION_DATE=$(date "+%m/%d/%Y")
fi

echo
echo "How many days ahead should it check (including the start date)?"
echo "Upcoming/future auctions are included -- e.g. 30 covers the next month."
read -p "Days to check (Enter = 30): " DAYS
# Keep only digits: "90 days" becomes 90, stray spaces vanish.
DAYS=$(echo "$DAYS" | tr -cd '0-9')
if [ -z "$DAYS" ]; then
    DAYS=30
fi

STAMP=$(echo "$AUCTION_DATE" | tr '/' '-')
XLSX="fl_leads_${STAMP}_${DAYS}days.xlsx"
CSV="fl_results_${STAMP}_${DAYS}days.csv"

echo
echo "Scraping Miami-Dade and Broward: ${DAYS} day(s) starting ${AUCTION_DATE}..."
echo "(roughly 5-15 minutes for a 30-day sweep, longer for more days;"
echo " leave this window open)"
echo

# Deliberately not aborting on failure (set -e) here: the post-run
# message below must still appear so the user knows to send the log.
./.fl_bot_venv/bin/python -m fl_foreclosure_bot \
    --county both \
    --auction-date "$AUCTION_DATE" \
    --days "$DAYS" \
    --output "$CSV" \
    --export-xlsx "$XLSX" || true

echo
DATA_ROWS=0
if [ -f "$CSV" ]; then
    DATA_ROWS=$(( $(wc -l < "$CSV") - 1 ))
    [ "$DATA_ROWS" -lt 0 ] && DATA_ROWS=0
fi
if [ -f "$XLSX" ] && [ "$DATA_ROWS" -gt 0 ]; then
    echo "Done! ${DATA_ROWS} listings found. Opening ${XLSX} now."
    echo "(Both files are saved in this same folder for later:"
    echo "  ${XLSX} and ${CSV})"
    open "$XLSX" || true
else
    echo "PROBLEM: the run finished but found 0 listings."
    echo
    echo "To get this fixed, send Claude these files from this folder:"
    echo "   1. fl_bot_run_log.txt"
    echo "   2. fl_debug_miami-dade.html   (if present)"
    echo "   3. fl_debug_broward.html      (if present)"
    echo
    echo "The two .html files are snapshots of what the website actually"
    echo "sent the bot -- exactly what's needed to fix this for good."
fi
echo
read -p "Press Enter to close this window..."
