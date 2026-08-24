"""The :class:`Config` facade: one resolved snapshot offering every tier.

``Config.load`` resolves configuration from files + env for one invocation;
the instance exposes the arbitrary-key facility (raw reads/writes at an
explicit :class:`ConfigAddress`), the typed read view (annotations,
provenance), the cwd-inferred generic-target surface, and the structural
write tier. The engines underneath live in the sibling modules; this module
composes them.
"""

from __future__ import annotations

__all__ = [
    "Config",
    "GenericTarget",
    "resolve_project_dir",
]

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import tomlkit

from wtm.config import slice as slice_view
from wtm.config.addressing import (
    ConfigAddress,
    Key,
    Scope,
    Settings,
    global_address,
    system_config_path,
    user_config_path,
)
from wtm.config.engine import (
    Probe,
    TierProbe,
    deep_merge,
    descend_value,
    dotted_candidates,
    fold_tiers,
    merge_locality,
    read_table,
)
from wtm.config.entities import (
    EntityHandle,
    ProjectEntity,
    WorkspaceEntity,
    WorktreeEntity,
    build_global_entity,
)
from wtm.config.generic import parse_inheritable_element
from wtm.config.manifest import PluginManifest
from wtm.config.model import (
    FLOOR_TIERS,
    HOOKS_POLICY_CELLS,
    INHERIT_FLOORS,
    INHERITABLE_DECL_KEY,
    INHERITABLE_KEYS,
    PLUGINS_DECL_KEY,
    RESERVED_KEYS,
    TYPED_INHERITABLE_KEYS,
    ConfigSlice,
    HookSpec,
    ProjectAnnotation,
    ResolvedWorktree,
    SliceTarget,
    SourceRung,
    Workspace,
    annotation_from_payload,
    check_kind,
    parse_hooks,
    str_or_none,
)
from wtm.config.registry import build_registry, global_probes, global_scalar, resolve_active
from wtm.config.store import Store, existing_entry, key_parts, set_leaf, workspaces_table
from wtm.config.targets import GenericTarget
from wtm.config.values import RemovableValue, TomlValue
from wtm.config.writes import (
    project_default_scope,
    worktree_default_scope,
)
from wtm.core import (
    abspath,
    current_dir,
    project_dir_for,
    valid_project_name,
    within,
    worktree_name,
)
from wtm.exceptions import KindConflictError, NoActiveWorkspaceError, WtmError

