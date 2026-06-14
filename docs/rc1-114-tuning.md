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
| 1 | `snacksnack/ai-incident-summarizer#12` | pure-Python | Real Python change; baseline for Python rubric dimensions | ☐ not run |
| 2 | `snacksnack/n8n-stakeholder-status-email#1` | n8n workflow | Exercises the RC1-112 check; planted 3 costly patterns + 2 benign nodes | ✅ run — 1 blocker / 9 warn / 2 nit |
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

- `REVIEW_BLOCK_ON` default is `leaked_secret`, but the rubric's category set
  uses `security` (not `leaked_secret`) — so blocking currently keys off the
  `blocker` *severity*, not that category. Confirm the intended verdict policy
  (formalized later in RC1-117) lines up before sign-off.

## Sign-off

> _Pending._ Quality is good enough to wire to the GitHub App (Milestone 2)
> when: false-positive rate is acceptable across the PR set, severities feel
> right, and the n8n ground-truth above matches. Record the explicit decision
> (who / date) here.
