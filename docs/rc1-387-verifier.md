# RC1-387 — Verifier pass: decision record

The first step of the multi-agent review sequence (RC1-387 → RC1-390 → RC1-391).
This story lands three things and records what they showed: two **precision
cases** in the corpus, a **verifier pass** behind a flag, and the **baseline
corpus run** that every later step is compared against.

- **Ticket:** [RC1-387](https://hirereidcollins.atlassian.net/browse/RC1-387)
- **Flag:** `REVIEW_VERIFY_FINDINGS` (default off), `REVIEW_VERIFY_MODEL` (optional)
- **Code:** `app/agent/verifier.py`, wired at the end of `review_pull_request`

## Context

The 2026-09-04 survey of where orchestration would earn its place across the
estate found that this reviewer is not broken: the last corpus run found all
thirteen planted defects with one advisory finding on the clean diff. Recall is
at ceiling. The number the corpus could not see was **precision** — whether a
diff that *looks* like it has a defect draws a warning it should not.

That is also the number a verifier pass moves. A second, tool-less reader with
one job ("does the diff support this claim") and no incentive to look thorough
is the standard shape for suppressing over-flagging without touching recall. It
costs one extra call per review, and only on reviews that produced findings.

The decision this record exists to make: **turn the flag on in production, or
leave it off**, based on numbers rather than on the pattern being standard.

## What was built

**Precision cases.** Two diffs, each with a planted *decoy*: a pattern the
rubric names as a defect, in a context where a competent reviewer would agree it
is fine. Scored on not raising the decoy at `warning` or above; a `nit` is
recorded, not failed (the harm the rubric warns about is warnings people learn
to ignore).

| Case | Decoy category | The decoy |
| --- | --- | --- |
| `benign-test-secret` | `leaked_secret` | A placeholder HMAC key in a test, used only to compute the digest the test asserts. The one category that blocks a merge is where a false positive costs the most. |
| `deliberate-broad-except` | `error_handling` | A catch-all at the top of a background worker that logs with the traceback and continues, explained in a comment and the description. This repo's own webhook uses the pattern. |

**Verifier.** One call after the loop submits, with the same system prompt, the
same rendering of the diff, and the numbered findings. It returns a verdict per
finding — keep, drop, or downgrade — through a strict tool schema. Python
applies the verdicts under rules the model cannot override: a finding with no
verdict is kept; a downgrade only lowers; nothing is added. Deterministic (n8n)
findings never pass through it. Drops and downgrades are logged with the
model's reason and carried on the `ReviewResult` (`verifier_dropped`,
`verifier_downgraded`) so the eval record shows what was suppressed.

**Cost accounting.** The verifier's tokens are folded into the result's totals
(so cost per review on the trend page includes it) and broken out separately
(`verifier_input_tokens`, `verifier_output_tokens`). The run's `prompt_version`
gains a `+verify-sha256:` suffix when the flag is on, so flag-off and flag-on
runs are different subject versions in the store and are never averaged.

**Prompt caching.** The verifier sends the same cached system block as the loop
but its own single tool. The API's cache prefix is `tools → system`, so a
different tool list means the verifier caches its *own* prefix (shared across
verifier calls within the TTL) rather than reading the loop's. Sharing the
loop's prefix would need a breakpoint on the tools block as well, which is
RC1-390's job when it builds the shared prefix deliberately. The cost of not
sharing is the system prompt at cache-read price per verifier call, measured
below.

## Method

Both runs use the same corpus (16 cases: 13 planted, 2 precision, 1 clean), the
same model (`claude-sonnet-4-6`), the same prompts, back to back on
2026-09-06, from the dry-run CLI with no `--repo-path` (diff-only, as the corpus
has always been run). Run 1 has the flag off and is the baseline. Run 2 has the
flag on. Nothing else differs.

The RC1-377 price monitor measures cost per *call*. A second call per review
pushes that number down while cost per review goes up, so it is not read as the
cost signal here; the run record's per-case `cost_usd` is.

## Results

Four runs on 2026-09-06: a baseline, a flag-on run that the Anthropic
workspace's credit balance cut off after six cases, and then — after the
accounting fix and the fixture change below — the corrected pair. Nothing
below is averaged across runs.

### Run 1 — flag off, baseline (`pr-review-20260906T134001`)

| | Result |
| --- | --- |
| Recall | **12 / 13** planted defects found. Missed: `unpythonic-loop` (zero findings). |
| Clean diff | 2 nits, 0 blockers (a boundary-test suggestion and a description mismatch). |
| Precision | **1 / 2** decoys held. `benign-test-secret` drew nothing on the decoy. `deliberate-broad-except` drew a warning. |
| Exit codes | All 16 match the verdict policy. |
| Wall clock | 504 s across 16 cases; `benign-test-secret` alone took 110 s. |

