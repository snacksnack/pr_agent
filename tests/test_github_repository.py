"""Tests for the GitHub-API-backed exploration tools (RC1-364).

The contract under test is the one the live webhook needed and did not have:
the same three tools as the local backend, served without a checkout, under a
per-review API budget, degrading to the diff's file list when the tree is
unreadable.
"""
from __future__ import annotations

import pytest

from app.agent.github_repository import MAX_GREP_REMOTE_FILES, GitHubRepository
from app.agent.local_repository import RepositoryError
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
    return GitHubRepository(gh, REF, "headsha", changed_files=["src/util.py"], api_budget=10)


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


def test_read_file_missing_is_a_tool_error(tools):
    with pytest.raises(RepositoryError, match="no such file at the PR head"):
        tools.read_file("gone.py")


def test_read_file_refuses_secrets_without_spending_a_call(tools, gh):
    with pytest.raises(RepositoryError, match="secrets/credentials"):
        tools.read_file(".env")
    assert gh.calls == []
    assert "ANTHROPIC_API_KEY=" in tools.read_file(".env.example")


def test_read_file_rejects_root_escape_and_the_root_itself(tools):
    with pytest.raises(RepositoryError, match="escapes"):
        tools.read_file("../secret.txt")
    with pytest.raises(RepositoryError, match="use list_dir"):
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
    with pytest.raises(RepositoryError, match="no such directory"):
        tools.list_dir("nope")


def test_list_dir_without_a_tree_says_so_and_leaves_read_file_working():
    gh = FakeGitHub(dict(FILES), tree=False)
    tools = GitHubRepository(gh, REF, "h", changed_files=["src/util.py"])
    with pytest.raises(RepositoryError, match="tree is not readable"):
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
    tools = GitHubRepository(gh, REF, "h", api_budget=3)  # 1 tree + 2 files
    out = tools.grep("line")
    assert "searched 2 of" in out and "narrow with a path or glob" in out
    assert tools.api_calls == 3


def test_grep_without_a_tree_confines_itself_to_the_changed_files():
    gh = FakeGitHub(dict(FILES), tree=False)
    tools = GitHubRepository(gh, REF, "h", changed_files=["src/util.py"])
    out = tools.grep("TODO")
    assert out == "(no matches)"  # app.py has the TODO but is not a changed file
    assert [c[1] for c in gh.calls if c[0] == "contents"] == ["src/util.py"]


def test_grep_file_cap_is_a_constant_the_budget_sits_on_top_of():
    many = {f"f{i:03}.txt": "needle\n" for i in range(MAX_GREP_REMOTE_FILES + 5)}
    gh = FakeGitHub(many)
    tools = GitHubRepository(gh, REF, "h", api_budget=1000)
    out = tools.grep("needle")
    assert f"searched {MAX_GREP_REMOTE_FILES} of {len(many)}" in out


# --- budget ---------------------------------------------------------------

def test_budget_exhaustion_is_a_tool_error_and_cached_reads_stay_free(gh):
    tools = GitHubRepository(gh, REF, "h", api_budget=1)
    tools.read_file("README.md")
    with pytest.raises(RepositoryError, match="GitHub API budget exhausted"):
        tools.read_file("src/app.py")
    # A cached read is still free after the budget is spent.
    assert "# Demo" in tools.read_file("README.md")


def test_string_line_numbers_are_the_callers_problem_now(tools):
    # RC1-427: the model-tool dispatcher that coerced "2" to 2 went with the
    # scout; the method takes integers.
    assert tools.read_file("README.md", 2, 2) == "2  line two"


# --- lock files (RC1-365) -------------------------------------------------

LOCK_FILES = {
    **FILES,
    "package-lock.json": '{"resolved": "https://registry.npmjs.org/left-pad"}\n',
    "uv.lock": 'source = { registry = "https://pypi.org/simple" }\n',
}


def test_remote_read_file_refuses_lock_files_without_spending_a_call():
    gh = FakeGitHub(dict(LOCK_FILES))
    tools = GitHubRepository(gh, REF, "h")
    with pytest.raises(RepositoryError, match="generated lock file"):
        tools.read_file("package-lock.json")
    assert gh.calls == []


def test_remote_grep_and_list_dir_skip_lock_files_even_when_they_changed():
    gh = FakeGitHub(dict(LOCK_FILES))
    tools = GitHubRepository(gh, REF, "h", changed_files=["package-lock.json", "src/util.py"])
    assert tools.grep("registry") == "(no matches)"
    fetched = [c[1] for c in gh.calls if c[0] == "contents"]
    assert "package-lock.json" not in fetched and "uv.lock" not in fetched
    listing = tools.list_dir()
    assert "package-lock.json" not in listing and "uv.lock" not in listing


def test_remote_grep_without_a_tree_still_skips_a_changed_lock_file():
    gh = FakeGitHub(dict(LOCK_FILES), tree=False)
    tools = GitHubRepository(gh, REF, "h", changed_files=["package-lock.json"])
    assert tools.grep("registry") == "(no matches)"
    assert [c for c in gh.calls if c[0] == "contents"] == []


# --- paths (RC1-394) -----------------------------------------------------------------

def test_remote_paths_come_from_the_tree_and_cost_the_one_tree_call(tools, gh):
    paths = tools.paths()
    assert sorted(paths) == [".env.example", "README.md", "src/app.py", "src/util.py"]
    assert tools.paths() == paths and tools.api_calls == 1, "the tree is cached"
    assert ".env" not in paths and "node_modules/x/index.js" not in paths


def test_remote_paths_are_none_without_a_tree_or_without_budget():
    assert GitHubRepository(FakeGitHub({}, tree=False), REF, "s", api_budget=5).paths() is None
    spent = GitHubRepository(FakeGitHub(dict(FILES)), REF, "s", api_budget=1)
    spent.read_file("src/app.py")
    assert spent.paths() is None
