"""Fixtures for the tsrc-ext tests.

Run from the fork root: `.venv/bin/python -m pytest
extensions/tsrc-ext/tests -q`.  The `tsrc` package is imported
from the checkout itself and `tsrc_ext` from `src/`; nothing here
depends on either package being pip-installed.
"""

import os
import sys
from pathlib import Path
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = REPO_ROOT / "extensions" / "tsrc-ext" / "src"
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("TSRC_TESTING", "true")
os.environ.setdefault("GIT_AUTHOR_NAME", "Tasty Test")
os.environ.setdefault("GIT_AUTHOR_EMAIL", "test@tsrc.io")
os.environ.setdefault("GIT_COMMITTER_NAME", "Tasty Test")
os.environ.setdefault("GIT_COMMITTER_EMAIL", "test@tsrc.io")

from tsrc.test.helpers.cli import CLI  # noqa: E402
from tsrc.test.helpers.git_server import GitServer  # noqa: E402


@pytest.fixture()
def tmp_path(tmpdir):  # type: ignore[no-untyped-def]
    return Path(tmpdir.strpath)


@pytest.fixture
def workspace_path(tmp_path: Path) -> Path:
    res = tmp_path / "work"
    res.mkdir()
    return res


@pytest.fixture
def git_server(tmp_path: Path) -> GitServer:
    return GitServer(tmp_path)


@pytest.fixture
def tsrc_cli(workspace_path: Path, monkeypatch: pytest.MonkeyPatch) -> CLI:
    monkeypatch.chdir(workspace_path)
    return CLI()


@pytest.fixture
def isolated_gitconfig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate HOME and the global gitconfig from the real user.

    Scalar's decomposed apply writes global `scalar.repo` /
    `maintenance.repo` entries; those must never touch ~/.gitconfig.
    """
    home = tmp_path / "home"
    home.mkdir()
    global_cfg = tmp_path / "gitconfig-global"
    global_cfg.write_text("")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_cfg))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    return {"home": home, "global_cfg": global_cfg}


def make_workspace(
    git_server: GitServer,
    tsrc_cli: CLI,
    repos: List[str],
    groups=None,
) -> str:
    """Create bare repos, an optional group, and `tsrc init` them.

    Returns the manifest URL.
    """
    for name in repos:
        git_server.add_repo(name)
        git_server.push_file(name, "README.txt", contents="hello\n")
    for group_name, members in (groups or {}).items():
        git_server.add_group(group_name, members, do_add_repos=False)
    manifest_url = git_server.manifest_url
    tsrc_cli.run("init", manifest_url)
    return manifest_url
