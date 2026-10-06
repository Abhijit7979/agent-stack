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
mkdir -p "$HH/scripts" "$HH/skills"
cp "$STACK/config/stack.env" "$STACK"/scripts/*.sh "$HH/scripts/"
chmod +x "$HH"/scripts/{dispatch,work-issue,triage-feed}.sh
rm -rf "$HH/skills/issue-triage" && cp -r "$STACK/skills/issue-triage" "$HH/skills/"

# 2. Hermes's own model: NOT set here. OpenCode's free tier only works from inside OpenCode
#    ("FreeTierError"), so Hermes needs its own provider — configure with `hermes model`.
mkdir -p "$WORK_ROOT/wt" "$WORK_ROOT/logs"
for u in ${RUNNER_USERS:-}; do sudo -n -u "$u" true || { echo "cannot sudo to $u" >&2; exit 1; }; done

# 3. GitHub: git pushes via gh's token; labels the pipeline uses.
gh auth setup-git
gh label create agent-ready   -R "$REPO" --color 0E8A16 --force --description "Triaged: agent may work this"
gh label create needs-human   -R "$REPO" --color D93F0B --force --description "Triaged: human needed"
gh label create agent-working -R "$REPO" --color FBCA04 --force --description "Agent is working this"
gh label create agent-pr      -R "$REPO" --color 1D76DB --force --description "Agent opened a PR"

# 4. Cron jobs (run inside the Hermes gateway).
jobs="$(cat "$HH/cron/jobs.json" 2>/dev/null || true)"
grep -q agent-triage <<<"$jobs" || hermes cron create "every 10m" \
  "Triage the untriaged issues listed in this message for $REPO using the issue-triage skill." \
  --skill issue-triage --script triage-feed.sh --name agent-triage
grep -q agent-dispatch <<<"$jobs" || hermes cron create "every 5m" \
  --no-agent --script dispatch.sh --name agent-dispatch

cat <<EOF

Installed. Manual steps left (secrets are never handled by this script):
  1. In $HH/.env set: GH_TOKEN=<bot fine-grained PAT>   OPENCODE_ZEN_API_KEY=<key>
     PAT scope: only $REPO — Contents RW, Pull requests RW, Issues RW, Metadata R,
     Administration R (readiness gate reads branch protection). NO Workflows permission.
  2. In $HH/config.yaml add:   terminal: { env_passthrough: [GH_TOKEN] }
  3. Verify headless OpenCode:  cd /tmp && opencode run -m $MODEL "reply with just: ok"
  4. Branch protection on $BASE_BRANCH with required CI checks + repo "Allow auto-merge",
     otherwise the stack stays PR-only (readiness gate).
  5. Run Hermes as a boot service:  sudo hermes gateway install --system
  6. Check:  hermes cron status && hermes cron list
EOF
