# RC1-474 — Command as the reviewer: a scoped, judged arm-vs-arm comparison

Round two of the Cohere hands-on work (round one: RC1-473, chatbot rerank in
reid_basic). One question: with the *same* pipeline, prompts, corpus and
scoring, what does `command-a-plus-05-2026` do in the reviewer seat that
`claude-sonnet-4-6` holds — and what does each review cost?

## Method

**The seat swap is the whole change.** The pipeline talks to "any object
whose `messages.create` is a coroutine" and reads responses through
duck-typed accessors, so the second vendor is an adapter (`evals/cohere.py`),
not a pipeline change: it accepts the exact request the pipeline builds for
Anthropic and answers in the shape the pipeline reads. Nothing in `app/`
changed; the production roster cannot reach the adapter (AC3). The whole
review stack swaps — reviewers *and* verifier run on the arm's model, the
RC1-428 "one review model" policy applied to a different model. "Verifier
survival" therefore reads as each arm's self-consistency, not one judge
scoring both arms; the judges that are identical across arms are the
corpus's deterministic characteristics (plant found / categorised /
calibrated, decoys, clean-diff noise, exit codes).

**The pinned subset** (13 of the 16 corpus cases; AC1) — every category with
teeth, both precision decoys, and the clean case:

```
leaked-secret sql-injection swallowed-exception breaking-signature
untested-risky-path unpinned-dependency pr-drift unbounded-scan
convention-break docstring-contradicts-code
benign-test-secret deliberate-broad-except clean
```

Dropped to fit the trial budget: `n8n-hot-cron` (its finding is
deterministic and model-free — the arm cannot differentiate on it),
`unpythonic-loop`, `general-dead-code` (the two lowest-stakes categories).
Both arms ran `--repo-path .` (context on), selected with the new `--cases`
flag so the subset cannot drift between arms:

```bash
uv run python -m evals --repo-path . --cases <the list above>                      # baseline
REVIEW_MODEL=command-a-plus-05-2026 uv run python -m evals --repo-path . --cases … # Command
```

**Platform differences the adapter had to absorb** (each measured, not
assumed, on 2026-09-29):

* **No forced tool call.** The flagship rejects Cohere's own `tool_choice`
  ("tool_choice is not supported for this model", HTTP 400), so the
  pipeline's `tool_choice: any` has no translation. Tools stay declared and
  the prompt's "call submit_review exactly once" is the only forcing; a
  prose answer counts through the pipeline's existing paths (unusable
  reviewer / keep-everything verifier) and how often that happened is part
  of the result.
* **No prompt cache.** The warm-cache call (RC1-390's premise) is answered
  locally by the adapter with zero usage — a real round trip would spend the
  rate limit warming nothing. Every Command case reads "cold" in the run
  summary; that is the platform, not a failure. Claude's arm keeps its cache
  economics, and that asymmetry is part of the honest cost story.
* **Trial-key pacing, kept out of the latency figures.** 10 requests/minute
  — *measured* via 429s in RC1-473; the ~20/min the ticket carried is wrong
  — so request starts are spaced 6.5 s apart and 429s retry (AC4). The
  pacing sleep lands in `pacing_wait_ms`, the API round trip in
  `api_latency_ms`, recorded per case, because a latency that includes a
  deliberate sleep is the RC1-475 measurement mistake. Wall-clock latency
  for the Command arm is therefore read from the adapter's numbers, never
  from the case latency.
* **No list price.** `command-a-plus-05-2026` has no published per-token
  price (checked cohere.com/pricing 2026-09-29; the newest generative model
  with a listed price is legacy Command R+ 08-2024). The estate rule is that
  the bill is the price ground truth and a guessed price is worse than none,
  so the arm's store records carry the token counts and `cost_usd` 0 (the
  trend page renders it "—"); prices for Cohere models live beside the
  adapter in `evals/cohere.py`, not in `agent_evals.pricing`, whose table is
  scoped to the published first-party Anthropic list. Any dollar figure for
  Command below is illustrative, priced at Command A 03-2025's last public
  rate ($2.50/$10.00 per MTok), and says so.

