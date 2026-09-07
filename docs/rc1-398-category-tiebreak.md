# The verifier's category tie-break — measurement and decision (RC1-398)

**Decision: no category preference order. The verifier's choice between two
categories for one defect is not a category choice at all — it keeps both
findings two times in three, and when it folds, the finding listed first
wins three times in four. The list order is the reviewer order, fixed by the
plan, which is why every "wrong category survived" in the records lost to the
same neighbours. The fix candidate is one sentence of the verifier's
instructions, measured below with the same probe; it is not shipped here
(see "What ships, and what does not").**

- **Ticket:** [RC1-398](https://hirereidcollins.atlassian.net/browse/RC1-398)
- **Code:** `evals/boundary.py` (eleven boundary cases: one defect, two
  categories, the pair of findings a first pass would file), `evals/tiebreak.py`
  (the scoring), `scripts/measure_tiebreak.py` (`history` / `probe` /
  `pipeline`), `tests/test_tiebreak.py`. **No change to the agent**, on either
  path; the single loop's request is byte for byte what it was.
- **Spend:** $2.10 across 256 billed calls (probe ×2, pipeline, smoke tests).

## The question

The multi-agent merge folds two findings only when file, line *and* category
agree. Two findings on one line under two categories both reach the verifier,
whose instruction reads:

> When two findings describe the same defect, keep the one whose category
> names it best, at the higher of their severities, and drop the other;
> never drop a finding as redundant unless a kept finding states the same
> defect.

RC1-393, RC1-394 and RC1-391 each recorded "the right defect, the wrong
category survived the verifier", on a different case each run, and RC1-394
deferred a cross-category dedupe rule because the known misses pointed in
opposite directions. This story measures the choice before anyone writes
the rule.

## 1. What the eval store already said

`measure_tiebreak.py history` reads every `pr-review` run record in the
store, and for each planted case with the verifier on classifies the
categories filed *on the plant* (the corpus's evidence match) as kept or
dropped.

| Outcome for the intended category | Case-runs |
| --- | --- |
| survived the verifier | 123 |
| filed on the plant, dropped by the verifier, another category kept | 9 |
| plant found, but never under the intended category | 3 |
| plant not found | 2 |

137 verified case-runs over 12 runs, 9 of them multi-agent. All nine drops
are multi-agent, and they are four cases: `convention-break` ×4,
`general-dead-code` ×3, `unbounded-scan`, `unpinned-dependency`. What the
intended category lost to: `convention → docs` 3, `convention → pythonic` 3,
`general → error_handling` 3, `infra_scalability → security` 1,
`dependencies → security` 1, `dependencies → convention` 1, `convention →
pr_drift` 1, `convention → tests` 1.

Two things the store cannot say: which finding was listed first (it keeps
messages, not indexes), and why (the drop reasons are logged, not stored).
The Fly logs hold a hundred lines, none of them a verifier drop, so live
reviews add nothing here. Hence the boundary cases.

## 2. The boundary cases

Eleven, in `evals/boundary.py`, each a diff whose one defect legitimately
fits two of the rubric's dimensions, with the two findings written out —
same line, same severity, each in its category's terms — and the intended
category named with the rubric's words for why. The pairs are the ones the
history produced, plus their reverses:

| Case | Intended | Rival |
| --- | --- | --- |
| bare-except-swallows | error_handling | pythonic |
| loop-where-repo-uses-comprehension | convention | pythonic |
| os-environ-under-comment | convention | docs |
| os-environ-against-description | convention | pr_drift |
| select-star-every-account | infra_scalability | security |
| retry-forever | error_handling | infra_scalability |
| dead-code-after-return | general | error_handling |
| git-dependency-on-branch | dependencies | security |
| docstring-and-description-say-three | docs | pr_drift |
| required-kwarg-added | breaking_change | tests |
| headers-in-error-log | security | error_handling |

They are not in `corpus.CASES`; adding them would change the recall
denominator every trend row is compared on.

## 3. The probe: the verifier alone

`probe` hands each pair to `verify_findings` exactly as the multi-agent path
does — the shared prefix (PR, the conventions page where the case needs one,
a skipped-scout brief), the reviewers' tool list, the absence rule — with
the pair as the whole first pass, in both orders, five times each. 110
calls, 43 ¢, 3.2 s a call.

**Kept: both 72, intended alone 27, rival alone 11.** Seven of the eleven
pairs came back "both" on all ten calls: two findings, one line, one
defect, and the verifier read them as two defects because each message had
its own angle (memory versus PII; the docstring versus the description; a
migration versus a missing test).

Of the 38 calls that kept exactly one:

| Order | Intended kept | Rival kept |
| --- | --- | --- |
| intended listed first | 17 | 0 |
| rival listed first | 10 | 11 |

The first-listed finding won 28 of 38. Every reason for a rival-kept verdict
reads "finding [0] already covers …", and [0] was the rival in every one of
them. Two pairs resisted the position: `security` beat `error_handling` on
all ten calls (headers in an error log), and `error_handling` was never
dropped for `infra_scalability` (retry forever). The rest split by order.

Agreement with the intended category, per pair: `security vs error_handling`
100 %, `error_handling vs infra_scalability` 50 %, `general vs
error_handling` 50 %, `error_handling vs pythonic` 40 %, `convention vs
pythonic` 30 %, every other pair 0 % — not because the rival won but because
both survived.

## 4. The pipeline: the whole review

`pipeline` runs each case through `review_pull_request(multi=True,
verify=True)` three times over a checkout holding only the conventions page
the case needs. 33 reviews, $1.10, 3.3 ¢ and 12–34 s each, scout skipped on
every one (the context was complete).

The pair was both filed on the plant in 20 of 33 reviews — the overlap is
the normal case on these diffs, not the exception. Of those 20: both kept 9,
rival dropped and intended kept 5, intended dropped and rival kept 6. The
survivor in the six was the diff-local reviewer's category every time
(`pythonic`, `docs`, `security`, `general`), and the drop reasons name
"finding [0]" — the merge lists reviewers in plan order, `diff_local` first,
so its findings are always [0] and [1]. Over all 33: intended survived 25,
verifier-dropped 7, never filed 1.

That is the history, reproduced with the mechanism visible: `convention`
(repo_context) and `infra_scalability` / `pr_drift` / `dependencies`
(change_intent) always sit below `diff_local`'s `docs`, `pythonic`,
`security` and `error_handling` in the list, and the list order is the
tie-break.

## 5. The candidate sentence, measured

`probe --rule candidate` swaps the tie-break sentence for this one, for the
run only (the shipped module text is restored after; a test checks it):

> When two findings point at the same line and the same defect — even if
> they describe it from different angles — keep exactly one: the one whose
> category is the rubric dimension that names the defect, at the higher of
> their severities, and drop the other. Their order in the list above means
> nothing: do not keep the earlier one because it came first, and do not
> keep both because their wording differs. Never drop a finding as redundant
> unless a kept finding states the same defect.

Same 110 calls, 53 ¢.

| | Shipped sentence | Candidate sentence |
| --- | --- | --- |
| both kept | 72 | 10 |
| intended kept alone | 27 | 66 |
| rival kept alone | 11 | 34 |
| first-listed won, of the decided calls | 28 / 38 (74 %) | 78 / 100 (78 %) |

The candidate does the folding: one pair (`select-star-every-account`) still
came back "both" every time, the other ten folded on every call. It does
not do the choosing. Told in so many words that order means nothing, the
verifier kept the first-listed finding at the same rate as before; four
pairs (`convention` against `pythonic`, `docs` and `pr_drift`; `docs`
against `pr_drift`) went exactly 5–5 by order, and the reasons still read
"finding [0] already covers …". Where the candidate produced a consistent
choice regardless of order, it was a preference for the more specific
dimension: `dependencies` over `security` and `breaking_change` over
`tests` (10/10 each), `security` over `error_handling` (10/10),
`error_handling` over `infra_scalability` (8/10) — and `error_handling`
over `general` (8/10), the wrong way by the corpus's labelling and the
right way by the rubric's own "general: anything that doesn't fit a
specific dimension".


## Decision

**A preference order between categories is declined.** The data does not
show a category preference to encode; it shows a position preference, and a
Python order between, say, `convention` and `docs` would be a rule written
over an artifact of the plan's reviewer order. Reversing that order would
move the same misses onto the other reviewers' categories.

**A verifier-instruction change is declined for the label too.** The misses
do cluster on the prompt — the shipped sentence leaves open whether two
differently-worded findings are "the same defect", and says nothing about
list order — but section 5 shows the sentence can make the verifier fold
and cannot make it choose: told that order means nothing, it followed order
at the same rate. The label choice, where the model has one, is "the more
specific dimension", which is the rubric's own instruction for `general`
and is not a rule this repository needs to write down.

**No rule, then, for the category** — measured and declined with the
numbers, the way RC1-391 closed the LangGraph question. The one thing the
data does recommend is unrelated to the label: the candidate sentence
folds the duplicate pair on ten of eleven cases where the shipped one folds
it on four, and a duplicate is what the author sees most often today.

**What the label costs the reader** is the part the ticket asked to weigh.
When the pair is folded the wrong way, the author still sees the defect,
with the same line and a comparable fix, under a neighbouring label; the
reader's action does not change. When the pair is *not* folded — the
common outcome — the author sees the same defect twice. The duplicate is the
larger cost, and it is the same sentence that fixes both.

## What ships, and what does not

Ships here: the cases, the scoring, the script, the tests, this record. The
agent is unchanged on both paths.

Does not ship here: the candidate sentence, and it would ship for the fold,
not for the label. It is a prompt change to the production verifier, and
the RC1-387/390 rule is that a prompt change goes in flag-gated with a
corpus run either side; that is a story of its own, filed from this one,
with the probe as its instrument (43–53 ¢, six minutes) and the corpus as
its regression check.

## Follow-ups

- Ship the candidate tie-break sentence behind a flag, for the duplicate
  fold: corpus run either side, probe before and after, and the clean-diff
  noise figure watched — a verifier that folds more readily may fold two
  real findings.
- The eval store's per-case observations keep the verifier's dropped
  messages but not the verdict reasons or list indexes; recording both
  (`_verifier_observations`) would have answered section 1 without section 3.
