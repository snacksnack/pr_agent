# RC1-393 — Cheap exploration: conventions file, callers by grep, a scout cap that follows: decision record

The third step of the multi-agent review sequence (RC1-387 → RC1-390 →
RC1-393 → RC1-391). RC1-390 built the scout and the three reviewers and
left the flag off because the scout cost more than the whole single loop.
This story moves the repeated part of exploration out of the model and into
Python, measures what that does to the scout, and revisits the decision.

- **Ticket:** [RC1-393](https://hirereidcollins.atlassian.net/browse/RC1-393)
- **Flag:** none new. `REVIEW_MULTI_AGENT` (default off) gates all of it;
  `REVIEW_SCOUT_CONTEXT_TURNS` (3) is the scout's cap once the context is in
  hand.
- **Code:** `app/agent/context.py` (new), `router.scout_turns`, the seed and
  prefix wiring in `app/agent/multi.py` and `scout.py`, `read_text` on both
  tool backends, the scout note in `app/agent/prompts.py`; `--repo-path` and
  `--no-repo-context` on `python -m evals`.
- **Runs:** see the Results section.

## Context

RC1-390's record ended with the number that decided it: on the one corpus
case with a repository to explore, the scout spent 3.5 ¢ and 24 s, more
than the single loop spent reviewing the same case. It also said where that
money went — establishing conventions, finding callers — and that both are
properties of the repository, re-derived on every PR. Reid's question during
that build was whether the convention check, which has to read the repo,
could stop being a repeated process. This story is the answer the ticket
laid out: the conventions file into the prefix by Python, callers by grep,
and a cached per-repo brief only if the first two leave the scout still the
cost.

The constraint is unchanged: know the cost and keep it down. The measurement
RC1-390 could not make — exploration cost on every case, not on one — is
the one this story exists to make, so the corpus gained a way to run every
case against a checkout.

## What was built

**Conventions file** (`context.conventions_file`). Python reads the first of
`CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md` (and the two usual places for the
last) that exists, through the same tool backend the model uses, so the live
path pays one Contents call and the CLI one file read. The file is split at
its headings and the sections a reviewer needs — conventions, style, testing,
layout, in that priority — go first, then the rest in file order, under a
6,000-character cap. This repository's own `CLAUDE.md` is 8,600 characters
with the conventions section two-thirds of the way down; a front-truncation
would have cut exactly the part that matters, which is why sections are
ranked rather than the file clipped.

**Callers by grep** (`context.changed_symbols`, `context.callers`). From the
added and removed lines of every hunk, Python takes the `def`, `class` and
column-zero constant names (Python and the JS forms), skipping dunders,
`test_*` and anything defined in a test file, and greps the repository for
each as a whole word. Definition lines are dropped; what remains — calls,
imports, references, test uses — goes into the prefix as `path:line: text`,
capped at 6 rows per symbol, 40 in total, 12 symbols. A symbol with no hits
is listed as unresolved; a symbol the caps or the API budget stopped the
search from reaching is listed as not searched, because "not searched" and
"no callers" mean different things to a breaking-change reviewer. On the live
path the grep is RC1-364's bounded remote grep, changed files first, under
the per-review API budget, and the files it fetches stay cached for the
scout.

**Where it goes.** The rendered block sits in the shared prefix between the
PR and the scout's brief, so the three reviewers and the verifier read it at
cache-read price, and in the scout's seed with a note that says the
conventions file and the callers are already there and its job is what they
do not say. When there is no conventions file and the diff defines nothing,
nothing is added and the prefix is byte for byte RC1-390's.

**The scout's cap follows the context** (`router.scout_turns`). This is the
part the ticket did not anticipate and the first measurement forced. With
the conventions file and callers in hand, and the search not cut off, the
scout's cap drops from `REVIEW_SCOUT_MAX_TURNS` (8) to
`REVIEW_SCOUT_CONTEXT_TURNS` (3). Otherwise it keeps the full cap. Python
decides, from a value, after the context is built. Why this exists is in the
Results.

**Measurement.** `python -m evals --repo-path PATH` copies the checkout into
each case's temporary directory (the n8n case's files written over it), so
the scout runs on all 16 cases instead of one; `--no-repo-context` is the
control that runs the same code with the context left out. Each is its own
subject version in the store (`+checkout`, `+no-context`), never averaged
with a diff-only run. The record carries the conventions file found and the
caller count per case.

