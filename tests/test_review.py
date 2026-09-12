"""Tests for the local dry-run CLI (RC1-113).

Fully offline: ingestion and the review pipeline are injected as fakes, so no
network, git, or API keys are touched. We assert the orchestration, output
formatting and exit codes; the deterministic checks run inside the pipeline
(RC1-425) and are tested there.
"""
from __future__ import annotations

import io

import pytest

from app import review as cli
from app.models import (
    ChangedFile,
    Finding,
    PRRef,
    PullRequest,
    ReviewOutcome,
    ReviewResult,
    RunMetrics,
)

# --- helpers --------------------------------------------------------------

def _pr(files=None) -> PullRequest:
    return PullRequest(
        ref=PRRef("octocat", "hello", 42),
        title="Add greeting",
        body="adds a greeting",
        base_sha="b" * 12,
        head_sha="h" * 12,
        changed_files_count=len(files or []),
        files=files or [],
        html_url="https://github.com/octocat/hello/pull/42",
    )


def _result(findings=None, summary="looks good") -> ReviewResult:
    return ReviewResult(summary=summary, findings=list(findings or []))


def _outcome(result=None, metrics=None) -> ReviewOutcome:
    """What the injected review returns (RC1-429): the review plus the run."""
    return ReviewOutcome(
        result if result is not None else _result(),
        metrics if metrics is not None else RunMetrics(model="claude-sonnet-4-6"),
    )


def _run(argv, *, pr=None, result=None, captured=None):
    pr = pr if pr is not None else _pr()
    result = result if result is not None else _result()
    out = captured if captured is not None else io.StringIO()
    code = cli.main(
        argv,
        fetch=lambda ref: pr,
        review=lambda p, repository: _outcome(result),
        out=out,
    )
    return code, out.getvalue()


# --- argument parsing -----------------------------------------------------

def test_pr_flag_is_required():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_bad_pr_spec_returns_error_exit(capsys):
    code = cli.main(
        ["--pr", "not-a-spec"], fetch=lambda ref: _pr(), review=lambda p, t, pre: _result()
    )
    assert code == cli.EXIT_ERROR
    assert "error" in capsys.readouterr().err.lower()


# --- happy path & formatting ---------------------------------------------

def test_clean_review_exit_ok_and_output():
    code, text = _run(["--pr", "octocat/hello#42"])
    assert code == cli.EXIT_OK
    assert "Review of octocat/hello#42" in text
    assert "https://github.com/octocat/hello/pull/42" in text
    assert "No findings" in text
    assert "Verdict: advisory" in text


def test_findings_render_severity_location_message_and_fix():
    findings = [
        Finding("warning", "tests", "no test for the new branch",
                file="app/x.py", line=12, suggestion="add a unit test"),
    ]
    code, text = _run(["--pr", "octocat/hello#42"], result=_result(findings))
    assert code == cli.EXIT_OK
    assert "[WARNING] app/x.py:12  (tests)" in text
    assert "no test for the new branch" in text
    assert "fix: add a unit test" in text
    assert "Totals: 0 blocker(s), 1 warning(s), 0 nit(s)" in text


# --- exit code reflects block_on ------------------------------------------

def test_leaked_secret_category_blocks(monkeypatch):
    # The committed-secret category is the canonical gate.
    findings = [Finding("blocker", "leaked_secret", "AWS key committed", file="cfg.py", line=3)]
    assert "leaked_secret" in cli.settings.block_on  # default block_on
    code, text = _run(["--pr", "octocat/hello#42"], result=_result(findings))
    assert code == cli.EXIT_BLOCKED
    assert "Verdict: BLOCK" in text


def test_non_secret_blocker_is_advisory(monkeypatch):
    # RC1-114 carryover: a blocker-severity finding that ISN'T a block_on
    # category (e.g. a correctness defect) is surfaced but does NOT gate.
    findings = [Finding("blocker", "security", "missing authz check", file="a.py", line=3)]
    code, _ = _run(["--pr", "octocat/hello#42"], result=_result(findings))
    assert code == cli.EXIT_OK


def test_block_on_is_configurable(monkeypatch):
    # A non-blocker finding whose category is in a configured block_on gates.
    monkeypatch.setattr(cli.settings, "review_block_on", "security", raising=False)
    findings = [Finding("warning", "security", "weak crypto", file="t.py", line=1)]
    assert "security" in cli.settings.block_on  # parsed from the CSV
    code, _ = _run(["--pr", "octocat/hello#42"], result=_result(findings))
    assert code == cli.EXIT_BLOCKED


