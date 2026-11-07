"""Tests for tsrc-worktree: add/list/remove/hold cycle, the
unregistered detection, and the ledger lifecycle.
"""

from pathlib import Path

import pytest
from conftest import make_workspace
from tsrc_ext import worktree
from tsrc_ext.common import run_cmd

from tsrc.git import run_git as tsrc_run_git
from tsrc.test.helpers.cli import CLI
from tsrc.test.helpers.git_server import GitServer


def test_add_list_remove_cycle(
    git_server: GitServer,
    tsrc_cli: CLI,
    workspace_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    make_workspace(git_server, tsrc_cli, ["foo", "bar"])
    assert worktree.main(["add", "foo", "task-a"]) == 0
    assert worktree.main(["add", "foo", "task-a"]) == 1  # duplicate
    assert worktree.main(["add", "bar", "task-b"]) == 0
    out = capsys.readouterr().out
    assert worktree.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "task-a" in out and "task-b" in out
    assert "alive" in out

    wt_a = workspace_path / ".tsrc" / "worktrees" / "task-a"
    assert (wt_a / "README.txt").exists()
    assert worktree.main(["remove", "task-a"]) == 0
    assert not wt_a.exists()
    capsys.readouterr()  # drain the "removed ..." line
    assert worktree.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "task-a" not in out
    assert "task-b" in out


def test_remove_refuses_dirty_worktree(
    git_server: GitServer,
    tsrc_cli: CLI,
    workspace_path: Path,
) -> None:
    make_workspace(git_server, tsrc_cli, ["foo"])
    assert worktree.main(["add", "foo", "task-a"]) == 0
    wt = workspace_path / ".tsrc" / "worktrees" / "task-a"
    (wt / "README.txt").write_text("DIRTY\n")
    assert worktree.main(["remove", "task-a"]) == 1
    assert wt.exists()  # still there
    assert worktree.main(["remove", "--force", "task-a"]) == 0
    assert not wt.exists()


def test_remove_refuses_unpushed_work(
    git_server: GitServer,
    tsrc_cli: CLI,
    workspace_path: Path,
) -> None:
    make_workspace(git_server, tsrc_cli, ["foo"])
    assert worktree.main(["add", "foo", "task-a"]) == 0
    wt = workspace_path / ".tsrc" / "worktrees" / "task-a"
    (wt / "new.txt").write_text("new work\n")
    tsrc_run_git(wt, "add", "new.txt", show_output=False, show_cmd=False)
    tsrc_run_git(wt, "commit", "-m", "new work", show_output=False, show_cmd=False)
    assert worktree.main(["remove", "task-a"]) == 1
    # Pushing the branch clears the refusal.
    tsrc_run_git(
        wt,
        "push",
        "origin",
        "HEAD:refs/heads/task-a",
        show_output=False,
        show_cmd=False,
    )
    assert worktree.main(["remove", "task-a"]) == 0


def test_hold_blocks_remove(
    git_server: GitServer,
    tsrc_cli: CLI,
    workspace_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    make_workspace(git_server, tsrc_cli, ["foo"])
    assert worktree.main(["add", "foo", "task-a"]) == 0
    assert worktree.main(["hold", "task-a"]) == 0
    assert worktree.main(["list"]) == 0
    assert "[held]" in capsys.readouterr().out
    assert worktree.main(["remove", "task-a"]) == 1
    assert worktree.main(["unhold", "task-a"]) == 0
    assert worktree.main(["remove", "task-a"]) == 0


def test_list_reports_unregistered_worktree(
    git_server: GitServer,
    tsrc_cli: CLI,
    workspace_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    make_workspace(git_server, tsrc_cli, ["foo"])
    manual = workspace_path / "manual-wt"
    tsrc_run_git(
        workspace_path / "foo",
        "worktree",
        "add",
        str(manual),
        "-b",
        "manual",
        show_output=False,
        show_cmd=False,
    )
    assert worktree.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "unregistered: %s (foo)" % manual in out


def test_prune_drops_missing_entries(
    git_server: GitServer,
    tsrc_cli: CLI,
    workspace_path: Path,
) -> None:
    make_workspace(git_server, tsrc_cli, ["foo"])
    assert worktree.main(["add", "foo", "task-a"]) == 0
    wt = workspace_path / ".tsrc" / "worktrees" / "task-a"
    # Simulate the prunable accident shape: directory deleted
    # outside the ledger.
    run_cmd(
        ["git", "worktree", "remove", "--force", str(wt)], cwd=workspace_path / "foo"
    )
    assert not wt.exists()
    assert worktree.main(["prune"]) == 0
    ledger = worktree.load_ledger(workspace_path)
    assert ledger["worktrees"] == []
