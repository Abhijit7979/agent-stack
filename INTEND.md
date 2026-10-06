# INTEND.md — Agentic Dev/Ops Stack

> North-star for every Claude session in this folder. Read it first. It says **why** and **what**, not how.
> How-to lives in `CLAUDE.md` and plans. If a task conflicts with this file, stop and ask.

## Goal

Get the output of an engineering team 3x our size with minimal overhead. We do it by building a thin
**glue layer on top of Hermes Agent**. Hermes triages GitHub Issues and hands the ready ones to a coding
agent. The coding agent ships the work through PRs and CI on its own.

Origin: the boss's directive for a lean agentic stack. Hermes handles orchestration and triage, and coding
agents do repo-level dev and tests.

## What we build (v1)

```
GitHub Issue ──► Hermes (triage) ──label──► agent-ready ──► OpenCode (fresh clone per issue)
                     │                                          │
                     └──► needs-human                           ▼
                                                   PR ──► CI green ──► merge ──► CI/CD deploy
```

1. **Intake:** GitHub Issues on one target repo.
2. **Triage:** a Hermes cron job sends each new issue to OpenCode for a verdict and labels it `agent-ready` or `needs-human` in code.
   - Humans can add or remove the label to override Hermes.
   - Only `agent-ready` issues are worked.
3. **Execution:** Hermes dispatches each `agent-ready` issue to **OpenCode** headless, in its own throwaway clone.
4. **Delivery:** OpenCode branches, commits, pushes and opens a PR that links the issue.
   - Once CI is green, the agent may merge.
   - The deploy then happens through the existing CI/CD pipeline.

## Principles

- **Extend, don't fork.** Use Hermes's existing pieces: skills, `delegate_task`, kanban, cron and GitHub support.
  - The `hermes-agent/` clone stays an untouched upstream copy.
  - Our code is config and skills that live outside it.
- **Swappable runner.** The coding agent is chosen by one config value, and so is the model.
  - Adding Claude Code or Codex later must be a config change plus a skill, not a rewrite.
- **Smallest thing that works.** No speculative abstractions, and nothing beyond the v1 scope below.
- **Everything leaves a trace.** Every agent action is visible in GitHub: labels, comments, PRs and commits.

## Stack (v1)

| Piece | Choice |
|---|---|
| Orchestrator / triage | Hermes Agent (`hermes-agent/`, upstream NousResearch), systemd service on our VPS |
| Coding agent | OpenCode (headless) |
| Model (all LLM work, through OpenCode) | Coding: `opencode/longcat-2.5-preview-free` (fallback `opencode/nemotron-3-ultra-free`). Triage: `opencode/nemotron-3.5-lightning-free`. Hermes itself needs no model (OpenCode free tier is OpenCode-only) |
| Intake | GitHub Issues |
| Identity | Dedicated GitHub bot account, fine-grained PAT scoped to the target repo |
| Isolation | One throwaway clone per issue; coding agent runs as a per-slot unprivileged unix user that can't read secrets or git metadata |
| Target repo | `Abhijit7979/testing-my-agent-layer` (private sandbox: Python + pytest + CI) |

The free preview model is accepted for the pilot only. Its data policy, rate limits and availability are
known risks.

## Autonomy and hard guardrails

Agents have **full autonomy**: they may push, open PRs, merge, deploy, run migrations and act on prod.
They may do so **only inside these rules**, which are never relaxed without a human editing this file:

1. **Merge only when CI passes.** Branch protection is on for `main`, and agents never bypass it or force-push.
2. **Deploy only through the CI/CD pipeline.** Agents never get shell or SSH access to prod.
3. **Secrets are never read by agents.**
   - The pipeline injects them at deploy time.
   - Agents never print, log or commit them.
4. **Migrations must be reversible.** Every migration needs a working down/rollback step.
5. **Every merge can be reverted with one command.** That means a single `git revert` of the merge commit or a pipeline rollback.
6. **Limits:** at most **2 tasks in parallel** and at most **30 minutes per task**.
   - When a task hits a limit, it stops.
   - The agent comments on the issue and relabels it `needs-human`.

**Readiness gate.** Full autonomy applies to a repo only if all three of these hold:
- the repo's CI runs real tests;
- deploys already go through a pipeline;
- branch protection is enabled.

If any one is missing, the agent runs **PR-only** on that repo: it opens PRs and a human merges them.

## Success metrics (2-week pilot)

| Metric | Target |
|---|---|
| Agent PRs merged per week | ≥ 5 |
| `agent-ready` issues merged with zero human code edits | ≥ 50% |
| Human spot-check review time per PR (after merge) | ≤ 15 min |
| Revert rate of agent merges | ≤ 10%. Above this, drop to PR-only mode |

## v1 done when

An `agent-ready` issue on the target repo runs end to end with no human touching the code:
issue → Hermes triage → OpenCode → PR → CI green → merge → pipeline deploy.

Deadline: 2 weeks from the start, with the metrics above being tracked.

## Not in v1 (later, do not build now)

- Slack intake. Hermes has a Slack platform plugin, so this will be wired in later.
- Claude Code as a coding agent, for deep repo work and refactors.
- ChatGPT Pro / Codex for fast background code and tests.
- More repos, moving to a GitHub App, and Docker isolation per task.
- A money budget and cost tracking. These come in when paid models arrive.
- A Jira or Linear bridge.

## Open items

- [x] Name the target repo and confirm it passes the readiness gate (CI `test` required, auto-merge on).
- [x] Create the bot account and token (classic, `repo` scope only), and set up branch protection.
- [x] Local Docker run end to end: issue #1 → triage → OpenCode → PR #2 → CI green → auto-merged (2026-10-06).
- [ ] Provision Hermes and OpenCode on the VPS (same Docker setup).
