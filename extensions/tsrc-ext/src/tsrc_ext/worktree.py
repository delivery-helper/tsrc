"""`tsrc-worktree`: worktree lifecycle and bundle start.

Implements design sections 6.2, 6.3 and 12 (minimal ledger form).

State lives in `<workspace>/.tsrc/worktrees.yml` — the ledger.
Each entry carries the bundle (task) grouping, owner, creation
time, base SHA, provision kind, hold flag, and the liveness state
from the last scan (`status` + `last_checked`).  Liveness follows
design section 12.3 in minimal form: an entry is `alive` when its
directory exists and its HEAD is on the recorded branch, `missing`
when the directory is gone (the prunable accident shape), and
`broken` when the directory exists but git cannot confirm it.
`list` performs the scan and reports worktrees that exist in git
but are not in the ledger as `unregistered`.

`start <branch>` is the `repo start` equivalent (design section
6.3): it cuts the same branch across a bundle of repos, provisions
a worktree per repo, and records the bundle.  Atomicity is
deliberate partial success: failures are listed, successes stand,
and re-running the same `start` fills in only what is missing.
A repo that already has the branch is taken into the bundle as a
member instead of being recreated.

The commands are thin windows onto the engine
(`tsrc_ext.engine`).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional, cast

from tsrc_ext.common import (
    TsrcExtError,
    default_owner,
    find_workspace,
    load_workspace_manifest,
    now_iso,
    run_cmd,
    run_git,
)
from tsrc_ext.engine import get_engine

try:
    import ruamel.yaml
except ImportError:  # pragma: no cover
    ruamel = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Ledger (.tsrc/worktrees.yml)
# ---------------------------------------------------------------------------


def ledger_path(workspace: Path) -> Path:
    return workspace / ".tsrc" / "worktrees.yml"


def worktrees_root(workspace: Path) -> Path:
    return workspace / ".tsrc" / "worktrees"


def _empty_ledger() -> dict:
    return {"version": 1, "bundles": [], "worktrees": []}


def load_ledger(workspace: Path) -> dict:
    path = ledger_path(workspace)
    if not path.exists():
        return _empty_ledger()
    if ruamel is None:
        raise TsrcExtError("ruamel.yaml is required to read the ledger")
    yaml = ruamel.yaml.YAML(typ="safe", pure=True)
    data = yaml.load(path.read_text()) or {}
    data.setdefault("version", 1)
    data.setdefault("bundles", [])
    data.setdefault("worktrees", [])
    return data


def save_ledger(workspace: Path, data: dict) -> None:
    if ruamel is None:
        raise TsrcExtError("ruamel.yaml is required to write the ledger")
    path = ledger_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml = ruamel.yaml.YAML(typ="safe", pure=True)
    tmp = path.with_suffix(".yml.tmp")
    with tmp.open("w") as handle:
        yaml.dump(data, handle)
    os.replace(tmp, path)


def find_entry(data: dict, name: str) -> Optional[dict]:
    for entry in data["worktrees"]:
        if entry.get("name") == name:
            # Return the live entry (callers mutate it and rely on
            # identity to drop it); the cast only satisfies mypy.
            return cast(dict, entry)
    return None


def _path_component(name: str) -> str:
    """A ledger name mapped to one directory component."""
    if not name or name in (".", ".."):
        raise TsrcExtError("invalid worktree name: %r" % name)
    if "/" in name or "\\" in name:
        return name.replace("/", "-").replace("\\", "-")
    return name


def _slug(dest: str) -> str:
    return dest.replace("/", "-")


# ---------------------------------------------------------------------------
# Small git helpers
# ---------------------------------------------------------------------------


def _branch_exists(clone: Path, branch: str) -> bool:
    rc, _, _ = run_cmd(
        ["git", "show-ref", "--verify", "--quiet", "refs/heads/" + branch],
        cwd=clone,
    )
    return rc == 0


def _rev_parse(clone: Path, ref: str) -> Optional[str]:
    rc, out, _ = run_cmd(["git", "rev-parse", "--verify", ref], cwd=clone)
    if rc != 0:
        return None
    return out.strip()


def _worktree_paths(clone: Path) -> List[Path]:
    """Paths git reports as worktrees of `clone`, excluding itself."""
    rc, out, _ = run_cmd(["git", "worktree", "list", "--porcelain"], cwd=clone)
    if rc != 0:
        return []
    paths: List[Path] = []
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree ") :].strip())
            if path.resolve() != clone.resolve():
                paths.append(path)
    return paths


def _read_gitdir(path: Path) -> Optional[Path]:
    """The admin directory a worktree's .git file points at."""
    gitfile = path / ".git"
    try:
        content = gitfile.read_text().strip()
    except OSError:
        return None
    if not content.startswith("gitdir:"):
        return None
    return Path(content[len("gitdir:") :].strip())


