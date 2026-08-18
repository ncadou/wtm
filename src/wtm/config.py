"""Configuration for wtm: workspaces, scopes, and the layered TOML config.

``wtm`` manages git worktrees over one or more named **workspaces** (each a
projects root ``<path>/<project>/.git`` + ``worktrees/``). Workspaces are
*declared* in config; individual projects are *discovered* on disk and only
*annotated* by config.

Config is read from up to five scopes, mirroring the hook scopes, most-general
to most-specific::

    system:    $WTM_SYSTEM_CONFIG or /etc/wtm/config.toml
    user:      $WTM_CONFIG or $XDG_CONFIG_HOME/wtm/config.toml
    workspace: <workspace.path>/.wtm/config.toml
    project:   <project>/.wtm/config.toml
    worktree:  <worktree>/.wtm/config.toml

This module only *reads* config. Writes go through two seams:
:mod:`wtm.registry` (the user-scope file's ``[workspaces.*]`` entries and
top-level ``default_workspace`` / ``base_branch``, via the ``wtm workspace
add/set/rename/delete/default`` and ``wtm config base-branch`` verbs) and
:mod:`wtm.config_writer` (the entity-local ``<ws>/.wtm/config.toml`` and
``<project>/.wtm/config.toml`` scalars, via ``wtm workspace set`` /
``wtm project set``); system and worktree config stays hand-edited.

The **workspace registry** (name -> path) lives at system/user scope. Scalar
values resolve most-specific-first (``CLI > env > worktree > project >
workspace > user > system > default``); inline hooks *accumulate* across scopes
on top of the builtin file hooks. Env vars ``WTM_NO_SYSTEM`` / ``WTM_NO_CONFIG``
suppress the system / all config files (test and CI isolation).

The active workspace is resolved ``$WTM_WORKSPACE > cwd (longest path match) >
default_workspace > sole workspace > error`` -- there is no built-in default
workspace, so with nothing declared and nothing selected the tool errors,
asking you to declare one. A WORKSPACE named in a command's positionals
overrides this per-target (see :mod:`wtm.target`); there is no ``-w`` flag.
"""

from __future__ import annotations

__all__ = [
    "Config",
    "HookSpec",
    "ProjectAnnotation",
    "ResolvedConfig",
    "ResolvedProject",
    "ResolvedWorktree",
    "Scope",
    "Workspace",
    "resolve_project_dir",
]

import os
import tomllib
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path

from wtm.core import (
    WtmError,
    abspath,
    current_dir,
    expand_path,
    project_dir_for,
    valid_project_name,
    within,
    worktree_name,
)


class Scope(StrEnum):
    """A config/hook scope, from most general to most specific."""

    SYSTEM = "system"
    USER = "user"
    WORKSPACE = "workspace"
    PROJECT = "project"
    WORKTREE = "worktree"


# --- data model --------------------------------------------------------------


@dataclass(frozen=True)
class HookSpec:
    """A scope's hook configuration: inline commands plus builtin-file controls.

    :param on: event -> list of ``sh -c`` command strings (inline hooks).
    :param builtins: whether to also discover builtin ``wtm-<event>`` files;
        ``None`` means unset (treated as enabled), ``False`` disables them for
        this scope. Carried as tri-state so a more-specific declaration site can
        override a less-specific one.
    :param dir: an override for this scope's builtin hook-discovery directory.
    """

    on: dict[str, list[str]] = field(default_factory=dict)
    builtins: bool | None = None
    dir: Path | None = None

    @property
    def run_builtins(self) -> bool:
        """Whether builtin file hooks should fire (unset defaults to yes)."""
        return self.builtins is not False


@dataclass(frozen=True)
class ProjectAnnotation:
    """Config overlay for a discovered project (never a declaration)."""

    description: str | None = None
    base_branch: str | None = None
    hooks: HookSpec = field(default_factory=HookSpec)


