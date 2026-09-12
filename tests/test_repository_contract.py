"""The RepositoryAccess contract, run against both adapters (RC1-424).

One file layout, one set of expectations, two repositories: a checkout in
``tmp_path`` and a fake GitHub serving the same files through the Trees and
Contents APIs. Anything the pipeline relies on from a repository is asserted
here once, so the local CLI/eval path and the live path cannot drift apart.
Adapter-specific behavior — the API budget, the tree fallback, symlinks —
lives in the adapter's own test module.
"""
from __future__ import annotations

import pytest

from app.agent.context import build_repo_context
from app.agent.github_repository import GitHubRepository
from app.agent.local_repository import LocalRepository
from app.agent.repository import (
    MAX_GREP_FILE_BYTES,
    MAX_READ_BYTES,
    RepositoryAccess,
    RepositoryError,
    is_secret_file,
)
from app.models import ChangedFile, PRRef, PullRequest

FILES = {
    "CLAUDE.md": "## Conventions\nread config via settings\n",
    "README.md": "# Demo\nline two\nline three\n",
    "src/app.py": "def hello():\n    return 'hi'  # TODO: i18n\n\n\ndef Bye():\n    pass\n",
    "src/util.py": "from src.app import hello\n\nVALUE = hello()\n",
    "tests/test_app.py": "from src.app import hello\n",
    "package.json": '{"dependencies": {"left-pad": "1.0.0"}}\n',
    "package-lock.json": '{"resolved": "https://registry.npmjs.org/left-pad"}\n',
    "node_modules/x/index.js": "TODO noise\n",
    ".git/config": "TODO secret in git\n",
    ".env": "ANTHROPIC_API_KEY=sk-ant-SHOULD-NOT-LEAK\n",
    ".env.example": "ANTHROPIC_API_KEY=\n",
    "big.txt": "needle\n" * (MAX_GREP_FILE_BYTES // 7 + 1),
    "long.txt": "x" * (MAX_READ_BYTES + 10),
}
SERVED = sorted(
    [".env.example", "CLAUDE.md", "README.md", "big.txt", "image.png", "long.txt",
     "package.json", "src/app.py", "src/util.py", "tests/test_app.py"]
)
CHANGED = ["src/app.py"]


class FakeGitHub:
    """Contents + Trees over ``files``. The tree lists directories too, as
    GitHub's does, and ``.git`` never appears in a real tree."""

    def __init__(self, files):
        self.files = files

    def get_file_text(self, ref, path, *, git_ref=None):
        return self.files.get(path)

    def get_tree(self, ref, sha):
        entries = [
            {"path": p, "type": "blob", "size": len(t or "")}
            for p, t in self.files.items()
            if not p.startswith(".git/")
        ]
        for d in {p.rsplit("/", 1)[0] for p in self.files if "/" in p}:
            entries.append({"path": d, "type": "tree", "size": 0})
        return entries


def _local(tmp_path):
    root = tmp_path / "repo"
    for path, text in FILES.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    (root / "image.png").write_bytes(b"\x89PNG\x00\x01\x02\xff\xfe")
    return LocalRepository(root)


def _github(tmp_path):
    files = dict(FILES)
    files["image.png"] = None  # the Contents API returns nothing for binary
    return GitHubRepository(
        FakeGitHub(files), PRRef("o", "r", 7), "headsha", changed_files=CHANGED, api_budget=100
    )


@pytest.fixture(params=[_local, _github], ids=["local", "github"])
def repository(request, tmp_path) -> RepositoryAccess:
    return request.param(tmp_path)


# --- the shape --------------------------------------------------------------------

def test_the_adapter_is_a_repository_access(repository):
    assert isinstance(repository, RepositoryAccess)
    assert repository.explorable is True


# --- read_text --------------------------------------------------------------------

def test_read_text_is_the_raw_file(repository):
    assert repository.read_text("README.md") == FILES["README.md"]
    assert repository.read_text("./src/app.py") == FILES["src/app.py"]


def test_read_text_clips_at_the_read_cap(repository):
    text = repository.read_text("long.txt")
    assert text is not None and len(text) == MAX_READ_BYTES


@pytest.mark.parametrize(
    "path",
    [
        "missing.py", "src", ".", "image.png", ".env", "package-lock.json",
        "../outside", "src/../../x",
    ],
    ids=["missing", "directory", "root", "binary", "secret", "lockfile", "escape", "escape-deep"],
)
def test_read_text_is_none_when_there_is_nothing_to_read(repository, path):
    assert repository.read_text(path) is None


def test_read_text_serves_a_template_lookalike(repository):
    assert repository.read_text(".env.example") == FILES[".env.example"]


# --- grep -------------------------------------------------------------------------

def test_grep_rows_read_the_same_from_either_adapter(repository):
    out = repository.grep("TODO")
    assert out.splitlines() == ["src/app.py:2: return 'hi'  # TODO: i18n"]


def test_grep_never_surfaces_withheld_or_noise_files(repository):
    assert repository.grep("SHOULD-NOT-LEAK") == "(no matches)"
    assert repository.grep("registry.npmjs") == "(no matches)"
    assert repository.grep("secret in git") == "(no matches)"
    assert repository.grep("TODO noise") == "(no matches)"


def test_grep_skips_files_over_the_size_cap(repository):
    assert repository.grep("needle") == "(no matches)"


def test_grep_is_scoped_to_a_path(repository):
    assert repository.grep(r"\bhello\b", "tests").splitlines() == [
        "tests/test_app.py:1: from src.app import hello"
    ]
    assert repository.grep("VALUE", "tests") == "(no matches)"


def test_grep_stops_at_max_results_and_says_so(repository):
    out = repository.grep("line", "README.md", max_results=1)
    assert out.splitlines() == ["README.md:2: line two", "... [stopped at 1 matches]"]


def test_grep_with_a_bad_pattern_is_a_repository_error(repository):
    with pytest.raises(RepositoryError, match="invalid regex"):
        repository.grep("(unclosed")


def test_grep_outside_the_root_is_a_repository_error(repository):
    with pytest.raises(RepositoryError, match="escapes"):
        repository.grep("x", "../outside")


# --- paths ------------------------------------------------------------------------

def test_paths_lists_what_the_repository_serves(repository):
    assert sorted(repository.paths()) == SERVED


# --- the whole context, from either adapter -----------------------------------------

def test_context_gathering_reads_the_same_repository_through_either_adapter(repository):
    pr = PullRequest(
        ref=PRRef("o", "r", 7),
        title="t",
        body="",
        files=[ChangedFile("src/app.py", "modified", patch="@@ -1 +1 @@\n+def hello():\n")],
    )
    ctx = build_repo_context(pr, repository)
    assert ctx.conventions_path == "CLAUDE.md" and "read config" in ctx.conventions
    assert ctx.callers == [
        "src/util.py:1: from src.app import hello",
        "src/util.py:3: VALUE = hello()",
    ]
    assert ctx.tests == [
        "tests/test_app.py:1: from src.app import hello",
        "tests/test_app.py: (test file named for src/app.py)",
    ]
    assert ctx.complete


# --- the guards -------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,secret",
    [
        (".env", True),
        (".env.local", True),
        (".env.production", True),
        ("id_rsa", True),
        ("server.pem", True),
        ("tls.key", True),
        ("credentials", True),
        (".npmrc", True),
        (".env.example", False),  # template lookalike stays readable
        (".env.sample", False),
        ("id_rsa.pub", False),  # public key is fine
        ("app.py", False),
        ("README.md", False),
    ],
)
def test_is_secret_file_classification(name, secret):
    assert is_secret_file(name) is secret