def _check_dirty(path: Path) -> bool:
    _, out, _ = run_cmd(["git", "status", "--porcelain"], cwd=path)
    return bool(out.strip())


def _ahead_count(path: Path, upstream: str) -> Optional[int]:
    rc, out, _ = run_cmd(
        ["git", "rev-list", "--count", "%s..HEAD" % upstream], cwd=path
    )
    if rc != 0:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def _unpushed_reason(entry: dict, path: Path) -> Optional[str]:  # noqa: C901
    """Why the worktree counts as unpushed, or None when pushed."""
    rc, out, _ = run_cmd(
        [
            "git",
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{upstream}",
        ],
        cwd=path,
    )
    if rc == 0:
        upstream = out.strip()
        ahead = _ahead_count(path, upstream)
        if ahead is None:
            return "cannot compare with upstream %s" % upstream
        if ahead > 0:
            return "%d commit(s) ahead of %s" % (ahead, upstream)
        return None
    # No upstream configured: the work still counts as pushed when
    # a remote branch of the same name points at HEAD (a plain
    # `git push origin HEAD:refs/heads/<branch>` lands there without
    # setting an upstream).
    head = _rev_parse(path, "HEAD")
    branch = entry.get("branch") or ""
    if head and branch:
        rc, remotes, _ = run_cmd(
            ["git", "for-each-ref", "--format=%(refname)", "refs/remotes/"],
            cwd=path,
        )
        if rc == 0:
            for ref in remotes.splitlines():
                if ref.rsplit("/", 1)[-1] == branch:
                    if _rev_parse(path, ref) == head:
                        return None
    base_branch = entry.get("base_branch") or "master"
    if _rev_parse(path, base_branch) is None:
        return "no upstream and base branch %s is not resolvable" % base_branch
    ahead = _ahead_count(path, base_branch)
    if ahead is None:
        return "cannot compare with base branch %s" % base_branch
    if ahead > 0:
        return "%d commit(s) not on base branch %s" % (ahead, base_branch)
    return None


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def _create_plain_worktree(
    clone: Path,
    path: Path,
    branch: str,
    base_ref: str,
    branch_exists: bool,
) -> None:
    if branch_exists:
        run_git(clone, "worktree", "add", str(path), branch)
    else:
        run_git(clone, "worktree", "add", "-b", branch, str(path), base_ref)


def _create_worktree(
    workspace: Path,
    clone: Path,
    dest: str,
    name: str,
    branch: str,
    base_ref: str,
    branch_exists: bool,
    provision: bool,
) -> dict:
    """Create the worktree; returns {"path", "provision", ...}."""
    merged = worktrees_root(workspace) / _path_component(name)
    if merged.exists():
        raise TsrcExtError("worktree path already exists: %s" % merged)
    if provision:
        from tsrc_ext.overlay import provision_overlay

        return provision_overlay(
            workspace, clone, dest, name, branch, base_ref, branch_exists
        )
    _create_plain_worktree(clone, merged, branch, base_ref, branch_exists)
    return {"path": str(merged), "provision": "plain"}


