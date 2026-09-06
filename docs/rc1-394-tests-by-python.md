# RC1-394 — Tests for the changed paths by Python, then no scout: decision record

The fourth step of the multi-agent review sequence (RC1-387 → RC1-390 →
RC1-393 → RC1-394 → RC1-391). RC1-393 moved conventions and callers out of
the scout and into Python, and ended with the scout still more than half
of the bill at three turns, kept because its one remaining job — do tests
exist for the changed paths? — was a question only a model was answering.
This story answers it in Python, skips the scout when every kind of
evidence is in the prefix, and measures what that does to cost, recall and
the run-C failure mode. It also carries the cost-per-PR scope folded in from
RC1-395: the PR behind a review is now on the trace, and the fleet
dashboard lists the last reviews.

- **Ticket:** [RC1-394](https://hirereidcollins.atlassian.net/browse/RC1-394)
- **Flag:** none new. `REVIEW_MULTI_AGENT` (default off) gates all of it;
  `REVIEW_SCOUT_COMPLETE_TURNS` (0) is the scout's cap once the context is
  complete, `REVIEW_SCOUT_CONTEXT_TURNS` (3) stays the cap for a context the
  read budget cut short.
- **Code:** the tests section in `app/agent/context.py` (`tests_for`,
  `is_test_path`, `named_test_files`, `test_roots`), `paths()` on both tool
  backends, `router.scout_turns`'s third case, `MISSING_EVIDENCE_NOTE` in
  `app/agent/prompts.py`, `ABSENCE_RULE` in `app/agent/verifier.py`,
  `observability.annotate_review_identity`; `tests_found` and
  `context_complete` on `ReviewResult`.
- **Datadog, as code** (RC1-378, in `tpm-automation-platform/datadog/`): a
  list widget on the fleet dashboard `bwm-uny-qqs`, "Cost per review" group.
- **Runs:** corpus `pr-review-20260906T174937`; live PRs in the tables below.

## Context

RC1-393's record ended on two numbers. With the conventions file and the
callers list in the prefix, the 3-turn scout was still 52–69 % of a live
review (11–28 ¢ of 18–45 ¢), and the same review with no scout at all cost
6–21 ¢ and, on the corpus, held recall at 13/13. It kept the scout because
the corpus cannot see what a scout adds — every planted defect is in the
diff — and because on one PR of three the scout's brief produced a
`warning/breaking_change` the no-scout run did not. RC1-395's first live
point made the case again: on PR #41 the scout was 64 % of the review.

What the scout had left to do after RC1-393 was, by its own prompt, one
thing: find the tests for the changed paths. A test file is named for its
module by convention (`tests/test_<module>.py`, `<module>_test.py`,
`<module>.test.*`), and a test that touches a module names it in an import
or a call. Both are a grep, and the callers grep was already returning
rows from `tests/` — it just filed them as callers. So the tests question
is Python's too, and once it is, the context is complete on all three
kinds of evidence the scout was built to gather.

The second thing RC1-393 left open was the run-C mechanism: tool-less
reviewers reading "the scout found nothing" as "the file does not exist"
and filing blockers on it, which the verifier kept. With the scout gone the
brief is gone, but the context block is a bounded grep and can be read the
same way — a caller not in the list, a test not in the section.

## What was built

**Tests for the changed paths** (`context.tests_for`). For each changed
source file — not a test, not a lock file, not prose — Python takes the
module's name (`app/agent/context.py` → `context`; a package's `__init__`
is named for its directory), finds the test files named for it in the
repository's file list, and greps the test tree for the name as a whole
word, rows into the section up to 6 per file and 30 in all, 12 files. The
test tree is found from the file list: the shortest path prefix ending in
a test directory name (`tests`, `test`, `__tests__`, `spec`), most test
files first, three roots at most — `tests/` here, `packages/*/tests/` in
launch-planner. A repository whose tests are named but not gathered is
grepped whole and filtered by name. The callers grep's hits inside test
files move to this section, so a symbol whose only references are in
tests is now listed as having no callers, which is what it has.

The section renders as `Tests touching the changed paths (found by grep
under tests/; changed source files: …)` followed by `path: (test file named
for source)` and `path:line: text` rows, then `(no test references: …)` for
the changed files nothing reached, or `(no test files found in the
repository)`. Both tool backends serve it through one new call, `paths()`
— a walk locally, the tree at the PR head remotely, which the live path
already fetches for `list_dir` and `grep` and caches.

