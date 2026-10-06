#!/usr/bin/env bash
# Work one GitHub issue end to end: fresh clone -> runner -> commit -> PR -> (auto-merge).
# Usage: work-issue.sh <issue-number>. Launched by dispatch.sh, which already set label agent-working.
# Guardrails are enforced here in code, not in the runner prompt (INTEND.md "hard guardrails").
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/stack.env"
umask 002  # checkout is group-writable so RUNNER_USER (same group) can edit it

N="$1"
BRANCH="agent/issue-$N"
WT="$WORK_ROOT/wt/$N"

fail() {
  gh issue comment "$N" -R "$REPO" --body "🤖 Agent stopped: $1. Handing back to a human." || true
  gh issue edit "$N" -R "$REPO" --remove-label agent-working --add-label needs-human || true
  exit 1
}
cleanup() { rm -rf "$WT"; }
trap cleanup EXIT
trap 'fail "unexpected error (line $LINENO)"' ERR

# --- trust gate: only issues opened by owners/members/collaborators (issue text becomes the prompt) ---
ASSOC="$(gh api "repos/$REPO/issues/$N" --jq .author_association)"
case "$ASSOC" in OWNER|MEMBER|COLLABORATOR) ;; *) fail "issue author is not a repo collaborator ($ASSOC)" ;; esac

# --- workspace: throwaway clone per issue, so nothing the runner leaves behind outlives the task ---
rm -rf "$WT"
gh repo clone "$REPO" "$WT" -- -q --depth 1 -b "$BASE_BRANCH"
git -C "$WT" checkout -q -b "$BRANCH"
chmod -R go-w "$WT/.git"  # runner may edit the work tree, never git internals

TITLE="$(gh issue view "$N" -R "$REPO" --json title --jq .title)"
BODY="$(gh issue view "$N" -R "$REPO" --json body --jq .body)"
PROMPT="Resolve GitHub issue #$N in this repository.

<issue>
Title: $TITLE

$BODY
</issue>

Rules:
- Make the smallest change that fully resolves the issue. Add or update tests for it.
- Run the project's tests/linters if you can find how, and fix what you broke.
- Do not commit, push, or open PRs — that is done for you.
- Do not edit CI config (.github/), .env files, or anything holding secrets.
- If the issue is unclear or unsafe to do, change nothing."

# --- run the coding agent as RUNNER_USER: sudo resets the env (no GH_TOKEN) and that user can't
# read $HERMES_HOME. Empty RUNNER_USER = same user, token merely stripped (local tests only).
as_runner() {
  if [ -n "${RUNNER_USER:-}" ]; then
    sudo -n -u "$RUNNER_USER" -H -- sh -c 'umask 002; cd "$0" && exec "$@"' "$WT" "$@"
  else
    (cd "$WT" && env -u GH_TOKEN -u GITHUB_TOKEN "$@")
  fi
}
run_runner() {  # $1 = model, $2 = seconds
  case "$RUNNER" in
    opencode) as_runner timeout "$2" opencode run -m "$1" "$PROMPT" ;;
    *) echo "unknown RUNNER=$RUNNER" >&2; return 2 ;;
  esac
}
# Fingerprint git config/hooks the runner could tamper with to run code under our GH_TOKEN later.
gitstate() { cat "$WT/.git/config"; ls -la "$WT/.git/hooks"; }
before="$(gitstate | cksum)"
# Every git call after the runner: ignore user/system config (runner could edit ~/.gitconfig), no hooks,
# no fsmonitor; push auth comes explicitly from gh.
SAFE_GIT=(env GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 git -c core.hooksPath=/dev/null
  -c core.fsmonitor=false -c credential.helper= -c "credential.helper=!gh auth git-credential" -C "$WT")
trap - ERR
deadline=$((SECONDS + TASK_TIMEOUT)); used="$MODEL"
for model in "$MODEL" "$FALLBACK_MODEL"; do
  left=$((deadline - SECONDS)); [ "$left" -gt 60 ] || break
  used="$model"
  run_runner "$model" "$left" && break
  [ -n "$("${SAFE_GIT[@]}" status --porcelain)" ] && break  # partial work exists; don't restart from a second model
done
trap 'fail "unexpected error (line $LINENO)"' ERR

[ "$(gitstate | cksum)" = "$before" ] || fail "the coding agent modified git config or hooks"
"${SAFE_GIT[@]}" add -A
CHANGED="$("${SAFE_GIT[@]}" diff --cached --name-only)"
[ -n "$CHANGED" ] || fail "the coding agent produced no changes (or ran out of its $((TASK_TIMEOUT / 60))-minute budget)"
if echo "$CHANGED" | grep -Eq "$PROTECTED_PATHS"; then
  fail "the change touches protected paths ($(echo "$CHANGED" | grep -E "$PROTECTED_PATHS" | tr '\n' ' '))"
fi

"${SAFE_GIT[@]}" -c user.name="$BOT_NAME" -c user.email="$BOT_EMAIL" commit -q -m "$TITLE (#$N)"
"${SAFE_GIT[@]}" push -q -f -u origin "$BRANCH"  # ponytail: -f only ever on our own agent/issue-N branch, never base

PR_URL="$(gh pr create -R "$REPO" --base "$BASE_BRANCH" --head "$BRANCH" --title "$TITLE" \
  --body "Closes #$N

Automated change by $RUNNER ($used). Revert with: \`git revert -m 1 <merge-sha>\`.")"

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