def add_worktree(
    workspace: Path,
    dest: str,
    name: str,
    *,
    branch: Optional[str] = None,
    base: Optional[str] = None,
    provision: bool = False,
    owner: Optional[str] = None,
) -> dict:
    manifest = load_workspace_manifest(workspace)
    repo = manifest.by_dest(dest)
    clone = workspace / dest
    if not clone.is_dir():
        raise TsrcExtError("repo %s is not cloned at %s" % (dest, clone))
    data = load_ledger(workspace)
    if find_entry(data, name) is not None:
        raise TsrcExtError("worktree '%s' is already in the ledger" % name)
    branch = branch or name
    base_ref = base or "HEAD"
    base_sha = _rev_parse(clone, base_ref)
    if base_sha is None:
        raise TsrcExtError("cannot resolve base %s in %s" % (base_ref, clone))
    exists = _branch_exists(clone, branch)
    created = _create_worktree(
        workspace, clone, dest, name, branch, base_sha, exists, provision
    )
    entry = {
        "name": name,
        "bundle": None,
        "repo": dest,
        "branch": branch,
        "path": created["path"],
        "owner": owner or default_owner(),
        "created": now_iso(),
        "base_sha": base_sha,
        "base_branch": repo.branch or "master",
        "provision": created["provision"],
        "held": False,
        "status": "alive",
        "last_checked": now_iso(),
    }
    if created["provision"] == "overlay":
        entry["base"] = created.get("base")
        entry["base_commit"] = created.get("base_commit")
    data["worktrees"].append(entry)
    save_ledger(workspace, data)
    return {"entry": entry}


def _scan_entry(entry: dict) -> str:
    """Liveness of one ledger entry (design section 12.3, minimal)."""
    path = Path(entry["path"])
    if not path.exists():
        return "missing"
    rc, out, _ = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
    if rc != 0:
        return "broken"
    if out.strip() != entry.get("branch"):
        return "broken"
    return "alive"


def list_worktrees(workspace: Path) -> dict:
    manifest = load_workspace_manifest(workspace)
    data = load_ledger(workspace)
    changed = False
    for entry in data["worktrees"]:
        status = _scan_entry(entry)
        if status != entry.get("status"):
            entry["status"] = status
            changed = True
        entry["last_checked"] = now_iso()
        changed = True
    if changed:
        save_ledger(workspace, data)
    known = {Path(e["path"]).resolve() for e in data["worktrees"]}
    unregistered: List[dict] = []
    for repo in manifest.repos:
        clone = workspace / repo.dest
        if not clone.is_dir():
            continue
        for path in _worktree_paths(clone):
            if path.resolve() not in known:
                unregistered.append({"repo": repo.dest, "path": str(path)})
    return {"entries": data["worktrees"], "unregistered": unregistered}


def remove_worktree(  # noqa: C901
    workspace: Path, name: str, *, force: bool = False
) -> dict:
    data = load_ledger(workspace)
    entry = find_entry(data, name)
    if entry is None:
        raise TsrcExtError("no worktree '%s' in the ledger" % name)
    if entry.get("held") and not force:
        raise TsrcExtError(
            "worktree '%s' is held (autohold); use --force to remove it" % name
        )
    path = Path(entry["path"])
    clone = workspace / entry["repo"]
    if path.exists():
        if _check_dirty(path) and not force:
            raise TsrcExtError(
                "worktree '%s' has uncommitted changes; "
                "commit or stash first, or use --force" % name
            )
        reason = _unpushed_reason(entry, path)
        if reason and not force:
            raise TsrcExtError(
                "worktree '%s' has unpushed work (%s); push first, "
                "or use --force" % (name, reason)
            )
        if entry.get("provision") == "overlay":
            # The .git file lives in upper, so after the unmount
            # the merged directory is empty and `git worktree
            # remove` cannot validate it.  Unregister by removing
            # the admin directory git itself points at (read from
            # the .git file while the mount is still up), which is
            # exactly what git's own removal does after deleting
            # the tree.
            gitdir = _read_gitdir(path)
            from tsrc_ext.overlay import teardown_overlay

            teardown_overlay(workspace, name, path)
            if path.exists():
                shutil.rmtree(path)
            if gitdir is not None and gitdir.is_dir():
                shutil.rmtree(gitdir)
        elif path.exists():
            # Plain worktrees go through git's own removal so the
            # registration is cleaned up with the directory.
            if clone.is_dir():
                rc, _, err = run_cmd(
                    ["git", "worktree", "remove", "--force", str(path)],
                    cwd=clone,
                )
                if rc != 0:
                    raise TsrcExtError(
                        "git worktree remove failed for %s: %s" % (path, err.strip())
                    )
            if path.exists():
                shutil.rmtree(path)
    elif clone.is_dir():
        run_git(clone, "worktree", "prune")
    data["worktrees"] = [e for e in data["worktrees"] if e is not entry]
    _drop_empty_bundles(data)
    save_ledger(workspace, data)
    return {"removed": name, "path": str(path)}


