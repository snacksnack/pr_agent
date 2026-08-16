"""Free checks over the planted-defect eval (RC1-253).

The subject itself is billed and stays out of CI. What runs here is the corpus's
own invariants and the scoring, which is where the mistakes live — a scorer that
cannot fail is not a scorer, so every check is exercised in both directions.
"""

from __future__ import annotations

import pytest

from app.agent.prompts import CATEGORIES
from app.models import Finding
from evals import corpus, subject


def test_every_category_has_a_planted_case():
    """The acceptance criterion, asserted rather than eyeballed.

    Adding a category to `CATEGORIES` without a case would otherwise leave a
    silent hole: the suite would stay green while covering less of the rubric.
    """
    covered = {c.category for c in corpus.CASES if c.category}
    missing = sorted(set(CATEGORIES) - covered)
    assert not missing, f"no planted case for: {', '.join(missing)}"


def test_there_is_exactly_one_clean_case_and_it_expects_no_defect():
    clean = [c for c in corpus.CASES if c.category is None]
    assert len(clean) == 1, "the precision proxy is one case, not a mode"
    assert clean[0].min_severity is None


def test_case_ids_are_unique():
    ids = [c.id for c in corpus.CASES]
    assert len(ids) == len(set(ids))


def _finding(**kw) -> Finding:
    return Finding(
        severity=kw.get("severity", "warning"),
        category=kw.get("category", "security"),
        message=kw.get("message", "something"),
        file=kw.get("file"),
        line=kw.get("line"),
        suggestion=kw.get("suggestion"),
    )


SQL = corpus.BY_ID["sql-injection"]


def test_on_target_needs_evidence_not_just_the_right_file():
    """The first run reported every planted case as 0 off-target.

    `_about_the_plant` used to accept a filename match, and every finding names
    the changed file — so the noise figure was a structural zero rather than a
    measurement. Recall was unaffected (evidence-only matching independently
    reproduced 13/13), but a number that cannot vary is not a number.
    """
    by_evidence = _finding(message="This is a SQL injection risk")
    assert subject._about_the_plant(by_evidence, SQL)

    same_file_different_issue = _finding(message="Consider a helper here", file="app/lookup.py")
    assert not subject._about_the_plant(same_file_different_issue, SQL), (
        "a finding about something else in the same file is noise, not recall"
    )

    unrelated = _finding(message="Rename this variable", file="app/other.py")
    assert not subject._about_the_plant(unrelated, SQL)


def test_the_filename_fallback_still_applies_to_a_case_with_no_evidence():
    """Kept for a case that declares no evidence tokens — otherwise nothing
    could ever be on-target for it."""
    from dataclasses import replace

    no_evidence = replace(SQL, evidence=())
    assert subject._about_the_plant(_finding(file="app/lookup.py"), no_evidence)


def test_recall_category_and_severity_are_scored_independently():
    """The whole point of the split: three different failures, three results."""
    correct = subject._score_planted(
        SQL, [_finding(category="security", severity="warning", message="SQL injection")]
    )
    assert all(c.passed for c in correct)

    # Found, but filed under the wrong category — a taxonomy problem, not a miss.
    mislabelled = {
        c.name: c
        for c in subject._score_planted(
            SQL, [_finding(category="general", severity="warning", message="SQL injection")]
        )
    }
    assert mislabelled["finds-the-planted-defect"].passed
    assert not mislabelled["categorises-it-correctly"].passed
    assert mislabelled["severity-is-calibrated"].passed

    # Found and categorised, but under-severe — a calibration problem.
    undersevere = {
        c.name: c
        for c in subject._score_planted(
            SQL, [_finding(category="security", severity="nit", message="SQL injection")]
        )
    }
    assert undersevere["finds-the-planted-defect"].passed
    assert undersevere["categorises-it-correctly"].passed
    assert not undersevere["severity-is-calibrated"].passed

    missed = {c.name: c for c in subject._score_planted(SQL, [])}
    assert not any(c.passed for c in missed.values())


def test_severity_is_a_floor_not_an_equality():
    """Caring more than the fixture author is not a failure."""
    over = subject._score_planted(
        SQL, [_finding(category="security", severity="blocker", message="SQL injection")]
    )
    assert all(c.passed for c in over)


def test_an_off_target_finding_cannot_satisfy_the_severity_floor():
    """A stray blocker elsewhere in the diff must not paper over a weak call
    on the planted defect itself."""
    secret = corpus.BY_ID["leaked-secret"]
    results = {
        c.name: c
        for c in subject._score_planted(
            secret,
            [
                _finding(
                    category="leaked_secret",
                    severity="nit",
                    message="hardcoded credential in the connection string",
                ),
                _finding(category="security", severity="blocker", message="unrelated blocker"),
            ],
        )
    }
    assert results["finds-the-planted-defect"].passed
    assert not results["severity-is-calibrated"].passed, (
        "the blocker was about something else"
    )


@pytest.mark.parametrize("case", corpus.CASES, ids=lambda c: c.id)
def test_every_case_builds_a_pull_request_with_patches(case):
    pr = corpus.pull_request(case)
    assert pr.files, f"{case.id} has no changed files"
    assert all(f.patch for f in pr.files), f"{case.id} has a file with no patch"
    assert pr.title and pr.head_sha