Two findings from this run changed the story's own premises:

1. **Recall is not at ceiling; it is 12 or 13 of 13 per run.** The two prior
   runs in the store (2026-08-27, 2026-08-31) each also missed exactly one
   case, a different one each time (`unpinned-dependency`, then
   `convention-break`, now `unpythonic-loop`). The "13 / 13" that the
   2026-09-04 survey leaned on was one good run. A single run of this corpus
   cannot show a one-case change in recall either way; the verifier's
   recall claim has to be read across at least two runs per flag state.

2. **Cost has been undercounted ~2.5x since prompt caching landed
   (RC1-350, 2026-08-31).** Cases recorded 5–8 input tokens where the
   pre-caching runs recorded 9,000–20,000, and the run totaled $0.30 against
   $0.76–$0.80 for the same corpus a week earlier. The loop summed only the
   API's `input_tokens`, which excludes `cache_read_input_tokens` and
   `cache_creation_input_tokens`. Fixed in this story (`TokenUsage`, priced
   at 1.25x for writes and 0.1x for reads); the library-side fix so other
   subjects do not repeat it is **RC1-392**. Every pr-review cost on the trend
   page between 2026-08-31 and this fix is low by that factor. Because the
   baseline's own cost is wrong, **the cost comparison below waits for the
   corrected pair.**

The failed decoy was a fair call against the fixture, not against the
reviewer: the diff dispatched through `handlers[delivery.kind]`, and the
finding said the new catch-all would mask a `KeyError` on an unknown kind.
That is a real observation about a second construct in the same hunk. The
fixture now dispatches through a plain call so only the catch-all is under
test; the case re-runs in the corrected pair.

### Run 2 — flag on, partial (`pr-review-20260906T134425`, 6 of 16 cases)

Stopped by the credit exhaustion after `unpinned-dependency`. Old accounting,
old fixture. The six that completed:

| Case | Baseline findings | Flag-on findings | Verifier action | Latency, off → on |
| --- | --- | --- | --- | --- |
| leaked-secret | 5 | 6 | kept all | 29 s → 47 s |
| sql-injection | 4 | 3 | kept all | 23 s → 23 s |
| swallowed-exception | 3 | 3 | kept all | 22 s → 26 s |
| breaking-signature | 6 | 5 | kept all | 32 s → 27 s |
| untested-risky-path | 4 | 3 | **dropped 1** (a `nit/pythonic` about an intermediate variable) | 38 s → 63 s |
| unpinned-dependency | 3 | 4 | **downgraded 1** | 29 s → 35 s |

Recall on the six: 6 / 6, and every planted finding survived the verifier at
its original severity. The one drop was a taste nit, which is the kind of
finding the pass exists to remove. The one downgrade was a `tests` finding on
a PR whose description promised code that was not in the diff.

Latency: the verifier adds one non-tool call, and on these six it added
between nothing and about 25 s. The finding counts differ between runs by one
or two on most cases with the flag *off* as well, so the finding-count column
is run-to-run variance more than verifier effect.

### Runs 3 and 4 — the corrected pair

Same corpus, same model, cache-aware accounting, tightened decoy fixture,
back to back (flag off `pr-review-20260906T140338`, flag on
`pr-review-20260906T141141`).

| | Flag off | Flag on |
| --- | --- | --- |
| Recall | 12 / 13 (missed `unpinned-dependency`, zero findings) | **13 / 13** |
| Categorized correctly | 12 / 13 (`convention-break` filed as `pr_drift`) | 13 / 13 |
| Clean diff | 2 nits, 0 blockers | **1 nit**, 0 blockers |
| Precision | 1 / 2 (`deliberate-broad-except` drew two warnings) | **2 / 2** |
| Cases passing | 13 / 16 | **16 / 16** |
| Cost, 16 cases | $0.442 | $0.546 (**+$0.104**, +23%) |
| of which verifier calls | — | $0.134 (0.84 ¢ per call) |
| Wall clock, 16 cases | 430 s | 482 s (**+52 s**, +3 s per case on average; range −9 s to +18 s) |
| Verifier actions | — | 2 dropped, 5 downgraded, across 55 findings |

**What the verifier did, finding by finding.** The five downgrades were all
`warning → nit` on soft findings: a raw connection returned without error
handling, a missing return annotation, a docstring point, and two of that
kind. The two drops:

- `benign-test-secret`: a nit that the hunk header said `+17` while the patch
  body had 16 lines. True of the fixture, and not a review finding. A good
  drop.
