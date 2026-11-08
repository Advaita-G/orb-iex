#!/bin/bash
# Install or update the two gap-up SHADOW launchd jobs. Idempotent. Touches ONLY com.orbiex.gapup-shadow and
# com.orbiex.gapup-review (never com.orbiex.shadow, daily-review, journal or paper). Usage: install-gapup.sh [--uninstall]
set -euo pipefail
DOMAIN="gui/$(id -u)"
for LABEL in com.orbiex.gapup-shadow com.orbiex.gapup-review; do
  SRC="$(cd "$(dirname "$0")" && pwd)/$LABEL.plist"
  DST="$HOME/Library/LaunchAgents/$LABEL.plist"
  plutil -lint "$SRC" >/dev/null
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  if [ "${1:-}" = "--uninstall" ]; then rm -f "$DST"; echo "uninstalled $LABEL"; continue; fi
  mkdir -p "$HOME/Library/LaunchAgents"
  cp "$SRC" "$DST"
  launchctl bootstrap "$DOMAIN" "$DST"
  launchctl print "$DOMAIN/$LABEL" | grep -E "state|runs|last exit" | head -3
  echo "installed $LABEL"
done
