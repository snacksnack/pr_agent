"""Tests for the deterministic repository context (RC1-393). Offline: a
local checkout in ``tmp_path`` and a fake GitHub for the remote backend."""
from __future__ import annotations

from app.agent import context
from app.agent.context import (
    MAX_CALLER_ROWS,
    MAX_CALLERS_PER_SYMBOL,
    MAX_CONVENTIONS_CHARS,
    MAX_SYMBOLS,
    RepoContext,
    build_repo_context,
    changed_symbols,
    select_sections,
)
from app.agent.remote_tools import RemoteRepoTools
from app.agent.tools import RepoTools, ToolError
from app.models import ChangedFile, PRRef, PullRequest


def _pr(*files):
    return PullRequest(
        ref=PRRef("o", "r", 1),
        files=[ChangedFile(name, "modified", patch=patch) for name, patch in files],
    )


# --- symbols -----------------------------------------------------------------

def test_changed_symbols_reads_defs_classes_and_constants_from_changed_lines():
    pr = _pr(
        (
            "app/a.py",
            "@@ -1,4 +1,5 @@\n"
            " import os\n"
            "-def fetch(self, path, *, timeout=30):\n"
            "+def fetch(self, path, timeout, verify_tls):\n"
            "+class Client:\n"
            "+    async def connect(self):\n"
            "+MAX_RETRIES = 3\n"
            "+    lowercase_local = 1\n"
            " def untouched():\n",
        ),
    )
    assert changed_symbols(pr) == ["fetch", "Client", "connect", "MAX_RETRIES"]


def test_changed_symbols_skips_dunders_tests_and_lock_files():
    pr = _pr(
        ("app/a.py", "+def __init__(self):\n+def okay():\n+def ok():\n+def test_helper():\n"),
        ("tests/test_a.py", "+def helper_in_tests():\n"),
        ("uv.lock", "+def nope():\n"),
        ("app/js.js", "+export async function render() {\n+class Widget {\n"),
    )
    assert changed_symbols(pr) == ["okay", "render", "Widget"], "two-letter names are skipped too"


def test_changed_symbols_ignores_diff_headers_and_files_without_a_patch():
    pr = _pr(("app/a.py", "--- a/app/a.py\n+++ b/app/a.py\n"), ("bin.png", None))
    assert changed_symbols(pr) == []


# --- conventions file ------------------------------------------------------------

def test_short_file_is_kept_whole():
    body, cut = select_sections("# T\n\n## Build\nstuff\n\n## Conventions\nrules\n")
    assert body.startswith("# T") and "rules" in body and cut is False


