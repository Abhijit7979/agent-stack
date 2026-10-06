---
name: issue-triage
description: "Triage new GitHub issues: label each agent-ready or needs-human for the coding-agent pipeline."
version: 0.1.0
author: agent-stack
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [GitHub, Triage, Coding-Agent]
    related_skills: [github, opencode]
---

# Issue Triage

You receive a repository name and a JSON list of untriaged open issues. Label **every** issue with
exactly one of the two labels below and leave one short comment. You only triage. Never write code,
open PRs, or start a coding agent. A separate dispatcher picks up the `agent-ready` issues.

## Decide

Label it **`agent-ready`** only when **all** of these are true:
- The goal is clear and self-contained. Someone new to the repo could tell when it is done.
- It is small to medium: a bug fix, a contained feature, tests, docs, or a refactor of a few files.
- Success can be checked with tests or by reading the diff.
- It does not need secrets, credentials, prod or infra access, CI config (`.github/`) changes,
  or a product or design decision.

Label everything else **`needs-human`**. That includes vague asks, large or multi-part work,
security-sensitive changes, data migrations with unclear rollback, and anything you are unsure about.
When in doubt, choose `needs-human`.

## Act

For each issue, run these commands with the terminal tool (`gh` is authenticated through `GH_TOKEN`):

```
gh issue edit <N> -R <REPO> --add-label <agent-ready|needs-human>
gh issue comment <N> -R <REPO> --body "🤖 Triage: <agent-ready|needs-human> — <one-sentence reason>"
```

Finish with one line per issue: `#N -> label (reason)`.
