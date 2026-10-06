#!/usr/bin/env bash
# Hermes no-agent cron job: start work on agent-ready issues, up to MAX_PARALLEL in flight.
# GitHub labels are the state machine: agent-ready -> agent-working -> agent-pr | needs-human.
# Prints one line per dispatched issue (Hermes delivers it); empty output = silent tick.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/stack.env"
mkdir -p "$WORK_ROOT/logs"

# ponytail: a worker killed by a VPS reboot leaves its issue stuck on agent-working
# (it eats a slot); relabel it agent-ready by hand. Add a stale-claim sweep if this bites.
working="$(gh issue list -R "$REPO" --state open --label agent-working --json number --jq length)"
slots=$((MAX_PARALLEL - working))
[ "$slots" -gt 0 ] || exit 0

gh issue list -R "$REPO" --state open --label agent-ready --json number,createdAt \
  --jq "sort_by(.createdAt) | .[:$slots] | .[].number" | while read -r n; do
  # Claim before launching so the next tick can't double-dispatch.
  gh issue edit "$n" -R "$REPO" --remove-label agent-ready --add-label agent-working >/dev/null
  setsid nohup "$HERE/work-issue.sh" "$n" >>"$WORK_ROOT/logs/issue-$n.log" 2>&1 </dev/null &
  echo "Dispatched #$n to $RUNNER"
done
