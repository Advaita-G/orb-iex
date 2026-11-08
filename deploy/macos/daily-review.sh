#!/bin/bash
# After-close pipeline: loss review, reconciliation, data audit, one research hypothesis
# (docs/DAILY-REVIEW.md + docs/DAILY-RESEARCH.md). Run by launchd (com.orbiex.daily-review).
# Never touches the running bot. Idempotent: a day with a report is never redone.
#
# Env (tests/dry runs): ORB_DAILY_DRY_RUN=1 prints what would run and calls nothing;
# ORB_DAILY_NOW=<ISO time> overrides "now" for the gate; ORB_DAILY_CLAUDE=<path> overrides the binary;
# ORB_DAILY_ROOT, ORB_DAILY_PY, ORB_DAILY_TIMEOUT_S (default 5400), ORB_DAILY_RETRY_WAIT_S (default 120).
set -uo pipefail

ROOT=${ORB_DAILY_ROOT:-~/orb-iex}
PY=${ORB_DAILY_PY:-$ROOT/.venv/bin/python}
REPORTS="$ROOT/runtime/reports"
LOGDIR="$ROOT/runtime/logs"
LOCK="$ROOT/runtime/daily-review.lock"
TIMEOUT=${ORB_DAILY_TIMEOUT_S:-5400}
RETRY_WAIT=${ORB_DAILY_RETRY_WAIT_S:-120}
MAX_TRIES=2
RC=0

cd "$ROOT" || exit 1
mkdir -p "$REPORTS" "$LOGDIR"
log() { echo "$(date '+%F %T') $*" >>"$LOGDIR/daily-review-launchd.log"; }

# One run at a time. A lock whose pid is gone (crash, reboot) is stale and replaced.
if ! mkdir "$LOCK" 2>/dev/null; then
  if kill -0 "$(cat "$LOCK/pid" 2>/dev/null)" 2>/dev/null; then log "another run is active; exiting"; exit 0; fi
  log "stale lock removed"; rm -rf "$LOCK"; mkdir "$LOCK" || exit 1
fi
echo $$ >"$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

pick_claude() {
  for c in "${ORB_DAILY_CLAUDE:-}" ~/.local/bin/claude ~/.npm-global/bin/claude; do
    [ -n "$c" ] && [ -x "$c" ] && "$c" --version >/dev/null 2>&1 && { echo "$c"; return 0; }
  done
  return 1
}

# Days owed a review: closed sessions (holidays and weekends excluded, early closes honoured)
# without a report, oldest first, up to 3 days back and 3 failed attempts each.
GATE_ARGS=(--reports-dir "$REPORTS")
[ -n "${ORB_DAILY_NOW:-}" ] && GATE_ARGS+=(--now "$ORB_DAILY_NOW")
DAYS=$("$PY" -m orb_iex daily-gate "${GATE_ARGS[@]}") || { log "daily-gate failed"; exit 1; }
if [ -z "$DAYS" ]; then log "nothing due"; exit 0; fi

if [ -n "${ORB_DAILY_DRY_RUN:-}" ]; then
  echo "DRY RUN: would review: $(echo $DAYS | tr '\n' ' ')"; echo "claude: $(pick_claude || echo NONE)"; exit 0
fi

CLAUDE=$(pick_claude) || { log "no working claude binary"
  /usr/bin/osascript -e 'display notification "Daily review cannot start: no working claude binary" with title "ORB paper bot"' >/dev/null 2>&1
  exit 1; }

run_claude() {  # $1 = day; kills the run after TIMEOUT seconds
  local day=$1 log_file="$LOGDIR/daily-review-$1.log" pid dog
  "$CLAUDE" -p "Today is $day (America/New_York). DAY=$day. Follow docs/DAILY-REVIEW.md and docs/DAILY-RESEARCH.md exactly, start to finish. Review session $day even if it is not today's date." \
    --permission-mode bypassPermissions \
    --disallowedTools "Bash(git push:*)" "Bash(git merge:*)" "Bash(git rebase:*)" "Bash(git reset:*)" \
                      "Bash(git checkout:*)" "Bash(git switch:*)" "Bash(git stash:*)" \
                      "Bash(launchctl:*)" "Bash(kill:*)" "Bash(pkill:*)" "Bash(killall:*)" \
    >>"$log_file" 2>&1 &
  pid=$!
  ( sleep "$TIMEOUT"; kill "$pid" 2>/dev/null ) >/dev/null 2>&1 & dog=$!
  wait "$pid"; local st=$?
  pkill -P "$dog" 2>/dev/null; kill "$dog" 2>/dev/null; wait "$dog" 2>/dev/null
  return $st
}

for DAY in $DAYS; do
  REPORT="$REPORTS/daily-review-$DAY.md"; ATT="$REPORTS/daily-review-$DAY.attempts"
  for try in $(seq 1 "$MAX_TRIES"); do
    [ -f "$REPORT" ] && break
    echo $(( $(cat "$ATT" 2>/dev/null || echo 0) + 1 )) >"$ATT"
    echo "== daily review $DAY try $try started $(date)" >>"$LOGDIR/daily-review-$DAY.log"
    run_claude "$DAY"; status=$?
    echo "== finished $(date) exit=$status" >>"$LOGDIR/daily-review-$DAY.log"
    [ -f "$REPORT" ] && break
    log "$DAY try $try failed (exit $status, no report)"
    [ "$try" -lt "$MAX_TRIES" ] && sleep "$RETRY_WAIT"
  done
  if [ -f "$REPORT" ]; then
    rm -f "$ATT"; msg="Daily review $DAY done. Report: runtime/reports/daily-review-$DAY.md"
  else
    RC=1; msg="Daily review $DAY FAILED. Log: runtime/logs/daily-review-$DAY.log"
  fi
  log "$msg"
  /usr/bin/osascript -e "display notification \"$msg\" with title \"ORB paper bot\"" >/dev/null 2>&1 || log "notification failed"
done
exit $RC
