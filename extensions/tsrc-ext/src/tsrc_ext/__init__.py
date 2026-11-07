"""Self-contained tsrc extension commands.

This package holds the `tsrc-scalar` and `tsrc-worktree` commands
invoked through tsrc's external-subcommand discovery mechanism
(`tsrc scalar ...` hands over to `tsrc-scalar`, and so on).

The commands are written as thin windows onto the engine
(see `tsrc_ext.engine`): when no engine is reachable they run
one-shot against the workspace on disk. See the design memo
`tsrc-fork-design-20261107.md`, sections 6 and 13.
"""

__version__ = "0.1.0"