**A complete context skips the scout** (`router.scout_turns`). Three cases,
decided from a value after the context is built. The conventions file was
found, the callers search finished and the tests search reached an answer:
the context is *complete* and the scout gets `REVIEW_SCOUT_COMPLETE_TURNS`,
zero by default, which skips it. Conventions and callers answered but the
tests search cut off before it could start — the live path's read budget
spent, or the tree unreadable — the scout keeps RC1-393's short cap. No
conventions file, or the callers search cut off: the full cap, as before.
"Reached an answer" includes the answer that the repository has no tests,
and a diff with no source file a test could reference; it excludes a search
the budget stopped before it found anything. A search the row caps cut
short still counts — the scout has the same grep and the same budget and
would do no better.

**The run-C guard.** Every reviewer's evidence paragraph now ends with the
line only `repo_context` had: when the context did not reach what you
needed, judge from the diff alone and raise nothing about the missing
evidence itself. The reviewers are also no longer told that a scout has
explored the repository; they are told what the context above is and that
a brief follows it when a scout ran. On the multi-agent path only, the
verifier's instructions gain a rule: a finding whose only evidence is that
something was not found in the bounded context or the brief — a file,
module, caller or symbol it does not mention — is dropped; a changed path
the tests list names as untested is a fair `tests` finding at warning, not
a blocker. The single loop's verifier request is byte for byte what it was.

**Cost per PR** (from the RC1-395 wrap-up). The `pr_review` workflow span is
tagged `repo`, `pr` and `head_sha` as it opens, so the LLM Observability
trace explorer is the per-PR ledger — a PR's reviews across its pushes
share `repo` and `pr` and differ in `head_sha` — and the span already
carries `cost_usd` and the per-stage costs. The metric is untouched on
purpose: a `pr` tag on it would be a billable custom metric per PR, five
with percentiles. Datadog's dashboard API accepts the LLM Observability
stream as a list widget's source (`data_source: llm_observability_stream`,
confirmed by creating and deleting a probe dashboard), so the fleet
dashboard's "Cost per review" group gained a "Last reviews" list — one row
per `pr_review` span with repo, PR, mode, scout, cost, latency and head
sha — as code, pushed and pulled back drift-clean. No events were needed.
The widget's column bindings were set from the span's attribute names
(`@tags.repo`, `@metrics.cost_usd`, `@meta.metadata.mode`) and have not
been eyeballed in the UI in this session; if a column renders empty, the
field name is the thing to fix, not the source.

**Also fixed on the way.** The corpus run logged one "Event loop is closed"
per case: the multi-agent path builds an `AsyncAnthropic` for the fan-out
and left it to the garbage collector, which schedules its close on the loop
`asyncio.run` has already torn down. It is now closed inside that loop. An
injected client is the caller's.

## Method

Two measurements, same model (`claude-sonnet-4-6`), verifier on, all on
2026-09-06.

*The corpus with a checkout*: `python -m evals --repo-path .`, flag on,
default settings — run E — against RC1-393's runs A (single loop), C
(context + 3-turn scout) and D (context, no scout, the configuration this
story makes the default) on the same 16 cases.

*Live PRs at their own heads*: the RC1-393 harness — a worktree at the PR's
head SHA, `review_pull_request(multi=True, verify=True)`, priced with
`app.pricing.review_cost` — on the three RC1-393 PRs of this repository in
two configurations (default; `REVIEW_SCOUT_COMPLETE_TURNS=3`, the scout kept
on the complete context), plus one PR of launch-planner and two of the n8n
concert repository, default configuration, so the conventions-file and
test-layout heuristics ran outside this repository. PR #35 was run twice in
the default configuration because the first run returned no findings.

## Results

### Live PRs of this repository, default configuration

| PR | Single loop (RC1-393) | Context + 3-turn scout (RC1-393) | No scout (RC1-393, `CONTEXT_TURNS=0`) | **This story** (complete context, no scout) |
| --- | --- | --- | --- | --- |
| #35, 6 files, +161/−10 | 48.3 ¢, 96 s, 1 finding | 17.9 ¢, 46 s, 3 | 6.0 ¢, 15 s, 3 | **5.5 ¢ / 7.0 ¢**, 4 s / 12 s fan-out, 0 / 2 |
| #33, 10 files, +666/−81 | 70.0 ¢, 110 s, 4 | 31.0 ¢, 57 s, 5 | 11.6 ¢, 22 s, 3 | **12.9 ¢**, 27 s, 3 |
| #39, 27 files, +2,168/−35 | $1.10, 100 s, 3 | 44.7 ¢, 56 s, 7 | 21.4 ¢, 58 s, 8 | **20.3 ¢**, 60 s, 7 |

What Python put in each prefix: `CLAUDE.md` on all three; 10, 31 and 27
caller rows; 17, 30 and 30 test rows (the last two at the row cap, both
with `tests/test_<module>.py` named for every changed module). The context
was complete on every run and no scout call was made. Where the money went
on #39: the one prefix write 7.9 ¢ (39 %), three reviewers 3.0–3.8 ¢ each,
the verifier 2.5 ¢.

