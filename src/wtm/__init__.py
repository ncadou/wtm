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

from wtm.cli import app, main
