#!/usr/bin/env bash
# Start JobHuntPA over (Linux and macOS): deletes the account and its data, keeps the settings (.env), and returns the
# app to its first run. For when the password and every recovery code are lost. Stop JobHuntPA first.
# It says what will be lost and asks you to type RESET. Option: --confirm RESET (no question).
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "JobHuntPA isn't set up yet. Run ./setup.sh first."
  exit 1
fi
exec .venv/bin/python -m backend.reset "$@"
