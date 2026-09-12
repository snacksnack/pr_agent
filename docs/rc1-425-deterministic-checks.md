# RC1-425 — The pipeline runs the deterministic checks and assembles the result

The n8n execution-cost check (RC1-112) ran outside `review_pull_request`:
the dry-run CLI read the changed workflow files from its checkout and ran
it, the webhook read them through the Contents API and ran it (RC1-121),
each handed the findings to the pipeline as `precomputed_findings` so the
model would see them as already recorded, and each appended them to the
result afterwards. Two copies of the ordering, two places a merge could be
missed or doubled, and a result that left the pipeline incomplete. This
story moves all of it inside: the pipeline runs the checks first, tells the
model, verifies only the model's claims, and returns the one result.

- **Ticket:** [RC1-425](https://hirereidcollins.atlassian.net/browse/RC1-425).
- **Related:** RC1-112 / RC1-121 (the check and its live path), RC1-422
  (one pipeline), RC1-424 (the repository contract the checks now read
  through), RC1-429 (result telemetry, next).
- **The request shape is unchanged.** `build_shared_prefix` renders the
  checks' findings exactly where it rendered `precomputed_findings`, with
  the same text; nothing the reviewers or the verifier send moved, so no
  corpus band run was owed. The n8n case — the one whose bytes now travel
  a different path — was run (below); the offline suite and the contract
  test are the rest of the evidence.

## The stage

`app/agent/checks/__init__.py` holds the registry and the runner:

| Name | What it is |
|---|---|
| `Check(name, run)` | one deterministic check: `run(pr, read_text) -> list[Finding]` |
| `CHECKS` | every check the pipeline runs, in order — `n8n` today |
| `run_deterministic_checks(pr, repository, checks)` | the stage: reads changed files through the `RepositoryAccess`, returns a `CheckRun` of findings, checks that completed and checks that raised |
| `MAX_CHECK_FILE_BYTES` | 1 MB — a check reads the whole file |

The pipeline's graph is now `plan -> checks -> context -> warm cache ->
reviewers -> merge -> verifier -> assemble`. The checks' findings are in
the shared prefix under "already recorded by automated checks" (the text
RC1-112 wrote), the verifier sees only the merged model findings, and
`_assemble` appends the checks' findings after the verified ones and
records `checks_run`, `checks_failed` and `deterministic_findings` on the
`ReviewResult`. The webhook builds a `GitHubRepository`, calls the
pipeline and posts what it returns; the CLI builds a `LocalRepository`,
calls the pipeline and prints; the eval subject wraps the same call. None
of them import the check any more.

## What the contract gained

`read_text(path, *, max_bytes=MAX_READ_BYTES)`. The contract's clip is
sized for the prefix (64 KB); a check parses the file, and the estate's
workflow exports are 92 KB (concert-intelligence) and 65 KB
(stakeholder-status-email). Routed through the default clip, both would
have failed `json.loads` and been skipped silently — the check would have
gone quiet on exactly the repositories it exists for. The stage asks for
`MAX_CHECK_FILE_BYTES`; both adapters honor it; the contract test covers
it. The GitHub Contents API stops at about 1 MB on its own, so the two
paths still see the same bytes.

## Behavior that changed, deliberately

- **The live check spends the review's API budget.** Each changed `.json`
  file the check reads is one Contents call under `remote_api_budget`
  (60), where before it was a call outside the budget. The read is cached
  and changed files are the first candidates a grep fetches, so on a
  workflow PR the context's own reads come from the cache and the review
  spends the same number of calls as before; the budget is simply honest
  about them. A budget already spent reads as `None` and the check skips
  the file, as a missing file always did.
- **The check reads through the guards.** A changed file the review may
  not see — a secret-named or lock file — is not read by the check either.
  No workflow export is named like one.
- **A failing check is a named, logged fact.** `checks_failed` on the
  result, `check_failed name=<check>` with the traceback in the log, the
  other checks still run. The webhook used to log and drop; the CLI had no
  guard of its own.
- **`precomputed_findings` is gone** from `review_pull_request`, the CLI's
  `ReviewFn` (now `(pr, repository) -> ReviewResult`) and the eval wrapper.

## Evidence

Offline: the suite is green with coverage at 95 % against the 88 % floor.
`tests/test_pipeline.py` exercises the stage with zero checks, the
registry finding nothing, one finding reaching the prefix and the result
once, two injected checks in order, a raising check recorded beside a
working one, a workflow padded past the prefix clip still tripping the
check, and the check reading through `GitHubRepository` at the PR head.
The verifier test proves a drop verdict removes the model's finding and
leaves the check's untouched, and that the verifier's suffix never lists
the check's finding.

Billed, the n8n corpus case with a checkout (`python -m evals --case
n8n-hot-cron --repo-path .`), run `pr-review-20260912T104934.206133Z`,
subject version `rubric-sha256:3620c9b013d4+verify-sha256:d1466a1a1b9d+multi-sha256:25e6b6634f49+checkout`
(unchanged from RC1-428):

| Characteristic | Result |
|---|---|
| finds the planted defect | pass — 3 findings on the plant |
| categorises it correctly | pass — `n8n` |
| severity is calibrated | pass — warning meets the warning floor |
| exit code matches the verdict policy | pass — exit 0, advisory |
| merges the deterministic finding once | pass — 1 computed finding, seen once |

Cost $0.039, latency 13.4 s (checks 0 ms, context 8 ms, fan-out 11.2 s,
verifier 1.8 s). The check's finding ("Node 'Schedule Trigger': schedule
trigger runs every 30 second(s)…") appears once; the model's two n8n
findings are about deduplication and error handling, not the schedule —
the already-recorded instruction held.

## Left open

- **Live check.** The first production review after the deploy should
  log `checks ran=n8n failed=- findings=N` from `app.agent.pipeline`
  before the `context` line, and a workflow PR should post the check's
  finding once.
- The `pr_review` span carries `deterministic_findings` and
  `checks_failed` as metrics; the per-review Datadog metric does not,
  which is RC1-429's question.
- `reviewer.render_pr` still says "use read_file" on a truncated or
  missing patch (RC1-424 left it for the next prefix-touching story); this
  story did not touch the prefix text either.
