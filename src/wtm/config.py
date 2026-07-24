"""The single config authority for wtm: reads, writes, scoping, and discovery.

``wtm`` manages git worktrees over one or more named **workspaces** (each a
projects root ``<path>/<project>/.git`` + ``worktrees/``). Workspaces are
*declared* in config; individual projects are *discovered* on disk and only
*annotated* by config.

Config lives in up to five scopes, most-general to most-specific::

    system:    $WTM_SYSTEM_CONFIG or /etc/wtm/config.toml
    user:      $WTM_CONFIG or $XDG_CONFIG_HOME/wtm/config.toml
    workspace: <workspace>/.wtm/config.toml
    project:   <project>/.wtm/config.toml
    worktree:  <worktree>/.wtm/config.toml

This module is the *only* place that reads or writes ``.wtm`` config -- no other
part of the app touches ``tomlkit`` or builds a ``.wtm/config.toml`` path. It
offers three tiers, all on :class:`Config`:

* a **generic value facility** -- :meth:`Config.get_value` /
  :meth:`Config.set_value` / :meth:`Config.unset_value` -- addressing any
  scope through a :class:`ConfigAddress` and a key (a bare key, or a tuple for a
  nested table). This is the substrate every other tier is built on;
* a **typed read view** (the :class:`Workspace` / :class:`ProjectAnnotation` /
  :class:`HookSpec` dataclasses and :meth:`Config.resolve_view`), resolving
  scalars most-specific-first via declarative cascades over the value facility
  and reporting provenance;
* a **structural write tier** -- the named registry mutations
  (:meth:`Config.add_workspace`, ``set_workspace``, ``rename_workspace``,
  ``remove_workspace``, ``set_default`` / ``clear_default``, ``set_base_branch``
  / ``clear_base_branch``) that own cross-key invariants (a rename fixes a
  dangling ``default_workspace``) and write only the user file.

Writes name their target scope explicitly; the "most-local home that exists"
placement policy is the caller's (see :mod:`wtm.workspace` / :mod:`wtm.project`).
Config-file scalars resolve most-local-first ``worktree > project > workspace >
user > system > default``; command-layer overrides (a CLI flag such as
``--base``, or the ``WTM_*`` env selectors) sit above this file cascade and are
applied by the calling command, not here. Inline hooks *accumulate* across
scopes on top of the builtin file hooks. ``WTM_NO_CONFIG`` suppresses all config
files (reads) and makes any write a hard error (mutating a file we are told to
ignore is contradictory); it is the test/CI isolation knob.

The active workspace is resolved ``$WTM_WORKSPACE > cwd (longest path match) >
default_workspace > sole workspace > error`` -- there is no built-in default
workspace, so with nothing declared and nothing selected the tool errors. A
WORKSPACE named in a command's positionals overrides this per-target (see
:mod:`wtm.target`); there is no ``-w`` flag.

Reads and writes share one comment- and format-preserving ``tomlkit`` round-trip
(re-read fresh, mutate, write back atomically via a temp file + ``os.replace``);
the loaded read snapshot is fixed for the invocation and a write does not refresh
it (``wtm`` loads once, acts, exits).
"""

from __future__ import annotations

__all__ = [
    "Config",
    "ConfigAddress",
    "GenericTarget",
    "HookSpec",
    "ProjectAnnotation",
    "ResolvedConfig",
    "ResolvedProject",
    "ResolvedWorktree",
    "Scope",
    "SourceRung",
    "Workspace",
    "resolve_project_dir",
    "validate_arbitrary_key",
]

import os
import tempfile
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path

import tomlkit

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


# --- scope addressing --------------------------------------------------------


