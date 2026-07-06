#!/bin/sh
set -eu

DATA_DIR="${ASTRBOTEX_DATA_DIR:-/app/data}"
PLUGIN_DIR="$DATA_DIR/plugins"
BUILTIN_DIR="/app/builtin_plugins"

mkdir -p "$PLUGIN_DIR" \
  "$PLUGIN_DIR/vision" \
  "$PLUGIN_DIR/perception" \
  "$PLUGIN_DIR/control" \
  "$PLUGIN_DIR/decision" \
  "$PLUGIN_DIR/special" \
  "$DATA_DIR/profiles/default"

if [ -d "$BUILTIN_DIR" ]; then
  find "$BUILTIN_DIR" -mindepth 1 -maxdepth 3 -type f -name plugin.json | while IFS= read -r manifest; do
    src_dir="$(dirname "$manifest")"
    rel_dir="${src_dir#$BUILTIN_DIR/}"
    dst_dir="$PLUGIN_DIR/$rel_dir"
    if [ ! -e "$dst_dir/plugin.json" ]; then
      mkdir -p "$(dirname "$dst_dir")"
      cp -a "$src_dir" "$dst_dir"
      echo "Installed builtin plugin: $rel_dir"
    fi
  done
fi

exec "$@"
