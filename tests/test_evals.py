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
    clean = [c for c in corpus.CASES if c.category is None and not c.trap]
    assert len(clean) == 1, "the precision proxy is one case, not a mode"
    assert clean[0].min_severity is None


# --- RC1-387: precision cases carry a decoy ----------------------------------


def test_precision_cases_have_a_trap_and_no_category():
    precision = [c for c in corpus.CASES if c.trap]
    assert len(precision) >= 2, "two decoys: the gating category and the deliberate catch-all"
    for case in precision:
        assert case.category is None and case.min_severity is None, case.id
        assert case.evidence == (), f"{case.id}: a decoy is scored on trap, not evidence"
    decoys = {c.id for c in precision}
    assert {"benign-test-secret", "deliberate-broad-except"} <= decoys


def test_a_planted_case_never_carries_a_trap():
    assert not [c.id for c in corpus.CASES if c.category and c.trap]


def test_decoy_scoring_tolerates_a_nit_and_fails_a_warning():
    secret = corpus.BY_ID["benign-test-secret"]

    nothing = subject._score_decoy(secret, [_finding(message="Consider a docstring")])
    assert nothing.passed and "drew nothing" in nothing.detail

    nit = subject._score_decoy(
        secret, [_finding(severity="nit", message="Hardcoded key in test; fine, but note it")]
    )
    assert nit.passed and "tolerated" in nit.detail

    warning = subject._score_decoy(
        secret,
        [_finding(severity="warning", category="leaked_secret", message="Committed secret")],
    )
    assert not warning.passed and "leaked_secret" in warning.detail

    blocker = subject._score_decoy(
        secret, [_finding(severity="blocker", message="Hardcoded credential")]
    )
    assert not blocker.passed


def test_precision_cases_expect_the_decoy_characteristic():
    by_id = {c.id: c for c in subject.CASES}
    assert "does-not-flag-the-decoy" in by_id["benign-test-secret"].expect
    assert "raises-no-blocker-on-a-clean-diff" in by_id["benign-test-secret"].expect
    assert "does-not-flag-the-decoy" not in by_id["clean"].expect
    assert "precision" in by_id["deliberate-broad-except"].tags


def test_verifier_observations_read_false_when_it_did_not_run():
    from app.models import ReviewResult

    assert subject._verifier_observations(None) == {"ran": False}
    off = subject._verifier_observations(ReviewResult())
    assert off["ran"] is False and off["dropped"] == 0
    on = subject._verifier_observations(
        ReviewResult(
            model="claude-sonnet-4-6",
            verified=True,
            verifier_dropped=[_finding(message="gone")],
            verifier_downgraded=2,
        )
    )
    assert on["ran"] and on["dropped"] == 1 and on["downgraded"] == 2
    assert on["dropped_messages"] == ["[warning/security] gone"]


def test_cost_prices_cache_writes_and_reads_not_just_uncached_input():
    """RC1-387: every run since prompt caching (RC1-350, 2026-08-31) priced only
    the uncached `input_tokens` — 5 to 8 per case where the context was ~10K —
    and reported a review at ~40% of its real cost. Cache writes are 1.25x
    the input price, reads 0.1x; both have to be in the number."""
    from decimal import Decimal

    from app.models import TokenUsage

    price = subject.pricing.PRICES["claude-sonnet-4-6"]
    usage = TokenUsage(
        input_tokens=8, output_tokens=1000,
        cache_creation_input_tokens=4000, cache_read_input_tokens=6000,
    )
    cost = subject._cost_usd("claude-sonnet-4-6", usage)
    expected = (
        Decimal(8) * price.input_per_mtok
        + Decimal(1000) * price.output_per_mtok
        + Decimal(4000) * price.input_per_mtok * Decimal("1.25")
        + Decimal(6000) * price.input_per_mtok * Decimal("0.1")
    ) / Decimal(1_000_000)
    assert cost == expected
    uncached_only = subject.pricing.cost_usd("claude-sonnet-4-6", 8, 1000)
    assert cost > uncached_only, "the cache tokens are not free"

    from app.models import ReviewResult

    recorded = subject._usage(
        1.0,
        ReviewResult(
            model="claude-sonnet-4-6", input_tokens=8, output_tokens=1000,
            cache_creation_input_tokens=4000, cache_read_input_tokens=6000,
        ),
    )
    assert recorded.input_tokens == 8, "the API's counts go on the record verbatim (RC1-392)"
    assert recorded.cache_creation_input_tokens == 4000
    assert recorded.cache_read_input_tokens == 6000
    assert recorded.context_tokens == 10008, "the whole context is still one property away"
    assert recorded.cost_usd == expected


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


