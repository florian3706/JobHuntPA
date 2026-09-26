#!/usr/bin/env bash
# JobHuntPA one-time setup for Linux and macOS.
# Usage: ./setup.sh            (or double-click "Setup.command" on a Mac)
# Options are passed to installer/installer.py: --reconfigure, --skip-browser, --non-interactive
set -euo pipefail
cd "$(dirname "$0")"

MIN_MINOR=10

python_ok() {
  "$1" -c "import sys; sys.exit(0 if sys.version_info >= (3, $MIN_MINOR) else 1)" 2>/dev/null
}

find_python() {
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && python_ok "$candidate"; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

confirm() {
  read -r -p "$1 [y/N] " reply
  [[ "$reply" =~ ^[Yy] ]]
}

install_python() {
  echo "JobHuntPA needs Python 3.$MIN_MINOR or newer, which wasn't found."
  case "$(uname -s)" in
    Darwin)
      if command -v brew >/dev/null 2>&1; then
        if confirm "Install Python 3.12 with Homebrew now?"; then brew install python@3.12; return; fi
      fi
      echo "Install Python from https://www.python.org/downloads/macos/ (the macOS installer),"
      echo "then run this setup again."
      open "https://www.python.org/downloads/macos/" 2>/dev/null || true
      exit 1 ;;
    Linux)
      if command -v apt-get >/dev/null 2>&1; then cmd="sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip"
      elif command -v dnf >/dev/null 2>&1; then cmd="sudo dnf install -y python3 python3-pip"
      elif command -v pacman >/dev/null 2>&1; then cmd="sudo pacman -S --needed python python-pip"
      elif command -v zypper >/dev/null 2>&1; then cmd="sudo zypper install -y python3 python3-pip"
      else echo "Install Python 3.$MIN_MINOR+ with your package manager, then run this setup again."; exit 1; fi
      echo "It can be installed with:  $cmd"
      if confirm "Run that now (asks for your password)?"; then bash -c "$cmd"; return; fi
      exit 1 ;;
    *) echo "Unsupported system: $(uname -s). On Windows use JobHuntPA-Setup.exe."; exit 1 ;;
  esac
}

PY="$(find_python || true)"
if [ -z "$PY" ]; then
  install_python
  PY="$(find_python || true)"
  [ -n "$PY" ] || { echo "Python still not found; please install it and run setup again."; exit 1; }
fi

# Debian/Ubuntu ship Python without the venv module.
if ! "$PY" -c "import venv, ensurepip" 2>/dev/null; then
  ver="$("$PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  if command -v apt-get >/dev/null 2>&1 && confirm "Python's venv module is missing. Install python$ver-venv now (asks for your password)?"; then
    sudo apt-get install -y "python$ver-venv"
  else
    echo "Install the Python venv module (e.g. sudo apt install python$ver-venv), then run setup again."
    exit 1
  fi
fi

chmod +x start.sh setup.sh uninstall.sh "Setup.command" "Start JobHuntPA.command" "Uninstall JobHuntPA.command" 2>/dev/null || true
exec "$PY" installer/installer.py "$@"
