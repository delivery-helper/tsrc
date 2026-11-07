# tsrc-ext — self-contained tsrc extension commands

`tsrc-scalar` and `tsrc-worktree`, the two commands of the tsrc
fork's stage 2 (design memo `tsrc-fork-design-20261107.md`,
sections 6, 12, 13). They are reached through tsrc's
external-subcommand discovery (`tsrc scalar ...` hands over to
`tsrc-scalar`, `tsrc worktree ...` to `tsrc-worktree`), and they
also run stand-alone.

## Install

From the fork checkout:

```sh
pip install -e .            # tsrc itself (for load_manifest)
pip install -e extensions/tsrc-ext
```

Without tsrc installed the commands still work: the manifest is
then read by a minimal reader for the same `manifest.yml`
(`dest` / `branch` / `url` / `groups` only).

## tsrc-scalar

```sh
tsrc scalar apply                    # all cloned repos
tsrc scalar apply -g daiko          # one group
tsrc scalar apply foo/bar -i app    # explicit dests + regex
tsrc scalar status                   # registered / partial / not-registered
tsrc scalar maintenance              # git maintenance run
tsrc scalar unregister --full foo/bar
```

Backend resolution (design section 4): `git scalar` → stand-alone
`scalar` → macOS Xcode-bundled `scalar` → decomposed form
(the 27 config keys from a real `scalar register`, applied with
`git config`, plus `git maintenance register` and the global
`scalar.repo` / `maintenance.repo` entries). On WSL the decomposed
form is what runs. `--backend` forces one backend for a run.
`unregister` alone leaves the local keys in place (as real
`scalar unregister` does); `--full` removes them too.

## tsrc-worktree

State lives in `<workspace>/.tsrc/worktrees.yml` (the ledger).
Worktrees live under `<workspace>/.tsrc/worktrees/<name>`.

```sh
tsrc worktree add foo/bar my-task
tsrc worktree add foo/bar my-task --branch feature-x --provision
tsrc worktree list                   # hold marks, liveness, unregistered
tsrc worktree hold my-task
tsrc worktree unhold my-task
tsrc worktree remove my-task         # refuses dirty / unpushed / held
tsrc worktree remove my-task --force
tsrc worktree prune                  # drop ledger entries whose dir is gone
tsrc worktree start feature-x -g daiko
tsrc worktree start feature-x foo/bar baz/qux
```

`remove` refuses uncommitted changes and unpushed work by
default. `start` cuts the same branch across a bundle of repos,
provisions one worktree per repo, and records the bundle in the
ledger. Partial failure is explicit: successes stand, failures are
listed, and re-running the same `start` fills in only what is
missing. A repo that already has the branch is taken into the
bundle as a member.

## --provision (overlay)

`--provision` provisions a worktree from a prebuilt base
(`<workspace>/.tsrc/prebuild/<dest-slug>/` — a full checkout with
dependencies installed and `.git` removed; record the commit in
`<slug>.json` beside the base directory, not inside it). On Linux/WSL the session tree is an overlayfs mount
(lowerdir=base, per-session upper/work), mounted with
`sudo mount -t overlay` (passwordless sudo required; see
`tsrc_ext/overlay.py` for why the alternatives do not fit a
per-command CLI). The base must be built from the same commit used
as the branch base, or `git status` in the session will show the
delta as uncommitted changes. Mac APFS-clone provisioning is not
implemented in this stage (`--provision` on macOS fails with a
clear message).

## Engine seam

Both commands are windows onto the engine (`tsrc_ext/engine.py`).
With no engine running they execute one-shot against the workspace
on disk. `TSRC_ENGINE_SOCKET` names the engine's Unix socket; the
IPC form is stage 2+ work, so a set variable currently fails loudly
instead of silently falling back.