@dataclass(frozen=True)
class Workspace:
    """A declared workspace: a named projects root plus its config overlay."""

    name: str
    path: Path
    description: str | None = None
    base_branch: str | None = None  # scope-wide default for its projects
    hooks: HookSpec = field(default_factory=HookSpec)
    projects: dict[str, ProjectAnnotation] = field(default_factory=dict)
    scope: Scope = Scope.USER  # where it was ultimately declared (for --sources)


# --- resolved view -----------------------------------------------------------


@dataclass(frozen=True)
class ResolvedWorktree:
    """The worktree tier of the resolved view (worktree-local config = hooks)."""

    name: str
    dir: Path
    hooks: HookSpec = field(default_factory=HookSpec)


@dataclass(frozen=True)
class ResolvedProject:
    """The cwd-bound project's effective annotation with base_branch provenance."""

    name: str
    dir: Path
    exists: bool
    description: str | None
    base_branch: str | None  # the project-tier value, if any
    effective_base_branch: str | None  # after the full resolution chain
    base_branch_source: str  # project | workspace | global | (none)
    hooks: HookSpec = field(default_factory=HookSpec)
    worktree: ResolvedWorktree | None = None  # set only when cwd is in a worktree


@dataclass(frozen=True)
class ResolvedConfig:
    """The effective config for the active cwd chain (workspace -> project -> worktree).

    A depth-capped, cwd-oriented view: the active workspace's effective scalars
    plus, only when the cwd sits inside them, the cwd-bound project and its
    worktree. Non-active workspaces are left as their declared :class:`Workspace`
    entries (rendered without descent).
    """

    workspaces: dict[str, Workspace]
    active: str | None
    default_base_branch: str | None
    active_workspace: Workspace | None  # the active workspace's effective entry
    project: ResolvedProject | None  # the cwd-bound project, when cwd is inside one


