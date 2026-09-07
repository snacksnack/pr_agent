# RC1-396 — Conventions files in the n8n repositories: measurement record

A follow-up to RC1-394, whose "Other repositories" table had the two n8n
repositories as the most expensive reviews in the estate: no conventions
file, so the context was never complete, so the scout ran to its full
eight-turn cap and was two thirds of the bill. The ticket offered two fixes
— a `CLAUDE.md` in each repository, or a router rule that treats callers
plus tests as enough when only the conventions question is open — and
chose the file. This record is the before/after.

- **Ticket:** [RC1-396](https://hirereidcollins.atlassian.net/browse/RC1-396)
- **Code here:** none in the agent. `scripts/measure_pr.py` gained
  `--repo-dir` (measure another checkout's PRs) and `--overlay` (lay files
  from that checkout's working tree over the worktree before the review),
  which is how a conventions file is measured against a PR that predates it.
- **Files elsewhere:** `CLAUDE.md` in
  [n8n-concert-intelligence-agent](https://github.com/snacksnack/n8n-concert-intelligence-agent)
  and
  [n8n-stakeholder-status-email](https://github.com/snacksnack/n8n-stakeholder-status-email),
  one PR each.
- **Flags:** none. `REVIEW_MULTI_AGENT=1` has been on in Fly since
  2026-09-07 00:15 UTC, so every PR on these repositories was paying the
  full scout until the files landed.

## What the files say

Each is a page, under the 6,000-character cap `context.select_sections`
applies, so the whole file reaches the prefix uncut: what the repository is,
where the logic lives (JavaScript Code nodes inside an exported workflow
JSON, a Python eval package that reads the prompt out of that JSON, a free
contract-test layer), and the conventions a change should be held to. The
conventions are the ones the tests already freeze, written in prose: no
secrets in the JSON (`$vars.*` and credentials by id), node names are an
interface the evals look up, the undeclared assumptions that are frozen
rather than fixed (positional preview matching in the concert workflow; the
prompt/parser contract and the computed health level in the status email),
side branches that must never block the main path, and the Python style
the ruff config enforces. Nothing in them is new policy; they state what
the repository already does so a reviewer does not have to explore for it.

## Method

Same PR, same head, twice: once as the PR was (no conventions file at its
head), once with the working-tree `CLAUDE.md` copied into the worktree
first. `--multi --verify`, the shipped defaults otherwise
(`REVIEW_SCOUT_COMPLETE_TURNS=0`). The before pair ran first, then a
five-minute gap so the prompt cache had expired, then the after pair, so
neither run read the other's cache. Concert #8 is the PR RC1-394 measured;
the status-email repository had no row in that record, so its #8 (three
files, the RC1-376 health-restatement change) is measured for the first
time here. No Datadog keys were in the shell, so these runs sent no LLM
Observability traces and do not appear on the fleet dashboard's last-reviews
list. Four billed reviews, 49 ¢ in total.

## Results

| PR | Conventions | Context | Scout | Cost | Wall | Findings |
| --- | --- | --- | --- | --- | --- | --- |
| concert #8, 4 files | none | 1 caller, 13 test rows, incomplete | ran, 8 turns | **28.3 ¢**; scout 19.1 ¢ (67 %) | 61 s | 6 (3 warning, 3 nit); 2 dropped |
| concert #8, 4 files | `CLAUDE.md` | same, complete | skipped | **8.5 ¢** | 19 s | 5 (1 warning, 4 nit); 1 dropped |
| status-email #8, 3 files | none | 0 callers, 6 test rows, incomplete | ran, 8 turns | **9.3 ¢**; scout 5.9 ¢ (63 %) | 36 s | 1 nit |
| status-email #8, 3 files | `CLAUDE.md` | same, complete | skipped | **3.0 ¢** | 5 s | 0 |

The log line the acceptance names appeared on both after-runs:
`context conventions=CLAUDE.md … complete=True scout_turns=0`.

Stage costs, after: the warm call 3.7 ¢ / 1.4 ¢, the three reviewers
1.1–1.4 ¢ / 0.5 ¢ each, the verifier 1.0 ¢ / not run (nothing to verify).
The scout was the whole difference; the rest of the pipeline cost the same
to within a few tenths of a cent, as it did in RC1-394.

### Findings, concert #8

The after-run found the finding the ticket names — `parent_id` on the root
span set to the string `'undefined'` rather than `null`, at
`workflows/concert-intelligence-agent.json:599`, as a warning — plus the
two missing return annotations in `evals/workflow.py`, a `tests` nit on a
substring assertion, and a `pr_drift` nit (the description says the span
builder runs once for all items; the node's `mode` says otherwise).

The before-run in this session did **not** find `parent_id` — it found the
hardcoded `api.datadoghq.com` intake host (a warning, correct for a
single-site deployment but not this one), a `@cache` on a function
returning a mutable list, and a `specifyBody: "json"` concern instead.
RC1-394's before-run of the same PR found `parent_id` and the host. So the
scout's brief moves which of a PR's real problems the reviewers reach for
run to run; with the conventions file in the prefix the reviewers read the
diff against the stated rules and the `parent_id` finding came from the
n8n reviewer without a brief. One run each; this is not evidence that the
no-scout review finds more, only that it found the named finding and cost
a third as much.

### Findings, status-email #8

One nit before (the new test covers two verb forms of "needing attention"
but not "requiring attention"), none after. The PR is a prompt wording
change, a scoring-guard change and its test; nothing in it is a defect,
and the before-run's nit is the kind that comes and goes between runs of
the same input. Three cents is under the 6–20 ¢ band the ticket predicted:
that band came from four-to-six-file PRs, and this one is three files with
no callers to list.

## Decision

Done as the ticket said: option 1, the file in each repository, no change
to the router. The concert repository's review went from 28 ¢ to 8.5 ¢ and
from 61 s to 19 s; the status-email repository's from 9.3 ¢ to 3 ¢. Both
are now in the band the rest of the estate pays.

**The third repository without a conventions file has already shown up,
and it is not an n8n one.** `tpm-automation-platform` has no `CLAUDE.md`,
`AGENTS.md` or `CONTRIBUTING.md` — RC1-397's two reviews of platform PR #64
ran the scout (9.8 ¢ and 10.5 ¢, 47–50 s) for that reason, not because its
tests search failed. The ticket's rule for a third repository was "consider
the router rule". On these numbers the file is still the better fix: it is
a page, it costs nothing in the agent, and the router rule would change
every repository's review and need its own corpus run. The platform file
is filed as a follow-up rather than done here because that repository is
large enough that its conventions page should be written by someone who
has the whole of it in view, not extracted from one session's survey.
`n8n-jira-notion-sync` also has no file, but it has never had a PR, so it
pays nothing today.

## What to watch

- The PRs that add the files were reviewed live at 10:48 UTC, but that is
  not the confirmation: the router classed both as documentation-only
  (`plan scout=False … reasons=('documentation-only change: scout and
  repo_context skipped',)`), so neither built a context and the log line
  reads `conventions=None`. The same minute, the live review of pr_agent #45
  did build one — `conventions=CLAUDE.md conventions_chars=6018 …
  complete=True scout_turns=0` — which shows the path working on a code PR,
  and also that this repository's own file is now just over the cap and is
  being cut to its priority sections.
- The live confirmation for the n8n repositories is the webhook log on the
  first code PR to each after the file merges: `context conventions=CLAUDE.md …
  complete=True scout_turns=0` and a `mode:multi` cost point in the
  5–10 ¢ range on the fleet dashboard.
- A conventions file over 6,000 characters is cut to its conventions,
  testing and layout sections first (`SECTION_KEYWORDS` order); both files
  are about 4,200 characters, so a future edit has room but not unlimited
  room.
