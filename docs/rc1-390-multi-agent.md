# RC1-390 — Scout, three evidence-scoped reviewers, Python router: decision record

The second step of the multi-agent review sequence (RC1-387 → RC1-390 →
RC1-391), and the record for the two together: RC1-387 added the verifier
and the precision cases and measured the single loop; this story splits the
loop and measures the split against that baseline.

- **Ticket:** [RC1-390](https://hirereidcollins.atlassian.net/browse/RC1-390)
- **Flag:** `REVIEW_MULTI_AGENT` (default off), `REVIEW_SCOUT_MAX_TURNS` (8)
- **Code:** `app/agent/router.py`, `app/agent/scout.py`, `app/agent/multi.py`,
  reviewer specs and the scout prompt in `app/agent/prompts.py`; the verifier
  gained a shared-prefix mode in `app/agent/verifier.py`
- **Runs:** baseline flag-on `pr-review-20260906T141141` (RC1-387);
  multi-agent `pr-review-20260906T144942`

## Context

The 2026-09-06 decision was that the multi-agent design is a deliverable in
itself for this agent, not only a fix for a measured defect, and that this
raises the bar: every agent has to be defensible. Thirteen agents, one per
category, is not. Three is, because a review has three distinct kinds of
evidence: the hunk alone; the hunk plus the repository around it; the hunk
plus what the change says it is. The category list is an output schema and
says nothing about what a reviewer must read to produce a finding.

Cost was the explicit constraint. Reid's instruction during the build:
whatever we do, know the cost and keep it down. The RC1-387 baseline with
the verifier on is $0.546 for the 16-case corpus, 3.4 ¢ per case, so the
question this record answers is whether three reviewers can be had for the
price of one loop. The 2026-09-04 survey said the only near-neutral shape is
one scout that explores once, specialists that share a cached prefix and
never call tools, and routing done by Python. That is what was built.

## What was built

**Router** (`plan_review`). A pure function over the PR's file list. The
diff-local reviewer always runs. The repo-context reviewer and the scout run
unless the change is documentation only. The change-intent reviewer always
reads the description against the diff and judges the scale of touched IO,
and adds dependency review only when a manifest changed and n8n review only
when a workflow export changed. When the repo tools have no repository
behind them (the dry-run CLI without `--repo-path`, which is how the corpus
runs), the scout is skipped: Python decides that in zero turns rather than
the model discovering it in three. The plan is a value, logged and tested.

**Scout** (`explore`). The single loop's exploration half, ending in a
`submit_brief` call instead of `submit_review`. Same tools, same file-read
cap, same moving cache marker, same forced final call, but a turn cap of 8
instead of 20 and a brief cut at 2,500 characters, because the brief is
context every reviewer pays for. It is the only agent with tools.

**Reviewers.** Three single calls, one per kind of evidence, fanned out with
`asyncio.gather`. Each sends the same system prompt, the same tool list
(`submit_review` and `verify_findings`), the same `tool_choice: any`, and the
same first content block — the PR rendering plus the brief — under one cache
breakpoint. Only the second block differs: the reviewer's slice of the
rubric, cut from `REVIEW_RUBRIC` at run time so there is one copy of the
text, plus instructions naming the categories it may raise. The rubric split:

| Reviewer | Rubric dimensions | Categories |
| --- | --- | --- |
| `diff_local` | 3 security & secrets, 2 pythonic, 6 error handling, docs | `leaked_secret`, `security`, `pythonic`, `error_handling`, `docs` |
| `repo_context` | 1 convention, 4 tests, 7 breaking changes | `convention`, `tests`, `breaking_change` |
| `change_intent` | 8 drift, 9 scale; 5 dependencies and 10 n8n when routed | `pr_drift`, `infra_scalability`, `dependencies`, `n8n` |

`general` is allowed from any reviewer.

**Two cache rules that shaped the request.** The API serves a cache entry
only once the request that wrote it has begun responding, so three
reviewers launched together would each write the prefix and none would
read it. A one-token warm call writes it first. And a different
`tool_choice` invalidates the cached messages, so the warm call, the
reviewers and the verifier all send `any` and are told which tool to call.
A reviewer that calls the wrong tool is counted (`unusable_reviewer_calls`)
and contributes nothing; a verifier that does reads as "keep everything".

**Merge** (`merge_findings`, `compose_summary`). Python. A finding outside
its reviewer's categories is discarded and counted — another reviewer had
that evidence. Findings at the same file, line and category fold into one,
keeping the more severe. PR-level findings never fold. The summary is the
reviewers' one-sentence summaries, the one holding the most serious finding
first. Then the RC1-387 verifier runs over the merged list, reading the same
shared prefix instead of rendering its own.

**Record.** Per stage — scout, warm call, each reviewer, verifier — the four
token counts and the cost, plus wall clock for the scout, the fan-out and
the verifier, and the minimum `cache_read_input_tokens` across the reviewer
calls. That last number is the design's premise, and it is read per call.

**Tracing.** The review is one `workflow` span with the scout, each reviewer
and the verifier as `agent` children and the warm call as a `task`; the
SDK's auto-instrumented calls hang under those. With the flag off nothing
about tracing changed.

## Method

Same corpus (16 cases: 13 planted, 2 precision, 1 clean), same model
(`claude-sonnet-4-6`), same day, from the dry-run CLI with no `--repo-path`
as the corpus has always been run. The comparison is the RC1-387 flag-on
run, because the verifier ships and the split keeps it. One multi-agent
run, verifier on. The RC1-387 record already showed recall moves by one
case between identical runs, so single-case differences below are read as
variance unless a mechanism explains them.

## Results

### The corpus, both ways

| | RC1-387 baseline, flag on | RC1-390 multi-agent |
| --- | --- | --- |
| Recall | 13 / 13 | **13 / 13** |
| Categorized correctly | 13 / 13 | 12 / 13 (`convention-break`, see below) |
| Clean diff | 1 nit, 0 blockers | 1 nit, 0 blockers |
| Precision | 2 / 2 | 1 / 2 (`deliberate-broad-except`, see below) |
| Cases passing | 16 / 16 | 14 / 16 |
| Cost, 16 cases | $0.546 | **$0.553** (+1.3%) |
| Cost, the 15 cases with no checkout | $0.516 (3.4 ¢ / case) | **$0.469 (3.1 ¢ / case, −9%)** |
| Cost, `n8n-hot-cron` (the one case with files to explore) | $0.030 | **$0.085** (2.8×) |
| Wall clock, the 15 diff-only cases | 18–43 s, mean 29 s | **12–24 s, mean 16 s** |
| Wall clock, `n8n-hot-cron` | 26 s | 51 s (scout 24.5 s) |
| Reviewer calls reading the prefix from cache | — | **16 / 16 cases, every call** |
| Findings discarded as off-scope | — | 1 |
| Verifier actions | 2 dropped, 5 downgraded | 15 dropped, 5 downgraded |

### Where the money went

| Stage | 16-case total | Note |
| --- | --- | --- |
| Reviewers, three | $0.375 | Output tokens are most of it: 900–1,700 per case across the three, against 700–2,000 for the single loop |
| Verifier | $0.096 | Down from $0.134: it now reads the shared prefix (2,600–2,900 tokens per case at cache-read price) instead of writing its own |
| Warm call | $0.048 | The prefix write, paid once per case; the system prompt part read from cache on every case after the first |
| Scout | $0.035 | One case ran it |

The cache did exactly what the design needs. On every case the warm call
wrote the diff and brief (roughly 600 tokens) and read the system prompt
(about 2,000); each reviewer then read 2,600–2,900 tokens and paid full
price only for its own 450–700-token suffix. Latency followed: the fan-out
took 9–21 s, which is one reviewer's call, not three.

**The scout is the cost.** On the one case with a checkout the scout spent
24 s and 3.5 ¢, more than the single loop spent on the whole review of the
same case (3.0 ¢). It read 6 files over 3 turns and wrote a 650-token brief.
The corpus is diff-only, so the 15 cheap cases never paid for exploration
at all; a live PR always would. **The cost projection for production is
therefore the `n8n-hot-cron` row, not the total: roughly 2–3× today's
per-review cost.** The ticket's "cost per PR at or below baseline" holds for
the reviewers and the verifier, and fails for the scout.

### The two failed cases

**`convention-break`** was found, three ways: `warning/convention` from
the repo-context reviewer, `warning/pr_drift` from the change-intent
reviewer, and `nit/docs` from the diff-local reviewer, all about the same
comment contradicting the same code. The merge folds duplicates only at the
same category, so all three reached the verifier, which dropped two as
redundant and kept the nit. The reviewer got the category right; the
verifier's choice of survivor lost it. This is a new failure mode that three
reviewers create and one loop cannot: the same defect seen from three kinds
of evidence, filed three ways.

**`deliberate-broad-except`** drew no finding on the catch-all itself
(`nit/error_handling` on the unbound exception variable, tolerated) but a
`warning/tests` that the new except branch has no test. The scorer counts
that as flagging the decoy because it names the branch; it is a
test-coverage finding, and a fair one. The baseline's 2 / 2 was on a run
whose first pass produced nits here; the RC1-387 record already called that
row variance.

### Three things the run showed about the prompts

1. **The repo-context reviewer reports missing evidence as findings.** Told
   to "say when the brief did not reach a file you needed", it raised a
   `nit/convention` on six cases saying the scout brief was skipped so
   conventions could not be checked. The verifier dropped every one, at
   about a cent per case of output for nothing. Fixed after the run: the
   instruction now says to raise nothing when the evidence is not there.
   Not re-measured.
2. **The verifier dropped a real blocker as a duplicate** on `pr-drift`:
   `blocker/breaking_change`, replacing `hmac.compare_digest` with `==`,
   which is the timing-safety defect in that diff and was raised by no other
   surviving finding. The second time in two records the verifier has
   removed a real finding on grounds of redundancy (RC1-387's `verify_tls`
   drop was the first). The verifier's instructions now say that when two
   findings describe one defect it keeps the one whose category names it
   best at the higher severity, and never drops a finding as redundant
   unless another kept finding states the same defect. That line applies
   with the multi-agent flag off as well, since the verifier ships; it
   changes the verifier's prompt hash, so the next flag-on run is a new
   subject version. Not re-measured.
3. **Off-scope discipline held.** One finding in 16 cases was raised outside
   its reviewer's categories (the repo-context reviewer filing `pr_drift`),
   and the merge discarded it. The reviewers stay in their lanes.

## Decision

**The flag stays off in production.** The design does what it claims —
recall held, every reviewer read the shared prefix, the fan-out is faster
than the loop, and on diff-only reviews it is 9% cheaper — but the cost that
matters for a live PR is exploration, and the scout costs more than the
whole single loop does today. Turning this on would roughly double or
triple the per-review cost against the RC1-377 guardrail for a review that
is not measurably better. Reid asked for cost to be known and kept down;
this is the number, and it says no.

What would change the answer is making exploration cheap, and that is
mostly a caching problem rather than an agent problem: the convention half
of the scout's brief is a property of the repository, not of the PR, and is
re-derived on every review. Three follow-ups, cheapest first:

- **Read the repo's own conventions file first.** Every repo in this estate
  carries a `CLAUDE.md` with a conventions section. Python can fetch it in
  one API call and put it in the shared prefix with no model turn at all;
  the scout then only has to look for what that file does not say.
- **Callers by grep, not by model.** For each function or class the diff
  changes, Python can grep the repo for callers and append the hits to the
  prefix. That is the repo-context reviewer's second question answered
  deterministically, and it is what the scout spends most of its turns on.
- **A per-repository brief, cached.** Have the scout write a repository-level
  brief once, key it by repo and default-branch SHA, store it beside the
  dedup state, and refresh it when the default branch moves. Each PR's scout
  then explores only the changed paths. The prompt cache cannot do this
  (five-minute TTL, per-request prefix); it needs a store.

With those in place the scout's per-PR work shrinks to callers and tests for
the changed paths, and the projection above changes. Until then the single
loop is the cheaper review of the same quality, and it stays on.

**Also settled here:** RC1-391 compares frameworks against this graph, not
against the single loop. The graph has five nodes and four edges written by
hand in `multi.py`, which is the thing a framework would draw.

## What this story changed regardless of the flag

- The verifier can read a caller-supplied prefix and tool list; its default
  request is the RC1-387 one, byte for byte.
- `parse_findings` is a function the loop and the reviewers share; the
  loop's own behavior is unchanged.
- The verifier's duplicate rule above, on every review with the verifier on.
- The eval record carries per-stage tokens, cost and latency, and the
  minimum reviewer cache read, so the next experiment on this path starts
  with the premise checkable.