## Method

Three runs, same corpus (16 cases), same model (`claude-sonnet-4-6`), same
day, verifier on in all three, every case against a checkout of this
repository:

| Run | Path | Context | Scout cap |
| --- | --- | --- | --- |
| A | single loop (production today) | — | — (the loop explores and judges in one conversation, cap 20) |
| B | multi-agent, RC1-390 as measured | off | 8 |
| C | multi-agent, this story | on | 3 when the context is complete, else 8 |

The checkout is this repository, not a fixture: the corpus diffs name files
that do not exist in it, so every scout hunts for a module it will not find.
That inflates exploration in B and C alike and is the same handicap for
both; the comparison is fair, the absolute numbers are a ceiling. The
callers grep finds nothing for the corpus's invented names, so the corpus
measures what the conventions file and the cap do, and the live PR below
measures the callers list.

## Results

### The first measurement, and why the cap exists

The first thing run, before any corpus, was one case (`convention-break`)
against this checkout with the context on and off, the scout's tool calls
logged. With the context off the scout read `CLAUDE.md` twice and spent 7.5 ¢
over 8 turns. With the context on it did not touch `CLAUDE.md` — the note
worked — and spent 7.0 ¢ over 8 turns on other greps and reads. **The scout
spends to its turn cap whatever it is handed.** Its cost is a function of its
budget, not of what it still needs to find, because every turn re-sends the
growing conversation and the file reads in it. Handing it the answers only
makes exploration cheaper if the budget shrinks with them. That is where
`scout_turns` came from, and every number below has it.

### Three real PRs, four configurations

The corpus cannot exercise the callers list (its names exist nowhere) and it
handicaps the scout (its files exist nowhere), so the decisive numbers are
three merged PRs of this repository, each run against a worktree at its own
head, verifier on, findings counted after the verifier. PR #39's worktree is
this branch, which is ahead of its head, so its scouts chased one real
mismatch; the relative numbers hold, the findings on that row do not.

| PR | Single loop (production today) | Multi, RC1-390 (8-turn scout, no context) | Multi, context + 3-turn scout | Multi, context, no scout |
| --- | --- | --- | --- | --- |
| #35, 6 files, +161/−10 | **48.3 ¢**, 96 s, 20 turns, 15 files, 1 finding | 25.6 ¢, 61 s, 3 | 17.9 ¢, 46 s, 3 | **6.0 ¢**, 15 s, 3 |
| #33, 10 files, +666/−81 | **70.0 ¢**, 110 s, 20 turns, 27 files, 4 | 45.8 ¢, 78 s, 6 | 31.0 ¢, 57 s, 5 | **11.6 ¢**, 22 s, 3 |
| #39, 27 files, +2,168/−35 | **$1.10**, 100 s, 20 turns, 27 files, 3 | 62.2 ¢, 83 s, 8 | 44.7 ¢, 56 s, 7 | **21.4 ¢**, 58 s, 8 |

Where the scout's money went, per PR, context off → on with the 3-turn cap:

| PR | Scout turns | Files read | Context tokens the scout read | Scout cost |
| --- | --- | --- | --- | --- |
| #35 | 8 → 3 | 11 → 5 | 132k → 47k | 19.8 ¢ → 10.9 ¢ |
| #33 | 8 → 3 | 12 → 5 | 237k → 84k | 34.5 ¢ → 18.7 ¢ |
| #39 | 8 → 3 | 13 → 4 | 335k → 114k | 47.9 ¢ → 27.8 ¢ |

Three things this table settles:

