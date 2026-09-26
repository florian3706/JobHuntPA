#!/usr/bin/env bash
# Start JobHuntPA and open it in your browser (Linux and macOS).
# Options: --port N, --no-browser
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "JobHuntPA isn't set up yet. Run ./setup.sh first."
  exit 1
fi
exec .venv/bin/python installer/launch.py "$@"
