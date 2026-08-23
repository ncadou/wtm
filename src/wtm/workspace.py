"""Workspace-level operations for wtm: add, delete, set, rename, default, show, enter, leave.

CLI-agnostic orchestration, like :mod:`wtm.project` / :mod:`wtm.worktree`: methods
return result objects and never print, log, or drive the shell (that is
:mod:`wtm.cli`'s job). Registry-mutating verbs (add/delete/set/rename/default)
delegate every write to :mod:`wtm.config` (the single config authority); the
lifecycle verbs (enter/leave) fire the ``workspace-enter`` / ``workspace-leave``
events via :func:`wtm.hooks.run_workspace_hooks`.

The class is named :class:`Workspaces` (plural) because :class:`wtm.config.Workspace`
already owns the singular noun (the declared-workspace dataclass this operates on).
"""

from __future__ import annotations

__all__ = [
    "WorkspaceAdded",
    "WorkspaceDeleted",
    "WorkspaceEntered",
    "WorkspaceLeft",
    "WorkspaceRenamed",
    "WorkspaceSet",
    "WorkspaceShown",
    "Workspaces",
]

import os
from dataclasses import dataclass, replace
from pathlib import Path

from wtm import git
from wtm.config import (
    WORKSPACE_WRITE_SCOPES,
    Config,
    ConfigAddress,
    Scope,
    check_write_scope,
    workspace_default_scope,
)
from wtm.core import (
    WtmError,
    abspath,
    current_dir,
    expand_path,
    leave_landing,
    project_dir_for,
    within,
)
from wtm.hooks import run_project_hooks, run_workspace_hooks
from wtm.project import Project
from wtm.worktree import Worktree


@dataclass(frozen=True)
class WorkspaceAdded:
    """Result of :meth:`Workspaces.add`."""

    name: str
    path: str  # the persisted (human) form
    resolved: Path  # absolute, after ~/$VAR expansion
    missing: bool  # path does not exist on disk
    created: bool  # path was mkdir'd (--create)
    shadows_system: bool  # a system-scope declaration of this name exists
    duplicate_of: str | None  # another workspace already at this resolved path


@dataclass(frozen=True)
class WorkspaceDeleted:
    """Result of :meth:`Workspaces.delete`."""

    name: str
    cleared_default: bool  # default_workspace pointed here and was cleared
    remains_at_system: bool  # a system-scope declaration survives


@dataclass(frozen=True)
class WorkspaceSet:
    """Result of :meth:`Workspaces.set`."""

    name: str
    resolved: Path | None  # new path (absolute) when the path was changed
    missing: bool  # new path does not exist on disk
    scope: Scope | None  # where scalars landed (None when only --path was set)


@dataclass(frozen=True)
class WorkspaceRenamed:
    """Result of :meth:`Workspaces.rename`."""

    old: str
    new: str
    fixed_default: bool  # default_workspace pointed at old and was rewritten


@dataclass(frozen=True)
class WorkspaceShown:
    """Result of :meth:`Workspaces.show`: a read-only per-workspace detail view."""

    name: str
    path: Path
    exists: bool
    description: str | None
    base_branch: str | None
    scope: Scope
    project_count: int


@dataclass(frozen=True)
class WorkspaceEntered:
    """Result of :meth:`Workspaces.enter`: the projects root to cd into."""

    name: str
    dir: Path


@dataclass(frozen=True)
class WorkspaceLeft:
    """Result of :meth:`Workspaces.leave`: where to land, and whether a cascade fired."""

    name: str
    cd_dir: Path | None  # None means "stay put"
    cascaded: bool  # a worktree and/or project 'leave' fired first


