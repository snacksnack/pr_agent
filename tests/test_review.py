"""Tests for the local dry-run CLI (RC1-113).

Fully offline: ingestion and the review loop are injected as fakes, so no
network, git, or API keys are touched. We assert the orchestration, output
formatting, exit codes, and the stub-tolerant n8n hook.
"""
from __future__ import annotations

import io
import json

import pytest

from app import review as cli
from app.models import Finding, PRRef, PullRequest, ChangedFile, ReviewResult


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
    return ReviewResult(
        summary=summary,
        findings=list(findings or []),
        model="claude-sonnet-4-6",
        tool_turns=2,
        files_read=1,
    )


def _run(argv, *, pr=None, result=None, captured=None):
    pr = pr if pr is not None else _pr()
    result = result if result is not None else _result()
    out = captured if captured is not None else io.StringIO()
    code = cli.main(
        argv,
        fetch=lambda ref: pr,
        review=lambda p, tools, pre: result,
        out=out,
    )
    return code, out.getvalue()


# --- argument parsing -----------------------------------------------------

def test_pr_flag_is_required():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_bad_pr_spec_returns_error_exit(capsys):
    code = cli.main(["--pr", "not-a-spec"], fetch=lambda ref: _pr(), review=lambda p, t, pre: _result())
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


# --- n8n hook is stub-tolerant --------------------------------------------

def test_n8n_hook_skips_benign_workflow(tmp_path):
    # A real but benign n8n workflow file on disk (no costly patterns) -> the
    # hook runs the RC1-112 check and adds nothing, without crashing.
    wf = {"nodes": [], "connections": {}}
    (tmp_path / "flow.json").write_text(json.dumps(wf))
    pr = _pr([ChangedFile(filename="flow.json", status="added")])

    out = io.StringIO()
    code = cli.main(
        ["--pr", "octocat/hello#42", "--repo-path", str(tmp_path)],
        fetch=lambda ref: pr,
        review=lambda p, tools, pre: _result(),
        out=out,
    )
    assert code == cli.EXIT_OK
    assert "No findings" in out.getvalue()


def test_n8n_hook_merges_findings(monkeypatch, tmp_path):
    # Simulate RC1-112 being implemented and returning a finding.
    wf = {"nodes": [{"type": "n8n-nodes-base.cron"}], "connections": {}}
    (tmp_path / "flow.json").write_text(json.dumps(wf))
    pr = _pr([ChangedFile(filename="flow.json", status="modified")])

    monkeypatch.setattr(cli.n8n, "check_workflow", lambda data: [
        {"severity": "warning", "category": "n8n", "message": "cron fires every minute"},
    ])

    out = io.StringIO()
    code = cli.main(
        ["--pr", "octocat/hello#42", "--repo-path", str(tmp_path)],
        fetch=lambda ref: pr,
        review=lambda p, tools, pre: _result(),
        out=out,
    )
    text = out.getvalue()
    assert "cron fires every minute" in text
    # The n8n finding had no file -> the hook should anchor it to the workflow.
    assert "flow.json" in text
    assert code == cli.EXIT_OK


def test_n8n_findings_passed_to_review_as_context(monkeypatch, tmp_path):
    # The deterministic n8n findings must be handed to the review loop (so the
    # model can avoid duplicating them) AND merged into the final result once.
    n8n_finding = Finding("warning", "n8n", "cron fires every minute", file="flow.json")
    monkeypatch.setattr(cli, "run_n8n_checks", lambda pr, root: [n8n_finding])
    pr = _pr([ChangedFile(filename="flow.json", status="modified")])

    seen = {}

    def fake_review(p, tools, precomputed):
        seen["precomputed"] = precomputed
        return _result()

    out = io.StringIO()
    code = cli.main(
        ["--pr", "octocat/hello#42", "--repo-path", str(tmp_path)],
        fetch=lambda ref: pr,
        review=fake_review,
        out=out,
    )
    # The loop received the deterministic finding as context...
    assert seen["precomputed"] == [n8n_finding]
    # ...and it appears exactly once in the final output (merged, not doubled).
    assert out.getvalue().count("cron fires every minute") == 1
    assert code == cli.EXIT_OK


def test_repo_path_must_be_a_directory(capsys):
    code = cli.main(
        ["--pr", "octocat/hello#42", "--repo-path", "/no/such/dir"],
        fetch=lambda ref: _pr(),
        review=lambda p, tools, pre: _result(),
    )
    assert code == cli.EXIT_ERROR
    assert "directory" in capsys.readouterr().err.lower()


# --- pure formatter -------------------------------------------------------

def test_format_review_without_pr_is_still_valid():
    text = cli.format_review(_result([Finding("nit", "docs", "add docstring")]))
    assert "Summary" in text and "looks good" in text
    assert "[NIT]" in text
