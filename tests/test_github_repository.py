"""The GitHub adapter's own behavior (RC1-364, RC1-424): what reading
through the API adds to the contract in ``test_repository_contract.py`` —
the per-review call budget, the read cache, the bounded grep and the
fallback to the PR's changed files when the tree is unreadable."""
from __future__ import annotations

import pytest

from app.agent.github_repository import MAX_GREP_REMOTE_FILES, GitHubRepository
from app.agent.repository import RepositoryError
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
def repo(gh):
    return GitHubRepository(gh, REF, "headsha", changed_files=["src/util.py"], api_budget=10)


def _fetched(gh):
    return [c[1] for c in gh.calls if c[0] == "contents"]


# --- read_text: the cache and the head sha -------------------------------------

def test_read_text_reads_at_the_head_sha_and_caches(repo, gh):
    assert repo.read_text("README.md") == FILES["README.md"]
    assert repo.read_text("./README.md") == FILES["README.md"]
    assert gh.calls == [("contents", "README.md", "headsha")]
    assert repo.api_calls == 1


def test_withheld_files_cost_no_call(repo, gh):
    assert repo.read_text(".env") is None
    assert repo.read_text("uv.lock") is None
    assert repo.read_text("../secret.txt") is None
    assert gh.calls == []


def test_a_missing_file_is_cached_as_missing(repo, gh):
    assert repo.read_text("gone.py") is None
    assert repo.read_text("gone.py") is None
    assert repo.api_calls == 1


# --- paths and the tree -------------------------------------------------------------

def test_paths_come_from_the_tree_and_cost_the_one_tree_call(repo, gh):
    paths = repo.paths()
    assert sorted(paths) == [".env.example", "README.md", "src/app.py", "src/util.py"]
    assert repo.paths() == paths and repo.api_calls == 1, "the tree is cached"
    assert repo.tree_available


def test_paths_are_none_without_a_tree_and_read_text_still_works():
    gh = FakeGitHub(dict(FILES), tree=False)
    repo = GitHubRepository(gh, REF, "h", changed_files=["src/util.py"])
    assert repo.paths() is None
    assert repo.tree_available is False
    assert repo.read_text("src/util.py") == "VALUE = 42\n"


def test_paths_are_none_once_the_budget_is_spent():
    spent = GitHubRepository(FakeGitHub(dict(FILES)), REF, "s", api_budget=1)
    spent.read_text("src/app.py")
    assert spent.paths() is None


# --- grep: bounded, changed files first ----------------------------------------------

def test_grep_searches_changed_files_first_and_skips_noise(repo, gh):
    out = repo.grep("TODO")
    assert "src/app.py:2:" in out and "node_modules" not in out
    assert _fetched(gh)[0] == "src/util.py", "the PR's own files are searched first"
    assert ".env" not in _fetched(gh)


def test_grep_reports_the_candidates_it_could_not_reach_under_the_budget(gh):
    repo = GitHubRepository(gh, REF, "h", api_budget=3)  # 1 tree + 2 files
    out = repo.grep("line")
    assert "searched 2 of" in out and "API budget" in out
    assert repo.api_calls == 3


def test_grep_file_cap_is_a_constant_the_budget_sits_on_top_of():
    many = {f"f{i:03}.txt": "needle\n" for i in range(MAX_GREP_REMOTE_FILES + 5)}
    repo = GitHubRepository(FakeGitHub(many), REF, "h", api_budget=1000)
    out = repo.grep("needle")
    assert f"searched {MAX_GREP_REMOTE_FILES} of {len(many)}" in out


def test_grep_without_a_tree_confines_itself_to_the_changed_files():
    gh = FakeGitHub(dict(FILES), tree=False)
    repo = GitHubRepository(gh, REF, "h", changed_files=["src/util.py"])
    assert repo.grep("TODO") == "(no matches)"  # app.py has the TODO but is not a changed file
    assert _fetched(gh) == ["src/util.py"]


def test_grep_without_a_tree_still_withholds_a_changed_lock_or_secret_file():
    files = {**FILES, "package-lock.json": '{"resolved": "https://registry.npmjs.org/x"}\n'}
    gh = FakeGitHub(files, tree=False)
    repo = GitHubRepository(gh, REF, "h", changed_files=["package-lock.json", ".env"])
    assert repo.grep("registry") == "(no matches)"
    assert repo.grep("SHOULD-NOT-LEAK") == "(no matches)"
    assert _fetched(gh) == []


# --- the budget --------------------------------------------------------------------

def test_a_grep_that_can_read_nothing_is_a_repository_error_not_no_matches(gh):
    """RC1-424: with the tree cached and the budget spent, the search used
    to answer "(no matches)", which context gathering rendered as "no
    references found" — a false answer. Nothing searched is not searched."""
    repo = GitHubRepository(gh, REF, "h", api_budget=1)
    assert repo.paths() is not None  # the one call: the tree
    with pytest.raises(RepositoryError, match="budget exhausted"):
        repo.grep("TODO")


def test_a_grep_whose_tree_is_out_of_budget_is_a_repository_error():
    repo = GitHubRepository(FakeGitHub(dict(FILES)), REF, "h", api_budget=1)
    repo.read_text("README.md")
    with pytest.raises(RepositoryError, match="budget exhausted"):
        repo.grep("TODO")


def test_a_grep_that_read_some_files_returns_them_and_says_what_it_missed(gh):
    repo = GitHubRepository(gh, REF, "h", changed_files=["src/app.py"], api_budget=2)
    out = repo.grep("TODO")  # tree + src/app.py, then the budget is gone
    assert out.splitlines()[0] == "src/app.py:2: return 'hi'  # TODO: i18n"
    assert "searched 1 of" in out


def test_cached_reads_stay_free_after_the_budget_is_spent(gh):
    repo = GitHubRepository(gh, REF, "h", changed_files=["README.md"], api_budget=1)
    assert repo.read_text("README.md") is not None
    assert repo.read_text("src/app.py") is None
    assert repo.read_text("README.md") is not None
    assert repo.api_calls == 1
    # A grep still needs the tree, and the tree needs a call it cannot have.
    with pytest.raises(RepositoryError, match="budget exhausted"):
        repo.grep("Demo")
