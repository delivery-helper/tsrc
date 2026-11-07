"""Engine seam (design memo sections 10 and 13).

The `tsrc-scalar` / `tsrc-worktree` commands are windows onto the
engine: they parse arguments, ask the engine to do the work, and
print the results.  The engine owns the state (the worktree ledger,
the per-repo Scalar state) and the sequencing.

There is no engine process yet, so `get_engine()` returns the
one-shot implementation (`OneShotEngine`), which executes each
operation directly against the workspace on disk.  When the engine
exists, this module is the single joint to swap: `get_engine()`
will connect to it (Unix socket + JSON, see design section 13.3)
and return an `Engine` speaking the same operations.  Nothing else
in this package talks to the engine.

One-shot mode loses, by design (design section 13.4): instant
answers (every query scans the workspace), automatic enforcement
(no watcher), and consistency between concurrent users.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Protocol, runtime_checkable

from tsrc_ext.common import TsrcExtError


@runtime_checkable
class Engine(Protocol):
    """Operations the window commands ask the engine for."""

    # -- Scalar (design section 6.1) -------------------------------
    def scalar_apply(  # noqa: E704
        self, repo_path: Path, *, backend: Optional[str] = None
    ) -> dict: ...

    def scalar_status(self, repo_path: Path) -> dict: ...  # noqa: E704

    def scalar_maintenance(self, repo_path: Path) -> dict: ...  # noqa: E704

    def scalar_unregister(  # noqa: E704
        self,
        repo_path: Path,
        *,
        full: bool = False,
        backend: Optional[str] = None,
    ) -> dict: ...

    # -- Worktree (design sections 6.2, 6.3, 12) -------------------
    def worktree_add(  # noqa: E704
        self,
        workspace: Path,
        dest: str,
        name: str,
        *,
        branch: Optional[str] = None,
        base: Optional[str] = None,
        provision: bool = False,
        owner: Optional[str] = None,
    ) -> dict: ...

    def worktree_list(self, workspace: Path) -> dict: ...  # noqa: E704

    def worktree_remove(  # noqa: E704
        self, workspace: Path, name: str, *, force: bool = False
    ) -> dict: ...

    def worktree_hold(  # noqa: E704
        self, workspace: Path, name: str, *, held: bool
    ) -> dict: ...

    def worktree_prune(self, workspace: Path) -> dict: ...  # noqa: E704

    def worktree_start(  # noqa: E704
        self,
        workspace: Path,
        branch: str,
        dests: List[str],
        *,
        provision: bool = False,
        fetch: bool = False,
        owner: Optional[str] = None,
    ) -> dict: ...


class OneShotEngine:
    """Engine that executes every operation directly, in-process.

    Used when no engine process is reachable.  Each method is a
    thin delegation to the operation modules, so the engine seam
    stays a single function (`get_engine`) even as the operations
    grow.
    """

    # -- Scalar ------------------------------------------------------
    def scalar_apply(self, repo_path: Path, *, backend: Optional[str] = None) -> dict:
        from tsrc_ext.scalar import apply_scalar

        return apply_scalar(repo_path, backend=backend)

    def scalar_status(self, repo_path: Path) -> dict:
        from tsrc_ext.scalar import scalar_status

        return scalar_status(repo_path)

    def scalar_maintenance(self, repo_path: Path) -> dict:
        from tsrc_ext.scalar import run_maintenance

        return run_maintenance(repo_path)

    def scalar_unregister(
        self,
        repo_path: Path,
        *,
        full: bool = False,
        backend: Optional[str] = None,
    ) -> dict:
        from tsrc_ext.scalar import unregister_scalar

        return unregister_scalar(repo_path, full=full, backend=backend)

    # -- Worktree ----------------------------------------------------
    def worktree_add(
        self,
        workspace: Path,
        dest: str,
        name: str,
        *,
        branch: Optional[str] = None,
        base: Optional[str] = None,
        provision: bool = False,
        owner: Optional[str] = None,
    ) -> dict:
        from tsrc_ext.worktree import add_worktree

        return add_worktree(
            workspace,
            dest,
            name,
            branch=branch,
            base=base,
            provision=provision,
            owner=owner,
        )

    def worktree_list(self, workspace: Path) -> dict:
        from tsrc_ext.worktree import list_worktrees

        return list_worktrees(workspace)

    def worktree_remove(
        self, workspace: Path, name: str, *, force: bool = False
    ) -> dict:
        from tsrc_ext.worktree import remove_worktree

        return remove_worktree(workspace, name, force=force)

    def worktree_hold(self, workspace: Path, name: str, *, held: bool) -> dict:
        from tsrc_ext.worktree import hold_worktree

        return hold_worktree(workspace, name, held=held)

    def worktree_prune(self, workspace: Path) -> dict:
        from tsrc_ext.worktree import prune_ledger

        return prune_ledger(workspace)

    def worktree_start(
        self,
        workspace: Path,
        branch: str,
        dests: List[str],
        *,
        provision: bool = False,
        fetch: bool = False,
        owner: Optional[str] = None,
    ) -> dict:
        from tsrc_ext.worktree import start_bundle

        return start_bundle(
            workspace,
            branch,
            dests,
            provision=provision,
            fetch=fetch,
            owner=owner,
        )


def get_engine() -> Engine:
    """Return the engine to use.

    `TSRC_ENGINE_SOCKET` names the engine's Unix socket.  The IPC
    form is stage 2+ work (design sections 10.5, 13.3): when the
    variable is set but no engine answers yet, fail loudly instead
    of silently falling back, so a half-wired setup cannot be
    mistaken for a working one.  Unset it to use one-shot mode.
    """
    socket_path = os.environ.get("TSRC_ENGINE_SOCKET")
    if socket_path:
        raise TsrcExtError(
            "engine IPC via TSRC_ENGINE_SOCKET=%s is not implemented yet "
            "(stage 2 ships one-shot mode only); unset TSRC_ENGINE_SOCKET "
            "to run one-shot" % socket_path
        )
    return OneShotEngine()
