"""Presentation helpers for wtm: stderr diagnostics, stdout exports, the cd channel.

The single place that talks to the user or the shell wrapper. Business-logic
modules (:mod:`wtm.project`, :mod:`wtm.worktree`) stay pure and return data;
:mod:`wtm.cli` and :mod:`wtm.hooks` render it through these helpers.
"""

from __future__ import annotations

__all__ = [
    "MANAGED_ENV_VARS",
    "config_slice_json",
    "emit_env_line",
    "emit_shell",
    "log",
    "tree_children_lines",
    "warn",
    "worktree_flat_all_json",
    "worktree_flat_all_lines",
    "worktree_flat_json",
    "worktree_flat_lines",
    "worktree_list_lines",
]

import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from wtm.config import config_slice_json

if TYPE_CHECKING:
    pass

FlatRow = tuple[str, str, str, str, str]  # (project, name, path, branch, head)
_FLAT_KEYS = ("project", "name", "path", "branch", "head")
# The --all variant prepends the workspace.
FlatAllRow = tuple[str, str, str, str, str, str]
_FLAT_ALL_KEYS = ("workspace", "project", "name", "path", "branch", "head")

# The WTM_* context vars the shell wrapper manages as a set: each shell move
# exports the applicable subset and unsets the rest, so the interactive shell
# reflects the current wtm session. WTM_WORKSPACE is included (it pins the
# active workspace until leave -- the session contract); the wrapper never sets
# any var outside this tuple.
MANAGED_ENV_VARS = (
    "WTM_WORKTREE_DIR",
    "WTM_BRANCH",
    "WTM_PROJECT",
    "WTM_PROJECT_DIR",
    "WTM_WORKTREES_DIR",
    "WTM_GIT_COMMON_DIR",
    "WTM_WORKSPACE",
    "WTM_WORKSPACE_DIR",
)


def log(message: str) -> None:
    """Print an informational diagnostic to stderr."""
    print(f"wtm: {message}", file=sys.stderr)


def warn(message: str) -> None:
    """Print a warning diagnostic to stderr."""
    print(f"wtm: warning: {message}", file=sys.stderr)


def _squote(value: str) -> str:
    """Single-quote *value* for POSIX ``sh`` (escaping embedded single quotes)."""
    escaped = value.replace("'", "'\\''")

    return f"'{escaped}'"


def emit_env_line(name: str, value: str) -> str:
    """Render ``export NAME='value'`` with single-quote escaping for ``eval``."""
    return f"export {name}={_squote(value)}"


def emit_shell(
    cd: Path | None = None,
    env: dict[str, str] | None = None,
    *,
    fallback: bool = True,
) -> None:
    """Emit shell directives to WTM_SHELL_FILE for the shell wrapper to source.

    The wrapper creates the file, passes it, then sources it -- so the emitted
    ``cd``/``export``/``unset`` lines run in the calling shell. *env* is the
    desired managed subset to export (see :data:`MANAGED_ENV_VARS`); every
    managed var *not* in *env* is unset, so the interactive shell reflects the
    current wtm session. An empty *env* (leave/delete) unsets the whole set.

    When not running under the wrapper (no ``WTM_SHELL_FILE``): with *fallback*
    print a manual-cd notice for *cd*, otherwise stay silent (delete's
    opportunistic rescue); env directives are dropped either way (they cannot
    affect the parent shell).
    """
    env = env or {}
    shell_file = os.environ.get("WTM_SHELL_FILE")
    if not shell_file:
        if cd is not None and fallback:
            warn("not running under the wtm shell wrapper; cd manually:")
            print(cd)

        return

    lines: list[str] = []
    if cd is not None:
        lines.append(f"cd -- {_squote(str(cd))}")
    lines.extend(emit_env_line(name, env[name]) for name in MANAGED_ENV_VARS if name in env)
    unset = [name for name in MANAGED_ENV_VARS if name not in env]
    if unset:
        lines.append("unset " + " ".join(unset))

    Path(shell_file).write_text("".join(f"{line}\n" for line in lines))


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


def worktree_flat_lines(rows: list[FlatRow]) -> list[str]:
    """Render flat cross-project worktree rows as tab-separated lines.

    Machine-readable projection for the ``project list --worktrees`` mode: one
    worktree per line, fields ``project\\tname\\tpath\\tbranch\\thead`` (branch
    empty when detached). Tab is safe: none of the fields can contain one.
    Policy-free — no session naming or path munging (a consumer's concern).
    """
    return ["\t".join(row) for row in rows]


def worktree_flat_json(rows: list[FlatRow]) -> str:
    """Render flat cross-project worktree rows as a JSON array of objects.

    The ``--json`` counterpart of :func:`worktree_flat_lines`: each row becomes
    ``{"project","name","path","branch","head"}``, key order preserved.
    """
    return json.dumps([dict(zip(_FLAT_KEYS, row, strict=True)) for row in rows])


def worktree_flat_all_lines(rows: list[FlatAllRow]) -> list[str]:
    """Render cross-workspace flat rows as tab-separated lines.

    The ``project list --all --worktrees`` projection: one worktree per line,
    fields ``workspace\\tproject\\tname\\tpath\\tbranch\\thead`` (the leading
    workspace column is the only difference from :func:`worktree_flat_lines`).
    """
    return ["\t".join(row) for row in rows]


def worktree_flat_all_json(rows: list[FlatAllRow]) -> str:
    """Render cross-workspace flat rows as a JSON array of objects.

    The ``--all --json`` counterpart, each row keyed
    ``{"workspace","project","name","path","branch","head"}``.
    """
    return json.dumps([dict(zip(_FLAT_ALL_KEYS, row, strict=True)) for row in rows])


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
