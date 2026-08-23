"""wtm - git worktree manager.

Manage git worktrees over one or more named workspaces, each laid out as::

    <workspace>/<project>/
        .git/                 bare repository
        worktrees/<name>/     worktrees

wtm knows only this structure. Workspaces are declared in config; projects are
discovered on disk. Anything project-specific (shared data/tools dirs, direnv,
sesh/tmux) is the concern of hooks, never of wtm itself.
"""

from __future__ import annotations

__all__ = ["app", "main"]


def __getattr__(name: str):
    """Lazily expose the CLI entry points without importing typer eagerly.

    Importing :mod:`wtm.config` (or any other submodule) must not drag the CLI
    stack in; ``wtm.app`` / ``wtm.main`` load :mod:`wtm.cli` on first access.
    """
    if name in __all__:
        from wtm import cli

        return getattr(cli, name)

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
