"""The structural write tier's placement policy: where an annotation lands.

One home for the rules every annotation writer shares: the per-entity write
scope allowlists, the auto-default ("deepest candidate whose directory
exists"), and the refusal message. The facade's write-target resolvers name
*where* a scope lands; this module answers *which* scope, when the caller did
not pick one.
"""

from __future__ import annotations

__all__ = [
    "GLOBAL_WRITE_SCOPES",
    "WORKSPACE_WRITE_SCOPES",
    "WORKTREE_WRITE_SCOPES",
    "PROJECT_WRITE_SCOPES",
    "check_write_scope",
    "deepest_existing",
    "project_default_scope",
    "scope_list",
    "workspace_default_scope",
    "worktree_default_scope",
]

from pathlib import Path

from wtm.config.addressing import Scope
from wtm.exceptions import WtmError

# The scopes each entity tier may be annotated at (a workspace has no ancestor
# file below it; a worktree adds its own local file; global work is confined to
# the two non-entity files).
PROJECT_WRITE_SCOPES = (Scope.PROJECT, Scope.WORKSPACE, Scope.USER, Scope.SYSTEM)
WORKSPACE_WRITE_SCOPES = (Scope.WORKSPACE, Scope.USER, Scope.SYSTEM)
WORKTREE_WRITE_SCOPES = (
    Scope.WORKTREE,
    Scope.PROJECT,
    Scope.WORKSPACE,
    Scope.USER,
    Scope.SYSTEM,
)
GLOBAL_WRITE_SCOPES = (Scope.USER, Scope.SYSTEM)


def deepest_existing(*candidates: tuple[Scope, Path | None], fallback: Scope) -> Scope:
    """The first scope in *candidates* whose directory exists, else *fallback*.

    *candidates* are ordered most-local-first as ``(scope, dir-or-None)`` pairs;
    a ``None`` dir never matches. *fallback* is the unconditional last resort
    (user scope in every caller), mirroring the pre-centralization chains.
    """
    for scope, directory in candidates:
        if directory is not None and directory.is_dir():
            return scope

    return fallback


def workspace_default_scope(ws_dir: Path) -> Scope:
    """The auto-default for a workspace annotation (deepest existing dir)."""
    return deepest_existing((Scope.WORKSPACE, ws_dir), fallback=Scope.USER)


def project_default_scope(project_dir: Path, ws_path: Path) -> Scope:
    """The auto-default for a project annotation (deepest existing dir)."""
    return deepest_existing(
        (Scope.PROJECT, project_dir),
        (Scope.WORKSPACE, ws_path),
        fallback=Scope.USER,
    )


def worktree_default_scope(wt_dir: Path, project_dir: Path, ws_path: Path | None) -> Scope:
    """The auto-default for a worktree annotation (deepest existing dir)."""
    return deepest_existing(
        (Scope.WORKTREE, wt_dir),
        (Scope.PROJECT, project_dir),
        (Scope.WORKSPACE, ws_path),
        fallback=Scope.USER,
    )


def check_write_scope(scope: Scope, allowed: tuple[Scope, ...], noun: str) -> None:
    """Raise unless annotating a *noun* at *scope* is allowed for that tier."""
    if scope not in allowed:
        raise WtmError(
            f"cannot write a {noun} annotation at '{scope}' scope "
            f"(choose one of {scope_list(allowed)})"
        )


def scope_list(scopes: tuple[Scope, ...]) -> str:
    """Render a scope tuple as a ``project, workspace, ...`` string."""
    return ", ".join(scope.value for scope in scopes)
