#!/usr/bin/env bash
# Copy the working tree into WSL for building.
#
# Written as a FILE rather than an inline `wsl -- bash -lc '...'` because the
# Windows-side argument parsing collapses quoted paths containing spaces, and
# "C:\Users\The Laptop Hut" contains two. An earlier inline version silently
# expanded to an empty variable — with `cp -r "$SRC"/*` that became `cp -r /*`
# and copied the host filesystem. Hence: paths resolved here, `set -eu`, and an
# explicit existence check before anything is written.
set -eu

SRC="/mnt/c/Users/The Laptop Hut/Desktop/edx/coursemate"
DST="$HOME/cm-build"

test -d "$SRC/packages" || { echo "FATAL: source tree not found at $SRC" >&2; exit 1; }
test -d "$DST" || { echo "FATAL: build dir not found at $DST" >&2; exit 1; }

rsync -a --delete --exclude '__pycache__' --exclude '*.egg-info' \
      "$SRC/packages/" "$DST/packages/"
rsync -a "$SRC/deploy/" "$DST/deploy/"
echo "synced $SRC -> $DST"
