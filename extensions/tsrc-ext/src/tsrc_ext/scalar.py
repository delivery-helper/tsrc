"""`tsrc-scalar`: apply and inspect Scalar registration per repo.

Implements design section 6.1.  The backend resolution order is
design section 4:

1. `git scalar` (the subcommand, when git resolves it),
2. a stand-alone `scalar` binary on PATH,
3. on macOS, the Xcode-bundled `scalar`,
4. the decomposed form: the config-key set captured from a real
   `scalar register` (Apple Git 2.54, see
   `tsrc-scalar-trial-20261107.md`) applied with `git config`,
   plus `git maintenance register` and the global `scalar.repo` /
   `maintenance.repo` entries.

The decomposed form is what runs on WSL, where no `scalar`
binary exists at all.  It does not reproduce the launchd
registration or the fsmonitor daemon a real `scalar register`
starts on macOS (design section 4, unverified equivalence);
`git maintenance` itself still schedules through the platform
mechanism git knows about.

The commands are thin windows onto the engine
(`tsrc_ext.engine`): argument parsing and printing live here,
the work lives in the operation functions below, reached through
`get_engine()`.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from tsrc_ext.common import (
    Selection,
    TsrcExtError,
    find_workspace,
    load_workspace_manifest,
    resolve_repos,
    run_cmd,
    run_git,
)
from tsrc_ext.engine import get_engine

# The 27 local config keys a real `scalar register` sets, captured
# from Apple Git 2.54 (see tsrc-scalar-trial-20261107.md).  A
# different git version may set a different set; `status` reports
# the match count so drift is visible instead of silent.
SCALAR_CONFIG_KEYS: Dict[str, str] = {
    "core.autocrlf": "false",
    "core.safecrlf": "false",
    "core.untrackedcache": "true",
    "core.fsmonitor": "true",
    "index.skiphash": "true",
    "index.threads": "true",
    "index.version": "4",
    "commitgraph.changedpaths": "true",
    "commitgraph.generationversion": "1",
    "fetch.writecommitgraph": "false",
    "gc.auto": "0",
    "receive.autogc": "false",
    "maintenance.auto": "false",
    "maintenance.strategy": "incremental",
    "fetch.showforcedupdates": "false",
    "fetch.unpacklimit": "1",
    "pack.usebitmaps": "false",
    "pack.usepathwalk": "true",
    "status.aheadbehind": "false",
    "log.excludedecoration": "refs/prefetch/*",
    "merge.renames": "true",
    "merge.stat": "false",
    "am.keepcr": "true",
    "gui.gcwarning": "false",
    "feature.experimental": "false",
    "feature.manyfiles": "false",
    "credential.https://dev.azure.com.usehttppath": "true",
}


@dataclass
class Backend:
    kind: str  # git-scalar | scalar | xcode | decomposed
    argv: List[str]  # command prefix; [] for the decomposed form
    description: str


def _git_scalar_works() -> bool:
    rc, _, _ = run_cmd(["git", "scalar", "--version"])
    return rc == 0


def _xcode_scalar_path() -> Optional[Path]:
    """Xcode-bundled scalar, via xcode-select or the fixed path."""
    rc, out, _ = run_cmd(["xcode-select", "-p"])
    if rc == 0:
        candidate = Path(out.strip()) / "usr" / "bin" / "scalar"
        if candidate.is_file():
            return candidate
    fixed = Path("/Applications/Xcode.app/Contents/Developer/usr/bin/scalar")
    if fixed.is_file():
        return fixed
    return None


def resolve_scalar(  # noqa: C901
    backend: Optional[str] = None,
) -> Backend:
    """Resolve the Scalar backend, in the design section 4 order."""
    if backend and backend != "auto":
        if backend == "git-scalar":
            return Backend(
                "git-scalar", ["git", "scalar"], "git scalar subcommand (forced)"
            )
        if backend == "scalar":
            path = shutil.which("scalar")
            if not path:
                raise TsrcExtError("no stand-alone `scalar` on PATH")
            return Backend("scalar", [path], "stand-alone scalar (forced)")
        if backend == "xcode":
            xcode_path = _xcode_scalar_path()
            if not xcode_path:
                raise TsrcExtError("no Xcode-bundled scalar found")
            return Backend("xcode", [str(xcode_path)], "Xcode-bundled scalar (forced)")
        if backend == "decomposed":
            return Backend("decomposed", [], "decomposed form (forced)")
        raise TsrcExtError("unknown backend '%s'" % backend)
    if _git_scalar_works():
        return Backend("git-scalar", ["git", "scalar"], "git scalar subcommand")
    path = shutil.which("scalar")
    if path:
        return Backend("scalar", [path], "stand-alone scalar on PATH")
    if sys.platform == "darwin":
        xcode = _xcode_scalar_path()
        if xcode is not None:
            return Backend("xcode", [str(xcode)], "Xcode-bundled scalar")
    return Backend(
        "decomposed",
        [],
        "decomposed form (no scalar binary found: config keys + "
        "git maintenance register)",
    )


def ensure_git_repo(path: Path) -> Path:
    path = path.resolve()
    rc, _, _ = run_cmd(["git", "rev-parse", "--git-dir"], cwd=path)
    if rc != 0:
        raise TsrcExtError("not a git repository: %s" % path)
    return path


def _global_values(key: str) -> List[str]:
    rc, out, _ = run_cmd(["git", "config", "--global", "--get-all", key])
    if rc != 0:
        return []
    return [line for line in out.splitlines() if line]


def _global_add_if_missing(key: str, value: str) -> bool:
    if value in _global_values(key):
        return False
    rc, _, err = run_cmd(["git", "config", "--global", "--add", key, value])
    if rc != 0:
        raise TsrcExtError(
            "git config --global --add %s failed: %s" % (key, err.strip())
        )
    return True


def _global_remove(key: str, value: str) -> bool:
    values = _global_values(key)
    if value not in values:
        return False
    remaining = [v for v in values if v != value]
    run_cmd(["git", "config", "--global", "--unset-all", key])
    for kept in remaining:
        _global_add_if_missing(key, kept)
    return True


def _local_key_state(repo: Path) -> Dict[str, Optional[str]]:
    """Current value of each Scalar key in the repo (None = unset)."""
    state: Dict[str, Optional[str]] = {}
    for key in SCALAR_CONFIG_KEYS:
        rc, out, _ = run_cmd(["git", "config", "--get", key], cwd=repo)
        state[key] = out.strip() if rc == 0 else None
    return state


def apply_scalar(repo_path: Path, backend: Optional[str] = None) -> dict:
    """Apply Scalar registration to one repo. Returns a result dict."""
    repo = ensure_git_repo(repo_path)
    resolved = resolve_scalar(backend)
    if resolved.kind != "decomposed":
        rc, out, err = run_cmd(resolved.argv + ["register"], cwd=repo)
        if rc != 0:
            raise TsrcExtError(
                "%s register failed in %s (exit %d): %s"
                % (
                    " ".join(resolved.argv),
                    repo,
                    rc,
                    (err or out).strip(),
                )
            )
        return {
            "repo": str(repo),
            "method": resolved.kind,
            "description": resolved.description,
            "keys_set": 0,
        }
    # Decomposed form: set the captured key set, register with
    # git maintenance, and mirror scalar register's global entries.
    for key, value in SCALAR_CONFIG_KEYS.items():
        run_git(repo, "config", key, value)
    run_git(repo, "maintenance", "register")
    _global_add_if_missing("scalar.repo", str(repo))
    _global_add_if_missing("maintenance.repo", str(repo))
    return {
        "repo": str(repo),
        "method": "decomposed",
        "description": resolved.description,
        "keys_set": len(SCALAR_CONFIG_KEYS),
    }


def scalar_status(repo_path: Path) -> dict:
    """Report the registration state of one repo."""
    repo = ensure_git_repo(repo_path)
    state = _local_key_state(repo)
    matched = sum(
        1 for key, want in SCALAR_CONFIG_KEYS.items() if state.get(key) == want
    )
    mismatched = [
        key
        for key, want in SCALAR_CONFIG_KEYS.items()
        if state.get(key) not in (None, want)
    ]
    missing = [
        key for key, want in SCALAR_CONFIG_KEYS.items() if state.get(key) is None
    ]
    global_scalar = str(repo) in _global_values("scalar.repo")
    global_maintenance = str(repo) in _global_values("maintenance.repo")
    if global_scalar and global_maintenance and matched == len(SCALAR_CONFIG_KEYS):
        overall = "registered"
    elif global_scalar or global_maintenance or matched > 0:
        overall = "partial"
    else:
        overall = "not-registered"
    return {
        "repo": str(repo),
        "state": overall,
        "global_scalar_repo": global_scalar,
        "global_maintenance_repo": global_maintenance,
        "keys_matched": matched,
        "keys_total": len(SCALAR_CONFIG_KEYS),
        "keys_missing": missing,
        "keys_mismatched": mismatched,
        "backend": resolve_scalar().kind,
    }


def run_maintenance(repo_path: Path) -> dict:
    repo = ensure_git_repo(repo_path)
    rc, out, err = run_cmd(["git", "maintenance", "run"], cwd=repo)
    if rc != 0:
        raise TsrcExtError(
            "git maintenance run failed in %s (exit %d): %s"
            % (repo, rc, (err or out).strip())
        )
    return {"repo": str(repo), "output": (out or err).strip()}


def unregister_scalar(
    repo_path: Path, *, full: bool = False, backend: Optional[str] = None
) -> dict:
    """Unregister one repo.

    `scalar unregister` alone leaves the 27 local keys in place
    (observed in the trial); pass `full=True` for the complete
    removal (the "full unregister" the design leaves to the
    implementation stage).
    """
    repo = ensure_git_repo(repo_path)
    resolved = resolve_scalar(backend)
    if resolved.kind != "decomposed":
        rc, out, err = run_cmd(resolved.argv + ["unregister"], cwd=repo)
        if rc != 0:
            raise TsrcExtError(
                "%s unregister failed in %s (exit %d): %s"
                % (
                    " ".join(resolved.argv),
                    repo,
                    rc,
                    (err or out).strip(),
                )
            )
    else:
        _global_remove("scalar.repo", str(repo))
        _global_remove("maintenance.repo", str(repo))
    removed_keys: List[str] = []
    if full:
        for key in SCALAR_CONFIG_KEYS:
            rc, _, _ = run_cmd(["git", "config", "--unset", key], cwd=repo)
            if rc == 0:
                removed_keys.append(key)
    return {
        "repo": str(repo),
        "method": resolved.kind,
        "full": full,
        "keys_removed": removed_keys,
    }


# ---------------------------------------------------------------------------
# CLI (window onto the engine)
# ---------------------------------------------------------------------------


def _add_selection_args(parser: argparse.ArgumentParser) -> None:
    """Repo selection, same shape as tsrc's add_repos_selection_args."""
    parser.add_argument(
        "-w",
        "--workspace",
        help="workspace path",
        type=Path,
        dest="workspace_path",
    )
    parser.add_argument(
        "-g",
        "--group",
        "--groups",
        nargs="+",
        dest="groups",
        help="groups to use",
    )
    parser.add_argument(
        "--all-cloned",
        action="store_true",
        dest="all_cloned",
        help="run on all cloned repos",
    )
    parser.add_argument(
        "-i",
        dest="include_regex",
        help="Include only repositories matching the regex",
    )
    parser.add_argument(
        "-e",
        dest="exclude_regex",
        help="Exclude repositories matching the regex",
    )
    parser.add_argument(
        "dests",
        nargs="*",
        help="repository dest(s), e.g. foo/bar",
    )


