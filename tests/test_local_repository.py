"""Tests for the repo-exploration tools (RC1-109)."""
from __future__ import annotations

import pytest

from app.agent.local_repository import LocalRepository, RepositoryError, is_secret_file


@pytest.fixture()
def repo(tmp_path):
    """A small repo checkout plus a secret file *outside* the root."""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "README.md").write_text("# Demo\nline two\nline three\n")
    (root / "src" / "app.py").write_text(
        "def hello():\n    return 'hi'  # TODO: i18n\n\n\ndef Bye():\n    pass\n"
    )
    (root / "src" / "util.py").write_text("VALUE = 42\n")
    (root / ".git" / "config").write_text("TODO secret in git\n")
    (root / "image.png").write_bytes(b"\x89PNG\x00\x01\x02\xff\xfe")
    # A local, gitignored secret file plus a safe template lookalike.
    (root / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-SHOULD-NOT-LEAK\n")
    (root / ".env.example").write_text("ANTHROPIC_API_KEY=\n")
    (tmp_path / "secret.txt").write_text("API_KEY=should-not-be-readable\n")
    return LocalRepository(root)


# --- read_file ------------------------------------------------------------

def test_read_file_numbers_lines(repo):
    out = repo.read_file("README.md")
    assert "1  # Demo" in out
    assert "2  line two" in out


def test_read_file_line_range(repo):
    out = repo.read_file("src/app.py", start_line=1, end_line=2)
    assert "1  def hello():" in out
    assert "Bye" not in out  # line 5 excluded


def test_read_file_missing(repo):
    with pytest.raises(RepositoryError):
        repo.read_file("nope.py")


def test_read_file_on_dir_errors(repo):
    with pytest.raises(RepositoryError):
        repo.read_file("src")


def test_read_file_binary_errors(repo):
    with pytest.raises(RepositoryError):
        repo.read_file("image.png")


@pytest.mark.parametrize("escape", ["../secret.txt", "/etc/hostname", "src/../../secret.txt"])
def test_read_file_blocks_traversal(repo, escape):
    with pytest.raises(RepositoryError):
        repo.read_file(escape)


# --- list_dir -------------------------------------------------------------

def test_list_dir_root_excludes_git(repo):
    out = repo.list_dir(".")
    assert "src/" in out
    assert "README.md" in out
    assert ".git" not in out  # noise dir excluded


def test_list_dir_subdir(repo):
    out = repo.list_dir("src")
    assert "app.py" in out and "util.py" in out


def test_list_dir_on_file_errors(repo):
    with pytest.raises(RepositoryError):
        repo.list_dir("README.md")


# --- grep -----------------------------------------------------------------

def test_grep_finds_with_location(repo):
    out = repo.grep("TODO")
    assert "src/app.py:2:" in out
    # .git is excluded, so the secret-in-git line must not appear
    assert ".git" not in out


def test_grep_glob_filter(repo):
    out = repo.grep("TODO", glob="*.md")
    assert out == "(no matches)"  # TODO only lives in a .py file


def test_grep_ignore_case(repo):
    assert "src/app.py" in repo.grep("bye", ignore_case=True)
    assert repo.grep("bye") == "(no matches)"  # case-sensitive by default


def test_grep_fixed_string(repo):
    # A regex-special pattern only matches as a literal when fixed=True.
    assert "src/util.py" in repo.grep("VALUE = 42", fixed=True)


def test_grep_invalid_regex_errors(repo):
    with pytest.raises(RepositoryError):
        repo.grep("(unclosed")


# --- secret-file guard (RC1-114 hardening) --------------------------------

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
        (".env.example", False),   # template lookalike stays readable
        (".env.sample", False),
        ("id_rsa.pub", False),     # public key is fine
        ("app.py", False),
        ("README.md", False),
    ],
)
def test_is_secret_file_classification(name, secret):
    assert is_secret_file(name) is secret


def test_read_file_refuses_secret(repo):
    with pytest.raises(RepositoryError) as exc:
        repo.read_file(".env")
    assert "secrets" in str(exc.value).lower()


