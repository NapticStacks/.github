"""Tests for the inventor-notebook guard (project-manager#311).

Each test builds a throwaway git repo, makes a base and a head commit, and runs
the guard over that range.
"""

import subprocess
from pathlib import Path

import pytest

import notebook_guard as ng


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, msg: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "README.md").write_text("x\n")
    commit(tmp_path, "base")
    return tmp_path


def write(repo: Path, rel: str, text: str = "x\n") -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_is_notebook_path_matches_any_component_case_insensitive():
    assert ng.is_notebook_path("JORDAN-CONFIDENTIAL/INVENTOR_NOTEBOOK_PROMPT_LOG.md")
    assert ng.is_notebook_path("sub/dir/jordan-confidential/entries/a.md")
    assert not ng.is_notebook_path("docs/JORDAN-CONFIDENTIAL.md")
    assert not ng.is_notebook_path("src/confidential/x.py")


def test_default_scope_is_jordans_folder_only():
    assert not ng.is_notebook_path("BEN-CONFIDENTIAL/log.md")
    assert ng.is_notebook_path("BEN-CONFIDENTIAL/log.md", ng.parse_folders("JORDAN-CONFIDENTIAL, BEN-CONFIDENTIAL"))


def test_adding_a_notebook_file_fails(repo: Path):
    base = git(repo, "rev-parse", "HEAD")
    write(repo, "JORDAN-CONFIDENTIAL/INVENTOR_NOTEBOOK_PROMPT_LOG.md")
    head = commit(repo, "add log")
    r = ng.check(str(repo), base, head)
    assert r.violations == ["JORDAN-CONFIDENTIAL/INVENTOR_NOTEBOOK_PROMPT_LOG.md"]


def test_modifying_a_tracked_notebook_file_fails(repo: Path):
    write(repo, "JORDAN-CONFIDENTIAL/log.md", "a\n")
    base = commit(repo, "old leak")
    write(repo, "JORDAN-CONFIDENTIAL/log.md", "b\n")
    head = commit(repo, "edit")
    assert ng.check(str(repo), base, head).violations == ["JORDAN-CONFIDENTIAL/log.md"]


def test_renaming_into_the_notebook_fails(repo: Path):
    write(repo, "notes.md", "some long enough content to be detected as a rename\n" * 5)
    base = commit(repo, "notes")
    (repo / "JORDAN-CONFIDENTIAL").mkdir()
    git(repo, "mv", "notes.md", "JORDAN-CONFIDENTIAL/notes.md")
    head = commit(repo, "move")
    assert ng.check(str(repo), base, head).violations == ["JORDAN-CONFIDENTIAL/notes.md"]


def test_untracking_the_notebook_passes(repo: Path):
    write(repo, "JORDAN-CONFIDENTIAL/log.md")
    base = commit(repo, "old leak")
    git(repo, "rm", "-q", "-r", "--cached", "JORDAN-CONFIDENTIAL")
    write(repo, ".gitignore", "JORDAN-CONFIDENTIAL/\n")
    head = commit(repo, "untrack")
    r = ng.check(str(repo), base, head)
    assert r.violations == []
    assert r.still_tracked == []


def test_unrelated_change_in_a_repo_that_still_tracks_it_warns_only(repo: Path):
    write(repo, "JORDAN-CONFIDENTIAL/log.md")
    base = commit(repo, "old leak")
    write(repo, "src/app.py")
    head = commit(repo, "feature")
    r = ng.check(str(repo), base, head)
    assert r.violations == []
    assert r.still_tracked == ["JORDAN-CONFIDENTIAL/log.md"]


def test_no_base_checks_the_tree_and_only_warns(repo: Path):
    write(repo, "JORDAN-CONFIDENTIAL/log.md")
    head = commit(repo, "leak")
    r = ng.check(str(repo), None, head)
    assert r.violations == []
    assert r.still_tracked == ["JORDAN-CONFIDENTIAL/log.md"]


def test_all_zero_base_is_treated_as_no_base(repo: Path):
    head = git(repo, "rev-parse", "HEAD")
    r = ng.check(str(repo), "0" * 40, head)
    assert r.violations == [] and r.still_tracked == []


def test_main_exit_codes(repo: Path, capsys):
    base = git(repo, "rev-parse", "HEAD")
    write(repo, "JORDAN-CONFIDENTIAL/log.md")
    head = commit(repo, "leak")
    assert ng.main(["--repo", str(repo), "--base", base, "--head", head]) == 1
    out = capsys.readouterr().out
    assert "::error file=JORDAN-CONFIDENTIAL/log.md" in out
    assert ng.main(["--repo", str(repo), "--base", head, "--head", head]) == 0
    assert "::warning" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["café.md", "tab\there.md", 'quote".md', "back\\slash.md"])
