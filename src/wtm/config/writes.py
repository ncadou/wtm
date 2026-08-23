"""The structural write tier's placement policy: where an annotation lands.

One home for the rules every annotation writer shares: the per-entity write
scope allowlists, the auto-default ("deepest candidate whose directory
exists"), the refusal message, and the tier a write lands on (the floor-check
half of the placement policy). The facade's write-target resolvers name
*where* a scope lands; this module answers *which* scope, when the caller did
not pick one.
"""

from __future__ import annotations

__all__ = [
    "GLOBAL_WRITE_SCOPES",
    "WORKSPACE_WRITE_SCOPES",
    "WORKTREE_WRITE_SCOPES",
    "PROJECT_WRITE_SCOPES",
    "check_key_floor",
    "check_write_scope",
    "deepest_existing",
    "project_default_scope",
    "scope_list",
    "tier_of_write_site",
    "workspace_default_scope",
    "worktree_default_scope",
]

from pathlib import Path

from wtm.config.addressing import Scope
from wtm.config.model import FLOOR_TIERS
from wtm.exceptions import WriteProhibitedError, WtmError

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


# --- floors ------------------------------------------------------------------


def tier_of_write_site(scope: Scope, parts: tuple[str, ...]) -> str:
    """The entity tier a write at *scope* under key *parts* lands on.

    A local file maps directly (worktree/project/workspace scope); the user
    and system files derive the tier from the structural prefix: a key nested
    under ``(workspaces, W)`` is a workspace-tier value, one nested deeper
    under ``(workspaces, W, projects, P)`` a project-tier value, one under
    ``(workspaces, W, projects, P, worktrees, N)`` a worktree-tier value; any
    other shape (a bare top-level key included) is global. The inverse of the
    write-target resolvers.
    """
    if scope is Scope.WORKTREE:
        return "worktree"

    if scope is Scope.PROJECT:
        return "project"

    if scope is Scope.WORKSPACE:
        return "workspace"

    rest = list(parts)
    if not rest or rest[0] != "workspaces":
        return "global"
    # Consume "workspaces" + the workspace NAME; classify by what follows.
    rest = rest[2:]
    if len(rest) < 2 or rest[0] != "projects":
        return "workspace"
    # Consume "projects" + the project NAME.
    rest = rest[2:]
    if len(rest) < 2 or rest[0] != "worktrees":
        return "project"

    return "worktree"


def check_key_floor(
    floor: str | None,
    head: str,
    scope: Scope,
    full_parts: tuple[str, ...],
) -> None:
    """Refuse writing a floored key deeper than its floor.

    *floor* is the key's effective floor (``None`` = unrestricted), *head* its
    name (for messages), and *full_parts* the complete store key (structural
    prefix included), from which the landing tier is derived via
    :func:`tier_of_write_site`. Raises ``WriteProhibitedError`` below the
    floor. Removals never come through here -- unsetting a stray below-floor
    value is cleanup, not placement.
    """
    if floor is None:
        return

    tier = tier_of_write_site(scope, full_parts)
    if FLOOR_TIERS[tier] < FLOOR_TIERS[floor]:
        raise WriteProhibitedError(
            f"'{head}' has floor '{floor}'; it cannot be written at "
            f"'{tier}' tier ({scope.value} scope)"
        )