def test_read_file_allows_env_example(repo):
    # The safe template must remain readable for convention context.
    assert "ANTHROPIC_API_KEY" in repo.read_file(".env.example")


def test_grep_never_surfaces_secret_contents(repo):
    # The secret value lives only in .env; grep must not return it.
    assert repo.grep("SHOULD-NOT-LEAK") == "(no matches)"
    # Sanity: grep still works on normal files.
    assert "src/app.py:2:" in repo.grep("TODO")


def test_grep_directly_on_secret_file_yields_nothing(repo):
    assert repo.grep("SHOULD-NOT-LEAK", ".env") == "(no matches)"


def test_list_dir_hides_secret_but_shows_template(repo):
    out = repo.list_dir(".")
    assert ".env.example" in out
    assert "  .env (" not in out  # the real .env entry (name + size) is hidden


# --- lock files are noise, not context (RC1-365) --------------------------

@pytest.fixture()
def repo_with_lock(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "package.json").write_text('{"dependencies": {"left-pad": "1.0.0"}}\n')
    (root / "package-lock.json").write_text(
        '{"packages": {"node_modules/left-pad": {"resolved": "https://registry.npmjs.org/x"}}}\n'
    )
    (root / "uv.lock").write_text(
        '[[package]]\nname = "httpx"\nsource = { registry = "https://pypi.org" }\n'
    )
    (root / "src" / "app.py").write_text("import httpx\n")
    return LocalRepository(root)


def test_read_file_refuses_lock_files_with_a_pointer_to_the_manifest(repo_with_lock):
    with pytest.raises(RepositoryError, match="generated lock file"):
        repo_with_lock.read_file("package-lock.json")
    with pytest.raises(RepositoryError, match="generated lock file"):
        repo_with_lock.read_file("uv.lock")
    assert "left-pad" in repo_with_lock.read_file("package.json")


def test_grep_and_list_dir_skip_lock_files(repo_with_lock):
    out = repo_with_lock.grep("registry")
    assert out == "(no matches)"
    assert "httpx" in repo_with_lock.grep("httpx")  # src/app.py, not uv.lock
    listing = repo_with_lock.list_dir()
    assert "package.json (" in listing
    assert "package-lock.json" not in listing and "uv.lock" not in listing


def test_explorable_is_false_for_an_empty_root(tmp_path):
    """RC1-390: the dry-run CLI hands the pipeline an empty directory when
    it has no checkout; the router skips the repository context on it."""
    from app.agent.local_repository import LocalRepository

    empty = tmp_path / "empty"
    empty.mkdir()
    assert LocalRepository(empty).explorable is False
    (empty / ".git").mkdir()
    assert LocalRepository(empty).explorable is False  # noise dirs do not count
    (empty / "a.py").write_text("x = 1\n")
    assert LocalRepository(empty).explorable is True


# --- read_text (RC1-393) ------------------------------------------------------------

def test_read_text_is_raw_and_never_raises(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# hi\nrules\n")
    (tmp_path / ".env").write_text("SECRET=1\n")
    (tmp_path / "uv.lock").write_text("lock\n")
    (tmp_path / "bin").write_bytes(b"\xff\xfe\x00")
    (tmp_path / "sub").mkdir()
    tools = LocalRepository(tmp_path)
    assert tools.read_text("CLAUDE.md") == "# hi\nrules\n"
    assert tools.read_text(".env") is None
    assert tools.read_text("uv.lock") is None
    assert tools.read_text("bin") is None
    assert tools.read_text("sub") is None
    assert tools.read_text("missing.md") is None
    assert tools.read_text("../outside") is None


# --- paths (RC1-394) -----------------------------------------------------------------

def test_paths_lists_every_file_the_tools_would_serve(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py").write_text("x\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("x\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "a.pyc").write_bytes(b"\x00")
    (tmp_path / ".env").write_text("SECRET=1\n")
    (tmp_path / "uv.lock").write_text("lock\n")
    assert sorted(LocalRepository(tmp_path).paths()) == ["app/a.py", "tests/test_a.py"]
