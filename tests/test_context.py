"""Tests for the deterministic repository context (RC1-393). Offline: a
local checkout in ``tmp_path`` and a fake GitHub for the remote backend."""
from __future__ import annotations

from app.agent import context
from app.agent.context import (
    MAX_CALLER_ROWS,
    MAX_CALLERS_PER_SYMBOL,
    MAX_CONVENTIONS_CHARS,
    MAX_SOURCE_FILES,
    MAX_SYMBOLS,
    MAX_TEST_ROOTS,
    MAX_TEST_ROWS,
    MAX_TESTS_PER_FILE,
    RepoContext,
    build_repo_context,
    changed_source_files,
    changed_symbols,
    is_test_path,
    module_stem,
    named_test_files,
    select_sections,
)
from app.agent.github_repository import GitHubRepository
from app.agent.local_repository import LocalRepository
from app.agent.repository import RepositoryError
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
    path, text, cut = context.conventions_file(LocalRepository(tmp_path))
    assert (path, text, cut) == ("AGENTS.md", "agents", False)


def test_no_conventions_file_is_none(tmp_path):
    assert context.conventions_file(LocalRepository(tmp_path)) == (None, "", False)


# --- callers ---------------------------------------------------------------------

def _checkout(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py").write_text("def fetch(path):\n    return path\n\nfetch('x')\n")
    (tmp_path / "app" / "b.py").write_text("from app.a import fetch\n\nout = fetch('y')\n")
    (tmp_path / "app" / "c.py").write_text("fetcher = 1\nprefetch = 2\n")
    return LocalRepository(tmp_path)


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
    ctx = build_repo_context(pr, LocalRepository(tmp_path))
    assert len(ctx.symbols) == MAX_SYMBOLS
    assert ctx.symbols_unsearched == [f"fetch{MAX_SYMBOLS}", f"fetch{MAX_SYMBOLS + 1}", "fetch"]
    assert f"(not searched: fetch{MAX_SYMBOLS}, fetch{MAX_SYMBOLS + 1}, fetch)" in ctx.render()

    pr = _pr(("app/a.py", "+def fetch():"))
    ctx = build_repo_context(pr, LocalRepository(tmp_path))
    assert len(ctx.callers) == MAX_CALLERS_PER_SYMBOL


def test_total_row_cap_marks_truncation(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "MAX_CALLER_ROWS", 4)
    (tmp_path / "many.py").write_text("\n".join(f"a = fn{i % 2}({i})" for i in range(20)))
    pr = _pr(("app/a.py", "+def fn0():\n+def fn1():\n+def fn2():"))
    ctx = build_repo_context(pr, LocalRepository(tmp_path))
    assert len(ctx.callers) == 4 and ctx.callers_truncated
    assert ctx.unresolved == [] and ctx.symbols_unsearched == ["fn1", "fn2"]
    assert "(not searched: fn1, fn2)" in ctx.render()
    assert "callers list capped" in ctx.render()


def test_a_tool_error_stops_the_search_and_is_recorded(tmp_path):
    class Refusing:
        def read_text(self, path):
            return None

        def grep(self, pattern, **kw):
            raise RepositoryError("GitHub API budget exhausted")

        def paths(self):
            return None

    pr = _pr(("app/a.py", "+def fetch():\n+def other():"))
    ctx = build_repo_context(pr, Refusing())
    assert ctx.search_stopped and ctx.symbols_unsearched == ["fetch", "other"]
    assert ctx.unresolved == []
    assert "read budget ran out" in ctx.render()
    assert ctx.tests_stopped and not ctx.tests_searched and not ctx.complete


def test_empty_context_renders_nothing():
    assert RepoContext().render() == "" and RepoContext().empty
    assert MAX_CALLER_ROWS >= MAX_CALLERS_PER_SYMBOL


# --- tests for the changed paths (RC1-394) -------------------------------------------

def test_is_test_path_by_directory_or_by_name():
    for path in (
        "tests/test_a.py",
        "tests/helpers.py",
        "packages/core/tests/test_x.py",
        "apps/web/__tests__/x.test.ts",
        "spec/x_spec.rb",
        "app/a_test.py",
        "src/x.spec.js",
        "conftest.py",
    ):
        assert is_test_path(path), path
    for path in ("app/a.py", "app/testing.py", "app/contest.py", "tests", "docs/tests.md"):
        assert not is_test_path(path), path


def test_changed_source_files_skips_tests_locks_and_prose():
    pr = _pr(
        ("app/a.py", "+x"),
        ("tests/test_a.py", "+x"),
        ("uv.lock", None),
        ("README.md", "+x"),
        ("workflows/w.json", "+x"),
        ("app/a.py", "+y"),
    )
    assert changed_source_files(pr) == ["app/a.py", "workflows/w.json"]


def test_module_stem_names_a_package_for_its_directory():
    assert module_stem("app/agent/context.py") == "context"
    assert module_stem("app/agent/__init__.py") == "agent"
    assert module_stem("apps/web/src/Button.test.tsx") == "Button"
    assert module_stem("Makefile") == "Makefile"


def test_test_roots_are_the_shortest_test_directories_most_files_first():
    paths = [
        "packages/core/tests/test_a.py",
        "packages/core/tests/test_b.py",
        "packages/agents/tests/test_c.py",
        "apps/web/__tests__/x.test.ts",
        "tests/test_d.py",
        "app/a.py",
    ]
    assert context.test_roots(paths) == [
        "packages/core/tests", "apps/web/__tests__", "packages/agents/tests"
    ]
    assert len(context.test_roots(paths)) == MAX_TEST_ROOTS
    assert context.test_roots(["app/a.py", "app/a_test.py"]) == [], "named tests, no directory"


def test_named_test_files_follow_the_three_conventions():
    paths = [
        "tests/test_context.py",
        "tests/test_context_extra.py",
        "app/context_test.py",
        "apps/web/__tests__/context.test.tsx",
        "apps/web/context.spec.ts",
        "app/context.py",
    ]
    assert named_test_files("app/context.py", paths) == [
        "app/context_test.py",
        "apps/web/__tests__/context.test.tsx",
        "apps/web/context.spec.ts",
        "tests/test_context.py",
    ]


def _checkout_with_tests(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "CLAUDE.md").write_text("## Conventions\nrules\n")
    (tmp_path / "app" / "a.py").write_text("def fetch(path):\n    return path\n")
    (tmp_path / "app" / "b.py").write_text("from app.a import fetch\n\nout = fetch('y')\n")
    (tmp_path / "app" / "c.py").write_text("untested = 1\n")
    (tmp_path / "tests" / "test_a.py").write_text(
        "from app.a import fetch\n\n\ndef test_fetch():\n    assert fetch('x') == 'x'\n"
    )
    (tmp_path / "tests" / "test_other.py").write_text("import app.b\n")
    return LocalRepository(tmp_path)


def test_tests_section_names_the_test_file_greps_the_tree_and_lists_the_untested(tmp_path):
    pr = _pr(
        ("app/a.py", "-def fetch(path):\n+def fetch(path, retries):\n"),
        ("app/c.py", "+untested = 2\n"),
    )
    ctx = build_repo_context(pr, _checkout_with_tests(tmp_path))
    # The callers grep's hits inside tests/ moved to the tests section...
    assert ctx.callers == ["app/b.py:1: from app.a import fetch", "app/b.py:3: out = fetch('y')"]
    assert "tests/test_a.py:1: from app.a import fetch" in ctx.tests
    assert "tests/test_a.py:5: assert fetch('x') == 'x'" in ctx.tests
    # ...and the module search adds the named file and the rows for its stem.
    assert "tests/test_a.py: (test file named for app/a.py)" in ctx.tests
    assert ctx.test_roots == ["tests"]
    assert ctx.source_files == ["app/a.py", "app/c.py"]
    assert ctx.untested == ["app/c.py"]
    assert ctx.tests_searched and ctx.complete
    rendered = ctx.render()
    tests_block = rendered[rendered.index("Tests touching the changed paths") :]
    assert "found by grep under tests/" in tests_block
    assert "(no test references: app/c.py)" in tests_block
    assert rendered.index("Callers of what changed") < rendered.index("Tests touching")


def test_a_repository_with_no_tests_says_so_and_still_counts_as_answered(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("## Conventions\nrules\n")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py").write_text("x = 1\n")
    pr = _pr(("app/a.py", "+x = 2\n"))
    ctx = build_repo_context(pr, LocalRepository(tmp_path))
    assert ctx.symbols == [] and ctx.untested == ["app/a.py"]
    assert ctx.tests_searched and ctx.complete
    assert "(no test files found in the repository)" in ctx.render()
    assert "by file name, no test directory found" in ctx.render()


def test_a_diff_with_no_source_file_is_answered_without_a_search(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("## Conventions\nrules\n")
    pr = _pr(("tests/test_a.py", "+def test_x(): pass\n"), ("README.md", "+hi\n"))
    ctx = build_repo_context(pr, LocalRepository(tmp_path))
    assert ctx.source_files == [] and ctx.tests_searched and ctx.complete
    assert "Tests touching" not in ctx.render()


def test_complete_needs_the_conventions_file_too(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py").write_text("x = 1\n")
    ctx = build_repo_context(_pr(("app/a.py", "+x = 2\n")), LocalRepository(tmp_path))
    assert ctx.tests_searched and not ctx.complete


def test_no_file_list_stops_the_tests_search_before_it_starts(tmp_path):
    class NoTree(LocalRepository):
        def paths(self):
            return None

    pr = _pr(("app/a.py", "+def fetch():\n"))
    ctx = build_repo_context(pr, NoTree(_checkout_with_tests(tmp_path).root))
    assert ctx.tests_stopped and not ctx.tests_searched and not ctx.complete
    assert ctx.source_files == [] and ctx.source_files_unsearched == ["app/a.py"]
    assert ctx.conventions_path == "CLAUDE.md", "the rest of the context is unaffected"
    assert "the tests search stopped early" in ctx.render()
    assert "(not searched: app/a.py)" in ctx.render()


def test_a_tool_error_in_the_tests_grep_stops_it_and_keeps_what_was_found(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "CLAUDE.md").write_text("## Conventions\nrules\n")
    (tmp_path / "app" / "widget.py").write_text("x = 1\n")
    (tmp_path / "app" / "gadget.py").write_text("y = 1\n")
    (tmp_path / "tests" / "test_widget.py").write_text("from app import widget\n")
    tools = LocalRepository(tmp_path)
    calls = []
    real_grep = tools.grep

    def grep(pattern, *args, **kw):
        calls.append(pattern)
        if "gadget" in pattern:
            raise RepositoryError("GitHub API budget exhausted")
        return real_grep(pattern, *args, **kw)

    tools.grep = grep
    pr = _pr(("app/widget.py", "+x = 2\n"), ("app/gadget.py", "+y = 2\n"))
    ctx = build_repo_context(pr, tools)
    assert ctx.tests_stopped and not ctx.tests_searched and not ctx.complete
    assert ctx.source_files == ["app/widget.py"]
    assert ctx.source_files_unsearched == ["app/gadget.py"]
    assert ctx.tests == [
        "tests/test_widget.py: (test file named for app/widget.py)",
        "tests/test_widget.py:1: from app import widget",
    ]
    rendered = ctx.render()
    assert "stopped early" in rendered and "(not searched: app/gadget.py)" in rendered
    assert "changed source files: app/widget.py, app/gadget.py" in rendered


def test_tests_rows_are_capped_per_file_and_in_total(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "widget.py").write_text("x = 1\n")
    (tmp_path / "tests" / "test_many.py").write_text(
        "\n".join(f"use_{i} = widget({i})" for i in range(40)) + "\n"
    )
    pr = _pr(("app/widget.py", "+x = 2\n"))
    ctx = build_repo_context(pr, LocalRepository(tmp_path))
    assert len(ctx.tests) == MAX_TESTS_PER_FILE and not ctx.tests_truncated

    monkeypatch.setattr(context, "MAX_TEST_ROWS", 4)
    ctx = build_repo_context(pr, LocalRepository(tmp_path))
    assert len(ctx.tests) == 4 and ctx.tests_truncated and ctx.tests_searched
    assert "tests list capped" in ctx.render()


def test_source_files_are_capped():
    pr = _pr(*((f"app/m{i}.py", "+x") for i in range(MAX_SOURCE_FILES + 2)))
    assert len(changed_source_files(pr)) == MAX_SOURCE_FILES + 2

    class Empty:
        def read_text(self, path):
            return None

        def grep(self, pattern, **kw):
            return "(no matches)"

        def paths(self):
            return ["app/m0.py"]

    ctx = build_repo_context(pr, Empty())
    assert len(ctx.source_files) == MAX_SOURCE_FILES
    assert ctx.source_files_unsearched == [
        f"app/m{MAX_SOURCE_FILES}.py", f"app/m{MAX_SOURCE_FILES + 1}.py"
    ]
    assert f"(not searched: app/m{MAX_SOURCE_FILES}.py" in ctx.render()
    assert MAX_TEST_ROWS >= MAX_TESTS_PER_FILE


def test_short_and_generic_stems_are_not_grepped_but_named_files_still_count(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_io.py").write_text("io = 1\n")
    pr = _pr(("app/io.py", "+x = 1\n"))
    ctx = build_repo_context(pr, LocalRepository(tmp_path))
    assert ctx.tests == ["tests/test_io.py: (test file named for app/io.py)"]


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
    tools = GitHubRepository(
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
    assert ctx.untested == ["app/a.py"] and ctx.tests_searched and ctx.complete


def test_remote_backend_finds_tests_through_the_tree_and_the_budgeted_grep():
    files = {
        "CLAUDE.md": "## Conventions\nremote rules\n",
        "app/a.py": "def fetch():\n    pass\n",
        "tests/test_a.py": "from app.a import fetch\n",
        "tests/test_z.py": "unrelated = 1\n",
    }
    gh = FakeGitHub(files)
    tools = GitHubRepository(
        gh, PRRef("o", "r", 1), "sha", changed_files=["app/a.py"], api_budget=10
    )
    ctx = build_repo_context(_pr(("app/a.py", "+def fetch():")), tools)
    assert ctx.test_roots == ["tests"]
    assert "tests/test_a.py: (test file named for app/a.py)" in ctx.tests
    assert "tests/test_a.py:1: from app.a import fetch" in ctx.tests
    assert ctx.callers == [] and ctx.unresolved == ["fetch"], "the test hit is not a caller"
    assert ctx.tests_searched  # the cap is an answer; no conventions file here, so not complete


def test_remote_tests_search_stops_when_the_tree_is_out_of_budget():
    gh = FakeGitHub({"CLAUDE.md": "## Conventions\nx\n", "app/a.py": "x = 1\n"})
    tools = GitHubRepository(gh, PRRef("o", "r", 1), "sha", api_budget=1)
    ctx = build_repo_context(_pr(("app/a.py", "+x = 2")), tools)
    assert ctx.conventions_path == "CLAUDE.md"
    assert ctx.tests_stopped and not ctx.complete


def test_remote_read_text_is_none_once_the_budget_is_spent():
    gh = FakeGitHub({"CLAUDE.md": "x", "app/a.py": "y"})
    tools = GitHubRepository(gh, PRRef("o", "r", 1), "sha", api_budget=1)
    assert tools.read_text("app/a.py") == "y"
    assert tools.read_text("CLAUDE.md") is None, "the context is optional; the review is not"


def test_a_file_whose_test_rows_the_cap_refused_is_not_called_untested(tmp_path, monkeypatch):
    """RC1-428: RC1-427's reference PR #39 drew three false "no tests" warnings
    from files whose rows the 30-row cap had refused; the list called them
    untested. A hit the cap refuses is still a test that exists."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "app").mkdir()
    for name in ("widget", "gadget"):
        (tmp_path / "app" / f"{name}.py").write_text("x = 1\n")
        (tmp_path / "tests" / f"test_{name}.py").write_text(
            "\n".join(f"use_{i} = {name}({i})" for i in range(8)) + "\n"
        )
    pr = _pr(("app/widget.py", "+x = 2\n"), ("app/gadget.py", "+x = 3\n"))
    monkeypatch.setattr(context, "MAX_TEST_ROWS", 3)
    ctx = build_repo_context(pr, LocalRepository(tmp_path))
    assert len(ctx.tests) == 3 and ctx.tests_truncated and ctx.tests_searched
    assert ctx.untested == []
    assert ctx.tests_unlisted == ["app/gadget.py"]
    rendered = ctx.render()
    assert "(no test references" not in rendered
    assert "(tests exist but did not fit under the cap for: app/gadget.py)" in rendered
    assert ctx.tests_searched  # the cap is an answer; no conventions file here, so not complete