def _drop_empty_bundles(data: dict) -> None:
    members = {e.get("bundle") for e in data["worktrees"]}
    data["bundles"] = [b for b in data.get("bundles", []) if b.get("id") in members]


def hold_worktree(workspace: Path, name: str, *, held: bool) -> dict:
    data = load_ledger(workspace)
    entry = find_entry(data, name)
    if entry is None:
        raise TsrcExtError("no worktree '%s' in the ledger" % name)
    entry["held"] = held
    save_ledger(workspace, data)
    return {"name": name, "held": held}


def prune_ledger(workspace: Path) -> dict:
    """Drop ledger entries whose directory is gone (design 12.3)."""
    data = load_ledger(workspace)
    removed: List[str] = []
    kept: List[dict] = []
    for entry in data["worktrees"]:
        if Path(entry["path"]).exists():
            kept.append(entry)
            continue
        clone = workspace / entry["repo"]
        if clone.is_dir():
            run_cmd(["git", "worktree", "prune"], cwd=clone)
        removed.append(entry["name"])
    data["worktrees"] = kept
    _drop_empty_bundles(data)
    save_ledger(workspace, data)
    return {"removed": removed}


def start_bundle(  # noqa: C901
    workspace: Path,
    branch: str,
    dests: List[str],
    *,
    provision: bool = False,
    fetch: bool = False,
    owner: Optional[str] = None,
) -> dict:
    """Cut one branch across a bundle of repos (design section 6.3)."""
    manifest = load_workspace_manifest(workspace)
    repos = [manifest.by_dest(dest) for dest in dests]
    data = load_ledger(workspace)
    bundle = next(
        (b for b in data.get("bundles", []) if b.get("id") == branch),
        None,
    )
    if bundle is None:
        bundle = {
            "id": branch,
            "created": now_iso(),
            "repos": [],
        }
        data.setdefault("bundles", []).append(bundle)
    results: List[dict] = []
    for repo in repos:
        result: dict = {
            "repo": repo.dest,
            "branch": branch,
            "outcome": "",
            "detail": "",
        }
        results.append(result)
        clone = workspace / repo.dest
        if not clone.is_dir():
            result["outcome"] = "failed"
            result["detail"] = "not cloned at %s" % clone
            continue
        base_branch = repo.branch or "master"
        if fetch:
            rc, _, err = run_cmd(["git", "fetch", "origin"], cwd=clone)
            if rc != 0:
                result["outcome"] = "failed"
                result["detail"] = "fetch failed: %s" % err.strip()
                continue
        base_sha = _rev_parse(clone, base_branch)
        if base_sha is None:
            result["outcome"] = "failed"
            result["detail"] = "cannot resolve base branch %s" % base_branch
            continue
        name = "%s--%s" % (branch, _slug(repo.dest))
        stale_note = ""
        existing = find_entry(data, name)
        if existing is not None:
            if Path(existing["path"]).exists():
                result["outcome"] = "already"
                result["detail"] = "worktree %s already provisioned" % name
                if repo.dest not in bundle["repos"]:
                    bundle["repos"].append(repo.dest)
                continue
            data["worktrees"] = [e for e in data["worktrees"] if e is not existing]
            stale_note = "stale ledger entry dropped; recreating. "
        exists = _branch_exists(clone, branch)
        base_differs = False
        if exists:
            tip = _rev_parse(clone, branch)
            base_differs = tip is not None and tip != base_sha
        else:
            rc, _, err = run_cmd(["git", "branch", branch, base_sha], cwd=clone)
            if rc != 0:
                result["outcome"] = "failed"
                result["detail"] = "cannot create branch: %s" % err.strip()
                continue
        try:
            created = _create_worktree(
                workspace,
                clone,
                repo.dest,
                name,
                branch,
                base_sha,
                True,
                provision,
            )
        except TsrcExtError as exc:
            result["outcome"] = "failed"
            result["detail"] = stale_note + str(exc)
            if not exists:
                # Roll the branch back: a failed start must not
                # leave a branch behind with no worktree.
                run_cmd(["git", "branch", "-D", branch], cwd=clone)
            continue
        entry = {
            "name": name,
            "bundle": branch,
            "repo": repo.dest,
            "branch": branch,
            "path": created["path"],
            "owner": owner or default_owner(),
            "created": now_iso(),
            "base_sha": base_sha,
            "base_branch": base_branch,
            "provision": created["provision"],
            "held": False,
            "status": "alive",
            "last_checked": now_iso(),
        }
        if created["provision"] == "overlay":
            entry["base"] = created.get("base")
            entry["base_commit"] = created.get("base_commit")
        data["worktrees"].append(entry)
        if repo.dest not in bundle["repos"]:
            bundle["repos"].append(repo.dest)
        if exists:
            result["outcome"] = "taken-in"
            suffix = ""
            if base_differs:
                suffix = " (base differs: branch is not at %s)" % base_branch
            result["detail"] = (
                stale_note + "existing branch taken into the bundle" + suffix
            )
        else:
            result["outcome"] = "created"
            result["detail"] = stale_note + "branch cut at %s" % base_sha[:12]
    save_ledger(workspace, data)
    failed = sum(1 for r in results if r["outcome"] == "failed")
    return {
        "branch": branch,
        "bundle": branch,
        "results": results,
        "failed": failed,
    }


