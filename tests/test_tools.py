"""Tests for the repo-exploration tools (RC1-109)."""
from __future__ import annotations

import pytest

from app.agent.tools import TOOL_SCHEMAS, RepoTools, ToolError, is_secret_file


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
    return RepoTools(root)


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
    with pytest.raises(ToolError):
        repo.read_file("nope.py")


def test_read_file_on_dir_errors(repo):
    with pytest.raises(ToolError):
        repo.read_file("src")


def test_read_file_binary_errors(repo):
    with pytest.raises(ToolError):
        repo.read_file("image.png")


@pytest.mark.parametrize("escape", ["../secret.txt", "/etc/hostname", "src/../../secret.txt"])
def test_read_file_blocks_traversal(repo, escape):
    with pytest.raises(ToolError):
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
    with pytest.raises(ToolError):
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
    with pytest.raises(ToolError):
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
    with pytest.raises(ToolError) as exc:
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


def test_dispatch_read_secret_returns_error_string(repo):
    out = repo.dispatch("read_file", {"path": ".env"})
    assert out.startswith("Error:") and "secrets" in out.lower()


# --- dispatch + schemas ---------------------------------------------------

def test_dispatch_returns_error_string_not_raise(repo):
    # Traversal via dispatch must be reported as a string, not raised.
    out = repo.dispatch("read_file", {"path": "../secret.txt"})
    assert out.startswith("Error:")


def test_dispatch_missing_arg(repo):
    assert repo.dispatch("grep", {}).startswith("Error:")


def test_dispatch_unknown_tool(repo):
    assert repo.dispatch("frobnicate", {}).startswith("Error:")


def test_dispatch_happy_path(repo):
    assert "src/" in repo.dispatch("list_dir", {"path": "."})


def test_tool_schemas_shape():
    names = {t["name"] for t in TOOL_SCHEMAS}
    assert names == {"read_file", "list_dir", "grep"}
    for tool in TOOL_SCHEMAS:
        assert tool["description"]
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        assert "properties" in schema
        assert "required" in schema


# --- dispatch survives what the model actually sends (RC1-364 follow-up) --

def test_dispatch_accepts_line_numbers_sent_as_strings(repo):
    out = repo.dispatch("read_file", {"path": "src/app.py", "start_line": "1", "end_line": "2"})
    assert "1  def hello():" in out and "Bye" not in out


def test_dispatch_reports_a_bad_line_number_instead_of_raising(repo):
    out = repo.dispatch("read_file", {"path": "src/app.py", "start_line": "twelve"})
    assert out == "Error: start_line must be an integer, got 'twelve'"


def test_dispatch_turns_any_unexpected_exception_into_a_tool_error(repo, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(repo, "grep", boom)
    out = repo.dispatch("grep", {"pattern": "x"})
    assert out.startswith("Error: grep failed (RuntimeError: disk on fire)")


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
    return RepoTools(root)


def test_read_file_refuses_lock_files_with_a_pointer_to_the_manifest(repo_with_lock):
    with pytest.raises(ToolError, match="generated lock file"):
        repo_with_lock.read_file("package-lock.json")
    with pytest.raises(ToolError, match="generated lock file"):
        repo_with_lock.read_file("uv.lock")
    assert "left-pad" in repo_with_lock.read_file("package.json")


def test_grep_and_list_dir_skip_lock_files(repo_with_lock):
    out = repo_with_lock.grep("registry")
    assert out == "(no matches)"
    assert "httpx" in repo_with_lock.grep("httpx")  # src/app.py, not uv.lock
    listing = repo_with_lock.list_dir()
    assert "package.json (" in listing
    assert "package-lock.json" not in listing and "uv.lock" not in listing


def test_the_read_file_schema_tells_the_model_lock_files_are_off_limits():
    read_tool = next(t for t in TOOL_SCHEMAS if t["name"] == "read_file")
    assert "lock files" in read_tool["description"]
