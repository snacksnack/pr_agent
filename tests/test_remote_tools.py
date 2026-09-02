"""Tests for the GitHub-API-backed exploration tools (RC1-364).

The contract under test is the one the live webhook needed and did not have:
the same three tools as the local backend, served without a checkout, under a
per-review API budget, degrading to the diff's file list when the tree is
unreadable.
"""
from __future__ import annotations

import pytest

from app.agent.remote_tools import MAX_GREP_REMOTE_FILES, RemoteRepoTools
from app.agent.tools import ToolError
from app.models import PRRef

REF = PRRef("o", "r", 7)


class FakeGitHub:
    """Contents + Trees, recorded. ``tree=None`` simulates an unreadable tree."""

    def __init__(self, files: dict[str, str], *, tree: bool = True):
        self.files = files
        self.has_tree = tree
        self.calls: list[tuple] = []

    def get_file_text(self, ref, path, *, git_ref=None):
        self.calls.append(("contents", path, git_ref))
        return self.files.get(path)

    def get_tree(self, ref, sha):
        self.calls.append(("tree", sha))
        if not self.has_tree:
            return None
        entries = []
        for p, text in self.files.items():
            entries.append({"path": p, "type": "blob", "size": len(text)})
        for d in {p.rsplit("/", 1)[0] for p in self.files if "/" in p}:
            entries.append({"path": d, "type": "tree", "size": 0})
        return entries


FILES = {
    "README.md": "# Demo\nline two\nline three\n",
    "src/app.py": "def hello():\n    return 'hi'  # TODO: i18n\n\n\ndef Bye():\n    pass\n",
    "src/util.py": "VALUE = 42\n",
    "node_modules/x/index.js": "TODO noise\n",
    ".env": "ANTHROPIC_API_KEY=sk-ant-SHOULD-NOT-LEAK\n",
    ".env.example": "ANTHROPIC_API_KEY=\n",
}


@pytest.fixture()
def gh():
    return FakeGitHub(dict(FILES))


@pytest.fixture()
def tools(gh):
    return RemoteRepoTools(gh, REF, "headsha", changed_files=["src/util.py"], api_budget=10)


# --- read_file ------------------------------------------------------------

def test_read_file_numbers_lines_and_reads_at_the_head_sha(tools, gh):
    out = tools.read_file("README.md")
    assert "1  # Demo" in out and "2  line two" in out
    assert gh.calls == [("contents", "README.md", "headsha")]


def test_read_file_is_cached_so_a_reread_costs_nothing(tools, gh):
    tools.read_file("README.md")
    tools.read_file("./README.md", start_line=2, end_line=2)
    assert tools.api_calls == 1
    assert len(gh.calls) == 1


def test_read_file_missing_is_a_tool_error_not_an_exception_out_of_dispatch(tools):
    with pytest.raises(ToolError, match="no such file at the PR head"):
        tools.read_file("gone.py")
    assert tools.dispatch("read_file", {"path": "gone.py"}).startswith("Error: no such file")


def test_read_file_refuses_secrets_without_spending_a_call(tools, gh):
    with pytest.raises(ToolError, match="secrets/credentials"):
        tools.read_file(".env")
    assert gh.calls == []
    assert "ANTHROPIC_API_KEY=" in tools.read_file(".env.example")


def test_read_file_rejects_root_escape_and_the_root_itself(tools):
    with pytest.raises(ToolError, match="escapes"):
        tools.read_file("../secret.txt")
    with pytest.raises(ToolError, match="use list_dir"):
        tools.read_file(".")


# --- list_dir -------------------------------------------------------------

def test_list_dir_root_hides_noise_and_secrets_and_costs_one_tree_call(tools, gh):
    out = tools.list_dir()
    assert out.startswith("./\n")
    assert "  src/" in out and "  README.md (" in out
    assert "node_modules" not in out and "  .env (" not in out
    assert "  .env.example (" in out
    assert [c[0] for c in gh.calls] == ["tree"]
    tools.list_dir("src")
    assert tools.api_calls == 1, "the tree is fetched once per review"


def test_list_dir_subdir_and_missing(tools):
    out = tools.list_dir("src")
    assert out.splitlines()[0] == "src/"
    assert "  app.py (" in out and "  util.py (" in out
    with pytest.raises(ToolError, match="no such directory"):
        tools.list_dir("nope")


def test_list_dir_without_a_tree_says_so_and_leaves_read_file_working():
    gh = FakeGitHub(dict(FILES), tree=False)
    tools = RemoteRepoTools(gh, REF, "h", changed_files=["src/util.py"])
    with pytest.raises(ToolError, match="tree is not readable"):
        tools.list_dir()
    assert tools.tree_available is False
    assert "VALUE = 42" in tools.read_file("src/util.py")


