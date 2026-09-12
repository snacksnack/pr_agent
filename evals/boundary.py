"""Boundary cases for the verifier's category tie-break (RC1-398).

Each case is a diff with one defect that legitimately fits two of the rubric's
categories, and the two findings a first pass would file on it — the same
defect on the same line, described once in each category's terms, at the same
severity. The merge (``pipeline.merge_findings``) folds findings only
when file, line *and* category agree, so a pair like this reaches the verifier
intact, and the verifier's instruction is to keep the one whose category names
the defect best. These cases exist to measure whether it does.

``intended`` is the category whose rubric dimension names the defect; the
``notes`` on each case say why, in the rubric's own words. ``rival`` is the
neighbour that fits well enough that a competent reviewer could file it, and
the pairs are drawn from the ones the corpus has actually produced (the
RC1-393/394/391 records): ``convention`` losing to ``docs``, ``pr_drift`` and
``pythonic``; ``general`` losing to ``error_handling``; ``infra_scalability``
losing to ``security``; ``dependencies`` losing to ``security``.

These are not part of ``corpus.CASES``: adding them would change the recall
denominator every trend row is compared on. They are driven by
``scripts/measure_tiebreak.py``, either straight into ``verify_findings`` with
the pair as the first pass (the controlled measurement) or through the whole
multi-agent review (what the pipeline does with them unassisted).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agent.prompts import CATEGORIES
from app.models import ChangedFile, Finding, PRRef, PullRequest

#: The conventions page laid into the checkout for the pipeline mode, and
#: rendered as the repository context for the probe: the one line the
#: ``convention`` cases need a repository for.
CONVENTIONS_PAGE = (
    "# CLAUDE.md\n\n## Conventions\n\n"
    "- Config comes from `app.config.settings` (pydantic-settings). Never read "
    "`os.environ` directly.\n"
    "- Collectors build their indexes with dict comprehensions; see "
    "`app/collectors/jira.py` and `app/collectors/github.py`.\n"
    "- Dependencies are pinned to exact versions in `requirements.txt`.\n"
)


@dataclass(frozen=True)
class BoundaryCase:
    """One defect, two categories, and the pair of findings that describe it."""

    id: str
    intended: str
    rival: str
    title: str
    body: str
    files: tuple[tuple[str, str], ...]  # (filename, patch)
    #: The first-pass pair: ``[0]`` carries ``intended``, ``[1]`` carries ``rival``.
    pair: tuple[Finding, Finding]
    #: Substrings that show a pipeline finding is about this defect (same
    #: contract as ``corpus.PlantedCase.evidence``).
    evidence: tuple[str, ...]
    notes: str = ""
    #: Whether the case needs the conventions page in its context/checkout.
    needs_conventions: bool = False
    extra_files: tuple[ChangedFile, ...] = field(default=())

    @property
    def categories(self) -> tuple[str, str]:
        return self.intended, self.rival


def _f(category: str, message: str, *, file: str, line: int, severity: str = "warning") -> Finding:
    return Finding(severity=severity, category=category, message=message, file=file, line=line)


CASES: tuple[BoundaryCase, ...] = (
    BoundaryCase(
        id="bare-except-swallows",
        intended="error_handling",
        rival="pythonic",
        title="Load the optional config file",
        body="Reads config.json when present.",
        evidence=("except", "swallow", "bare", "catch", "silent"),
        notes=(
            "Rubric 6 names 'no swallowed exceptions'; rubric 2 lists 'broad bare "
            "excepts' as unidiomatic. The defect is that every failure is hidden "
            "behind an empty dict, which is the error-handling dimension's claim; "
            "the bare `except:` is how it happens."
        ),
        files=(
            (
                "app/config_file.py",
                "@@ -0,0 +1,9 @@\n"
                "+import json\n"
                "+\n"
                "+\n"
                "+def load_config(path):\n"
                "+    try:\n"
                "+        with open(path) as fh:\n"
                "+            return json.load(fh)\n"
                "+    except:\n"
                "+        return {}\n",
            ),
        ),
        pair=(
            _f(
                "error_handling",
                "The bare except swallows every failure — a missing file, malformed "
                "JSON, even KeyboardInterrupt — and returns an empty config as if "
                "nothing happened, so a caller cannot tell a missing config from an "
                "empty one and nothing is logged.",
                file="app/config_file.py",
                line=8,
            ),
            _f(
                "pythonic",
                "Bare `except:` is unidiomatic: it catches BaseException, including "
                "KeyboardInterrupt and SystemExit, and silently returns {} on any "
                "error. Catch the specific exceptions (OSError, json.JSONDecodeError).",
                file="app/config_file.py",
                line=8,
            ),
        ),
    ),
    BoundaryCase(
        id="loop-where-repo-uses-comprehension",
        intended="convention",
        rival="pythonic",
        title="Add an id index helper to the collectors",
        body="Builds the lookup the sync step needs.",
        evidence=("comprehension", "loop", "index", "consisten", "other collectors"),
        needs_conventions=True,
        notes=(
            "Rubric 1 judges the change against THIS repo's patterns and the "
            "conventions page says collectors use dict comprehensions; rubric 2 "
            "would call the same loop unidiomatic anywhere. The evidence that makes "
            "it a finding is the repository's, so convention names it."
        ),
        files=(
            (
                "app/collectors/index.py",
                "@@ -0,0 +1,6 @@\n"
                "+def index_by_id(items):\n"
                "+    out = {}\n"
                "+    for item in items:\n"
                "+        out[item.id] = item\n"
                "+    return out\n",
            ),
        ),
        pair=(
            _f(
                "convention",
                "This repo's collectors build their indexes with dict comprehensions "
                "(see the conventions page and app/collectors/jira.py); this helper "
                "builds the same index with a manual loop, so it is inconsistent with "
                "its neighbours.",
                file="app/collectors/index.py",
                line=3,
                severity="nit",
            ),
            _f(
                "pythonic",
                "A manual loop filling a dict is what a dict comprehension is for: "
                "`return {item.id: item for item in items}`.",
                file="app/collectors/index.py",
                line=3,
                severity="nit",
            ),
        ),
    ),
    BoundaryCase(
        id="os-environ-under-comment",
        intended="convention",
        rival="docs",
        title="Add a feature-flag accessor",
        body="Convenience wrapper.",
        evidence=("os.environ", "settings", "convention", "comment"),
        notes=(
            "The comment restates the repository convention and the code two lines "
            "below breaks it. The code is what is wrong, which is rubric 1; the "
            "'docs' reading — a comment that no longer describes the code — is "
            "true but names the symptom."
        ),
        files=(
            (
                "app/feature_flags.py",
                "@@ -0,0 +1,7 @@\n"
                "+import os\n"
                "+\n"
                "+\n"
                "+# Config always comes from app.config.settings, never os.environ.\n"
                "+def flag_enabled(name: str) -> bool:\n"
                '+    return os.environ.get(f"FLAG_{name}", "") == "1"\n',
            ),
        ),
        pair=(
            _f(
                "convention",
                "Reads os.environ directly where this repo's convention — restated in "
                "the comment two lines above — is that config comes from "
                "app.config.settings. Add the flag to Settings and read it there.",
                file="app/feature_flags.py",
                line=6,
            ),
            _f(
                "docs",
                "The comment says config always comes from app.config.settings, but "
                "the function immediately reads os.environ; the comment is wrong "
                "about the code it sits on and will mislead the next reader.",
                file="app/feature_flags.py",
                line=6,
            ),
        ),
    ),
    BoundaryCase(
        id="os-environ-against-description",
        intended="convention",
        rival="pr_drift",
        title="Add a feature-flag accessor",
        body=(
            "Adds `flag_enabled(name)` following the existing settings pattern, so "
            "flags are read the same way as every other config value."
        ),
        evidence=("os.environ", "settings", "convention", "pattern", "description"),
        needs_conventions=True,
        notes=(
            "Same defect as os-environ-under-comment with the convention stated in "
            "the PR description instead of a comment. The description is accurate "
            "about the intent and wrong about the code; rubric 8 could file that, "
            "but the thing to fix is the code against the repo's pattern (rubric 1)."
        ),
        files=(
            (
                "app/feature_flags.py",
                "@@ -0,0 +1,5 @@\n"
                "+import os\n"
                "+\n"
                "+\n"
                "+def flag_enabled(name: str) -> bool:\n"
                '+    return os.environ.get(f"FLAG_{name}", "") == "1"\n',
            ),
        ),
        pair=(
            _f(
                "convention",
                "Reads os.environ directly; this repo's convention (CLAUDE.md) is that "
                "config comes from app.config.settings, and every other config value "
                "is read that way. Add the flag to Settings.",
                file="app/feature_flags.py",
                line=5,
            ),
            _f(
                "pr_drift",
                "The description says the accessor follows the existing settings "
                "pattern, but the diff reads os.environ directly rather than "
                "app.config.settings — the PR does not do what it says.",
                file="app/feature_flags.py",
                line=5,
            ),
        ),
    ),
    BoundaryCase(
        id="select-star-every-account",
        intended="infra_scalability",
        rival="security",
        title="Add nightly report job",
        body="Emails a summary of every account.",
        evidence=("every account", "all accounts", "unbounded", "memory", "select *", "pagina"),
        notes=(
            "Rubric 9 names 'unbounded queries ... missing pagination/limits'. The "
            "same line also pulls every column, PII included, which rubric 3 could "
            "read as exposure; the defect that grows with the table is the "
            "unbounded read."
        ),
        files=(
            (
                "app/reports.py",
                "@@ -0,0 +1,4 @@\n"
                "+def nightly_report(db):\n"
                "+    accounts = db.query('SELECT * FROM accounts').fetchall()\n"
                "+    rows = [render(a) for a in accounts]\n"
                "+    return '\\n'.join(rows)\n",
            ),
        ),
        pair=(
            _f(
                "infra_scalability",
                "`SELECT * FROM accounts` with fetchall() loads every account into "
                "memory on every nightly run with no limit or pagination; this grows "
                "linearly with the table and will not survive the account count this "
                "job is meant for. Stream or page the query.",
                file="app/reports.py",
                line=2,
            ),
            _f(
                "security",
                "`SELECT *` pulls every column of every account — including whatever "
                "PII the accounts table carries — into the report process; select "
                "only the columns the summary needs.",
                file="app/reports.py",
                line=2,
            ),
        ),
    ),
    BoundaryCase(
        id="retry-forever",
        intended="error_handling",
        rival="infra_scalability",
        title="Retry transient fetch failures",
        body="Makes the poller resilient to blips.",
        evidence=("forever", "unbounded", "no limit", "backoff", "retry", "indefinite"),
        notes=(
            "Rubric 6: failures handled deliberately — this loop retries forever with "
            "no cap, no backoff and no log. Rubric 9 could name the same loop as a "
            "cost cliff under an outage. The defect is the retry policy."
        ),
        files=(
            (
                "app/poller.py",
                "@@ -0,0 +1,7 @@\n"
                "+def fetch_with_retry(url):\n"
                "+    while True:\n"
                "+        try:\n"
                "+            return http.get(url, timeout=2)\n"
                "+        except http.Error:\n"
                "+            continue\n",
            ),
        ),
        pair=(
            _f(
                "error_handling",
                "Retries forever: a permanently failing URL spins this call "
                "indefinitely with no attempt limit, no backoff and nothing logged, "
                "so the failure is never surfaced. Cap the attempts and raise.",
                file="app/poller.py",
                line=2,
            ),
            _f(
                "infra_scalability",
                "An unbounded retry loop with no backoff hammers the endpoint as fast "
                "as it can fail; every poller doing this during an outage is a "
                "self-inflicted load spike and a cost cliff. Bound it and back off.",
                file="app/poller.py",
                line=2,
            ),
        ),
    ),
    BoundaryCase(
        id="dead-code-after-return",
        intended="general",
        rival="error_handling",
        title="Add a guard to the parser",
        body="Defensive check.",
        evidence=("unreachable", "dead", "never", "after return", "after the return"),
        notes=(
            "The corpus's general-dead-code case. Unreachable code fits no numbered "
            "dimension, which is what 'general' is for; the error-handling reading "
            "— the guard never fires — is what the dead code happens to be."
        ),
        files=(
            (
                "app/parser.py",
                "@@ -20,6 +20,10 @@ def parse(payload):\n"
                "     return json.loads(payload)\n"
                "+    if not payload:\n"
                "+        raise ValueError('empty payload')\n",
            ),
        ),
        pair=(
            _f(
                "general",
                "Unreachable: the guard sits after the return statement, so it never "
                "runs. Move it above the return or delete it.",
                file="app/parser.py",
                line=21,
            ),
            _f(
                "error_handling",
                "The empty-payload check never fires because it follows the return, so "
                "an empty payload reaches json.loads and raises JSONDecodeError instead "
                "of the intended ValueError. Move the guard above the return.",
                file="app/parser.py",
                line=21,
            ),
        ),
    ),
    BoundaryCase(
        id="git-dependency-on-branch",
        intended="dependencies",
        rival="security",
        title="Add fastjson for the export path",
        body="Faster serialisation for large exports.",
        evidence=("pin", "branch", "@main", "mutable", "reproduc", "supply"),
        needs_conventions=True,
        notes=(
            "Rubric 5 asks whether a new dependency is 'necessary, reputable, and "
            "pinned'; a git URL at a branch is none of pinned. Rubric 3 could call "
            "it a supply-chain risk. The manifest line is a dependency defect."
        ),
        files=(
            (
                "requirements.txt",
                "@@ -4,3 +4,4 @@\n"
                " httpx==0.27.2\n"
                " pydantic==2.9.2\n"
                "+fastjson @ git+https://github.com/someone/fastjson@main\n",
            ),
        ),
        pair=(
            _f(
                "dependencies",
                "fastjson is installed from a moving branch (`@main`) while every "
                "other requirement is pinned to an exact version; builds stop being "
                "reproducible and a push to that branch changes what ships. Pin to a "
                "tag or commit, or a released version.",
                file="requirements.txt",
                line=6,
            ),
            _f(
                "security",
                "Pulling a dependency from a mutable branch of a third-party GitHub "
                "repository lets anyone with push access to that repo run code in this "
                "build and at import time; pin to an immutable commit or a released "
                "package.",
                file="requirements.txt",
                line=6,
            ),
        ),
    ),
    BoundaryCase(
        id="docstring-and-description-say-three",
        intended="docs",
        rival="pr_drift",
        title="Add a retry helper",
        body="Adds `retry(fn)`, which retries a call up to three times on failure.",
        evidence=("three", "five", "docstring", "says", "attempts", "retries"),
        notes=(
            "The docstring and the PR description both say three; the loop makes "
            "five attempts. Rubric 8 covers the description; the docstring is the "
            "artifact that ships with the code, which is the cross-cutting 'docs' "
            "note. The corpus's docstring-contradicts-code case has drawn pr_drift "
            "as its rival on most runs."
        ),
        files=(
            (
                "app/retry.py",
                "@@ -0,0 +1,9 @@\n"
                "+def retry(fn):\n"
                '+    """Call fn, retrying up to three times on failure."""\n'
                "+    for _ in range(5):\n"
                "+        try:\n"
                "+            return fn()\n"
                "+        except OSError:\n"
                "+            continue\n"
                "+    raise RuntimeError('gave up')\n",
            ),
        ),
        pair=(
            _f(
                "docs",
                "The docstring says the call is retried up to three times; the loop "
                "makes five attempts. Fix the docstring or the range.",
                file="app/retry.py",
                line=3,
                severity="nit",
            ),
            _f(
                "pr_drift",
                "The PR description says retry() retries up to three times, but the "
                "diff makes five attempts; the description does not match the code.",
                file="app/retry.py",
                line=3,
                severity="nit",
            ),
        ),
    ),
    BoundaryCase(
        id="required-kwarg-added",
        intended="breaking_change",
        rival="tests",
        title="Add a priority to outbound mail",
        body="Lets the digest mark itself low priority.",
        evidence=("required", "keyword", "priority", "existing caller", "break", "signature"),
        notes=(
            "Rubric 7: a change to a function signature that breaks existing callers "
            "and is not called out. Rubric 4 sees the same line as a changed path "
            "with no test updated. What breaks callers is the signature."
        ),
        files=(
            (
                "app/mail.py",
                "@@ -10,7 +10,7 @@\n"
                " \n"
                " \n"
                "-def send(to, subject, body):\n"
                "+def send(to, subject, body, *, priority):\n"
                "     msg = _build(to, subject, body)\n"
                "     _transport.deliver(msg)\n",
            ),
        ),
        pair=(
            _f(
                "breaking_change",
                "`priority` is a new required keyword-only parameter on send(), so every "
                "existing call `send(to, subject, body)` now raises TypeError; the PR "
                "does not mention this. Give it a default or migrate the callers in "
                "the same change.",
                file="app/mail.py",
                line=13,
            ),
            _f(
                "tests",
                "The new required `priority` argument ships with no test; the existing "
                "tests for send() still call it with three arguments and will fail. "
                "Add a test for the new parameter and update the callers.",
                file="app/mail.py",
                line=13,
            ),
        ),
    ),
    BoundaryCase(
        id="headers-in-error-log",
        intended="security",
        rival="error_handling",
        title="Log failed upstream requests",
        body="More context when the upstream API rejects a call.",
        evidence=("header", "authorization", "token", "sensitive", "log"),
        notes=(
            "Rubric 3 names 'logging of sensitive data'; rubric 6 names 'adequate "
            "(but not sensitive) logging'. Both dimensions describe this line; the "
            "defect is that a bearer token reaches the logs, which is the security "
            "dimension's word for it."
        ),
        files=(
            (
                "app/upstream.py",
                "@@ -30,4 +30,7 @@ def call(request):\n"
                "     try:\n"
                "         return _session.send(request)\n"
                "-    except HTTPError:\n"
                "+    except HTTPError as exc:\n"
                '+        logger.error("upstream failed: %s headers=%s", exc, request.headers)\n'
                "         raise\n",
            ),
        ),
        pair=(
            _f(
                "security",
                "The error log prints request.headers, which carries the Authorization "
                "bearer token for the upstream API; the token lands in the log "
                "aggregator in plain text. Log the status and URL, never the headers.",
                file="app/upstream.py",
                line=34,
            ),
            _f(
                "error_handling",
                "The failure log dumps the whole request headers dict, which is noisy "
                "and includes credentials; log the status code and URL so the line is "
                "useful and safe.",
                file="app/upstream.py",
                line=34,
            ),
        ),
    ),
)

BY_ID: dict[str, BoundaryCase] = {case.id: case for case in CASES}

_VALID = set(CATEGORIES)


def _check(case: BoundaryCase) -> None:
    a, b = case.pair
    if case.intended == case.rival:
        raise ValueError(f"{case.id}: intended and rival are the same category")
    if {case.intended, case.rival} - _VALID:
        raise ValueError(f"{case.id}: unknown category in {case.categories}")
    if (a.category, b.category) != case.categories:
        raise ValueError(
            f"{case.id}: pair categories {a.category, b.category} != {case.categories}"
        )
    if (a.file, a.line) != (b.file, b.line) or not a.file or not a.line:
        raise ValueError(f"{case.id}: the pair must share a file and line")
    if a.severity != b.severity:
        raise ValueError(f"{case.id}: the pair must share a severity, or severity decides")
    if not case.evidence:
        raise ValueError(f"{case.id}: no evidence tokens")


for _case in CASES:
    _check(_case)


def pull_request(case: BoundaryCase) -> PullRequest:
    files = [
        ChangedFile(
            filename=name,
            status="added" if patch.startswith("@@ -0,0") else "modified",
            additions=patch.count("\n+"),
            deletions=patch.count("\n-"),
            patch=patch,
        )
        for name, patch in case.files
    ]
    return PullRequest(
        ref=PRRef("snacksnack", "eval-fixture", abs(hash(case.id)) % 900 + 100),
        title=case.title,
        body=case.body,
        state="open",
        author="contributor",
        base_ref="main",
        head_ref=f"boundary/{case.id}",
        head_sha=f"sha-{case.id}",
        files=[*files, *case.extra_files],
        changed_files_count=len(files) + len(case.extra_files),
    )


def repo_files(case: BoundaryCase) -> tuple[tuple[str, str], ...]:
    """Files to lay into the case's checkout for the pipeline mode."""
    return (("CLAUDE.md", CONVENTIONS_PAGE),) if case.needs_conventions else ()