def test_expectations_match_the_corpus():
    """Each `Case` must expect the characteristics its category implies."""
    by_id = {c.id: c for c in subject.CASES}
    assert all("exit-code-matches-the-verdict-policy" in c.expect for c in subject.CASES), (
        "'does not block' is as much a promise as 'blocks'"
    )
    assert by_id["clean"].expect[0] == "raises-no-blocker-on-a-clean-diff"


def test_the_verdict_check_encodes_category_gating_not_severity():
    """`block_on` gates on category. A blocker-severity finding in a non-gating
    category is advisory, and the corpus proves that is not hypothetical."""
    from app import review as review_cli

    secret = corpus.BY_ID["leaked-secret"]
    sql = corpus.BY_ID["sql-injection"]
    blocker = _finding(category="security", severity="blocker", message="SQL injection")

    assert subject._verdict(secret, review_cli.EXIT_BLOCKED, []).passed
    assert not subject._verdict(secret, review_cli.EXIT_OK, []).passed, (
        "a committed secret that does not block is the failure this repo gates on"
    )

    advisory = subject._verdict(sql, review_cli.EXIT_OK, [blocker])
    assert advisory.passed, "a blocker outside block_on must not gate"
    assert "did not gate, as designed" in advisory.detail, "the tension should be visible"

    assert not subject._verdict(sql, review_cli.EXIT_BLOCKED, [blocker]).passed, (
        "blocking on a non-gating category would be a policy regression"
    )


# --- RC1-255: the prompt contract, free and gating ------------------------


def test_the_severity_words_the_scorer_ranks_are_the_ones_the_prompt_defines():
    """`subject._SEVERITY_RANK` orders nit < warning < blocker.

    Those strings come from `SEVERITY_GUIDANCE`. If the prompt were reworded to
    use different labels, every severity check would silently compare against
    values the model never emits — and the planted-defect suite is billed, so
    CI would never notice. This is the free half of that guarantee.
    """
    from app.agent.prompts import SEVERITY_GUIDANCE

    for severity in subject._SEVERITY_RANK:
        assert f"{severity}:" in SEVERITY_GUIDANCE, (
            f"the prompt no longer defines {severity!r}, which the scorer ranks"
        )


def test_every_block_on_category_is_a_real_category():
    """A typo in `block_on` would silently disable gating.

    `verdict.py` gates on category membership. A category that does not exist
    matches nothing, so every review would come back advisory — including one
    with a committed secret — and no test would fail. The eval's leaked-secret
    case asserts the exit code, but it is billed; this runs on every push.
    """
    from app.agent.prompts import CATEGORIES
    from app.config import settings

    unknown = [c for c in settings.block_on if c not in CATEGORIES]
    assert not unknown, (
        f"block_on names {', '.join(unknown)}, which is not in CATEGORIES — "
        "nothing would gate, and the build would stay green"
    )
    assert settings.block_on, "an empty block_on means nothing can ever block a merge"


def test_the_corpus_covers_every_gating_category():
    """Whatever gates must have a planted case proving it gates."""
    from app.config import settings

    covered = {c.category for c in corpus.CASES if c.category}
    assert set(settings.block_on) <= covered, (
        f"{set(settings.block_on) - covered} gate the verdict but have no planted case"
    )


# --- RC1-255: the committed run log is a data artifact --------------------


def test_the_committed_run_log_contains_no_provider_shaped_credential():
    """Run records store model output verbatim, so they inherit its sensitivity.

    This repo's eval plants a credential on purpose and the review agent quotes
    it back in a finding — so the record of that run contains the fixture. That
    is fine while the fixture is an invented value no scanner claims, and it was
    not fine when the fixture used a realistic `sk_live_...`: GitHub push
    protection refused the log, correctly.

    Committing run records (agent-evals/docs/trend.md) makes this a standing
    property rather than a one-off. The guard is here rather than in the harness
    because this is the only repo whose subject deliberately handles secrets.
    """
    import re
    from pathlib import Path

    log = Path(__file__).resolve().parents[1] / "eval-runs" / "runs.jsonl"
    if not log.exists():
        pytest.skip("no committed run log yet")

    provider_shaped = re.compile(
        r"sk_(live|test)_[A-Za-z0-9]{20,}"
        r"|AKIA[0-9A-Z]{16}"
        r"|gh[pousr]_[A-Za-z0-9]{36}"
        r"|xox[baprs]-[A-Za-z0-9-]{10,}"
        r"|AIza[0-9A-Za-z_-]{35}"
        r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    )
    hits = [
        i
        for i, line in enumerate(log.read_text().splitlines(), start=1)
        if provider_shaped.search(line)
    ]
    assert not hits, (
        f"run log line(s) {hits} contain a provider-shaped credential. A planted "
        "secret must be a value no real scanner claims — see the leaked-secret "
        "case in evals/corpus.py for why, and drop the affected records rather "
        "than allowlisting them."
    )
