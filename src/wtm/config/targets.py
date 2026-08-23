"""The generic-target model: one entity addressed for arbitrary-key work."""

from __future__ import annotations

__all__ = ["GenericTarget"]

from collections.abc import Callable
from dataclasses import dataclass, field

from wtm.config.addressing import ConfigAddress, Scope
from wtm.config.engine import Probe, TierProbe


@dataclass(frozen=True)
class GenericTarget:
    """A named entity (worktree/project/workspace/global) ready for key work.

    *label* names the entity for messages, *probes* is its locality cascade
    (most-local-first, feeding reads), *write_target* maps a scope to its
    ``(address, prefix)`` write site, *allowed_scopes* gates ``-s/--scope``,
    and *default_scope* is the auto-default (the deepest scope whose dir
    exists). *inherit* maps each inheritable key to its extended, tier-tagged
    probe list -- the entity's own cascade followed by its ancestors' -- so
    reads report the same effective value a real command would take.
    """

    label: str
    probes: list[Probe]
    write_target: Callable[[Scope], tuple[ConfigAddress, tuple[str, ...]]]
    allowed_scopes: tuple[Scope, ...]
    default_scope: Scope
    inherit: dict[str, list[TierProbe]] = field(default_factory=dict)
