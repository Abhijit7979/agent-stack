#!/usr/bin/env bash
# Work one GitHub issue end to end: worktree -> runner -> commit -> PR -> (auto-merge).
# Usage: work-issue.sh <issue-number>. Launched by dispatch.sh, which already set label agent-working.
# Guardrails are enforced here in code, not in the runner prompt (INTEND.md "hard guardrails").
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/stack.env"

N="$1"
BRANCH="agent/issue-$N"
CLONE="$WORK_ROOT/repo"
WT="$WORK_ROOT/wt/$N"

fail() {
  gh issue comment "$N" -R "$REPO" --body "🤖 Agent stopped: $1. Handing back to a human." || true
  gh issue edit "$N" -R "$REPO" --remove-label agent-working --add-label needs-human || true
  exit 1
}
cleanup() { git -C "$CLONE" worktree remove --force "$WT" 2>/dev/null || true; }
trap cleanup EXIT
trap 'fail "unexpected error (line $LINENO)"' ERR

# --- workspace: one shared clone, one worktree per issue ---
[ -d "$CLONE/.git" ] || gh repo clone "$REPO" "$CLONE"
git -C "$CLONE" fetch -q origin "$BASE_BRANCH"
git -C "$CLONE" worktree remove --force "$WT" 2>/dev/null || true
git -C "$CLONE" branch -D "$BRANCH" 2>/dev/null || true
git -C "$CLONE" worktree add -q -b "$BRANCH" "$WT" "origin/$BASE_BRANCH"

TITLE="$(gh issue view "$N" -R "$REPO" --json title --jq .title)"
BODY="$(gh issue view "$N" -R "$REPO" --json body --jq .body)"
PROMPT="Resolve GitHub issue #$N in this repository.

Title: $TITLE

$BODY

Rules:
- Make the smallest change that fully resolves the issue. Add or update tests for it.
- Run the project's tests/linters if you can find how, and fix what you broke.
- Do not commit, push, or open PRs — that is done for you.
- Do not edit CI config (.github/), .env files, or anything holding secrets.
- If the issue is unclear or unsafe to do, change nothing."

# --- run the coding agent; GitHub token is stripped so it cannot push/merge on its own ---
run_runner() {  # $1 = model, $2 = seconds
  case "$RUNNER" in
    opencode) (cd "$WT" && env -u GH_TOKEN -u GITHUB_TOKEN timeout "$2" opencode run -m "$1" "$PROMPT") ;;
    *) echo "unknown RUNNER=$RUNNER" >&2; return 2 ;;
  esac
}
trap - ERR
deadline=$((SECONDS + TASK_TIMEOUT))
for model in "$MODEL" "$FALLBACK_MODEL"; do
  left=$((deadline - SECONDS)); [ "$left" -gt 60 ] || break
  run_runner "$model" "$left" && break
  [ -n "$(git -C "$WT" status --porcelain)" ] && break  # partial work exists; don't restart from a second model
done
trap 'fail "unexpected error (line $LINENO)"' ERR

git -C "$WT" add -A
CHANGED="$(git -C "$WT" diff --cached --name-only)"
[ -n "$CHANGED" ] || fail "the coding agent produced no changes (or ran out of its $((TASK_TIMEOUT / 60))-minute budget)"
if echo "$CHANGED" | grep -Eq "$PROTECTED_PATHS"; then
  fail "the change touches protected paths ($(echo "$CHANGED" | grep -E "$PROTECTED_PATHS" | tr '\n' ' '))"
fi

git -C "$WT" -c user.name="$BOT_NAME" -c user.email="$BOT_EMAIL" commit -q -m "$TITLE (#$N)"
git -C "$WT" push -q -f -u origin "$BRANCH"  # ponytail: -f only ever on our own agent/issue-N branch, never base

PR_URL="$(gh pr create -R "$REPO" --base "$BASE_BRANCH" --head "$BRANCH" --title "$TITLE" \
  --body "Closes #$N

Automated change by $RUNNER ($MODEL). Revert with: \`git revert -m 1 <merge-sha>\`.")"

# --- readiness gate: auto-merge only with auto-merge enabled + required checks on base ---
ready() {
  [ "$(gh api "repos/$REPO" --jq .allow_auto_merge)" = "true" ] || return 1
  local checks
  checks="$(gh api "repos/$REPO/branches/$BASE_BRANCH/protection" \
    --jq '(.required_status_checks.contexts // []) + [(.required_status_checks.checks // [])[].context] | length')" || return 1
  [ "$checks" -gt 0 ]
}
NOTE="PR opened, waiting for a human to merge (PR-only mode)."
if [ "$MERGE_MODE" = "auto" ] && ready 2>/dev/null; then
  gh pr merge "$PR_URL" -R "$REPO" --auto --squash --delete-branch
  NOTE="Auto-merge enabled: merges when required CI checks pass."
fi

gh issue edit "$N" -R "$REPO" --remove-label agent-working --add-label agent-pr
gh issue comment "$N" -R "$REPO" --body "🤖 $PR_URL — $NOTE"
echo "#$N -> $PR_URL"
