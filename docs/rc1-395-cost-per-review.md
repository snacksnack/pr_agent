# RC1-395 — Cost per review in Datadog: decision record

The cost story's last step before the flag flip. RC1-390 and RC1-393 built a
multi-agent review that costs less per review than the single loop and
decided the flag should go on; the decision was made from eval run records
and scratch-script logs, because nothing in Datadog said what a review
cost. This story gives Datadog that number: one point per review, priced at
the end of the trace, with a widget and a monitor on it.

- **Ticket:** [RC1-395](https://hirereidcollins.atlassian.net/browse/RC1-395)
- **Flag:** none. Both paths are priced; nothing changes a request.
- **Code:** `app/pricing.py` (new), the dispatcher in
  `app/agent/reviewer.py` (one `pr_review` workflow span for both paths),
  `annotate_review_cost` / `ship_review_metrics` in `app/observability.py`,
  the call in `app/webhook.py`; `verifier_model`, `scout_ran`, `latency_ms`
  on `ReviewResult`.
- **Datadog, as code** (RC1-378, in `tpm-automation-platform/datadog/`):
  monitor `319523084` and a "Cost per review" group on the fleet dashboard
  `bwm-uny-qqs`. Platform PR #62; this repository's PR #41.

## Context

The only cost signal Datadog had for the PR agent was RC1-377's monitor,
which is cost per *call*: `ml_obs.span.llm.total.cost` over the number of
`llm` spans. The multi-agent path replaces one long loop with five or six
short calls, so that number goes *down* when the flag goes on whether or not
the review got cheaper. A per-review figure needs the four token counts of
every stage summed and priced at the end of the review, and the result
already carries them (`ReviewResult.stage_usage`, `verifier_usage`, the
totals, since RC1-387 and RC1-390). The gap was pricing them where the trace
is still open and putting the number where a dashboard and a monitor can
read it.

**The ticket's open point, settled first.** Does LLM Observability roll
estimated cost up to the workflow span on its own? No. Queried on
2026-09-06: `ml_obs.span.llm.total.cost` carries only `span_kind:llm`
(`ml_obs.span` itself has `workflow`, `agent`, `task` and `llm`), and the
per-token cost metrics beside it are the same. Cost exists on the calls and
nowhere else, so the metric in step 3 of the ticket is the whole story, and
the monitor queries it rather than any span.

## What was built

**A price table the image can use** (`app/pricing.py`). The runtime image
is `python:3.12-slim` with no git, and the eval harness's
`agent_evals.pricing` is pinned by git ref, so the webhook cannot import it
— the same reason `app/observability.py` exists. The table is a copy: five
models, input and output per million tokens, plus the two cache rates the
API bills at (writes 1.25x the input price, reads 0.1x). `tests/test_pricing.py`
asserts the copy equals the harness's table in both directions, that the
cache rates equal the harness's (RC1-392), and that `cost_usd` matches
`evals/subject._cost_usd` to the token for every model, so the two cannot
drift silently. An unknown model raises rather than pricing at zero, and the
live path catches that at the boundary and ships nothing: a gap in the
series means unmeasured, a zero would mean free (the platform's rule since
RC1-305).

`review_cost` prices a finished result. The multi path recorded every
stage, so each is priced and summed; the single loop recorded only its
total and the verifier's share, so its stages are `loop` (total less
verifier) and `verifier`. The verifier is priced at the model it actually
ran on — `review_verify_model` may differ from the review model, and the
result now records which (`verifier_model`).

**One workflow span, both paths.** Before this only the multi path opened a
`pr_review` workflow span, inside `review_pull_request_multi`. It now opens
in `review_pull_request`, the dispatcher both paths share, and the finished
review is priced *inside* it — `annotate_review_cost` runs before the
`with` closes, which a test asserts by event order — so the trace's root
carries `cost_usd`, `stage_cost_usd_<stage>` and `latency_s` as metrics and
`mode`, `scout`, `scout_turns`, `verified`, `conventions_file` as metadata.
The single loop's body moved to `_review_single` unchanged; the multi path
lost one indent level. Flag off is the same request shape, byte for byte.

*A dot in a metric key drops the span.* Found on the first live run: the
ticket named the keys `stage_cost_usd.scout`, and ddtrace warned that a `.`
"would prevent the span from being ingested" and rewrote it. Stage names
also carry colons (`reviewer:diff_local`), so the keys fold anything
outside word characters to `_` (`stage_cost_usd_reviewer_diff_local`).