def test_nits_only_do_not_block():
    findings = [Finding("nit", "pythonic", "use a comprehension", file="a.py", line=5)]
    code, _ = _run(["--pr", "octocat/hello#42"], result=_result(findings))
    assert code == cli.EXIT_OK


# --- the CLI loads and prints; the pipeline owns the result (RC1-425) --------

def test_the_cli_hands_the_pipeline_the_checkout_and_prints_its_result_once(tmp_path):
    # What the pipeline returns is what is printed — the CLI merges nothing.
    from app.agent.local_repository import LocalRepository

    pr = _pr([ChangedFile(filename="flow.json", status="modified")])
    result = _result([Finding("warning", "n8n", "cron fires every minute", file="flow.json")])
    seen = {}

    def fake_review(p, repository):
        seen["repository"] = repository
        return _outcome(result)

    out = io.StringIO()
    code = cli.main(
        ["--pr", "octocat/hello#42", "--repo-path", str(tmp_path)],
        fetch=lambda ref: pr,
        review=fake_review,
        out=out,
    )
    assert isinstance(seen["repository"], LocalRepository)
    assert seen["repository"].root == tmp_path
    assert out.getvalue().count("cron fires every minute") == 1
    assert "flow.json" in out.getvalue()
    assert code == cli.EXIT_OK


def test_without_a_checkout_the_pipeline_gets_an_empty_repository():
    seen = {}

    def fake_review(p, repository):
        seen["explorable"] = repository.explorable  # the temp dir is gone after main()
        return _outcome()

    code = cli.main(
        ["--pr", "octocat/hello#42"], fetch=lambda ref: _pr(), review=fake_review, out=io.StringIO()
    )
    assert code == cli.EXIT_OK
    assert seen["explorable"] is False


def test_format_review_names_the_checks_the_pipeline_ran():
    ran = RunMetrics(checks_run=("n8n",), deterministic_findings=1)
    assert "checks(run=n8n, findings=1)" in cli.format_review(_result(), metrics=ran)
    failed = RunMetrics(checks_run=("n8n",), deterministic_findings=1, checks_failed=("boom",))
    text = cli.format_review(_result(), metrics=failed)
    assert "checks(run=n8n, findings=1, failed=boom)" in text
    assert "checks(" not in cli.format_review(_result(), metrics=RunMetrics())


def test_format_review_without_metrics_prints_the_review_alone():
    # RC1-429: the review reads the same without the run's diagnostics; no
    # fake defaults are printed in their place.
    text = cli.format_review(_result([Finding("nit", "pythonic", "tidy", file="a.py", line=1)]))
    assert "tidy" in text and "Totals:" in text
    assert "model=" not in text and "reviewers=" not in text and "context(" not in text


# --- repo path + pure formatter (restored in RC1-429: the RC1-425 cut dropped them) ---

def test_repo_path_must_be_a_directory(capsys):
    code = cli.main(
        ["--pr", "octocat/hello#42", "--repo-path", "/no/such/dir"],
        fetch=lambda ref: _pr(),
        review=lambda p, repository: _outcome(),
    )
    assert code == cli.EXIT_ERROR
    assert "directory" in capsys.readouterr().err.lower()


def test_format_review_without_pr_is_still_valid():
    text = cli.format_review(_result([Finding("nit", "docs", "add docstring")]))
    assert "Summary" in text and "looks good" in text
    assert "[NIT]" in text


def test_format_review_names_the_reviewers_and_the_merge():
    metrics = RunMetrics(
        model="claude-sonnet-4-6",
        reviewers_run=("diff_local", "repo_context", "change_intent"),
        off_scope_findings=1,
    )
    text = cli.format_review(_result([Finding("nit", "docs", "add docstring")]), metrics=metrics)
    assert "reviewers=diff_local,repo_context,change_intent" in text
    assert "context(conventions=none, callers=0)" in text
    assert "merged(off_scope=1, deduplicated=0)" in text
    assert "mode=" not in text


def test_format_review_names_the_context_python_gathered():
    metrics = RunMetrics(
        model="claude-sonnet-4-6", reviewers_run=("diff_local",),
        conventions_file="CLAUDE.md", callers_found=7,
    )
    text = cli.format_review(_result(), metrics=metrics)
    assert "context(conventions=CLAUDE.md, callers=7)" in text
