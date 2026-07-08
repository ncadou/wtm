"""Presentation helpers for wtm: stderr diagnostics, stdout exports, the cd channel.

The single place that talks to the user or the shell wrapper. Business-logic
modules (:mod:`wtm.project`, :mod:`wtm.worktree`) stay pure and return data;
:mod:`wtm.cli` and :mod:`wtm.hooks` render it through these helpers.
"""

from __future__ import annotations

__all__ = [
    "emit_cd",
    "emit_env_line",
    "log",
    "tree_children_lines",
    "warn",
    "worktree_list_lines",
]

import os
import sys
from pathlib import Path


def log(message: str) -> None:
    """Print an informational diagnostic to stderr."""
    print(f"wtm: {message}", file=sys.stderr)


def warn(message: str) -> None:
    """Print a warning diagnostic to stderr."""
    print(f"wtm: warning: {message}", file=sys.stderr)


def emit_env_line(name: str, value: str) -> str:
    """Render ``export NAME='value'`` with single-quote escaping for ``eval``."""
    escaped = value.replace("'", "'\\''")

    return f"export {name}='{escaped}'"


def emit_cd(target: Path, *, fallback: bool = True) -> None:
    """Write *target* to WTM_CD_FILE for the shell wrapper.

    When not running under the wrapper (no ``WTM_CD_FILE``): with *fallback*
    print a manual-cd notice, otherwise stay silent (the cd could not affect
    the parent shell anyway, e.g. delete's opportunistic rescue).
    """
    cd_file = os.environ.get("WTM_CD_FILE")
    if cd_file:
        Path(cd_file).write_text(f"{target}\n")
    elif fallback:
        warn("not running under the wtm shell wrapper; cd manually:")
        print(target)


def tree_children_lines(children: list[str]) -> list[str]:
    """Render *children* as GNU-tree-style connector lines (last gets the elbow).

    A pure helper: it returns the lines so the caller decides where they go.
    An empty list yields no lines (an empty project shows just its name).
    """
    lines = []
    last = len(children) - 1
    for index, child in enumerate(children):
        connector = "└── " if index == last else "├── "
        lines.append(f"{connector}{child}")

    return lines


def worktree_list_lines(rows: list[tuple[str, str]]) -> list[str]:
    """Render ``(name, annotation)`` rows as name-aligned worktree lines.

    A pure formatter, like :func:`tree_children_lines`: the caller pre-derives
    each row (name relative to ``worktrees/`` or a fallback path, plus an
    annotation such as ``abc1234 [branch]``); this only left-aligns the name
    column to its widest entry. An empty list yields no lines.
    """
    if not rows:
        return []

    width = max(len(name) for name, _ in rows)

    return [f"{name.ljust(width)}  {annotation}".rstrip() for name, annotation in rows]
