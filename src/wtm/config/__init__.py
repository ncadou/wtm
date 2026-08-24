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

This package is the *only* place that reads or writes ``.wtm`` config -- no
other part of the app touches ``tomlkit`` or builds a ``.wtm/config.toml``
path. It offers three tiers, all on :class:`Config`:

* an **arbitrary-key facility** -- :meth:`Config.get_value` /
  :meth:`Config.set_value` / :meth:`Config.unset_value` -- addressing any
  scope through a :class:`ConfigAddress` and a key. This is the substrate
  every other tier is built on;
* a **typed read view** (the dataclasses in :mod:`wtm.config.model`) resolving
  scalars most-local-first via locality merges over the value facility;
* a **structural write tier** -- the named registry mutations that own
  cross-key invariants.

Writes name their target scope explicitly; config-file scalars resolve
most-local-first ``worktree > project > workspace > user > system > default``.
Inline hooks *accumulate* across scopes on top of the builtin file hooks.
``WTM_NO_CONFIG`` suppresses all config files (reads) and makes any write a
hard error; it is the test/CI isolation knob.

Import from this package root only: the submodules are internal layout, not
API. The public contract is whatever this ``__init__`` re-exports.
"""

from __future__ import annotations

from wtm.config.addressing import ConfigAddress, DepthTier, Key, Scope, Settings
from wtm.config.config import Config, resolve_project_dir
from wtm.config.entities import (
    EntityHandle,
    GlobalEntity,
    ProjectEntity,
    WorkspaceEntity,
    WorktreeEntity,
)
from wtm.config.generic import (
    InheritableDecl,
    parse_inheritable_element,
    parse_policy_value,
    validate_arbitrary_key,
    validate_arbitrary_key_path,
    validate_inheritable_element,
)
from wtm.config.manifest import (
    MANIFEST_FIELDS,
    PLUGIN_API_VERSION,
    PluginManifest,
    parse_manifest,
)
from wtm.config.model import (
    FLOOR_TIERS,
    HOOKS_POLICY_CELLS,
    INHERIT_FLOORS,
    INHERITABLE_KEYS,
    NON_INHERITABLE_KEYS,
    PLUGINS_DECL_KEY,
    RESERVED_KEYS,
    TYPED_INHERITABLE_KEYS,
    ConfigSlice,
    HookSpec,
    ProjectAnnotation,
    ResolvedProject,
    ResolvedWorkspace,
    ResolvedWorktree,
    SliceTarget,
    SourceRung,
    Workspace,
)
from wtm.config.slice import config_slice_json
from wtm.config.targets import GenericTarget
from wtm.config.values import (
    TYPED_VALUE_FORMATS,
    TomlValue,
    json_leaf_default,
    parse_toml_value,
    parse_typed_value,
    render_scalar,
)
from wtm.config.writes import (
    PROJECT_WRITE_SCOPES,
    WORKSPACE_WRITE_SCOPES,
    WORKTREE_WRITE_SCOPES,
    check_key_floor,
    check_write_scope,
    project_default_scope,
    scope_list,
    tier_of_write_site,
    workspace_default_scope,
    worktree_default_scope,
)

__all__ = [
    "Config",
    "ConfigAddress",
    "ConfigSlice",
    "DepthTier",
    "EntityHandle",
    "FLOOR_TIERS",
    "GenericTarget",
    "GlobalEntity",
    "HOOKS_POLICY_CELLS",
    "HookSpec",
    "INHERITABLE_KEYS",
    "INHERIT_FLOORS",
    "InheritableDecl",
    "Key",
    "MANIFEST_FIELDS",
    "NON_INHERITABLE_KEYS",
    "PLUGINS_DECL_KEY",
    "PLUGIN_API_VERSION",
    "PROJECT_WRITE_SCOPES",
    "PluginManifest",
    "ProjectAnnotation",
    "ProjectEntity",
    "RESERVED_KEYS",
    "ResolvedProject",
    "ResolvedWorkspace",
    "ResolvedWorktree",
    "Scope",
    "Settings",
    "SliceTarget",
    "SourceRung",
    "TYPED_INHERITABLE_KEYS",
    "TYPED_VALUE_FORMATS",
    "TomlValue",
    "WORKSPACE_WRITE_SCOPES",
    "WORKTREE_WRITE_SCOPES",
    "Workspace",
    "WorkspaceEntity",
    "WorktreeEntity",
    "check_key_floor",
    "check_write_scope",
    "config_slice_json",
    "json_leaf_default",
    "parse_inheritable_element",
    "parse_manifest",
    "parse_policy_value",
    "parse_toml_value",
    "parse_typed_value",
    "project_default_scope",
    "render_scalar",
    "resolve_project_dir",
    "scope_list",
    "tier_of_write_site",
    "validate_arbitrary_key",
    "validate_arbitrary_key_path",
    "validate_inheritable_element",
    "workspace_default_scope",
    "worktree_default_scope",
]
