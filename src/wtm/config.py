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
  / ``clear_base_branch``, ``set_project_annotation``) that own cross-key
  invariants (a rename fixes a dangling ``default_workspace``) and write only the
  user file.

Writes name their target scope explicitly; the "most-local home that exists"
placement policy is the caller's (see :mod:`wtm.workspace` / :mod:`wtm.project`).
Scalar values resolve ``CLI > env > worktree > project > workspace > user >
system > default``; inline hooks *accumulate* across scopes on top of the
builtin file hooks. ``WTM_NO_CONFIG`` suppresses all config files (reads) and
makes any write a hard error (mutating a file we are told to ignore is
contradictory); it is the test/CI isolation knob.

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
import tempfile
import tomllib
from collections.abc import Sequence
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


# --- generic value store -----------------------------------------------------


Key = str | tuple[str, ...]


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
    """Walk/create the intermediate tables for a nested write, returning the leaf table."""
    table: object = doc
    for part in parts:
        child = table.get(part)
        if child is None:
            child = tomlkit.table()
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
        local_file = workspace.path / name / ".wtm" / "config.toml"
        local = _parse_project_local(self._store.read(local_file))

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

        Evaluates a declarative cascade of ``(address, key)`` probes over the
        generic value facility, most-specific-first, returning the first hit and
        its tier: ``project`` (project-local file or the user/system registry
        annotation), ``workspace`` (workspace-local file or the registry entry),
        ``global`` (a top-level ``base_branch``), or ``(none)``. The project-local
        file and the registry annotation share the ``project`` tier: both are
        per-project overrides.
        """
        for address, key, tier in self._base_branch_cascade(workspace, name):
            value = self.get_value(address, key)
            if value:
                return value, tier

        return None, "(none)"

    def _base_branch_cascade(
        self, workspace: Workspace | None, name: str
    ) -> list[tuple[ConfigAddress, Key, str]]:
        """The ordered ``(address, key, tier)`` probes for a project's base branch."""
        user, system = ConfigAddress.user(), ConfigAddress.system()
        probes: list[tuple[ConfigAddress, Key, str]] = []
        if workspace is not None:
            ws_name, ws_path = workspace.name, workspace.path
            proj_key = ("workspaces", ws_name, "projects", name, "base_branch")
            ws_key = ("workspaces", ws_name, "base_branch")
            # Project tier: project-local file, then the user registry annotation.
            proj_addr = ConfigAddress.project(ws_path / name)
            probes.append((proj_addr, ("project", "base_branch"), "project"))
            probes.append((user, proj_key, "project"))
            # Workspace tier: workspace-local file, then the user/system entry.
            probes.append((ConfigAddress.workspace(ws_path), "base_branch", "workspace"))
            probes.append((user, ws_key, "workspace"))
            probes.append((system, ws_key, "workspace"))
        # Global tier: the top-level default (user wins over system).
        probes.append((user, "base_branch", "global"))
        probes.append((system, "base_branch", "global"))

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

        return _parse_worktree_local(self._store.read(worktree_dir / ".wtm" / "config.toml"))

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

    def set_project_annotation(
        self, workspace: str, project: str, key: str, value: str | None
    ) -> None:
        """Set (or clear) one project-annotation scalar under a workspace's registry entry.

        Writes ``[workspaces.WORKSPACE.projects.PROJECT] key = value`` into the
        user file (the user-scope fallback for a project whose dir does not yet
        exist); a ``None``/empty *value* removes the key. The workspace must
        already be declared at user scope.
        """
        doc = self._store.user_doc()
        entry = _existing_entry(doc, workspace)
        projects = entry.get("projects")
        if projects is None:
            projects = tomlkit.table(is_super_table=True)
            entry["projects"] = projects
        annotation = projects.get(project)
        if annotation is None:
            annotation = tomlkit.table()
            projects[project] = annotation
        if value:
            annotation[key] = value
        else:
            annotation.pop(key, None)
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
            _store=store,
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
            hooks=_parse_worktree_local(self._store.read(wt_dir / ".wtm" / "config.toml")),
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


def _build_registry(system: dict, user: dict, store: _Store) -> dict[str, Workspace]:
    """Build the workspace registry from system + user files (user wins)."""
    registry: dict[str, Workspace] = {}
    for scope, table in ((Scope.SYSTEM, system), (Scope.USER, user)):
        for name, entry in (table.get("workspaces") or {}).items():
            registry[name] = _merge_workspace(registry.get(name), name, entry, scope)

    # Fold each workspace's own local file on top of the registry declaration.
    for name, ws in list(registry.items()):
        local = store.read(ws.path / ".wtm" / "config.toml")
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


# --- config paths ------------------------------------------------------------


def _system_config_path() -> Path:
    """The system config file path (env override or default)."""
    return Path(os.environ.get("WTM_SYSTEM_CONFIG") or "/etc/wtm/config.toml")


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


def _parse_project_local(table: dict) -> ProjectAnnotation:
    """Parse a project-local file dict's ``[project]`` table."""
    project = table.get("project") or {}

    return ProjectAnnotation(
        description=project.get("description"),
        base_branch=project.get("base_branch"),
        hooks=_parse_hooks(project.get("hooks")),
    )


def _parse_worktree_local(table: dict) -> HookSpec:
    """Parse a worktree-local file dict's ``[hooks]`` table."""
    return _parse_hooks(table.get("hooks"))


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
