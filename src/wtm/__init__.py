"""wtm - git worktree manager.

Manage git worktrees laid out as::

    $WTM_PROJECTS_ROOT/<project>/
        .git/                 bare repository
        worktrees/<name>/     worktrees

wtm knows only this structure. Anything project-specific (shared data/tools
dirs, direnv, sesh/tmux) is the concern of hooks, never of wtm itself.
"""

from __future__ import annotations

__all__ = ["app", "main"]

from wtm.cli import app, main
