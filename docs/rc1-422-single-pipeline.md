# RC1-422 — One production pipeline: decision record

The single loop (RC1-110) explored the repository and wrote the review in one
conversation. RC1-390 built the scout, the routed reviewers and the Python
merge beside it behind `REVIEW_MULTI_AGENT`, and RC1-393/394 moved the
exploration into Python until the scout was skipped on any repository with
a conventions file. The flag went on in Fly on 2026-09-07. This story makes
that path the only one: the loop, the flag and the flag-off constraints are
gone, `multi.py` is `pipeline.py`, and `review_pull_request` lives there.

- **Ticket:** [RC1-422](https://hirereidcollins.atlassian.net/browse/RC1-422)
- **Flags:** `REVIEW_MULTI_AGENT` and `MAX_TOOL_TURNS` removed. Pydantic
  ignores them in the environment, so the Fly secret can be unset after the
  merge without a boot risk (`tests/test_config.py` pins that).
- **Code:** `app/agent/pipeline.py` (was `multi.py`; `review_pull_request`
  opens the `pr_review` span and prices the review itself), `app/agent/reviewer.py`
  (the shared primitives only: `render_pr`, `_create`, `parse_findings`),
  `app/agent/prompts.py` (the loop's `INSTRUCTIONS` gone), `app/config.py`,
  `app/models.py` (`mode` defaults to `multi`), `app/observability.py`,
  `app/review.py`, `app/webhook.py`, `evals/subject.py` (the pipeline's
  prompt hash is always in the subject version), `scripts/measure_pr.py`
  (`--multi` gone).
- **Runs (2026-09-12 UTC, `claude-sonnet-4-6`, verifier on):** corpus
  `pr-review-20260912T011615` against a copy of this checkout; the three reference PRs
  in the table below; the production series from Datadog for the week the
  flag has been on.

## Go/no-go, declared before the runs

Promote the pipeline if, with the loop deleted and nothing else changed:

1. **Corpus recall** stays in the recorded band: 12–13 of 13 planted defects
   (four consecutive baselines each missed at most one, a different one each
   time — README), with **no blocker on the clean diff or either decoy**.
2. **Reference PRs** #35, #33 and #39 at their own heads price within 25 % of
   RC1-394's run E (5.5–7.0 ¢, 12.9 ¢, 20.3 ¢), each with no scout call.
3. **Production** since the flag went on reads below the single loop's last
   live points on every review.

The corpus comparison is a byte-for-byte check as much as a quality one: the
pipeline's request shape is untouched, so the subject version (the hash of the
scout's and reviewers' instructions, plus the verifier's) is the one RC1-394's
run E recorded.

## What the runs said

### Corpus (sixteen cases, checkout, verifier on)

| | RC1-394 run E | **This story** |
| --- | --- | --- |
| Run | `…T174937` | `…T011615` |
| Cases passing | 13 / 16 | **15 / 16** |
| Recall | 13 / 13 | **13 / 13** |
| Categorized correctly | 11 / 13 | **13 / 13** |
| Precision | 1 / 2, no blocker | 1 / 2, no blocker (`deliberate-broad-except` drew `warning/tests` on its untested branch, as in every multi-agent run since RC1-390) |
| Clean diff | 1 nit | **0 findings** |
| Verifier | 19 dropped, 4 downgraded | 16 dropped, 5 downgraded |
| Cost | $0.71 (4.4 ¢ / case) | **$0.72 (4.5 ¢ / case)** |
| Wall clock per case | 12–23 s, median 17 | 4–22 s, median 18 |

The eval store recorded tonight's run under the same subject version as run
E — `rubric-sha256:3620c9b0…+verify-sha256:d2ac2909…+multi-sha256:a5950a28…+checkout`
— which is the byte-for-byte check: deleting the loop changed nothing the
pipeline sends. The scout made no call on any case (context complete on
all sixteen: `CLAUDE.md`, 15 caller rows, 38 test rows), so the two runs
are the same requests a week apart. Within that, the two category misses
run E carried (`unbounded-scan`, `general-dead-code`) came back correct
tonight and the clean diff drew nothing rather than one nit; the one
precision miss is unchanged. Cost is within 3 % of run E.

### Reference PRs of this repository, at their own heads

| PR | Single loop (RC1-393) | RC1-394 run E | **This story** |
| --- | --- | --- | --- |
| #35, 6 files, +161/−10 | 48.3 ¢, 96 s, 1 finding | 5.5 / 7.0 ¢, 4 / 12 s, 0 / 2 | **5.8 ¢**, 13 s, 0 (1 dropped by the verifier) |
| #33, 10 files, +666/−81 | 70.0 ¢, 110 s, 4 | 12.9 ¢, 27 s, 3 | **12.4 ¢**, 27 s, 3 nits (3 dropped) |
| #39, 27 files, +2,168/−35 | $1.10, 100 s, 3 | 20.3 ¢, 60 s, 7 | **21.3 ¢**, 58 s, 11 (4 warnings; 1 dropped) |

The context was complete on all three (`CLAUDE.md`, callers and tests in
the prefix) and no scout call was made, as in run E. Cost is within 5 % of
run E on every PR (the criterion allowed 25 %) and wall clock within a few
seconds. Where the money went on #39: the one prefix write 7.9 ¢ (37 %),
three reviewers 3.5–3.9 ¢ each, the verifier 2.4 ¢ — run E's shape. The
finding count on #39 (11 against run E's 7) is the run-to-run spread
RC1-390 recorded on identical requests, not a change in the request: two
of the four warnings are the tests reviewer reading "no test references"
for `router.py` and `scout.py`, which is true of that head. One of the
others names `asyncio.run` inside the pipeline as an event-loop hazard for
the webhook's background task — the exact defect RC1-426 is filed for.

### Production, from the per-review metric (RC1-395)

`pr_agent.review.cost_usd` and `pr_agent.review.latency_s`, every review the
webhook priced from 2026-09-06 to 2026-09-12, split at the moment the flag
went on (2026-09-07 00:15 UTC). Points are Datadog's 30-minute rollup.

| | Reviews | Cost min / p50 / p95 / max | Latency p50 / p95 / max |
| --- | --- | --- | --- |
| Single loop, last live points (09-06) | 3 | 30.1 ¢ / 38.7 ¢ / — / $1.25 | 123 s / — / 207 s |
| Pipeline (09-07 → 09-12) | 35 | 1.1 ¢ / 3.7 ¢ / 20.0 ¢ / 27.7 ¢ | 13 s / 68 s / 79 s |

Ten repositories reviewed on the pipeline in that week, every one below
the cheapest single-loop point. The most expensive pipeline review (27.7 ¢,
73 s; `agent-evals`, 09-07 19:00 UTC) predates that repository's
conventions file by four hours (RC1-402, 22:48 UTC), so the scout ran on it;
its next review, 09-08, cost 3.1 ¢. The three `reid_basic` reviews (12–23 ¢,
57–79 s) are the portfolio site's TypeScript PRs, the largest in the week.

## Decision

**Promoted.** All three criteria held, none of them narrowly: corpus
recall 13/13 with no blocker on the clean diff or either decoy and the
subject version unchanged; the three reference PRs within 5 % of run E's
cost with no scout call; thirty-five production reviews across ten
repositories in the week the flag was on, every one cheaper than the
cheapest single-loop review of the week before (27.7 ¢ at worst against
30.1 ¢ at best), at a p50 of 3.7 ¢ and 13 s against 38.7 ¢ and 123 s.

What the single loop still had over the pipeline — a model that explores
and judges in one conversation — is the thing RC1-393 showed costing 48 ¢
to $1.10 a review, because on a real PR it read to its cap every time. The
pipeline's exploration is Python's where the repository has a conventions
file, and the scout's on a short cap where it does not; RC1-427 measures
whether that scout still earns its place.

## What is gone, what stayed

Gone: `_review_single`, `_maybe_verify`, `_force_submit`,
`format_pr_for_review` and `ALL_TOOLS` in `reviewer.py`; the loop's
`INSTRUCTIONS` in `prompts.py`; `review_multi_agent` and `max_tool_turns` in
`config.py`; the dispatcher in `review_pull_request`; the `--multi` switch on
`measure_pr.py`; the "flag off is byte-identical" convention in `CLAUDE.md`;
the single-loop tests (24 in `tests/test_reviewer.py`, replaced by 17 on the
primitives) and the dispatch tests in `tests/test_pipeline.py`.

Stayed, deliberately: the Python router, the deterministic context, the
optional scout, the fan-out, the merge and the optional verifier — the shape
the ticket asked to keep. `ReviewResult` is unchanged except that `mode`
now defaults to `multi`; RC1-429 owns splitting its telemetry out. The
verifier's own-prefix request shape (no `shared_prefix`) stays because the
RC1-398 tie-break probe sends it; RC1-428 decides the verifier's future.
`reviewer.py` keeps its name for now: it holds the primitives the scout and
the verifier import, and RC1-426 (one async client abstraction) is the
natural point to rename it with the rest of that restructuring.

## After the merge

- `fly secrets unset REVIEW_MULTI_AGENT` on the app, when convenient; the
  setting is ignored either way.
- The eval store's subject version for the default configuration now always
  carries the `+multi-sha256` segment; rows without it are the retired loop's.