class Workspaces:
    """Workspace-level operations bound to a resolved :class:`Config`."""

    def __init__(self, config: Config) -> None:
        self.config = config

    # --- registry mutations --------------------------------------------------

    def add(
        self,
        name: str,
        path: str | None,
        *,
        description: str | None,
        base_branch: str | None,
        create: bool,
    ) -> WorkspaceAdded:
        """Register a new workspace in the user-scope config.

        *path* defaults to the cwd. The persisted string keeps a ``~``/``$VAR``
        form; a bare relative arg is made absolute. Hard-errors on a name
        already declared at user scope; warns (via the result) when it shadows a
        system entry or reuses another workspace's path.
        """
        _valid_workspace_name(name)
        scopes = self.config.declaring_scopes(name)
        if Scope.USER in scopes:
            raise WtmError(
                f"workspace '{name}' already declared (user scope); "
                "edit it with 'wtm workspace set' or delete it first"
            )

        persist, resolved = _prepare_path(path)
        missing, created = self._materialise(resolved, create=create)
        duplicate_of = self._path_owner(resolved)

        self.config.add_workspace(name, persist, description=description, base_branch=base_branch)

        return WorkspaceAdded(
            name=name,
            path=persist,
            resolved=resolved,
            missing=missing,
            created=created,
            shadows_system=Scope.SYSTEM in scopes,
            duplicate_of=duplicate_of,
        )

    def delete(self, name: str) -> WorkspaceDeleted:
        """Unregister a workspace from the user-scope config (never touches disk)."""
        scopes = self.config.declaring_scopes(name)
        if not scopes:
            raise WtmError(f"workspace '{name}' is not declared")
        if Scope.USER not in scopes:
            raise WtmError(
                f"workspace '{name}' is declared only at system scope; "
                "edit the system config to remove it"
            )

        cleared = self.config.remove_workspace(name)

        return WorkspaceDeleted(
            name=name,
            cleared_default=cleared,
            remains_at_system=Scope.SYSTEM in scopes,
        )

    def set(
        self,
        name: str,
        *,
        path: str | None,
        description: str | None,
        base_branch: str | None,
        scope: Scope | None = None,
    ) -> WorkspaceSet:
        """Edit an existing workspace's scalars (partial update).

        ``--path`` always writes the user-scope registry entry (the declaration
        is *found* via its path, so it cannot live in the workspace-local file);
        it refuses a system-only name. ``--description`` / ``--base-branch`` land
        at *scope* (one of workspace / user / system); when *scope* is ``None``
        the auto-default is the deepest scope whose directory exists -- the
        workspace-local ``<ws>/.wtm/config.toml`` when the workspace dir exists,
        else the user-scope registry entry. A field passed as ``""`` is removed;
        ``path`` may not be emptied. At least one field must be given.
        """
        if path is None and description is None and base_branch is None:
            raise WtmError("nothing to set (pass --path, --description, or --base-branch)")
        if path == "":
            raise WtmError("a workspace path may not be empty")

        ws = self._require(name)
        if path is not None:
            self._set_registry_path(name, path)

        resolved: Path | None = None
        missing = False
        if path:
            _, resolved = _prepare_path(path)
            missing, _ = self._materialise(resolved, create=False)

        scalar_scope: Scope | None = None
        if description is not None or base_branch is not None:
            ws_dir = resolved if resolved is not None else ws.path
            scalar_scope = self._set_scalars(
                name, ws, ws_dir, scope, description=description, base_branch=base_branch
            )

        return WorkspaceSet(name=name, resolved=resolved, missing=missing, scope=scalar_scope)

    def _set_scalars(
        self,
        name: str,
        ws,
        ws_dir: Path,
        scope: Scope | None,
        *,
        description: str | None,
        base_branch: str | None,
    ) -> Scope:
        """Place description/base_branch at *scope* (auto: deepest existing dir)."""
        scope = scope if scope is not None else workspace_default_scope(ws_dir)
        check_write_scope(scope, WORKSPACE_WRITE_SCOPES, "workspace")

        # user / system land in the registry entry, which must already declare
        # the workspace at that scope (system-only is refused for a user write).
        if scope is Scope.USER:
            self._require_user_scope(name)
        elif scope is Scope.SYSTEM:
            self._require_scope(name, scope)

        address, prefix = self.config.workspace_write_target(ws, scope)
        self._write_scalars(address, prefix, description=description, base_branch=base_branch)

        return scope

    def _write_scalars(
        self,
        address: ConfigAddress,
        prefix: tuple[str, ...],
        *,
        description: str | None,
        base_branch: str | None,
    ) -> None:
        """Set/clear description/base_branch under *prefix* at *address*."""
        for key, value in (("description", description), ("base_branch", base_branch)):
            if value is None:
                continue
            full = (*prefix, key) if prefix else key
            if value == "":
                self.config.unset_value(address, full)
            else:
                self.config.set_value(address, full, value)

    def _set_registry_path(self, name: str, path: str) -> None:
        """Write ``path`` to the user-scope registry entry (system-only refused)."""
        self._require_user_scope(name)
        persist, _ = _prepare_path(path)
        self.config.set_workspace(name, path=persist)

    def _require_scope(self, name: str, scope: Scope) -> None:
        """Raise unless *name* is declared at *scope* (for a system-scope write)."""
        if scope not in self.config.declaring_scopes(name):
            raise WtmError(f"workspace '{name}' is not declared at {scope} scope")

    def _require_user_scope(self, name: str) -> None:
        """Raise unless *name* is declared at user scope (route system-only via add)."""
        scopes = self.config.declaring_scopes(name)
        if Scope.USER in scopes:
            return
        if scopes:
            raise WtmError(
                f"workspace '{name}' is declared only at system scope; "
                "use 'wtm workspace add' to create a user-scope override"
            )
        raise WtmError(f"workspace '{name}' is not declared")

    def rename(self, old: str, new: str) -> WorkspaceRenamed:
        """Rename the registry key (never moves the directory on disk)."""
        _valid_workspace_name(new)
        old_scopes = self.config.declaring_scopes(old)
        if Scope.USER not in old_scopes:
            if old_scopes:
                raise WtmError(
                    f"workspace '{old}' is declared only at system scope; cannot rename it"
                )
            raise WtmError(f"workspace '{old}' is not declared")
        if self.config.declaring_scopes(new):
            raise WtmError(f"workspace '{new}' already declared")

        fixed = self.config.rename_workspace(old, new)

        return WorkspaceRenamed(old=old, new=new, fixed_default=fixed)

    def default_set(self, name: str) -> None:
        """Set the top-level ``default_workspace`` (name must be declared)."""
        if not self.config.declaring_scopes(name):
            raise WtmError(f"workspace '{name}' is not declared")
        self.config.set_default(name)

    def default_show(self) -> str | None:
        """Return the current ``default_workspace``, or ``None`` when unset."""
        return self.config.default_workspace

    def default_clear(self) -> None:
        """Remove the top-level ``default_workspace`` key."""
        self.config.clear_default()

    # --- read view -----------------------------------------------------------

    def show(self, name: str | None) -> WorkspaceShown:
        """Return a read-only detail view for *name* (default: the active workspace)."""
        ws = self._require(name)
        wcfg = replace(self.config, active=ws.name)

        return WorkspaceShown(
            name=ws.name,
            path=ws.path,
            exists=ws.path.is_dir(),
            description=ws.description,
            base_branch=ws.base_branch,
            scope=ws.scope,
            project_count=len(Project(wcfg).list()),
        )

    # --- lifecycle -----------------------------------------------------------

    def enter(self, name: str | None, *, strict: bool) -> WorkspaceEntered:
        """cd into a workspace's projects root, firing ``workspace-enter`` hooks."""
        ws = self._require(name)
        if not ws.path.is_dir():
            raise WtmError(f"workspace path does not exist: {ws.path}")
        run_workspace_hooks("workspace-enter", ws.name, ws.path, self.config, strict=strict)

        return WorkspaceEntered(name=ws.name, dir=ws.path)

    def leave(self, name: str | None, *, strict: bool) -> WorkspaceLeft:
        """Fire ``workspace-leave`` hooks; cascade the inner tiers when inside them.

        When cwd is inside a project of the target workspace, the teardown runs
        the full chain (worktree->project->workspace): the worktree's ``leave``
        fires first (when standing in a worktree), then that project's
        ``project-leave``, then ``workspace-leave``. The single cd-out lands at
        ``$OLDPWD`` if it is outside the workspace tree, else (having cascaded
        out of a worktree/project) the projects-root's parent, else stays put.
        An explicit NAME you are not standing in fires only ``workspace-leave``
        and never cds.
        """
        explicit = name is not None
        ws = self._require(name)
        cwd = current_dir()
        root = Path(os.path.realpath(ws.path))
        inside = cwd is not None and within(cwd, root)

        cascaded = False
        project_dir = project_dir_for(root, cwd) if inside else None
        if project_dir is not None:
            # Worktree tier first (only when standing in a live worktree).
            if git.is_repo(cwd) and within(cwd, project_dir / "worktrees"):
                try:
                    Worktree(self.config).leave([], strict=strict)
                    cascaded = True
                except (WtmError, git.GitError):
                    cascaded = False
            # Project tier next, regardless of the worktree outcome.
            run_project_hooks("project-leave", project_dir, self.config, strict=strict)
            cascaded = True

        run_workspace_hooks("workspace-leave", ws.name, ws.path, self.config, strict=strict)

        cd_dir = leave_landing(root, inside=inside, explicit=explicit, cascaded=cascaded)

        return WorkspaceLeft(name=ws.name, cd_dir=cd_dir, cascaded=cascaded)

    # --- helpers -------------------------------------------------------------

    def _require(self, name: str | None):
        """Resolve *name* (or the active workspace) to a declared :class:`Workspace`."""
        if name is None:
            return self.config.active_workspace
        if name not in self.config.workspaces:
            raise WtmError(f"unknown workspace: '{name}'")

        return self.config.workspaces[name]

    def _materialise(self, resolved: Path, *, create: bool) -> tuple[bool, bool]:
        """Check a target path; optionally mkdir it. Return (missing, created).

        Rejects a path that exists as a non-directory.
        """
        if resolved.exists():
            if not resolved.is_dir():
                raise WtmError(f"path exists but is not a directory: {resolved}")

            return False, False
        if create:
            resolved.mkdir(parents=True, exist_ok=True)

            return False, True

        return True, False

    def _path_owner(self, resolved: Path) -> str | None:
        """Return an already-declared workspace name sharing *resolved*'s path, if any."""
        for other in self.config.workspaces.values():
            if Path(os.path.realpath(other.path)) == Path(os.path.realpath(resolved)):
                return other.name

        return None


# --- module helpers ----------------------------------------------------------


def _valid_workspace_name(name: str) -> None:
    """Validate a workspace name (a single, safe registry key)."""
    if name in ("", ".", ".."):
        raise WtmError(f"invalid workspace name: '{name}'")
    if "/" in name:
        raise WtmError(f"workspace name may not contain '/': {name}")


def _prepare_path(raw: str | None) -> tuple[str, Path]:
    """Return the (persisted-string, resolved-absolute-Path) for a workspace path.

    ``None`` defaults to the cwd. A ``~``/``$VAR`` or absolute form is persisted
    verbatim; a bare relative arg is made absolute against the cwd. The persisted
    string is validated to expand to an absolute path (a loud misconfig
    otherwise), matching how the reader treats config paths.
    """
    if raw is None:
        cwd = current_dir(resolve=False)
        if cwd is None:
            raise WtmError(
                "cwd is unavailable (the directory was removed); "
                "pass an explicit PATH to 'workspace add'"
            )

        return str(cwd), cwd
    if raw.startswith("~") or "$" in raw or raw.startswith("/"):
        persist = raw
    else:
        persist = str(abspath(raw))

    return persist, expand_path(persist)
