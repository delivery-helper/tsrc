"""Tests for tsrc-worktree: add/list/remove cycle, the
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


def test_hold_and_unhold_are_not_commands(
    git_server: GitServer,
    tsrc_cli: CLI,
    workspace_path: Path,
) -> None:
    make_workspace(git_server, tsrc_cli, ["foo"])
    assert worktree.main(["add", "foo", "task-a"]) == 0
    # autohold was dropped (2026-11-07): there is no hold state
    # to set or clear, so the commands no longer exist and
    # argparse rejects them.
    with pytest.raises(SystemExit):
        worktree.main(["hold", "task-a"])
    with pytest.raises(SystemExit):
        worktree.main(["unhold", "task-a"])


def test_legacy_held_flag_is_dropped_on_load(
    git_server: GitServer,
    tsrc_cli: CLI,
    workspace_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    make_workspace(git_server, tsrc_cli, ["foo"])
    assert worktree.main(["add", "foo", "task-a"]) == 0
    # Simulate a ledger written while holds still existed.
    data = worktree.load_ledger(workspace_path)
    entry = worktree.find_entry(data, "task-a")
    assert entry is not None
    entry["held"] = True
    worktree.save_ledger(workspace_path, data)
    # The flag carries no meaning anymore: no [held] mark in the
    # list output, and list's re-save drops the key from the file.
    assert worktree.main(["list"]) == 0
    assert "[held]" not in capsys.readouterr().out
    ledger = worktree.load_ledger(workspace_path)
    saved = worktree.find_entry(ledger, "task-a")
    assert saved is not None and "held" not in saved
    # And the entry is removable without --force, under the usual
    # guards (it is clean and has no unpushed work).
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
