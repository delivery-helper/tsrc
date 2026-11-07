"""Overlay provisioning for `tsrc-worktree --provision`.

Implements the WSL/Linux half of design section 6.2, following the
prebuild-overlay trial (see `prebuild-overlay-trial.result.md`):

* the prebuilt base is a frozen tree: a full checkout with
  dependencies installed and its `.git` removed.  It lives at
  `<workspace>/.tsrc/prebuild/<dest-slug>/` and is treated as
  read-only (the trial verified the base stays byte-identical).
* a session tree is an overlayfs mount: lowerdir=base,
  upperdir/workdir per session under
  `<workspace>/.tsrc/overlay/<name>/`, merged at the worktree path.
  Edits land in upper only; cleanup is unmount + delete upper.
* the `.git` separation follows the trial recipe: register the
  worktree once in a throwaway directory (`git worktree add
  --no-checkout`), rewrite that registration's `gitdir` pointer at
  the merged path, drop a `.git` file into merged, then
  `git reset --mixed HEAD` to fill the index without touching the
  working tree.  HEAD / index / branch are per-session; objects
  and refs stay shared with the source clone.

Mount provider (the trial's open homework, decided here):
`sudo mount -t overlay` — the "limited sudo mount helper" option.
The alternatives were measured in the trial and do not work for a
per-command CLI: `unshare -rm` mounts live only inside their
private mount namespace and are invisible from outside (and
`nsenter` into them fails unprivileged), and `fuse-overlayfs` is
not installed on WSL.  When `sudo -n mount` is unavailable the
provision fails with a clear message instead of silently falling
back to a plain worktree.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from tsrc_ext.common import TsrcExtError, git_output, run_cmd, run_git


def prebuild_base_path(workspace: Path, dest: str) -> Path:
    slug = dest.replace("/", "-")
    return workspace / ".tsrc" / "prebuild" / slug


def overlay_dirs(workspace: Path, name: str) -> dict:
    root = workspace / ".tsrc" / "overlay" / name
    return {
        "root": root,
        "upper": root / "upper",
        "work": root / "work",
    }


def read_base_commit(base: Path) -> Optional[str]:
    """The commit a prebuilt base was frozen from, if recorded.

    The record lives beside the base (`<slug>.json` next to
    `<slug>/`), not inside it: a file inside the base tree would
    show up as untracked in every provisioned worktree.
    """
    info = base.with_suffix(".json")
    if not info.exists():
        return None
    try:
        import json

        data = json.loads(info.read_text())
    except Exception:
        return None
    commit = data.get("commit")
    return str(commit) if commit else None


def check_sudo_mount() -> None:
    rc, _, _ = run_cmd(["sudo", "-n", "true"])
    if rc != 0:
        raise TsrcExtError(
            "overlay provisioning needs passwordless sudo for "
            "`mount -t overlay` (sudo -n failed); fuse-overlayfs is "
            "not installed, and unshare mounts are invisible outside "
            "their namespace. Run as plain worktree instead, or "
            "provide a prebuilt base + a mount provider."
        )


def is_mounted(path: Path) -> bool:
    return os.path.ismount(str(path))


def mount_overlay(base: Path, upper: Path, work: Path, merged: Path) -> None:
    for directory in (upper, work, merged):
        directory.mkdir(parents=True, exist_ok=True)
    opts = "lowerdir=%s,upperdir=%s,workdir=%s" % (base, upper, work)
    rc, out, err = run_cmd(
        ["sudo", "mount", "-t", "overlay", "overlay", "-o", opts, str(merged)]
    )
    if rc != 0:
        raise TsrcExtError(
            "overlay mount failed for %s (exit %d): %s"
            % (merged, rc, (err or out).strip())
        )
    if not is_mounted(merged):
        raise TsrcExtError(
            "overlay mount reported success but %s is not a mount point" % merged
        )


def unmount_overlay(merged: Path) -> bool:
    """Unmount the overlay at `merged`. Returns True if one was up."""
    if not is_mounted(merged):
        return False
    rc, out, err = run_cmd(["sudo", "umount", str(merged)])
    if rc != 0:
        raise TsrcExtError(
            "failed to unmount overlay at %s (exit %d): %s"
            % (merged, rc, (err or out).strip())
        )
    return True


def provision_overlay(
    workspace: Path,
    clone_path: Path,
    dest: str,
    name: str,
    branch: str,
    base_ref: str,
    branch_exists: bool,
) -> dict:
    """Create an overlay-provisioned worktree at the merged path.

    Returns {"path": ..., "provision": "overlay", "base": ...,
    "base_commit": ...}.  The caller (worktree add/start) owns the
    ledger entry; this function owns the mount and the gitdir wiring.
    """
    base = prebuild_base_path(workspace, dest)
    if not base.is_dir():
        raise TsrcExtError(
            "no prebuilt base for %s (expected %s). Build one first: "
            "check the repo out there, install its dependencies, "
            "then remove its .git and record the commit in "
            "<slug>.json beside it. "
            "Without a base, --provision cannot run." % (dest, base)
        )
    if (base / ".git").exists():
        raise TsrcExtError(
            "prebuilt base %s still has a .git entry; freeze it first "
            "(remove .git) so sessions cannot share HEAD/index" % base
        )
    check_sudo_mount()
    dirs = overlay_dirs(workspace, name)
    merged = workspace / ".tsrc" / "worktrees" / name
    if merged.exists():
        raise TsrcExtError("worktree path already exists: %s" % merged)
    tmp_parent = dirs["root"]
    tmp_parent.mkdir(parents=True, exist_ok=True)
    tmp_wt = tmp_parent / "tmp-wt"
    if tmp_wt.exists():
        shutil.rmtree(tmp_wt)
    # 1. Register the worktree once in a throwaway directory.
    if branch_exists:
        add_cmd = ["worktree", "add", "--no-checkout", str(tmp_wt), branch]
    else:
        add_cmd = [
            "worktree",
            "add",
            "--no-checkout",
            "-b",
            branch,
            str(tmp_wt),
            base_ref,
        ]
    run_git(clone_path, *add_cmd, check=True)
    try:
        gitfile = tmp_wt / ".git"
        content = gitfile.read_text().strip()
        if not content.startswith("gitdir:"):
            raise TsrcExtError(
                "unexpected .git file in throwaway worktree: %s" % content
            )
        gitdir = Path(content[len("gitdir:") :].strip())
        # 2. Mount the overlay at the real worktree path.
        mount_overlay(base, dirs["upper"], dirs["work"], merged)
        try:
            # 3. Rewire the registration to the merged path.
            (merged / ".git").write_text("gitdir: %s\n" % gitdir)
            (gitdir / "gitdir").write_text("%s/.git\n" % merged)
            # 4. Fill the index without touching the working tree.
            run_git(merged, "reset", "--mixed", "HEAD")
            git_output(merged, "status", "--porcelain")
        except Exception:
            unmount_overlay(merged)
            raise
    finally:
        # The throwaway directory itself is no longer a worktree;
        # its registration now points at merged.
        if tmp_wt.exists():
            shutil.rmtree(tmp_wt)
    return {
        "path": str(merged),
        "provision": "overlay",
        "base": str(base),
        "base_commit": read_base_commit(base),
    }


def _remove_tree(path: Path) -> None:
    """Remove an overlay backing directory.

    The kernel creates root-owned artifacts inside the overlay
    workdir (the mount runs as root via sudo), so a plain rmtree
    fails with Permission denied.  Fall back to `sudo rm -rf`
    for that case; the mount provider already requires sudo.
    """
    try:
        shutil.rmtree(path)
    except PermissionError:
        rc, out, err = run_cmd(["sudo", "rm", "-rf", str(path)])
        if rc != 0:
            raise TsrcExtError(
                "failed to remove overlay dir %s (exit %d): %s"
                % (path, rc, (err or out).strip())
            )


def teardown_overlay(workspace: Path, name: str, merged: Path) -> dict:
    """Unmount and remove the overlay backing for a worktree."""
    unmounted = unmount_overlay(merged)
    dirs = overlay_dirs(workspace, name)
    if dirs["root"].exists():
        _remove_tree(dirs["root"])
    return {"unmounted": unmounted}
