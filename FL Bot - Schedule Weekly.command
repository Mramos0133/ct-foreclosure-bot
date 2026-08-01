#!/bin/bash
# Double-click ONCE to schedule the bot to run automatically every
# Monday at 7:00 AM. A fresh spreadsheet (fl_leads_weekly_<date>.xlsx)
# appears in this folder each week -- no clicking needed after this.
# To change the day/time: edit WEEKDAY/HOUR below and double-click again.
# To stop the schedule: double-click "FL Bot - Stop Weekly.command".

WEEKDAY=1   # 0=Sunday, 1=Monday, ... 6=Saturday
HOUR=7      # 24-hour clock: 7 = 7:00 AM
MINUTE=0

DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.flforeclosurebot.weekly.plist"

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.flforeclosurebot.weekly</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${DIR}/fl_bot_weekly_run.sh</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>${WEEKDAY}</integer>
        <key>Hour</key>
        <integer>${HOUR}</integer>
        <key>Minute</key>
        <integer>${MINUTE}</integer>
    </dict>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null
launchctl load "$PLIST"

echo "======================================================"
echo "  Weekly schedule installed!"
echo "======================================================"
echo
echo "Every Monday at 7:00 AM this folder gets a fresh file:"
echo "    fl_leads_weekly_<date>.xlsx"
echo
echo "Notes:"
echo " - Your Mac must be on. If it's asleep at 7 AM, the run"
echo "   happens when it next wakes up. If it's shut off, that"
echo "   week is skipped (run manually with the other file)."
echo " - The double-click runner still works anytime you want"
echo "   fresh data on demand."
echo " - Each run writes its progress to fl_bot_weekly_log.txt."
echo " - IMPORTANT: don't move/rename this folder -- the schedule"
echo "   points at it. If you move it, double-click this file"
echo "   again afterward to re-point the schedule."
echo
read -p "Press Enter to close this window..."
