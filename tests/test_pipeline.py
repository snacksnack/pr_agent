"""Tests for the review pipeline (RC1-390, RC1-422, RC1-427). Offline: scripted
fakes for the sync client (the verifier) and the async client (warm call,
reviewers)."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from app.agent import pipeline
from app.agent.local_repository import LocalRepository
from app.agent.prompts import CHANGE_INTENT, DIFF_LOCAL, REPO_CONTEXT
from app.agent.router import ReviewPlan
from app.models import ChangedFile, Finding, PRRef, PullRequest, TokenUsage


def _usage(uncached=3, out=4, write=0, read=900):
    return SimpleNamespace(
        input_tokens=uncached,
        output_tokens=out,
        cache_creation_input_tokens=write,
        cache_read_input_tokens=read,
    )


class SyncMessages:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if not self._scripted:
            raise AssertionError("sync fake ran out of scripted responses")
        return SimpleNamespace(content=self._scripted.pop(0), usage=_usage(10, 5, 100, 0))


class AsyncMessages:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if not self._scripted:
            raise AssertionError("async fake ran out of scripted responses")
        content = self._scripted.pop(0)
        # The warm call is the first request and writes the prefix; the rest read it.
        usage = _usage(0, 1, 900, 0) if len(self.calls) == 1 else _usage()
        return SimpleNamespace(content=content, usage=usage)


def _sync(*scripted):
    return SimpleNamespace(messages=SyncMessages(scripted))


def _async(*scripted):
    return SimpleNamespace(messages=AsyncMessages(scripted))


def _use(name, **inp):
    return {"type": "tool_use", "id": "t", "name": name, "input": inp}


def _submit(summary, findings):
    return [_use("submit_review", summary=summary, findings=findings)]


def _finding(severity, category, message, file="app/x.py", line=3):
    return {
        "severity": severity, "category": category, "message": message, "file": file, "line": line
    }


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "x.py").write_text("x = 1\n")
    return LocalRepository(root)


@pytest.fixture()
def pr():
    return PullRequest(
        ref=PRRef("o", "r", 9),
        title="Change x",
        body="changes x",
        files=[ChangedFile("app/x.py", "modified", patch="@@ -1 +1 @@\n-x = 0\n+x = 1")],
    )


WARM = []


def _run(pr, repo, sync, async_client, **kw):
    """The outcome (RC1-429): ``.review`` is what is posted, ``.metrics`` the run."""
    kw.setdefault("model", "m")
    return pipeline.review_pull_request(pr, repo, client=sync, async_client=async_client, **kw)


# --- the happy path -----------------------------------------------------------

def test_three_reviewers_then_merge(pr, repo):
    sync = _sync([_use("verify_findings", verdicts=[])])
    async_client = _async(
        WARM,
        _submit("A secret is committed.", [_finding("blocker", "leaked_secret", "key in code")]),
        _submit("No tests cover x.", [_finding("warning", "tests", "untested", line=9)]),
        _submit("", []),
    )
    result = _run(pr, repo, sync, async_client)

    assert result.metrics.mode == "multi"
    assert list(result.metrics.reviewers_run) == ["diff_local", "repo_context", "change_intent"]
    assert [(f.severity, f.category) for f in result.review.findings] == [
        ("blocker", "leaked_secret"),
        ("warning", "tests"),
    ]
    assert result.review.summary == "A secret is committed. No tests cover x."
    # RC1-428: the verifier is a stage; the sync client serves it and nothing else.
    assert result.metrics.verified is True and len(sync.messages.calls) == 1
    assert set(result.metrics.stage_usage) == {
        "warm_cache", "reviewer:diff_local", "reviewer:repo_context", "reviewer:change_intent",
        "verifier",
    }


def test_every_call_that_shares_the_prefix_sends_it_identically(pr, repo):
    """The design's premise: warm call, reviewers and verifier send the same
    tools, system, tool_choice and first content block, so the API serves
    one cache entry to all of them."""
    sync = _sync([_use("verify_findings", verdicts=[])])
    async_client = _async(
        WARM,
        _submit("s", [_finding("warning", "security", "injection")]),
        _submit("", []),
        _submit("", []),
    )
    _run(pr, repo, sync, async_client)

    calls = async_client.messages.calls + [sync.messages.calls[-1]]
    assert len(calls) == 5
    first = calls[0]
    for call in calls:
        assert call["tools"] == first["tools"]
        assert call["system"] == first["system"]
        assert call["tool_choice"] == {"type": "any"}
        prefix = call["messages"][0]["content"][0]
        assert prefix == first["messages"][0]["content"][0]
        assert prefix["cache_control"] == {"type": "ephemeral"}
    assert [t["name"] for t in first["tools"]] == ["submit_review", "verify_findings"]
    # The prefix carries the PR and the context; the suffix is per call.
    prefix_text = first["messages"][0]["content"][0]["text"]
    assert "Pull request: o/r#9" in prefix_text and "Tests touching" in prefix_text
    assert len(first["messages"][0]["content"]) == 1 and first["max_tokens"] == 1  # warm call
    suffixes = [c["messages"][0]["content"][1]["text"] for c in calls[1:]]
    assert "'diff_local' reviewer" in suffixes[0]
    assert "'repo_context' reviewer" in suffixes[1]
    assert "'change_intent' reviewer" in suffixes[2]
    assert "verifying a first-pass review" in suffixes[3]


def test_reviewer_suffix_carries_only_its_rubric_slice(pr, repo):
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    _run(pr, repo, _sync(), async_client)
    diff_local = async_client.messages.calls[1]["messages"][0]["content"][1]["text"]
    assert "3. Security & secrets" in diff_local and "2. Pythonic-ness" in diff_local
    assert "1. Convention consistency" not in diff_local
    assert "leaked_secret, security, pythonic, error_handling, docs" in diff_local
    intent = async_client.messages.calls[3]["messages"][0]["content"][1]["text"]
    assert "8. PR-description" in intent and "5. Dependency" not in intent


def test_token_usage_is_summed_across_every_stage(pr, repo):
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    result = _run(pr, repo, _sync(), async_client)
    # warm (0,1,900,0) + 3 reviewers (3,4,0,900); no scout since RC1-427
    assert result.metrics.usage == TokenUsage(9, 13, 900, 2700)
    assert result.metrics.stage_usage["warm_cache"].cache_creation_input_tokens == 900
    assert all(
        result.metrics.stage_usage[f"reviewer:{n}"].cache_read_input_tokens == 900
        for n in ("diff_local", "repo_context", "change_intent")
    )


# --- the merge ----------------------------------------------------------------

def _out(spec, findings, summary=""):
    return pipeline.ReviewerOutput(spec, summary=summary, findings=findings)


def test_merge_discards_findings_outside_the_reviewers_categories():
    outputs = [
        _out(DIFF_LOCAL, [Finding("warning", "tests", "not mine", "a.py", 1)]),
        _out(REPO_CONTEXT, [Finding("warning", "tests", "mine", "a.py", 1)]),
    ]
    merged, off_scope, deduplicated = pipeline.merge_findings(outputs)
    assert [f.message for f in merged] == ["mine"]
    assert off_scope == 1 and deduplicated == 0


def test_merge_allows_general_from_any_reviewer():
    outputs = [_out(CHANGE_INTENT, [Finding("nit", "general", "dead code", "a.py", 4)])]
    merged, off_scope, _ = pipeline.merge_findings(outputs)
    assert len(merged) == 1 and off_scope == 0


def test_merge_folds_same_file_line_category_keeping_the_more_severe():
    outputs = [
        _out(DIFF_LOCAL, [Finding("nit", "general", "first", "a.py", 4)]),
        _out(REPO_CONTEXT, [Finding("warning", "general", "second", "a.py", 4)]),
        _out(CHANGE_INTENT, [Finding("nit", "general", "third", "a.py", 4)]),
    ]
    merged, _, deduplicated = pipeline.merge_findings(outputs)
    assert [(f.severity, f.message) for f in merged] == [("warning", "second")]
    assert deduplicated == 2


def test_merge_never_folds_pr_level_findings():
    outputs = [
        _out(CHANGE_INTENT, [Finding("nit", "pr_drift", "a"), Finding("nit", "pr_drift", "b")]),
    ]
    merged, _, deduplicated = pipeline.merge_findings(outputs)
    assert len(merged) == 2 and deduplicated == 0


def test_summary_leads_with_the_reviewer_holding_the_most_serious_finding():
    outputs = [
        _out(DIFF_LOCAL, [Finding("nit", "docs", "d")], summary="Docs nit."),
        _out(REPO_CONTEXT, [Finding("warning", "tests", "t")], summary="Untested."),
        _out(CHANGE_INTENT, [], summary="   "),
    ]
    assert pipeline.compose_summary(outputs) == "Untested. Docs nit."


def test_summary_when_nobody_found_anything():
    outputs = [_out(DIFF_LOCAL, []), _out(CHANGE_INTENT, [])]
    assert pipeline.compose_summary(outputs) == (
        "No issues found by the diff_local, change_intent reviewers."
    )


# --- degraded reviewer answers ----------------------------------------------

def test_reviewer_calling_the_wrong_tool_is_counted_not_crashed(pr, repo):
    async_client = _async(
        WARM,
        [_use("verify_findings", verdicts=[])],  # the wrong tool
        [{"type": "text", "text": "no tool at all"}],
        _submit("fine", [_finding("nit", "pr_drift", "d", file=None, line=None)]),
    )
    result = _run(pr, repo, _sync([_use("verify_findings", verdicts=[])]), async_client)
    assert result.metrics.unusable_reviewer_calls == 2
    assert [f.category for f in result.review.findings] == ["pr_drift"]
    assert result.review.summary == "fine"


def test_malformed_and_coerced_findings_are_counted_across_reviewers(pr, repo):
    async_client = _async(
        WARM,
        _submit("", [{"severity": "breaking_change", "category": "security", "message": "m"}]),
        _submit("", [{"category": "tests"}]),  # no severity or message
        _submit("", []),
    )
    result = _run(pr, repo, _sync([_use("verify_findings", verdicts=[])]), async_client)
    assert result.metrics.coerced_findings == 1 and result.metrics.malformed_findings == 1
    assert result.review.findings[0].severity == "warning"


# --- routing and the context --------------------------------------------------------

def test_documentation_only_change_skips_the_context_and_the_repo_context_reviewer(repo):
    pr = PullRequest(
        ref=PRRef("o", "r", 2),
        title="Docs",
        files=[ChangedFile("README.md", "modified", patch="+hello")],
    )
    async_client = _async(WARM, _submit("", []), _submit("", []))
    result = _run(pr, repo, _sync(), async_client)
    assert list(result.metrics.reviewers_run) == ["diff_local", "change_intent"]
    assert result.metrics.conventions_file is None and result.metrics.callers_found == 0
    assert "Repository conventions" not in (
        async_client.messages.calls[0]["messages"][0]["content"][0]["text"]
    )


def test_an_explicit_plan_is_honoured(pr, repo):
    plan = ReviewPlan(context=False, reviewers=(DIFF_LOCAL,), reasons=("test",))
    async_client = _async(WARM, _submit("only me", []))
    result = _run(pr, repo, _sync(), async_client, plan=plan)
    assert list(result.metrics.reviewers_run) == ["diff_local"]
    assert result.review.summary == "only me"


# --- the deterministic checks (RC1-425) ------------------------------------------

HOT_CRON = json.dumps(
    {
        "nodes": [
            {
                "name": "Cron",
                "type": "n8n-nodes-base.cron",
                "parameters": {"triggerTimes": {"item": [{"mode": "everyMinute"}]}},
            }
        ],
        "connections": {},
    }
)


def _workflow_pr(pr):
    pr.files.append(ChangedFile("flows/poll.json", "added", patch="@@ -0,0 +1 @@\n+{}"))
    return pr


def _quiet():
    """Three reviewers with nothing to say; the sync fake has no scripted
    response, so any verifier call fails the test."""
    return _async(WARM, _submit("", []), _submit("", []), _submit("", []))


def _prefix(async_client):
    return async_client.messages.calls[0]["messages"][0]["content"][0]["text"]


def test_a_check_finding_is_in_the_prefix_and_in_the_result_once(pr, repo):
    (repo.root / "flows").mkdir()
    (repo.root / "flows" / "poll.json").write_text(HOT_CRON)
    async_client = _quiet()
    result = _run(_workflow_pr(pr), repo, _sync(), async_client)
    assert [(f.category, f.file) for f in result.review.findings] == [("n8n", "flows/poll.json")]
    assert "every minute" in result.review.findings[0].message
    assert list(result.metrics.checks_run) == ["n8n"] and list(result.metrics.checks_failed) == []
    assert result.metrics.deterministic_findings == 1
    assert "checks" in result.metrics.stage_latency_ms
    # The reviewers read it as already recorded: after the diff, before the context.
    prefix = _prefix(async_client)
    assert "already recorded by automated checks" in prefix
    at = prefix.index("every minute")
    assert prefix.index("--- flows/poll.json") < at
    assert "Callers of what changed" not in prefix[:at]
    # Nothing to verify: the verifier judges the model's claims, not the check's.
    assert result.metrics.verified is False


def test_the_verifier_judges_only_the_models_findings(pr, repo):
    (repo.root / "flows").mkdir()
    (repo.root / "flows" / "poll.json").write_text(HOT_CRON)
    claim = _finding("warning", "security", "model claim")
    async_client = _async(WARM, _submit("s", [claim]), _submit("", []), _submit("", []))
    drop_first = [{"index": 0, "decision": "drop", "reason": "no"}]
    sync = _sync([_use("verify_findings", verdicts=drop_first)])
    result = _run(_workflow_pr(pr), repo, sync, async_client)
    suffix = sync.messages.calls[0]["messages"][0]["content"][1]["text"]
    assert "[0] warning / security" in suffix and "[1]" not in suffix
    assert "every minute" not in suffix
    assert [f.message for f in result.metrics.verifier_dropped] == ["model claim"]
    assert [f.category for f in result.review.findings] == ["n8n"]
    assert result.metrics.verified is True and result.metrics.deterministic_findings == 1


def test_no_workflow_changed_means_the_check_ran_and_found_nothing(pr, repo):
    async_client = _quiet()
    result = _run(pr, repo, _sync(), async_client)
    assert list(result.metrics.checks_run) == ["n8n"] and result.metrics.deterministic_findings == 0
    assert result.review.findings == []
    assert "already recorded" not in _prefix(async_client)


def test_no_checks_at_all_leaves_the_result_the_models_own(pr, repo):
    async_client = _quiet()
    result = _run(pr, repo, _sync(), async_client, checks=())
    assert list(result.metrics.checks_run) == [] and result.metrics.deterministic_findings == 0
    assert "already recorded" not in _prefix(async_client)


def test_several_checks_run_in_order_and_each_is_recorded(pr, repo):
    from app.agent.checks import Check

    def first(pull, read):
        return [Finding("nit", "general", "from first", file="app/x.py")]

    def second(pull, read):
        return [Finding("warning", "general", "from second", file="app/x.py")]

    claim = _finding("warning", "security", "model claim")
    async_client = _async(WARM, _submit("s", [claim]), _submit("", []), _submit("", []))
    sync = _sync([_use("verify_findings", verdicts=[])])
    checks = (Check("first", first), Check("second", second))
    result = _run(pr, repo, sync, async_client, checks=checks)
    messages = [f.message for f in result.review.findings]
    assert messages == ["model claim", "from first", "from second"]
    assert list(result.metrics.checks_run) == ["first", "second"]
    assert result.metrics.deterministic_findings == 2


def test_a_failing_check_is_recorded_and_the_review_goes_on(pr, repo, caplog):
    from app.agent.checks import Check

    def boom(pull, read):
        raise RuntimeError("bad export")

    def fine(pull, read):
        return [Finding("nit", "general", "still here", file="app/x.py")]

    caplog.set_level("INFO", logger="app.agent.checks")
    result = _run(pr, repo, _sync(), _quiet(), checks=(Check("boom", boom), Check("fine", fine)))
    assert list(result.metrics.checks_failed) == ["boom"]
    assert list(result.metrics.checks_run) == ["fine"]
    assert [f.message for f in result.review.findings] == ["still here"]
    assert "check_failed name=boom" in caplog.text
    assert "bad export" in caplog.text  # the traceback is logged, not swallowed


def test_checks_read_a_changed_file_whole_not_clipped_for_the_prefix(pr, repo):
    from app.agent.repository import MAX_READ_BYTES

    data = json.loads(HOT_CRON)
    data["meta"] = {"padding": "x" * (MAX_READ_BYTES + 1000)}  # a real export can top 90 KB
    (repo.root / "flows").mkdir()
    (repo.root / "flows" / "poll.json").write_text(json.dumps(data))
    result = _run(_workflow_pr(pr), repo, _sync(), _quiet())
    assert result.metrics.deterministic_findings == 1


def test_checks_read_through_the_github_adapter_at_the_pr_head(pr):
    from app.agent.github_repository import GitHubRepository

    fetched = []

    class FakeGitHub:
        def get_file_text(self, ref, path, *, git_ref=None):
            fetched.append((path, git_ref))
            return HOT_CRON if path == "flows/poll.json" else None

        def get_tree(self, ref, sha):
            return [{"path": "flows/poll.json", "type": "blob", "size": len(HOT_CRON)}]

    pr = _workflow_pr(pr)
    pr.head_sha = "headsha"
    repository = GitHubRepository(
        FakeGitHub(), pr.ref, "headsha", changed_files=[f.filename for f in pr.files]
    )
    result = _run(pr, repository, _sync(), _quiet())
    assert result.metrics.deterministic_findings == 1
    assert ("flows/poll.json", "headsha") in fetched


# --- the verifier ------------------------------------------------------------------

def test_verifier_runs_on_the_merged_findings_and_reads_the_shared_prefix(pr, repo):
    sync = _sync(
        [_use("verify_findings", verdicts=[{"index": 0, "decision": "drop", "reason": "fixture"}])],
    )
    async_client = _async(
        WARM,
        _submit("", [_finding("blocker", "leaked_secret", "test key")]),
        _submit("", [_finding("warning", "tests", "untested", line=9)]),
        _submit("", []),
    )
    result = _run(pr, repo, sync, async_client)

    assert result.metrics.verified is True
    assert [f.category for f in result.review.findings] == ["tests"]
    assert [f.category for f in result.metrics.verifier_dropped] == ["leaked_secret"]
    # RC1-390 bookkeeping survives the verifier's copy, and its stage is added.
    assert result.metrics.mode == "multi" and result.metrics.reviewers_run[0] == "diff_local"
    assert "verifier" in result.metrics.stage_usage
    assert result.metrics.usage.cache_creation_input_tokens == 1000  # warm 900 + verifier 100


def test_verifier_is_skipped_when_there_is_nothing_to_verify(pr, repo):
    sync = _sync()
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    result = _run(pr, repo, sync, async_client)
    assert result.metrics.verified is False and sync.messages.calls == []


def test_empty_checkout_skips_the_context_and_the_reviewers_still_run(pr, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    result = _run(pr, LocalRepository(empty), _sync(), async_client)
    assert list(result.metrics.reviewers_run) == ["diff_local", "repo_context", "change_intent"]
    assert result.metrics.conventions_file is None and not result.metrics.context_complete
    assert set(result.metrics.stage_latency_ms) == {"checks", "context", "fan_out"}


def test_stage_latency_is_recorded_for_the_verifier_too(pr, repo):
    sync = _sync([_use("verify_findings", verdicts=[])])
    async_client = _async(
        WARM, _submit("", [_finding("nit", "docs", "d")]), _submit("", []), _submit("", [])
    )
    result = _run(pr, repo, sync, async_client)
    assert set(result.metrics.stage_latency_ms) == {"checks", "context", "fan_out", "verifier"}
    assert all(v >= 0 for v in result.metrics.stage_latency_ms.values())


# --- RC1-393: deterministic repository context --------------------------------

def _repo_with_conventions(tmp_path):
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "CLAUDE.md").write_text("# Notes\n\n## Conventions\n\n- read config via settings\n")
    (root / "app" / "x.py").write_text("def helper():\n    return 1\n")
    (root / "app" / "y.py").write_text("from app.x import helper\n\nvalue = helper()\n")
    return LocalRepository(root)


def _pr_changing_helper():
    return PullRequest(
        ref=PRRef("o", "r", 9),
        title="Change helper",
        body="changes helper",
        files=[
            ChangedFile(
                "app/x.py",
                "modified",
                patch="@@ -1 +1 @@\n-def helper():\n+def helper(flag=False):",
            )
        ],
    )


def test_context_reaches_the_shared_prefix(tmp_path):
    """Conventions, callers and tests, gathered by Python, are the prefix's
    context block — the review's whole exploration since RC1-427."""
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    result = _run(_pr_changing_helper(), _repo_with_conventions(tmp_path), _sync(), async_client)

    prefix = async_client.messages.calls[0]["messages"][0]["content"][0]["text"]
    assert "Repository conventions, from CLAUDE.md" in prefix
    assert "read config via settings" in prefix
    assert "app/y.py:1: from app.x import helper" in prefix
    assert "app/y.py:3: value = helper()" in prefix
    assert "Tests touching the changed paths" in prefix
    assert prefix.index("Repository conventions") < prefix.index("Callers of what changed")
    assert "Scout's brief" not in prefix and "scout" not in prefix.lower()

    assert result.metrics.conventions_file == "CLAUDE.md"
    assert result.metrics.callers_found == 2
    assert result.metrics.stage_latency_ms["context"] >= 0
    assert result.metrics.context_complete
    # Every reviewer's suffix carries the missing-evidence guard.
    for call in async_client.messages.calls[1:]:
        assert "raise nothing about the missing evidence itself" in (
            call["messages"][0]["content"][1]["text"]
        )


