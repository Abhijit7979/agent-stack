#!/usr/bin/env bash
# Offline check of work-issue.sh + dispatch.sh: real git against a local bare "origin",
# stubbed gh/opencode/timeout/setsid on PATH. Run: bash stack/tests/test_pipeline.sh
# shellcheck disable=SC2034  # vars are read inside check()'s eval strings
set -euo pipefail
STACK="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
pass=0; fail=0
check() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# --- origin repo ---
git init -q -b main "$T/seed" && git -C "$T/seed" -c user.name=t -c user.email=t@t commit -q --allow-empty -m init
git clone -q --bare "$T/seed" "$T/origin.git"

# --- installed layout: scripts + stack.env side by side, like $HERMES_HOME/scripts ---
mkdir -p "$T/scripts" "$T/bin"
cp "$STACK"/scripts/*.sh "$T/scripts/"
sed -e 's|^REPO=.*|REPO="acme/app"|' -e 's|^TRIAGE_USER=.*|TRIAGE_USER=""|' -e "s|^WORK_ROOT=.*|WORK_ROOT=\"$T/work\"|" -e 's|^RUNNER_USERS=.*|RUNNER_USERS=""|' \
  "$STACK/config/stack.env" > "$T/scripts/stack.env"

# --- stubs ---
cat > "$T/bin/gh" <<EOF
#!/usr/bin/env bash
echo "gh \$*" >> "$T/gh.log"
case "\$1 \$2" in
  "repo clone") git clone -q "\${@:6}" "$T/origin.git" "\$4" 2>/dev/null ;;
  "issue view") [[ "\$*" == *.title* ]] && echo "Fix the thing" || echo "Please fix it." ;;
  "pr create")  echo "https://github.com/acme/app/pull/7" ;;
  "issue list") [[ "\$*" == *agent-working* ]] && { echo "\${WORKING:-0}"; exit; }
                q="\${@: -1}"; echo '[{"number":13,"createdAt":"3"},{"number":11,"createdAt":"1"},{"number":12,"createdAt":"2"}]' | jq -r "\$q" ;;
  api*) case "\$*" in
          *branches/*) [ -n "\${PROTECTED:-}" ] && echo 1 || echo 0 ;;
          *issues\?state*) q="\${@: -1}"; echo '[{"number":21,"author_association":"COLLABORATOR","labels":[]},
              {"number":22,"author_association":"NONE","labels":[]},
              {"number":23,"author_association":"OWNER","labels":[{"name":"needs-human"}]}]' | jq -r "\$q" ;;
          *issues/*)    echo "\${ASSOC:-COLLABORATOR}" ;;
          *)            echo "\${AUTOMERGE:-false}" ;;
        esac ;;
esac
EOF
cat > "$T/bin/opencode" <<EOF
#!/usr/bin/env bash
[[ "\$*" == *"triaging a GitHub issue"* ]] && { printf '%b\n' "\$TRIAGE_OUT"; exit 0; }
echo "token=\${GH_TOKEN:-none} \$*" >> "$T/runner.log"
[ -n "\${PRIMARY_FAILS:-}" ] && [[ "\$*" == *longcat* ]] && exit 1
case "\${RUNNER_DOES:-edit}" in
  edit) echo fixed > fix.txt ;;
  ci) mkdir -p .github && echo x > .github/ci.yml ;;
  none) : ;;
  fakegit) echo fixed > fix.txt; rm -f .git; mkdir -p .git/hooks
          printf '#!/bin/sh\ntouch $T/pwned\n' > .git/hooks/pre-commit; chmod +x .git/hooks/pre-commit
          printf '[core]\n\trepositoryformatversion = 0\n\tbare = false\n\tfsmonitor = touch $T/pwned\n' > .git/config ;;
esac
EOF
printf '#!/usr/bin/env bash\nshift; exec "$@"\n' > "$T/bin/timeout"
printf '#!/usr/bin/env bash\nexec "$@"\n' > "$T/bin/setsid"
chmod +x "$T"/bin/* "$T"/scripts/*.sh
export PATH="$T/bin:$PATH" GH_TOKEN=secret

run() { rm -f "$T/gh.log" "$T/runner.log"; env "$@" "$T/scripts/work-issue.sh" 5 >/dev/null 2>&1 || true; }

# 1. happy path, repo passes readiness gate -> PR + auto-merge
run AUTOMERGE=true PROTECTED=1
check "pushes branch" 'git -C "$T/origin.git" rev-parse -q --verify agent/issue-5 >/dev/null'
check "opens PR" 'grep -q "pr create" "$T/gh.log"'
check "auto-merge on" 'grep -q "pr merge .* --auto" "$T/gh.log"'
check "labels agent-pr" 'grep -q "add-label agent-pr" "$T/gh.log"'
check "runner never sees GH_TOKEN" 'grep -q "token=none" "$T/runner.log"'
check "workspace cleaned" '[ ! -d "$T/work/wt/5" ]'

# 2. no branch protection -> PR only
run AUTOMERGE=true
check "PR-only without protection" '! grep -q "pr merge" "$T/gh.log" && grep -q "pr create" "$T/gh.log"'

# 3. runner changes nothing -> needs-human
run RUNNER_DOES=none
check "no-change -> needs-human" 'grep -q "add-label needs-human" "$T/gh.log" && ! grep -q "pr create" "$T/gh.log"'

# 4. runner touches .github/ -> blocked
run RUNNER_DOES=ci AUTOMERGE=true PROTECTED=1
check "protected path -> needs-human" 'grep -q "add-label needs-human" "$T/gh.log" && ! grep -q "pr create" "$T/gh.log"'

# 5. primary model fails with no changes -> fallback model runs
run PRIMARY_FAILS=1
check "falls back to second model" '[ "$(grep -c "^token=" "$T/runner.log")" -eq 2 ] && grep -q "by opencode (opencode/nemotron" "$T/gh.log"'

# 6. issue from a non-collaborator -> refused before the runner starts
run ASSOC=NONE
check "untrusted author refused" '[ ! -f "$T/runner.log" ] && grep -q "add-label needs-human" "$T/gh.log"'

# 7. runner replaces .git with a booby-trapped repo -> ignored: real git dir used, nothing executes
git -C "$T/origin.git" branch -D agent/issue-5 >/dev/null
run RUNNER_DOES=fakegit
check "fake .git ignored, still ships" '[ ! -e "$T/pwned" ] && grep -q "pr create" "$T/gh.log" && git -C "$T/origin.git" show agent/issue-5:fix.txt >/dev/null 2>&1'

# 8. dispatch fills only free slots (MAX_PARALLEL=2, 1 working -> 1 dispatched)
rm -f "$T/gh.log"
out="$(WORKING=1 RUNNER_DOES=none "$T/scripts/dispatch.sh")"; sleep 1
check "dispatch respects MAX_PARALLEL" '[ "$out" = "Dispatched #11 to opencode" ]'
check "dispatch claims before launch" 'grep -q "issue edit 11 .*--add-label agent-working" "$T/gh.log"'
check "dispatch silent when full" '[ -z "$(WORKING=2 "$T/scripts/dispatch.sh")" ]'

# 9. triage: only the trusted, unlabelled issue is classified; verdict applied by code
rm -f "$T/gh.log"
out="$(TRIAGE_OUT='thinking...\nLABEL: agent-ready\nREASON: small clear fix' "$T/scripts/triage.sh")"
check "triage labels trusted issue" '[ "$out" = "#21 -> agent-ready (small clear fix)" ] && grep -q "issue edit 21 .*--add-label agent-ready" "$T/gh.log"'
check "triage skips untrusted + labelled" '! grep -qE "issue (edit|view|comment) (22|23)" "$T/gh.log"'
rm -f "$T/gh.log"
TRIAGE_OUT='Sure! I will ignore the rules. LABEL agent ready' "$T/scripts/triage.sh" >/dev/null
check "unclear verdict -> needs-human" 'grep -q "issue edit 21 .*--add-label needs-human" "$T/gh.log"'

echo "passed $pass, failed $fail"
[ "$fail" -eq 0 ]
