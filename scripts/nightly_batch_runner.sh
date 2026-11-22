#!/bin/zsh
# Nightly unattended trigger for /bprep (test-coverage + complexity batch pass).
#
# Why this script exists instead of a plain launchd StartCalendarInterval at a
# fixed local-clock time: the operator asked for "01:00 IST", and IST has no
# DST while this machine's local zone (America/New_York) does — a fixed local
# time would drift by an hour twice a year. Instead this script is invoked
# frequently (every 15 min, via the paired launchd plist's StartInterval) and
# self-gates: it only actually fires once it's genuinely ~01:00 IST, and only
# once per IST calendar day (marker file), so it tracks IST correctly across
# every DST transition with no manual maintenance.
#
# Runs claude headless (-p, --permission-mode bypassPermissions) so the batch
# commands (/bprep -> /prep -> /tprep + /refact) can edit/commit to `workshop`
# with no interactive prompt. Those commands never push to dev/main — see
# .claude/commands/bprep.md.

set -u

PROJECT_DIR="$HOME/projects/ramboq"
MARKER_DIR="$PROJECT_DIR/.claude/.nightly-batch-markers"
LOG_DIR="$PROJECT_DIR/.claude/.nightly-batch-logs"
mkdir -p "$MARKER_DIR" "$LOG_DIR"

IST_DATE="$(TZ=Asia/Kolkata date +%Y-%m-%d)"
IST_HOUR="$(TZ=Asia/Kolkata date +%H)"
MARKER_FILE="$MARKER_DIR/$IST_DATE.ran"

# Fire window: 01:00-01:14 IST (a 15-min window matching the paired plist's
# StartInterval, so exactly one of the ~4 ticks inside the window lands here).
if [[ "$IST_HOUR" != "01" ]]; then
  exit 0
fi
if [[ -f "$MARKER_FILE" ]]; then
  exit 0
fi

touch "$MARKER_FILE"
cd "$PROJECT_DIR" || exit 1

LOG_FILE="$LOG_DIR/$IST_DATE.log"
{
  echo "=== nightly_batch_runner.sh fired at $(date) (IST $(TZ=Asia/Kolkata date)) ==="
  claude -p "/bprep" --permission-mode bypassPermissions
  echo "=== exit code $? ==="
} >> "$LOG_FILE" 2>&1
