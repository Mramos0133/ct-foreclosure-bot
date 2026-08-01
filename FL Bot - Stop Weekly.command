#!/bin/bash
# Double-click to turn OFF the automatic weekly run.
# (Re-enable anytime by double-clicking "FL Bot - Schedule Weekly.command".)

PLIST="$HOME/Library/LaunchAgents/com.flforeclosurebot.weekly.plist"

if [ -f "$PLIST" ]; then
    launchctl unload "$PLIST" 2>/dev/null
    rm -f "$PLIST"
    echo "Weekly schedule removed. The bot will no longer run automatically."
else
    echo "No weekly schedule was installed -- nothing to remove."
fi
echo
read -p "Press Enter to close this window..."
