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
→ one **review pipeline** (Anthropic SDK; `app/agent/pipeline.py`): Python
runs the deterministic checks (n8n workflow cost) and gathers the repository
context (conventions file, callers, tests, by grep), three evidence-scoped
reviewers fan out on one cached prefix, Python merges, the verifier judges
the model's findings, and the pipeline hands back the one complete result
(RC1-425). No model explores: the scout that did was measured and retired
(RC1-427). Reviews are **advisory by default**; they escalate to "Request
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
      runner (`n8n.run_checks(pr, read_text)`); the live path sourced changed
      files at the PR head via the Contents API and `process_event` ran it,
      fed the findings to the loop as context and merged once, mirroring the
      CLI — until RC1-425 moved all of that into the pipeline. Recoverable:
      missing/non-JSON/unparseable files skip, never fail the review.

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

Multi-agent review (RC1-387 → RC1-390 → RC1-391; see the Jira tickets):
- [x] RC1-387 verifier pass + precision cases + baseline corpus run
      (`app/agent/verifier.py`, `REVIEW_VERIFY_FINDINGS`; decision record
      with the flag-off/flag-on numbers in `docs/rc1-387-verifier.md`)
- [x] RC1-390 scout + three evidence-scoped reviewers + Python router
      (`app/agent/router.py`, `scout.py`, `pipeline.py` — then `multi.py`
      behind `REVIEW_MULTI_AGENT`; numbers and the decision in
      `docs/rc1-390-multi-agent.md`)
- [x] RC1-393 cheap exploration: conventions file + callers by grep into the
      shared prefix by Python, scout cap follows the context
      (`app/agent/context.py`, `router.scout_turns`; `REVIEW_SCOUT_CONTEXT_TURNS`;
      corpus run with `--repo-path` and the decision in
      `docs/rc1-393-cheap-exploration.md`)
- [x] RC1-395 cost per review in Datadog: priced at the end of the trace from
      the token counts the result already carries, one `pr_agent.review.cost_usd`
      point per review from the webhook, widget + p95 monitor as code in the
      platform (`app/pricing.py`, `observability.annotate_review_cost` /
      `ship_review_metrics`; record in `docs/rc1-395-cost-per-review.md`)
- [x] RC1-394 tests for the changed paths by Python; a complete context
      (conventions + callers + tests) skips the scout by default
      (`context.tests_for`, `router.scout_turns`; `REVIEW_SCOUT_COMPLETE_TURNS`,
      default 0); the run-C guard on every reviewer and the multi path's
      verifier; the `pr_review` span tagged repo/pr/head_sha and a last-reviews
      list on the fleet dashboard (record in `docs/rc1-394-tests-by-python.md`)
- [x] RC1-391 spike: the same graph on LangGraph, measured against `multi.py`
      on the corpus and live PRs; the comparison and the "when I would reach
      for this" answer in `docs/rc1-391-langgraph-spike.md`. Live-PR pricing
      harness: `scripts/measure_pr.py`. The port itself was removed in
      RC1-421 once the question was answered; asyncio is the one orchestrator.
- [x] RC1-396 conventions files in the two n8n repositories so their reviews
      skip the scout; `measure_pr.py --repo-dir/--overlay` measures a file
      against a PR that predates it (before/after in
      `docs/rc1-396-conventions-files.md`)
- [x] RC1-421 LangGraph spike removed; asyncio is the one orchestrator.
- [x] RC1-422 the pipeline is the one production path: the single loop,
      `REVIEW_MULTI_AGENT` and `MAX_TOOL_TURNS` are gone, `multi.py` is
      `pipeline.py`, `review_pull_request` lives there and opens the
      `pr_review` span itself; corpus and live-PR numbers against the
      recorded band in `docs/rc1-422-single-pipeline.md`
