"""The local adapter's own behavior (RC1-109, RC1-424): what a checkout on
disk adds to the contract in ``test_repository_contract.py`` — path
resolution through symlinks, an empty root, walk order."""
from __future__ import annotations

import pytest

from app.agent.local_repository import LocalRepository
from app.agent.repository import RepositoryError


def test_a_root_that_is_not_a_directory_is_refused(tmp_path):
    (tmp_path / "file").write_text("x\n")
    with pytest.raises(RepositoryError, match="not a directory"):
        LocalRepository(tmp_path / "file")
    with pytest.raises(RepositoryError, match="not a directory"):
        LocalRepository(tmp_path / "missing")


def test_explorable_is_false_for_an_empty_root(tmp_path):
    """RC1-390: the dry-run CLI hands the pipeline an empty directory when
    it has no checkout; the router skips the repository context on it."""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert LocalRepository(empty).explorable is False
    (empty / ".git").mkdir()
    assert LocalRepository(empty).explorable is False  # noise dirs do not count
    (empty / "a.py").write_text("x = 1\n")
    assert LocalRepository(empty).explorable is True


def test_a_symlink_out_of_the_root_is_not_followed(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("API_KEY=should-not-be-readable\n")
    (root / "link.txt").symlink_to(tmp_path / "secret.txt")
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("x = 1\n")
    repo = LocalRepository(root)
    assert repo.read_text("link.txt") is None
    with pytest.raises(RepositoryError, match="escapes"):
        repo.grep("API_KEY", "link.txt")
    assert repo.read_text("src/a.py") == "x = 1\n"


def test_grep_on_a_missing_path_is_a_repository_error(tmp_path):
    with pytest.raises(RepositoryError, match="no such path"):
        LocalRepository(tmp_path).grep("x", "nope")


def test_grep_on_one_file_searches_just_that_file(tmp_path):
    (tmp_path / "a.py").write_text("needle\n")
    (tmp_path / "b.py").write_text("needle\n")
    assert LocalRepository(tmp_path).grep("needle", "b.py") == "b.py:1: needle"


def test_grep_skips_binary_files(tmp_path):
    (tmp_path / "bin").write_bytes(b"\xff\xfe\x00needle")
    (tmp_path / "a.py").write_text("needle\n")
    assert LocalRepository(tmp_path).grep("needle") == "a.py:1: needle"


def test_paths_are_posix_and_leave_out_what_grep_leaves_out(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py").write_text("x\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "a.pyc").write_bytes(b"\x00")
    (tmp_path / ".env").write_text("SECRET=1\n")
    (tmp_path / "uv.lock").write_text("lock\n")
    assert LocalRepository(tmp_path).paths() == ["app/a.py"]
