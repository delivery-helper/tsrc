"""Tests for `tsrc-worktree start` (design section 6.3) and for
`--provision` overlay worktrees (design sections 6.2, 8).
"""

import json
import os
import shutil
from pathlib import Path

import pytest
from conftest import make_workspace
from tsrc_ext import worktree
from tsrc_ext.common import run_cmd

from tsrc.git import run_git as tsrc_run_git
from tsrc.test.helpers.cli import CLI
from tsrc.test.helpers.git_server import GitServer


def test_start_cuts_bundle_and_is_idempotent(
    git_server: GitServer,
    tsrc_cli: CLI,
    workspace_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    make_workspace(
        git_server,
        tsrc_cli,
        ["foo", "bar"],
        groups={"g": ["foo", "bar"]},
    )
    assert worktree.main(["start", "feat-x", "-g", "g"]) == 0
    out = capsys.readouterr().out
    assert "foo: created" in out
    assert "bar: created" in out
    for dest in ("foo", "bar"):
        clone = workspace_path / dest
        rc, branches, _ = run_cmd(["git", "branch", "--list", "feat-x"], cwd=clone)
        assert "feat-x" in branches
        wt = workspace_path / ".tsrc" / "worktrees" / ("feat-x--" + dest)
        assert (wt / "README.txt").exists()
    ledger = worktree.load_ledger(workspace_path)
    assert ledger["bundles"][0]["id"] == "feat-x"
    assert sorted(ledger["bundles"][0]["repos"]) == ["bar", "foo"]
    assert len(ledger["worktrees"]) == 2
    assert all(e["bundle"] == "feat-x" for e in ledger["worktrees"])

    # Re-running the same start fills in nothing and exits 0.
    assert worktree.main(["start", "feat-x", "-g", "g"]) == 0
    out = capsys.readouterr().out
    assert "foo: already" in out
    assert "bar: already" in out
    ledger = worktree.load_ledger(workspace_path)
    assert len(ledger["worktrees"]) == 2


def test_start_partial_failure_and_rerun(
    git_server: GitServer,
    tsrc_cli: CLI,
    workspace_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    make_workspace(
        git_server,
        tsrc_cli,
        ["foo", "bar"],
        groups={"g": ["foo", "bar"]},
    )
    # Block bar's worktree path with a plain file: bar's start
    # must fail while foo's succeeds.
    root = workspace_path / ".tsrc" / "worktrees"
    root.mkdir(parents=True)
    blocker = root / "feat-x--bar"
    blocker.write_text("in the way\n")
    assert worktree.main(["start", "feat-x", "-g", "g"]) == 1
    out = capsys.readouterr().out
    assert "foo: created" in out
    assert "bar: failed" in out
    ledger = worktree.load_ledger(workspace_path)
    assert [e["repo"] for e in ledger["worktrees"]] == ["foo"]

    blocker.unlink()
    assert worktree.main(["start", "feat-x", "-g", "g"]) == 0
    out = capsys.readouterr().out
    assert "foo: already" in out
    assert "bar: created" in out
    ledger = worktree.load_ledger(workspace_path)
    assert sorted(e["repo"] for e in ledger["worktrees"]) == ["bar", "foo"]


def test_start_takes_in_existing_branch(
    git_server: GitServer,
    tsrc_cli: CLI,
    workspace_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    make_workspace(git_server, tsrc_cli, ["foo"])
    clone = workspace_path / "foo"
    # Existing branch feat-y at master, then advance master so the
    # branch base differs from the bundle base.
    tsrc_run_git(clone, "branch", "feat-y", show_output=False, show_cmd=False)
    (clone / "on-master.txt").write_text("master moved\n")
    tsrc_run_git(clone, "add", "on-master.txt", show_output=False, show_cmd=False)
    tsrc_run_git(
        clone,
        "commit",
        "-m",
        "advance master",
        show_output=False,
        show_cmd=False,
    )
    assert worktree.main(["start", "feat-y", "foo"]) == 0
    out = capsys.readouterr().out
    assert "foo: taken-in" in out
    assert "base differs" in out
    ledger = worktree.load_ledger(workspace_path)
    entry = ledger["worktrees"][0]
    assert entry["branch"] == "feat-y"
    assert entry["bundle"] == "feat-y"


def test_start_requires_target(
    git_server: GitServer,
    tsrc_cli: CLI,
    workspace_path: Path,
) -> None:
    make_workspace(git_server, tsrc_cli, ["foo"])
    assert worktree.main(["start", "feat-x"]) == 1


def _sudo_mount_available() -> bool:
    rc, _, _ = run_cmd(["sudo", "-n", "true"])
    return rc == 0


@pytest.mark.skipif(
    not _sudo_mount_available(),
    reason="overlay provision needs passwordless sudo mount",
)
def test_provision_overlay_roundtrip(
    git_server: GitServer,
    tsrc_cli: CLI,
    workspace_path: Path,
) -> None:
    """Overlay provision: base stays intact, edits land in upper."""
    make_workspace(git_server, tsrc_cli, ["foo"])
    clone = workspace_path / "foo"
    base = workspace_path / ".tsrc" / "prebuild" / "foo"
    shutil.copytree(clone, base, ignore=shutil.ignore_patterns(".git"))
    _, head, _ = run_cmd(["git", "rev-parse", "HEAD"], cwd=clone)
    (workspace_path / ".tsrc" / "prebuild" / "foo.json").write_text(
        json.dumps({"commit": head.strip(), "repo": "foo"})
    )
    assert worktree.main(["add", "foo", "ovl", "--provision"]) == 0
    merged = workspace_path / ".tsrc" / "worktrees" / "ovl"
    assert os.path.ismount(str(merged))
    assert (merged / "README.txt").read_text() == "hello\n"
    rc, out, _ = run_cmd(["git", "status", "--porcelain"], cwd=merged)
    assert rc == 0 and out.strip() == ""
    rc, out, _ = run_cmd(["git", "worktree", "list", "--porcelain"], cwd=clone)
    assert str(merged) in out
    ledger = worktree.load_ledger(workspace_path)
    entry = ledger["worktrees"][0]
    assert entry["provision"] == "overlay"
    assert entry["base_commit"] == head.strip()

    # An edit lands in upper only; the base is untouched.
    (merged / "README.txt").write_text("edited\n")
    assert (base / "README.txt").read_text() == "hello\n"
    upper = workspace_path / ".tsrc" / "overlay" / "ovl" / "upper"
    assert (upper / "README.txt").read_text() == "edited\n"

    assert worktree.main(["remove", "--force", "ovl"]) == 0
    assert not os.path.ismount(str(merged))
    assert not merged.exists()
    assert not (workspace_path / ".tsrc" / "overlay" / "ovl").exists()
    assert (base / "README.txt").read_text() == "hello\n"


def test_provision_without_base_fails_clearly(
    git_server: GitServer,
    tsrc_cli: CLI,
    workspace_path: Path,
) -> None:
    make_workspace(git_server, tsrc_cli, ["foo"])
    assert worktree.main(["add", "foo", "ovl", "--provision"]) == 1
    ledger = worktree.load_ledger(workspace_path)
    assert ledger["worktrees"] == []
