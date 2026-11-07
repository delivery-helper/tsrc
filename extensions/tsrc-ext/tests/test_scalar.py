"""Tests for tsrc-scalar: backend resolution, decomposed apply,
status, maintenance, and unregister.

Global gitconfig is isolated by the `isolated_gitconfig` fixture,
so the decomposed form never touches the real ~/.gitconfig.
"""

import os
import stat
from pathlib import Path

import pytest
from conftest import make_workspace
from tsrc_ext import scalar
from tsrc_ext.common import run_cmd

from tsrc.test.helpers.cli import CLI
from tsrc.test.helpers.git_server import GitServer


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    return bin_dir


def write_script(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_resolution_prefers_git_scalar(fake_bin: Path) -> None:
    """A working `git scalar` wins over a stand-alone scalar."""
    log = fake_bin / "git.log"
    write_script(
        fake_bin / "git",
        'if [ "$1" = "scalar" ]; then echo "$@" >> "%s"; exit 0; fi\n'
        'exec /usr/bin/git "$@"\n' % log,
    )
    write_script(fake_bin / "scalar", "exit 0\n")
    backend = scalar.resolve_scalar()
    assert backend.kind == "git-scalar"
    assert backend.argv == ["git", "scalar"]


def test_resolution_falls_back_to_standalone_scalar(fake_bin: Path) -> None:
    write_script(
        fake_bin / "git",
        'if [ "$1" = "scalar" ]; then exit 1; fi\n' 'exec /usr/bin/git "$@"\n',
    )
    write_script(fake_bin / "scalar", "exit 0\n")
    backend = scalar.resolve_scalar()
    assert backend.kind == "scalar"
    assert backend.argv == [str(fake_bin / "scalar")]


def test_resolution_falls_back_to_decomposed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no git scalar and no binary, the decomposed form is used."""
    monkeypatch.setattr(scalar, "_git_scalar_works", lambda: False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    backend = scalar.resolve_scalar()
    assert backend.kind == "decomposed"
    assert backend.argv == []


def test_apply_status_maintenance_unregister_decomposed(
    git_server: GitServer,
    tsrc_cli: CLI,
    workspace_path: Path,
    isolated_gitconfig: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end decomposed cycle on a real cloned repo."""
    make_workspace(git_server, tsrc_cli, ["foo"])
    monkeypatch.setattr(scalar, "_git_scalar_works", lambda: False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    repo = workspace_path / "foo"

    assert scalar.main(["apply", "foo"]) == 0
    # All 27 keys are set locally, and the global entries exist.
    for key, want in scalar.SCALAR_CONFIG_KEYS.items():
        rc, out, _ = run_cmd(["git", "config", "--get", key], cwd=repo)
        assert rc == 0, key
        assert out.strip() == want, key
    rc, out, _ = run_cmd(["git", "config", "--global", "--get-all", "scalar.repo"])
    assert str(repo) in out.splitlines()
    rc, out, _ = run_cmd(["git", "config", "--global", "--get-all", "maintenance.repo"])
    assert str(repo) in out.splitlines()

    assert scalar.main(["status", "foo"]) == 0
    status = scalar.scalar_status(repo)
    assert status["state"] == "registered"
    assert status["keys_matched"] == len(scalar.SCALAR_CONFIG_KEYS)

    assert scalar.main(["maintenance", "foo"]) == 0

    assert scalar.main(["unregister", "--full", "foo"]) == 0
    status = scalar.scalar_status(repo)
    assert status["state"] == "not-registered"
    assert status["keys_matched"] == 0
    rc, out, _ = run_cmd(["git", "config", "--global", "--get-all", "scalar.repo"])
    assert str(repo) not in out.splitlines()


def test_apply_with_fake_scalar_binary(
    git_server: GitServer,
    tsrc_cli: CLI,
    workspace_path: Path,
    fake_bin: Path,
    isolated_gitconfig: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a scalar binary, apply delegates instead of decomposing."""
    make_workspace(git_server, tsrc_cli, ["foo"])
    log = fake_bin / "scalar.log"
    write_script(
        fake_bin / "scalar",
        'echo "$PWD :: $*" >> "%s"\n' % log,
    )
    write_script(
        fake_bin / "git",
        'if [ "$1" = "scalar" ]; then exit 1; fi\n' 'exec /usr/bin/git "$@"\n',
    )
    repo = workspace_path / "foo"
    assert scalar.main(["apply", "foo"]) == 0
    lines = log.read_text().splitlines()
    assert lines == ["%s :: register" % repo]
    # The binary path must not have set the local keys itself.
    rc, _, _ = run_cmd(["git", "config", "--get", "core.fsmonitor"], cwd=repo)
    assert rc != 0

    assert scalar.main(["unregister", "foo"]) == 0
    lines = log.read_text().splitlines()
    assert lines[-1] == "%s :: unregister" % repo


def test_status_reports_partial_when_keys_removed(
    git_server: GitServer,
    tsrc_cli: CLI,
    workspace_path: Path,
    isolated_gitconfig: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_workspace(git_server, tsrc_cli, ["foo"])
    monkeypatch.setattr(scalar, "_git_scalar_works", lambda: False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    repo = workspace_path / "foo"
    assert scalar.main(["apply", "foo"]) == 0
    # Drop one local key: status must degrade to partial, not stay
    # registered, while the global entries remain.
    run_cmd(["git", "config", "--unset", "core.fsmonitor"], cwd=repo)
    status = scalar.scalar_status(repo)
    assert status["state"] == "partial"
    assert status["keys_matched"] == len(scalar.SCALAR_CONFIG_KEYS) - 1


def test_apply_unknown_repo_fails(
    git_server: GitServer,
    tsrc_cli: CLI,
    workspace_path: Path,
    isolated_gitconfig: dict,
) -> None:
    make_workspace(git_server, tsrc_cli, ["foo"])
    assert scalar.main(["apply", "no-such-repo"]) == 1