# --- config ------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """Resolved configuration for one wtm invocation: reads, writes, discovery."""

    workspaces: dict[str, Workspace]
    active: str | None
    system_scope_hooks: HookSpec
    user_scope_hooks: HookSpec
    _system_hooks_dir: Path
    _user_hooks_dir: Path
    _store: Store
    default_base_branch: str | None = None
    default_workspace: str | None = None
    _settings: Settings = field(default_factory=Settings.from_env)
    _plugins: tuple[PluginManifest, ...] = ()

    # --- active workspace ----------------------------------------------------

    @property
    def active_workspace(self) -> Workspace:
        """The active workspace, or a helpful error when none is resolved."""
        if self.active is None:
            if not self.workspaces:
                raise NoActiveWorkspaceError(
                    "no workspaces declared; add one to your wtm config "
                    "([workspaces.NAME] path = ...)"
                )

            raise NoActiveWorkspaceError(
                "no active workspace; pass WORKSPACE PROJECT NAME, set WTM_WORKSPACE, "
                "cd into a workspace, or set a default-workspace"
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
    def no_hooks(self) -> bool:
        """Whether hook firing is disabled for this invocation (``WTM_NO_HOOKS``)."""
        return self._settings.no_hooks

    @property
    def flushed_config_files(self) -> list[Path]:
        """The config files this invocation actually wrote, in flush order.

        Feeds the ``post-config`` hook event (see
        :func:`wtm.hooks.run_config_hooks`); empty for read-only invocations.
        """
        return list(self._store.flushed)

    # --- builtin hook-discovery dirs -----------------------------------------

    def hooks_dir(
        self,
        scope: Scope,
        *,
        ws: Workspace | None = None,
        project_dir: Path | None = None,
        worktree_dir: Path | None = None,
    ) -> Path:
        """The builtin hook-discovery directory for *scope*: the single authority.

        Each tier's ``[hooks] dir`` override wins; otherwise the tier default --
        system ``$WTM_SYSTEM_HOOKS_DIR`` (default ``/etc/wtm/hooks``), user XDG,
        workspace ``<ws>/.wtm/hooks``, project ``<project>/.wtm/hooks``,
        worktree ``<worktree>/.wtm/hooks``. Firing and management both resolve
        through here, so an override is honored identically on both sides. The
        workspace scope resolves against *ws* -- firing passes the workspace
        *containing* the event's subject, management the active one; with no
        *ws* (an undeclared project) it falls back to *project_dir*'s parent.
        """
        if scope is Scope.SYSTEM:
            return self.system_scope_hooks.dir or self._system_hooks_dir

        if scope is Scope.USER:
            return self.user_scope_hooks.dir or self._user_hooks_dir

        if scope is Scope.WORKSPACE:
            if ws is not None and ws.hooks.dir is not None:
                return ws.hooks.dir
            if ws is not None:
                return ws.path / ".wtm" / "hooks"
            if project_dir is not None:
                return project_dir.parent / ".wtm" / "hooks"

            raise WtmError("workspace hooks dir needs a workspace or project dir")

        if scope is Scope.PROJECT:
            if project_dir is None:
                raise WtmError("project hooks dir needs a project dir")

            spec = self.scope_hook_spec(
                scope, project_dir, worktree_dir or project_dir / "worktrees"
            )
            return spec.dir or project_dir / ".wtm" / "hooks"

        if project_dir is None or worktree_dir is None:
            raise WtmError("worktree hooks dir needs a project dir and worktree dir")

        spec = self.scope_hook_spec(scope, project_dir, worktree_dir)

        return spec.dir or worktree_dir / ".wtm" / "hooks"

    # --- plugins -------------------------------------------------------------

    def attach_plugins(self, manifests: Sequence[PluginManifest]) -> Config:
        """Bind resolved plugin *manifests* onto this snapshot (a replaced copy).

        The CLI resolves manifests once per invocation (entry point first, then
        the ``<command> wtm-manifest`` subprocess) right after :meth:`load` and
        attaches them here, so hook firing, namespace ownership, and the
        inheritable union all see the active set. Library users who never
        attach get today's behavior unchanged.
        """
        return replace(self, _plugins=tuple(sorted(manifests, key=lambda m: m.name)))

    def _plugin_probes(
        self,
        *,
        ws: Workspace | None = None,
        project_dir: Path | None = None,
        worktree_dir: Path | None = None,
    ) -> list[Probe]:
        """The top-level probe chain for plugin activation, most-local-first.

        Unlike entity cascades these probes never descend into a registry
        table: a ``[plugins]`` cell is meaningful only at a scope file's top
        level. The worktree/project rungs appear only when the caller supplies
        those dirs.
        """
        probes: list[Probe] = []
        if worktree_dir is not None:
            probes.append(Probe(Scope.WORKTREE, worktree_dir / ".wtm" / "config.toml", ()))
        if project_dir is not None:
            probes.append(Probe(Scope.PROJECT, project_dir / ".wtm" / "config.toml", ()))
        if ws is not None:
            probes.append(Probe(Scope.WORKSPACE, ws.path / ".wtm" / "config.toml", ()))
        probes.append(Probe(Scope.USER, user_config_path(), ()))
        probes.append(Probe(Scope.SYSTEM, system_config_path(), ()))

        return probes

    def plugin_registry(
        self,
        *,
        ws: Workspace | None = None,
        project_dir: Path | None = None,
        worktree_dir: Path | None = None,
    ) -> dict[str, bool]:
        """The ``[plugins]`` activation table folded most-local-wins per cell.

        Each scope file's top-level ``[plugins]`` table answers one boolean per
        plugin name; the most-local scope carrying a cell wins for that name
        (so a workspace may enable or disable one plugin without touching the
        others declared above it). A non-table ``[plugins]`` or a non-boolean
        cell is a hard error, matching the read-side kind rules.
        """
        registry: dict[str, bool] = {}
        probes = self._plugin_probes(ws=ws, project_dir=project_dir, worktree_dir=worktree_dir)
        for probe in probes:
            node = read_table(self._store, probe).get(PLUGINS_DECL_KEY)
            if node is None:
                continue
            if not isinstance(node, dict):
                raise KindConflictError(
                    f"config key '{PLUGINS_DECL_KEY}' must be a table ({probe.file})"
                )
            for name, value in node.items():
                if name in registry:
                    continue

                if not isinstance(value, bool):
                    raise WtmError(
                        f"[{PLUGINS_DECL_KEY}] '{name}' must be a boolean ({probe.file})"
                    )
                registry[name] = value

        return registry

    def active_plugins(
        self,
        *,
        ws: Workspace | None = None,
        project_dir: Path | None = None,
        worktree_dir: Path | None = None,
    ) -> tuple[PluginManifest, ...]:
        """The attached manifests whose activation cell is true for this context."""
        registry = self.plugin_registry(ws=ws, project_dir=project_dir, worktree_dir=worktree_dir)

        return tuple(m for m in self._plugins if registry.get(m.name) is True)

    def plugin_namespaces(
        self,
        *,
        ws: Workspace | None = None,
        project_dir: Path | None = None,
        worktree_dir: Path | None = None,
    ) -> frozenset[str]:
        """The arbitrary-key heads owned by plugins active in this context."""
        heads: set[str] = set()
        for manifest in self.active_plugins(
            ws=ws, project_dir=project_dir, worktree_dir=worktree_dir
        ):
            heads.update(manifest.namespaces)

        return frozenset(heads)

    def plugin_commands(
        self,
        event: str,
        *,
        ws: Workspace | None = None,
        project_dir: Path | None = None,
        worktree_dir: Path | None = None,
    ) -> list[tuple[str, str]]:
        """Hook commands the active plugins declare for *event*, ``(name, cmd)``.

        Manifests run in sorted-name order; within a manifest, declared order.
        The firing engine treats each command exactly like an inline config
        hook (same env contract, same strict/non-fatal rules).
        """
        rows: list[tuple[str, str]] = []
        for manifest in self.active_plugins(
            ws=ws, project_dir=project_dir, worktree_dir=worktree_dir
        ):
            rows.extend((manifest.name, cmd) for cmd in manifest.hooks.get(event, ()))

        return rows

    def registered_plugin_names(self) -> list[str]:
        """Every plugin named by any known scope's ``[plugins]`` table (union).

        The management-surface view: scans the system and user files plus the
        active workspace's (and, when inferrable, the cwd's project/worktree)
        files, so ``plugin list`` sees registrations regardless of where they
        sit. Activation still resolves per context via :meth:`plugin_registry`.
        """
        names: set[str] = set()
        for _scope, probe in self._known_plugin_probes():
            node = read_table(self._store, probe).get(PLUGINS_DECL_KEY)
            if isinstance(node, dict):
                names.update(node)

        return sorted(names)

    def registration_sites(self, name: str) -> dict[Scope, bool]:
        """Where *name* has an activation cell among known scopes, mapped to its value."""
        sites: dict[Scope, bool] = {}
        for scope, probe in self._known_plugin_probes():
            node = read_table(self._store, probe).get(PLUGINS_DECL_KEY)
            if isinstance(node, dict) and name in node:
                sites[scope] = bool(node[name])

        return sites

    def _known_plugin_probes(self) -> list[tuple[Scope, Probe]]:
        """(scope, top-level probe) for every file a plugin cell may sit in."""
        probes: list[tuple[Scope, Probe]] = [
            (Scope.SYSTEM, Probe(Scope.SYSTEM, system_config_path(), ())),
            (Scope.USER, Probe(Scope.USER, user_config_path(), ())),
        ]
        ws = self._active_ws_or_none()
        if ws is not None:
            probe = Probe(Scope.WORKSPACE, ws.path / ".wtm" / "config.toml", ())
            probes.append((Scope.WORKSPACE, probe))

        location = slice_view.cwd_location(self)
        if location is not None:
            loc_ws = self.workspaces.get(location.workspace)
            if loc_ws is not None and location.project is not None:
                project_dir = loc_ws.path / location.project
                proj_probe = Probe(Scope.PROJECT, project_dir / ".wtm" / "config.toml", ())
                probes.append((Scope.PROJECT, proj_probe))
                if location.name is not None:
                    wt_file = project_dir / "worktrees" / location.name / ".wtm" / "config.toml"
                    probes.append((Scope.WORKTREE, Probe(Scope.WORKTREE, wt_file, ())))

        return probes

    def set_plugin_cell(self, name: str, value: bool, scope: Scope) -> ConfigAddress:
        """Write *name*'s activation cell into *scope*'s file top level."""
        address = self._plugin_address(scope)
        self.set_value(address, (PLUGINS_DECL_KEY, name), value)

        return address

    def unset_plugin_cell(self, name: str, scope: Scope) -> ConfigAddress:
        """Remove *name*'s activation cell from *scope*'s file (no-op if absent)."""
        address = self._plugin_address(scope)
        self.unset_value(address, (PLUGINS_DECL_KEY, name))

        return address

    def unset_plugin_cell_found(
        self,
        name: str,
        *,
        ws: Workspace | None = None,
        project_dir: Path | None = None,
        worktree_dir: Path | None = None,
    ) -> Scope:
        """Remove *name*'s cell from the most-local scope carrying it.

        :raises WtmError: when no known scope declares the plugin.
        """
        probes = self._plugin_probes(ws=ws, project_dir=project_dir, worktree_dir=worktree_dir)
        for probe in probes:
            node = read_table(self._store, probe).get(PLUGINS_DECL_KEY)
            if not (isinstance(node, dict) and name in node):
                continue

            if probe.scope in (Scope.USER, Scope.SYSTEM):
                address = global_address(probe.scope)
            else:
                address = ConfigAddress(probe.scope, probe.file.parent.parent)
            self.unset_value(address, (PLUGINS_DECL_KEY, name))

            return probe.scope

        raise WtmError(f"plugin '{name}' is not registered in any known scope")

    def seed_plugin_defaults(
        self, manifest: PluginManifest, scope: Scope
    ) -> list[tuple[str, object]]:
        """Write *manifest*'s default values at *scope*'s file top level.

        Returns the ``(dotted key, value)`` pairs written so the caller can
        report them. Seeding is a one-time materialization: afterwards the
        values are ordinary user-owned config, untouched by ``plugin remove``.
        """
        address = self._plugin_address(scope)
        written: list[tuple[str, object]] = []
        for parts, value in manifest.defaults:
            self.set_value(address, parts, value)
            written.append((".".join(parts), value))

        return written

    def _plugin_address(self, scope: Scope) -> ConfigAddress:
        """The top-level file address for a plugin cell at *scope*."""
        if scope is Scope.SYSTEM or scope is Scope.USER:
            return global_address(scope)
        if scope is Scope.WORKSPACE:
            return ConfigAddress.workspace(self.active_workspace.path)
        if scope is Scope.PROJECT:
            return ConfigAddress.project(resolve_project_dir(self, None, None))
        location = slice_view.cwd_location(self)
        if (
            location is not None
            and location.name is not None
            and (ws := self.workspaces.get(location.workspace)) is not None
            and location.project is not None
        ):
            wt_dir = ws.path / location.project / "worktrees" / location.name

            return ConfigAddress.worktree(wt_dir)

        raise WtmError(f"'{scope.value}' scope needs an entity dir for plugin cells")

    def _active_ws_or_none(self) -> Workspace | None:
        """The active workspace when one resolves, else ``None`` (never raises)."""
        try:
            return self.active_workspace
        except NoActiveWorkspaceError:
            return None

    def cwd_plugin_dirs(self) -> tuple[Path | None, Path | None]:
        """Best-effort ``(project_dir, worktree_dir)`` for the cwd's plugin context.

        The default activation context for whole-invocation concerns (the CLI
        bootstrap): the cwd-inferred project and worktree, or ``None``s when
        the cwd sits outside any project/worktree.
        """
        location = slice_view.cwd_location(self)
        if location is None:
            return None, None

        ws = self.workspaces.get(location.workspace)
        if ws is None or location.project is None:
            return None, None

        project_dir = ws.path / location.project

        return (
            project_dir,
            project_dir / "worktrees" / location.name if location.name else None,
        )

    # --- arbitrary-key facility ----------------------------------------------

    def get_value(self, address: ConfigAddress, key: Key) -> object | None:
        """Read one scalar at *key* from the file *address* names (raw, unmerged).

        *key* is a bare key for a top-level scalar, or a tuple of parts for a
        nested table (e.g. ``("project", "base-branch")``). Returns ``None``
        when the scope's file or the key is absent. The value is whatever TOML
        parsed (str/int/float/bool/dates) -- reads are native-typed, and
        typed writes land as their native kind too (strings by default).
        """
        return self._store.get(address.file, key)

    def set_value(self, address: ConfigAddress, key: Key, value: TomlValue) -> None:
        """Write one scalar at *key* into the file *address* names.

        The scope is named explicitly by the caller; the file (and its parent
        dirs) is created on first write. *value* is any native TOML scalar --
        a string by default, or a typed scalar via :mod:`wtm.config.values`.
        Refused under ``WTM_NO_CONFIG``.
        """
        self._store.set(address.file, key, value)

    def unset_value(self, address: ConfigAddress, key: Key) -> None:
        """Remove one scalar at *key* from the file *address* names (a no-op if absent)."""
        self._store.unset(address.file, key)

    def add_to_array(self, address: ConfigAddress, key: Key, value: TomlValue | Mapping) -> None:
        """Add *value* to the array at *key* in the file *address* names.

        Set semantics: the add is idempotent (a value already present is a
        no-op). A scalar *value* may be any native TOML type; a mapping
        appends an inline-table element. Read-modify-write under a per-file
        lock; the file is created on first add. Refused under
        ``WTM_NO_CONFIG``.
        """
        self._store.array_add(address.file, key, value)

    def remove_from_array(self, address: ConfigAddress, key: Key, value: RemovableValue) -> None:
        """Strip every occurrence of *value* from the array at *key* (a no-op if absent).

        Scalars match by native equality, so a typed element is removed by
        its own kind of value.
        """
        self._store.array_remove(address.file, key, value)

    def set_table_value(
        self, address: ConfigAddress, key: Key, subkey: str, value: TomlValue
    ) -> None:
        """Set *subkey* = *value* in the table at *key* in the file *address* names.

        Set semantics: an empty table is created on first write; a non-table
        value already at *key* is a kind clash. Read-modify-write under a
        per-file lock. Refused under ``WTM_NO_CONFIG``.
        """
        self._store.table_set(address.file, key, subkey, value)

    def unset_table_value(self, address: ConfigAddress, key: Key, subkey: str) -> None:
        """Remove *subkey* from the table at *key* (a no-op if the key or sub-key is absent)."""
        self._store.table_unset(address.file, key, subkey)

    # --- scope write targets -------------------------------------------------

    def project_write_target(
        self, workspace: Workspace, name: str, scope: Scope
    ) -> tuple[ConfigAddress, tuple[str, ...]]:
        """Resolve where a project annotation lands at *scope*: (address, key prefix).

        Mirrors the project probe table's inverse: the address names the file for
        *scope* and the prefix is the table path an own scalar sits under there
        (empty at project-local top-level). ``key`` is appended by the caller.
        """
        ws_path = workspace.path
        if scope is Scope.PROJECT:
            return ConfigAddress.project(ws_path / name), ()

        if scope is Scope.WORKSPACE:
            return ConfigAddress.workspace(ws_path), ("projects", name)

        if scope in (Scope.USER, Scope.SYSTEM):
            return global_address(scope), ("workspaces", workspace.name, "projects", name)

        raise WtmError(f"cannot write a project annotation at {scope} scope")

    def workspace_write_target(
        self, workspace: Workspace, scope: Scope
    ) -> tuple[ConfigAddress, tuple[str, ...]]:
        """Resolve where a workspace annotation lands at *scope*: (address, key prefix)."""
        if scope is Scope.WORKSPACE:
            return ConfigAddress.workspace(workspace.path), ()

        if scope in (Scope.USER, Scope.SYSTEM):
            return global_address(scope), ("workspaces", workspace.name)

        raise WtmError(f"cannot write a workspace annotation at {scope} scope")

    def worktree_write_target(
        self, workspace: Workspace | None, project_dir: Path, name: str, scope: Scope
    ) -> tuple[ConfigAddress, tuple[str, ...]]:
        """Resolve where a worktree annotation lands at *scope*: (address, key prefix).

        The inverse of the worktree probe table: worktree-local top-level, then
        project-local ``[worktrees.NAME]``, then workspace-local
        ``[projects.PROJECT.worktrees.NAME]``, then user/system
        ``[workspaces.W.projects.PROJECT.worktrees.NAME]``. The workspace/user/
        system tiers require the project's declared *workspace*.
        """
        wt_dir = project_dir / "worktrees" / name
        if scope is Scope.WORKTREE:
            return ConfigAddress.worktree(wt_dir), ()

        if scope is Scope.PROJECT:
            return ConfigAddress.project(project_dir), ("worktrees", name)

        if workspace is None:
            raise WtmError(f"project is not under a declared workspace: {project_dir}")

        if scope is Scope.WORKSPACE:
            return (
                ConfigAddress.workspace(workspace.path),
                ("projects", project_dir.name, "worktrees", name),
            )

        if scope in (Scope.USER, Scope.SYSTEM):
            return (
                global_address(scope),
                ("workspaces", workspace.name, "projects", project_dir.name, "worktrees", name),
            )

        raise WtmError(f"cannot write a worktree annotation at {scope} scope")

    # --- arbitrary-key surface (cwd-inferred entity) -------------------------

    def resolve_generic_target(self, cwd: Path | None) -> GenericTarget:
        """Resolve the cwd-inferred entity an arbitrary ``config`` key acts on.

        Picks the deepest entity the cwd sits inside -- worktree, else project,
        else the active workspace -- and returns a :class:`GenericTarget` naming
        it: its label, its locality probe cascade (most-local-first), the
        per-scope write-target resolver, its allowed write scopes, and the
        auto-default scope (the deepest whose dir exists). The active workspace
        is the floor; with none resolved this raises the no-active-workspace
        error.
        """
        ws = self.active_workspace
        project_dir = project_dir_for(ws.path, abspath(str(cwd))) if cwd is not None else None
        if project_dir is not None:
            wt_name = self._cwd_worktree_name(project_dir, abspath(str(cwd)))
            if wt_name is not None:
                return self._worktree_generic_target(ws, project_dir, wt_name)

            return self._project_generic_target(ws, project_dir)

        return self._workspace_generic_target(ws)

    def global_generic_target(self) -> GenericTarget:
        """Resolve the global-tier :class:`GenericTarget` (top-level user/system keys).

        The entity-less counterpart to :meth:`resolve_generic_target`: the
        target's write sites are USER/SYSTEM scope with an empty structural
        prefix -- the top level of the two non-entity files -- so an arbitrary
        key written through it is a true global default rather than a
        ``[workspaces...]``-nested annotation.
        """
        return build_global_entity(self)._target

    def resolve_generic_target_for(
        self, workspace: str, project: str | None = None, name: str | None = None
    ) -> GenericTarget:
        """Resolve a :class:`GenericTarget` from explicit entity names (locator-driven).

        The locator-driven counterpart to :meth:`resolve_generic_target`:
        instead of inferring the entity from a cwd, the entity is named
        explicitly by its workspace/project/worktree names -- as resolved by
        the locator cascade. Dispatches to the same per-tier helpers as the
        cwd path, so the resulting :class:`GenericTarget` is interchangeable.

        :param workspace: the declared workspace name (always required).
        :param project: the project name (required for a worktree target).
        :param name: the worktree name (set only for a worktree target).
        """
        ws = self.workspaces[workspace]
        if name is not None:
            if project is None:
                raise WtmError("a worktree target requires a project")

            return self._worktree_generic_target(ws, ws.path / project, name)
        if project is not None:
            return self._project_generic_target(ws, ws.path / project)

        return self._workspace_generic_target(ws)

    def _cwd_worktree_name(self, project_dir: Path, cwd: Path) -> str | None:
        """Return the worktree name if *cwd* sits inside one under *project_dir*."""
        worktrees_dir = project_dir / "worktrees"
        if not within(cwd, worktrees_dir) or cwd == worktrees_dir:
            return None

        rel = cwd.relative_to(worktrees_dir)
        wt_dir = worktrees_dir / rel.parts[0]

        return worktree_name(wt_dir, worktrees_dir) or wt_dir.name

    def _worktree_generic_target(
        self, ws: Workspace, project_dir: Path, name: str
    ) -> GenericTarget:
        """The generic target for a cwd-bound worktree."""
        wt_dir = project_dir / "worktrees" / name
        default = worktree_default_scope(wt_dir, project_dir, ws.path)

        return GenericTarget(
            label=f"worktree '{name}'",
            probes=self._worktree_probes(project_dir, name),
            write_target=lambda scope: self.worktree_write_target(ws, project_dir, name, scope),
            allowed_scopes=(
                Scope.WORKTREE,
                Scope.PROJECT,
                Scope.WORKSPACE,
                Scope.USER,
                Scope.SYSTEM,
            ),
            default_scope=default,
            inherit=self._inherit_map(
                ws, project_dir.name, own=self._worktree_probes(project_dir, name), worktree=name
            ),
            floors=self.inheritable_floors(ws, project_dir.name, worktree=name),
        )

    def _project_generic_target(self, ws: Workspace, project_dir: Path) -> GenericTarget:
        """The generic target for a cwd-bound project."""
        default = project_default_scope(project_dir, ws.path)

        return GenericTarget(
            label=f"project '{project_dir.name}'",
            probes=self._project_probes(ws, project_dir.name),
            write_target=lambda scope: self.project_write_target(ws, project_dir.name, scope),
            allowed_scopes=(Scope.PROJECT, Scope.WORKSPACE, Scope.USER, Scope.SYSTEM),
            default_scope=default,
            inherit=self._inherit_map(ws, project_dir.name, own=None),
            floors=self.inheritable_floors(ws, project_dir.name),
        )

    def _workspace_generic_target(self, ws: Workspace) -> GenericTarget:
        """The generic target for the active workspace (the cwd floor)."""
        default = Scope.WORKSPACE if ws.path.is_dir() else Scope.USER

        return GenericTarget(
            label=f"workspace '{ws.name}'",
            probes=self._workspace_probes(ws),
            write_target=lambda scope: self.workspace_write_target(ws, scope),
            allowed_scopes=(Scope.WORKSPACE, Scope.USER, Scope.SYSTEM),
            default_scope=default,
            inherit=self._inherit_map(ws, None, own=None),
            floors=self.inheritable_floors(ws, None),
        )

    def _inherit_map(
        self,
        ws: Workspace,
        project: str | None,
        *,
        own: list[Probe] | None,
        worktree: str | None = None,
    ) -> dict[str, list[TierProbe]]:
        """The tier-tagged inherit chain for every inheritable key of an entity.

        Maps each effective inheritable key (see :meth:`inheritable_keys`) to its
        most-local-first chain: the entity's *own* cascade rungs (tier ``None``,
        so its most-local value can win wholesale) followed by the shared
        ancestor tiers (:meth:`_ancestor_tiers`) -- then pruned of every rung
        below the key's floor (see :meth:`inheritable_floors`). The builtin
        ``base-branch`` floor (``project``) is what keeps it a project-tier
        concept with no worktree tier of its own, so ``config get base-branch``
        resolves the same rung every consumer sees via :meth:`provenance`.
        """
        ancestors = self._ancestor_tiers(ws, project)
        own_rungs = [TierProbe(None, p) for p in own] if own else []
        floors = self.inheritable_floors(ws, project, worktree=worktree)

        mapping: dict[str, list[TierProbe]] = {}
        for key in self.inheritable_keys(ws, project, worktree=worktree):
            chain: tuple[TierProbe, ...] = (*own_rungs, *ancestors)
            floor = floors.get(key)
            if floor is not None:
                # An own rung's tier is None only on a worktree target -- its
                # implicit tier there is exactly "worktree".
                limit = FLOOR_TIERS[floor]
                chain = tuple(tp for tp in chain if FLOOR_TIERS[tp.tier or "worktree"] >= limit)

            mapping[key] = list(chain)

        return mapping

    def _ancestor_tiers(self, ws: Workspace, project: str | None) -> list[TierProbe]:
        """The project/workspace/global ancestor rungs for an entity, most-local-first.

        Concatenates the project cascade (tier ``project``, when a *project* is
        given), the workspace-own cascade (tier ``workspace``), and the global
        top-level default (tier ``global``) -- the shared tail of every
        inheritable key's chain, the rungs :meth:`provenance` labels by tier.
        """
        rungs: list[TierProbe] = []
        if project is not None:
            rungs += [TierProbe("project", p) for p in self._project_probes(ws, project)]
        rungs += [TierProbe("workspace", p) for p in self._workspace_probes(ws)]
        rungs += [TierProbe("global", p) for p in global_probes()]

        return rungs

    def inheritable_keys(
        self, ws: Workspace | None, project: str | None, *, worktree: str | None = None
    ) -> frozenset[str]:
        """The effective non-entity-specific key set for an entity's resolution.

        The builtin :data:`INHERITABLE_KEYS`, unioned with every active
        plugin's declared elements, unioned with every user-declared
        ``inheritable`` array found along the entity's containment chain: the
        project cascade (when a *project* is given), the workspace-own cascade,
        and the global top-level. The declaration *accumulates* across sources
        (a workspace may opt a key in for all its projects), so a key is
        inheritable for an entity if any source names it.
        """
        return frozenset(self.inheritable_decls(ws, project, worktree=worktree))

    def inheritable_decls(
        self,
        ws: Workspace | None,
        project: str | None,
        *,
        worktree: str | None = None,
    ) -> dict[str, str | None]:
        """The effective inheritable keys mapped to their floors (``None`` = none).

        Same accumulation as :meth:`inheritable_keys`, but each element may
        carry a floor (an ``{ key = ..., floor = ... }`` entry). A malformed or
        refused element raises at read time (the same hard-error treatment a
        kind clash gets). Precedence per key: builtin floors first, then the
        active plugins' declarations (a baseline that travels with the
        plugin's activation), then explicit user declarations -- the probe
        scan below is most-local-first and its first sighting sticks, so a
        user-declared floor always outranks a plugin's.
        """
        decls: dict[str, str | None] = {k: INHERIT_FLOORS.get(k) for k in INHERITABLE_KEYS}

        # Explicit user declarations, most-local-first with first sighting
        # sticking -- they outrank everything below.
        user_decls: dict[str, str | None] = {}
        project_dir = ws.path / project if ws is not None and project is not None else None
        worktree_dir = (
            project_dir / "worktrees" / worktree
            if worktree is not None and project_dir is not None
            else None
        )
        probes: list[Probe] = []
        if ws is not None:
            if project is not None:
                probes += self._project_probes(ws, project)
            probes += self._workspace_probes(ws)
        probes += global_probes()
        for probe in probes:
            declared = read_table(self._store, probe).get(INHERITABLE_DECL_KEY)
            if not isinstance(declared, (list, tuple)):
                continue

            for element in declared:
                decl = parse_inheritable_element(element, file=probe.file)
                if decl.key not in user_decls:
                    user_decls[decl.key] = decl.floor

        # Plugin baseline: fills the gaps neither the builtins nor the user
        # named; it travels with the plugin's activation.
        for manifest in self.active_plugins(
            ws=ws, project_dir=project_dir, worktree_dir=worktree_dir
        ):
            for decl in manifest.inheritable:
                if decl.key not in user_decls and decl.key not in decls:
                    decls[decl.key] = decl.floor

        decls.update(user_decls)

        return decls

    def inheritable_floors(
        self, ws: Workspace | None, project: str | None, *, worktree: str | None = None
    ) -> dict[str, str]:
        """The effective floors only: inheritable keys that declare one."""
        return {
            key: floor
            for key, floor in self.inheritable_decls(ws, project, worktree=worktree).items()
            if floor is not None
        }

    def _inherited_extra(
        self,
        local: dict[str, object],
        ws: Workspace | None,
        project: str | None,
        *,
        own_tier: str,
        worktree: str | None = None,
    ) -> dict[str, object]:
        """Fold inherited ancestor values into an entity's own ``extra`` payload.

        Starts from *local* (the entity's locality-merged arbitrary keys);
        *own_tier* names the calling entity's tier (``project`` / ``worktree``).
        For every effective inheritable key (see :meth:`inheritable_keys`,
        excluding the typed-surface :data:`TYPED_INHERITABLE_KEYS` --
        ``base-branch`` owns its provenance method) folds the ancestor chain,
        pruned of every rung below the key's floor (see
        :meth:`inheritable_floors`), and merges it in. A key whose floor sits
        above *own_tier* is out of band entirely: an entity-local value is
        dropped (mirroring how ``config get`` prunes the same rung), not just
        unfolded. Otherwise a *scalar* key present entity-locally wins
        wholesale (it is the most-local rung) -- but a *table* key deep-merges
        ancestor cells into the local table (local cells win, absent cells fall
        through), so a section inherits cell-by-cell -- keeping ``config show``
        and the resolved view in step with ``config get``.
        """
        if ws is None:
            return local

        ancestors = self._ancestor_tiers(ws, project)
        floors = self.inheritable_floors(ws, project, worktree=worktree)
        merged = dict(local)
        for key in self.inheritable_keys(ws, project, worktree=worktree):
            if key in TYPED_INHERITABLE_KEYS:
                continue

            floor = floors.get(key)
            below_floor = floor is not None and FLOOR_TIERS[own_tier] < FLOOR_TIERS[floor]
            if not below_floor and key in local and not isinstance(local[key], dict):
                continue

            rungs: list[TierProbe] = ancestors
            if floor is not None:
                limit = FLOOR_TIERS[floor]
                rungs = [tp for tp in ancestors if FLOOR_TIERS[tp.tier] >= limit]

            effective, chain = fold_tiers(self._store, rungs, key)
            if not chain:
                if below_floor:
                    merged.pop(key, None)
                continue

            keep_table = not below_floor and key in local and isinstance(local[key], dict)
            merged[key] = deep_merge(effective, local[key]) if keep_table else effective

        return merged

    def resolve_generic_value(
        self, target: GenericTarget, key: Key
    ) -> tuple[object | None, list[SourceRung]]:
        """Resolve *key* over *target*'s cascade: (effective value, full chain).

        *key* is a bare key for a top-level scalar/table, or a tuple of parts
        (equivalently a dotted path) for a nested cell. The *head* part is folded
        over the cascade (a table head deep-merges across rungs; a scalar/array
        head takes the most-local rung), then the remaining parts descend inside
        the effective value. The effective value is ``None`` when unset; the
        chain lists every rung that carries the *head*, most-local-first, as
        :class:`SourceRung` records -- the material for ``config get --origin``
        (which descends the same remaining parts per rung for per-cell
        provenance). An inheritable key (see :meth:`inheritable_keys`) folds the
        entity's own cascade plus its ancestors' (tier-tagged), so the effective
        value matches what a real command would use; every other key stays
        entity-local. A scalar/array/table kind clash for the head across scopes
        is a hard error, matching the read-side rule (one rule for every read
        surface).

        The reserved ``hooks`` head is special-cased for its policy cells: a
        ``hooks.strict`` / ``hooks.enabled`` path folds per-rung *inside* the
        container (:meth:`fold_tiers` with *cell*), so the reported value is the
        same most-local-wins ladder the firing engine resolves. Any other
        ``hooks.*`` path folds nothing (the surface is refused at the write
        gate; ``on`` cells belong to ``hook set/unset``).
        """
        parts = key_parts(key)
        head, rest = parts[0], parts[1:]
        if head == "hooks" and len(rest) == 1 and rest[0] in HOOKS_POLICY_CELLS:
            return self._fold_policy_cell(target, rest[0])

        rungs = target.inherit.get(head)
        tiered = (
            rungs if rungs is not None else [TierProbe(None, probe) for probe in target.probes]
        )
        effective, chain = fold_tiers(self._store, tiered, head)
        effective = descend_value(effective, rest)

        return effective, chain

    def _fold_policy_cell(
        self, target: GenericTarget, cell: str
    ) -> tuple[object | None, list[SourceRung]]:
        """Fold a ``[hooks]`` policy cell over *target*'s fire-time ladder.

        The read-side twin of the firing ladder: the entity's own probe cascade
        (its registry entries are per-scope hook specs) plus the user/system
        files' top-level tables -- the scopes whose own ``[hooks]`` every
        operation folds. Each carrying rung's chain entry holds the whole
        ``hooks`` table (so ``--origin`` descends to the cell per rung), while
        the effective value is the most-local rung's cell -- no cross-tier
        merging, matching :func:`merge_hooks`' tri-state rule. A kind clash for
        the cell across scopes is a hard error.
        """
        chain: list[SourceRung] = []
        prev: object = None
        label = f"hooks.{cell}"
        for probe in self._policy_ladder(target):
            node = read_table(self._store, probe).get("hooks")
            if not isinstance(node, dict) or cell not in node:
                continue

            value = node[cell]
            check_kind(label, prev, value, probe.file)
            chain.append(SourceRung(probe.scope, node, probe.file, None))
            prev = value

        if not chain:
            return None, []

        return chain[0].value.get(cell), chain

    def _policy_ladder(self, target: GenericTarget) -> list[Probe]:
        """*target*'s probes plus the user/system top-level rungs (deduplicated).

        The global tier's own target already carries the two top-level probes;
        entity targets do not, so they are appended here -- least-local last,
        mirroring where the fire ladder reads its system/user scope specs.
        """
        ladder = list(target.probes)
        seen = {(p.scope, p.file, p.table_path) for p in ladder}
        for scope, path in (
            (Scope.USER, user_config_path()),
            (Scope.SYSTEM, system_config_path()),
        ):
            probe = Probe(scope, path, ())
            if (probe.scope, probe.file, probe.table_path) not in seen:
                ladder.append(probe)

        return ladder

    def provenance(self, target: GenericTarget, key: Key) -> SourceRung | None:
        """Resolve where *key*'s effective value comes from over *target*'s cascade.

        The most-local :class:`SourceRung` carrying the key (its ``value`` /
        ``tier`` / ``scope`` / ``file``), or ``None`` when the key appears
        nowhere in the cascade. The generic provenance read behind every
        origin label -- a typed-surface caller takes ``rung.value``, an
        inheritable-key label maps ``rung.tier``, an entity-local one
        ``rung.scope``.
        """
        _effective, chain = self.resolve_generic_value(target, key)

        return chain[0] if chain else None

    def effective_keys(
        self, target: GenericTarget, *, deep: bool = False
    ) -> list[tuple[str, SourceRung]]:
        """The non-unset keys ``config get`` would resolve for *target*.

        Completion material for the arbitrary-key ``config get``: every key whose
        :meth:`resolve_generic_value` yields a non-empty chain, paired with its
        most-local (winning) rung. The effective set is the entity's own present
        arbitrary keys (walked across *target*'s probe cascade, reserved keys
        excluded) unioned with the inheritable keys actually set on an ancestor
        (``target.inherit``), so a ``base-branch`` defined only at the workspace
        or global tier still appears. Sorted by key; the rung is the same head
        :meth:`resolve_generic_value` returns, so a caller can label by its
        ``tier`` (an inheritable key) or ``scope`` (an entity-local key) without
        a second resolve.

        With *deep*, each table-valued key is also walked into: every sub-table
        head and leaf cell emits a dotted path (``ci.runner.image``) paired with
        the most-local rung carrying that cell (per-cell provenance, mirroring
        ``config get --origin``), so deep dotted completion offers every
        addressable cell. Scalars and arrays emit their bare key only.
        """
        candidates: set[str] = set(target.inherit)
        for probe in target.probes:
            table = read_table(self._store, probe)
            candidates.update(key for key in table if key not in RESERVED_KEYS)
        keys: list[tuple[str, SourceRung]] = []
        for key in sorted(candidates):
            effective, chain = self.resolve_generic_value(target, key)
            if not chain:
                continue
            keys.append((key, chain[0]))
            if deep and isinstance(effective, dict):
                keys.extend(dotted_candidates(effective, chain, (), key))

        return keys

    def _extra_origins_for(
        self, target: GenericTarget, extra: dict[str, object]
    ) -> dict[str, list[SourceRung]]:
        """Per-head :class:`SourceRung` chains for each key in *extra*.

        The ``config show --origin`` material: for every arbitrary key on a slice
        node, the same chain :meth:`resolve_generic_value` returns
        (locality-merged, plus ancestors for an inheritable key), so the renderer
        can label each cell with its defining tier/scope. Re-resolves per key
        (cheap; the store is in-memory) rather than threading chains out of the
        locality merge -- one source of truth, shared with ``config get --origin``.
        """
        return {key: self.resolve_generic_value(target, key)[1] for key in extra}

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
        """Resolve a project's effective annotation via the locality cascade.

        Folds the project probe table (project-local top-level, workspace-local
        ``[projects.NAME]``, user/system ``[workspaces.W.projects.NAME]``)
        most-local-wins; inline hooks accumulate builtins-first across scopes.
        Reserved container keys are excluded; the remainder lands in ``extra``.
        """
        payload, hooks = merge_locality(self._store, self._project_probes(workspace, name))
        annotation = annotation_from_payload(payload, hooks)
        extra = self._inherited_extra(annotation.extra, workspace, name, own_tier="project")

        return replace(annotation, extra=extra)

    def _project_probes(self, workspace: Workspace, name: str) -> list[Probe]:
        """The project locality cascade, most-local-first."""
        ws_path = workspace.path
        proj_reg = ("workspaces", workspace.name, "projects", name)
        return [
            Probe(Scope.PROJECT, ConfigAddress.project(ws_path / name).file, ()),
            Probe(Scope.WORKSPACE, ConfigAddress.workspace(ws_path).file, ("projects", name)),
            Probe(Scope.USER, user_config_path(), proj_reg),
            Probe(Scope.SYSTEM, system_config_path(), proj_reg),
        ]

    def _workspace_probes(self, workspace: Workspace) -> list[Probe]:
        """The workspace locality cascade, most-local-first."""
        return [
            Probe(Scope.WORKSPACE, ConfigAddress.workspace(workspace.path).file, ()),
            Probe(Scope.USER, user_config_path(), ("workspaces", workspace.name)),
            Probe(Scope.SYSTEM, system_config_path(), ("workspaces", workspace.name)),
        ]

    def _worktree_probes(self, project_dir: Path, name: str) -> list[Probe]:
        """The worktree locality cascade, most-local-first."""
        ws = self.workspace_of(project_dir)
        wt_dir = project_dir / "worktrees" / name
        probes = [
            Probe(Scope.WORKTREE, ConfigAddress.worktree(wt_dir).file, ()),
            Probe(Scope.PROJECT, ConfigAddress.project(project_dir).file, ("worktrees", name)),
        ]
        if ws is not None:
            probes.append(
                Probe(
                    Scope.WORKSPACE,
                    ConfigAddress.workspace(ws.path).file,
                    ("projects", project_dir.name, "worktrees", name),
                )
            )
            for scope, path in (
                (Scope.USER, user_config_path()),
                (Scope.SYSTEM, system_config_path()),
            ):
                probes.append(
                    Probe(
                        scope,
                        path,
                        ("workspaces", ws.name, "projects", project_dir.name, "worktrees", name),
                    )
                )

        return probes

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

        # Worktree scope: accumulate across the full worktree probe cascade
        # (worktree-local + project/workspace/user/system registries), mirroring
        # the project scope -- a worktree hook declared in an ancestor registry
        # must fire too. Fall back to the worktree-local file only when
        # worktree_dir is not a real worktree under this project (e.g. the
        # project-lifecycle sentinel or an unresolved `hook list` target).
        worktrees_dir = project_dir / "worktrees"
        if worktree_dir.parent == worktrees_dir:
            name = worktree_name(worktree_dir, worktrees_dir) or worktree_dir.name
            _, hooks = merge_locality(self._store, self._worktree_probes(project_dir, name))
            return hooks

        return parse_hooks(self._store.read(worktree_dir / ".wtm" / "config.toml").get("hooks"))

    # --- scope detection (fresh reads) ---------------------------------------

    def declaring_scopes(self, name: str) -> set[Scope]:
        """Return the scopes (user and/or system) whose config declares *name*.

        Reads the raw user and system files, so it reflects the actual
        declaration sites -- unlike :attr:`Workspace.scope`, which records only
        the winning (most-specific) scope after the merge.
        """
        scopes: set[Scope] = set()
        if name in (self._store.read(system_config_path()).get("workspaces") or {}):
            scopes.add(Scope.SYSTEM)
        if name in (self._store.read(user_config_path()).get("workspaces") or {}):
            scopes.add(Scope.USER)

        return scopes

    # --- structural write tier (user file only) ------------------------------

    def add_workspace(
        self,
        name: str,
        path: str,
        *,
        description: str | None = None,
        base_branch: str | None = None,
    ) -> None:
        """Write a new ``[workspaces.NAME]`` entry into the user file.

        Only the caller-supplied scalars are written (``path`` always; the rest
        when given). The caller owns collision/validation policy.
        """
        with self._store.user_transaction() as doc:
            workspaces = workspaces_table(doc)
            entry = tomlkit.table()
            entry["path"] = path
            if description:
                entry["description"] = description
            if base_branch:
                entry["base-branch"] = base_branch
            workspaces[name] = entry

    def set_workspace(
        self,
        name: str,
        *,
        path: str | None = None,
        description: str | None = None,
        base_branch: str | None = None,
    ) -> None:
        """Edit an existing user-scope entry in place (partial update).

        Only the fields passed as non-``None`` are touched. A field passed as the
        empty string is *removed*; ``path`` may not be emptied (validated upstream).
        """
        with self._store.user_transaction() as doc:
            entry = existing_entry(doc, name)
            for key, value in (
                ("path", path),
                ("description", description),
                ("base-branch", base_branch),
            ):
                if value is None:
                    continue

                if value == "":
                    entry.pop(key, None)
                else:
                    entry[key] = value

    def rename_workspace(self, old: str, new: str) -> bool:
        """Rename the registry key *old* to *new*, preserving the entry's subtables.

        Fixes a top-level ``default-workspace`` that pointed at *old*. Returns
        whether that fixup happened.
        """
        with self._store.user_transaction() as doc:
            entry = existing_entry(doc, old)
            workspaces = doc["workspaces"]
            workspaces[new] = entry
            del workspaces[old]
            fixed = doc.get("default-workspace") == old
            if fixed:
                doc["default-workspace"] = new

        return fixed

    def remove_workspace(self, name: str) -> bool:
        """Delete the ``[workspaces.NAME]`` entry from the user file.

        Also clears a top-level ``default-workspace`` that pointed at *name*.
        Returns whether that fixup happened.
        """
        with self._store.user_transaction() as doc:
            existing_entry(doc, name)  # existence check
            del doc["workspaces"][name]
            fixed = doc.get("default-workspace") == name
            if fixed:
                del doc["default-workspace"]

        return fixed

    def set_default(self, name: str) -> None:
        """Set the top-level ``default-workspace`` key in the user file."""
        with self._store.user_transaction() as doc:
            set_leaf(doc, "default-workspace", name)

    def clear_default(self) -> None:
        """Remove the top-level ``default-workspace`` key from the user file."""
        with self._store.user_transaction() as doc:
            doc.pop("default-workspace", None)

    def set_base_branch(self, value: str) -> None:
        """Set the top-level ``base-branch`` (global default fork point) in the user file."""
        with self._store.user_transaction() as doc:
            set_leaf(doc, "base-branch", value)

    def clear_base_branch(self) -> None:
        """Remove the top-level ``base-branch`` key from the user file."""
        with self._store.user_transaction() as doc:
            doc.pop("base-branch", None)

    # --- loading -------------------------------------------------------------

    @classmethod
    def load(cls, *, cwd: Path | None = None, settings: Settings | None = None) -> Config:
        """Load and resolve config from files + env for one invocation.

        :param cwd: the current directory for cwd-based workspace inference
            (defaults to the live cwd, or ``None`` when the cwd has been removed
            -- then the cwd tier is simply skipped rather than crashing).
        :param settings: explicit load-time knobs (:class:`Settings`);
            ``None`` resolves them from the environment, matching the CLI.

        The active workspace is resolved from ``$WTM_WORKSPACE > cwd >
        default-workspace > sole workspace``; there is no ``-w`` flag -- a
        WORKSPACE named in a command's positionals overrides this per-target
        (see :mod:`wtm.target`).
        """
        resolved_settings = settings if settings is not None else Settings.from_env()
        cwd = cwd if cwd is not None else current_dir(resolve=False)
        store = Store(no_config=resolved_settings.no_config)

        system = store.read(system_config_path())
        user = store.read(user_config_path())

        registry = build_registry(system, user, store)
        default_workspace = global_scalar(user, system, "default-workspace")
        active = resolve_active(
            registry,
            cwd=cwd,
            default_workspace=default_workspace,
            selector=resolved_settings.workspace_selector,
        )

        return cls(
            workspaces=registry,
            active=active,
            system_scope_hooks=parse_hooks(system.get("hooks")),
            user_scope_hooks=parse_hooks(user.get("hooks")),
            _system_hooks_dir=resolved_settings.resolved_system_hooks_dir(),
            _user_hooks_dir=resolved_settings.resolved_user_hooks_dir(),
            _store=store,
            default_base_branch=global_scalar(user, system, "base-branch"),
            default_workspace=default_workspace,
            _settings=resolved_settings,
        )

    def reload(self) -> Config:
        """Re-read every config file under this snapshot's settings (fresh instance).

        A ``Config`` is a load-once snapshot: its store caches parsed documents
        and its registry is fixed, so a long-lived holder would serve stale
        reads. :meth:`reload` is the refresh knob -- daemons call it per
        reconcile batch to get a new snapshot equivalent to a fresh
        :meth:`load`, bound to the same :class:`Settings`.
        """
        return type(self).load(cwd=current_dir(resolve=False), settings=self._settings)

    # --- worktree resolution -------------------------------------------------

    def resolve_worktree(self, project_dir: Path, name: str) -> ResolvedWorktree:
        """Resolve a worktree's effective config by name via the locality cascade.

        The by-name core shared by the cwd view and the tree dump: fold the
        worktree probe table most-local-wins, split its ``description`` off, and
        inherit the remaining arbitrary keys down the containment chain. The
        ``base-branch`` carve-out (a worktree has no base-branch tier) is upheld
        by the probes never carrying one to the effective view.
        """
        wt_dir = project_dir / "worktrees" / name
        payload, hooks = merge_locality(self._store, self._worktree_probes(project_dir, name))
        local = {k: v for k, v in payload.items() if k != "description"}
        extra = self._inherited_extra(
            local,
            self.workspace_of(project_dir),
            project_dir.name,
            own_tier="worktree",
            worktree=name,
        )

        return ResolvedWorktree(
            name=name,
            dir=wt_dir,
            description=str_or_none(payload.get("description")),
            hooks=hooks,
            extra=extra,
        )

    # --- slice/discovery delegates -------------------------------------------

    def cwd_location(self, *, cwd: Path | None = None) -> SliceTarget | None:
        """Infer the target entity from the cwd (see :func:`wtm.config.slice.cwd_location`)."""
        return slice_view.cwd_location(self, cwd=cwd)

    def resolve_slice(
        self,
        *,
        target: SliceTarget | None,
        recurse: bool = False,
        depth_limit: int | None = None,
    ) -> ConfigSlice:
        """Materialise a pruned config tree (see :func:`wtm.config.slice.resolve_slice`)."""
        return slice_view.resolve_slice(
            self, target=target, recurse=recurse, depth_limit=depth_limit
        )

    def project_names(self, ws: Workspace) -> set[str]:
        """Projects under *ws* (on-disk unioned with declared; see the slice module)."""
        return slice_view.project_names(self, ws)

    def declared_project_names(self, ws: Workspace) -> set[str]:
        """Project names declared across *ws*'s files (see the slice module)."""
        return slice_view.declared_project_names(self, ws)

    def declared_worktree_names(self, ws: Workspace, project: str) -> set[str]:
        """Worktree names declared for a project (see the slice module)."""
        return slice_view.declared_worktree_names(self, ws, project)

    # --- entity handles -------------------------------------------------------

    def entity(self, *tokens: str) -> EntityHandle:
        """Bind an :class:`EntityHandle` to a named (or cwd-inferred) entity.

        Count-based, mirroring the target grammar: ``entity()`` infers the
        deepest entity from the cwd (the global tier when outside every
        workspace), ``entity(WORKSPACE)``, ``entity(WORKSPACE, PROJECT)``, and
        ``entity(WORKSPACE, PROJECT, NAME)`` name one explicitly.
        """
        if len(tokens) > 3:
            raise WtmError("entity takes at most WORKSPACE PROJECT NAME")

        if not tokens:
            return self._entity_from_cwd()

        padded = (*tokens, None, None)
        workspace, project, name = padded[0], padded[1], padded[2]
        if workspace not in self.workspaces:
            raise WtmError(f"unknown workspace: '{workspace}'")

        target = self.resolve_generic_target_for(workspace, project, name)
        cls = (
            WorktreeEntity
            if name is not None
            else ProjectEntity
            if project is not None
            else WorkspaceEntity
        )

        return cls(
            self,
            target,
            workspace_name=workspace,
            project_name=project,
            name=name,
        )

    def _entity_from_cwd(self) -> EntityHandle:
        """The deepest entity the cwd sits inside (global when outside all)."""
        location = slice_view.cwd_location(self)
        if location is None:
            return build_global_entity(self)

        target = self.resolve_generic_target_for(
            location.workspace, location.project, location.name
        )
        cls = {
            "worktree": WorktreeEntity,
            "project": ProjectEntity,
            "workspace": WorkspaceEntity,
        }[location.tier]

        return cls(
            self,
            target,
            workspace_name=location.workspace,
            project_name=location.project,
            name=location.name,
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
