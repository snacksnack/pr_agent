# RC1-428 — The verifier's permanent policy: decision record

The verifier (RC1-387) is a second, tool-less call that re-reads the merged
findings against the same prefix the reviewers saw and may drop or
downgrade them; Python applies the verdicts under rules the model cannot
override. It shipped behind `REVIEW_VERIFY_FINDINGS`, the flag went on in
Fly on 2026-09-06, and every production review since has run with it — but
the code still carries both paths, a model override, an own-prefix request
shape nothing sends, and a tie-break sentence RC1-398 measured and RC1-400
was filed to ship behind a second flag. This story chooses one policy —
the verifier as an ordinary stage, or no verifier — and removes the rest.

- **Ticket:** [RC1-428](https://hirereidcollins.atlassian.net/browse/RC1-428);
  RC1-400 (the duplicate-fold sentence) is resolved inside it.
- **Baseline:** RC1-427's post-removal corpus run `pr-review-20260912T022357`
  (verifier on, shipped sentence) and its reference-PR rows.
- **Instrument:** `python -m evals --repo-path .` with the flag either way,
  `scripts/measure_pr.py 35 33 39` with and without `--verify`,
  `scripts/measure_tiebreak.py probe --runs 5` (110 verifier calls over the
  eleven boundary pairs, both orders).

## What changed before the runs

Two things, both in the branch before any arm ran, so every arm measures
the same code:

1. **The final instruction text.** RC1-398's candidate sentence — a pair on
   one line and one defect is one finding, whatever the wording, and list
   order is not a tie-break — replaces the shipped one, and RC1-394's
   absence rule is part of the instructions rather than appended per call.
   The verifier has one request shape: the reviewers' prefix, their tools,
   `tool_choice: any`. The RC1-387 shape that rendered the PR itself is
   gone; nothing sent it (the tie-break probe sends the shared one).
2. **A context bug the RC1-427 record handed here.** The reference-PR #39
   review carried three warnings that `router.py`, `scout.py` and the
   verifier had no tests, all false. The record read it as the reviewer
   over-reading a capped list; it was not. `context.tests_for` counted a
   hit only when its row fit under the 30-row cap, so a changed file whose
   rows the cap refused was rendered as **"(no test references: …)"** —
   a false statement in the prefix, which a tool-less reviewer repeats and
   which the verifier's absence rule is written not to drop ("a changed
   path the tests list names as untested is a fair 'tests' finding").
   Fixed: a hit the cap refuses is still a test that exists; such files are
   rendered as "(tests exist but did not fit under the cap for: …)" and are
   never in `untested`. No verifier instruction could have fixed this, and
   its removal is not a point for either policy.

## Go/no-go, declared before the runs

Both policies are measured on the same code with the flag as the switch.
Arms: the sixteen-case corpus against a copy of this checkout, verifier off
(**OFF**) and on with the final text (**ON**); the three reference PRs #35,
#33 and #39 at their own heads, both ways; and the boundary probe with the
final text. Every drop and downgrade the ON arms make is read against the
fixture or the head and classed as one of: **fold** (a kept finding states
the same defect), **unsupported** (the diff does not support it), or
**true and distinct** (a real defect no kept finding states).

**The verifier stays, as a permanent stage, only if all of these hold:**

1. **No recall harm.** ON recall is in the recorded band (12–13 of 13) and
   no planted defect is found before the verifier and lost after it: every
   planted case that has a finding on the plant before the pass still has
   one after.
2. **It is right four times in five.** Of every drop and downgrade across
   the ON corpus and the three ON reference PRs, at most one in five is
   true-and-distinct, and none of those is a blocker. RC1-387 recorded one
   such drop in 55 verdicts; RC1-390 one more; if the rate is now worse
   than one in five the pass is removing what the author needs.
3. **It earns its call.** ON posts fewer duplicate-or-unsupported findings
   than OFF: across the corpus's clean diff and two decoys plus the three
   reference PRs, the count of findings a reader would call a duplicate or
   unsupported is lower in ON than in OFF, and there is no blocker on the
   clean diff or the decoys in ON.
4. **The fold works** (RC1-400's acceptance, resolved here): the probe with
   the final text keeps both findings of a boundary pair on at most 15 of
   110 calls (the shipped sentence: 72; the candidate in RC1-398: 10), and
   ON adds no finding to the clean diff and no blocker to the decoys.
5. **Cost and latency.** The verifier's share of the review is at or under
   25 % on the corpus and on each reference PR (RC1-387 accepted +23 %
   when it wrote its own prefix; it now reads the shared one), and ON's
   median wall clock per corpus case is within 10 s of OFF's.

**Removal is the outcome if 1, 2, 3 or 4 fails.** Cost or latency alone
over the line is recorded and the stage stays, since the last two numbers
have never been close. If the verifier goes, the duplicate a reader sees
most often — one defect under two categories, same line — is left to the
merge, and the record says what a Python fold at file and line would have
done to the OFF arm's findings.

If it stays: `REVIEW_VERIFY_FINDINGS`, `REVIEW_VERIFY_MODEL`, the `verify`
switch on `review_pull_request` and `measure_pr.py --verify` go; the eval
subject version always carries the verifier's hash; `verified` on the
result and the metric tag keep their meaning (false when the review had
nothing to verify and made no call).

## What the runs said

All five arms ran on 2026-09-12 between 02:52 and 03:03 UTC on
`claude-sonnet-4-6`, from commit 9f5f8f4, for $2.65 in total.

### Corpus (sixteen cases, checkout)

| | OFF: no verifier | **ON: verifier, final text** |
| --- | --- | --- |
| Run | `…T025533` | `…T030153` |
| Recall | 13 / 13 | **13 / 13** |
| Cases passing | 15 / 16 | 15 / 16 |
| Clean diff | 1 nit (`tests`) | **0 findings** |
| Precision | 1 / 2 (`deliberate-broad-except` drew `warning/tests`, as in every pipeline run) | 1 / 2 (the same case, the same finding) |
| Findings posted, off the plant | 30 | **20** |
| Verifier | — | 20 dropped, 3 downgraded |
| Cost | $0.58 (3.6 ¢ / case) | $0.69 (4.3 ¢), verifier $0.11 (**17 %**) |
| Wall clock per case | 7–17 s, median 13 | 4–25 s, median **17** |

**Criterion 1, no recall harm: met.** Every planted defect was found in
both arms, and in ON every planted case still had a finding on the plant
after the pass. Where the verifier dropped a finding on the plant it was
the second category of the same defect (see the classification below).

**Criterion 4, the fold: met on the corpus.** ON put nothing on the clean
diff and no blocker on either decoy. On `benign-test-secret` the reviewers
raised `blocker/leaked_secret` on the throwaway signing key — the one
category that gates a merge, on the one case built to test it — and the
verifier dropped it. OFF's reviewers happened not to raise it this run;
RC1-427's arms show they do.

### The twenty drops, read against the fixtures

| Class | Corpus drops | Which |
| --- | --- | --- |
| **Fold** — a kept finding states the same defect | **16** | the hardcoded `DATABASE_URL` under `convention` (the `leaked_secret` blocker kept); `pythonic`, `pr_drift` and `convention` restatements of the breaking signature, the swallowed exception, the `os.environ` read, the index loop, the `hmac.compare_digest` regression, the contradicting docstring, the dead code; `pyyaml` unpinned under `convention` (kept under `dependencies`); five "missing `from __future__ import annotations`" findings each beside a kept one saying the same |
| **Unsupported** — the diff does not support it | **3** | a SQL-injection warning on `SELECT * FROM accounts` conditional on "if `db.query` ever interpolates"; the decoy blocker on `benign-test-secret`; a nit that `hmac.new` is "legacy" |
| **True and distinct** — a real defect no kept finding states | **1** | `unbounded-scan`: "`render` is called but never imported; `NameError` at runtime". True of the four-line fixture, at warning. |

The three downgrades are not classed: the eval record keeps the verifier's
counts and dropped messages but not its verdict reasons or which finding a
downgrade landed on (RC1-398's follow-up, still open). None of the three
touched a planted finding, since every plant survived at or above its
minimum severity.

### Reference PRs of this repository, at their own heads

| PR | OFF: no verifier | **ON: verifier, final text** |
| --- | --- | --- |
| #35, 6 files | 5.9 ¢, 13 s, 4 nits | **3.0 ¢** (verifier 0.6 ¢, 20 %), 11 s, 1 nit; 0 dropped |
| #33, 10 files | 10.9 ¢, 19 s, 6 nits | **6.6 ¢** (1.5 ¢, 23 %), 22 s, 5 nits; 1 dropped, 1 downgraded |
| #39, 27 files | 15.7 ¢, 37 s, 13 (7 warnings / 6 nits) | **10.3 ¢** (2.4 ¢, 23 %), 41 s, 7 (3 / 4); 5 dropped |

ON is cheaper than OFF on every PR despite the extra call: the reviewers
wrote fewer findings this run (output tokens are the expensive ones), which
is the run-to-run spread RC1-390 recorded, not the verifier. The verifier's
share, 20–23 %, is the number to hold it to.

The seven verdicts on the PRs, read against the heads:

- **Fold, 3:** the `asyncio.run` hazard on #39 under `infra_scalability`
  (kept under `error_handling`); two unused-constant nits on `prompts.py`;
  a vaguer nit on the `remote_tools` budget guard beside a precise one.
- **Unsupported, 3:** a warning that an exception escapes `scout.py`'s
  dispatch, when `RepoTools.dispatch` returns error strings by convention;
  a nit about `# ---` section separators; a nit that `router.py` lacks a
  test for `is_manifest("poetry.lock")`, dropped with the reason *"tests
  exist for router.py but the context was capped — absence … is not
  evidence of absence"* — the absence rule reading the new cap line.
- **Downgrade, 1, reasonable:** the tree-truncation warning on `github.py`
  to a nit, because the PR description acknowledges it.
- **True and distinct: 0.**

**Criterion 2, right four times in five: met.** Across the corpus and the
PRs the verifier made 26 drops; one is true-and-distinct (4 %), at warning.
Of the 30 verdicts including downgrades, the three unclassed corpus
downgrades could not move the rate past 13 %.

**Criterion 3, it earns its call: met.** What a reader would call a
duplicate or unsupported in the posted review, OFF against ON:

| | OFF | ON |
| --- | --- | --- |
| Clean diff | 1 | 0 |
| `benign-test-secret` | the same "`__future__` import" nit twice | one garbled nit (`hmac.new` "is not a valid Python API … actually valid") the verifier let through |
| #35 | a nit about a docstring on the wrong function | 0 |
| #33 | 0 | 0 |
| #39 | `asyncio.run` three times under three categories; `is_manifest` three times; two false `tests` warnings (`scout.py`'s budget path and `verify_findings`' shared-prefix path both have tests at that head) | `asyncio.run` once; one false `tests` warning (`verify_findings`' new parameters — tested at that head) |
| Total | **≥ 10** | **2** |

What the verifier does not do is also in that table: it let one false
`tests` warning through on #39 and a garbled nit on the decoy. Its verdicts
are right; its coverage is not total. With the context fix, the #39 prefix
no longer says `router.py` and `scout.py` are untested (they were "tests
exist but did not fit under the cap"), and the three false warnings the
RC1-427 record handed here did not recur in either arm.

### The boundary probe, final text

110 calls, $0.50, 3.1 s a call.

| | RC1-398, shipped sentence | RC1-398, candidate | **This story, final text** |
| --- | --- | --- | --- |
| both kept | 72 | 10 | **10** |
| intended kept alone | 27 | 66 | 69 |
| rival kept alone | 11 | 34 | 31 |
| first-listed won, of the decided calls | 74 % | 78 % | 83 % |

**Criterion 4 on the probe: met.** The fold rate RC1-398 measured for the
candidate sentence holds with the absence rule folded into the same text:
ten of eleven pairs fold on every call (`select-star-every-account` keeps
both on five of ten, as before). The label is still positional, as RC1-398
found and declined to rule on; the pairs where the choice is consistent
regardless of order are the same ones (`convention` over `pr_drift`,
`security` over `error_handling`, `breaking_change` over `tests`).

**Criterion 5, cost and latency: met.** Verifier share 17 % on the corpus
and 20–23 % on the PRs, under the 25 % line; median corpus wall clock
17 s against 13 s, within the 10 s allowance.

### Production, for the record

The flag has been on in Fly since 2026-09-06. The per-review metric for the
six days to 2026-09-12 shows 49 reviews: 26 tagged `verified:true` and 23
`verified:false` — the latter are reviews with no findings, on which the
pass made no call. That is the "skip naturally for clean reviews" behavior
the ticket asks for, already in place; the removal of the flag changes no
production request.

## Decision

**The verifier stays, as a permanent stage.** All five criteria held on
the arms declared for them: recall 13/13 with every plant surviving the
pass; one true-and-distinct drop in 26; ten-plus duplicate-or-unsupported
findings in the OFF reviews against two in ON, with the decoy blocker
removed and the clean diff left empty; the boundary pair folded on 100 of
110 calls; a 17–23 % share of the review's cost and four seconds of median
wall clock.

Its instruction text is the one measured here: RC1-398's fold sentence
(RC1-400 is resolved by this story, without a flag) and RC1-394's absence
rule. Its request shape is the reviewers' prefix, tools and `tool_choice`.
There is no flag, no model override, no `verify` switch: a review with
findings is verified, a review without makes no call.

**What went:** `REVIEW_VERIFY_FINDINGS` and `REVIEW_VERIFY_MODEL`
(`config.py`, `.env.example`, README), the `verify` parameter of
`review_pull_request`, the own-prefix request shape and `absence_rule`
switch of `verify_findings` (it now takes the prefix, tools and tool
choice, and no pull request), `measure_pr.py --verify`, the tie-break
swapping in `evals/tiebreak.py` and `measure_tiebreak.py --rule` with the
`SHIPPED`/`CANDIDATE` constants, the flag-conditional segment of the eval
subject version (`+verify-sha256:` is always present; rows without it are
flag-off rows), and the tests that pinned each. The architecture diagram's
"Optional verifier" is "Verifier", solid.

**What stayed:** `verified`, `verifier_dropped`, `verifier_downgraded`,
`verifier_usage` and `verifier_model` on `ReviewResult` (RC1-429 owns the
telemetry split; `verifier_model` still prices eval-store rows from before
the override went), the `verified` tag on the per-review metric (false
means nothing to verify), the `verifier` stage in pricing and spans, and
the boundary cases with the probe as the instrument for the next
instruction change.

**Not a point for either policy, fixed here anyway:** the tests-list cap
bug in `context.tests_for` (above). It was the RC1-427 record's case for
this story and turned out to be Python's.

## After the merge

- `fly secrets unset REVIEW_VERIFY_FINDINGS` on the app when convenient;
  the setting is ignored either way (`tests/test_config.py` pins that).
- The eval store's subject version for the default configuration now
  carries `+verify-sha256:d1466a1a1b9d` (the final text); RC1-427's rows carry the
  shipped sentence's `d2ac2909…`, and `…T025533` is the one flag-off row
  of the current pipeline.
- Still open from RC1-398: record the verifier's verdict reasons and the
  index a downgrade landed on in the eval observations, so the next
  reading of its drops does not need a capture harness beside the run.
