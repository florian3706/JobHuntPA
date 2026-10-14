#!/usr/bin/env bash
# Start JobHuntPA at boot on Linux, as a systemd user service (for hosting it on your own network: README,
# "Hosting on your own network").
#
#   ./service.sh install [--port N] [--data-dir PATH]   install and start it (again, to change the options)
#   ./service.sh remove                                 stop it and remove it (your data stays)
#   ./service.sh status                                 is it installed, running and answering?
#
# --port      the port on this computer (default 8000). JobHuntPA listens on 127.0.0.1 only: other devices reach it
#             through Tailscale serve or a reverse proxy on this computer.
# --data-dir  the data folder (default: the one JobHuntPA already uses, else ~/.local/share/JobHuntPA). The service
#             passes it as JOBHUNT_DATA_DIR, so its first run doesn't ask where the data lives.
#
# The AppImage runs this with: ./JobHuntPA-x86_64.AppImage --service install [options]
set -euo pipefail

NAME="jobhuntpa.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/$NAME"
cd "$(dirname "$0")"
APP_DIR="$(pwd)"

say() { printf '%s\n' "$*"; }
die() { say "$*" >&2; exit 1; }

if [ "$(uname -s)" != "Linux" ]; then
  die "service.sh is for Linux (systemd). On other systems, start JobHuntPA with its start script."
fi
command -v systemctl >/dev/null 2>&1 || die "systemctl wasn't found: this needs a Linux system that uses systemd."

IDENT="$APP_DIR"  # what the unit names, so the uninstaller finds the service of this install
if [ -n "${APPIMAGE:-}" ]; then
  PYTHON="${APPDIR:?}/python/bin/python3"
  START=("$APPIMAGE" --server)
  IDENT="$APPIMAGE"  # (the AppImage's own folder is a new temporary mount each time it runs)
else
  PYTHON="$APP_DIR/.venv/bin/python"
  [ -x "$PYTHON" ] || die "JobHuntPA isn't set up yet. Run ./setup.sh first."
  START=("$PYTHON" -m uvicorn backend.app:app --host 127.0.0.1 --proxy-headers --forwarded-allow-ips 127.0.0.1)
fi

# systemd reads % as the start of a specifier and needs quotes around words with spaces.
quote() { local v="${1//%/%%}"; v="${v//\\/\\\\}"; printf '"%s"' "${v//\"/\\\"}"; }
# In ExecStart= systemd also replaces $NAME and ${NAME} with environment variables: $$ is a plain $.
quote_exec() { local v; v="$(quote "$1")"; printf '%s' "${v//\$/\$\$}"; }

current_data_dir() {
  # The data folder JobHuntPA would use now (JOBHUNT_DATA_DIR, the first run's pointer, data/ next to the app), else
  # the Linux default.
  "$PYTHON" -I -c '
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from backend import paths
place = paths.locate(Path(sys.argv[1]))
print(place.folder or paths.default_folder(Path(sys.argv[1])))
' "$APP_DIR"
}

install() {
  local port=8000 data=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --port) port="${2:-}"; shift 2 ;;
      --port=*) port="${1#*=}"; shift ;;
      --data-dir) data="${2:-}"; shift 2 ;;
      --data-dir=*) data="${1#*=}"; shift ;;
      *) die "Unknown option: $1 (see the top of service.sh)" ;;
    esac
  done
  [[ "$port" =~ ^[0-9]+$ ]] && [ "$port" -ge 1024 ] && [ "$port" -le 65535 ] \
    || die "--port takes a number from 1024 to 65535."
  if [ -z "$data" ]; then data="$(current_data_dir)"; fi
  case "$data" in
    "~"*) data="$HOME${data#\~}" ;;
  esac
  [[ "$data" = /* ]] || die "--data-dir takes the whole path of a folder, starting with /."
  # The same check as the first run's "Where your data lives" (backend/paths.py check_folder): a new or empty folder,
  # or one that already holds JobHuntPA's data, never a folder of your own files (the uninstaller deletes the data
  # folder). It also makes the folder, owner-only.
  data="$("$PYTHON" -I -c '
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from backend import paths
try:
    path, kind = paths.check_folder(sys.argv[2])
    paths.prepare_folder(path)
except paths.FolderProblem as problem:
    print(problem, file=sys.stderr)
    sys.exit(1)
print(path)
' "$APP_DIR" "$data")" || die "--data-dir: that folder can't be JobHuntPA's data folder (see the message above)."

  mkdir -p "$UNIT_DIR"
  local exec_line="" word
  for word in "${START[@]}" --port "$port"; do exec_line+="$(quote_exec "$word") "; done
  cat > "$UNIT" <<EOF
# Made by JobHuntPA's service.sh for the app in $IDENT. Remove it with: ./service.sh remove
[Unit]
Description=JobHuntPA (personal job-hunt dashboard)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$(quote "$([ -n "${APPIMAGE:-}" ] && printf '%s' "$data" || printf '%s' "$APP_DIR")")
Environment=$(quote "JOBHUNT_DATA_DIR=$data")
ExecStart=${exec_line% }
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
  chmod 600 "$UNIT"
  systemctl --user daemon-reload
  systemctl --user enable "$NAME" >/dev/null
  systemctl --user restart "$NAME"
  # Without lingering, a user service starts only when that user signs in; with it, at boot.
  if ! loginctl show-user "$USER" --property=Linger 2>/dev/null | grep -q "=yes"; then
    if ! loginctl enable-linger "$USER" 2>/dev/null; then
      say "To start JobHuntPA at boot (not only when you sign in), run once:  sudo loginctl enable-linger $USER"
    fi
  fi
  say "JobHuntPA now starts by itself at http://127.0.0.1:$port (data folder: $data)."
  say "No account yet? Open that address and use the setup code from $data/first-run.txt"
  say "(or: journalctl --user -u $NAME | grep first-run). Next: the README's \"Hosting on your own network\"."
}

remove() {
  if [ ! -f "$UNIT" ]; then say "The JobHuntPA service isn't installed."; return 0; fi
  systemctl --user disable --now "$NAME" >/dev/null 2>&1 || true
  rm -f "$UNIT"
  systemctl --user daemon-reload
  say "The JobHuntPA service is removed. Your data folder is untouched; start JobHuntPA with ./start.sh."
}

status() {
  if [ ! -f "$UNIT" ]; then say "The JobHuntPA service isn't installed. Install it with: ./service.sh install"; return 0; fi
  local port
  port="$(sed -n 's/.*--port" "\([0-9]*\)".*/\1/p' "$UNIT" | head -1)"
  systemctl --user --no-pager status "$NAME" || true
  if "$PYTHON" -I -c 'import sys, urllib.request; urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/health", timeout=3)' \
      "${port:-8000}" 2>/dev/null; then
    say "It answers at http://127.0.0.1:${port:-8000}"
  else
    say "It doesn't answer at http://127.0.0.1:${port:-8000} (yet). Its messages: journalctl --user -u $NAME"
  fi
}

case "${1:-}" in
  install) shift; install "$@" ;;
  remove) remove ;;
  status) status ;;
  *) die "Usage: ./service.sh install [--port N] [--data-dir PATH] | remove | status" ;;
esac
