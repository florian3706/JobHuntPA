#!/bin/bash
# macOS: double-click to uninstall JobHuntPA (offers a backup first).
cd "$(dirname "$0")"
./uninstall.sh
echo
read -r -p "Press Enter to close this window."
