# RC1-427 — The scout's marginal value: decision record

The scout (RC1-390) is the one stage of the pipeline with tools: it explores
the repository once and writes a brief the three reviewers read from their
shared prefix. RC1-393 moved the conventions file and the callers into
Python, RC1-394 the tests, and since then the router skips the scout
whenever that context is complete — which, after RC1-396 and RC1-402 put a
conventions file in every repository the App reviews, is every production
review of the last week (RC1-422's record: 35 reviews, scout on one, and
that one four hours before its repository's conventions file landed).

What the scout still is: a module, a prompt, a tool schema, a settings
triple, a router branch, a result field, a pricing stage, a span, an
observability tag, and a failure mode (`ReviewError` when it will not
submit a brief). What it still does is run on a repository without a
conventions file, or on a live review whose tree call ran out of budget.
This story measures what that is worth and removes the scout unless the
number clears a threshold written down before the runs.

- **Ticket:** [RC1-427](https://hirereidcollins.atlassian.net/browse/RC1-427)
- **Instrument:** a `scout` switch on `review_pull_request` (RC1-427), reached
  as `python -m evals --no-scout` and `scripts/measure_pr.py --no-scout`,
  each composing with the RC1-393 `--no-repo-context` control. A run with
  the scout off is its own subject version (`+no-scout`).
- **Baseline:** RC1-422's runs — corpus `pr-review-20260912T011615` and the
  three reference PRs at their own heads — are the "complete context, no
  scout" arm and were not re-run.

## Go/no-go, declared before the runs

The scout is measured in the two conditions it can still run in, against
the same review with it switched off. Everything else — verifier on,
checkout, the same heads — is held.

| Arm | Context in the prefix | Scout | Where it stands for |
| --- | --- | --- | --- |
| **N8** | none (`--no-repo-context`) | full cap, 8 turns | a repository with no conventions file today |
| **N0** | none | off | the same repository after removal |
| **C3** | complete | forced, 3 turns (`REVIEW_SCOUT_COMPLETE_TURNS=3`) | what a scout adds on top of everything Python found |
| **C0** | complete | off (the default) | production today; RC1-422's runs |

Sample: the sixteen-case corpus against a copy of this checkout (verifier
on) for every arm, and the three reference PRs #35, #33, #39 at their own
heads for every arm. Budget: about $8 across the six new runs.

**Removal is the default.** The scout stays only if **both** hold:

1. **It finds defects the review otherwise misses.** On the corpus, N8
   recall exceeds N0 recall by two or more planted defects (recall has been
   13/13 in every configuration since RC1-387, so this can only be met if
   removing the scout *breaks* recall); or, on the reference PRs, N8 carries
   a warning-or-above finding on two of the three PRs that N0 does not, that
   the verifier kept, and that reads as true against the head on inspection.
2. **It does so without adding noise.** N8's clean-diff findings and decoy
   blockers are at or below N0's.

C3 against C0 is reported for the record and settles the narrower question
RC1-394 left open — whether a scout on top of a complete context earns its
cost — under the same rule: a kept, true, warning-or-above finding on two of
three PRs that C0 lacks. It does not by itself keep the scout, because the
router already skips it there.

If the scout goes, the removal is checked the way RC1-422 was: a corpus run
within the recorded band (recall 12–13/13, no blocker on the clean diff or
the decoys) and the three reference PRs within 25 % of RC1-422's cost, since
the shared prefix loses its "Scout's brief" line and the request changes.

## What the runs said

### Corpus (sixteen cases, checkout, verifier on)

| | N8: no context, scout 8 turns | N0: no context, no scout | C3: context, scout 3 turns | C0: context, no scout (RC1-422) |
| --- | --- | --- | --- | --- |
| Run | `…T015302` | `…T015701` | `…T020722` | `…T011615` |
| Recall | 13 / 13 | **13 / 13** | 13 / 13 | 13 / 13 |
| Categorized correctly | 13 / 13 | 10 / 13 | 11 / 13 | 13 / 13 |
| Precision (decoys left alone) | 1 / 2 (`benign-test-secret` tripped) | 1 / 2 (`deliberate-broad-except`, as always) | **0 / 2** | 1 / 2 |
| Blockers on clean or decoys | 0 | 0 | 0 | 0 |
| Clean diff | **3 findings** | 1 | 1 | 0 |
| Cases passing | 15 / 16 | 12 / 16 | 12 / 16 | 15 / 16 |
| Verifier | 22 dropped, 2 downgraded | 12 dropped, 5 downgraded | 25 dropped, 8 downgraded | 16 dropped, 5 downgraded |
| Cost | $2.26 (14.1 ¢ / case), scout $1.68 | **$0.50 (3.1 ¢)** | $1.80 (11.2 ¢), scout $0.98 | $0.72 (4.5 ¢) |
| Wall clock per case | 38–105 s, median 53 | 10–20 s, median 15 | 30–45 s, median 38 | 4–22 s, median 18 |

**Criterion 1 on the corpus: not met.** N8's recall over N0's is zero, not
two. Recall has been 13/13 in every configuration since RC1-387 and the
no-context, no-scout arm did not break it: every planted defect is in the
diff, and the reviewers find it from the diff.

**Criterion 2: not met either.** N8 put three findings on the clean diff
where N0 put one, and tripped the `benign-test-secret` decoy at warning
where N0 left it alone. The scout with the whole job and eight turns is
the noisiest configuration in the table.

What the scout *did* move without context is the category: 13/13 against
N0's 10/13. That is the conventions file's job — C0 has it at 13/13 with
no scout — and the three N0 misses (`breaking-signature` read as `docs`,
`convention-break` as `docs`, `general-dead-code` as `breaking_change`)
are what a reviewer does with a rubric and no statement of the
repository's own conventions.

C3 is the arm that answers RC1-394's leftover question, and the answer is
the wrong direction: a scout on top of a complete context held recall,
lost two categories, tripped **both** decoys (0/2, the only arm to do so),
and cost 2.5× C0 for it. The brief does not add evidence the reviewers
lacked; it adds a second, model-written account of the same repository
for them to over-read.


### Reference PRs of this repository, at their own heads

Cost, wall clock, findings after the verifier (warnings / nits), and the
scout's share of the cost. C0 is RC1-422's row for the same head.

| PR | N8: no context, scout 8 turns | N0: no context, no scout | C3: context, scout 3 turns | C0: context, no scout |
| --- | --- | --- | --- | --- |
| #35, 6 files | 24.5 ¢, 59 s, 0 / 3, scout 18.9 ¢ | **4.9 ¢**, 16 s, 0 / 0 | 18.4 ¢, 46 s, 0 / 2, scout 12.1 ¢ | 5.8 ¢, 13 s, 0 / 0 |
| #33, 10 files | 44.1 ¢, 82 s, 0 / 6, scout 33.3 ¢ | **10.7 ¢**, 31 s, 1 / 3 | 32.7 ¢, 52 s, 1 / 3, scout 19.4 ¢ | 12.4 ¢, 27 s, 0 / 3 |
| #39, 27 files | 65.3 ¢, 95 s, 3 / 5, scout 47.3 ¢ | **8.9 ¢**, 5 s, 0 / 0 | 45.9 ¢, 49 s, 1 / 1, scout 32.3 ¢ | 21.3 ¢, 58 s, 5 / 6 |

**Criterion 1 on the reference PRs: not met on any of the three.** The
rule asks for a kept, true, warning-or-above finding on two PRs that N0
lacks. N8 raised no warning at all on #35 or #33. On #39 it raised three,
and each is false against that head (`4632050`, the RC1-390 PR):

- *"the scout's budget-exhaustion path is untested"* — `tests/test_scout.py`
  at that head has `test_exhausted_turn_budget_forces_a_brief`,
  `test_file_read_budget_is_enforced` and
  `test_forced_call_without_a_brief_is_an_error`.
- *"the verifier's `shared_prefix` path is untested"* — `tests/test_verifier.py`
  has `test_shared_prefix_mode_reads_the_prefix_verbatim_and_sends_the_shared_tools`.
- *"`stage_latency_ms` is mutated after `verify_findings` returns, so the
  verifier's latency is lost"* — the verifier's copy holds the same dict,
  the write lands, and `test_stage_latency_is_recorded_for_the_verifier_too`
  asserts it.

The scout's brief did not make the reviewers see defects; it made the tests
reviewer confident about absences it could not check, which is the RC1-393
run-C failure mode (the absence rule was added to the verifier for it, and
the verifier let these three through). What N8 added that N0 lacked was nits:
a helper defined above its exception class, an unmentioned lock-file
exclusion in a tool description, `dispatch_tool` typed `Any`, and — the one
real point — `asyncio.run` inside the pipeline, which C0 also raises and
RC1-426 is filed for.

**C3 against C0** adds nothing of substance either. On #33, C3's one
warning (the tree API's `truncated` flag) is C0's third nit, promoted; on
#39, C3's warning (*"`explorable` is untested"*) is false — four test
references at that head — and C0's eleven findings on the same head include
every point C3 raised. C3 costs 2.2–3.2× C0 for it.

**One number to take seriously: N0 on #39.** With no context in the prefix
and no scout, the review of the 27-file PR returned nothing in five
seconds. That arm is harsher than production without a conventions file —
`--no-repo-context` removes the callers and tests grep as well, which a
repository without a conventions file still gets — but it shows what the
reviewers do with a bare diff on a large change: nothing. The remedy is the
one every repository the App reviews already has (RC1-396, RC1-402): a
conventions file, after which C0's review of the same head carries eleven
findings at a third of N8's cost. The scout is not the remedy; N8 on that
head is three false warnings for 65 ¢.


## Decision

**Removed.** Neither criterion was met on either sample. The scout found
no defect the review otherwise missed — recall was 13/13 on every arm and
its only warnings on the reference PRs were false — and it added noise
wherever it ran: three clean-diff findings and a tripped decoy with no
context, both decoys tripped on top of a complete context. Its one
measurable contribution, the category of a finding on a repository with no
conventions file, is what the conventions file provides at no model cost,
and every repository the App reviews has one.

What goes: `app/agent/scout.py` and its tests; the scout's prompt, context
note and `submit_brief` schema; the three scout-cap settings and
`max_files_read`; `router.scout_turns` and the plan's `scout` flag (now
`context`: whether Python gathers repository context, the gate the scout
used to share); the "Scout's brief" line of the shared prefix and the
reviewers' instruction to read it; `brief`, `scout_ran`, `tool_turns`,
`files_read` and `truncated` on `ReviewResult`; the `scout` stage in
pricing, spans and the per-review metric's tag; the model-tool surface in
`tools.py` and `remote_tools.py` (`TOOL_SCHEMAS`, `dispatch`) that only the
scout called; and the cache-marked conversation primitives in `reviewer.py`
that only the scout's loop used. The `scout` and `--no-scout` switches this
story added for the measurement go with it.

What stays: the deterministic context (conventions, callers, tests), the
router's reviewer choice, the fan-out, the merge and the optional verifier
(RC1-428). `RepoContext.complete` stays as telemetry on the span and the
metric. The read-and-grep methods on both tool backends stay for RC1-424 to
shape into the one interface.

REMOVAL_CHECK
