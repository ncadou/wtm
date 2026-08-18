"""The target grammar: decode a command's positionals into a worktree target.

A single deep module for wtm's ``[[[WORKSPACE] PROJECT] NAME] | DIR | cwd``
decoding. Every command that points at a worktree crosses this seam; before, the
same grammar was decoded in :mod:`wtm.core`, :mod:`wtm.worktree`, :mod:`wtm.hooks`,
and re-derived by hand in :mod:`wtm.cli`'s completion.

:func:`resolve` and :func:`slot_kind` are pure -- they take the workspace
registry (name -> projects root) and the current directory explicitly, so the
grammar is the test surface without a subprocess. :func:`resolve_target` is the
one config-bound edge that reads the live cwd.

The leading ``WORKSPACE`` is optional and only reachable with the full triple: a
non-cwd workspace requires all three tokens (``WORKSPACE PROJECT NAME``). With
fewer tokens the workspace falls back to the active one (the ``active_root``),
mirroring the ``WTM_WORKSPACE > cwd > default > sole`` precedence resolved
upstream. Workspace, project, and worktree names share one flat namespace, so a
2-token target is always ``PROJECT NAME``, never ``WORKSPACE PROJECT``.

The accepted shape varies by :class:`TargetMode`:

- ``WORKTREE``  -- ``DIR`` and the bare-cwd default both allowed.
- ``DELETE``    -- ``DIR`` allowed, but a target is required (no cwd default).
- ``NAME_ONLY`` -- ``[[WORKSPACE] PROJECT] NAME`` only; no ``DIR``, no cwd default.
"""

from __future__ import annotations

__all__ = [
    "TargetMode",
    "TokenKind",
    "looks_like_path",
    "resolve",
    "resolve_target",
    "slot_kind",
    "split_target",
    "split_workspace_project",
]

from enum import Enum
from pathlib import Path

from wtm.config import Config
from wtm.core import (
    WtmError,
    abspath,
    current_dir,
    project_dir_for,
    reject_traversal,
    valid_project_name,
)


class TargetMode(Enum):
    """Which shape of the target grammar a command accepts."""

    WORKTREE = "worktree"  # DIR ok, bare-cwd default ok
    DELETE = "delete"  # DIR ok, no cwd default (target required)
    NAME_ONLY = "name_only"  # [[WORKSPACE] PROJECT] NAME only; no DIR, no cwd


class TokenKind(Enum):
    """What may appear at a positional slot during completion."""

    WORKSPACE = "workspace"
    PROJECT = "project"
    WORKTREE_NAME = "worktree_name"


def resolve(
    mode: TargetMode,
    args: list[str],
    *,
    roots: dict[str, Path],
    active_root: Path | None,
    cwd: Path | None,
) -> Path:
    """Resolve *args* to a worktree directory under *mode*'s grammar (pure).

    :param mode: which shape is accepted (see :class:`TargetMode`).
    :param args: the command's remaining positionals.
    :param roots: the declared workspaces, name -> projects root; feeds the
        leading ``WORKSPACE`` token's registry lookup.
    :param active_root: the active workspace's projects root (the fallback for
        the ``<= 2``-token cases), or ``None`` when no workspace is active.
    :param cwd: the current directory (used for the cwd default / project
        inference), or ``None`` when the shell's directory was removed -- the
        cwd-dependent forms then raise a clear error instead of crashing.
        Passed explicitly so the grammar stays pure.
    :returns: the resolved worktree directory.

    Grammar:

    - ``WORKSPACE PROJECT NAME`` -> ``<roots[WORKSPACE]>/PROJECT/worktrees/NAME``
    - ``PROJECT NAME`` -> ``<active_root>/PROJECT/worktrees/NAME``
    - ``NAME`` -> ``<cwd-project>/worktrees/NAME`` (project inferred from *cwd*);
      ``NAME`` may be nested. Under ``WORKTREE``/``DELETE`` a single arg is an
      explicit path when it starts with ``/``, ``./``, ``../`` or is ``.``/``..``.
    - no args -> *cwd* (``WORKTREE`` only; ``DELETE`` requires a target).
    """
    if mode is TargetMode.NAME_ONLY:
        workspace, project, name = split_target(args)
        if looks_like_path(name):
            raise WtmError(f"takes a NAME, not a path: {name}")
        reject_traversal(name)

        return _project_dir(roots, active_root, cwd, workspace, project) / "worktrees" / name

    # WORKTREE / DELETE share the DIR-and-inference grammar; they differ only
    # in whether the zero-arg cwd default is allowed.
    count = len(args)
    if count == 0:
        if mode is TargetMode.DELETE:
            raise WtmError(
                "a target is required (WORKSPACE PROJECT NAME | PROJECT NAME | NAME | DIR)"
            )

        return _require_cwd(cwd)
    if count == 1:
        arg = args[0]
        if looks_like_path(arg):
            return abspath(arg)
        reject_traversal(arg)
        root = _require_active(active_root)
        project_dir = project_dir_for(root, _require_cwd(cwd))
        if project_dir is None:
            raise WtmError(
                "1-arg NAME given but cwd is not under "
                f"{root} (pass PROJECT NAME or an explicit ./ path)"
            )

        return project_dir / "worktrees" / arg

    # count 2 (PROJECT NAME) or 3 (WORKSPACE PROJECT NAME).
    workspace, project, name = split_target(args)
    valid_project_name(project)  # type: ignore[arg-type]
    reject_traversal(name)

    return _resolve_root(roots, active_root, workspace) / project / "worktrees" / name