def _resolve_targets(args: argparse.Namespace) -> tuple:
    workspace = find_workspace(args.workspace_path)
    manifest = load_workspace_manifest(workspace)
    selection = Selection(
        groups=args.groups,
        all_cloned=args.all_cloned,
        include_regex=args.include_regex,
        exclude_regex=args.exclude_regex,
        dests=list(args.dests),
    )
    repos = resolve_repos(manifest, workspace, selection)
    if not repos:
        raise TsrcExtError("no repositories selected")
    return workspace, repos


def _cmd_apply(args: argparse.Namespace) -> int:
    _, repos = _resolve_targets(args)
    engine = get_engine()
    workspace = find_workspace(args.workspace_path)
    failed = 0
    for repo in repos:
        repo_path = workspace / repo.dest
        try:
            result = engine.scalar_apply(repo_path, backend=args.backend)
            print(
                "%s: applied (%s, %d keys)"
                % (repo.dest, result["method"], result["keys_set"])
            )
        except TsrcExtError as exc:
            print("%s: FAILED: %s" % (repo.dest, exc), file=sys.stderr)
            failed += 1
    return 1 if failed else 0


def _cmd_status(args: argparse.Namespace) -> int:
    _, repos = _resolve_targets(args)
    engine = get_engine()
    workspace = find_workspace(args.workspace_path)
    counts = {"registered": 0, "partial": 0, "not-registered": 0}
    for repo in repos:
        result = engine.scalar_status(workspace / repo.dest)
        counts[result["state"]] += 1
        print(
            "%s: %s (global scalar.repo=%s maintenance.repo=%s, "
            "local keys %d/%d)"
            % (
                repo.dest,
                result["state"],
                "yes" if result["global_scalar_repo"] else "no",
                "yes" if result["global_maintenance_repo"] else "no",
                result["keys_matched"],
                result["keys_total"],
            )
        )
        if result["keys_mismatched"]:
            print("  mismatched keys: %s" % ", ".join(result["keys_mismatched"]))
    print(
        "registered=%d partial=%d not-registered=%d (backend: %s)"
        % (
            counts["registered"],
            counts["partial"],
            counts["not-registered"],
            resolve_scalar().kind,
        )
    )
    return 0


