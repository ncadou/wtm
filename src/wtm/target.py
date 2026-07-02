"""The target grammar: decode a command's positionals into a worktree target.

A single deep module for wtm's ``[PROJECT] NAME | NAME | DIR | cwd`` decoding.
Every command that points at a worktree crosses this seam; before, the same
grammar was decoded in :mod:`wtm.core`, :mod:`wtm.worktree`, :mod:`wtm.hooks`,
and re-derived by hand in :mod:`wtm.cli`'s completion.

:func:`resolve` and :func:`slot_kind` are pure -- they take the projects root
and the current directory explicitly, so the grammar is the test surface
without a subprocess. :func:`resolve_target` is the one config-bound edge that
reads the live cwd.

The accepted shape varies by :class:`TargetMode`:

- ``WORKTREE``  -- ``DIR`` and the bare-cwd default both allowed.
- ``DELETE``    -- ``DIR`` allowed, but a target is required (no cwd default).
- ``NAME_ONLY`` -- ``PROJECT NAME`` / ``NAME`` only; no ``DIR``, no cwd default.
"""

from __future__ import annotations

__all__ = [
    "TargetMode",
    "TokenKind",
    "resolve",
    "resolve_target",
    "slot_kind",
    "split_project_name",
]

import os
from enum import Enum
from pathlib import Path

from wtm.core import (
    Config,
    WtmError,
    abspath,
    project_dir_for,
    reject_traversal,
    valid_project_name,
)


class TargetMode(Enum):
    """Which shape of the target grammar a command accepts."""

    WORKTREE = "worktree"  # DIR ok, bare-cwd default ok
    DELETE = "delete"  # DIR ok, no cwd default (target required)
    NAME_ONLY = "name_only"  # PROJECT NAME / NAME only; no DIR, no cwd


class TokenKind(Enum):
    """What may appear at a positional slot during completion."""

    PROJECT = "project"
    WORKTREE_NAME = "worktree_name"


def resolve(mode: TargetMode, args: list[str], *, projects_root: Path, cwd: Path) -> Path:
    """Resolve *args* to a worktree directory under *mode*'s grammar (pure).

    :param mode: which shape is accepted (see :class:`TargetMode`).
    :param args: the command's remaining positionals.
    :param projects_root: the configured projects root.
    :param cwd: the current directory (used for the cwd default / project
        inference). Passed explicitly so the grammar stays pure.
    :returns: the resolved worktree directory.

    Grammar:

    - ``PROJECT NAME`` -> ``<projects_root>/PROJECT/worktrees/NAME``
    - ``NAME`` -> ``<cwd-project>/worktrees/NAME`` (project inferred from *cwd*);
      ``NAME`` may be nested. Under ``WORKTREE``/``DELETE`` a single arg is an
      explicit path when it starts with ``/``, ``./``, ``../`` or is ``.``/``..``.
    - no args -> *cwd* (``WORKTREE`` only; ``DELETE`` requires a target).
    """
    if mode is TargetMode.NAME_ONLY:
        project, name = split_project_name(args)
        if _looks_like_path(name):
            raise WtmError(f"takes a NAME, not a path: {name}")
        reject_traversal(name)

        return _project_dir(projects_root, cwd, project) / "worktrees" / name

    # WORKTREE / DELETE share the DIR-and-inference grammar; they differ only
    # in whether the zero-arg cwd default is allowed.
    count = len(args)
    if count == 0:
        if mode is TargetMode.DELETE:
            raise WtmError("a target is required (PROJECT NAME | NAME | DIR)")

        return abspath(str(cwd))
    if count == 1:
        arg = args[0]
        if _looks_like_path(arg):
            return abspath(arg)
        reject_traversal(arg)
        project_dir = project_dir_for(projects_root, cwd)
        if project_dir is None:
            raise WtmError(
                "1-arg NAME given but cwd is not under "
                f"{projects_root} (pass PROJECT NAME or an explicit ./ path)"
            )

        return project_dir / "worktrees" / arg
    if count == 2:
        project, name = args
        valid_project_name(project)
        reject_traversal(name)

        return projects_root / project / "worktrees" / name

    raise WtmError("too many arguments (expected PROJECT NAME | NAME | DIR)")


def slot_kind(mode: TargetMode, args: list[str]) -> frozenset[TokenKind]:
    """Return the token kinds admissible at the next completion slot.

    *args* is what the shell has completed so far (not counting the token being
    typed). At the first slot a bare token is ambiguous -- a PROJECT or a NAME
    of the cwd-inferred project -- so both kinds are offered, except under
    ``NAME_ONLY`` (``create``-style) where only a fresh PROJECT can be
    completed; the new NAME being invented has nothing to offer. The empty set
    means nothing target-shaped completes here.
    """
    n = len(args)
    if mode is TargetMode.NAME_ONLY:
        # Only the first slot (the PROJECT) completes; a brand-new NAME does not.
        return frozenset({TokenKind.PROJECT}) if n == 0 else frozenset()

    # WORKTREE / DELETE: slot 0 is PROJECT-or-NAME, slot 1 is that project's NAME.
    if n == 0:
        return frozenset({TokenKind.PROJECT, TokenKind.WORKTREE_NAME})
    if n == 1:
        return frozenset({TokenKind.WORKTREE_NAME})

    return frozenset()


def split_project_name(args: list[str]) -> tuple[str | None, str]:
    """Split positionals into (PROJECT, NAME): one arg is NAME, two are PROJECT NAME.

    Public because ``create`` needs the split directly (it builds its own
    destination for ``-d/--dir`` rather than going through :func:`resolve`).
    """
    if len(args) == 2:
        return args[0], args[1]
    if len(args) == 1:
        return None, args[0]
    if len(args) == 0:
        raise WtmError("usage: a NAME is required")

    raise WtmError("too many arguments")


def resolve_target(config: Config, mode: TargetMode, args: list[str]) -> Path:
    """Config-bound edge of :func:`resolve`: the one place that reads the live cwd."""
    return resolve(mode, args, projects_root=config.projects_root, cwd=abspath(os.getcwd()))


# --- helpers -----------------------------------------------------------------


def _looks_like_path(arg: str) -> bool:
    """A single arg is a path only if it starts with '/', './', '../' or is '.'/'..'."""
    return (
        arg.startswith("/") or arg.startswith("./") or arg.startswith("../") or arg in (".", "..")
    )


def _project_dir(projects_root: Path, cwd: Path, project: str | None) -> Path:
    """Resolve the project dir for a NAME_ONLY parse from PROJECT or the cwd."""
    if project is not None:
        valid_project_name(project)

        return projects_root / project

    project_dir = project_dir_for(projects_root, cwd)
    if project_dir is None:
        raise WtmError(f"no PROJECT given and cwd is not under {projects_root}")

    return project_dir