**The metric.** `pr_agent.review.cost_usd` and `pr_agent.review.latency_s`,
both distributions, one point per review, submitted agentless to the v1
`distribution_points` endpoint from the webhook — and only from the
webhook. The dry-run CLI and the eval corpus run the same review function
and would otherwise write corpus cases into the production series. Tags:
`repo`, `mode:single|multi`, `scout:ran|skipped|none`,
`verified:true|false`, `model`, and `ml_app:pr-review-agent` so the fleet
dashboard's template variable applies. Every distinct combination is a
billable custom metric, five more once percentiles are on; the set is what
changes a review's price and nothing else. Percentile aggregation
(`p50`…`p99`) is a per-metric setting, switched on once through the metrics
tag-configuration API for both metrics on 2026-09-06; it is outside the
platform's sync loop and recorded here.

**The objects.** Monitor `319523084`,
`percentile(last_1d):p95:pr_agent.review.cost_usd{*} > 1`, warn $0.50 /
alert $1.00. The thresholds come from RC1-393's numbers: multi with the
3-turn scout ran 18–45 ¢ per review, the single loop 48 ¢–$1.10, so the
warn line sits above the intended path's range and the alert line above
the old one's — the alert also catches the flag going back off by accident.
RC1-377's per-call monitor stays; its message and this one's each say what
the other measures. The fleet dashboard gained a group: p50 and p95 query
values, p95 by mode with the two thresholds as markers, p95 by repo, p95
latency by mode. Created through the API and the file, pulled back, drift
clean across 22 objects.

## Live verification

The webhook path could not produce the first point before the merge, so PR
#41 itself was reviewed locally with the new code, tracing on, once per
path, and each result shipped through `ship_review_metrics` — the webhook's
call, minus the webhook. Both are real reviews of a real PR of this
repository (10 files, +600/−138), verifier on, `claude-sonnet-4-6`, against
this checkout.

| Path | Cost, priced here | LLM Obs's own estimate for the trace | Latency | Turns | Findings | Stages |
| --- | --- | --- | --- | --- | --- | --- |
| Multi, context + 3-turn scout | **38.9 ¢** | 38.9 ¢ | 49.8 s | 3 (scout) | 5 | scout 24.9 ¢ · warm cache 8.1 ¢ · reviewers 1.8 / 1.5 / 1.2 ¢ · verifier 1.2 ¢ |
| Single loop (production today) | **69.6 ¢** | 69.6 ¢ | 106.8 s | 20 (the cap) | 2 | loop 63.1 ¢ · verifier 6.5 ¢ |

Both prices match Datadog's own estimate for the same traces to the fourth
decimal: `$0.3888` for the multi review and `$1.0847` for the two together,
summed over their `llm` spans — the acceptance criterion was within a cent.
The multi trace is one `workflow` span with three `agent` spans, two `task`
spans and nine `llm` spans under it; the single loop's is one `workflow`
span, one `agent` span (the verifier) and twenty-two `llm` spans. Each root
carries the cost metrics.

Both points were queryable by `max` within a minute. The `p50`/`p95`
aggregations stayed empty for them: percentile aggregation was switched on
between the two submissions and applies to points from then on, and the
second point did not surface under it within the fifteen minutes watched.
The first webhook review after the deploy is the check that they populate.

Two things the numbers say that RC1-393 could only say from the corpus.
The scout is 64% of the multi review on a PR this size (24.9 ¢ of 38.9 ¢),
which is RC1-394's case made on a live PR. The warm-cache call is 8 ¢ here
because this PR's shared prefix is 79K tokens — the write is paid once and
the four calls that follow read it for 4 ¢ between them. And the single loop
spent its whole 20-turn cap, took twice as long, and found fewer things,
which is the RC1-393 #33 row again (70.0 ¢ against 31.0 ¢) on a different
PR.

## Decision

Nothing to decide in this story; it makes the earlier one visible. The flag
is still off in production as of this session — `REVIEW_MULTI_AGENT` is
not among the Fly app's secrets — and RC1-393's decision that it should go
on stands. Once it is on, the first live reviews are the check: one point
each on the dashboard's "Cost per review" group, `mode:multi`, under the
warn line. A `mode:single` point after that date is the flag having been
turned back off.

## What this story changed regardless of the flag

- Every review now runs inside a `pr_review` workflow span, single loop
  included, and the span carries the review's price and latency.
- `ReviewResult` records `verifier_model`, `scout_ran` and `latency_ms`.
- The webhook ships one cost point and one latency point per review.
- The price table lives in the app as well as the harness, with a test
  holding the two equal.