# --- config ------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """Resolved, read-only configuration for one wtm invocation."""

    workspaces: dict[str, Workspace]
    active: str | None
    system_scope_hooks: HookSpec
    user_scope_hooks: HookSpec
    _system_hooks_dir: Path
    _user_hooks_dir: Path
    default_base_branch: str | None = None
    default_workspace: str | None = None

    # --- active workspace ----------------------------------------------------

    @property
    def active_workspace(self) -> Workspace:
        """The active workspace, or a helpful error when none is resolved."""
        if self.active is None:
            if not self.workspaces:
                raise WtmError(
                    "no workspaces declared; add one to your wtm config "
                    "([workspaces.NAME] path = ...)"
                )
            raise WtmError(
                "no active workspace; pass WORKSPACE PROJECT NAME, set WTM_WORKSPACE, "
                "cd into a workspace, or set a default_workspace"
            )

        return self.workspaces[self.active]

    @property
    def projects_root(self) -> Path:
        """The active workspace's path (the projects root commands operate on)."""
        return self.active_workspace.path

    def workspace_root(self, workspace: str | None) -> Path:
        """Resolve a projects root: the named workspace's, else the active fallback.

        A named *workspace* is looked up in the registry (the leading WORKSPACE
        token of the target grammar); ``None`` falls back to the active
        workspace, raising the no-active-workspace error when none is resolved.
        """
        if workspace is not None:
            if workspace not in self.workspaces:
                raise WtmError(f"unknown workspace: '{workspace}'")

            return self.workspaces[workspace].path

        return self.projects_root

    @property
    def system_hooks_dir(self) -> Path:
        """The system-scope builtin hook-discovery directory."""
        return self.system_scope_hooks.dir or self._system_hooks_dir

    @property
    def user_hooks_dir(self) -> Path:
        """The user-scope builtin hook-discovery directory."""
        return self.user_scope_hooks.dir or self._user_hooks_dir

    @property
    def workspace_hooks_dir(self) -> Path:
        """The active workspace's builtin hook-discovery directory."""
        ws = self.active_workspace

        return ws.hooks.dir or ws.path / ".wtm" / "hooks"

    # --- workspace lookup ----------------------------------------------------

    def workspace_of(self, project_dir: Path) -> Workspace | None:
        """Return the declared workspace containing *project_dir* (longest match)."""
        target = abspath(str(project_dir))
        best: Workspace | None = None
        for ws in self.workspaces.values():
            if target == ws.path or ws.path in target.parents:
                if best is None or len(ws.path.parts) > len(best.path.parts):
                    best = ws

        return best

    # --- per-project resolution ----------------------------------------------

    def project_annotation(self, workspace: Workspace, name: str) -> ProjectAnnotation:
        """Merge a project's registry annotation with its project-local config.

        Project-local scalars win; inline hooks accumulate (registry first,
        then project-local).
        """
        base = workspace.projects.get(name, ProjectAnnotation())
        local = _parse_project_local(workspace.path / name / ".wtm" / "config.toml")

        return ProjectAnnotation(
            description=local.description or base.description,
            base_branch=local.base_branch or base.base_branch,
            hooks=_merge_hooks(base.hooks, local.hooks),
        )

    def base_branch_for(self, workspace: Workspace, name: str) -> str | None:
        """Resolve a project's configured base branch (most-specific first)."""
        return self.base_branch_provenance(workspace, name)[0]

    def base_branch_provenance(
        self, workspace: Workspace | None, name: str
    ) -> tuple[str | None, str]:
        """Resolve a project's effective base branch and the tier it came from.

        The tier is one of ``project`` (project-local file or registry
        annotation), ``workspace``, ``global``, or ``(none)``. The project-local
        file and the registry annotation share the ``project`` tier: both are
        per-project overrides and the annotation is not carried with a finer
        scope once merged.
        """
        annotation = (
            self.project_annotation(workspace, name)
            if workspace is not None
            else ProjectAnnotation()
        )
        if annotation.base_branch:
            return annotation.base_branch, "project"
        if workspace is not None and workspace.base_branch:
            return workspace.base_branch, "workspace"
        if self.default_base_branch:
            return self.default_base_branch, "global"

        return None, "(none)"

    def scope_hook_spec(self, scope: Scope, project_dir: Path, worktree_dir: Path) -> HookSpec:
        """Return the inline :class:`HookSpec` for *scope* given a target's dirs."""
        if scope is Scope.SYSTEM:
            return self.system_scope_hooks
        if scope is Scope.USER:
            return self.user_scope_hooks
        if scope is Scope.WORKSPACE:
            ws = self.workspace_of(project_dir)

            return ws.hooks if ws is not None else HookSpec()
        if scope is Scope.PROJECT:
            ws = self.workspace_of(project_dir)
            if ws is None:
                return HookSpec()

            return self.project_annotation(ws, project_dir.name).hooks

        return _parse_worktree_local(worktree_dir / ".wtm" / "config.toml")

    # --- loading -------------------------------------------------------------

    @classmethod
    def load(cls, *, cwd: Path | None = None) -> Config:
        """Load and resolve config from files + env for one invocation.

        :param cwd: the current directory for cwd-based workspace inference
            (defaults to the live cwd, or ``None`` when the cwd has been removed
            -- then the cwd tier is simply skipped rather than crashing).

        The active workspace is resolved from ``$WTM_WORKSPACE > cwd >
        default_workspace > sole workspace``; there is no ``-w`` flag -- a
        WORKSPACE named in a command's positionals overrides this per-target
        (see :mod:`wtm.target`).
        """
        cwd = cwd if cwd is not None else current_dir(resolve=False)
        no_config = _flag_env("WTM_NO_CONFIG")
        no_system = no_config or _flag_env("WTM_NO_SYSTEM")

        system = {} if no_system else _read_toml(_system_config_path())
        user = {} if no_config else _read_toml(_user_config_path())

        registry = _build_registry(system, user)
        default_workspace = user.get("default_workspace") or system.get("default_workspace")
        active = _resolve_active(registry, cwd=cwd, default_workspace=default_workspace)

        xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        system_dir = Path(os.environ.get("WTM_SYSTEM_HOOKS_DIR") or "/etc/wtm/hooks")
        user_dir = Path(os.environ.get("WTM_USER_HOOKS_DIR") or xdg / "wtm" / "hooks")

        return cls(
            workspaces=registry,
            active=active,
            system_scope_hooks=_parse_hooks(system.get("hooks")),
            user_scope_hooks=_parse_hooks(user.get("hooks")),
            _system_hooks_dir=system_dir,
            _user_hooks_dir=user_dir,
            default_base_branch=user.get("base_branch") or system.get("base_branch"),
            default_workspace=default_workspace,
        )

    # --- resolved view -------------------------------------------------------

    def resolve_view(self, *, cwd: Path | None = None) -> ResolvedConfig:
        """Materialise the effective config for the active cwd chain.

        Descent happens only in the active workspace, and only as deep as the
        cwd sits: the cwd-bound project (if cwd is under one) carries its merged
        effective annotation, and its worktree tier is attached only when cwd is
        inside an actual worktree. ``None`` *cwd* (the shell dir was removed)
        simply skips the descent.
        """
        cwd = cwd if cwd is not None else current_dir(resolve=False)
        active_ws = self.workspaces.get(self.active) if self.active is not None else None

        project = self._resolve_cwd_project(active_ws, cwd) if active_ws is not None else None

        return ResolvedConfig(
            workspaces=self.workspaces,
            active=self.active,
            default_base_branch=self.default_base_branch,
            active_workspace=active_ws,
            project=project,
        )

    def _resolve_cwd_project(self, ws: Workspace, cwd: Path | None) -> ResolvedProject | None:
        """Resolve the cwd-bound project under *ws*, or ``None`` when cwd is elsewhere."""
        if cwd is None:
            return None
        project_dir = project_dir_for(ws.path, abspath(str(cwd)))
        if project_dir is None:
            return None

        annotation = self.project_annotation(ws, project_dir.name)
        effective, source = self.base_branch_provenance(ws, project_dir.name)

        return ResolvedProject(
            name=project_dir.name,
            dir=project_dir,
            exists=(project_dir / ".git").is_dir(),
            description=annotation.description,
            base_branch=annotation.base_branch,
            effective_base_branch=effective,
            base_branch_source=source,
            hooks=annotation.hooks,
            worktree=self._resolve_cwd_worktree(project_dir, abspath(str(cwd))),
        )

    def _resolve_cwd_worktree(self, project_dir: Path, cwd: Path) -> ResolvedWorktree | None:
        """Resolve the cwd-bound worktree under *project_dir*, else ``None``."""
        worktrees_dir = project_dir / "worktrees"
        if not within(cwd, worktrees_dir) or cwd == worktrees_dir:
            return None
        rel = cwd.relative_to(worktrees_dir)
        wt_dir = worktrees_dir / rel.parts[0]
        name = worktree_name(wt_dir, worktrees_dir) or wt_dir.name

        return ResolvedWorktree(
            name=name,
            dir=wt_dir,
            hooks=_parse_worktree_local(wt_dir / ".wtm" / "config.toml"),
        )


