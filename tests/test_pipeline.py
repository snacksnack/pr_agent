"""Tests for the pipeline-agent review path (RC1-390). Offline: scripted fakes
for the sync client (scout, verifier) and the async client (warm call,
reviewers)."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from app.agent import pipeline
from app.agent.prompts import CHANGE_INTENT, DIFF_LOCAL, REPO_CONTEXT
from app.agent.router import ReviewPlan
from app.agent.tools import RepoTools
from app.config import Settings
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
    return RepoTools(root)


@pytest.fixture()
def pr():
    return PullRequest(
        ref=PRRef("o", "r", 9),
        title="Change x",
        body="changes x",
        files=[ChangedFile("app/x.py", "modified", patch="@@ -1 +1 @@\n-x = 0\n+x = 1")],
    )


SCOUT = [[_use("submit_brief", brief="Config is read through settings everywhere.")]]
WARM = []


def _run(pr, repo, sync, async_client, **kw):
    kw.setdefault("model", "m")
    return pipeline.review_pull_request(pr, repo, client=sync, async_client=async_client, **kw)


# --- the happy path -----------------------------------------------------------

def test_scout_then_three_reviewers_then_merge(pr, repo):
    sync = _sync(*SCOUT)
    async_client = _async(
        WARM,
        _submit("A secret is committed.", [_finding("blocker", "leaked_secret", "key in code")]),
        _submit("No tests cover x.", [_finding("warning", "tests", "untested", line=9)]),
        _submit("", []),
    )
    result = _run(pr, repo, sync, async_client)

    assert result.mode == "multi"
    assert result.reviewers_run == ["diff_local", "repo_context", "change_intent"]
    assert result.brief == "Config is read through settings everywhere."
    assert [(f.severity, f.category) for f in result.findings] == [
        ("blocker", "leaked_secret"),
        ("warning", "tests"),
    ]
    assert result.summary == "A secret is committed. No tests cover x."
    assert result.tool_turns == 1 and result.files_read == 0  # the scout's
    assert result.verified is False
    assert set(result.stage_usage) == {
        "scout", "warm_cache", "reviewer:diff_local", "reviewer:repo_context",
        "reviewer:change_intent",
    }


def test_every_call_that_shares_the_prefix_sends_it_identically(pr, repo):
    """The design's premise: warm call, reviewers and verifier send the same
    tools, system, tool_choice and first content block, so the API serves
    one cache entry to all of them."""
    sync = _sync(
        *SCOUT,
        [_use("verify_findings", verdicts=[])],
    )
    async_client = _async(
        WARM,
        _submit("s", [_finding("warning", "security", "injection")]),
        _submit("", []),
        _submit("", []),
    )
    _run(pr, repo, sync, async_client, verify=True)

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
    # The prefix carries the PR and the brief; the suffix is per call.
    prefix_text = first["messages"][0]["content"][0]["text"]
    assert "Pull request: o/r#9" in prefix_text and "Scout's brief:" in prefix_text
    assert len(first["messages"][0]["content"]) == 1 and first["max_tokens"] == 1  # warm call
    suffixes = [c["messages"][0]["content"][1]["text"] for c in calls[1:]]
    assert "'diff_local' reviewer" in suffixes[0]
    assert "'repo_context' reviewer" in suffixes[1]
    assert "'change_intent' reviewer" in suffixes[2]
    assert "verifying a first-pass review" in suffixes[3]


def test_reviewer_suffix_carries_only_its_rubric_slice(pr, repo):
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    _run(pr, repo, _sync(*SCOUT), async_client)
    diff_local = async_client.messages.calls[1]["messages"][0]["content"][1]["text"]
    assert "3. Security & secrets" in diff_local and "2. Pythonic-ness" in diff_local
    assert "1. Convention consistency" not in diff_local
    assert "leaked_secret, security, pythonic, error_handling, docs" in diff_local
    intent = async_client.messages.calls[3]["messages"][0]["content"][1]["text"]
    assert "8. PR-description" in intent and "5. Dependency" not in intent


def test_token_usage_is_summed_across_every_stage(pr, repo):
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    result = _run(pr, repo, _sync(*SCOUT), async_client)
    # scout (10,5,100,0) + warm (0,1,900,0) + 3 reviewers (3,4,0,900)
    assert result.usage == TokenUsage(19, 18, 1000, 2700)
    assert result.stage_usage["warm_cache"].cache_creation_input_tokens == 900
    assert all(
        result.stage_usage[f"reviewer:{n}"].cache_read_input_tokens == 900
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
    result = _run(pr, repo, _sync(*SCOUT), async_client)
    assert result.unusable_reviewer_calls == 2
    assert [f.category for f in result.findings] == ["pr_drift"]
    assert result.summary == "fine"


def test_malformed_and_coerced_findings_are_counted_across_reviewers(pr, repo):
    async_client = _async(
        WARM,
        _submit("", [{"severity": "breaking_change", "category": "security", "message": "m"}]),
        _submit("", [{"category": "tests"}]),  # no severity or message
        _submit("", []),
    )
    result = _run(pr, repo, _sync(*SCOUT), async_client)
    assert result.coerced_findings == 1 and result.malformed_findings == 1
    assert result.findings[0].severity == "warning"


# --- routing and the scout --------------------------------------------------------

def test_documentation_only_change_skips_the_scout(repo):
    pr = PullRequest(
        ref=PRRef("o", "r", 2),
        title="Docs",
        files=[ChangedFile("README.md", "modified", patch="+hello")],
    )
    sync = _sync()  # no scripted scout call: it must not be made
    async_client = _async(WARM, _submit("", []), _submit("", []))
    result = _run(pr, repo, sync, async_client)
    assert result.reviewers_run == ["diff_local", "change_intent"]
    assert result.brief.startswith("(scout skipped: documentation-only")
    assert sync.messages.calls == []
    assert result.stage_usage["scout"].context_tokens == 0


def test_an_explicit_plan_is_honoured(pr, repo):
    plan = ReviewPlan(scout=False, reviewers=(DIFF_LOCAL,), reasons=("test",))
    async_client = _async(WARM, _submit("only me", []))
    result = _run(pr, repo, _sync(), async_client, plan=plan)
    assert result.reviewers_run == ["diff_local"] and result.summary == "only me"


def test_scout_turn_cap_comes_from_settings(pr, repo, monkeypatch):
    monkeypatch.setattr(pipeline, "settings", Settings(_env_file=None, review_scout_max_turns=1))
    sync = _sync(
        [_use("grep", pattern="x")],  # turn 1, the cap
        [_use("submit_brief", brief="forced")],
    )
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    result = _run(pr, repo, sync, async_client)
    assert result.brief == "forced" and result.truncated is True


def test_precomputed_findings_reach_the_shared_prefix_and_the_scout(pr, repo):
    sync = _sync(*SCOUT)
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    _run(pr, repo, sync, async_client, precomputed_findings=[Finding("warning", "n8n", "hot cron")])
    assert "hot cron" in sync.messages.calls[0]["messages"][0]["content"][0]["text"]
    assert "hot cron" in async_client.messages.calls[0]["messages"][0]["content"][0]["text"]


# --- the verifier ------------------------------------------------------------------

def test_verifier_runs_on_the_merged_findings_and_reads_the_shared_prefix(pr, repo):
    sync = _sync(
        *SCOUT,
        [_use("verify_findings", verdicts=[{"index": 0, "decision": "drop", "reason": "fixture"}])],
    )
    async_client = _async(
        WARM,
        _submit("", [_finding("blocker", "leaked_secret", "test key")]),
        _submit("", [_finding("warning", "tests", "untested", line=9)]),
        _submit("", []),
    )
    result = _run(pr, repo, sync, async_client, verify=True)

    assert result.verified is True
    assert [f.category for f in result.findings] == ["tests"]
    assert [f.category for f in result.verifier_dropped] == ["leaked_secret"]
    # RC1-390 bookkeeping survives the verifier's copy, and its stage is added.
    assert result.mode == "multi" and result.reviewers_run[0] == "diff_local"
    assert "verifier" in result.stage_usage
    assert result.usage.cache_creation_input_tokens == 1100  # scout 100 + warm 900 + verifier 100


def test_verifier_is_skipped_when_there_is_nothing_to_verify(pr, repo):
    sync = _sync(*SCOUT)
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    result = _run(pr, repo, sync, async_client, verify=True)
    assert result.verified is False and len(sync.messages.calls) == 1


def test_empty_checkout_skips_the_scout_and_the_reviewers_still_run(pr, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    sync = _sync()
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    result = _run(pr, RepoTools(empty), sync, async_client)
    assert sync.messages.calls == []
    assert result.brief.startswith("(scout skipped: no repository checkout")
    assert result.reviewers_run == ["diff_local", "repo_context", "change_intent"]
    assert set(result.stage_latency_ms) == {"context", "scout", "fan_out"}


def test_stage_latency_is_recorded_for_the_verifier_too(pr, repo):
    sync = _sync(*SCOUT, [_use("verify_findings", verdicts=[])])
    async_client = _async(
        WARM, _submit("", [_finding("nit", "docs", "d")]), _submit("", []), _submit("", [])
    )
    result = _run(pr, repo, sync, async_client, verify=True)
    assert set(result.stage_latency_ms) == {"context", "scout", "fan_out", "verifier"}
    assert all(v >= 0 for v in result.stage_latency_ms.values())


# --- RC1-393: deterministic repository context --------------------------------

def _repo_with_conventions(tmp_path):
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "CLAUDE.md").write_text("# Notes\n\n## Conventions\n\n- read config via settings\n")
    (root / "app" / "x.py").write_text("def helper():\n    return 1\n")
    (root / "app" / "y.py").write_text("from app.x import helper\n\nvalue = helper()\n")
    return RepoTools(root)


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


def test_context_reaches_the_shared_prefix_and_the_scout_seed(tmp_path):
    """With the scout kept on a complete context (the measurement arm,
    `scout_complete_turns` above zero) the seed carries the context and the
    note; the prefix carries the context and not the note."""
    sync = _sync(*SCOUT)
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    result = _run(
        _pr_changing_helper(),
        _repo_with_conventions(tmp_path),
        sync,
        async_client,
        scout_complete_turns=3,
    )

    seed = sync.messages.calls[0]["messages"][0]["content"][0]["text"]
    assert "Repository conventions, from CLAUDE.md" in seed
    assert "read config via settings" in seed
    assert "app/y.py:1: from app.x import helper" in seed
    assert "Some of that work is already done" in seed, "the scout is told not to redo it"
    assert seed.index("Repository conventions") < seed.index("You are the scout")

    prefix = async_client.messages.calls[0]["messages"][0]["content"][0]["text"]
    assert "Repository conventions, from CLAUDE.md" in prefix
    assert "app/y.py:3: value = helper()" in prefix
    assert prefix.index("Callers of what changed") < prefix.index("Scout's brief:")
    assert "Some of that work is already done" not in prefix, "the scout note is the scout's"

    assert result.conventions_file == "CLAUDE.md"
    assert result.callers_found == 2
    assert result.stage_latency_ms["context"] >= 0
    assert "tests touching the changed paths" in seed, "the scout is told tests are done too"
    assert "Tests touching the changed paths" in prefix
    assert result.context_complete and result.scout_ran


def test_context_can_be_switched_off_for_measurement(tmp_path):
    sync = _sync(*SCOUT)
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    result = _run(
        _pr_changing_helper(),
        _repo_with_conventions(tmp_path),
        sync,
        async_client,
        repo_context=False,
    )
    seed = sync.messages.calls[0]["messages"][0]["content"][0]["text"]
    prefix = async_client.messages.calls[0]["messages"][0]["content"][0]["text"]
    assert "Repository conventions" not in seed and "Repository conventions" not in prefix
    assert "Some of that work is already done" not in seed
    assert result.conventions_file is None and result.callers_found == 0


def test_no_context_leaves_the_rc1_390_prefix_plus_the_tests_line(pr, repo):
    """No conventions file and no symbols in the diff: the conventions and
    callers blocks are absent, so the prefix is what RC1-390 measured plus
    the one tests block RC1-394 always renders for a source file — here,
    that the repository has no tests. Without a conventions file the
    context is not complete, so the scout still runs, with the full cap."""
    sync = _sync(*SCOUT)
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    result = _run(pr, repo, sync, async_client)
    prefix = async_client.messages.calls[0]["messages"][0]["content"][0]["text"]
    tests_block = (
        "Tests touching the changed paths (found by grep by file name, no test "
        "directory found; changed source files: app/x.py):\n"
        "(no test files found in the repository)"
    )
    assert prefix == pipeline.build_shared_prefix(
        pr, None, SCOUT[0][0]["input"]["brief"], tests_block
    )
    assert "Repository conventions" not in prefix and "Callers of what changed" not in prefix
    seed = sync.messages.calls[0]["messages"][0]["content"][0]["text"]
    assert "Some of that work is already done" in seed
    assert result.scout_ran and not result.context_complete


def test_context_is_not_gathered_when_the_scout_is_skipped(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    result = _run(_pr_changing_helper(), RepoTools(empty), _sync(), async_client)
    assert result.conventions_file is None and result.callers_found == 0


def test_context_survives_the_verifier(tmp_path):
    sync = _sync(*SCOUT, [_use("verify_findings", verdicts=[])])
    async_client = _async(
        WARM, _submit("", [_finding("nit", "docs", "d")]), _submit("", []), _submit("", [])
    )
    result = _run(
        _pr_changing_helper(), _repo_with_conventions(tmp_path), sync, async_client, verify=True
    )
    assert result.verified
    assert result.conventions_file == "CLAUDE.md" and result.callers_found == 2


class _NoFileList(RepoTools):
    """A checkout whose file list cannot be read (the live path with the
    tree call out of budget): conventions and callers are answered, the
    tests search is not, so the context is answered but not complete and
    the scout keeps the RC1-393 short cap."""

    def paths(self):
        return None


def test_scout_turn_cap_shrinks_with_the_context_and_not_without(tmp_path, monkeypatch):
    """With the conventions file and callers in hand but the tests search
    cut off, the scout gets the short cap: here 1 turn, so a scout that
    keeps exploring is forced to submit on the next call. Without the
    context it keeps the full cap."""
    repo = _NoFileList(_repo_with_conventions(tmp_path).root)
    explore = [_use("read_file", path="app/x.py")]
    forced = [_use("submit_brief", brief="forced")]
    sync = _sync(explore, forced)
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    result = _run(
        _pr_changing_helper(), repo, sync, async_client, scout_max_turns=5, scout_context_turns=1
    )
    assert result.tool_turns == 1 and result.truncated and result.brief == "forced"
    assert sync.messages.calls[1]["tool_choice"] == {"type": "tool", "name": "submit_brief"}
    assert not result.context_complete

    sync = _sync(explore, explore, forced)
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    result = _run(
        _pr_changing_helper(),
        repo,
        sync,
        async_client,
        scout_max_turns=2,
        scout_context_turns=1,
        repo_context=False,
    )
    assert result.tool_turns == 2, "no context: the full cap"


def test_scout_context_turns_come_from_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pipeline, "settings", Settings(_env_file=None, review_scout_context_turns=1)
    )
    sync = _sync([_use("read_file", path="app/x.py")], [_use("submit_brief", brief="b")])
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    repo = _NoFileList(_repo_with_conventions(tmp_path).root)
    result = _run(_pr_changing_helper(), repo, sync, async_client)
    assert result.tool_turns == 1 and result.truncated


def test_scout_complete_turns_come_from_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pipeline, "settings", Settings(_env_file=None, review_scout_complete_turns=1)
    )
    sync = _sync([_use("read_file", path="app/x.py")], [_use("submit_brief", brief="b")])
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    result = _run(_pr_changing_helper(), _repo_with_conventions(tmp_path), sync, async_client)
    assert result.tool_turns == 1 and result.truncated and result.context_complete


def test_a_complete_context_skips_the_scout_by_default(tmp_path):
    """RC1-394: conventions, callers and tests answered by Python — no scout
    call at all, the prefix carries all three, the brief says why."""
    repo = _repo_with_conventions(tmp_path)
    sync = _sync()
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    result = _run(_pr_changing_helper(), repo, sync, async_client)
    assert sync.messages.calls == [], "no scout call"
    assert result.brief.startswith("(scout skipped: the conventions file, the callers")
    assert result.conventions_file == "CLAUDE.md" and result.callers_found == 2
    assert result.context_complete and not result.scout_ran and result.tool_turns == 0
    prefix = async_client.messages.calls[0]["messages"][0]["content"][0]["text"]
    assert "Repository conventions, from CLAUDE.md" in prefix
    assert "Callers of what changed" in prefix
    assert "Tests touching the changed paths" in prefix
    assert prefix.index("Tests touching") < prefix.index("Scout's brief:")
    # Every reviewer's suffix carries the missing-evidence guard.
    for call in async_client.messages.calls[1:]:
        assert "raise nothing about the missing evidence itself" in (
            call["messages"][0]["content"][1]["text"]
        )


def test_the_verifier_gets_the_absence_rule_on_this_path_only(tmp_path):
    from app.agent.verifier import ABSENCE_RULE

    repo = _repo_with_conventions(tmp_path)
    finding = _finding("blocker", "general", "app/z.py does not exist in this repository")
    sync = _sync([_use("verify_findings", verdicts=[])])
    async_client = _async(
        WARM, _submit("s", [finding]), _submit("", []), _submit("", [])
    )
    result = _run(_pr_changing_helper(), repo, sync, async_client, verify=True)
    assert result.verified
    suffix = sync.messages.calls[0]["messages"][0]["content"][1]["text"]
    assert ABSENCE_RULE in suffix


def test_a_zero_context_cap_skips_the_scout_when_the_context_is_complete(tmp_path):
    repo = _repo_with_conventions(tmp_path)
    sync = _sync()
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    result = _run(_pr_changing_helper(), repo, sync, async_client, scout_context_turns=0)
    assert sync.messages.calls == [], "no scout call"
    assert result.brief.startswith("(scout skipped: the conventions file")
    assert result.conventions_file == "CLAUDE.md" and result.callers_found == 2
    prefix = async_client.messages.calls[0]["messages"][0]["content"][0]["text"]
    assert "Repository conventions, from CLAUDE.md" in prefix

    # Without the context the scout has the whole job and runs.
    sync = _sync(*SCOUT)
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    result = _run(
        _pr_changing_helper(), repo, sync, async_client, scout_context_turns=0, repo_context=False
    )
    assert len(sync.messages.calls) == 1 and result.brief == SCOUT[0][0]["input"]["brief"]


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
    plan = ReviewPlan(scout=True, reviewers=(DIFF_LOCAL, REPO_CONTEXT, CHANGE_INTENT))
    pipeline.review_pull_request(pr, repo, client=_sync(*SCOUT), model="m", plan=plan)
    assert closed == [True]

    injected = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    injected.close = lambda: (_ for _ in ()).throw(AssertionError("closed the caller's client"))
    _run(pr, repo, _sync(*SCOUT), injected)


# --- what the cost metric needs from this path (RC1-395) -------------------------

def test_scout_ran_is_recorded_when_the_scout_made_calls(pr, repo):
    result = _run(
        pr, repo, _sync(*SCOUT), _async(WARM, _submit("a", []), _submit("b", []), _submit("c", [])),
        verify=False,
    )
    assert result.scout_ran is True


def test_scout_ran_is_false_when_the_plan_skipped_it(repo):
    docs = PullRequest(
        ref=PRRef("o", "r", 9), title="Docs", body="",
        files=[ChangedFile("README.md", "modified", patch="@@ -1 +1 @@\n-a\n+b")],
    )
    result = _run(
        docs, repo, _sync(), _async(WARM, _submit("a", []), _submit("c", [])), verify=False
    )
    assert result.scout_ran is False and result.mode == "multi"


def test_scout_ran_survives_the_verifier(pr, repo):
    finding = _finding("warning", "security", "x")
    sync = _sync(*SCOUT, [_use("verify_findings", verdicts=[])])
    result = _run(
        pr, repo, sync,
        _async(WARM, _submit("a", [finding]), _submit("b", []), _submit("c", [])),
        verify=True,
    )
    assert result.verified and result.scout_ran is True
    assert result.verifier_model == "m"


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
        pr, repo, _sync(*SCOUT), _async(WARM, _submit("a", []), _submit("b", []), _submit("c", [])),
        verify=False,
    )
    assert events[0] == ("open", "workflow", "pr_review")
    assert events[-1] == ("close", "workflow", "pr_review")
    assert events[-2] == ("priced",)
    assert ("open", "agent", "scout") in events
    assert result.latency_ms > 0


def test_the_verifier_defaults_to_the_settings_flag(pr, repo, monkeypatch):
    """RC1-387's flag is read here now that this is the only entry point."""
    monkeypatch.setattr(
        pipeline, "settings", Settings(_env_file=None, review_verify_findings=True)
    )
    one = [_finding("warning", "security", "real")]
    sync = _sync(*SCOUT, [_use("verify_findings", verdicts=[])])
    async_client = _async(WARM, _submit("", one), _submit("", []), _submit("", []))
    result = _run(pr, repo, sync, async_client)
    assert result.verified is True and len(sync.messages.calls) == 2

    monkeypatch.setattr(pipeline, "settings", Settings(_env_file=None))
    sync = _sync(*SCOUT)
    async_client = _async(WARM, _submit("", one), _submit("", []), _submit("", []))
    result = _run(pr, repo, sync, async_client)
    assert result.verified is False and len(sync.messages.calls) == 1