def _cmd_maintenance(args: argparse.Namespace) -> int:
    _, repos = _resolve_targets(args)
    engine = get_engine()
    workspace = find_workspace(args.workspace_path)
    failed = 0
    for repo in repos:
        try:
            result = engine.scalar_maintenance(workspace / repo.dest)
            print("%s: maintenance run ok" % repo.dest)
            if result["output"]:
                print("  %s" % result["output"])
        except TsrcExtError as exc:
            print("%s: FAILED: %s" % (repo.dest, exc), file=sys.stderr)
            failed += 1
    return 1 if failed else 0


def _cmd_unregister(args: argparse.Namespace) -> int:
    _, repos = _resolve_targets(args)
    engine = get_engine()
    workspace = find_workspace(args.workspace_path)
    failed = 0
    for repo in repos:
        try:
            result = engine.scalar_unregister(
                workspace / repo.dest,
                full=args.full,
                backend=args.backend,
            )
            print(
                "%s: unregistered (%s%s)"
                % (
                    repo.dest,
                    result["method"],
                    (
                        ", %d local keys removed" % len(result["keys_removed"])
                        if result["full"]
                        else ""
                    ),
                )
            )
        except TsrcExtError as exc:
            print("%s: FAILED: %s" % (repo.dest, exc), file=sys.stderr)
            failed += 1
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tsrc-scalar",
        description="Apply and inspect Scalar registration per repo.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_apply = sub.add_parser(
        "apply", help="apply Scalar registration to selected repos"
    )
    _add_selection_args(p_apply)
    p_apply.add_argument(
        "--backend",
        choices=["auto", "git-scalar", "scalar", "xcode", "decomposed"],
        default="auto",
        help="force a backend (default: auto, design section 4 order)",
    )
    p_apply.set_defaults(run=_cmd_apply)

    p_status = sub.add_parser("status", help="show which repos are Scalar-registered")
    _add_selection_args(p_status)
    p_status.set_defaults(run=_cmd_status)

    p_maint = sub.add_parser(
        "maintenance", help="run `git maintenance run` for selected repos"
    )
    _add_selection_args(p_maint)
    p_maint.set_defaults(run=_cmd_maintenance)

    p_unreg = sub.add_parser(
        "unregister", help="remove Scalar registration from selected repos"
    )
    _add_selection_args(p_unreg)
    p_unreg.add_argument(
        "--full",
        action="store_true",
        help="also unset the local config keys (scalar unregister "
        "alone leaves them in place)",
    )
    p_unreg.add_argument(
        "--backend",
        choices=["auto", "git-scalar", "scalar", "xcode", "decomposed"],
        default="auto",
    )
    p_unreg.set_defaults(run=_cmd_unregister)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.run(args))
    except TsrcExtError as exc:
        print("tsrc-scalar: error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
