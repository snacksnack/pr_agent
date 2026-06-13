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
        review=lambda p, tools: result,
        out=out,
    )
    return code, out.getvalue()


# --- argument parsing -----------------------------------------------------

def test_pr_flag_is_required():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_bad_pr_spec_returns_error_exit(capsys):
    code = cli.main(["--pr", "not-a-spec"], fetch=lambda ref: _pr(), review=lambda p, t: _result())
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

def test_blocker_severity_blocks(monkeypatch):
    findings = [Finding("blocker", "security", "AWS key committed", file="cfg.py", line=3)]
    code, text = _run(["--pr", "octocat/hello#42"], result=_result(findings))
    assert code == cli.EXIT_BLOCKED
    assert "Verdict: BLOCK" in text


def test_block_on_category_blocks(monkeypatch):
    # A non-blocker finding whose category is in block_on still gates.
    monkeypatch.setattr(cli.settings, "review_block_on", "leaked_secret", raising=False)
    findings = [Finding("warning", "leaked_secret", "token in fixture", file="t.py", line=1)]
    # settings.block_on parses the CSV; confirm our finding category matches it.
    assert "leaked_secret" in cli.settings.block_on
    code, _ = _run(["--pr", "octocat/hello#42"], result=_result(findings))
    assert code == cli.EXIT_BLOCKED


def test_nits_only_do_not_block():
    findings = [Finding("nit", "pythonic", "use a comprehension", file="a.py", line=5)]
    code, _ = _run(["--pr", "octocat/hello#42"], result=_result(findings))
    assert code == cli.EXIT_OK


# --- n8n hook is stub-tolerant --------------------------------------------

def test_n8n_hook_skips_while_stub(tmp_path):
    # A real n8n workflow file on disk, but RC1-112 still raises
    # NotImplementedError -> the hook must add nothing and not crash.
    wf = {"nodes": [], "connections": {}}
    (tmp_path / "flow.json").write_text(json.dumps(wf))
    pr = _pr([ChangedFile(filename="flow.json", status="added")])

    out = io.StringIO()
    code = cli.main(
        ["--pr", "octocat/hello#42", "--repo-path", str(tmp_path)],
        fetch=lambda ref: pr,
        review=lambda p, tools: _result(),
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
        review=lambda p, tools: _result(),
        out=out,
    )
    text = out.getvalue()
    assert "cron fires every minute" in text
    # The n8n finding had no file -> the hook should anchor it to the workflow.
    assert "flow.json" in text
    assert code == cli.EXIT_OK


def test_repo_path_must_be_a_directory(capsys):
    code = cli.main(
        ["--pr", "octocat/hello#42", "--repo-path", "/no/such/dir"],
        fetch=lambda ref: _pr(),
        review=lambda p, tools: _result(),
    )
    assert code == cli.EXIT_ERROR
    assert "directory" in capsys.readouterr().err.lower()


# --- pure formatter -------------------------------------------------------

def test_format_review_without_pr_is_still_valid():
    text = cli.format_review(_result([Finding("nit", "docs", "add docstring")]))
    assert "Summary" in text and "looks good" in text
    assert "[NIT]" in text
