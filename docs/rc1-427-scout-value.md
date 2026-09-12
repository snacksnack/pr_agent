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

RUNS

## Decision

DECISION
