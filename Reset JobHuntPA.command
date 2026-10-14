#!/bin/bash
# macOS: double-click to start JobHuntPA over (deletes the account and its data, keeps the settings). Quit JobHuntPA
# first; it asks you to type RESET before anything is deleted.
cd "$(dirname "$0")"
./reset.sh
echo
read -r -p "Press Enter to close this window."
