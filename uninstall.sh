#!/usr/bin/env bash
# Remove JobHuntPA from this computer (Linux and macOS).
# Offers to save a backup of your data first. Options: --yes, --no-backup, --remove-browser
cd "$(dirname "$0")"
DIR="$(pwd)"
if command -v python3 >/dev/null 2>&1; then PY=python3
elif [ -x .venv/bin/python ]; then PY=.venv/bin/python
else echo "Python not found; delete this folder to uninstall: $DIR"; exit 1; fi
# Run from a temporary copy so deleting this folder can't cut the uninstaller off.
TMP="$(mktemp -d)"
cp installer/uninstaller.py "$TMP/"
cd "$TMP"
"$PY" "$TMP/uninstaller.py" --folder "$DIR" "$@"
status=$?
rm -rf "$TMP"
exit $status