@dataclass(frozen=True)
class ConfigAddress:
    """A single config file, named by its scope (+ the entity dir when local).

    The system/user files come from env/XDG; the workspace/project/worktree
    files are all ``<entity_dir>/.wtm/config.toml`` -- the scope carries the
    semantic tier (used for provenance and the resolution cascades) while
    *entity_dir* pins the file. This is the address every read/write names.
    """

    scope: Scope
    entity_dir: Path | None = None

    @classmethod
    def system(cls) -> ConfigAddress:
        """The system-scope address (``$WTM_SYSTEM_CONFIG`` or ``/etc``)."""
        return cls(Scope.SYSTEM)

    @classmethod
    def user(cls) -> ConfigAddress:
        """The user-scope address (``$WTM_CONFIG`` or the XDG default)."""
        return cls(Scope.USER)

    @classmethod
    def workspace(cls, ws_dir: Path) -> ConfigAddress:
        """The workspace-local address for the workspace rooted at *ws_dir*."""
        return cls(Scope.WORKSPACE, ws_dir)

    @classmethod
    def project(cls, project_dir: Path) -> ConfigAddress:
        """The project-local address for the project at *project_dir*."""
        return cls(Scope.PROJECT, project_dir)

    @classmethod
    def worktree(cls, worktree_dir: Path) -> ConfigAddress:
        """The worktree-local address for the worktree at *worktree_dir*."""
        return cls(Scope.WORKTREE, worktree_dir)

    @property
    def file(self) -> Path:
        """The TOML file this address resolves to."""
        if self.scope is Scope.SYSTEM:
            return _system_config_path()

        if self.scope is Scope.USER:
            return _user_config_path()

        if self.entity_dir is None:
            raise WtmError(f"{self.scope} address needs an entity dir")

        return self.entity_dir / ".wtm" / "config.toml"


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
    """Config overlay for a discovered project (never a declaration).

    :param extra: arbitrary (non-reserved) keys resolved by the locality merge,
        beyond the known scalar fields.
    """

    description: str | None = None
    base_branch: str | None = None
    hooks: HookSpec = field(default_factory=HookSpec)
    extra: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Workspace:
    """A declared workspace: a named projects root plus its config overlay.

    :param extra: arbitrary (non-reserved) keys resolved by the locality merge,
        beyond the known scalar fields.
    """

    name: str
    path: Path
    description: str | None = None
    base_branch: str | None = None  # scope-wide default for its projects
    hooks: HookSpec = field(default_factory=HookSpec)
    scope: Scope = Scope.USER  # where it was ultimately declared (for --sources)
    extra: dict[str, object] = field(default_factory=dict)


# --- resolved view -----------------------------------------------------------


@dataclass(frozen=True)
class ResolvedWorktree:
    """The worktree tier of the resolved view (locality-merged over the ladder).

    :param description: the worktree's scalar annotation, if any.
    :param extra: arbitrary (non-reserved) keys resolved by the locality merge.
    """

    name: str
    dir: Path
    description: str | None = None
    hooks: HookSpec = field(default_factory=HookSpec)
    extra: dict[str, object] = field(default_factory=dict)


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
    extra: dict[str, object] = field(default_factory=dict)
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


@dataclass(frozen=True)
class GenericTarget:
    """The cwd-inferred entity a generic ``config`` key acts on.

    Resolved by :meth:`Config.resolve_generic_target`: *label* names the entity
    for messages, *probes* is its locality cascade (most-local-first, feeding
    reads), *write_target* maps a scope to its ``(address, prefix)`` write site,
    *allowed_scopes* gates ``-s/--scope``, and *default_scope* is the auto-default
    (the deepest scope whose dir exists). *inherit* maps an inheritable key (see
    :data:`INHERITABLE_KEYS`) to its extended, tier-tagged probe list -- the
    entity's own cascade followed by its ancestors' -- so ``config get`` reports
    the same effective value a real command would take.
    """

    label: str
    probes: list[_Probe]
    write_target: Callable[[Scope], tuple[ConfigAddress, tuple[str, ...]]]
    allowed_scopes: tuple[Scope, ...]
    default_scope: Scope
    inherit: dict[str, list[_TierProbe]] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceRung:
    """One resolved rung of a ``config get --sources`` chain.

    :param scope: the config scope (file tier) the value was read from.
    :param value: the value present at this rung.
    :param file: the config file the value lives in.
    :param tier: for an inheritable key, the definition level it came from
        (``project`` / ``workspace`` / ``global``); ``None`` for a plain
        (entity-local) key, where the scope already names the source.
    """

    scope: Scope
    value: object
    file: Path
    tier: str | None = None


# --- generic value store -----------------------------------------------------


Key = str | tuple[str, ...]

RESERVED_KEYS = frozenset({"hooks", "projects", "worktrees", "path", "default_workspace"})
_CONTAINER_KEYS = frozenset({"hooks", "projects", "worktrees"})
# Keys whose tables are pure containers of named children (rendered as
# super-tables so a fresh nested write produces dotted headers, no empty parent).
_SUPER_TABLE_KEYS = frozenset({"workspaces", "projects", "worktrees"})
# Keys whose read resolution inherits down the containment chain (Layer 2): the
# generic `config get` folds an entity's own cascade plus its ancestors' for
# these, so it reports the same effective value a real command would use.
INHERITABLE_KEYS = frozenset({"base_branch"})


def validate_arbitrary_key(key: str) -> None:
    """Reject a reserved name as an arbitrary ``config`` key (raises on clash).

    The container tables (``hooks`` / ``projects`` / ``worktrees``) and the
    user-file declarations (``path`` / ``default_workspace``) own their own
    typed surfaces; the generic key facility must not shadow them.
    """
    if key in RESERVED_KEYS:
        raise WtmError(
            f"'{key}' is a reserved key; use its dedicated command "
            f"(reserved: {', '.join(sorted(RESERVED_KEYS))})"
        )


