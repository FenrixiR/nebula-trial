#!/bin/sh
set -e
here=$(cd "$(dirname "$0")" && pwd)
dest="$1/klippy/plugins"
[ -d "$dest" ] || { echo "no $dest"; exit 1; }
for f in "$here"/plugins/*.py; do ln -sf "$f" "$dest/$(basename "$f")"; echo "linked $(basename "$f")"; done