Against RC1-393's no-scout row the acceptance asked for at-or-below, the
result is parity: one PR under, one over by 1.3 ¢, one under, with the
second run of #35 a cent above the first. The tests section adds up to 30
rows to the prefix — about a thousand tokens written once at 1.25× and read
four times at 0.1× — and that is the 1.3 ¢. Run-to-run variance is the
same size: #35's two runs found 0 and 2 nits, at 5.5 ¢ and 7.0 ¢. (The
second run's wall clock, 148 s, was a 136 s verifier call; the fan-out took
12 s.)

### The scout kept on a complete context

`REVIEW_SCOUT_COMPLETE_TURNS=3`, the same three PRs — what a 3-turn scout
still adds when it is told conventions, callers and tests are done:

| PR | Cost | Scout | Turns, files | Findings |
| --- | --- | --- | --- | --- |
| #35 | 18.9 ¢ | 12.2 ¢ (64 %) | 3, 5 | 2 |
| #33 | 31.2 ¢ | 18.9 ¢ (61 %) | 3, 6 | 5 |
| #39 | 47.7 ¢ | 32.9 ¢ (69 %) | 3, 6 | 4 |

The same prices as RC1-393's row C, within a cent or two, so the tests
section changed nothing about what the scout spends — RC1-393's finding
again: a scout spends to its cap whatever it is handed. What it bought:
on #33, two findings more than the no-scout run, one of them a fair
`warning/tests` on the grep loop's untested budget branch; on #35 and #39,
the same count or fewer. The `warning/breaking_change` that RC1-393 credited
to the scout on #33 — a `RemoteRepoTools` handed to a loop typed
`RepoTools` — did not recur in either configuration today. One finding in
three PRs was a signal, not a measurement; it is not a signal that repeats.

### Other repositories

| PR | Repo | Conventions file | Context | Scout | Cost | Findings |
| --- | --- | --- | --- | --- | --- | --- |
| launch-planner #74, 6 files, +86/−8 | `CLAUDE.md` | found | complete: 0 callers, 14 test rows from `packages/planner-core/tests`, `packages/agents/tests` | skipped | **5.8 ¢**, 17 s | 3 nits, one a correct `tests` on the untested new truncation branch; 1 dropped |
| n8n concert #8, 4 files, +149/−2 | none | — | 1 caller, 13 test rows | ran, 8 turns, 4 files | **24.6 ¢**, 65 s; scout 16.0 ¢ (65 %) | 5 — a real one (`parent_id: 'undefined'` as a string literal), a hardcoded Datadog host; 3 dropped |
| n8n concert #1, 5 files, +647/−116 | none | — | 0 callers, 0 test rows (workflow JSON and a script) | ran, 8 turns | **48.5 ¢**, 83 s; scout 33.1 ¢ (68 %) | 6, five at warning; 2 dropped |

The heuristics held outside this repository: the test roots were found
under `packages/*/tests`, the named-file match found `test_<module>.py`
there, and the n8n repository's `tests/` was found for a Python change. The
n8n rows say something else. That repository has no conventions file, so
under RC1-393's rule the scout has the whole job and the full cap — and
at eight turns it is two thirds of a 25–49 ¢ review, the most expensive
reviews in this record by far. The fix is either side of the rule: a
`CLAUDE.md` in the two n8n repositories makes their reviews 6–20 ¢, or the
router could treat callers-plus-tests answered as enough to shorten the
scout when only the conventions question is open. Neither is this story's;
the first is a ten-line file and the second wants a measurement of its
own. Filed as the follow-up in the ticket.

### The corpus, five ways

Sixteen cases, each against a copy of this checkout, verifier on. RC1-393's
runs A, C and D for comparison; E is this story at its defaults.

| | A: single loop | C: context + 3-turn scout | D: context, no scout | **E: this story** |
| --- | --- | --- | --- | --- |
| Run | `…T155401` | `…T155147` | `…T155749` | `…T174937` |
| Cases passing | 16 / 16 | 13 / 16 | 14 / 16 | 13 / 16 |
| Recall | 13 / 13 | 13 / 13 | 13 / 13 | **13 / 13** |
| Categorized correctly | 13 / 13 | 12 / 13 | 12 / 13 (`convention-break`) | 11 / 13 (`unbounded-scan` → five other categories; `general-dead-code` → `error_handling`, as in C) |
| Precision | 2 / 2 | 0 / 2 (two blockers on absent files) | 1 / 2 | 1 / 2 — `deliberate-broad-except` drew `warning/tests` on its untested branch, as in D; **no blocker on either case** |
| Clean diff | 1 nit | 1 nit | 1 nit | 1 nit |
| Verifier | 5 dropped, 1 downgraded | 24 dropped, 4 downgraded | 21 dropped, 2 downgraded | 19 dropped, 4 downgraded |
| Merge | — | — | 4 folded, 1 off-scope | 2 folded, 3 off-scope |
| Cost | $1.26 (7.9 ¢ / case) | $1.63 (10.2 ¢) | $0.68 (4.2 ¢) | **$0.71 (4.4 ¢)** |
| Scout | — | $0.85 (52 %) | — | — (0 cases: context complete on 16) |
| Wall clock per case | 23–210 s, median 45 | 33–50 s | 11–24 s | **12–23 s, median 17** |