# ---------------------------------------------------------------------------
# CLI (window onto the engine)
# ---------------------------------------------------------------------------


def _add_workspace_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-w",
        "--workspace",
        help="workspace path",
        type=Path,
        dest="workspace_path",
    )


def _cmd_add(args: argparse.Namespace) -> int:
    engine = get_engine()
    workspace = find_workspace(args.workspace_path)
    result = engine.worktree_add(
        workspace,
        args.repo,
        args.name,
        branch=args.branch,
        base=args.base,
        provision=args.provision,
        owner=args.owner,
    )
    entry = result["entry"]
    print(
        "%s: %s (%s, %s)"
        % (entry["name"], entry["path"], entry["branch"], entry["provision"])
    )
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    engine = get_engine()
    workspace = find_workspace(args.workspace_path)
    result = engine.worktree_list(workspace)
    entries = result["entries"]
    if not entries:
        print("(no worktrees in the ledger)")
    for entry in entries:
        print(
            "%s%s  %s  %s  %s  %s  %s"
            % (
                entry["name"],
                " [held]" if entry.get("held") else "",
                entry["repo"],
                entry["branch"],
                entry.get("bundle") or "-",
                entry["provision"],
                entry.get("status", "?"),
            )
        )
    for item in result["unregistered"]:
        print("unregistered: %s (%s)" % (item["path"], item["repo"]))
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    engine = get_engine()
    workspace = find_workspace(args.workspace_path)
    result = engine.worktree_remove(workspace, args.name, force=args.force)
    print("removed %s" % result["removed"])
    return 0


def _cmd_hold(args: argparse.Namespace, held: bool) -> int:
    engine = get_engine()
    workspace = find_workspace(args.workspace_path)
    result = engine.worktree_hold(workspace, args.name, held=held)
    print("%s: %s" % (result["name"], "held" if held else "unheld"))
    return 0


