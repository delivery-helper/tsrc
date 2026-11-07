"""Tests for the external subcommand discovery mechanism.

When the subcommand name is not a builtin command, `tsrc <name>`
hands over to a `tsrc-<name>` executable found on PATH.
"""

import contextlib
import os
import stat
from pathlib import Path
from typing import Iterator, List, Tuple
from unittest import mock

import pytest

from tsrc.cli.main import main


class ExecCalledError(Exception):
    """Raised by the mocked handover to simulate a successful exec."""


# The two ways `_maybe_exec_external_command()` can hand over:
# `os.execvp` on POSIX, `subprocess.call` on Windows.
HandoverMocks = Tuple[mock.MagicMock, mock.MagicMock]


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
    if os.name == "nt":
        # On Windows, `shutil.which()` only finds files whose
        # extension is listed in PATHEXT:
        name += ".bat"
    script = bin_dir / name
    script.write_text("#!/bin/sh\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


@contextlib.contextmanager
def patched_handover(simulate: bool) -> Iterator[HandoverMocks]:
    """Mock both handover mechanisms.

    With `simulate` set, calling one of them raises `ExecCalledError`
    instead of running the external command.
    """
    side_effect = ExecCalledError if simulate else None
    with mock.patch("os.execvp", side_effect=side_effect) as execvp, mock.patch(
        "subprocess.call", side_effect=side_effect
    ) as call:
        yield execvp, call


def run_expecting_handover(args: List[str]) -> HandoverMocks:
    """Run main() expecting a handover, and return the mocks so
    callers can check how the external command was invoked."""
    with patched_handover(simulate=True) as mocks:
        with pytest.raises(ExecCalledError):
            main(args)
    return mocks


def assert_handover(mocks: HandoverMocks, name: str, args: List[str]) -> None:
    execvp, call = mocks
    if os.name == "nt":
        execvp.assert_not_called()
        call.assert_called_once()
        command = call.call_args[0][0]
        expected = name + ".bat"
        assert Path(command[0]).name == expected
        assert command[1:] == args
    else:
        call.assert_not_called()
        execvp.assert_called_once_with(name, [name, *args])


def assert_no_handover(mocks: HandoverMocks) -> None:
    execvp, call = mocks
    execvp.assert_not_called()
    call.assert_not_called()


def test_external_command_receives_remaining_args(fake_bin: Path) -> None:
    make_external_command(fake_bin, "tsrc-hello")
    mocks = run_expecting_handover(["hello", "one", "two words"])
    assert_handover(mocks, "tsrc-hello", ["one", "two words"])


def test_unknown_command_without_executable(fake_bin: Path) -> None:
    # No `tsrc-nosuchcommand` on PATH: argparse rejects the
    # unknown subcommand, as it did before the discovery mechanism.
    with patched_handover(simulate=False) as mocks:
        with pytest.raises(SystemExit) as e:
            main(["nosuchcommand"])
    assert e.value.code != 0
    assert_no_handover(mocks)


def test_builtin_command_takes_precedence(
    fake_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_external_command(fake_bin, "tsrc-status")
    # Run from an empty directory: the builtin `status` command
    # fails because there is no workspace, which proves the builtin
    # ran instead of the external command found on PATH.
    monkeypatch.chdir(fake_bin)
    with patched_handover(simulate=False) as mocks:
        with pytest.raises(SystemExit) as e:
            main(["status"])
    assert e.value.code != 0
    assert_no_handover(mocks)


def test_discovery_after_global_options(fake_bin: Path) -> None:
    make_external_command(fake_bin, "tsrc-hello")
    mocks = run_expecting_handover(
        ["--verbose", "-q", "--color", "always", "hello", "x"]
    )
    assert_handover(mocks, "tsrc-hello", ["x"])
    # `--verbose` is mapped to the VERBOSE environment variable,
    # which is the convention `setup_ui()` already honors:
    assert os.environ.get("VERBOSE") == "1"


def test_discovery_after_color_with_equals(fake_bin: Path) -> None:
    make_external_command(fake_bin, "tsrc-hello")
    mocks = run_expecting_handover(["--color=never", "hello"])
    assert_handover(mocks, "tsrc-hello", [])


def test_version_is_not_preempted(fake_bin: Path) -> None:
    make_external_command(fake_bin, "tsrc-hello")
    with patched_handover(simulate=False) as mocks:
        with pytest.raises(SystemExit) as e:
            main(["--version", "hello"])
    assert e.value.code == 0
    assert_no_handover(mocks)


def test_windows_handover_propagates_exit_code(fake_bin: Path) -> None:
    # On Windows there is no exec: the external command runs as a
    # child process and its exit code becomes the exit code of
    # `tsrc` itself. Simulate that branch on any platform.
    make_external_command(fake_bin, "tsrc-hello")
    with mock.patch.object(os, "name", "nt"), mock.patch(
        "shutil.which", return_value="C:\\bin\\tsrc-hello.exe"
    ), mock.patch("subprocess.call", return_value=3) as call:
        with pytest.raises(SystemExit) as e:
            main(["hello", "x"])
    assert e.value.code == 3
    call.assert_called_once_with(["C:\\bin\\tsrc-hello.exe", "x"])