1. **RC1-390's cost projection was wrong in the other direction.** It said
   the multi-agent path would cost 2–3× the single loop on a live PR, from
   the one corpus case where the loop had one file to read. On a real PR the
   single loop runs to its 20-turn cap every time — 15 to 27 file reads,
   each re-sent on every later turn — and costs 48 ¢ to $1.10. Every
   multi-agent configuration, RC1-390's included, is cheaper than production
   today. The corpus is diff-only; it never showed the loop exploring.
2. **The context plus the cap cuts the scout by 42–46 % and the review by
   28–32 %** against RC1-390, and the review to 40–45 % of today's cost.
   Python found `CLAUDE.md` on every PR and 10, 40 and 34 caller rows
   (PR #39 defined 51 symbols; 12 were searched, the rest listed as not
   searched).
3. **The scout is still more than half of the bill** at three turns, so the
   cheapest configuration by far is to skip it once the context is complete
   (`REVIEW_SCOUT_CONTEXT_TURNS=0`): 6 to 21 ¢, roughly a tenth of today, and
   the fan-out's wall clock. The bill is then the one prefix write (diff plus
   conventions plus callers, 7k–20k tokens at 1.25×, about half the cost) and
   four tool-less calls reading it from cache.

What the scout bought on these three PRs: on #33 the 3-turn scout's brief
produced a `warning/breaking_change` — the webhook passes a
`RemoteRepoTools` where the loop is typed `RepoTools` — that the single loop
also found and the no-scout run did not. It is exactly the finding a scout
exists for: a caller-side mismatch outside the hunks and outside the grep
hits. On #35 and #39 the no-scout run found the same or more. One finding in
three PRs is a signal, not a measurement.

### The corpus, four ways

Sixteen cases, every one against a copy of this checkout, verifier on. The
diffs name files the checkout does not have, so every scout hunts and the
single loop has something to read; the handicap is the same across the rows.

| | A: single loop | B: multi, RC1-390 | C: multi, context + cap | D: multi, context, no scout |
| --- | --- | --- | --- | --- |
| Run | `pr-review-20260906T155401` | `…T155558` | `…T155147` | `…T155749` |
| Cases passing | **16 / 16** | 15 / 16 | 13 / 16 | 14 / 16 |
| Recall | 13 / 13 | 13 / 13 | 13 / 13 | 13 / 13 |
| Categorized correctly | 13 / 13 | 12 / 13 (`convention-break` → `pr_drift`) | 12 / 13 (`general-dead-code` → `error_handling`) | 12 / 13 (`convention-break` → `docs`, `pythonic`, `tests`) |
| Precision | 2 / 2 | 2 / 2 | **0 / 2** (see below) | 1 / 2 (`deliberate-broad-except`: `warning/tests` on the untested branch, as in RC1-390) |
| Clean diff | 1 nit | 2 nits | 1 nit | 1 nit |
| Verifier | 5 dropped, 1 downgraded | 22 dropped | 24 dropped, 4 downgraded | 21 dropped, 2 downgraded |
| Cost, 16 cases | **$1.26** (7.9 ¢ / case) | $1.99 (12.4 ¢) | $1.63 (10.2 ¢) | **$0.68** (4.2 ¢) |
| Scout | — | $1.47 (74 % of the run) | $0.85 (52 %) | — |
| Scout turns | loop: 4–11 | 5–8, mostly 8 | 3 on 9 cases, 8 on 7 | — |
| Wall clock per case | 23–52 s (one 210 s) | 36–84 s (one 177 s) | 33–50 s | 11–24 s |

Run C ran before the cap rule was corrected: it gave the short cap only when
the diff defined a symbol, so the seven cases that change a function body or
a manifest kept the full cap (scout 5.5–8.0 ¢ each, against 2.3–6.4 ¢ on the
nine capped cases). A diff that defines nothing has no callers to find, which
is an answer, not a gap; the rule now asks only for the conventions file and
an uninterrupted search. Run D and the live PRs above ran under the corrected
rule.

**Against the diff-only baseline.** RC1-387's flag-on run of the same corpus
cost $0.55; the single loop with a checkout cost $1.26. The 2.3× is the
exploration the corpus never measured, and it is the number that made
RC1-390's projection wrong: the loop's 3.4 ¢ per case was the price of having
nothing to read.

