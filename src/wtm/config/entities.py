"""Entity handles: the embedder-facing sugar over one resolved config entity.

A handle binds a :class:`GenericTarget` (the locality cascade, write sites,
allowed scopes) to convenience operations, so a scoped read or write is one
call instead of a resolve-then-address dance. Handles are created through
:meth:`Config.entity` -- count-based, mirroring the target grammar::

    cfg.entity()                          # cwd-inferred deepest entity
    cfg.entity("ws")                      # workspace
    cfg.entity("ws", "proj")              # project
    cfg.entity("ws", "proj", "feat")      # worktree

With no tokens and a cwd outside every declared workspace, the global tier is
the answer (a :class:`GlobalEntity`).

``scope=None`` on any write applies the centralized placement policy (the
deepest candidate scope whose directory exists); an explicit scope must be in
the entity's allowed set. Reads return native TOML values; writes are
string-only.
"""

from __future__ import annotations

__all__ = [
    "EntityHandle",
    "GlobalEntity",
    "ProjectEntity",
    "WorkspaceEntity",
    "WorktreeEntity",
]

from typing import TYPE_CHECKING

from wtm.config.addressing import ConfigAddress, Key, Scope, global_address
from wtm.config.engine import TierProbe
from wtm.config.generic import validate_inheritable_element
from wtm.config.model import (
    INHERITABLE_DECL_KEY,
    INHERITABLE_KEYS,
    ProjectAnnotation,
    ResolvedWorktree,
)
from wtm.config.registry import global_probes
from wtm.config.targets import GenericTarget
from wtm.config.writes import GLOBAL_WRITE_SCOPES, check_write_scope
from wtm.exceptions import WtmError

if TYPE_CHECKING:
    from wtm.config.config import Config


class EntityHandle:
    """One entity's key surface: resolved reads and scoped writes.

    Created by :meth:`Config.entity`; do not instantiate by hand. The wrapped
    :class:`GenericTarget` carries the locality probes and per-scope write
    sites; this class turns them into single calls.
    """

    kind = "entity"

    def __init__(
        self,
        config: Config,
        target: GenericTarget,
        *,
        workspace_name: str | None = None,
        project_name: str | None = None,
        name: str | None = None,
    ) -> None:
        self._config = config
        self._target = target
        self.workspace_name = workspace_name
        self.project_name = project_name
        self.name = name

    # -- identity --------------------------------------------------------------

    @property
    def label(self) -> str:
        """The entity's display label (as used in CLI messages)."""
        return self._target.label

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self._target.label}>"

    # -- reads -----------------------------------------------------------------

    def get(self, key: str, subkey: str | None = None) -> object | None:
        """Resolve *key* over the entity's full cascade (native value or ``None``).

        *key* may be dotted (``ci.runner.image``); *subkey* is an alternative to
        a trailing dotted segment. ``None`` means the key appears nowhere in the
        cascade. An inheritable key folds ancestor entities too, matching what a
        real command would use.
        """
        path = self._path(key, subkey)
        effective, _chain = self._config.resolve_generic_value(self._target, path)

        return effective

    def keys(self, *, deep: bool = False) -> list[str]:
        """Every resolvable key for this entity, sorted (dotted paths with *deep*)."""
        return [key for key, _rung in self._config.effective_keys(self._target, deep=deep)]

    # -- writes ----------------------------------------------------------------

    def set(self, key: str, value: str, *, scope: Scope | None = None) -> ConfigAddress:
        """Set scalar *key* = *value* at *scope* (default: the placement policy).

        Returns the address of the file that received the write.
        """
        address, prefix = self._write_site(scope)
        self._config.set_value(address, self._join(prefix, key), value)

        return address

    def unset(self, key: str, *, scope: Scope | None = None) -> ConfigAddress:
        """Remove *key* at *scope* (a whole table included; no-op when absent)."""
        address, prefix = self._write_site(scope)
        self._config.unset_value(address, self._join(prefix, key))

        return address

    def array_add(self, key: str, value: str, *, scope: Scope | None = None) -> ConfigAddress:
        """Add *value* to the string array at *key* (idempotent set semantics)."""
        address, prefix = self._write_site(scope)
        self._config.add_to_array(address, self._join(prefix, key), value)

        return address

    def array_remove(self, key: str, value: str, *, scope: Scope | None = None) -> ConfigAddress:
        """Strip every occurrence of *value* from the array at *key*."""
        address, prefix = self._write_site(scope)
        self._config.remove_from_array(address, self._join(prefix, key), value)

        return address

    def table_set(
        self, key: str, subkey: str, value: str, *, scope: Scope | None = None
    ) -> ConfigAddress:
        """Set *subkey* = *value* inside the section at *key*."""
        address, prefix = self._write_site(scope)
        self._config.set_table_value(address, self._join(prefix, key), subkey, value)

        return address

    def table_unset(self, key: str, subkey: str, *, scope: Scope | None = None) -> ConfigAddress:
        """Remove *subkey* from the section at *key* (a no-op when absent)."""
        address, prefix = self._write_site(scope)
        self._config.unset_table_value(address, self._join(prefix, key), subkey)

        return address

    def inheritable_add(self, key: str, *, scope: Scope | None = None) -> ConfigAddress:
        """Opt *key* into the entity's ``inheritable`` allowlist (validated).

        The declaration accumulates across scopes like ``hooks.on``; each
        element is checked (:func:`validate_inheritable_element`) so reserved or
        hardcoded-non-inheritable names are refused.
        """
        validate_inheritable_element(key)
        address, prefix = self._write_site(scope)
        self._config.add_to_array(address, self._join(prefix, INHERITABLE_DECL_KEY), key)

        return address

    def inheritable_remove(self, key: str, *, scope: Scope | None = None) -> ConfigAddress:
        """Remove *key* from the entity's ``inheritable`` allowlist at *scope*."""
        validate_inheritable_element(key)
        address, prefix = self._write_site(scope)
        self._config.remove_from_array(address, self._join(prefix, INHERITABLE_DECL_KEY), key)

        return address

    # -- internals ---------------------------------------------------------------

    def _path(self, key: str, subkey: str | None) -> Key:
        """Build an entity-relative dotted path from *key* plus optional *subkey*."""
        parts = [*key.split("."), *([subkey] if subkey is not None else [])]

        return parts[0] if len(parts) == 1 else tuple(parts)

    def _join(self, prefix: tuple[str, ...], key: str) -> Key:
        """Prepend the structural *prefix* to a possibly-dotted user *key*."""
        parts = (*prefix, *key.split("."))

        return parts[0] if len(parts) == 1 else parts

    def _write_site(self, scope: Scope | None) -> tuple[ConfigAddress, tuple[str, ...]]:
        """Validate/apply *scope*, returning ``(address, structural prefix)``."""
        resolved = scope if scope is not None else self._target.default_scope
        check_write_scope(resolved, self._target.allowed_scopes, self.kind)

        return self._target.write_target(resolved)


