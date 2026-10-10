# CLAUDE.md

**Read `INTEND.md` first.** It is the goal, the scope and the guardrails for this project. This file covers how to work here.
If they conflict, INTEND.md wins. Stop and ask.

## Layout

```
hermes_agent/
├── INTEND.md        # north-star (what/why) — edit only with the user's agreement
├── CLAUDE.md        # this file (how)
├── hermes-agent/    # upstream NousResearch clone — READ-ONLY, never edit
└── stack/                       # our glue layer
    ├── config/stack.env         # THE config: repo, runner, models, limits, merge mode
    ├── scripts/triage.sh        # no-agent cron job: OpenCode returns a verdict, script applies the label
    ├── scripts/dispatch.sh      # no-agent cron job: agent-ready → agent-working, launches workers
    ├── scripts/work-issue.sh    # one issue: fresh clone → runner → PR → auto-merge (guardrails live here)
    ├── deploy/Dockerfile        # official Hermes image + gh, opencode, runner1/runner2/triager users
    ├── deploy/docker-compose.yml# local stand-in for the VPS (token in gitignored deploy/.env)
    ├── deploy/install.sh        # install into HERMES_HOME: scripts, labels, cron jobs (run by start.sh on boot)
    └── tests/test_pipeline.sh   # offline check, stubbed gh/opencode — run after any script change
```

**Flow:** Hermes cron `agent-triage` (every 10m) runs `triage.sh`. It asks OpenCode (`TRIAGE_MODEL`)
for a verdict and applies the label in code. Hermes cron `agent-dispatch` (every 5m) starts
`work-issue.sh` on up to `MAX_PARALLEL` ready issues. Both are no-agent jobs. Hermes needs no model of
its own, because OpenCode's free tier only works from inside OpenCode.

**Isolation:** each coding task runs as its slot's unix user (`runner1`/`runner2`), and triage runs
as `triager`. None of these users can read `HERMES_HOME`, which holds `GH_TOKEN`, or the git dir.
After every run, all their processes are killed and their home directories are wiped.
GitHub labels are the only state: `agent-ready → agent-working → agent-pr | needs-human`.

**Why cron + scripts and not Hermes kanban:** kanban workers are Hermes profiles. An external CLI lane
(OpenCode, Codex or Claude Code) is "not yet a paved path" upstream (`kanban-worker-lanes.md`). Keeping
the git, PR and merge steps in a deterministic script also means the guardrails are code, not prompts.

**Test:** `bash stack/tests/test_pipeline.sh`. It must print `failed 0`.

- **Never modify `hermes-agent/`.** Read it to learn how Hermes works.
  - Its own rules are in `hermes-agent/AGENTS.md`.
  - If upstream really has to change, write a patch proposal and ask first.
- Keep our code in `stack/`, and keep it small: config and skills first, scripts only when config can't do the job.

## Reuse before building

Hermes already ships most of what we need. Look there before writing anything:

| Need | Where in `hermes-agent/` |
|---|---|
| Run OpenCode / Claude Code / Codex | `skills/autonomous-ai-agents/{opencode,claude-code,codex}/SKILL.md` |
| Subagent delegation | `tools/delegate_tool*.py`, `tools/async_delegation.py` (`acp_command` for external agents) |
| Task board / dispatch | `hermes_cli/kanban*.py`, `tools/kanban_tools.py`, `gateway/kanban_watchers*.py` |
| Scheduling / polling | `cron/`, `tools/cronjob_tools.py` |
| GitHub | `skills/software-development/github/`, `hermes_cli/github_api.py` |
| Fallback model | `hermes fallback` |
| Config defaults | `hermes_cli/config_defaults.py`, `cli-config.yaml.example`; user config is `~/.hermes/config.yaml` |

## Key commands

- Run OpenCode headless in a checkout:
  `opencode run --dir <checkout> -m opencode/longcat-2.5-preview-free "<task>"`
- Local stack: `cd stack/deploy && docker compose up -d --build` (`start.sh` runs `install.sh` on every boot)
- The model is `opencode/longcat-2.5-preview-free`, and the fallback is `opencode/nemotron-3-ultra-free`.
  - Both are set in **one place** in `stack/config/`.
  - Never hardcode the model name anywhere else.

## Rules when building

- **Use one config value for the runner.** OpenCode is the v1 runner. Claude Code and Codex must be addable later with a config change plus a skill.
- **Never write anything that weakens the guardrails in INTEND.md.** That includes:
  - bypassing CI or branch protection;
  - force-pushing;
  - giving the agent shell or SSH access to prod;
  - reading or logging secrets;
  - running a migration with no rollback.
- **No secrets in this folder.**
  - Tokens such as the GitHub bot PAT and OpenCode auth live in env or `~/.hermes/.env` on the VPS.
  - Don't read or print `~/.hermes/.env` or `auth.json`.
- **Keep the agent's trail in GitHub.** Every state change appears as an issue label, a comment or a PR.
- **Limits:** at most 2 tasks in parallel and at most 30 minutes per task. On timeout or failure, comment on the issue and relabel it `needs-human`.
- **Prove it, don't claim it.** Before calling something done:
  - show it running on a real or test issue;
  - make sure any non-trivial logic leaves one runnable check behind.
- **Don't build anything from INTEND.md's "Not in v1" list.**

## Workflow

1. Check the task against INTEND.md's scope.
2. Find what Hermes already provides, using the reuse table above.
3. Write the smallest change in `stack/`.
4. Verify it end to end.
5. Update INTEND.md's **Open items** checklist when an item is closed.
