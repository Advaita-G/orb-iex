#!/bin/bash
# Install or update the daily-review launchd job. Idempotent. Touches only com.orbiex.daily-review
# (never the com.orbiex.paper trading job). Usage: install-daily-review.sh [--uninstall]
set -euo pipefail
LABEL=com.orbiex.daily-review
SRC="$(cd "$(dirname "$0")" && pwd)/$LABEL.plist"
DST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

plutil -lint "$SRC" >/dev/null
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
if [ "${1:-}" = "--uninstall" ]; then rm -f "$DST"; echo "uninstalled $LABEL"; exit 0; fi
mkdir -p "$HOME/Library/LaunchAgents"
cp "$SRC" "$DST"
launchctl bootstrap "$DOMAIN" "$DST"
launchctl print "$DOMAIN/$LABEL" | grep -E "state|run interval|runs|last exit" | head -5
echo "installed $LABEL"
