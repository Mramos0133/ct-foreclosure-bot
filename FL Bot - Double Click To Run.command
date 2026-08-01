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

echo "Press Enter for the normal update: keeps you covered 90 days out,"
echo "and only checks the days it hasn't already got (fast after the"
echo "first run)."
echo
echo "Or type a specific start date (like 08/03/2026) to re-check a"
echo "particular range instead."
read -p "Start date (Enter = normal update): " AUCTION_DATE
# Keep only digits and slashes, so stray spaces/words don't break the run.
AUCTION_DATE=$(echo "$AUCTION_DATE" | tr -cd '0-9/')

STAMP=$(date "+%m-%d-%Y")
XLSX="fl_leads_${STAMP}.xlsx"
CSV="fl_newly_fetched_${STAMP}.csv"

if [ -z "$AUCTION_DATE" ]; then
    echo
    echo "How many days ahead do you want covered?"
    read -p "Days ahead (Enter = 90): " HORIZON
    HORIZON=$(echo "$HORIZON" | tr -cd '0-9')
    [ -z "$HORIZON" ] && HORIZON=90
    echo
    echo "Updating Miami-Dade and Broward, ${HORIZON} days out..."
    echo "(the first run takes a while; later runs only fetch new days)"
    echo
    # Deliberately not aborting on failure (set -e): the post-run message
    # below must still appear so the user knows to send the log.
    ./.fl_bot_venv/bin/python -m fl_foreclosure_bot \
        --county both \
        --horizon "$HORIZON" \
        --output "$CSV" \
        --export-xlsx "$XLSX" || true
else
    echo
    read -p "How many days from that date? (Enter = 1): " DAYS
    DAYS=$(echo "$DAYS" | tr -cd '0-9')
    [ -z "$DAYS" ] && DAYS=1
    echo
    echo "Re-checking Miami-Dade and Broward: ${DAYS} day(s) from ${AUCTION_DATE}..."
    echo
    ./.fl_bot_venv/bin/python -m fl_foreclosure_bot \
        --county both \
        --auction-date "$AUCTION_DATE" \
        --days "$DAYS" \
        --output "$CSV" \
        --export-xlsx "$XLSX" || true
fi

echo
# Judge success by what's in the accumulated store, NOT by how many rows
# this run fetched: a normal weekly update legitimately finds few or no
# new listings, and that is not a failure.
TOTAL_ROWS=$(./.fl_bot_venv/bin/python -c "
import sqlite3, sys
try:
    print(sqlite3.connect('fl_auction_store.sqlite3').execute('SELECT COUNT(*) FROM listings').fetchone()[0])
except Exception:
    print(0)
" 2>/dev/null || echo 0)
NEW_ROWS=0
if [ -f "$CSV" ]; then
    NEW_ROWS=$(( $(wc -l < "$CSV") - 1 ))
    [ "$NEW_ROWS" -lt 0 ] && NEW_ROWS=0
fi
if [ -f "$XLSX" ] && [ "$TOTAL_ROWS" -gt 0 ]; then
    echo "Done! ${TOTAL_ROWS} listings on file (${NEW_ROWS} fetched this run)."
    echo "Opening ${XLSX} now -- start with the ACTIVE tab."
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