**Cost numbers must not be projected to live reviews** — the corpus is
diff-only and the single loop explores to cap. Arm-vs-arm on identical
inputs is the valid comparison.

## Results

The compared records (both 13/13 cases end to end, AC1):
`pr-review-20260930T023659.389030Z` (Claude) and
`pr-review-20260930T025623.132938Z` (Command). Two earlier Command attempts
each lost one case to a Cohere-side `422 INVALID_TOOL_GENERATION` — the API
refusing its own model's malformed tool call, on a *different* case each
time — and stayed recorded as attempts; the adapter now retries those and
degrades to the unusable-reviewer path, and the clean run needed neither.

| | Claude (`claude-sonnet-4-6`) | Command (`command-a-plus-05-2026`) |
|---|---|---|
| Cases passed | 12/13 | 11/13 |
| Recall (planted defects) | **10/10** | 9/10 (`unpinned-dependency` missed) |
| Categorisation | 10/10 | 8/10 (docstring case filed as `pr_drift`) |
| Severity calibrated | 10/10 | 9/10 |
| Precision (decoys left alone) | 1/2 | **2/2** |
| Noise on the clean diff | 0 | 0 |
| Off-target findings, whole run | 18 | 23 (8 on `unbounded-scan` alone) |
| Verifier survival | 46/63 kept (73%); 17 dropped, 1 downgraded | 51/54 kept (94%); 3 dropped |
| Reviewer calls with no tool call | 0 | 2 of 39 (plus 6 and 2 in the two attempts) |
| Tokens per review (in / out) | 2,953 uncached + 2,618 cache-write + 19,434 cache-read / 1,344 | 14,436 / 3,552 |
| $ per review | **$0.0447** (list, cache-priced) | unpriced — no list price; ~$0.0716 at Command A 03-2025's $2.50/$10.00, illustrative only |
| Model latency | 16.4 s per case wall clock (fan-out 12.7 s + verifier 3.7 s avg) | 3.5 s p50 / 9.7 s p95 per call, 49 calls; case wall clock 24.0 s is pacing-dominated (~15.1 s/case deliberate wait) and not comparable |

What the table cannot show:

* **Tool-call reliability is the arm's real weakness.** With no way to force
  a tool call, Command sometimes answers in prose: 10 of ~115 reviewer calls
  across the three runs produced no `submit_review` (Claude: 0). Both of the
  clean run's failures trace to it or to its neighbourhood — the missed
  `unpinned-dependency` had both context-bearing reviewers come back
  unusable. When Command *does* call the tool, its reviews are competitive.
* **Run-to-run variance is large at n=13.** Across three Command runs:
  precision 2/2 → 1/2 → 2/2; the docstring case went unusable-miss →
  correct → wrong-category; `unpinned-dependency` went found-at-nit → miss
  → miss. Single-run differences of one case are noise here; the recall gap
  and the tool-call gap were stable, and so was Claude's baseline (its one
  precision miss is the same tests-flavoured warning RC1-387 documented).
* **Verifier survival is self-consistency, not a shared judge.** Command's
  verifier kept 94% where Claude's kept 73% — read together with Command's
  higher off-target count, the Command verifier is the *less* aggressive
  second pass, not the better-reviewed first pass.
* **The cost story is really a caching story.** Command's prompt is a third
  of Claude's per review, yet the illustrative cost lands ~1.6× higher,
  because Claude serves ~78% of its context from cache at 0.1×. A
  cache-less platform pays full price for the pipeline's
  shared-prefix-read-four-times design.

## Decision

Claude keeps the seat; no production change (AC3 held — nothing in `app/`
changed). The natural-seat comparison is done and publishable: Command
A+ is a competent reviewer when it submits through the tool, with notably
better decoy discipline in 2 of 3 runs, but it misses the pipeline's two
structural requirements — a forceable tool call and a prompt cache — and
one planted defect. The adapter, the `--cases` pinned-subset switch and the
pacing/latency observations stay as experiment infrastructure for the next
bake-off (RC1-477 wants the same arms on a dashboard).
