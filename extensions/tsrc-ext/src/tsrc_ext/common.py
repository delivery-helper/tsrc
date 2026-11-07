"""Shared helpers for the tsrc extension commands.

Workspace discovery and manifest reading follow tsrc's own rules:
the workspace is the directory holding `.tsrc/config.yml`, found by
walking up from the current directory, and the manifest is read with
tsrc's own `load_manifest` whenever the `tsrc` package can be
imported.  When it cannot (the extension commands are usable
stand-alone), a minimal reader for the same `manifest.yml` file is
used instead, so the schema interpretation can only drift in the
fields the commands actually need (`dest`, `branch`, `url`, `groups`).
"""

from __future__ import annotations

import getpass
import os
import re
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import ruamel.yaml
except ImportError:  # pragma: no cover - ruamel.yaml is a declared dependency
    ruamel = None  # type: ignore[assignment]


class TsrcExtError(Exception):
    """An expected failure: print the message and exit 1."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_owner() -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USER", "unknown")
    try:
        host = socket.gethostname()
    except Exception:
        host = "unknown"
    return "%s@%s" % (user, host)


def run_cmd(
    argv: List[str],
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[int, str, str]:
    """Run a command, capturing stdout and stderr as text."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        argv,
        cwd=str(cwd) if cwd is not None else None,
        env=full_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def git_cmd(*args: str) -> List[str]:
    """Build a git command line.

    Mirrors tsrc's own convention: when running under the tsrc test
    suite (TSRC_TESTING=true) the file:// protocol is allowed, so
    local test remotes work.
    """
    cmd = ["git"]
    if os.environ.get("TSRC_TESTING"):
        cmd += ["-c", "protocol.file.allow=always"]
    cmd += list(args)
    return cmd


def run_git(repo: Path, *args: str, check: bool = True) -> Tuple[int, str, str]:
    rc, out, err = run_cmd(git_cmd(*args), cwd=repo)
    if check and rc != 0:
        raise TsrcExtError(
            "git %s failed in %s (exit %d): %s"
            % (" ".join(args), repo, rc, (err or out).strip())
        )
    return rc, out, err


def git_output(repo: Path, *args: str) -> str:
    _, out, _ = run_git(repo, *args)
    return out.strip()


def find_workspace(explicit: Optional[Path] = None) -> Path:
    """Return the workspace root: `-w` wins, else walk up for `.tsrc`."""
    if explicit is not None:
        path = Path(explicit).resolve()
        if not (path / ".tsrc").is_dir():
            raise TsrcExtError("no .tsrc directory in workspace path: %s" % path)
        return path
    head = Path(os.getcwd()).resolve()
    while True:
        if (head / ".tsrc").is_dir():
            return head
        parent = head.parent
        if parent == head:
            raise TsrcExtError(
                "could not find current workspace "
                "(no .tsrc directory walking up from %s)" % os.getcwd()
            )
        head = parent


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


@dataclass
class RepoInfo:
    dest: str
    branch: Optional[str] = None
    url: Optional[str] = None

    @property
    def clone_path(self) -> Optional[Path]:
        return None  # filled in by resolve step; kept simple on purpose


@dataclass
class ManifestData:
    repos: List[RepoInfo] = field(default_factory=list)
    groups: Dict[str, List[str]] = field(default_factory=dict)

    def dests(self) -> List[str]:
        return [repo.dest for repo in self.repos]

    def by_dest(self, dest: str) -> RepoInfo:
        for repo in self.repos:
            if repo.dest == dest:
                return repo
        raise TsrcExtError("no repo with dest '%s' in manifest" % dest)


def _manifest_path(workspace: Path) -> Path:
    return workspace / ".tsrc" / "manifest" / "manifest.yml"


def _load_manifest_minimal(workspace: Path) -> ManifestData:
    """Minimal manifest reader, used when `tsrc` cannot be imported.

    Reads the same `.tsrc/manifest/manifest.yml` file tsrc itself
    maintains, but only the fields these commands need.
    """
    path = _manifest_path(workspace)
    if not path.exists():
        raise TsrcExtError("no local manifest at %s (run `tsrc init` first)" % path)
    if ruamel is None:
        raise TsrcExtError("ruamel.yaml is required to read the manifest")
    yaml = ruamel.yaml.YAML(typ="safe", pure=True)
    parsed = yaml.load(path.read_text()) or {}
    data = ManifestData()
    for entry in parsed.get("repos") or []:
        data.repos.append(
            RepoInfo(
                dest=entry["dest"],
                branch=entry.get("branch"),
                url=entry.get("url"),
            )
        )
    for name, group in (parsed.get("groups") or {}).items():
        data.groups[name] = list(group.get("repos") or [])
    return data


def load_workspace_manifest(workspace: Path) -> ManifestData:
    """Load the workspace manifest, preferring tsrc's own loader."""
    path = _manifest_path(workspace)
    try:
        from tsrc.manifest import load_manifest
    except ImportError:
        return _load_manifest_minimal(workspace)
    if not path.exists():
        raise TsrcExtError("no local manifest at %s (run `tsrc init` first)" % path)
    manifest = load_manifest(path)
    data = ManifestData()
    for repo in manifest.get_repos(all_=True):
        data.repos.append(
            RepoInfo(
                dest=repo.dest,
                branch=repo.branch,
                url=repo.remotes[0].url if repo.remotes else None,
            )
        )
    if manifest.group_list and manifest.group_list.groups:
        for name, group in manifest.group_list.groups.items():
            data.groups[name] = list(group.elements)
    return data


# ---------------------------------------------------------------------------
# Repo selection (mirrors tsrc's add_repos_selection_args)
# ---------------------------------------------------------------------------


@dataclass
class Selection:
    groups: Optional[List[str]] = None
    all_cloned: bool = False
    include_regex: Optional[str] = None
    exclude_regex: Optional[str] = None
    dests: List[str] = field(default_factory=list)


def resolve_repos(  # noqa: C901
    manifest: ManifestData,
    workspace: Path,
    selection: Selection,
) -> List[RepoInfo]:
    """Resolve the repos a command should operate on.

    Same shape as tsrc's `resolve_repos`: groups win over
    `--all-cloned`, explicit dests win over both, regexes filter
    afterwards.  With no selector at all, every cloned repo in the
    manifest is used.
    """
    if selection.dests:
        chosen = [manifest.by_dest(dest) for dest in selection.dests]
    elif selection.groups:
        dests: List[str] = []
        for name in selection.groups:
            if name not in manifest.groups:
                raise TsrcExtError(
                    "unknown group '%s' (known groups: %s)"
                    % (name, ", ".join(sorted(manifest.groups)) or "(none)")
                )
            for dest in manifest.groups[name]:
                if dest not in dests:
                    dests.append(dest)
        chosen = [manifest.by_dest(dest) for dest in dests]
    elif selection.all_cloned:
        chosen = list(manifest.repos)
    else:
        chosen = list(manifest.repos)
    if not selection.dests and not selection.groups:
        # Default and --all-cloned both mean "cloned repos only".
        chosen = [repo for repo in chosen if (workspace / repo.dest).exists()]
    if selection.include_regex:
        pattern = re.compile(selection.include_regex)
        chosen = [repo for repo in chosen if pattern.search(repo.dest)]
    if selection.exclude_regex:
        pattern = re.compile(selection.exclude_regex)
        chosen = [repo for repo in chosen if not pattern.search(repo.dest)]
    return chosen