class GlobalEntity(EntityHandle):
    """The global tier: top-level defaults in the user/system files."""

    kind = "global"


class WorkspaceEntity(EntityHandle):
    """A declared workspace's key surface."""

    kind = "workspace"


class ProjectEntity(EntityHandle):
    """A discovered (or declared-inert) project's key surface."""

    kind = "project"

    def annotation(self) -> ProjectAnnotation:
        """The project's effective annotation (description/base_branch/hooks/extra)."""
        ws = self._config.workspaces[self.workspace_name]

        return self._config.project_annotation(ws, self.project_name)

    def base_branch_provenance(self) -> tuple[str | None, str]:
        """The effective base branch and its definition tier."""
        ws = self._config.workspaces[self.workspace_name]

        return self._config.base_branch_provenance(ws, self.project_name)


class WorktreeEntity(EntityHandle):
    """A worktree's key surface (no base_branch tier)."""

    kind = "worktree"

    def annotation(self) -> ResolvedWorktree:
        """The worktree's effective config (description/hooks/extra)."""
        ws = self._config.workspaces[self.workspace_name]
        project_dir = ws.path / self.project_name

        return self._config.resolve_worktree(project_dir, self.name)


def build_global_entity(config: Config) -> GlobalEntity:
    """Build the global-tier handle: top-level user/system probes only."""
    target = GenericTarget(
        label="global",
        probes=global_probes(),
        write_target=_global_write_target,
        allowed_scopes=GLOBAL_WRITE_SCOPES,
        default_scope=Scope.USER,
        inherit={
            key: [TierProbe("global", probe) for probe in global_probes()]
            for key in INHERITABLE_KEYS
        },
    )

    return GlobalEntity(config, target)


def _global_write_target(scope: Scope) -> tuple[ConfigAddress, tuple[str, ...]]:
    """Map USER/SYSTEM to their file addresses (the only global write sites)."""
    if scope not in GLOBAL_WRITE_SCOPES:
        raise WtmError(f"cannot write a global annotation at '{scope}' scope")

    return global_address(scope), ()
