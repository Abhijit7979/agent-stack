#!/usr/bin/env bash
# Work one GitHub issue end to end: fresh clone -> runner -> commit -> PR -> (auto-merge).
# Usage: work-issue.sh <issue-number>. Launched by dispatch.sh, which already set label agent-working.
# Guardrails are enforced here in code, not in the runner prompt (INTEND.md "hard guardrails").
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/stack.env"

N="$1"
BRANCH="agent/issue-$N"
WT="$WORK_ROOT/wt/$N"   # work tree: writable by this task's runner user only (ACL)
GD="$WORK_ROOT/git/$N"  # git metadata: hermes-only, the runner can't read or replace it

fail() {
  gh issue comment "$N" -R "$REPO" --body "🤖 Agent stopped: $1. Handing back to a human." || true
  gh issue edit "$N" -R "$REPO" --remove-label agent-working --add-label needs-human || true
  exit 1
}
U=""                    # runner unix user for this task (one per slot)
# Kill anything the runner left running, wipe its home (no state carries to the next task), drop the tree.
cleanup() {
  if [ -n "$U" ]; then
    sudo -n -u "$U" -- sh -c 'kill -9 -1' 2>/dev/null || true  # builtin kill; procps kill rejects -1
    sudo -n -u "$U" -H -- sh -c 'find "$HOME" -mindepth 1 -delete; rm -rf "$0"' "$WT" 2>/dev/null || true
  fi
  rm -rf "$WT" "$GD"
}
trap cleanup EXIT
trap 'fail "unexpected error (line $LINENO)"' ERR

# --- trust gate: only issues opened by owners/members/collaborators (issue text becomes the prompt) ---
ASSOC="$(gh api "repos/$REPO/issues/$N" --jq .author_association)"
case "$ASSOC" in OWNER|MEMBER|COLLABORATOR) ;; *) fail "issue author is not a repo collaborator ($ASSOC)" ;; esac

# --- runner slot: a dedicated unix user per in-flight task, so parallel tasks can't touch each other.
# The flock is held (fd 9) until this script exits.
mkdir -p "$WORK_ROOT/wt"; [ -d "$WORK_ROOT/git" ] || mkdir -m 700 "$WORK_ROOT/git"
for u in ${RUNNER_USERS:-}; do
  exec 9>"$WORK_ROOT/git/slot-$u.lock"
  if flock -n 9; then U="$u"; break; fi
done
[ -z "${RUNNER_USERS:-}" ] || [ -n "$U" ] || fail "no free runner slot"

# --- workspace: throwaway clone per issue, so nothing the runner leaves behind outlives the task ---
cleanup
gh repo clone "$REPO" "$WT" -- -q --separate-git-dir "$GD" --depth 1 -b "$BASE_BRANCH"
git -C "$WT" checkout -q -b "$BRANCH"
chmod 700 "$WT"  # other slot users can't even read it
[ -z "$U" ] || setfacl -R -m "u:$U:rwX,d:u:$U:rwX,d:u:$(id -un):rwX" "$WT"  # + we can read what it creates

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

# --- run the coding agent as the slot user: sudo resets the env (no GH_TOKEN) and that user can't
# read $HERMES_HOME. No RUNNER_USERS = same user, token merely stripped (local tests only).
as_runner() {
  if [ -n "$U" ]; then
    sudo -n -u "$U" -H -- sh -c 'cd "$0" && exec "$@"' "$WT" "$@"
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
# Every git call after the runner: explicit hermes-only git dir (ignore whatever `.git` the runner left
# in the work tree), no user/system config, no hooks, no fsmonitor; push auth comes explicitly from gh.
SAFE_GIT=(env GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 git --git-dir="$GD" --work-tree="$WT"
  -c core.hooksPath=/dev/null -c core.fsmonitor=false -c credential.helper=
  -c "credential.helper=!gh auth git-credential")
trap - ERR
deadline=$((SECONDS + TASK_TIMEOUT)); used="$MODEL"
for model in "$MODEL" "$FALLBACK_MODEL"; do
  left=$((deadline - SECONDS)); [ "$left" -gt 60 ] || break
  used="$model"
  run_runner "$model" "$left" && break
  [ -n "$("${SAFE_GIT[@]}" status --porcelain)" ] && break  # partial work exists; don't restart from a second model
done
trap 'fail "unexpected error (line $LINENO)"' ERR

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
