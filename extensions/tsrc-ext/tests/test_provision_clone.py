"""Tests for the macOS APFS-clone form of `--provision` (6.2, 8).

The suite runs on Linux, where GNU cp has no `-c` flag and there
is no APFS, so the copy itself is faked: a `cp` shim first in
PATH translates `-cR` into a plain recursive copy, and the
provision dispatch is pointed at the clone kind.  What is under
test here is the wiring around the copy (throwaway registration,
gitdir rewire, index fill, ledger fields, teardown) -- the
copy-on-write capacity semantics are an OS facility, validated
on a real Mac (see the stage result file).
"""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from conftest import make_workspace
from tsrc_ext import worktree
from tsrc_ext.common import run_cmd

from tsrc.test.helpers.cli import CLI
from tsrc.test.helpers.git_server import GitServer


@pytest.fixture
def fake_cp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `cp` that accepts BSD `-cR` by dropping the clone flag."""
    real_cp = shutil.which("cp")
    assert real_cp is not None
    shim = tmp_path / "shim"
    shim.mkdir()
    script = shim / "cp"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-cR" ]; then\n'
        "  shift\n"
        '  exec %s -R "$@"\n'
        "fi\n"
        'exec %s "$@"\n' % (real_cp, real_cp)
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ.get("PATH", ""))


@pytest.fixture
def clone_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worktree, "_provision_kind", lambda: "apfs-clone")


def _make_base(workspace_path: Path, dest: str) -> str:
    """Freeze a prebuilt base from the workspace clone; return HEAD."""
    clone = workspace_path / dest
    base = workspace_path / ".tsrc" / "prebuild" / dest
    shutil.copytree(clone, base, ignore=shutil.ignore_patterns(".git"))
    _, head, _ = run_cmd(["git", "rev-parse", "HEAD"], cwd=clone)
    record = workspace_path / ".tsrc" / "prebuild" / (dest + ".json")
    record.write_text(json.dumps({"commit": head.strip(), "repo": dest}))
    return head.strip()


def test_provision_kind_by_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert worktree._provision_kind() == "apfs-clone"
    monkeypatch.setattr(sys, "platform", "linux")
    assert worktree._provision_kind() == "overlay"


def test_provision_clone_roundtrip(
    git_server: GitServer,
    tsrc_cli: CLI,
    workspace_path: Path,
    fake_cp: None,
    clone_dispatch: None,
) -> None:
    """Clone provision: real directory, base intact, clean teardown."""
    make_workspace(git_server, tsrc_cli, ["foo"])
    clone = workspace_path / "foo"
    head = _make_base(workspace_path, "foo")
    base = workspace_path / ".tsrc" / "prebuild" / "foo"

    assert worktree.main(["add", "foo", "cln", "--provision"]) == 0
    merged = workspace_path / ".tsrc" / "worktrees" / "cln"
    # A clone is a real directory (no mount), with its own inodes.
    assert not os.path.ismount(str(merged))
    assert (merged / "README.txt").read_text() == "hello\n"
    assert (
        os.stat(str(merged / "README.txt")).st_ino
        != os.stat(str(base / "README.txt")).st_ino
    )
    rc, out, _ = run_cmd(["git", "status", "--porcelain"], cwd=merged)
    assert rc == 0 and out.strip() == ""
    rc, out, _ = run_cmd(["git", "worktree", "list", "--porcelain"], cwd=clone)
    assert str(merged) in out
    # The throwaway registration directory is gone after wiring.
    assert not (workspace_path / ".tsrc" / "clone" / "cln" / "tmp-wt").exists()

    ledger = worktree.load_ledger(workspace_path)
    entry = ledger["worktrees"][0]
    assert entry["provision"] == "apfs-clone"
    assert entry["base"] == str(base)
    assert entry["base_commit"] == head

    # An edit lands in the clone only; the base is untouched.
    (merged / "README.txt").write_text("edited\n")
    assert (base / "README.txt").read_text() == "hello\n"

    assert worktree.main(["remove", "--force", "cln"]) == 0
    assert not merged.exists()
    assert not (workspace_path / ".tsrc" / "clone" / "cln").exists()
    assert (base / "README.txt").read_text() == "hello\n"
    rc, out, _ = run_cmd(["git", "worktree", "list", "--porcelain"], cwd=clone)
    assert str(merged) not in out
    ledger = worktree.load_ledger(workspace_path)
    assert ledger["worktrees"] == []


def test_provision_clone_without_base_fails_clearly(
    git_server: GitServer,
    tsrc_cli: CLI,
    workspace_path: Path,
    fake_cp: None,
    clone_dispatch: None,
) -> None:
    make_workspace(git_server, tsrc_cli, ["foo"])
    assert worktree.main(["add", "foo", "cln", "--provision"]) == 1
    ledger = worktree.load_ledger(workspace_path)
    assert ledger["worktrees"] == []


def test_start_provision_clone_bundle(
    git_server: GitServer,
    tsrc_cli: CLI,
    workspace_path: Path,
    fake_cp: None,
    clone_dispatch: None,
    capsys: pytest.CaptureFixture,
) -> None:
    make_workspace(
        git_server,
        tsrc_cli,
        ["foo", "bar"],
        groups={"g": ["foo", "bar"]},
    )
    for dest in ("foo", "bar"):
        _make_base(workspace_path, dest)
    assert worktree.main(["start", "feat-c", "-g", "g", "--provision"]) == 0
    out = capsys.readouterr().out
    assert "foo: created" in out
    assert "bar: created" in out
    ledger = worktree.load_ledger(workspace_path)
    assert len(ledger["worktrees"]) == 2
    assert all(e["provision"] == "apfs-clone" for e in ledger["worktrees"])
    assert all(e["base_commit"] for e in ledger["worktrees"])
    for dest in ("foo", "bar"):
        wt = workspace_path / ".tsrc" / "worktrees" / ("feat-c--" + dest)
        assert (wt / "README.txt").exists()
        assert worktree.main(["remove", "feat-c--" + dest]) == 0
        assert not wt.exists()
    ledger = worktree.load_ledger(workspace_path)
    assert ledger["worktrees"] == []
