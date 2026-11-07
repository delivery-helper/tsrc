""" Main tsrc entry point. """

import argparse
import functools
import os
import shutil
import subprocess
import sys
from typing import Callable, Optional, Sequence

import cli_ui as ui
import colored_traceback

from tsrc import __version__
from tsrc.cli import (
    apply_manifest,
    dump_manifest,
    foreach,
    init,
    log,
    manifest,
    status,
    sync,
)
from tsrc.errors import Error

ArgsList = Optional[Sequence[str]]
MainFunc = Callable[..., None]


#: Names of the commands implemented by tsrc itself, matching the
#: names given to `add_parser()` by the modules in `tsrc.cli`.
BUILTIN_COMMANDS = frozenset(
    [
        "apply-manifest",
        "dump-manifest",
        "foreach",
        "init",
        "log",
        "manifest",
        "status",
        "sync",
    ]
)

# Global options that may precede the subcommand name and take no value.
_FLAG_OPTIONS = ("--verbose", "-q", "--quiet")

# Global options that make argparse print something and exit before
# any subcommand runs: discovery must not preempt them.
_TERMINAL_OPTIONS = ("--version", "-h", "--help")


def _subcommand_index(argv: Sequence[str]) -> Optional[int]:
    """Return the index of the subcommand token in `argv`.

    Skip the global options that may precede the subcommand
    (``--verbose``, ``-q``/``--quiet`` and ``--color``), and return
    None when there is no subcommand token to look at, or when a
    terminal option (``--version``, ``-h``, ``--help``) is present:
    those are left for argparse to handle.
    """
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in _TERMINAL_OPTIONS:
            return None
        if token in _FLAG_OPTIONS:
            index += 1
            continue
        if token == "--color":
            index += 2
            continue
        if token.startswith("--color="):
            index += 1
            continue
        break
    if index >= len(argv):
        return None
    if argv[index].startswith("-"):
        return None
    return index


def _maybe_exec_external_command(argv: Sequence[str]) -> None:
    """Hand over to an external `tsrc-<name>` command, if any.

    When the subcommand is not a builtin command and an executable
    named `tsrc-<name>` exists on PATH, run it with every argument
    that follows the subcommand name, unchanged (the same
    convention as `git-<name>` commands). Builtin commands always
    take precedence. In every other case, return without doing
    anything and let argparse handle the arguments as usual.

    On POSIX, this process is replaced by the external command
    with `os.execvp()`. On Windows there is no exec, so the
    external command runs as a child process instead, and its
    exit code is propagated.
    """
    index = _subcommand_index(argv)
    if index is None:
        return
    name = argv[index]
    if name in BUILTIN_COMMANDS or "/" in name:
        return
    external = "tsrc-" + name
    path = shutil.which(external)
    if path is None:
        return
    if "--verbose" in argv[:index]:
        # `setup_ui()` honors the VERBOSE environment variable:
        # reuse that existing convention so the flag is not lost
        # when handing over to the external command.
        os.environ.setdefault("VERBOSE", "1")
    if os.name == "nt":
        sys.exit(subprocess.call([path, *argv[index + 1 :]]))
    os.execvp(external, [external, *argv[index + 1 :]])


def main_wrapper(main_func: MainFunc) -> MainFunc:
    """Wraps main() entry point to better deal with errors."""

    @functools.wraps(main_func)
    def wrapped(args: ArgsList = None) -> None:
        colored_traceback.add_hook()
        try:
            main_func(args=args)
        except Error as e:
            # "expected" failure, display it and exit note: we allow
            # Error instances to have an empty message. In that
            # case, do not print anything and assume relevant info has
            # already been printed.
            if e.message:  # noqa: B306
                ui.error(e.message)  # noqa: B306
            sys.exit(1)
        except KeyboardInterrupt:
            ui.warning("Interrupted by user, quitting")
            sys.exit(1)

    return wrapped


def setup_ui(args: argparse.Namespace) -> None:
    """Configure the cli_ui package using options
    set on the command line and environment variables.

    """
    verbose = False
    if os.environ.get("VERBOSE"):
        verbose = True
    if args.verbose:
        verbose = args.verbose
    ui.setup(verbose=verbose, quiet=args.quiet, color=args.color)


@main_wrapper
def main(args: ArgsList = None) -> None:
    """Main entry point."""
    main_impl(args=args)


def testable_main(args: ArgsList) -> None:
    """Same behavior as the main entrypoint, except we never
    hide backtraces when an exception is raised.

    """
    main_impl(args=args)


def main_impl(args: ArgsList = None) -> None:
    argv: Sequence[str] = sys.argv[1:] if args is None else args
    _maybe_exec_external_command(argv)

    parser = argparse.ArgumentParser(prog="tsrc")
    parser.add_argument("--version", action="version", version="tsrc " + __version__)

    parser.add_argument("--verbose", help="show debug messages", action="store_true")
    parser.add_argument(
        "-q", "--quiet", help="only display warnings and errors", action="store_true"
    )
    parser.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        help="whether to enable colored output",
    )

    actions_parser = parser.add_subparsers(help="available actions", dest="action")

    for module in (
        apply_manifest,
        dump_manifest,
        foreach,
        init,
        log,
        manifest,
        status,
        sync,
    ):
        module.configure_parser(actions_parser)

    namespace = parser.parse_args(args=args)

    setup_ui(namespace)
    if not hasattr(namespace, "run"):
        parser.print_help()
        sys.exit(1)
    namespace.run(namespace)
