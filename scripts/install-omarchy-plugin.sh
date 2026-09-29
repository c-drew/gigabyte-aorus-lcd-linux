#!/usr/bin/env bash
# Install the AORUS LCD bar widget into the Omarchy shell (no root needed).
#   scripts/install-omarchy-plugin.sh
# It talks to the aorus-lcd service (sudo scripts/install.sh) on 127.0.0.1:5090.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
id=io.github.c-drew.aorus-lcd
dest="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/$id"

omarchy plugin validate "$repo/omarchy-plugin"
# The shell refuses symlinks inside plugins, so this is a copy; re-run after updating.
mkdir -p "$dest"
cp -r "$repo/omarchy-plugin/." "$dest/"
omarchy-shell shell rescanPlugins >/dev/null 2>&1 || true
sleep 1
if jq -e --arg id "$id" '[.bar.layout[][]?.id] | index($id)' \
     "${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/shell.json" >/dev/null 2>&1; then
  echo "updated $dest (already on the bar)"
else
  omarchy plugin enable "$id" --section right --before omarchy.bluetooth ||
    omarchy plugin enable "$id" --section right
  echo "installed $dest and added it to the bar"
fi