**The corpus disagrees with the live PRs on the single loop, and the live
PRs are right about production.** On the corpus the single loop stopped after
4 to 11 turns and beat every multi-agent row on cost; on every real PR it ran
to 20. The corpus diffs are small and self-contained, so the loop runs out of
things to look at. A real PR does not.

**Run C's precision failures.** Both decoy cases drew blockers — `[blocker/
tests] the import from app.signature will raise ImportError`, `[blocker/
general] app/worker.py does not exist in this repository` — from tool-less
reviewers reading a brief that said, correctly, that the file is not in the
checkout. Runs A and B did not do this on the same cases, and the single loop
in A treated the absence as the fixture's problem. On a live PR the files
exist, so the artifact cannot occur, but the mechanism can: a reviewer that
cannot look for itself will escalate "the scout found nothing" into a
finding, and the verifier kept both. The repo-context reviewer is already
told to raise nothing about missing evidence; the diff-local and
change-intent reviewers are not. That is the thing to watch on the first live
reviews with the flag on, and the verdict policy means a blocker in any
category but `leaked_secret` is advisory either way.

**Run D, no scout.** Recall held at 13 / 13 with no model exploring at all,
the review of every case took 11–24 s, and the run cost $0.68 — less than
the single loop with a checkout by half, and close to RC1-387's diff-only
$0.55 with the conventions file now in every prefix. Neither precision
blocker from run C recurred: with no brief saying the file was absent, the
reviewers judged the diff. The two failures are the two RC1-390 already
knew: `convention-break` filed three ways by three reviewers with the
convention-named one not the survivor (the cross-category dedupe question,
still open), and the untested new branch in `deliberate-broad-except`
drawing a fair `warning/tests`.

## Decision

**The flag should go on, with the 3-turn scout (the default), and the
Fly secret is Reid's to set after the merge.** RC1-390 left it off because
the scout cost more than the whole single loop; that comparison came from a
corpus that never let the single loop explore. With a repository behind it
the single loop is the most expensive review of the four, and the multi-agent
path with the conventions file and callers in the prefix and the scout held
to three turns is 40–45 % of its cost on real PRs, in about half the wall
clock, with the scout's evidence still in the review. The scout stays because
on one of three PRs it was the difference between finding a caller-side
defect and not, and three turns is what its remaining job needs.

**The no-scout configuration is the next lever, not this one's default.** It
is a tenth of today's cost and, on these PRs, reviewed as well; but the
corpus cannot see what a scout adds and three PRs are not a measurement. The
follow-up that would make it safe is cheap and deterministic: find tests for
the changed paths the way callers are found now, so the scout's last job is
Python's too. Then the scout can go, and the review is the warm write plus
four cached reads.

**What to watch when the flag goes on:** `context conventions=… callers=…
scout_turns=…` and `scout_done` in the webhook log per review, blockers on
absent evidence (the run C mechanism), and per-stage cost from the LLM Obs
trace. Turn it back off if blockers appear on things the scout could not
find rather than things it found.

**Also settled here:** the eval corpus is diff-only by construction and
cannot price exploration; `--repo-path` is the way to run it when
exploration is the question, and the record carries `+checkout` so those
runs are never averaged with the rest. RC1-391 compares frameworks against
this graph — six nodes now, with the context step — and should inherit the
live-PR harness rather than the corpus for cost.

## What this story changed regardless of the flag

- `read_text` on both tool backends, for Python callers.
- Two fields on `ReviewResult` (`conventions_file`, `callers_found`) and one
  observability metric (`scout_turn_cap`), so a run record says what the
  scout was handed.
- `python -m evals --repo-path` and `--no-repo-context`, and the corpus run
  record's `checkout` and `context` observations.
- The single loop's request shape is untouched; flag off is byte for byte
  what it was (run A is the same subject version as RC1-387's baseline plus
  `+checkout`).
- Spend on the measurements in this record, all runs included: about $11.

