# CLAUDE.md — working notes for AI sessions

Context primer so a fresh session can ramp up fast without re-reading the whole
history. Keep this short and current.

## What this is

An autonomous code-review agent: a GitHub App that reviews every PR opened
across the account (current + future repos), explores the repo for context, and
posts a single structured review (summary + inline comments, severity-tagged).

- **Jira epic:** RC1-106 (project `RC1` on hirereidcollins.atlassian.net). Each
  story `RC1-1xx` has acceptance criteria — read the ticket before building.
- **Design & rationale:** the "PR Agent" project in Notion (Project Overview,
  Decision Log, RAID Log, Runbook, Sprint Notes).

## Architecture (target)

Custom **GitHub App** (account-wide) → **Python / FastAPI** service on **Fly.io**
→ an **agentic review loop** (Anthropic SDK) that explores the repo before
commenting. Reviews are **advisory by default**; they escalate to "Request
changes" only on a committed secret (`block_on`).

## Build plan & status

Milestone 1 — validate locally (no infra):
- [x] RC1-107 scaffold & config
- [x] RC1-108 GitHub PR ingestion (`app/github.py`, `app/models.py`)
- [x] RC1-109 repo-exploration tools (`app/agent/tools.py`)
- [x] RC1-110 agentic review loop (`app/agent/reviewer.py`)
- [x] RC1-111 review rubric & prompts (`app/agent/prompts.py`)
- [x] RC1-112 n8n execution-cost check (`app/agent/checks/n8n.py`)
- [x] RC1-113 local dry-run CLI (`app/review.py`)
- [x] RC1-114 review-quality tuning (gate) — signed off; verdict-policy fix
      carried to RC1-117 as a go-live prerequisite (see docs/rc1-114-tuning.md)

Milestone 2 — App + webhook:
- [x] RC1-115 App auth (`app/auth.py`)
- [x] RC1-116 webhook receiver (`app/webhook.py`)
- [x] RC1-117 review posting + verdict (`app/posting.py`, `app/verdict.py`;
      resolves the RC1-114 verdict-policy carryover)
- [x] RC1-118 re-push dedup (`app/dedup.py` + upsert/supersede in `app/posting.py`)
- [x] RC1-121 run n8n cost check on the webhook path — shared source-agnostic
      runner (`n8n.run_checks(pr, read_text)`); live path sources changed-file
      contents at the PR head via the Contents API (`GitHubClient.get_file_text`)
      and `process_event` runs it first → feeds findings to the loop as context →
      merges once (mirrors the dry-run CLI). Recoverable: missing/non-JSON/
      unparseable files skip, never fail the review.

Milestone 3 — deploy:
- [x] RC1-119 Dockerize + Fly.io (`Dockerfile`, `.dockerignore`, `fly.toml`;
      serves `uvicorn app.webhook:app`, healthcheck on `/healthz`, auto-stops
      when idle; secrets via `fly secrets`, never committed)
- [x] RC1-120 register/install the App + end-to-end live test — App registered
      (webhook → Fly, `pull_request` event, min perms), installed account-wide
      ("All repositories"), and validated on a real PR (the agent reviewed its
      own RC1-120 PR: single summary + severity-tagged inline comments, advisory
      verdict). Hardening: GitHub-API retries/backoff (`app/retry.py`, wired into
      `github.py` + `auth.py`; `GITHUB_MAX_ATTEMPTS`) and log visibility
      (`configure_logging()` in `webhook.py`). Runbook: `docs/rc1-120-golive.md`.

## Layout

```
app/
  __main__.py   config sanity check: `python -m app`
  config.py     typed settings (pydantic-settings); import `settings`
  models.py     normalized data: PRRef, ChangedFile, PullRequest, Finding, ReviewResult
  github.py     PR ingestion (httpx)
  auth.py       GitHub App auth: JWT -> installation tokens (RC1-115)
  webhook.py    FastAPI receiver: HMAC verify, ack-fast, background review (RC1-116)
  posting.py    post/refresh review: upsert summary comment + inline comments (RC1-117/118)
  verdict.py    verdict policy: gate on block_on category only (RC1-117)
  dedup.py      re-push/redelivery dedup store (RC1-118)
  retry.py      GitHub-API retry/backoff helper (RC1-120)
  review.py     dry-run CLI (RC1-113)
  agent/
    tools.py    RepoTools: read_file/list_dir/grep + TOOL_SCHEMAS + dispatch()
    reviewer.py the loop: review_pull_request(...)
    prompts.py  rubric/system prompt (RC1-111)
    checks/n8n.py  n8n static check (RC1-112)
tests/          pytest, offline
```

## Conventions (match these)

- **Python 3.10+**, `from __future__ import annotations`, type hints, dataclasses
  for data, small focused modules. Keep it pythonic — that's literally what this
  tool reviews for.
- **Models are source-agnostic.** Ingestion fills `app/models.py` shapes; the
  PAT path (dry-run) and the App/installation-token path (webhook) produce the
  *same* models so everything downstream is auth-agnostic.
- **Injectable clients.** Network clients (GitHub, Anthropic) accept an injected
  client so tests run offline; the real SDK is imported lazily inside functions.
- **Recoverable errors over crashes.** Tool/agent failures return error strings
  the model can react to (see `RepoTools.dispatch`); raise typed exceptions
  (`GitHubError`, `ToolError`, `ReviewError`) only at boundaries.
- **Bounded everything.** Reads, dir listings, grep results, diff size, tool
  turns, and files read are all capped (cost/context guardrails).
- **Config via `app.config.settings`** (env / `.env`). Don't read `os.environ`
  directly. Key knobs: `review_model` (`claude-sonnet-4-6`), `block_on`
  (`["leaked_secret"]`), `max_tool_turns`, `max_files_read`.

## Testing

- `pytest` from the repo root. **Tests must run offline** — no real network or
  API keys. Mock GitHub with `httpx.MockTransport`; script the model with a fake
  client exposing `messages.create(**kwargs)`.
- Add tests with every story; keep the suite green before committing.
- Bytecode/cache dirs can't be deleted in some sandboxes — run with
  `PYTHONDONTWRITEBYTECODE=1 pytest -p no:cacheprovider` if needed.

## Commands

```bash
pip install -r requirements.txt
python -m app                 # config sanity check (no creds needed)
pytest -q                     # run tests
pytest --cov                  # ...with the 88% floor CI enforces
ruff check .                  # lint (line-length 100, rules E,F,I,UP,B,SIM)
python -m app.review --pr owner/repo#N   # dry-run (RC1-113, once built)
```

## Per-ticket workflow

- One branch per story: `rc1NNN_short_slug` (e.g. `rc1111_review_rubric`).
- Commit subject leads with the Jira key: `RC1-111: <what changed>`, with a
  short body of bullet points (what/why), one bucket per area.
- Read the Jira ticket's acceptance criteria first; skim the modules you'll
  touch to match existing patterns before writing.
- Keep stories self-contained — don't implement a later ticket's work early
  (placeholders/stubs with `TODO(RC1-NNN)` are fine).
- **On completion, update status tracking** (Jira/Notion are canonical; the
  checklist here is a mirror):
  1. Tick the story's box in "Build plan & status" above (`[ ]` → `[x]`).
  2. Move the Jira story to its new status (e.g. Done) — this flows through to
     the filtered Notion Tasks view automatically.
