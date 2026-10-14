#!/usr/bin/env bash
# Build the Linux AppImage: JobHuntPA with its own Python and packages inside (docs/design/open-source.md §1.1).
#
#   installer/appimage.sh [--out DIR]
#
# Writes DIR/JobHuntPA-<VERSION>-x86_64.AppImage (default DIR: dist/). .github/workflows/release.yml runs it on a tag,
# on the oldest supported Ubuntu runner, and ci.yml builds it and starts it on every push; a developer can run it on
# any x86-64 Linux with bash, curl, tar, sha256sum and desktop-file-validate (desktop-file-utils). It downloads two pinned, checksum-verified files from GitHub
# (a relocatable CPython from python-build-standalone, and appimagetool, which itself fetches the AppImage runtime)
# and the packages in requirements.txt from PyPI. Nothing is installed on this computer: everything happens in
# build/appimage/.
#
# The browser for JavaScript-only careers pages is not inside: the app downloads it into the data folder when a
# careers page first needs it and the person agrees.
set -euo pipefail

PYTHON_VERSION="3.12.12"
PBS_RELEASE="20251014"
PBS_FILE="cpython-${PYTHON_VERSION}+${PBS_RELEASE}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${PBS_FILE}"
PBS_SHA256="c74addcd1b033a6e4d60ead3ab47fcc995569027e01d3061c4a934f363c4a0cf"
TOOL_VERSION="1.9.0"
TOOL_URL="https://github.com/AppImage/appimagetool/releases/download/${TOOL_VERSION}/appimagetool-x86_64.AppImage"
TOOL_SHA256="46fdd785094c7f6e545b61afcfb0f3d98d8eab243f644b4b17698c01d06083d1"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/dist"
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --out=*) OUT="${1#*=}"; shift ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

fail() {
  echo >&2
  echo "AppImage build stopped: $*" >&2
  exit 1
}

[ "$(uname -s)" = "Linux" ] || fail "an AppImage is built on Linux (this is $(uname -s))."
[ "$(uname -m)" = "x86_64" ] || fail "this script builds the x86-64 AppImage, on an x86-64 computer (this is $(uname -m))."
for tool in curl tar sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || fail "it needs '$tool', which isn't installed."
done
# appimagetool checks the menu entry with it, and stops without a word when it's missing.
command -v desktop-file-validate >/dev/null 2>&1 \
  || fail "it needs desktop-file-validate: install desktop-file-utils (sudo apt install desktop-file-utils)."

VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
WORK="$ROOT/build/appimage"
CACHE="$WORK/downloads"
APPDIR="$WORK/JobHuntPA.AppDir"
TARGET="$OUT/JobHuntPA-${VERSION}-x86_64.AppImage"
mkdir -p "$CACHE" "$OUT"

# A file from GitHub, kept in build/appimage/downloads and checked against its pinned SHA-256 every time.
fetch() {
  local url="$1" dest="$2" sum="$3"
  if [ ! -f "$dest" ] || ! echo "$sum  $dest" | sha256sum -c --status; then
    echo "Downloading $url"
    rm -f "$dest.part"
    curl -fSL --retry 3 --connect-timeout 30 -o "$dest.part" "$url" \
      || fail "couldn't download $url. Check the internet connection (and any proxy), then run this again."
    mv "$dest.part" "$dest"
  fi
  echo "$sum  $dest" | sha256sum -c --status \
    || { rm -f "$dest"; fail "$(basename "$dest") doesn't match its pinned checksum. Nothing was built."; }
}

echo "Building JobHuntPA $VERSION AppImage in $WORK"
fetch "$PBS_URL" "$CACHE/$PBS_FILE" "$PBS_SHA256"
fetch "$TOOL_URL" "$CACHE/appimagetool" "$TOOL_SHA256"
chmod +x "$CACHE/appimagetool"

rm -rf "$APPDIR"
mkdir -p "$APPDIR/app"

echo "Unpacking Python $PYTHON_VERSION"
tar -xzf "$CACHE/$PBS_FILE" -C "$APPDIR"  # makes $APPDIR/python
PY="$APPDIR/python/bin/python3"
[ -x "$PY" ] || fail "the Python archive didn't hold python/bin/python3."

echo "Installing the packages from requirements.txt"
"$PY" -m pip install --disable-pip-version-check --no-cache-dir --no-compile -r "$ROOT/requirements.txt" \
  || fail "installing the packages failed (see above). Check the internet connection, then run this again."

echo "Copying the app"
# The files the app runs from; tests, tools and design notes stay out.
if git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git -C "$ROOT" ls-files -z -- backend frontend installer VERSION requirements.txt service.sh README.md 'LICENSE*' \
    | (cd "$ROOT" && xargs -0 cp --parents -t "$APPDIR/app")
else
  (cd "$ROOT" && cp -r --parents backend frontend installer VERSION requirements.txt service.sh README.md "$APPDIR/app")
  find "$APPDIR/app" -name __pycache__ -type d -prune -exec rm -rf {} +
fi
[ -f "$APPDIR/app/backend/app.py" ] || fail "the app's files weren't copied."
chmod +x "$APPDIR/app/service.sh"

# The AppImage is mounted read-only, so Python can't save compiled files at run time: compile them now.
"$PY" -m compileall -q -j 0 "$APPDIR/app" "$APPDIR/python/lib" >/dev/null || true

cp "$ROOT/installer/appimage/AppRun" "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"
cp "$ROOT/installer/appimage/jobhuntpa.desktop" "$APPDIR/jobhuntpa.desktop"
cp "$ROOT/installer/appimage/jobhuntpa.svg" "$APPDIR/jobhuntpa.svg"
cp "$ROOT/installer/appimage/jobhuntpa.svg" "$APPDIR/.DirIcon"

echo "Checking that the app loads with the bundled Python"
CHECK="$(mktemp -d)"
JOBHUNT_DATA_DIR="$CHECK" JOBHUNT_ENV_PATH="$CHECK/.env" PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
  "$PY" -c "import sys; sys.path.insert(0, '$APPDIR/app'); import backend.app" \
  || { rm -rf "$CHECK"; fail "the app doesn't load with the bundled Python (see above)."; }
rm -rf "$CHECK"

echo "Packing $TARGET"
rm -f "$TARGET"
# appimagetool is itself an AppImage: extract-and-run works where FUSE isn't available (containers, CI).
APPIMAGE_EXTRACT_AND_RUN=1 ARCH=x86_64 "$CACHE/appimagetool" --no-appstream "$APPDIR" "$TARGET" \
  || fail "appimagetool couldn't pack the AppImage (see above). It downloads the AppImage runtime from GitHub."
chmod +x "$TARGET"
echo "Done: $TARGET ($(du -h "$TARGET" | cut -f1))"
