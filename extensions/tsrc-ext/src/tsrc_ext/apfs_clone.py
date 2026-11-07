"""APFS-clone provisioning for `tsrc-worktree --provision` (macOS).

Implements the macOS half of design section 6.2, following the
prebuild-overlay trial (see `prebuild-overlay-trial.result.md`):

* the prebuilt base is the same frozen tree the Linux form uses:
  a full checkout with dependencies installed and its `.git`
  removed, at `<workspace>/.tsrc/prebuild/<dest-slug>/`, with
  the frozen commit recorded in `<slug>.json` beside the base
  (not inside it, so it never shows up in a session tree).
* a session tree is a copy-on-write clone of the base made with
  `cp -cR`.  Unlike the overlay form there is no mount: the
  clone is a real directory whose blocks are shared with the
  base until written, so edits can never leak into the base,
  and the trial measured several clones held at once with no
  measurable decrease of the volume's free space.
* the `.git` separation follows the same recipe as the overlay
  form: register the worktree once in a throwaway directory
  (`git worktree add --no-checkout`), rewrite that
  registration's `gitdir` pointer at the clone path, drop a
  `.git` file into the clone, then `git reset --mixed HEAD`
  to fill the index without touching the working tree.  HEAD /
  index / branch are per-session; objects and refs stay shared
  with the source clone.

Two platform caveats, both from the trial:

* `cp -c` silently falls back to a plain copy when the
  destination is on a different filesystem, which would quietly
  give up the capacity benefit.  Base and worktree both live
  under `<workspace>/.tsrc`, so provisioning verifies they are
  on the same device before copying and fails otherwise.
* `du` cannot measure a clone's real size (shared blocks are
  charged to every clone), so capacity is judged by the
  volume's free space, never by `du` of the clone.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from tsrc_ext.common import TsrcExtError, git_output, run_cmd, run_git
from tsrc_ext.overlay import prebuild_base_path, read_base_commit


def clone_root(workspace: Path, name: str) -> Path:
    """Staging directory for one clone provisioning (by name).

    Only holds the throwaway worktree registration while the
    clone is being wired up; teardown removes it.
    """
    return workspace / ".tsrc" / "clone" / name


def _check_base(base: Path, dest: str) -> None:
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


def _check_same_device(base: Path, workspace: Path) -> None:
    """`cp -c` needs base and clone on one APFS volume.

    Both live under the workspace's `.tsrc`, so this only fails
    for exotic layouts (a symlinked `.tsrc`, a separately
    mounted prebuild dir).  Failing here is deliberate: a silent
    fallback to a full copy would look identical to the user
    while consuming the full tree size per session.
    """
    if os.stat(str(base)).st_dev != os.stat(str(workspace)).st_dev:
        raise TsrcExtError(
            "prebuilt base %s is on a different filesystem than the "
            "workspace %s; `cp -c` would silently fall back to a "
            "plain copy. Move the base onto the workspace volume "
            "to provision APFS clones." % (base, workspace)
        )


def provision_clone(
    workspace: Path,
    clone_path: Path,
    dest: str,
    name: str,
    branch: str,
    base_ref: str,
    branch_exists: bool,
) -> dict:
    """Create an APFS-clone-provisioned worktree at the clone path.

    Returns {"path": ..., "provision": "apfs-clone",
    "base": ..., "base_commit": ...}.  The caller (worktree
    add/start) owns the ledger entry; this function owns the
    copy and the gitdir wiring.
    """
    base = prebuild_base_path(workspace, dest)
    _check_base(base, dest)
    _check_same_device(base, workspace)
    root = clone_root(workspace, name)
    merged = workspace / ".tsrc" / "worktrees" / name
    if merged.exists():
        raise TsrcExtError("worktree path already exists: %s" % merged)
    root.mkdir(parents=True, exist_ok=True)
    tmp_wt = root / "tmp-wt"
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
        # 2. Clone the base, copy-on-write, at the worktree path.
        #  creates the destination but not missing parents.
        merged.parent.mkdir(parents=True, exist_ok=True)
        rc, out, err = run_cmd(["cp", "-cR", str(base), str(merged)])
        if rc != 0:
            raise TsrcExtError(
                "APFS clone of base %s to %s failed (exit %d): %s"
                % (base, merged, rc, (err or out).strip())
            )
        try:
            # 3. Rewire the registration to the clone path.
            (merged / ".git").write_text("gitdir: %s\n" % gitdir)
            (gitdir / "gitdir").write_text("%s/.git\n" % merged)
            # 4. Fill the index without touching the working tree.
            run_git(merged, "reset", "--mixed", "HEAD")
            git_output(merged, "status", "--porcelain")
        except Exception:
            if merged.exists():
                shutil.rmtree(merged)
            raise
    finally:
        # The throwaway directory itself is no longer a worktree;
        # its registration now points at the clone.
        if tmp_wt.exists():
            shutil.rmtree(tmp_wt)
    return {
        "path": str(merged),
        "provision": "apfs-clone",
        "base": str(base),
        "base_commit": read_base_commit(base),
    }


def teardown_clone(workspace: Path, name: str, merged: Path) -> dict:
    """Remove the clone tree and its provisioning staging.

    There is nothing to unmount: the clone is a plain directory,
    and deleting it returns its (few) private blocks.  The base
    is never touched.  The caller removes the worktree's admin
    directory (from the `.git` file it read beforehand).
    """
    removed = False
    if merged.exists():
        shutil.rmtree(merged)
        removed = True
    root = clone_root(workspace, name)
    if root.exists():
        shutil.rmtree(root)
    return {"tree_removed": removed}