- [x] RC1-427 the scout measured on top of the Python context (corpus and
      the reference PRs, four arms) and removed: no defect found that the
      review otherwise missed, noise wherever it ran; `scout.py`, its prompt,
      settings, router branch, result fields, pricing stage, metric tag and
      the model-tool surface in `tools.py` are gone; the reviewers' prompt
      no longer mentions a brief (record in `docs/rc1-427-scout-value.md`)
- [x] RC1-398 the verifier's category tie-break, measured before any
      cross-category dedupe rule: boundary cases in `evals/boundary.py`
      (one defect, two categories, the pair of findings), scoring in
      `evals/tiebreak.py`, `scripts/measure_tiebreak.py history|probe|pipeline`
      (decision in `docs/rc1-398-category-tiebreak.md`)
- [x] RC1-428 the verifier is a permanent stage (RC1-400 folded in): the
      corpus both ways, the three reference PRs both ways and the boundary
      probe against five thresholds declared first; `REVIEW_VERIFY_FINDINGS`,
      `REVIEW_VERIFY_MODEL`, the `verify` switch and `measure_pr.py --verify`
      are gone, the fold sentence and the absence rule are the one
      instruction text; a tests-list cap bug that produced false "no tests"
      warnings fixed in `context.py` (record in `docs/rc1-428-verifier-policy.md`)
- [x] RC1-424 one repository contract: `RepositoryAccess` (`explorable`,
      `read_text`, `grep`, `paths`) in `app/agent/repository.py` with the
      shared guards; `LocalRepository` and `GitHubRepository` are its two
      adapters and pass one contract test; the pipeline is typed against the
      protocol; the model-facing `read_file` / `list_dir` are gone; a spent
      API budget is "not searched", never "no matches"
      (record in `docs/rc1-424-repository-access.md`)
