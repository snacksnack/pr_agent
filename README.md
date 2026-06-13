# PR Review Agent

An autonomous code-review agent that reviews every pull request opened across a
GitHub account — current repos and any created in the future. On each PR it
explores the repository for context and posts a single structured review
(summary + inline comments, severity-tagged) covering convention consistency,
pythonic-ness, security, n8n workflow execution cost, test coverage, dependency
risk, and PR-description-vs-diff drift.

- **Jira epic:** [RC1-106](https://hirereidcollins.atlassian.net/browse/RC1-106)
- **Design & decisions:** the "PR Agent" project in Notion (Project Overview,
  Decision Log, RAID Log, Runbook, Sprint Notes)

## Architecture (target)

Custom **GitHub App** (installed account-wide — all repos, including future) →
**Python / FastAPI** service on **Fly.io** → an **agentic review loop** that
explores the repo before commenting. Reviews are **advisory by default** and
escalate to "Request changes" only on a committed secret.

## Status

This is the **RC1-107** scaffold: project layout, configuration, and runnable
skeleton. The review logic is built out across the stories below.

## Getting started

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in values when you reach RC1-113
python -m app                 # config sanity check — no credentials required
pytest -q                     # run the test suite
```

`python -m app` prints which credentials are set (never their values) and the
active review settings, then confirms the config loads.

## Layout

```
app/
  __main__.py          # `python -m app` config sanity check (RC1-107)
  config.py            # typed settings via pydantic-settings (RC1-107)
  github.py            # PR ingestion: diff + metadata (RC1-108)
  review.py            # local dry-run CLI: `python -m app.review` (RC1-113)
  agent/
    tools.py           # repo-exploration tools: read / list / grep (RC1-109)
    reviewer.py        # the agentic review loop (RC1-110)
    prompts.py         # review rubric + structured-output schema (RC1-111)
    checks/
      n8n.py           # n8n execution-cost static check (RC1-112)
tests/                 # pytest suite
```

## Configuration

All settings load from environment variables (and an optional `.env`). See
`.env.example` for the full list. Key ones:

| Variable | Purpose | Default |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | Review agent loop | — |
| `GITHUB_TOKEN` | PAT for the dry-run CLI (RC1-113) | — |
| `REVIEW_MODEL` | Workhorse review model | `claude-sonnet-4-6` |
| `REVIEW_BLOCK_ON` | Categories that block a merge (CSV; empty = advisory only) | `leaked_secret` |
| `MAX_TOOL_TURNS` / `MAX_FILES_READ` | Agent-loop guardrails | `20` / `40` |

## Build plan (RC1-106)

**Milestone 1 — validate locally (no infra):** RC1-107 scaffold · RC1-108
ingestion · RC1-109 tools · RC1-110 loop · RC1-111 rubric · RC1-112 n8n check ·
RC1-113 dry-run CLI · RC1-114 quality tuning (gate).

**Milestone 2 — GitHub App + webhook:** RC1-115 App auth · RC1-116 webhook
receiver · RC1-117 review posting + verdict policy · RC1-118 re-push dedup.

**Milestone 3 — deploy:** RC1-119 Dockerize + Fly.io · RC1-120 register/install
the App + end-to-end live test.
