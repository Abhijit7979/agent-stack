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

# 3. GitHub token: Hermes never passes GH_TOKEN to children, so cron scripts get it as AGENT_GH_TOKEN
#    (env_passthrough + the profile's .env). Taken from the process env if given there (docker
#    env_file); the value is never printed.
touch "$HH/.env" && chmod 600 "$HH/.env"
if [ -n "${GH_TOKEN:-}" ] && ! grep -q '^AGENT_GH_TOKEN=' "$HH/.env"; then
  printf 'AGENT_GH_TOKEN=%s\n' "$GH_TOKEN" >> "$HH/.env"
fi
grep -q '^AGENT_GH_TOKEN=' "$HH/.env" || { echo "AGENT_GH_TOKEN missing from $HH/.env" >&2; exit 1; }
hermes config set terminal.env_passthrough '["AGENT_GH_TOKEN"]'

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
  1. AGENT_GH_TOKEN in $HH/.env (or GH_TOKEN in env before install) = bot's classic PAT, "repo" scope only (NOT
     "workflow"); bot = write collaborator on $REPO only. Fine-grained tokens can't reach another
     user's personal repo.
  2. Restart the gateway so it loads .env:  hermes gateway restart   (Docker: docker compose restart)
  3. Verify headless OpenCode:  cd /tmp && opencode run -m $MODEL "reply with just: ok"
  4. Branch protection on $BASE_BRANCH with required CI checks + repo "Allow auto-merge",
     otherwise the stack stays PR-only (readiness gate).
  5. Run Hermes as a boot service:  sudo hermes gateway install --system
  6. Check:  hermes cron status && hermes cron list
EOF