def test_no_context_leaves_the_pr_plus_the_tests_line(pr, repo):
    """No conventions file and no symbols in the diff: the conventions and
    callers blocks are absent, so the prefix is the PR plus the one tests
    block RC1-394 always renders for a source file — here, that the
    repository has no tests."""
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    result = _run(pr, repo, _sync(), async_client)
    prefix = async_client.messages.calls[0]["messages"][0]["content"][0]["text"]
    tests_block = (
        "Tests touching the changed paths (found by grep by file name, no test "
        "directory found; changed source files: app/x.py):\n"
        "(no test files found in the repository)"
    )
    assert prefix == pipeline.build_shared_prefix(pr, None, tests_block)
    assert "Repository conventions" not in prefix and "Callers of what changed" not in prefix
    assert not result.metrics.context_complete


def test_context_can_be_switched_off_for_measurement(tmp_path):
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    result = _run(
        _pr_changing_helper(),
        _repo_with_conventions(tmp_path),
        _sync(),
        async_client,
        repo_context=False,
    )
    prefix = async_client.messages.calls[0]["messages"][0]["content"][0]["text"]
    assert "Repository conventions" not in prefix
    assert result.metrics.conventions_file is None and result.metrics.callers_found == 0


def test_context_is_not_gathered_when_there_is_nothing_to_explore(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    result = _run(_pr_changing_helper(), LocalRepository(empty), _sync(), async_client)
    assert result.metrics.conventions_file is None and result.metrics.callers_found == 0


def test_context_survives_the_verifier(tmp_path):
    sync = _sync([_use("verify_findings", verdicts=[])])
    async_client = _async(
        WARM, _submit("", [_finding("nit", "docs", "d")]), _submit("", []), _submit("", [])
    )
    result = _run(
        _pr_changing_helper(), _repo_with_conventions(tmp_path), sync, async_client
    )
    assert result.metrics.verified
    assert result.metrics.conventions_file == "CLAUDE.md" and result.metrics.callers_found == 2


class _NoFileList(LocalRepository):
    """A checkout whose file list cannot be read (the live path with the
    tree call out of budget): conventions and callers are answered, the
    tests search is not, so the context is answered but not complete."""

    def paths(self):
        return None


def test_a_context_without_the_tests_answer_is_reported_incomplete(tmp_path):
    repo = _NoFileList(_repo_with_conventions(tmp_path).root)
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    result = _run(_pr_changing_helper(), repo, _sync(), async_client)
    assert result.metrics.conventions_file == "CLAUDE.md" and result.metrics.callers_found == 2
    assert not result.metrics.context_complete


def test_the_verifier_gets_the_absence_rule_on_this_path_only(tmp_path):
    from app.agent.verifier import ABSENCE_RULE

    repo = _repo_with_conventions(tmp_path)
    finding = _finding("blocker", "general", "app/z.py does not exist in this repository")
    sync = _sync([_use("verify_findings", verdicts=[])])
    async_client = _async(
        WARM, _submit("s", [finding]), _submit("", []), _submit("", [])
    )
    result = _run(_pr_changing_helper(), repo, sync, async_client)
    assert result.metrics.verified
    suffix = sync.messages.calls[0]["messages"][0]["content"][1]["text"]
    assert ABSENCE_RULE in suffix


def test_the_client_this_review_built_is_closed_on_its_own_loop(pr, repo, monkeypatch):
    """RC1-394: the corpus run logged "Event loop is closed" once per case —
    an AsyncAnthropic built here and left to the garbage collector. It is
    closed inside the loop that used it; an injected client is not."""
    import sys
    import types

    closed = []

    class FakeAsync:
        def __init__(self, **kw):
            self.messages = AsyncMessages([WARM, _submit("", []), _submit("", []), _submit("", [])])

        async def close(self):
            closed.append(True)

    fake_sdk = types.SimpleNamespace(AsyncAnthropic=FakeAsync, Anthropic=lambda **kw: _sync())
    monkeypatch.setitem(sys.modules, "anthropic", fake_sdk)
    plan = ReviewPlan(context=True, reviewers=(DIFF_LOCAL, REPO_CONTEXT, CHANGE_INTENT))
    pipeline.review_pull_request(pr, repo, client=_sync(), model="m", plan=plan)
    assert closed == [True]

    injected = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    injected.close = lambda: (_ for _ in ()).throw(AssertionError("closed the caller's client"))
    _run(pr, repo, _sync(), injected)


def test_the_review_runs_inside_one_workflow_span_and_is_priced_while_open(
    pr, repo, monkeypatch
):
    """One `pr_review` span per review, opened here (RC1-395; the dispatcher
    that used to open it went with the single loop in RC1-422), the stage
    spans inside it, and the cost annotation before it closes so the cost
    lands on the root."""
    from contextlib import contextmanager

    events = []

    @contextmanager
    def fake_span(kind, name):
        events.append(("open", kind, name))
        yield
        events.append(("close", kind, name))

    monkeypatch.setattr(pipeline, "stage_span", fake_span)
    monkeypatch.setattr(
        pipeline, "annotate_review_cost", lambda result: events.append(("priced",))
    )
    result = _run(
        pr, repo, _sync(), _async(WARM, _submit("a", []), _submit("b", []), _submit("c", [])),
    )
    assert events[0] == ("open", "workflow", "pr_review")
    assert events[-1] == ("close", "workflow", "pr_review")
    assert events[-2] == ("priced",)
    assert ("open", "task", "repo_context") in events
    assert result.metrics.latency_ms > 0

