# RC1-114 — Review-quality tuning pass

Milestone 1 go/no-go gate. Run the dry-run (`python -m app.review`) against
several real PRs, judge the output, and tune prompts / rubric / thresholds
until reviews are sharp and low-noise. This doc is the record of that pass and
the home of the final sign-off.

- **Ticket:** [RC1-114](https://hirereidcollins.atlassian.net/browse/RC1-114)
- **Depends on:** RC1-112 (n8n check) — done, merged in PR #7.

## How runs are done

Runs happen on a local machine (the dry-run hits `api.github.com` for PR
ingestion and `api.anthropic.com` for the review loop; both need real
credentials in `.env`). For each PR, point `--repo-path` at a local checkout so
the agent can explore files for grounded convention/context findings.

```bash
python -m app.review --pr <owner/repo#N> --repo-path /path/to/local/clone
```

Capture the full terminal output for each run, then record below: finding
count, any false positives (flagged but shouldn't be) or false negatives
(missed real issues), and whether each severity feels calibrated.

## PR set

Aiming for 3–5 representative PRs across repo types.

| # | PR | Type | Why it's in the set | Status |
|---|----|------|---------------------|--------|
| 1 | `snacksnack/ai-incident-summarizer#12` | pure-Python | Real Python change; baseline for Python rubric dimensions | ✅ run — advisory, 4 warn / 3 nit |
| 2 | `snacksnack/n8n-stakeholder-status-email#1` | n8n workflow | Exercises the RC1-112 check; planted 3 costly patterns + 2 benign nodes | ✅ run x2 — fixes validated (Run 2) |
| 3 | _TBD_ | _TBD_ | dependency/config or larger refactor — TBD | ☐ |
| 4 | _TBD_ | _TBD_ | optional | ☐ |
| 5 | _TBD_ | _TBD_ | optional | ☐ |

### Expected ground truth — PR #2 (n8n)

The deterministic check should flag exactly these three, and leave the HTTP
Request and Slack nodes alone (false-positive guard):

| Node | Pattern | Expect |
|------|---------|--------|
| Every Minute (`scheduleTrigger`) | runs every 1 minute | warning |
| Loop Over Incidents (`splitInBatches`) | batch size 0 (unbounded) | warning |
| Summarize Incident (`executeWorkflow`) | per-item fan-out, no execute-once | warning |
| Fetch Open Incidents (`httpRequest`) | normal | (no finding) |
| Post Summary to Slack (`slack`) | normal | (no finding) |

## Run log

One entry per run. Paste the output, then note the assessment.

### Run 1 — `snacksnack/n8n-stakeholder-status-email#1` — 2026-06-13

Model `claude-sonnet-4-6`, `--repo-path` to local clone. turns=5, files_read=6.
**Totals: 1 blocker, 9 warnings, 2 nits. Verdict: BLOCK (exit 1).**

Key outcomes (full key redacted from this doc):

- **BLOCKER (security) — FALSE POSITIVE.** The agent reported an Anthropic key +
  Gmail "committed in the repository" and advised scrubbing history. Verified
  otherwise: `git log --all -- .env` is empty, `.env` is untracked and gitignored
  (`.gitignore:1`). It's a purely local working-tree file, never committed or
  pushed — no exposure. The agent's repo-exploration tools read the local
  working tree (incl. gitignored files), and it asserted "committed" without
  checking git tracking. Two problems: (1) a false *blocker* that gates a merge —
  the worst FP class; (2) a real local secret was pulled into the model context
  and a prefix echoed into output. (Key was independently rotated weeks ago, so
  zero live impact.) → fix: make repo tools gitignore-aware (see Tuning changes).
- **n8n ground truth matched:** all 3 planted patterns flagged (every-minute
  schedule, `batchSize: 0` loop, per-item sub-workflow fan-out); HTTP Request
  and Slack nodes correctly NOT flagged. ✅
- **Grounded convention findings:** file belongs in `workflows/`; repo uses an
  HTTP-Request Slack-webhook pattern (per README known-issue) not the native
  Slack node; compared against the existing weekly-cron workflow. Impressive,
  repo-aware — these are true positives.

**Assessment**

- False positives: **one serious** — the security blocker (untracked, gitignored
  local `.env` mis-reported as committed; see above). The model findings
  (`executeWorkflow` workflowId-as-name, hardcoded placeholder URL) are fair.
- False negatives (missed): none notable.
- Severity calibration: feels right — single blocker reserved for the secret;
  cost/convention issues as warnings; docs/url as nits.
- **Noise — duplication (main tuning signal):** the model re-stated the
  every-minute schedule (`:8`) and `batchSize: 0` (`:31`) that the
  deterministic n8n check already reported → same issue listed twice, count
  inflated 12 → ~10. See Tuning changes.

<!-- copy this block per run -->

### Run 2 — `snacksnack/n8n-stakeholder-status-email#1` — 2026-06-13 (after fixes)

Re-run after the dedup refactor + secret-file guard. turns=11, files_read=6.
**Totals: 1 blocker, 9 warnings, 1 nit.**

Both fixes confirmed on the live model:

- **No false-positive secret blocker.** The local untracked `.env` is absent from
  the review entirely — the secret-file guard worked. ✅
- **No n8n duplication.** The 3 deterministic findings (every-minute schedule,
  `batchSize:0`, fan-out) each appear exactly once; the model deferred to the
  static check instead of restating (was 2 dups in Run 1). ✅
- **Model still adds genuine value.** Placeholder URL, missing dedup logic,
  `workflowId`-as-name, native-Slack-vs-webhook convention, no error handling,
  file location, README — all real, none mechanical-cost duplicates. ✅

**New finding — verdict policy (not a review-quality issue):** the BLOCK verdict
now comes from a model-assigned `blocker` *severity* on a non-secret correctness
issue (placeholder URL, `:22`), not a committed secret. `blocking_findings()`
gates on `category in block_on OR severity == 'blocker'`, and `block_on`
(`leaked_secret`) matches no real category — so in practice ANY blocker-severity
finding gates, contradicting the "advisory by default; block only on a committed
secret" design (CLAUDE.md/README). The model's severity is defensible per the
rubric (a workflow that fails every run is a real correctness defect); the
mismatch is in the *verdict policy*. This is RC1-117's remit ("review posting +
verdict") — see Open questions; flag as a hard prerequisite before going live.

### Run 3 — `snacksnack/ai-incident-summarizer#12` (pure-Python) — 2026-06-13

DynamoDB seed-script PR. turns=20 (hit cap), files_read=15.
**Totals: 0 blocker, 4 warnings, 3 nits. Verdict: advisory (exit 0).** ✅

Strong on regular Python:

- **Correct advisory verdict** — no secret, nothing raised to blocker, no false
  gate. Confirms the Run 2 over-block is specific to non-secret blocker-severity
  findings, not general.
- **Accurate, specific, grounded findings:** README pip-install drift,
  module-level `sys.exit()` running at import, unconditional `requests` import,
  and a genuine TTL-from-`created_at`-vs-now logic bug (deep reasoning).
  Severities feel right.
- **Minor notes:** (1) slight redundancy — the top-level `requests` import is
  raised both as a `dependencies` warning and a `convention` nit (same line, two
  angles). (2) Review **truncated** at the 20-turn cap (15 files) for a 2-file
  PR — solid review anyway, but worth watching whether budgets are tight / the
  agent over-explores. Neither is a quality blocker.

No false positives. This is the pure-Python coverage the gate requires.

## Tuning changes

Log every prompt / rubric / threshold change made during the pass, with the
before/after behaviour that motivated it (so the gate is auditable).

| Change | File | Before → after | Motivated by |
|--------|------|----------------|--------------|
| **Applied (pending validation run).** Make deterministic checks the single source of truth: run them first and feed their findings into the review loop as already-recorded context, so the model builds on them instead of re-deriving them. Dedup happens at the source, not via fuzzy post-matching. | `app/agent/prompts.py` (new `format_precomputed_findings`; rubric dim 10 now defers mechanical cost patterns), `app/agent/reviewer.py` (`format_pr_for_review` / `review_pull_request` take `precomputed_findings`), `app/review.py` (compute n8n findings first → pass as context → merge once) | duplicate n8n findings appeared once from the static check and again from the model | Run 1 duplication (`:8`, `:31`) |

**Validation:** re-run PR #2 and confirm (a) the 3 n8n warnings still appear exactly once each, (b) total drops from 12 toward ~9–10, (c) the model still adds its non-mechanical n8n findings (workflow-id placeholder, error handling, Slack-webhook convention). Covered offline by `tests/` (96 passing); the live re-run confirms model behavior.

| **Applied — secret-file guard on repo tools.** `RepoTools` now refuses to read/grep/list secret & credential files (`.env*`, `*.pem`/`*.key`/`*.p12`/`*.pfx`, `id_rsa`/`id_dsa`/`id_ecdsa`/`id_ed25519`, `credentials*`, `.npmrc`/`.pypirc`/`.netrc`), with templates (`.env.example`, `*.pub`, …) still readable. Kills the false-blocker and stops local secrets entering model context. Genuine *committed* secrets are still caught from the PR diff at ingestion. | `app/agent/tools.py` (`is_secret_file` + guards in read_file/_walk_files/list_dir) | tools surfaced gitignored local files → false-positive secret blocker + secret leakage | Run 1 false-positive blocker |
| _Deferred (follow-up ticket)_ — full `.gitignore`-awareness (skip any ignored path, not just the secret denylist) to also cut noise from build dirs etc. Needs `git check-ignore` or a gitignore parser; kept out of the offline-testable core. | `app/agent/tools.py` (RC1-109) | broader noise reduction beyond secrets | Run 1 analysis | 

## Open questions / known gaps

- **Verdict policy mismatch (demonstrated in Run 2).** `REVIEW_BLOCK_ON` default
  is `leaked_secret`, but the rubric's category set uses `security` (not
  `leaked_secret`) — so `block_on` matches no real category and blocking keys off
  `blocker` *severity* alone. Effect: any blocker-severity finding gates the
  merge (Run 2 blocked on a placeholder-URL correctness issue, not a secret),
  contradicting "advisory by default; block only on a committed secret".
  Fix direction for **RC1-117**: give committed secrets a distinct category
  (e.g. `leaked_secret`), set `block_on` to it, and gate on `block_on` category
  only (drop the bare `severity == 'blocker'` clause) — so non-secret blockers
  are surfaced prominently but don't force "Request changes". Treat as a hard
  prerequisite before wiring to GitHub.

## Sign-off

**Signed off — 2026-06-13, Reid Collins (snacksnack).** Review quality is good
enough to wire to the GitHub App (Milestone 2).

Basis:

- Exercised on 2 representative PRs (n8n workflow ×2, pure-Python), Runs 1–3.
  Judged sufficient coverage given the breadth of rubric dimensions hit (n8n
  execution-cost, security, convention, error-handling, dependencies, pr-drift,
  docs, and a real logic bug).
- The two issues Run 1 surfaced are fixed and validated live: n8n finding
  duplication (Run 2) and the false-positive secret blocker + secret leak
  (Run 2). The pure-Python review (Run 3) is accurate, grounded, and
  well-calibrated, with a correct advisory verdict.
- Remaining nits are minor (slight finding redundancy; turn-budget truncation on
  a small PR); none block the gate.

**Carried to RC1-117 as a hard prerequisite (verdict policy):** the BLOCK verdict
currently gates on any `blocker`-severity finding, not just committed secrets
(demonstrated in Run 2). Must be fixed before going live — see Open questions.

> **Resolved in RC1-117.** Committed secrets now get a distinct `leaked_secret`
> category (added to the rubric/schema in `app/agent/prompts.py`), `block_on`
> defaults to `leaked_secret`, and the shared verdict policy (`app/verdict.py`)
> gates on `block_on` *category* only — the bare `severity == 'blocker'` clause
> is gone. A non-secret blocker is surfaced prominently but stays advisory.
> Covered by `tests/test_verdict.py` and the updated `tests/test_review.py`.
