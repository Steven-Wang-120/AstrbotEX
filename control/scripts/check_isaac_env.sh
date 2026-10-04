#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/lib/isaac_common.sh"
exec python -B "$ASTREX_ROOT/scripts/lib/isaac_entry.py" check "$@"
