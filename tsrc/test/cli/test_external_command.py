"""Tests for the external subcommand discovery mechanism.

When the subcommand name is not a builtin command, `tsrc <name>`
hands over to a `tsrc-<name>` executable found on PATH.
"""

import os
import stat
from pathlib import Path
from typing import List
from unittest import mock

import pytest

from tsrc.cli.main import main


class ExecCalled(Exception):
    """Raised by the mocked os.execvp to simulate a successful exec."""


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make `tmp_path` the only entry on PATH and return it.

    Fake `tsrc-*` executables can be created inside it with
    `make_external_command()`.
    """
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("VERBOSE", raising=False)
    return tmp_path


def make_external_command(bin_dir: Path, name: str) -> None:
    script = bin_dir / name
    script.write_text("#!/bin/sh\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def run_expecting_exec(args: List[str]) -> mock.MagicMock:
    """Run main() with os.execvp mocked so that it raises
    ExecCalled instead of replacing the process, and return the
    mock so callers can check how exec was invoked."""
    with mock.patch("os.execvp", side_effect=ExecCalled) as execvp:
        with pytest.raises(ExecCalled):
            main(args)
    return execvp


def test_external_command_receives_remaining_args(fake_bin: Path) -> None:
    make_external_command(fake_bin, "tsrc-hello")
    execvp = run_expecting_exec(["hello", "one", "two words"])
    execvp.assert_called_once_with("tsrc-hello", ["tsrc-hello", "one", "two words"])


def test_unknown_command_without_executable(fake_bin: Path) -> None:
    # No `tsrc-nosuchcommand` on PATH: argparse rejects the
    # unknown subcommand, as it did before the discovery mechanism.
    with mock.patch("os.execvp") as execvp:
        with pytest.raises(SystemExit) as e:
            main(["nosuchcommand"])
    assert e.value.code != 0
    execvp.assert_not_called()


def test_builtin_command_takes_precedence(
    fake_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_external_command(fake_bin, "tsrc-status")
    # Run from an empty directory: the builtin `status` command
    # fails because there is no workspace, which proves the builtin
    # ran instead of the external command found on PATH.
    monkeypatch.chdir(fake_bin)
    with mock.patch("os.execvp") as execvp:
        with pytest.raises(SystemExit) as e:
            main(["status"])
    assert e.value.code != 0
    execvp.assert_not_called()


def test_discovery_after_global_options(fake_bin: Path) -> None:
    make_external_command(fake_bin, "tsrc-hello")
    execvp = run_expecting_exec(["--verbose", "-q", "--color", "always", "hello", "x"])
    execvp.assert_called_once_with("tsrc-hello", ["tsrc-hello", "x"])
    # `--verbose` is mapped to the VERBOSE environment variable,
    # which is the convention `setup_ui()` already honors:
    assert os.environ.get("VERBOSE") == "1"


def test_discovery_after_color_with_equals(fake_bin: Path) -> None:
    make_external_command(fake_bin, "tsrc-hello")
    execvp = run_expecting_exec(["--color=never", "hello"])
    execvp.assert_called_once_with("tsrc-hello", ["tsrc-hello"])


def test_version_is_not_preempted(fake_bin: Path) -> None:
    make_external_command(fake_bin, "tsrc-hello")
    with mock.patch("os.execvp") as execvp:
        with pytest.raises(SystemExit) as e:
            main(["--version", "hello"])
    assert e.value.code == 0
    execvp.assert_not_called()