- [x] RC1-425 the pipeline owns the deterministic checks and the result:
      `app/agent/checks` is the registry (`Check`, `CHECKS`) and the stage
      (`run_deterministic_checks`, reading changed files whole through the
      repository contract's new `max_bytes`); the pipeline runs it first,
      puts the findings in the prefix as already-recorded, verifies only the
      model's findings and appends the checks' once (`checks_run`,
      `checks_failed`, `deterministic_findings` on the result); the webhook
      loads and publishes, the CLI loads and prints, `precomputed_findings`
      is gone (record in `docs/rc1-425-deterministic-checks.md`)

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
  pricing.py    RC1-395: model prices + cache rates, a copy of the eval harness's
                (the image cannot import it); review_cost() prices a ReviewResult
  observability.py  LLM Obs enable + spans (RC1-322/390); cost per review onto the
                workflow span and the per-review metric (RC1-395)
  agent/
    repository.py  RC1-424: RepositoryAccess (explorable/read_text/grep/paths), RepositoryError,
                the shared guards (secret, lock, noise, caps) both adapters use
    local_repository.py   LocalRepository: the contract from a checkout on disk (RC1-109)
    github_repository.py  GitHubRepository: the contract from Trees + Contents at the PR
                head, under the per-review API budget (RC1-364)
    pipeline.py the review: review_pull_request(...) — checks -> context -> warm cache
                -> reviewers (gather) -> merge -> verifier -> assemble; opens the
                pr_review span (RC1-390/422/427/428/425)
    reviewer.py model-facing primitives every stage shares: render_pr, the cache
                marker + system block, _tokens, parse_findings (RC1-110/422/427)
    verifier.py second pass over the merged findings: drop, downgrade, fold one
                defect under two categories; a stage, not a flag (RC1-387/428)
    router.py   RC1-390: which reviewers run, decided from the file list, and
                whether there is a repository to gather context from
    context.py  RC1-393/394: conventions file + callers + tests by grep, Python only,
                into the prefix; `complete` when all three are answered
    prompts.py  rubric/system prompt (RC1-111); reviewer specs (RC1-390)
    checks/     RC1-425: Check + CHECKS registry and run_deterministic_checks, the
                pipeline's first stage; checks/n8n.py is the n8n static check (RC1-112)
tests/          pytest, offline
```

## Conventions (match these)

- **Python 3.10+**, `from __future__ import annotations`, type hints, dataclasses
  for data, small focused modules. Keep it pythonic — that's literally what this
  tool reviews for.
- **Models are source-agnostic.** Ingestion fills `app/models.py` shapes; the
  read-only path (dry-run: the gh CLI's token, else the PAT; RC1-430) and the
  App/installation-token path (webhook) produce the
  *same* models so everything downstream is auth-agnostic.
- **Injectable clients.** Network clients (GitHub, Anthropic) accept an injected
  client so tests run offline; the real SDK is imported lazily inside functions.
- **Recoverable errors over crashes.** A repository read that cannot be
  answered is `None`; a search that cannot run raises `RepositoryError`, which
  context gathering records as "not searched" and tells the reviewers. Raise
  typed exceptions (`GitHubError`, `RepositoryError`, `ReviewError`) only at
  boundaries.
- **One repository contract.** The pipeline and `context.py` are typed against
  `RepositoryAccess`, never an adapter; the guards live in `repository.py`, so
  what a review may see is decided once. A new backend passes
  `tests/test_repository_contract.py` before anything else.
- **Bounded everything.** Reads, grep results, diff size, and API calls per
  live review are all capped (cost/context guardrails).
- **The pipeline assembles the result.** Deterministic checks run inside
  `review_pull_request`, first; their findings are in the prefix as
  already-recorded and in the result exactly once, after the verifier. A
  caller never runs a check or merges a finding; a new check is one entry in
  `app/agent/checks.CHECKS`.
- **Config via `app.config.settings`** (env / `.env`). Don't read `os.environ`
  directly. Key knobs: `review_model` (`claude-sonnet-4-6`), `block_on`
  (`["leaked_secret"]`), `remote_api_budget`
  (the Contents/Trees calls one live review may spend).
- **One pipeline; the request shape is measured, not assumed.** Any change to
  what the reviewers or the verifier send (prefix, tools,
  `tool_choice`, cache markers) needs a corpus run (`python -m evals
  --repo-path .`) against the band in `docs/rc1-422-single-pipeline.md`, and
  a `scripts/measure_pr.py` run on the three reference PRs for cost.

## Testing

- `pytest` from the repo root. **Tests must run offline** — no real network or
  API keys. Mock GitHub with `httpx.MockTransport`; script the model with a fake
  client exposing `messages.create(**kwargs)`.
- Add tests with every story; keep the suite green before committing.
- Bytecode/cache dirs can't be deleted in some sandboxes — run with
  `PYTHONDONTWRITEBYTECODE=1 pytest -p no:cacheprovider` if needed.

## Commands

```bash
pip install -r requirements-dev.txt   # dev setup (the image installs runtime-only requirements.txt)
python -m app                 # config sanity check (no creds needed)
pytest -q                     # run tests
pytest --cov                  # ...with the 88% floor CI enforces
ruff check .                  # lint (line-length 100, rules E,F,I,UP,B,SIM)
python -m evals --list        # the planted-defect corpus (free)
python -m evals               # run it (BILLED — needs ANTHROPIC_API_KEY)
python -m evals --repo-path .  # ...with a checkout every case greps for context (RC1-393)
python -m app.review --pr owner/repo#N   # dry-run (RC1-113, once built)
python scripts/measure_pr.py 35 33 39
                               # price reviews of real PRs at their own head (BILLED; RC1-391)
python scripts/measure_pr.py 8 --repo-dir ../n8n-concert-intelligence --overlay CLAUDE.md
                               # ...another repo's PR, with a working-tree file laid over the head (RC1-396)
PYTHONPATH=. python scripts/measure_tiebreak.py history    # the eval store's on-plant survivors (free; RC1-398)
PYTHONPATH=. python scripts/measure_tiebreak.py probe --runs 5     # BILLED: the verifier over each boundary pair, both orders
PYTHONPATH=. python scripts/measure_tiebreak.py pipeline --runs 3  # BILLED: whole multi-agent reviews of the boundary cases
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