# --- RC1-390: the multi-agent path in the record -------------------------------


def test_prompt_version_always_carries_the_pipeline_prompts():
    """RC1-422: the reviewers' instructions are the prompt, so their hash is
    always in the version, after the verifier's (RC1-428)."""
    version = subject.prompt_version()
    assert "+multi-sha256:" in version
    assert version.index("+verify-sha256:") < version.index("+multi-sha256:")


def test_multi_observations_read_false_when_there_is_no_result():
    assert subject._multi_observations(None) == {"ran": False}


def test_multi_observations_carry_stages_and_the_cache_premise():
    from app.models import ReviewResult, TokenUsage

    result = ReviewResult(
        model="claude-sonnet-4-6",
        mode="multi",
        reviewers_run=["diff_local", "change_intent"],
        stage_usage={
            "warm_cache": TokenUsage(0, 1, 900, 0),
            "reviewer:diff_local": TokenUsage(3, 40, 0, 900),
            "reviewer:change_intent": TokenUsage(3, 40, 0, 0),
        },
        off_scope_findings=2,
        deduplicated_findings=1,
        unusable_reviewer_calls=0,
    )
    obs = subject._multi_observations(result)
    assert obs["ran"] is True and obs["reviewers"] == ["diff_local", "change_intent"]
    assert "scout_skipped" not in obs and "brief_chars" not in obs
    assert obs["min_reviewer_cache_read"] == 0, "one reviewer wrote the prefix: the premise failed"
    assert obs["stages"]["reviewer:diff_local"]["cache_read"] == 900
    assert obs["stages"]["warm_cache"]["cost_usd"] != "0"
    assert obs["off_scope"] == 2 and obs["deduplicated"] == 1


def test_prompt_version_names_a_checkout_and_the_context_control(monkeypatch):
    """RC1-393: a run against a checkout, and one with the deterministic
    context off, are each their own subject version."""
    from app.config import Settings

    monkeypatch.setattr(subject, "settings", Settings(_env_file=None))
    plain = subject.prompt_version()
    assert subject.prompt_version(checkout=True) == plain + "+checkout"
    assert subject.prompt_version(checkout=True, repo_context=False) == (
        plain + "+checkout+no-context"
    )

    assert subject.version(checkout=True).prompt_version.endswith("+checkout")


def test_multi_observations_carry_the_context_and_the_checkout():
    from app.models import ReviewResult

    result = ReviewResult(
        model="claude-sonnet-4-6", mode="multi", conventions_file="CLAUDE.md", callers_found=5
    )
    obs = subject._multi_observations(result, checkout=True)
    assert obs["checkout"] is True
    assert obs["context"] == {
        "conventions_file": "CLAUDE.md", "callers": 5, "tests": 0, "complete": False
    }
    assert subject._multi_observations(result)["checkout"] is False


def test_materialised_checkout_is_the_repo_copy_with_the_case_files_over_it(tmp_path):
    src = tmp_path / "src"
    (src / "app").mkdir(parents=True)
    (src / ".git").mkdir()
    (src / "__pycache__").mkdir()
    (src / "CLAUDE.md").write_text("rules\n")
    (src / ".env").write_text("SECRET=1\n")
    (src / "app" / "a.py").write_text("a = 1\n")
    (src / ".git" / "HEAD").write_text("ref\n")
    (src / "__pycache__" / "x.pyc").write_bytes(b"\x00")

    into = tmp_path / "into"
    into.mkdir()
    subject.materialise_checkout(into, src, (("workflows/w.json", "{}"),))
    assert (into / "CLAUDE.md").read_text() == "rules\n"
    assert (into / "app" / "a.py").exists()
    assert (into / "workflows" / "w.json").read_text() == "{}"
    assert not (into / ".git").exists() and not (into / "__pycache__").exists()
    assert not (into / ".env").exists(), "a local secret never enters a checkout the model reads"

    bare = tmp_path / "bare"
    bare.mkdir()
    subject.materialise_checkout(bare, None, (("w.json", "{}"),))
    assert sorted(p.name for p in bare.iterdir()) == ["w.json"]