def resolve_project_dir(config: Config, workspace: str | None, project: str | None) -> Path:
    """Resolve a project dir (explicit WORKSPACE/PROJECT or the cwd fallback).

    A named *workspace* is looked up in the registry; otherwise the active
    workspace's root is used (mirroring the target grammar's ``<= 2``-token
    fallback in :mod:`wtm.target`).
    """
    root = config.workspace_root(workspace)
    if project:
        valid_project_name(project)

        return root / project

    cwd = current_dir(resolve=False)
    project_dir = project_dir_for(root, cwd) if cwd is not None else None
    if project_dir is None:
        raise WtmError(f"no PROJECT given and cwd is not under {root}")

    return project_dir


# --- registry construction ---------------------------------------------------


def _build_registry(system: dict, user: dict) -> dict[str, Workspace]:
    """Build the workspace registry from system + user files (user wins)."""
    registry: dict[str, Workspace] = {}
    for scope, table in ((Scope.SYSTEM, system), (Scope.USER, user)):
        for name, entry in (table.get("workspaces") or {}).items():
            registry[name] = _merge_workspace(registry.get(name), name, entry, scope)

    # Fold each workspace's own local file on top of the registry declaration.
    for name, ws in list(registry.items()):
        local = _read_toml(ws.path / ".wtm" / "config.toml")
        registry[name] = _apply_workspace_local(ws, local)

    return registry


