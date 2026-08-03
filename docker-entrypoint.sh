#!/bin/sh
set -eu

DATA_DIR="${ASTRBOTEX_DATA_DIR:-/app/data}"
PLUGIN_DIR="$DATA_DIR/plugins"

mkdir -p "$PLUGIN_DIR" \
  "$PLUGIN_DIR/vision" \
  "$PLUGIN_DIR/perception" \
  "$PLUGIN_DIR/control" \
  "$PLUGIN_DIR/decision" \
  "$PLUGIN_DIR/special" \
  "$PLUGIN_DIR/interaction" \
  "$DATA_DIR/profiles/default"

exec "$@"