def test_quoted_paths_are_still_caught(repo: Path, name: str):
    base = git(repo, "rev-parse", "HEAD")
    write(repo, f"JORDAN-CONFIDENTIAL/{name}")
    head = commit(repo, "leak")
    assert ng.check(str(repo), base, head).violations == [f"JORDAN-CONFIDENTIAL/{name}"]


def test_add_then_delete_inside_the_range_fails(repo: Path):
    base = git(repo, "rev-parse", "HEAD")
    write(repo, "JORDAN-CONFIDENTIAL/log.md")
    commit(repo, "leak")
    git(repo, "rm", "-q", "JORDAN-CONFIDENTIAL/log.md")
    head = commit(repo, "remove")
    assert ng.check(str(repo), base, head).violations == ["JORDAN-CONFIDENTIAL/log.md"]


def test_other_folders_only_when_listed(repo: Path):
    base = git(repo, "rev-parse", "HEAD")
    write(repo, "BEN-CONFIDENTIAL/INVENTOR_NOTEBOOK_PROMPT_LOG.md")
    head = commit(repo, "ben's notebook")
    assert ng.check(str(repo), base, head).violations == []
    both = ng.parse_folders("JORDAN-CONFIDENTIAL,BEN-CONFIDENTIAL")
    assert ng.check(str(repo), base, head, both).violations == ["BEN-CONFIDENTIAL/INVENTOR_NOTEBOOK_PROMPT_LOG.md"]


def test_notebook_added_in_a_merge_commit_fails(repo: Path):
    base = git(repo, "rev-parse", "HEAD")
    default = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    git(repo, "checkout", "-q", "-b", "side")
    write(repo, "side.md")
    commit(repo, "side work")
    git(repo, "checkout", "-q", default)
    write(repo, "main.md")
    commit(repo, "main work")
    git(repo, "merge", "-q", "--no-commit", "--no-ff", "side")
    write(repo, "JORDAN-CONFIDENTIAL/log.md")
    head = commit(repo, "merge, with git add -A")
    assert ng.check(str(repo), base, head).violations == ["JORDAN-CONFIDENTIAL/log.md"]


def test_missing_base_fails_when_strict(repo: Path):
    head = git(repo, "rev-parse", "HEAD")
    assert ng.main(["--repo", str(repo), "--base", "1" * 40, "--head", head, "--strict-base"]) == 2


def test_missing_base_after_force_push_warns_and_checks_tree(repo: Path, capsys):
    head = git(repo, "rev-parse", "HEAD")
    assert ng.main(["--repo", str(repo), "--base", "1" * 40, "--head", head]) == 0
    assert "not in this clone" in capsys.readouterr().out


def test_error_lines_escape_workflow_command_characters():
    assert ng.escape_property("a:b,c%d\ne") == "a%3Ab%2Cc%25d%0Ae"


def test_merge_adds_then_later_commit_deletes_fails(repo: Path):
    base = git(repo, "rev-parse", "HEAD")
    default = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    git(repo, "checkout", "-q", "-b", "side")
    write(repo, "side.md")
    commit(repo, "side work")
    git(repo, "checkout", "-q", default)
    write(repo, "main.md")
    commit(repo, "main work")
    git(repo, "merge", "-q", "--no-commit", "--no-ff", "side")
    write(repo, "JORDAN-CONFIDENTIAL/log.md")
    commit(repo, "merge, with git add -A")
    git(repo, "rm", "-q", "JORDAN-CONFIDENTIAL/log.md")
    head = commit(repo, "remove it again")
    assert ng.check(str(repo), base, head).violations == ["JORDAN-CONFIDENTIAL/log.md"]


def test_force_push_that_drops_an_old_notebook_edit_passes(repo: Path):
    write(repo, "JORDAN-CONFIDENTIAL/log.md", "a\n")
    root = commit(repo, "old leak")
    write(repo, "JORDAN-CONFIDENTIAL/log.md", "b\n")
    before = commit(repo, "bad edit, later force-pushed away")
    git(repo, "reset", "-q", "--hard", root)
    write(repo, "src/app.py")
    after = commit(repo, "the rewritten branch")
    assert ng.check(str(repo), before, after).violations == []


def test_jordans_folder_is_always_in_scope():
    folders = ng.parse_folders("BEN-CONFIDENTIAL, /DARIEN-CONFIDENTIAL/")
    assert ng.is_notebook_path("JORDAN-CONFIDENTIAL/log.md", folders)
    assert ng.is_notebook_path("darien-confidential/log.md", folders)
    assert ng.is_notebook_path("JORDAN-CONFIDENTIAL/log.md", ng.parse_folders(""))