def _merge_workspace(prev: Workspace | None, name: str, entry: dict, scope: Scope) -> Workspace:
    """Merge a ``[workspaces.NAME]`` entry onto any earlier-scope declaration."""
    if "path" not in entry and prev is None:
        raise WtmError(f"workspace '{name}' has no path")
    path = expand_path(entry["path"]) if "path" in entry else prev.path
    hooks = _parse_hooks(entry.get("hooks"))
    projects = _parse_projects(entry.get("projects"))

    if prev is None:
        return Workspace(
            name=name,
            path=path,
            description=entry.get("description"),
            base_branch=entry.get("base_branch"),
            hooks=hooks,
            projects=projects,
            scope=scope,
        )

    return replace(
        prev,
        path=path,
        description=entry.get("description") or prev.description,
        base_branch=entry.get("base_branch") or prev.base_branch,
        hooks=_merge_hooks(prev.hooks, hooks),
        projects=_merge_projects(prev.projects, projects),
        scope=scope,
    )


def _apply_workspace_local(ws: Workspace, local: dict) -> Workspace:
    """Merge a workspace's own ``.wtm/config.toml`` onto its registry entry."""
    if not local:
        return ws

    return replace(
        ws,
        description=local.get("description") or ws.description,
        base_branch=local.get("base_branch") or ws.base_branch,
        hooks=_merge_hooks(ws.hooks, _parse_hooks(local.get("hooks"))),
        projects=_merge_projects(ws.projects, _parse_projects(local.get("projects"))),
    )


# --- active workspace resolution ---------------------------------------------


def _resolve_active(
    registry: dict[str, Workspace],
    *,
    cwd: Path | None,
    default_workspace: str | None,
) -> str | None:
    """Resolve the active workspace name per the documented precedence.

    ``$WTM_WORKSPACE > cwd (longest match) > default_workspace > sole workspace
    > None``. A WORKSPACE named in a command's positionals overrides this
    per-target (see :mod:`wtm.target`); there is no ``-w`` flag. A ``None`` *cwd*
    (the shell's directory was removed) simply skips the cwd tier.
    """
    selector = os.environ.get("WTM_WORKSPACE")
    if selector:
        if selector not in registry:
            raise WtmError(f"unknown workspace: '{selector}'")

        return selector

    matched = _workspace_for_cwd(registry, cwd) if cwd is not None else None
    if matched is not None:
        return matched

    if default_workspace:
        if default_workspace not in registry:
            raise WtmError(f"default_workspace '{default_workspace}' is not declared")

        return default_workspace

    if len(registry) == 1:
        return next(iter(registry))

    return None