Recall held with no model exploring on any case; the context was complete
on all sixteen (`CLAUDE.md`, 13 caller rows, 39 test rows — the corpus's
invented modules have no tests, so most cases got `(no test references…)`,
which is a true statement about the fixture). Cost and latency are run D's
within noise; the extra 2.7 ¢ over sixteen cases is the tests section in
sixteen prefixes. The one precision miss is the one every multi-agent run
has had since RC1-390 — the untested new branch in
`deliberate-broad-except` drawing a fair warning — and it is a warning,
which the acceptance criterion allows. Run C's two blockers on absent files
did not recur, though the corpus cannot say whether the guard or the
missing brief is the reason: D had no brief either and no blockers. The
guard's test is a live PR whose context names a caller the grep did not
reach; none of the six today produced a finding about absent evidence.

`unbounded-scan` is new as a categorization miss: the reviewers filed the
planted defect as `error_handling`, `pr_drift`, `pythonic`, `security` and
`tests` but never `infra_scalability`, the change-intent reviewer's
category. Three reviewers found the defect and the one whose category
names it did not. The same shape as `convention-break` in RC1-390 and
RC1-393 — one defect, several categories, the wrong survivor — on a
different case; see the deferral below.

## Decision

**The default is the no-scout review, and it stands.** A PR whose
repository has a conventions file now gets a review with no scout call,
the prefix carrying conventions, callers and tests, at 5–20 ¢ on this
repository's PRs — a tenth of the single loop's 48 ¢–$1.10 and 40–45 % of
RC1-393's default — in 4–60 s, with recall at 13/13 on the corpus and no
blocker on either precision case. The scout kept on top of a complete
context costs 3× the review for one extra finding on one PR of three,
and the one finding RC1-393 credited it with did not recur. Reid's
constraint — know the cost and keep it down — is met with the scout off.

**Cross-category dedupe: deferred, explicitly.** The RC1-393 question was
whether the merge should fold one defect filed three ways under different
categories. Today's data: the merge folded 2 findings and discarded 3
off-scope across sixteen cases, and the two categorization misses are both
"the right defect, the wrong category survived the verifier". A
cross-category fold needs a rule for which category wins, and the two
cases point in different directions — `convention` should have beaten
`docs` and `pr_drift`; `infra_scalability` should have beaten
`error_handling` and `security` — so any fixed preference order would fix
one and could break the other. The verifier already has the instruction to
keep the finding whose category names the defect best; the corpus says it
does not reliably do so. The honest next step is a measurement of the
verifier's choices across more cases, which is RC1-391's harness question
as much as this one's, and it is left there.

**What to watch when the flag goes on** (`fly secrets set
REVIEW_MULTI_AGENT=1`, still Reid's call; the code default stays off):

- `context conventions=… callers=… tests=… complete=True scout_turns=0` and
  no `scout_done` line per review, and `review_cost mode=multi` under the
  dashboard's warn line.
- `repo_tools api_calls=` on the webhook log: the tests grep spends the
  per-review API budget on the test tree (up to 30 files) after the callers
  grep has spent it on the smallest 30 files of the repository. Sixty is
  the budget; a `tests_searched=False` in the `repo_context` line means it
  ran out before the tests search started and the scout got three turns.
- Findings whose evidence is an absence — "no caller", "no test named",
  "file not in the context" — at any severity above nit. The reviewer note
  and the verifier rule exist for these; the corpus could not exercise them.
- The "Last reviews" list on the fleet dashboard: one row per review, cost
  and latency filled. An empty column is a field-name problem.

## What this story changed regardless of the flag

- `paths()` on both tool backends, for Python callers.
- `tests_found` and `context_complete` on `ReviewResult`; `tests_found` and
  `context_complete` on the workflow span; the corpus record's `context`
  observation carries `tests` and `complete`.
- The `pr_review` span is tagged `repo`, `pr`, `head_sha` on both paths.
- The single loop's request shape and its verifier's request are untouched;
  flag off is byte for byte what it was.
- Spend on the measurements in this record: about $3.