- `breaking-signature`: *"A new required parameter `verify_tls` is added with
  no default value. Any existing caller…"* This is a **real finding**. Two
  other `breaking_change` findings on the same signature were kept and the
  case still passed, so the verifier most likely read it as redundant with
  them. It is the one action in 55 I would not have taken, and it is the
  reason drops are logged with the model's reason rather than applied
  silently.

**What the verifier did not do.** No planted defect was dropped or
downgraded on either the six-case partial run or this one. The recall gain
(12 → 13) and the precision gain (1 / 2 → 2 / 2) are **not** the verifier's
doing: it cannot add a finding, and on `deliberate-broad-except` it took no
action at all — the first pass simply produced nits on the decoy this time
where the previous run produced warnings. Both gains are first-pass
run-to-run variance, which the three earlier baselines already showed
spans one case in either direction. The verifier's measurable effect is the
row of drops and downgrades and the noise line (2 → 1 on the clean diff,
where it also took no action). Read the recall and precision rows as "not
harmed", not as "improved by the verifier".

**Cache behavior, as predicted.** Every verifier call after the first read
1,596 tokens from cache (the system prompt) and wrote 1,000–1,700 (the diff
plus the findings). The verifier's prefix is its own, shared across verifier
calls within the TTL, and the cost of not sharing the loop's prefix is those
~1.5K write tokens per call — about 0.6 ¢ of the 0.84 ¢.

**Two defects the pair surfaced in the loop itself, both fixed here:**

- One flag-on finding came back with `severity: "breaking_change"`. The tool
  schema's enum guides the model but does not bind it, and
  `_result_from_submission` only checked the field was non-empty, so the
  finding would have been posted with a nonsense severity and sorted last.
  Unknown severities are now coerced to `warning` and counted
  (`coerced_findings`).
- Three of the four flag-off runs on record missed exactly one case with
  **zero findings** on a diff with an obvious defect, and the record could
  not say whether the model submitted nothing, ran out of turns, or
  submitted findings the loop threw away as malformed. The loop now counts
  malformed findings instead of skipping them silently, and the run record
  carries `tool_turns`, `files_read`, `truncated`, `malformed_findings` and
  `coerced_findings` per case. The next zero-finding miss is diagnosable.

Two runs in the store are artifacts of the credit outage and read as such on
the trend page: `pr-review-20260906T134425` (flag on, 6 cases then 10
errors) and `pr-review-20260906T134436` (flag off, 16 errors). The store is
append-only by design.

## Decision

**Recommend turning the flag on in production, as a logged trial.** The
grounds, in the order the ticket set them:

1. **No recall harm.** 19 of 19 planted defects across the two flag-on runs
   survived at their original severity.
2. **Precision moved in the right direction where the verifier acted:** five
   overstated warnings became nits and two low-value findings were removed.
   It did not act on the decoy this run, so the precision *row* is variance;
   the *actions* are the evidence.
3. **Cost is inside the guardrail.** +0.65 ¢ per review (+23%) against
   RC1-377's warn threshold of $6 and a real-review cost around 3–4 ¢.
   The RC1-377 price monitor is cost per *call* and will read lower with the
   flag on, because the verifier call is cheap; it is not the signal here.
4. **Latency is +3 s per review on average,** on a background worker nobody
   waits for.

The one questionable drop in 55 argues for the trial being *logged*: every
drop and downgrade goes to the webhook log with the model's reason
(`verifier_drop`, `verifier_downgrade`), so a week of live reviews will show
whether the `verify_tls` case was a one-off. If real findings are being
dropped, the prompt gets a "do not drop a finding for being similar to
another" line, or the flag comes back off — both are a one-line change.

The flag is off in this branch. Turning it on is a Fly secret
(`REVIEW_VERIFY_FINDINGS=1`) and is Reid's call, not this PR's.

**Not decided by this story:** whether the reviewer should be split further.
That is RC1-390, and it now starts from a corrected baseline: recall 12–13 of
13 per run, cost $0.44 per 16-case corpus, and a precision score that the
first pass alone reaches on a good run. RC1-390's cost claim has to be made
against the *flag-on* number if the verifier ships, since the split keeps the
verifier.

## What this story changed regardless of the flag

- Two precision cases in the corpus, and a `does-not-flag-the-decoy`
  characteristic that the summary prints as its own line.
- Cache-aware token accounting on every review, in the loop and the verifier,
  with the four counts in the run record's observations.
- A 180 s request timeout on the SDK client the loop builds. The default
  (600 s × 3 attempts) let one stalled response hold a corpus case for
  half an hour while the API was answering in about a second.
- `prompt_version` carries a `+verify-sha256:` suffix when the flag is on, so
  flag-on and flag-off runs are separate subject versions in the store.