def _cmd_prune(args: argparse.Namespace) -> int:
    engine = get_engine()
    workspace = find_workspace(args.workspace_path)
    result = engine.worktree_prune(workspace)
    for name in result["removed"]:
        print("pruned %s" % name)
    if not result["removed"]:
        print("(nothing to prune)")
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    workspace = find_workspace(args.workspace_path)
    manifest = load_workspace_manifest(workspace)
    dests: List[str] = []
    if args.groups:
        for name in args.groups:
            if name not in manifest.groups:
                raise TsrcExtError(
                    "unknown group '%s' (known groups: %s)"
                    % (name, ", ".join(sorted(manifest.groups)) or "(none)")
                )
            for dest in manifest.groups[name]:
                if dest not in dests:
                    dests.append(dest)
    for dest in args.dests:
        manifest.by_dest(dest)  # raises for unknown dests
        if dest not in dests:
            dests.append(dest)
    if not dests:
        raise TsrcExtError("start needs a target: --group <g> or repo dest(s)")
    engine = get_engine()
    result = engine.worktree_start(
        workspace,
        args.branch,
        dests,
        provision=args.provision,
        fetch=args.fetch,
        owner=args.owner,
    )
    for item in result["results"]:
        print(
            "%s: %s%s"
            % (
                item["repo"],
                item["outcome"],
                (" (%s)" % item["detail"]) if item["detail"] else "",
            )
        )
    print(
        "bundle %s: %d repo(s), %d failed"
        % (result["bundle"], len(result["results"]), result["failed"])
    )
    return 1 if result["failed"] else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tsrc-worktree",
        description="Worktree lifecycle and bundle start.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="add a worktree from a repo clone")
    _add_workspace_arg(p_add)
    p_add.add_argument("repo", help="repository dest, e.g. foo/bar")
    p_add.add_argument("name", help="worktree name")
    p_add.add_argument("--branch", help="branch to check out (default: name)")
    p_add.add_argument("--base", help="base ref for a new branch (default: HEAD)")
    p_add.add_argument(
        "--provision",
        action="store_true",
        help="provision from the prebuilt base via overlayfs",
    )
    p_add.add_argument("--owner", help="owner recorded in the ledger")
    p_add.set_defaults(run=_cmd_add)

    p_list = sub.add_parser("list", help="list ledger worktrees (with hold marks)")
    _add_workspace_arg(p_list)
    p_list.set_defaults(run=_cmd_list)

    p_remove = sub.add_parser("remove", help="close a worktree")
    _add_workspace_arg(p_remove)
    p_remove.add_argument("name", help="worktree name")
    p_remove.add_argument(
        "--force",
        action="store_true",
        help="remove despite uncommitted/unpushed work or a hold",
    )
    p_remove.set_defaults(run=_cmd_remove)

    p_hold = sub.add_parser("hold", help="mark a worktree held (autohold)")
    _add_workspace_arg(p_hold)
    p_hold.add_argument("name", help="worktree name")
    p_hold.set_defaults(run=lambda a: _cmd_hold(a, True))

    p_unhold = sub.add_parser("unhold", help="clear a worktree hold")
    _add_workspace_arg(p_unhold)
    p_unhold.add_argument("name", help="worktree name")
    p_unhold.set_defaults(run=lambda a: _cmd_hold(a, False))

    p_prune = sub.add_parser(
        "prune", help="drop ledger entries whose directory is gone"
    )
    _add_workspace_arg(p_prune)
    p_prune.set_defaults(run=_cmd_prune)

    p_start = sub.add_parser("start", help="cut one branch across a bundle of repos")
    _add_workspace_arg(p_start)
    p_start.add_argument("branch", help="branch name to cut")
    p_start.add_argument(
        "-g",
        "--group",
        "--groups",
        nargs="+",
        dest="groups",
        help="groups to use",
    )
    p_start.add_argument(
        "dests",
        nargs="*",
        help="repository dest(s), e.g. foo/bar",
    )
    p_start.add_argument(
        "--provision",
        action="store_true",
        help="provision worktrees from prebuilt bases via overlayfs",
    )
    p_start.add_argument(
        "--fetch",
        action="store_true",
        help="fetch before resolving each repo's base branch tip",
    )
    p_start.add_argument("--owner", help="owner recorded in the ledger")
    p_start.set_defaults(run=_cmd_start)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.run(args))
    except TsrcExtError as exc:
        print("tsrc-worktree: error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