# --- grep -----------------------------------------------------------------

def test_grep_searches_changed_files_first_and_skips_noise(tools, gh):
    out = tools.grep("TODO")
    assert "src/app.py:2:" in out
    assert "node_modules" not in out
    fetched = [c[1] for c in gh.calls if c[0] == "contents"]
    assert fetched[0] == "src/util.py", "the PR's own files are searched first"
    assert ".env" not in fetched


def test_grep_scopes_by_path_and_glob(tools, gh):
    assert "src/util.py:1: VALUE = 42" in tools.grep("VALUE", "src", glob="*.py")
    assert tools.grep("VALUE", "src", glob="*.md") == "(no matches)"
    assert tools.grep("value", ignore_case=True).startswith("src/util.py:1:")


def test_grep_reports_the_candidates_it_could_not_reach_under_the_budget(gh):
    tools = RemoteRepoTools(gh, REF, "h", api_budget=3)  # 1 tree + 2 files
    out = tools.grep("line")
    assert "searched 2 of" in out and "narrow with a path or glob" in out
    assert tools.api_calls == 3


def test_grep_without_a_tree_confines_itself_to_the_changed_files():
    gh = FakeGitHub(dict(FILES), tree=False)
    tools = RemoteRepoTools(gh, REF, "h", changed_files=["src/util.py"])
    out = tools.grep("TODO")
    assert out == "(no matches)"  # app.py has the TODO but is not a changed file
    assert [c[1] for c in gh.calls if c[0] == "contents"] == ["src/util.py"]


def test_grep_file_cap_is_a_constant_the_budget_sits_on_top_of():
    many = {f"f{i:03}.txt": "needle\n" for i in range(MAX_GREP_REMOTE_FILES + 5)}
    gh = FakeGitHub(many)
    tools = RemoteRepoTools(gh, REF, "h", api_budget=1000)
    out = tools.grep("needle")
    assert f"searched {MAX_GREP_REMOTE_FILES} of {len(many)}" in out


# --- budget + dispatch ----------------------------------------------------

def test_budget_exhaustion_tells_the_model_to_submit(gh):
    tools = RemoteRepoTools(gh, REF, "h", api_budget=1)
    tools.read_file("README.md")
    out = tools.dispatch("read_file", {"path": "src/app.py"})
    assert out.startswith("Error: GitHub API budget exhausted")
    assert "submit your review" in out
    # A cached read is still free after the budget is spent.
    assert "# Demo" in tools.read_file("README.md")


def test_dispatch_mirrors_the_local_backend(tools):
    assert tools.dispatch("list_dir", {}).startswith("./")
    assert tools.dispatch("grep", {"pattern": "["}).startswith("Error: invalid regex")
    assert tools.dispatch("nope", {}) == "Error: unknown tool: 'nope'"
    assert tools.dispatch("grep", {}) == "Error: missing required argument 'pattern'"


def test_remote_dispatch_accepts_string_line_numbers(tools):
    # The exact shape that killed the first live review under RC1-364.
    out = tools.dispatch("read_file", {"path": "README.md", "start_line": "2", "end_line": "2"})
    assert out == "2  line two"


# --- lock files (RC1-365) -------------------------------------------------

LOCK_FILES = {
    **FILES,
    "package-lock.json": '{"resolved": "https://registry.npmjs.org/left-pad"}\n',
    "uv.lock": 'source = { registry = "https://pypi.org/simple" }\n',
}


def test_remote_read_file_refuses_lock_files_without_spending_a_call():
    gh = FakeGitHub(dict(LOCK_FILES))
    tools = RemoteRepoTools(gh, REF, "h")
    out = tools.dispatch("read_file", {"path": "package-lock.json"})
    assert out.startswith("Error: refused: 'package-lock.json' is a generated lock file")
    assert gh.calls == []


def test_remote_grep_and_list_dir_skip_lock_files_even_when_they_changed():
    gh = FakeGitHub(dict(LOCK_FILES))
    tools = RemoteRepoTools(gh, REF, "h", changed_files=["package-lock.json", "src/util.py"])
    assert tools.grep("registry") == "(no matches)"
    fetched = [c[1] for c in gh.calls if c[0] == "contents"]
    assert "package-lock.json" not in fetched and "uv.lock" not in fetched
    listing = tools.list_dir()
    assert "package-lock.json" not in listing and "uv.lock" not in listing


def test_remote_grep_without_a_tree_still_skips_a_changed_lock_file():
    gh = FakeGitHub(dict(LOCK_FILES), tree=False)
    tools = RemoteRepoTools(gh, REF, "h", changed_files=["package-lock.json"])
    assert tools.grep("registry") == "(no matches)"
    assert [c for c in gh.calls if c[0] == "contents"] == []