class _Store:
    """The comment-preserving ``tomlkit`` document cache backing every read/write.

    One document per file path, loaded lazily and cached; a write mutates the
    cached document and rewrites the file atomically. ``no_config`` (the
    ``WTM_NO_CONFIG`` knob) makes reads return an empty document and any write a
    hard error.
    """

    def __init__(self, *, no_config: bool) -> None:
        self._no_config = no_config
        self._docs: dict[Path, tomlkit.TOMLDocument] = {}

    def doc(self, path: Path) -> tomlkit.TOMLDocument:
        """Return the cached document for *path* (parsing it on first access)."""
        if self._no_config:
            return tomlkit.document()

        cached = self._docs.get(path)
        if cached is not None:
            return cached

        try:
            with open(path, encoding="utf-8") as handle:
                doc = tomlkit.parse(handle.read())
        except FileNotFoundError:
            doc = tomlkit.document()
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise WtmError(f"config error in {path}: {exc}") from exc

        self._docs[path] = doc

        return doc

    def read(self, path: Path) -> dict:
        """Return *path*'s contents as a plain (unwrapped) dict, ``{}`` when absent."""
        return self.doc(path).unwrap()

    def get(self, path: Path, key: Key) -> str | None:
        """Return the scalar at *key* in *path* (unwrapped), or ``None`` when absent."""
        node: object = self.doc(path)
        for part in _key_parts(key):
            if not isinstance(node, dict) or part not in node:
                return None

            node = node[part]
        if isinstance(node, dict):
            return None

        return _unwrap_scalar(node)

    def set(self, path: Path, key: Key, value: str) -> None:
        """Set *key* = *value* in *path*, creating tables and the file as needed."""
        doc = self._writable_doc(path)
        parts = _key_parts(key)
        table = _descend_for_write(doc, parts[:-1])
        _set_leaf(table if parts[:-1] else doc, parts[-1], value)
        self._flush(path, doc)

    def unset(self, path: Path, key: Key) -> None:
        """Remove *key* from *path* (a no-op when it is absent)."""
        doc = self._writable_doc(path)
        parts = _key_parts(key)
        node: object = doc
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                return

            node = node[part]
        if isinstance(node, dict):
            node.pop(parts[-1], None)
        self._flush(path, doc)

    def user_doc(self) -> tomlkit.TOMLDocument:
        """Return the writable user-file document (for the structural write tier)."""
        return self._writable_doc(_user_config_path())

    def flush_user(self, doc: tomlkit.TOMLDocument) -> None:
        """Write back a user-file document mutated by the structural write tier."""
        self._flush(_user_config_path(), doc)

    def _writable_doc(self, path: Path) -> tomlkit.TOMLDocument:
        """Return a fresh document for *path*, refusing under ``WTM_NO_CONFIG``."""
        if self._no_config:
            raise WtmError("cannot write config while WTM_NO_CONFIG is set")

        return self.doc(path)

    def _flush(self, path: Path, doc: tomlkit.TOMLDocument) -> None:
        """Cache *doc* and write it to *path* atomically."""
        self._docs[path] = doc
        _atomic_write(path, tomlkit.dumps(doc))


def _key_parts(key: Key) -> tuple[str, ...]:
    """Normalise a key into a tuple of path parts (a bare string is one part)."""
    return (key,) if isinstance(key, str) else tuple(key)


def _unwrap_scalar(node: object) -> str:
    """Return a tomlkit scalar as a plain Python value."""
    unwrap = getattr(node, "unwrap", None)

    return unwrap() if callable(unwrap) else node


def _descend_for_write(doc: tomlkit.TOMLDocument, parts: Sequence[str]):
    """Walk/create the intermediate tables for a nested write, returning the leaf table.

    A freshly created container level (``workspaces`` / ``projects`` /
    ``worktrees``) is a super-table so it renders as dotted ``[workspaces.NAME]``
    headers with no empty ``[workspaces]`` line -- matching the structural write
    tier; the name/entity segments in between stay normal (they hold scalars).
    """
    table: object = doc
    for part in parts:
        child = table.get(part)
        if child is None:
            child = tomlkit.table(is_super_table=part in _SUPER_TABLE_KEYS)
            table[part] = child
        table = child

    return table


def _set_leaf(table, key: str, value: str) -> None:
    """Set a scalar leaf, keeping a *new* top-level key above any trailing table.

    A plain ``doc[key] = value`` on a document that ends in a table appends the
    bare key *inside* that table (a tomlkit foot-gun); a brand-new top-level key
    is instead inserted at the document head. Nested tables and existing keys are
    set in place.
    """
    is_doc = isinstance(table, tomlkit.TOMLDocument)
    if is_doc and key not in table and table.body:
        table._insert_at(0, key, value)
    else:
        table[key] = value