def test_long_file_keeps_convention_sections_first_and_says_it_cut():
    filler = "x" * (MAX_CONVENTIONS_CHARS // 2)
    text = (
        f"# Title\n\n## Build plan\n{filler}\n\n## Layout\nlayout text\n\n"
        f"## Conventions (match these)\nrule one\n\n## Testing\ntest rule\n\n"
        f"## History\n{filler}\n"
    )
    body, cut = select_sections(text)
    assert cut is True
    assert body.index("## Conventions") < body.index("## Testing") < body.index("## Layout")
    assert body.index("## Layout") < body.index("# Title") < body.index("## Build plan")
    assert body.endswith("... [section cut]")
    assert len(body) <= MAX_CONVENTIONS_CHARS + 40


def test_headingless_file_is_cut_at_the_cap():
    body, cut = select_sections("word " * MAX_CONVENTIONS_CHARS)
    assert cut is True and body.endswith("... [section cut]")


def test_first_conventions_file_found_wins(tmp_path):
    (tmp_path / "CONTRIBUTING.md").write_text("contrib\n")
    (tmp_path / "AGENTS.md").write_text("agents\n")
    path, text, cut = context.conventions_file(RepoTools(tmp_path))
    assert (path, text, cut) == ("AGENTS.md", "agents", False)


def test_no_conventions_file_is_none(tmp_path):
    assert context.conventions_file(RepoTools(tmp_path)) == (None, "", False)


# --- callers ---------------------------------------------------------------------

def _checkout(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py").write_text("def fetch(path):\n    return path\n\nfetch('x')\n")
    (tmp_path / "app" / "b.py").write_text("from app.a import fetch\n\nout = fetch('y')\n")
    (tmp_path / "app" / "c.py").write_text("fetcher = 1\nprefetch = 2\n")
    return RepoTools(tmp_path)


def test_callers_exclude_the_definition_and_match_whole_words(tmp_path):
    pr = _pr(("app/a.py", "-def fetch(path):\n+def fetch(path, retries):\n"))
    ctx = build_repo_context(pr, _checkout(tmp_path))
    assert ctx.symbols == ["fetch"]
    assert ctx.callers == [
        "app/a.py:4: fetch('x')",
        "app/b.py:1: from app.a import fetch",
        "app/b.py:3: out = fetch('y')",
    ]
    assert ctx.unresolved == [] and ctx.conventions_path is None
    rendered = ctx.render()
    assert rendered.startswith("Callers of what changed")
    assert "Repository conventions" not in rendered


def test_symbols_with_no_callers_are_listed_as_unresolved(tmp_path):
    pr = _pr(("app/a.py", "+def fetch():\n+def brand_new():\n"))
    ctx = build_repo_context(pr, _checkout(tmp_path))
    assert ctx.unresolved == ["brand_new"]
    assert "(no references found for: brand_new)" in ctx.render()


def test_only_unresolved_symbols_says_so_without_a_list(tmp_path):
    pr = _pr(("app/a.py", "+def brand_new():\n"))
    ctx = build_repo_context(pr, _checkout(tmp_path))
    assert ctx.callers == []
    assert "(no references found outside the definitions)" in ctx.render()
    assert ctx.empty is False, "the symbols were searched; the reviewers should know"


def test_callers_are_capped_per_symbol_and_in_total(tmp_path):
    (tmp_path / "many.py").write_text("\n".join(f"use_{i} = fetch({i})" for i in range(50)))
    defs = "\n".join(f"+def fetch{i}():" for i in range(MAX_SYMBOLS + 2))
    pr = _pr(("app/a.py", defs + "\n+def fetch():"))
    ctx = build_repo_context(pr, RepoTools(tmp_path))
    assert len(ctx.symbols) == MAX_SYMBOLS
    assert ctx.symbols_unsearched == [f"fetch{MAX_SYMBOLS}", f"fetch{MAX_SYMBOLS + 1}", "fetch"]
    assert f"(not searched: fetch{MAX_SYMBOLS}, fetch{MAX_SYMBOLS + 1}, fetch)" in ctx.render()

    pr = _pr(("app/a.py", "+def fetch():"))
    ctx = build_repo_context(pr, RepoTools(tmp_path))
    assert len(ctx.callers) == MAX_CALLERS_PER_SYMBOL


def test_total_row_cap_marks_truncation(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "MAX_CALLER_ROWS", 4)
    (tmp_path / "many.py").write_text("\n".join(f"a = fn{i % 2}({i})" for i in range(20)))
    pr = _pr(("app/a.py", "+def fn0():\n+def fn1():\n+def fn2():"))
    ctx = build_repo_context(pr, RepoTools(tmp_path))
    assert len(ctx.callers) == 4 and ctx.callers_truncated
    assert ctx.unresolved == [] and ctx.symbols_unsearched == ["fn1", "fn2"]
    assert "(not searched: fn1, fn2)" in ctx.render()
    assert "callers list capped" in ctx.render()


def test_a_tool_error_stops_the_search_and_is_recorded(tmp_path):
    class Refusing:
        def read_text(self, path):
            return None

        def grep(self, pattern, **kw):
            raise ToolError("GitHub API budget exhausted")

    pr = _pr(("app/a.py", "+def fetch():\n+def other():"))
    ctx = build_repo_context(pr, Refusing())
    assert ctx.search_stopped and ctx.symbols_unsearched == ["fetch", "other"]
    assert ctx.unresolved == []
    assert "read budget ran out" in ctx.render()


def test_empty_context_renders_nothing():
    assert RepoContext().render() == "" and RepoContext().empty
    assert MAX_CALLER_ROWS >= MAX_CALLERS_PER_SYMBOL


# --- the remote backend ------------------------------------------------------------

class FakeGitHub:
    def __init__(self, files):
        self.files = files
        self.calls = []

    def get_file_text(self, ref, path, *, git_ref=None):
        self.calls.append(path)
        return self.files.get(path)

    def get_tree(self, ref, sha):
        return [{"path": p, "type": "blob", "size": len(t)} for p, t in self.files.items()]


def test_remote_backend_serves_the_same_context_under_the_api_budget():
    files = {
        "CLAUDE.md": "## Conventions\nremote rules\n",
        "app/a.py": "def fetch():\n    pass\n",
        "app/b.py": "fetch()\n",
        ".env": "SECRET=1\n",
    }
    gh = FakeGitHub(files)
    tools = RemoteRepoTools(
        gh, PRRef("o", "r", 1), "sha", changed_files=["app/a.py"], api_budget=10
    )
    pr = _pr(("app/a.py", "+def fetch():"))
    ctx = build_repo_context(pr, tools)
    assert ctx.conventions_path == "CLAUDE.md" and "remote rules" in ctx.conventions
    assert ctx.callers == ["app/b.py:1: fetch()"]
    assert ".env" not in gh.calls
    assert tools.read_text(".env") is None
    assert tools.read_text("../CLAUDE.md") is None
    assert tools.read_text("uv.lock") is None


def test_remote_read_text_is_none_once_the_budget_is_spent():
    gh = FakeGitHub({"CLAUDE.md": "x", "app/a.py": "y"})
    tools = RemoteRepoTools(gh, PRRef("o", "r", 1), "sha", api_budget=1)
    assert tools.read_text("app/a.py") == "y"
    assert tools.read_text("CLAUDE.md") is None, "the context is optional; the review is not"
