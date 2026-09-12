# RC1-424 — One repository contract for the local and GitHub adapters

The review reads a repository through two backends: a checkout on disk for
the dry-run CLI, the eval corpus and the measurement scripts, and the GitHub
Trees and Contents APIs for the live webhook, which has no checkout
(RC1-364). Both served the same calls, but the contract lived in two class
bodies and a `getattr(..., "explorable", True)`, the pipeline was typed
against the local class while the webhook handed it the remote one, and the
two disagreed on what a spent API budget looks like. This story writes the
contract down once and makes both adapters pass the same tests.

- **Ticket:** [RC1-424](https://hirereidcollins.atlassian.net/browse/RC1-424).
- **Related:** RC1-109 (the local tools), RC1-364 (the remote ones),
  RC1-427 (retired the model-facing surface and left the read-and-grep
  methods "for RC1-424 to shape into the one interface").
- **No model calls changed.** The rows context gathering renders into the
  prefix are byte-identical, so no corpus run was owed; the offline suite
  and the contract tests are the evidence.

## The contract

`app/agent/repository.py` holds `RepositoryAccess`, a `Protocol` with one
property and three methods — the whole of what `app/agent/context.py`
asks of a repository since RC1-427:

| Member | Answer | When there is nothing to answer |
|---|---|---|
| `explorable` | whether a repository is behind this at all | `False` (the CLI's empty temp dir) |
| `read_text(path)` | the raw file, clipped to `MAX_READ_BYTES` | `None`: missing, directory, binary, withheld, outside the root, out of budget |
| `paths()` | every served file path | `None`: no tree, or no budget for the one call it costs |
| `grep(pattern, path, max_results)` | `file:line: text` rows, `(no matches)`, a `... [note]` line when capped or cut short | raises `RepositoryError` when it could not search at all |

The guards are shared helpers in the same module, so the adapters cannot
drift on what a review may see: `is_secret_file`, `is_lockfile`,
`is_withheld`, `is_noise_path`, the caps, `compile_pattern`, `grep_text`,
`render_grep`. `ToolError` is `RepositoryError`; "tool" named the surface
RC1-427 removed.

The adapters are `LocalRepository` (`app/agent/local_repository.py`) and
`GitHubRepository` (`app/agent/github_repository.py`). The pipeline imports
the protocol and nothing else from either; the CLI builds the local one, the
webhook the GitHub one. The GitHub adapter's `api_calls` and
`tree_available` stay its own — the webhook that constructs it logs them.

## What went

`read_file` (numbered lines, ranges), `list_dir`, `format_file_text`, the
`ignore_case` / `fixed` / `glob` options on `grep`, and the two caps only
they used. They were the model's tools; no model has had tools since
RC1-427, and nothing called them but their own tests. Keeping methods
outside the contract on both adapters would have been a second contract.

## What was wrong, and is not now

**A spent budget read as "no callers".** With the tree cached and the
budget gone, the remote `grep` returned `(no matches)` plus a note
addressed to a model that no longer reads it; `context.callers` saw no hit
rows and rendered *"(no references found for: …)"* into the prefix — a
false statement, of the kind RC1-428 found the tests list making. The
local adapter raised in its equivalent case. Now a grep that could read no
file at all raises `RepositoryError`, which context gathering already
records as *not searched* and says so to the reviewers. A grep that read
some files still returns what it found, with the note.

**`scripts/measure_tiebreak.py pipeline` would have crashed.** The billed
mode still passed `verify=True` to `review_pull_request`, a keyword RC1-428
removed. Fixed on the line the rename touched.

## The tests

`tests/test_repository_contract.py` is one file layout and one set of
expectations run against both adapters through a parametrized fixture:
reads, clipping, every `None` case, grep rows, withheld and noise files,
the size cap, scoping, the result cap, the two error cases, `paths`, and
`build_repo_context` over the same repository giving the same context. The
adapter modules keep what is theirs: symlinks and the empty root for the
local one; the cache, the budget, the tree fallback and the bounded grep
for the GitHub one.

## Still to do, not here

The diff rendering in `reviewer.py` still says *"use read_file"* on a
truncated or missing patch. It is prefix text, so changing it is a corpus
run under the RC1-422 rule; it will go with the next story that touches
the prefix.