def _atomic_write(path: Path, text: str) -> None:
    """Atomically write *text* to *path* (creating parent dirs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".wtm-config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass

        raise


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
    _store: _Store
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

    # --- generic value facility ----------------------------------------------

    def get_value(self, address: ConfigAddress, key: Key) -> str | None:
        """Read one scalar at *key* from the file *address* names (raw, unmerged).

        *key* is a bare key for a top-level scalar, or a tuple of parts for a
        nested table (e.g. ``("project", "base_branch")``). Returns ``None`` when
        the scope's file or the key is absent.
        """
        return self._store.get(address.file, key)

    def set_value(self, address: ConfigAddress, key: Key, value: str) -> None:
        """Write one scalar at *key* into the file *address* names.

        The scope is named explicitly by the caller; the file (and its parent
        dirs) is created on first write. Refused under ``WTM_NO_CONFIG``.
        """
        self._store.set(address.file, key, value)

    def unset_value(self, address: ConfigAddress, key: Key) -> None:
        """Remove one scalar at *key* from the file *address* names (a no-op if absent)."""
        self._store.unset(address.file, key)

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
            return _global_address(scope), ("workspaces", workspace.name, "projects", name)

        raise WtmError(f"cannot write a project annotation at {scope} scope")

    def workspace_write_target(
        self, workspace: Workspace, scope: Scope
    ) -> tuple[ConfigAddress, tuple[str, ...]]:
        """Resolve where a workspace annotation lands at *scope*: (address, key prefix)."""
        if scope is Scope.WORKSPACE:
            return ConfigAddress.workspace(workspace.path), ()

        if scope in (Scope.USER, Scope.SYSTEM):
            return _global_address(scope), ("workspaces", workspace.name)

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
                _global_address(scope),
                ("workspaces", workspace.name, "projects", project_dir.name, "worktrees", name),
            )

        raise WtmError(f"cannot write a worktree annotation at {scope} scope")

    # --- generic key surface (arbitrary keys, cwd-inferred entity) -----------

    def resolve_generic_target(self, cwd: Path | None) -> GenericTarget:
        """Resolve the cwd-inferred entity a generic ``config`` key acts on.

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
        default = (
            Scope.WORKTREE
            if wt_dir.is_dir()
            else Scope.PROJECT
            if project_dir.is_dir()
            else Scope.WORKSPACE
            if ws.path.is_dir()
            else Scope.USER
        )

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
            inherit={"base_branch": self._base_branch_inherit(ws, project_dir.name)},
        )

    def _project_generic_target(self, ws: Workspace, project_dir: Path) -> GenericTarget:
        """The generic target for a cwd-bound project."""
        default = (
            Scope.PROJECT
            if project_dir.is_dir()
            else Scope.WORKSPACE
            if ws.path.is_dir()
            else Scope.USER
        )

        return GenericTarget(
            label=f"project '{project_dir.name}'",
            probes=self._project_probes(ws, project_dir.name),
            write_target=lambda scope: self.project_write_target(ws, project_dir.name, scope),
            allowed_scopes=(Scope.PROJECT, Scope.WORKSPACE, Scope.USER, Scope.SYSTEM),
            default_scope=default,
            inherit={"base_branch": self._base_branch_inherit(ws, project_dir.name)},
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
            inherit={"base_branch": self._base_branch_inherit(ws, None)},
        )

    def _base_branch_inherit(self, ws: Workspace, project: str | None) -> list[_TierProbe]:
        """The tier-tagged base_branch chain for an entity, most-local-first.

        Concatenates the project cascade (tier ``project``, when a *project* is
        given), the workspace-own cascade (tier ``workspace``), and the global
        top-level default (tier ``global``) -- so the fold's most-local winner
        matches :meth:`base_branch_provenance` exactly. A worktree inherits its
        containing project's chain (base_branch is a project-tier concept, with
        no worktree tier of its own).
        """
        rungs: list[_TierProbe] = []
        if project is not None:
            rungs += [_TierProbe("project", p) for p in self._project_probes(ws, project)]
        rungs += [_TierProbe("workspace", p) for p in self._workspace_probes(ws)]
        rungs += [_TierProbe("global", p) for p in _global_probes()]

        return rungs

    def resolve_generic_value(
        self, target: GenericTarget, key: str
    ) -> tuple[object | None, list[SourceRung]]:
        """Resolve *key* over *target*'s cascade: (effective value, full chain).

        The effective value is the most-local present one (``None`` when unset);
        the chain lists every rung that carries *key*, most-local-first, as
        :class:`SourceRung` records -- the material for ``config get --sources``.
        An inheritable key (see :data:`INHERITABLE_KEYS`) folds the entity's own
        cascade plus its ancestors' (tier-tagged), so the effective value matches
        what a real command would use; every other key stays entity-local. A
        scalar/array/table kind clash for *key* across scopes is a hard error,
        matching :func:`_merge_locality` (one rule for every read surface).
        """
        rungs = target.inherit.get(key)
        tiered = (
            rungs if rungs is not None else [_TierProbe(None, probe) for probe in target.probes]
        )
        chain: list[SourceRung] = []
        prev: object = None
        for tiered_probe in tiered:
            probe = tiered_probe.probe
            table = _read_table(self._store, probe)
            if key in table and key not in _CONTAINER_KEYS:
                value = table[key]
                _check_kind(key, prev, value, probe)
                chain.append(SourceRung(probe.scope, value, probe.file, tiered_probe.tier))
                prev = value

        effective = chain[0].value if chain else None

        return effective, chain

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

    def declared_project_names(self, workspace: Workspace) -> set[str]:
        """Return the project names *declared* for *workspace* across its files.

        The keys of every ``projects`` table along the workspace probe cascade
        (workspace-local top-level, user/system ``[workspaces.NAME.projects]``).
        A *declaration* listing -- distinct from on-disk discovery -- so ``config
        show`` can surface annotated-but-inert projects.
        """
        names: set[str] = set()
        for probe in self._workspace_probes(workspace):
            table = _read_table(self._store, probe)
            projects = table.get("projects")
            if isinstance(projects, dict):
                names.update(projects)

        return names

    # --- per-project resolution ----------------------------------------------

    def project_annotation(self, workspace: Workspace, name: str) -> ProjectAnnotation:
        """Resolve a project's effective annotation via the locality cascade.

        Folds the project probe table (project-local top-level, workspace-local
        ``[projects.NAME]``, user/system ``[workspaces.W.projects.NAME]``)
        most-local-wins; inline hooks accumulate builtins-first across scopes.
        Reserved container keys are excluded; the remainder lands in ``extra``.
        """
        payload, hooks = _merge_locality(self._store, self._project_probes(workspace, name))

        return _annotation_from_payload(payload, hooks)

    def base_branch_for(self, workspace: Workspace, name: str) -> str | None:
        """Resolve a project's configured base branch (most-specific first)."""
        return self.base_branch_provenance(workspace, name)[0]

    def base_branch_provenance(
        self, workspace: Workspace | None, name: str
    ) -> tuple[str | None, str]:
        """Resolve a project's effective base branch and the tier it came from.

        A Layer-2 inheritance over the containment chain: the project's own
        locality-merged ``base_branch`` (tier ``project``, wherever in the
        project probe table it won) outranks the workspace's own merged
        ``base_branch`` (tier ``workspace``), which outranks the global top-level
        default (tier ``global``); ``(none)`` when nothing is set.
        """
        if workspace is not None:
            proj_payload, _ = _merge_locality(self._store, self._project_probes(workspace, name))
            if proj_payload.get("base_branch"):
                return str(proj_payload["base_branch"]), "project"

            ws_payload, _ = _merge_locality(self._store, self._workspace_probes(workspace))
            if ws_payload.get("base_branch"):
                return str(ws_payload["base_branch"]), "workspace"

        for address in (ConfigAddress.user(), ConfigAddress.system()):
            value = self.get_value(address, "base_branch")
            if value:
                return value, "global"

        return None, "(none)"

    def _project_probes(self, workspace: Workspace, name: str) -> list[_Probe]:
        """The project locality cascade, most-local-first."""
        ws_path = workspace.path
        proj_reg = ("workspaces", workspace.name, "projects", name)
        return [
            _Probe(Scope.PROJECT, ConfigAddress.project(ws_path / name).file, ()),
            _Probe(Scope.WORKSPACE, ConfigAddress.workspace(ws_path).file, ("projects", name)),
            _Probe(Scope.USER, _user_config_path(), proj_reg),
            _Probe(Scope.SYSTEM, _system_config_path(), proj_reg),
        ]

    def _workspace_probes(self, workspace: Workspace) -> list[_Probe]:
        """The workspace locality cascade, most-local-first."""
        return [
            _Probe(Scope.WORKSPACE, ConfigAddress.workspace(workspace.path).file, ()),
            _Probe(Scope.USER, _user_config_path(), ("workspaces", workspace.name)),
            _Probe(Scope.SYSTEM, _system_config_path(), ("workspaces", workspace.name)),
        ]

    def _worktree_probes(self, project_dir: Path, name: str) -> list[_Probe]:
        """The worktree locality cascade, most-local-first."""
        ws = self.workspace_of(project_dir)
        wt_dir = project_dir / "worktrees" / name
        probes = [
            _Probe(Scope.WORKTREE, ConfigAddress.worktree(wt_dir).file, ()),
            _Probe(Scope.PROJECT, ConfigAddress.project(project_dir).file, ("worktrees", name)),
        ]
        if ws is not None:
            probes.append(
                _Probe(
                    Scope.WORKSPACE,
                    ConfigAddress.workspace(ws.path).file,
                    ("projects", project_dir.name, "worktrees", name),
                )
            )
            for scope, path in (
                (Scope.USER, _user_config_path()),
                (Scope.SYSTEM, _system_config_path()),
            ):
                probes.append(
                    _Probe(
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
            _, hooks = _merge_locality(self._store, self._worktree_probes(project_dir, name))
            return hooks

        return _parse_hooks(self._store.read(worktree_dir / ".wtm" / "config.toml").get("hooks"))

    # --- scope detection (fresh reads) ---------------------------------------

    def declaring_scopes(self, name: str) -> set[Scope]:
        """Return the scopes (user and/or system) whose config declares *name*.

        Reads the raw user and system files, so it reflects the actual
        declaration sites -- unlike :attr:`Workspace.scope`, which records only
        the winning (most-specific) scope after the merge.
        """
        scopes: set[Scope] = set()
        if name in (self._store.read(_system_config_path()).get("workspaces") or {}):
            scopes.add(Scope.SYSTEM)
        if name in (self._store.read(_user_config_path()).get("workspaces") or {}):
            scopes.add(Scope.USER)

        return scopes

    def base_branch_source(self) -> Scope | None:
        """Return the scope declaring the top-level ``base_branch`` (user wins), or ``None``.

        Reads the raw user and system files so a getter can annotate which scope a
        real command would take the global default from.
        """
        if self._store.read(_user_config_path()).get("base_branch"):
            return Scope.USER

        if self._store.read(_system_config_path()).get("base_branch"):
            return Scope.SYSTEM

        return None

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
        doc = self._store.user_doc()
        workspaces = _workspaces_table(doc)
        entry = tomlkit.table()
        entry["path"] = path
        if description:
            entry["description"] = description
        if base_branch:
            entry["base_branch"] = base_branch
        workspaces[name] = entry
        self._store.flush_user(doc)

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
        doc = self._store.user_doc()
        entry = _existing_entry(doc, name)
        for key, value in (
            ("path", path),
            ("description", description),
            ("base_branch", base_branch),
        ):
            if value is None:
                continue

            if value == "":
                entry.pop(key, None)
            else:
                entry[key] = value
        self._store.flush_user(doc)

    def rename_workspace(self, old: str, new: str) -> bool:
        """Rename the registry key *old* to *new*, preserving the entry's subtables.

        Fixes a top-level ``default_workspace`` that pointed at *old*. Returns
        whether that fixup happened.
        """
        doc = self._store.user_doc()
        entry = _existing_entry(doc, old)
        workspaces = doc["workspaces"]
        workspaces[new] = entry
        del workspaces[old]
        fixed = doc.get("default_workspace") == old
        if fixed:
            doc["default_workspace"] = new
        self._store.flush_user(doc)

        return fixed

    def remove_workspace(self, name: str) -> bool:
        """Delete the ``[workspaces.NAME]`` entry from the user file.

        Also clears a top-level ``default_workspace`` that pointed at *name*.
        Returns whether that fixup happened.
        """
        doc = self._store.user_doc()
        _existing_entry(doc, name)  # existence check
        del doc["workspaces"][name]
        fixed = doc.get("default_workspace") == name
        if fixed:
            del doc["default_workspace"]
        self._store.flush_user(doc)

        return fixed

    def set_default(self, name: str) -> None:
        """Set the top-level ``default_workspace`` key in the user file."""
        doc = self._store.user_doc()
        _set_leaf(doc, "default_workspace", name)
        self._store.flush_user(doc)

    def clear_default(self) -> None:
        """Remove the top-level ``default_workspace`` key from the user file."""
        doc = self._store.user_doc()
        doc.pop("default_workspace", None)
        self._store.flush_user(doc)

    def set_base_branch(self, value: str) -> None:
        """Set the top-level ``base_branch`` (global default fork point) in the user file."""
        doc = self._store.user_doc()
        _set_leaf(doc, "base_branch", value)
        self._store.flush_user(doc)

    def clear_base_branch(self) -> None:
        """Remove the top-level ``base_branch`` key from the user file."""
        doc = self._store.user_doc()
        doc.pop("base_branch", None)
        self._store.flush_user(doc)

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
        store = _Store(no_config=no_config)

        system = store.read(_system_config_path())
        user = store.read(_user_config_path())

        registry = _build_registry(system, user, store)
        default_workspace = _global_scalar(user, system, "default_workspace")
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
            _store=store,
            default_base_branch=_global_scalar(user, system, "base_branch"),
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
            extra=annotation.extra,
            worktree=self._resolve_cwd_worktree(project_dir, abspath(str(cwd))),
        )

    def _resolve_cwd_worktree(self, project_dir: Path, cwd: Path) -> ResolvedWorktree | None:
        """Resolve the cwd-bound worktree under *project_dir* via the locality cascade."""
        worktrees_dir = project_dir / "worktrees"
        if not within(cwd, worktrees_dir) or cwd == worktrees_dir:
            return None

        rel = cwd.relative_to(worktrees_dir)
        wt_dir = worktrees_dir / rel.parts[0]
        name = worktree_name(wt_dir, worktrees_dir) or wt_dir.name
        payload, hooks = _merge_locality(self._store, self._worktree_probes(project_dir, name))
        extra = {k: v for k, v in payload.items() if k != "description"}

        return ResolvedWorktree(
            name=name,
            dir=wt_dir,
            description=_str_or_none(payload.get("description")),
            hooks=hooks,
            extra=extra,
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


# --- locality merge engine ---------------------------------------------------


@dataclass(frozen=True)
class _Probe:
    """One rung of a locality cascade: a scope, its file, and the table path.

    *table_path* is the sequence of keys to descend from the file root to reach
    the entity's own table (empty for a self-config file whose root *is* the
    entity's table).
    """

    scope: Scope
    file: Path
    table_path: tuple[str, ...]


@dataclass(frozen=True)
class _TierProbe:
    """A probe tagged with the definition tier it contributes to a read.

    *tier* is the inheritance level (``project`` / ``workspace`` / ``global``)
    the *probe*'s value belongs to, or ``None`` for a plain (entity-local) key
    whose scope already names the source. The tier is orthogonal to the probe's
    scope (a project-tier value can live in the user file, say).
    """

    tier: str | None
    probe: _Probe


def _read_table(store: _Store, probe: _Probe) -> dict:
    """Return the raw table *probe* points at (``{}`` when any step is absent)."""
    node: object = store.read(probe.file)
    for part in probe.table_path:
        if not isinstance(node, dict):
            return {}

        node = node.get(part)
    if not isinstance(node, dict):
        return {}

    return node


def _merge_locality(store: _Store, probes: Sequence[_Probe]) -> tuple[dict[str, object], HookSpec]:
    """Fold *probes* least-to-most-local into (payload, hooks).

    *probes* are ordered most-local-first (the caller's natural cascade order);
    the fold runs in reverse so more-local values overwrite. Reserved keys are
    stripped from the scalar payload: ``hooks`` accumulates into a single
    :class:`HookSpec` (builtins-first across scopes), ``projects`` / ``worktrees``
    are child-descent containers handled elsewhere, and ``path`` /
    ``default_workspace`` are declaration keys that own their typed surfaces (so
    a workspace registry entry's own ``path`` never leaks into ``extra``). A kind
    mismatch (scalar / array / table) for the same key across scopes is a hard
    error.
    """
    payload: dict[str, object] = {}
    hooks = HookSpec()
    for probe in reversed(probes):
        table = _read_table(store, probe)
        hooks = _merge_hooks(hooks, _parse_hooks(table.get("hooks")))
        for key, value in table.items():
            if key in RESERVED_KEYS:
                continue

            _check_kind(key, payload.get(key), value, probe)
            payload[key] = value

    return payload, hooks


def _check_kind(key: str, prev: object, value: object, probe: _Probe) -> None:
    """Raise on a scalar/array/table kind clash for *key* across scopes."""
    if prev is None or _kind(prev) == _kind(value):
        return

    raise WtmError(
        f"config key '{key}' has conflicting kinds across scopes "
        f"({_kind(prev)} vs {_kind(value)} at {probe.file})"
    )


def _kind(value: object) -> str:
    """Classify a config value as ``table`` / ``array`` / ``scalar``."""
    if isinstance(value, dict):
        return "table"

    if isinstance(value, (list, tuple)):
        return "array"

    return "scalar"


# --- registry construction ---------------------------------------------------


def _build_registry(system: dict, user: dict, store: _Store) -> dict[str, Workspace]:
    """Build the workspace registry: declaration discovery, then payload probing.

    Phase 1 discovers each workspace's *declaration* -- its name, ``path``, and
    winning ``scope`` -- from the system/user files only (a workspace-local file
    cannot relocate its own workspace, so ``path`` is a global declaration).
    Phase 2 fills the payload (``description`` / ``base_branch`` / ``hooks`` /
    ``extra``) through the single presence-based locality merge over the
    workspace probe cascade, so the registry and probe engines never diverge.
    """
    # Phase 1: declaration index (name -> path + declaring scope; user wins).
    declarations: dict[str, tuple[Path, Scope]] = {}
    for scope, table in ((Scope.SYSTEM, system), (Scope.USER, user)):
        for name, entry in (table.get("workspaces") or {}).items():
            prev = declarations.get(name)
            if "path" not in entry and prev is None:
                raise WtmError(f"workspace '{name}' has no path")

            path = expand_path(entry["path"]) if "path" in entry else prev[0]
            declarations[name] = (path, scope)

    # Phase 2: fold each workspace's payload through the probe engine.
    registry: dict[str, Workspace] = {}
    for name, (path, scope) in declarations.items():
        ws = Workspace(name=name, path=path, scope=scope)
        payload, hooks = _merge_locality(store, _workspace_probes_for(name, path))
        registry[name] = replace(
            ws,
            description=_str_or_none(payload.get("description")),
            base_branch=_str_or_none(payload.get("base_branch")),
            hooks=hooks,
            extra={k: v for k, v in payload.items() if k not in ("description", "base_branch")},
        )

    return registry


def _workspace_probes_for(name: str, path: Path) -> list[_Probe]:
    """The workspace locality cascade for a bare (name, path), most-local-first.

    A free function mirroring :meth:`Config._workspace_probes`, usable during the
    registry build before any :class:`Workspace` object exists.
    """
    return [
        _Probe(Scope.WORKSPACE, ConfigAddress.workspace(path).file, ()),
        _Probe(Scope.USER, _user_config_path(), ("workspaces", name)),
        _Probe(Scope.SYSTEM, _system_config_path(), ("workspaces", name)),
    ]


def _global_probes() -> list[_Probe]:
    """The global top-level cascade (user then system), most-local-first.

    The tail of an inheritable key's chain: the top-level default read from the
    two non-entity files (e.g. the global ``base_branch``).
    """
    return [
        _Probe(Scope.USER, _user_config_path(), ()),
        _Probe(Scope.SYSTEM, _system_config_path(), ()),
    ]


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


# --- config paths ------------------------------------------------------------


def _global_scalar(user: dict, system: dict, key: str) -> str | None:
    """Resolve a global top-level scalar presence-based (user present else system)."""
    if key in user:
        return user[key]

    if key in system:
        return system[key]

    return None


def _system_config_path() -> Path:
    """The system config file path (env override or default)."""
    return Path(os.environ.get("WTM_SYSTEM_CONFIG") or "/etc/wtm/config.toml")


def _global_address(scope: Scope) -> ConfigAddress:
    """The user/system ConfigAddress for *scope* (the two non-entity files)."""
    return ConfigAddress.system() if scope is Scope.SYSTEM else ConfigAddress.user()


def _user_config_path() -> Path:
    """The user config file path (env override or XDG default)."""
    if override := os.environ.get("WTM_CONFIG"):
        return Path(override)

    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")

    return xdg / "wtm" / "config.toml"


# --- structural write helpers ------------------------------------------------


def _workspaces_table(doc: tomlkit.TOMLDocument):
    """Return the document's ``[workspaces]`` super-table, creating it if absent.

    A super-table renders its children as ``[workspaces.NAME]`` headers with no
    bare ``[workspaces]`` line.
    """
    existing = doc.get("workspaces")
    if existing is not None:
        return existing

    table = tomlkit.table(is_super_table=True)
    doc["workspaces"] = table

    return table


def _existing_entry(doc: tomlkit.TOMLDocument, name: str):
    """Return an existing ``[workspaces.NAME]`` entry, or raise if absent."""
    workspaces = doc.get("workspaces")
    if workspaces is None or name not in workspaces:
        raise WtmError(f"workspace '{name}' is not declared at user scope")

    return workspaces[name]


# --- TOML parsing helpers ----------------------------------------------------


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


def _annotation_from_payload(payload: dict[str, object], hooks: HookSpec) -> ProjectAnnotation:
    """Split a locality-merged payload into the known scalars plus ``extra``."""
    extra = {k: v for k, v in payload.items() if k not in ("description", "base_branch")}

    return ProjectAnnotation(
        description=_str_or_none(payload.get("description")),
        base_branch=_str_or_none(payload.get("base_branch")),
        hooks=hooks,
        extra=extra,
    )


def _str_or_none(value: object) -> str | None:
    """Coerce a present scalar to ``str``; ``None`` stays ``None``."""
    return None if value is None else str(value)


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


def _flag_env(name: str) -> bool:
    """Return whether an env var is set to a truthy value."""
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no")