def slot_kind(mode: TargetMode, args: list[str]) -> frozenset[TokenKind]:
    """Return the token kinds admissible at the next completion slot.

    *args* is what the shell has completed so far (not counting the token being
    typed). The leading slots are ambiguous because ``WORKSPACE``, ``PROJECT``,
    and ``NAME`` share one namespace and the decode is count-based: at slot 0 a
    bare token may be any of the three (a WORKSPACE, a PROJECT, or a NAME of the
    cwd-inferred project); at slot 1 it may be a PROJECT (the WORKSPACE reading)
    or a NAME (the PROJECT reading). Under ``NAME_ONLY`` (``create``-style) the
    trailing NAME being invented has nothing to offer, so its slot is empty. The
    empty set means nothing target-shaped completes here.
    """
    n = len(args)
    if mode is TargetMode.NAME_ONLY:
        # [[WORKSPACE] PROJECT] NAME: slot 0 WORKSPACE-or-PROJECT, slot 1 PROJECT
        # (only via the WORKSPACE reading), the invented NAME offers nothing.
        if n == 0:
            return frozenset({TokenKind.WORKSPACE, TokenKind.PROJECT})
        if n == 1:
            return frozenset({TokenKind.PROJECT})

        return frozenset()

    # WORKTREE / DELETE: slot 0 is WORKSPACE-or-PROJECT-or-NAME, slot 1 is
    # PROJECT (WORKSPACE reading) or NAME (PROJECT reading), slot 2 is the NAME.
    if n == 0:
        return frozenset({TokenKind.WORKSPACE, TokenKind.PROJECT, TokenKind.WORKTREE_NAME})
    if n == 1:
        return frozenset({TokenKind.PROJECT, TokenKind.WORKTREE_NAME})
    if n == 2:
        return frozenset({TokenKind.WORKTREE_NAME})

    return frozenset()


def split_target(args: list[str]) -> tuple[str | None, str | None, str]:
    """Split positionals into (WORKSPACE, PROJECT, NAME) by count.

    One arg is ``NAME``, two are ``PROJECT NAME``, three are
    ``WORKSPACE PROJECT NAME``; omitted leading slots are ``None``. Public
    because ``create`` needs the split directly (it builds its own destination
    for ``-d/--dir`` rather than going through :func:`resolve`).
    """
    if len(args) == 3:
        return args[0], args[1], args[2]
    if len(args) == 2:
        return None, args[0], args[1]
    if len(args) == 1:
        return None, None, args[0]
    if len(args) == 0:
        raise WtmError("usage: a NAME is required")

    raise WtmError("too many arguments")


def split_workspace_project(args: list[str]) -> tuple[str | None, str | None]:
    """Split a project-level ``[[WORKSPACE] PROJECT]`` into (WORKSPACE, PROJECT).

    The project-level analog of :func:`split_target` (no NAME): zero args are
    both ``None`` (cwd inference), one is ``PROJECT``, two are
    ``WORKSPACE PROJECT``. Used by the project-level commands (list/resolve),
    which resolve a project rather than a worktree.
    """
    if len(args) == 2:
        return args[0], args[1]
    if len(args) == 1:
        return None, args[0]
    if len(args) == 0:
        return None, None

    raise WtmError("too many arguments (expected [[WORKSPACE] PROJECT])")


def resolve_target(config: Config, mode: TargetMode, args: list[str]) -> Path:
    """Config-bound edge of :func:`resolve`: the one place that reads the live cwd."""
    roots = {name: ws.path for name, ws in config.workspaces.items()}
    active_root = config.workspaces[config.active].path if config.active is not None else None

    cwd = current_dir(resolve=False)

    return resolve(mode, args, roots=roots, active_root=active_root, cwd=cwd)


# --- helpers -----------------------------------------------------------------


def looks_like_path(arg: str) -> bool:
    """A single arg is a path only if it starts with '/', './', '../' or is '.'/'..'."""
    return (
        arg.startswith("/") or arg.startswith("./") or arg.startswith("../") or arg in (".", "..")
    )


def _project_dir(
    roots: dict[str, Path],
    active_root: Path | None,
    cwd: Path | None,
    workspace: str | None,
    project: str | None,
) -> Path:
    """Resolve the project dir for a NAME_ONLY parse from WORKSPACE/PROJECT or the cwd."""
    if project is not None:
        valid_project_name(project)

        return _resolve_root(roots, active_root, workspace) / project

    root = _require_active(active_root)
    project_dir = project_dir_for(root, _require_cwd(cwd))
    if project_dir is None:
        raise WtmError(f"no PROJECT given and cwd is not under {root}")

    return project_dir


def _resolve_root(roots: dict[str, Path], active_root: Path | None, workspace: str | None) -> Path:
    """Pick the projects root: the named workspace's, else the active fallback."""
    if workspace is not None:
        if workspace not in roots:
            raise WtmError(f"unknown workspace: '{workspace}'")

        return roots[workspace]

    return _require_active(active_root)


def _require_cwd(cwd: Path | None) -> Path:
    """Return *cwd* or raise the shell-directory-gone error for a cwd-dependent form."""
    if cwd is None:
        raise WtmError(
            "cwd is unavailable (the directory was removed); "
            "pass an explicit target (WORKSPACE PROJECT NAME | PROJECT NAME | DIR)"
        )

    return cwd


def _require_active(active_root: Path | None) -> Path:
    """Return the active projects root or raise the no-active-workspace error."""
    if active_root is None:
        raise WtmError(
            "no active workspace; pass WORKSPACE PROJECT NAME, set WTM_WORKSPACE, "
            "cd into a workspace, or set a default_workspace"
        )

    return active_root
