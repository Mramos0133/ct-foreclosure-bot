#!/bin/bash
# Double-clickable runner for the FL foreclosure auction bot (Mac).
# First run: sets everything up (Python packages + browser), then scrapes.
# Later runs: skips setup and goes straight to scraping.
set -e
cd "$(dirname "$0")"

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

echo "Which auction date do you want? Type it like: 07/08/2026"
read -p "Auction date (MM/DD/YYYY): " AUCTION_DATE
if [ -z "$AUCTION_DATE" ]; then
    echo "No date entered -- nothing to do."
    read -p "Press Enter to close this window..."
    exit 1
fi

STAMP=$(echo "$AUCTION_DATE" | tr '/' '-')
XLSX="fl_leads_${STAMP}.xlsx"
CSV="fl_results_${STAMP}.csv"

echo
echo "Scraping Miami-Dade and Broward for ${AUCTION_DATE}..."
echo "(a couple of minutes; leave this window open)"
echo

./.fl_bot_venv/bin/python -m fl_foreclosure_bot \
    --county both \
    --auction-date "$AUCTION_DATE" \
    --output "$CSV" \
    --export-xlsx "$XLSX"

echo
if [ -f "$XLSX" ]; then
    echo "Done! Opening ${XLSX} now."
    echo "(Both files are saved in this same folder for later:"
    echo "  ${XLSX} and ${CSV})"
    open "$XLSX"
else
    echo "The run finished but no Excel file was produced -- scroll up"
    echo "for any error text and send it back to Claude to fix."
fi
echo
read -p "Press Enter to close this window..."
