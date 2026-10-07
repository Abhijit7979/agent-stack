#!/usr/bin/env bash
# Hermes no-agent cron job: start work on agent-ready issues, up to MAX_PARALLEL in flight.
# GitHub labels are the state machine: agent-ready -> agent-working -> agent-pr | needs-human.
# Prints one line per dispatched issue (Hermes delivers it); empty output = silent tick.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/stack.env"
REPO="${1:-$REPO}"
ONLY="${2:-}"
[[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo "invalid repository" >&2; exit 2; }
[[ -z "$ONLY" || "$ONLY" =~ ^[0-9]+$ ]] || { echo "invalid issue number" >&2; exit 2; }
mkdir -p "$WORK_ROOT/logs" "$WORK_ROOT/git"
exec 8>"$WORK_ROOT/git/dispatch.lock"
flock -n 8 || exit 0

# ponytail: a worker killed by a VPS reboot leaves its issue stuck on agent-working
# (it eats a slot); relabel it agent-ready by hand. Add a stale-claim sweep if this bites.
working="$(gh issue list -R "$REPO" --state open --label agent-working --json number --jq length)"
slots=$((MAX_PARALLEL - working))
[ "$slots" -gt 0 ] || exit 0

gh issue list -R "$REPO" --state open --label agent-ready --limit 1000 --json number,createdAt \
  --jq "sort_by(.createdAt) | map(select(.number == ${ONLY:-.number})) | .[:$slots] | .[].number" | while read -r n; do
  # Claim before launching so the next tick can't double-dispatch.
  gh issue edit "$n" -R "$REPO" --remove-label agent-ready --add-label agent-working >/dev/null
  setsid nohup "$HERE/work-issue.sh" "$n" "$REPO" >>"$WORK_ROOT/logs/${REPO//\//--}-$n.log" 2>&1 </dev/null 8>&- &
  echo "Dispatched $REPO#$n to $RUNNER"
done
