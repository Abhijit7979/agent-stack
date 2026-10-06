#!/usr/bin/env bash
# Pre-run script for the Hermes triage cron job: prints open issues that have no stack label yet.
# Nothing to triage -> {"wakeAgent": false}, so the tick costs zero LLM calls.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/stack.env"

issues="$(gh issue list -R "$REPO" --state open --limit 20 --json number,title,body,labels --jq '
  [ .[] | select([.labels[].name] | any(. == "agent-ready" or . == "needs-human"
                                         or . == "agent-working" or . == "agent-pr") | not)
        | {number, title, body: (.body // "" | .[0:4000])} ]')"

if [ "$issues" = "[]" ]; then
  echo '{"wakeAgent": false}'
else
  echo "Repository: $REPO"
  echo "Untriaged issues:"
  echo "$issues"
fi
