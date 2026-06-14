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
cp .env.example .env          # then fill in ANTHROPIC_API_KEY + GITHUB_TOKEN
python -m app                 # config sanity check — no credentials required
pytest -q                     # run the test suite
```

`python -m app` prints which credentials are set (never their values) and the
active review settings, then confirms the config loads.

## Dry-run a review (RC1-113)

Run the full review against a real PR and print the would-be review to your
terminal — nothing is posted to GitHub. This is the tool used to tune review
quality before the App and hosting exist. It needs `ANTHROPIC_API_KEY` (the
review loop) and `GITHUB_TOKEN` (a PAT with read access to the repo).

```bash
python -m app.review --pr owner/repo#123
python -m app.review --pr owner/repo#123 --repo-path /path/to/local/clone
```

The pipeline is ingest → agentic review loop + rubric → n8n execution-cost
check → formatted output. Pass `--repo-path` pointing at a local checkout so the
agent can explore the repo's files for context; without it the review runs from
the diff alone. Use `--model` to override the review model for a run.

Output leads with the overall summary, then one block per finding — severity,
`file:line`, category, message, and a suggested fix — followed by totals and
run metadata. The exit code is CI-friendly:

| Code | Meaning |
| --- | --- |
| `0` | Review ran; no blocking finding (advisory) |
| `1` | Review ran; a `block_on` finding was present |
| `2` | The review could not be produced (e.g. bad PR spec, ingestion error) |

## Webhook service (RC1-116)

The live path is a FastAPI app that GitHub POSTs events to. It verifies the
delivery's HMAC signature, acknowledges within GitHub's ~10s window, and runs the
review on a background task. It handles `pull_request` `opened` / `synchronize` /
`reopened`; everything else is acked and ignored. Needs `GITHUB_WEBHOOK_SECRET`
(and, for the background review, the App credentials + `ANTHROPIC_API_KEY`).

```bash
uvicorn app.webhook:app --port 8000     # GET /healthz, POST /webhook
```

Posting the review back to the PR is wired up in RC1-117; today the background
worker runs the review and logs the outcome.

## Layout

```
app/
  __main__.py          # `python -m app` config sanity check (RC1-107)
  config.py            # typed settings via pydantic-settings (RC1-107)
  github.py            # PR ingestion: diff + metadata (RC1-108)
  auth.py              # GitHub App auth: JWT -> installation tokens (RC1-115)
  webhook.py           # FastAPI webhook receiver: HMAC verify + async review (RC1-116)
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
| `GITHUB_APP_ID` / `GITHUB_APP_PRIVATE_KEY` | GitHub App auth for the live service (RC1-115) | — |
| `GITHUB_WEBHOOK_SECRET` | HMAC secret for verifying webhook deliveries (RC1-116) | — |

## Build plan (RC1-106)

**Milestone 1 — validate locally (no infra):** RC1-107 scaffold · RC1-108
ingestion · RC1-109 tools · RC1-110 loop · RC1-111 rubric · RC1-112 n8n check ·
RC1-113 dry-run CLI · RC1-114 quality tuning (gate).

**Milestone 2 — GitHub App + webhook:** RC1-115 App auth · RC1-116 webhook
receiver · RC1-117 review posting + verdict policy · RC1-118 re-push dedup.

**Milestone 3 — deploy:** RC1-119 Dockerize + Fly.io · RC1-120 register/install
the App + end-to-end live test.