def _workspace_for_cwd(registry: dict[str, Workspace], cwd: Path) -> str | None:
    """Return the workspace whose path best contains *cwd* (longest match).

    Ties on an identical path (two names, same dir) are a hard error.
    """
    best: list[str] = []
    best_len = -1
    for name, ws in registry.items():
        if cwd == ws.path or ws.path in cwd.parents:
            depth = len(ws.path.parts)
            if depth > best_len:
                best, best_len = [name], depth
            elif depth == best_len and ws.path == registry[best[0]].path:
                best.append(name)
    if not best:
        return None
    if len(best) > 1:
        raise WtmError(
            f"cwd matches multiple workspaces: {', '.join(sorted(best))} "
            "(name the WORKSPACE, or set WTM_WORKSPACE)"
        )

    return best[0]


# --- TOML parsing helpers ----------------------------------------------------


def _system_config_path() -> Path:
    """The system config file path (env override or default)."""
    return Path(os.environ.get("WTM_SYSTEM_CONFIG") or "/etc/wtm/config.toml")


def _user_config_path() -> Path:
    """The user config file path (env override or XDG default)."""
    if override := os.environ.get("WTM_CONFIG"):
        return Path(override)
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")

    return xdg / "wtm" / "config.toml"


def _read_toml(path: Path) -> dict:
    """Parse a TOML file; missing file yields ``{}``, a parse error is fatal."""
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise WtmError(f"config error in {path}: {exc}") from exc


def _parse_hooks(table: dict | None) -> HookSpec:
    """Parse a ``[hooks]``-shaped table into a :class:`HookSpec`."""
    if not table:
        return HookSpec()
    on: dict[str, list[str]] = {}
    for event, value in (table.get("on") or {}).items():
        on[event] = [value] if isinstance(value, str) else list(value)
    dir_value = table.get("dir")

    return HookSpec(
        on=on,
        builtins=table.get("builtins"),
        dir=expand_path(dir_value) if dir_value else None,
    )


def _parse_projects(table: dict | None) -> dict[str, ProjectAnnotation]:
    """Parse a ``[projects]`` table into name -> :class:`ProjectAnnotation`."""
    result: dict[str, ProjectAnnotation] = {}
    for name, entry in (table or {}).items():
        result[name] = ProjectAnnotation(
            description=entry.get("description"),
            base_branch=entry.get("base_branch"),
            hooks=_parse_hooks(entry.get("hooks")),
        )

    return result


def _parse_project_local(path: Path) -> ProjectAnnotation:
    """Parse a project-local ``.wtm/config.toml`` ``[project]`` table."""
    table = _read_toml(path).get("project") or {}

    return ProjectAnnotation(
        description=table.get("description"),
        base_branch=table.get("base_branch"),
        hooks=_parse_hooks(table.get("hooks")),
    )


def _parse_worktree_local(path: Path) -> HookSpec:
    """Parse a worktree-local ``.wtm/config.toml`` ``[hooks]`` table."""
    return _parse_hooks(_read_toml(path).get("hooks"))


def _merge_hooks(a: HookSpec, b: HookSpec) -> HookSpec:
    """Merge two same-scope hook specs: accumulate commands, more-specific wins."""
    on: dict[str, list[str]] = {event: list(cmds) for event, cmds in a.on.items()}
    for event, cmds in b.on.items():
        on.setdefault(event, []).extend(cmds)

    return HookSpec(
        on=on,
        builtins=b.builtins if b.builtins is not None else a.builtins,
        dir=b.dir or a.dir,
    )


def _merge_projects(
    a: dict[str, ProjectAnnotation], b: dict[str, ProjectAnnotation]
) -> dict[str, ProjectAnnotation]:
    """Merge two project-annotation maps by name (b's scalars win; hooks accumulate)."""
    result = dict(a)
    for name, ann in b.items():
        prev = result.get(name)
        if prev is None:
            result[name] = ann
        else:
            result[name] = ProjectAnnotation(
                description=ann.description or prev.description,
                base_branch=ann.base_branch or prev.base_branch,
                hooks=_merge_hooks(prev.hooks, ann.hooks),
            )

    return result


def _flag_env(name: str) -> bool:
    """Return whether an env var is set to a truthy value."""
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no")
