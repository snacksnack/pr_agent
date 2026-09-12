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

_(filled in after the runs)_
