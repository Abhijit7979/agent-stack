#!/usr/bin/env bash
# Install the agent stack on the VPS (Linux). Safe to re-run.
# Prereqs: hermes, opencode, gh, git installed; REPO set in stack/config/stack.env.
set -euo pipefail
STACK="$(cd "$(dirname "$0")/.." && pwd)"
HH="${HERMES_HOME:-$HOME/.hermes}"
source "$STACK/config/stack.env"

for bin in hermes opencode gh git timeout setsid flock setfacl; do
  command -v "$bin" >/dev/null || { echo "missing: $bin" >&2; exit 1; }
done
[ "$REPO" != "OWNER/REPO" ] || { echo "set REPO in stack/config/stack.env first" >&2; exit 1; }

# 1. Scripts + config must live under $HERMES_HOME/scripts/ (cron rejects paths outside it).
mkdir -p "$HH/scripts"
cp "$STACK/config/stack.env" "$STACK"/scripts/*.sh "$HH/scripts/"
chmod +x "$HH"/scripts/{dispatch,work-issue,triage}.sh

# 2. No Hermes model needed: OpenCode's free tier only works from inside OpenCode ("FreeTierError"),
#    so every LLM call (triage + coding) goes through `opencode run`; Hermes schedules and delivers.
mkdir -p "$WORK_ROOT/wt" "$WORK_ROOT/logs"
for u in ${RUNNER_USERS:-} ${TRIAGE_USER:-}; do sudo -n -u "$u" true || { echo "cannot sudo to $u" >&2; exit 1; }; done

# 3. GitHub token -> $HH/agent-gh-token (0600), read by stack.env. Deliberately not a Hermes env
#    passthrough: that would expose it to every Hermes terminal session. Value never printed.
TOKEN_FILE="$HH/agent-gh-token"
if [ -n "${GH_TOKEN:-}" ] && [ ! -s "$TOKEN_FILE" ]; then (umask 077 && printf '%s' "$GH_TOKEN" > "$TOKEN_FILE"); fi
[ -s "$TOKEN_FILE" ] || { echo "no token: set GH_TOKEN in env or write $TOKEN_FILE (chmod 600)" >&2; exit 1; }
chmod 600 "$TOKEN_FILE"
sed -i '/^AGENT_GH_TOKEN=/d' "$HH/.env" 2>/dev/null || true   # migrate off the old passthrough
hermes config set terminal.env_passthrough '[]'

# 4. GitHub: git pushes via gh's token; labels the pipeline uses.
gh auth setup-git
gh label create agent-ready   -R "$REPO" --color 0E8A16 --force --description "Triaged: agent may work this"
gh label create needs-human   -R "$REPO" --color D93F0B --force --description "Triaged: human needed"
gh label create agent-working -R "$REPO" --color FBCA04 --force --description "Agent is working this"
gh label create agent-pr      -R "$REPO" --color 1D76DB --force --description "Agent opened a PR"

# 5. Cron jobs (run inside the Hermes gateway).
jobs="$(cat "$HH/cron/jobs.json" 2>/dev/null || true)"
grep -q agent-triage <<<"$jobs" || hermes cron create "every 10m" \
  --no-agent --script triage.sh --name agent-triage
grep -q agent-dispatch <<<"$jobs" || hermes cron create "every 5m" \
  --no-agent --script dispatch.sh --name agent-dispatch

cat <<EOF

Installed. Manual steps left (secrets are never handled by this script):
  1. Token in $HH/agent-gh-token (or GH_TOKEN in env before install) = bot's classic PAT, "repo" scope only (NOT
     "workflow"); bot = write collaborator on $REPO only. Fine-grained tokens can't reach another
     user's personal repo.
  2. Verify headless OpenCode:  cd /tmp && opencode run -m $MODEL "reply with just: ok"
  3. Branch protection on $BASE_BRANCH with required CI checks + repo "Allow auto-merge",
     otherwise the stack stays PR-only (readiness gate).
  4. Run Hermes as a boot service:  sudo hermes gateway install --system
  5. Check:  hermes cron status && hermes cron list
EOF
