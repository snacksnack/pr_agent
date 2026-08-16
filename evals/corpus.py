"""Diffs with defects planted on purpose (RC1-253).

One case per entry in `prompts.CATEGORIES`, plus a deliberately clean diff. The
per-category coverage is asserted rather than eyeballed — `tests/test_evals.py`
fails if `CATEGORIES` grows and this corpus does not.

## These are patches, not repositories

The dry-run CLI reviews from the diff alone when no `--repo-path` is given, and
that is the mode used here. It keeps a case to one readable literal, and it
scores the reviewer on what a reviewer actually receives first. The cost is that
the exploration tools are not exercised; `app/agent/tools.py` has its own tests,
and conflating the two would make a failure unattributable.

## Each defect is objective

A planted defect has to be something a competent reviewer would agree is wrong,
or the eval measures taste rather than detection. `eval("...")` on request data
is a defect. Naming a variable `l` is an opinion. Where a category is inherently
softer — `docs`, `convention` — the plant is made unambiguous by the diff
contradicting itself: a docstring that states the opposite of the code, a file
that breaks the convention the surrounding lines establish.

## The clean diff is the load-bearing one

Recall alone is trivially gamed by flagging everything, and a reviewer that
flags everything is the failure mode this repo exists to avoid — its own system
prompt says *"over-flagging trains people to ignore reviews"*. The clean case is
a real, ordinary change: it must draw no blocker, and the number of findings it
does draw is reported as the noise figure.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models import ChangedFile, PRRef, PullRequest


@dataclass(frozen=True)
class PlantedCase:
    """One PR with a known defect, and what a correct review says about it."""

    id: str
    #: The category the finding must carry. `None` for the clean case.
    category: str | None
    #: The minimum severity a correct review assigns. Checked as a floor rather
    #: than an exact match: calling a planted SQL injection a `blocker` when the
    #: corpus says `warning` is not a failure, and asserting equality would
    #: punish a reviewer for caring more than the fixture author did.
    min_severity: str | None
    title: str
    body: str
    files: tuple[tuple[str, str], ...]  # (filename, patch)
    #: Substrings, any of which shows the finding is about the planted defect
    #: rather than something else in the same diff.
    evidence: tuple[str, ...] = ()
    notes: str = ""
    extra_files: tuple[ChangedFile, ...] = field(default=())
    #: Files materialised into a temporary checkout for this case, as
    #: (path, contents). Only the n8n case needs one: the deterministic check
    #: reads workflow JSON off disk, so without a checkout it silently returns
    #: nothing — and a case that silently checks nothing is worse than no case.
    repo_files: tuple[tuple[str, str], ...] = field(default=())


def _pr(case: PlantedCase, number: int) -> PullRequest:
    files = [
        ChangedFile(
            filename=name,
            status="modified",
            additions=patch.count("\n+"),
            deletions=patch.count("\n-"),
            patch=patch,
        )
        for name, patch in case.files
    ]
    return PullRequest(
        ref=PRRef("snacksnack", "eval-fixture", number),
        title=case.title,
        body=case.body,
        state="open",
        author="contributor",
        base_ref="main",
        head_ref=f"planted/{case.id}",
        head_sha=f"sha-{case.id}",
        files=[*files, *case.extra_files],
        changed_files_count=len(files) + len(case.extra_files),
    )


def pull_request(case: PlantedCase) -> PullRequest:
    return _pr(case, abs(hash(case.id)) % 900 + 100)


# --- the corpus -----------------------------------------------------------

CASES: tuple[PlantedCase, ...] = (
    PlantedCase(
        id="leaked-secret",
        category="leaked_secret",
        min_severity="blocker",
        title="Add billing database client",
        body="Wires up the billing service connection.",
        evidence=("credential", "secret", "password", "hardcod", "environment", "connection"),
        notes=(
            "The only category that gates the verdict. Must also exit BLOCKED.\n\n"
            "The credential is a production database password in a connection "
            "string, not a vendor-issued token, and that is deliberate. The "
            "first version used a realistic `sk_live_...` value and GitHub push "
            "protection refused the push — correctly, since it matches Stripe's "
            "published pattern. A fixture has to trip the *reviewer* without "
            "tripping a real secret scanner, so it uses a credential shape no "
            "provider pattern claims."
        ),
        files=(
            (
                "app/billing_db.py",
                "@@ -0,0 +1,10 @@\n"
                "+import psycopg\n"
                "+\n"
                "+# Production billing database.\n"
                '+DATABASE_URL = (\n'
                "+    \"postgresql://billing_svc:Wint3rmute-prod-2026\"\n"
                '+    "@db-billing.internal.example:5432/billing"\n'
                "+)\n"
                "+\n"
                "+\n"
                "+def connect():\n"
                "+    return psycopg.connect(DATABASE_URL)\n",
            ),
        ),
    ),
    PlantedCase(
        id="sql-injection",
        category="security",
        min_severity="warning",
        title="Add user lookup endpoint",
        body="Lets support search users by email.",
        evidence=("injection", "parameter", "sanitiz", "f-string", "format"),
        files=(
            (
                "app/lookup.py",
                "@@ -0,0 +1,7 @@\n"
                "+def find_user(conn, email: str):\n"
                "+    # Search by email\n"
                '+    query = f"SELECT id, name FROM users WHERE email = \'{email}\'"\n'
                "+    cur = conn.cursor()\n"
                "+    cur.execute(query)\n"
                "+    return cur.fetchone()\n",
            ),
        ),
    ),
    PlantedCase(
        id="swallowed-exception",
        category="error_handling",
        min_severity="warning",
        title="Harden the webhook dispatch",
        body="Stops transient failures from crashing the worker.",
        evidence=("except", "swallow", "silent", "bare", "log"),
        files=(
            (
                "app/dispatch.py",
                "@@ -12,7 +12,11 @@ def dispatch(event):\n"
                "-    handler(event)\n"
                "+    try:\n"
                "+        handler(event)\n"
                "+    except Exception:\n"
                "+        pass\n",
            ),
        ),
    ),
    PlantedCase(
        id="breaking-signature",
        category="breaking_change",
        min_severity="warning",
        title="Tidy up the public client",
        body="Small cleanup, no behaviour change.",
        evidence=("breaking", "signature", "caller", "positional", "required"),
        notes="The body claims no behaviour change, which is itself false.",
        files=(
            (
                "app/client.py",
                "@@ -30,7 +30,7 @@ class ApiClient:\n"
                "-    def fetch(self, path, *, timeout=30, retries=3):\n"
                "+    def fetch(self, path, timeout, retries, verify_tls):\n"
                '         """Fetch a path from the API."""\n',
            ),
        ),
    ),
    PlantedCase(
        id="untested-risky-path",
        category="tests",
        min_severity="warning",
        title="Add refund calculation",
        body="Adds proration for mid-cycle cancellations.",
        evidence=("test", "coverage", "untested"),
        notes="Money-handling arithmetic with no accompanying test file.",
        files=(
            (
                "app/refunds.py",
                "@@ -0,0 +1,9 @@\n"
                "+def prorated_refund(cents: int, days_used: int, days_in_period: int) -> int:\n"
                "+    if days_in_period == 0:\n"
                "+        return 0\n"
                "+    unused = days_in_period - days_used\n"
                "+    return int(cents * unused / days_in_period)\n",
            ),
        ),
    ),
    PlantedCase(
        id="unpinned-dependency",
        category="dependencies",
        min_severity="warning",
        title="Add yaml parsing",
        body="Needed for the new config loader.",
        evidence=("pin", "version", "unpinned", "supply", "range"),
        files=(
            (
                "requirements.txt",
                "@@ -10,3 +10,5 @@ httpx>=0.27\n"
                " pytest>=8\n"
                "+pyyaml\n"
                "+requests\n",
            ),
        ),
    ),
    PlantedCase(
        id="pr-drift",
        category="pr_drift",
        min_severity="nit",
        title="Fix typo in the README",
        body="Just a typo fix in the docs — no code changes.",
        evidence=("description", "drift", "readme", "claim", "actually"),
        notes="The body says docs-only; the diff changes auth behaviour.",
        files=(
            (
                "app/auth.py",
                "@@ -44,7 +44,7 @@ def verify_token(token: str) -> bool:\n"
                "-    return hmac.compare_digest(token, expected)\n"
                "+    return token == expected\n",
            ),
        ),
    ),
    PlantedCase(
        id="unbounded-scan",
        category="infra_scalability",
        min_severity="warning",
        title="Add nightly report job",
        body="Emails a summary of every account.",
        evidence=("scale", "memory", "all", "pagina", "unbounded", "batch"),
        files=(
            (
                "app/reports.py",
                "@@ -0,0 +1,8 @@\n"
                "+def nightly_report(db):\n"
                "+    accounts = db.query('SELECT * FROM accounts').fetchall()\n"
                "+    rows = [render(a) for a in accounts]\n"
                "+    return '\\n'.join(rows)\n",
            ),
        ),
    ),
    PlantedCase(
        id="unpythonic-loop",
        category="pythonic",
        min_severity="nit",
        title="Add a helper to collect ids",
        body="Small utility.",
        evidence=("comprehension", "enumerate", "idiom", "pythonic", "range(len"),
        files=(
            (
                "app/util.py",
                "@@ -0,0 +1,7 @@\n"
                "+def ids_of(items):\n"
                "+    out = []\n"
                "+    for i in range(len(items)):\n"
                "+        out.append(items[i].id)\n"
                "+    return out\n",
            ),
        ),
    ),
    PlantedCase(
        id="convention-break",
        category="convention",
        min_severity="nit",
        title="Add a settings accessor",
        body="Convenience wrapper.",
        evidence=("convention", "os.environ", "settings", "consistent", "existing"),
        notes=(
            "CLAUDE.md states the convention outright: config comes from "
            "`app.config.settings`, never `os.environ` directly. The diff "
            "breaks it two lines below a comment restating it."
        ),
        files=(
            (
                "app/feature_flags.py",
                "@@ -0,0 +1,7 @@\n"
                "+import os\n"
                "+\n"
                "+# Config always comes from app.config.settings.\n"
                "+def flag_enabled(name: str) -> bool:\n"
                '+    return os.environ.get(f"FLAG_{name}", "") == "1"\n',
            ),
        ),
    ),
    PlantedCase(
        id="docstring-contradicts-code",
        category="docs",
        min_severity="nit",
        title="Document the retry helper",
        body="Adds a docstring.",
        evidence=("docstring", "doc", "says", "contradic", "actually", "incorrect"),
        notes="The docstring states the opposite of what the code does.",
        files=(
            (
                "app/backoff.py",
                "@@ -1,4 +1,9 @@\n"
                " def delay_for(attempt: int) -> float:\n"
                '+    """Return a constant one-second delay, regardless of attempt.\n'
                "+\n"
                "+    Never grows, so a long outage cannot cause a thundering herd.\n"
                '+    """\n'
                "     return 2 ** attempt\n",
            ),
        ),
    ),
    PlantedCase(
        id="general-dead-code",
        category="general",
        min_severity="nit",
        title="Add a guard to the parser",
        body="Defensive check.",
        evidence=("unreachable", "dead", "never", "after return", "always"),
        files=(
            (
                "app/parser.py",
                "@@ -20,6 +20,10 @@ def parse(payload):\n"
                "     return json.loads(payload)\n"
                "+    if not payload:\n"
                "+        raise ValueError('empty payload')\n",
            ),
        ),
    ),
    # The deterministic check, as its own case. It is not scored on whether the
    # *model* found anything — the finding is computed before the model runs and
    # handed to the loop as already-recorded context. What is scored is that
    # exactly one of them reaches the merged result: zero means the loop dropped
    # it, two means it was merged twice and a reviewer sees the same complaint
    # from a rule and from the model.
    PlantedCase(
        id="n8n-hot-cron",
        category="n8n",
        min_severity="warning",
        title="Add a polling workflow",
        body="Checks the queue on a schedule.",
        evidence=("30 second", "n8n", "interval", "schedule"),
        notes=(
            "Deterministic. Asserts the computed finding reaches the merged "
            "result exactly once. The workflow is otherwise well-formed on "
            "purpose: the first version left `connections` empty, and the model "
            "correctly flagged that as a second, unintended defect — which made "
            "the case test two things and the merge check misread it as a "
            "duplicate."
        ),
        files=(
            (
                "workflows/queue-poller.json",
                "@@ -0,0 +1,20 @@\n+{ new n8n workflow polling every 30 seconds }\n",
            ),
        ),
        repo_files=(
            (
                "workflows/queue-poller.json",
                '''{
  "name": "Queue Poller",
  "nodes": [
    {
      "name": "Schedule Trigger",
      "type": "n8n-nodes-base.scheduleTrigger",
      "parameters": {
        "rule": {"interval": [{"field": "seconds", "secondsInterval": 30}]}
      }
    },
    {
      "name": "Fetch Queue",
      "type": "n8n-nodes-base.httpRequest",
      "parameters": {"url": "https://queue.internal.example/pending", "method": "GET"}
    }
  ],
  "connections": {
    "Schedule Trigger": {"main": [[{"node": "Fetch Queue", "type": "main", "index": 0}]]}
  }
}
''',
            ),
        ),
    ),
    # The clean case. An ordinary, correct change — the precision proxy.
    PlantedCase(
        id="clean",
        category=None,
        min_severity=None,
        title="Extract a helper for period labels",
        body=(
            "Pulls the period-label formatting out of two call sites into one "
            "function, with a test. No behaviour change."
        ),
        notes="Must draw no blocker. Findings here are the noise figure.",
        files=(
            (
                "app/labels.py",
                "@@ -0,0 +1,11 @@\n"
                "+from __future__ import annotations\n"
                "+\n"
                "+from datetime import date\n"
                "+\n"
                "+\n"
                "+def period_label(start: date) -> str:\n"
                '+    """Human-readable label for the week beginning `start`."""\n'
                '+    return f"Week of {start.isoformat()}"\n',
            ),
            (
                "tests/test_labels.py",
                "@@ -0,0 +1,8 @@\n"
                "+from datetime import date\n"
                "+\n"
                "+from app.labels import period_label\n"
                "+\n"
                "+\n"
                "+def test_period_label_uses_iso_dates():\n"
                '+    assert period_label(date(2026, 8, 3)) == "Week of 2026-08-03"\n',
            ),
        ),
    ),
)

BY_ID: dict[str, PlantedCase] = {c.id: c for c in CASES}
