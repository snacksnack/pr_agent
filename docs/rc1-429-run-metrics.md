# RC1-429 — The review and the run's metrics are two objects

`ReviewResult` carried the summary and findings beside twenty-odd fields
about how the review was produced: four token counts, per-stage usage and
latency, the reviewers that ran, what the verifier dropped, the context
statistics, the check counters RC1-425 added. Pricing, Datadog, the eval
store, the CLI's diagnostics line, verdict and posting all read the one
object, and every new measurement widened it. This story splits it: the
review a caller publishes, the metrics it prices and ships, returned
together and never mixed.

- **Ticket:** [RC1-429](https://hirereidcollins.atlassian.net/browse/RC1-429).
- **Related:** RC1-395 (cost per review), RC1-390 (stage metrics), RC1-425
  (the pipeline assembles the result), RC1-387 (the verifier).
- **No model call changed.** The prefix, the reviewers' suffixes and the
  verifier's request are byte-identical; no corpus band run was owed. The
  n8n case with a checkout was run once through the eval subject to prove
  the eval path end to end on the new shapes (below).

## The shapes

| Type | Holds | Read by |
|---|---|---|
| `ReviewResult` | `summary`, `findings` (+ `sorted_findings`, `blockers`, `has_blocking`) | verdict, posting, the CLI's review text, the eval scorer |
| `RunMetrics` (frozen) | `model`, `mode`, `reviewers_run`, `usage`, `stage_usage`, `stage_latency_ms`, `latency_ms`, the malformed/coerced/off-scope/deduplicated/unusable counts, the context statistics, `verified` + `verifier_dropped` + `verifier_downgraded` + `verifier_usage` + `verifier_model`, `checks_run` + `checks_failed` + `deterministic_findings` | pricing, observability (span annotation and the Datadog points), the eval observations, the CLI's diagnostics line, the measurement scripts |
| `ReviewOutcome` (frozen) | `review`, `metrics` | every caller of `review_pull_request` |
| `verifier.Verification` (frozen) | `kept`, `dropped`, `downgraded`, `usage`, `model`, `ran` | the pipeline, the tiebreak probe |

`RunMetrics` carries no summary and no kept finding; `verifier_dropped` is
the record of what the verifier removed, diagnostics rather than review.
Lists are tuples. The dicts (`stage_usage`, `stage_latency_ms`) are built
once by the pipeline and not touched after; the dataclass is frozen.

## What moved

- **The pipeline** builds the review and the metrics side by side at the
  end of `_review` and returns the pair; `review_pull_request` adds the
  wall clock with `dataclasses.replace` and annotates the span from the
  metrics. Folding the verifier's tokens into the totals is the pipeline's
  job now, not the verifier's.
- **The verifier** takes a list of findings and returns a `Verification`.
  It no longer rebuilds a result, so the RC1-425 carry-through concern
  (a field the verifier forgot to copy) cannot recur. The tiebreak probe
  passes its pair straight in instead of wrapping it in a bare result.
- **Pricing** (`review_cost`), **observability** (`annotate_review_cost`,
  `review_metric_tags`, `review_metric_points`, `ship_review_metrics`)
  take `RunMetrics`. The metric names and the tag set (`ml_app`, `repo`,
  `mode`, `verified`, `model`) are unchanged.
- **The webhook** posts `outcome.review` and ships `outcome.metrics`.
  **The CLI** prints the review and, when it has the metrics, one
  diagnostics line (`format_metrics`); `format_review` without metrics
  prints the review alone, no placeholder values. **Posting** and
  **verdict** are untouched: they only ever read summary and findings.
- **The eval subject** scores `outcome.review.findings` and builds its
  observations from `outcome.metrics`. The observation keys and the
  `Usage` record are the same, so the store's history compares.
  **Tiebreak** and **`measure_pr.py`** read the two halves.

## Evidence

Offline: the suite is green with coverage at 95 % against the 88 % floor.
Pricing and observability tests build `RunMetrics`; the CLI tests build a
`ReviewOutcome` and prove `format_review` prints no telemetry without
metrics; the verifier tests assert on `Verification`; the pipeline tests
read `.review` and `.metrics`.

Restored on the way: four CLI tests (the `--repo-path` directory check and
three `format_review` tests) that the RC1-425 section cut had dropped from
`tests/test_review.py` — merged without them, noticed here by comparing
test names against the RC1-424 merge.

Billed, the n8n corpus case with a checkout, run ID and numbers below —
the same characteristics, observation keys and verifier record as this
morning's RC1-425 run, from the new shapes:

| Run `pr-review-20260912T111626.616064Z` | value |
|---|---|
| characteristics | 5/5 pass (found, category `n8n`, warning floor, exit 0, merged once) |
| observation keys | identical to `pr-review-20260912T104934.206133Z` (RC1-425, same subject version) |
| stage usage keys | `warm_cache`, `reviewer:*` ×3, `verifier` |
| stage latency keys | `checks`, `context`, `fan_out`, `verifier` |
| verifier | ran, 0 dropped, 0 downgraded, $0.0058 |
| cost / latency | $0.043 / 17.7 s |

## Left open

- `RunMetrics.mode` exists only so the eval store's `single` rows and the
  Datadog `mode:` tag keep their meaning; the pipeline always writes
  `multi`.
- The per-review Datadog metric still carries no check counters; they are
  on the span. Adding a tag would be a new billable combination and is a
  separate decision.
