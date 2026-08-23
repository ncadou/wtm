"""The resolution engines: locality cascades and the inheritance fold.

A :class:`Probe` is one rung of a locality cascade (a scope, its file, and the
table path to the entity's own table); :func:`merge_locality` folds a probe
list most-local-wins into a payload plus accumulated hooks, and
:func:`fold_tiers` folds a single key over tier-tagged probes into an effective
value plus its provenance chain. Kind clashes are hard errors everywhere.
"""

from __future__ import annotations

__all__ = [
    "Probe",
    "TierProbe",
    "deep_merge",
    "descend_value",
    "dotted_candidates",
    "fold_tiers",
    "merge_locality",
    "read_table",
]

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from wtm.config.addressing import Scope
from wtm.config.model import (
    CONTAINER_KEYS,
    RESERVED_KEYS,
    HookSpec,
    SourceRung,
    check_kind,
    merge_hooks,
    parse_hooks,
)

if TYPE_CHECKING:
    from wtm.config.store import Store


@dataclass(frozen=True)
class Probe:
    """One rung of a locality cascade: a scope, its file, and the table path.

    *table_path* is the sequence of keys to descend from the file root to reach
    the entity's own table (empty for a self-config file whose root *is* the
    entity's table).
    """

    scope: Scope
    file: Path
    table_path: tuple[str, ...]


@dataclass(frozen=True)
class TierProbe:
    """A probe tagged with the definition tier it contributes to a read.

    *tier* is the inheritance level (``project`` / ``workspace`` / ``global``)
    the *probe*'s value belongs to, or ``None`` for a plain (entity-local) key
    whose scope already names the source. The tier is orthogonal to the probe's
    scope (a project-tier value can live in the user file, say).
    """

    tier: str | None
    probe: Probe


def read_table(store: Store, probe: Probe) -> dict:
    """Return the raw table *probe* points at (``{}`` when any step is absent)."""
    node: object = store.read(probe.file)
    for part in probe.table_path:
        if not isinstance(node, dict):
            return {}

        node = node.get(part)
    if not isinstance(node, dict):
        return {}

    return node


def merge_locality(store: Store, probes: Sequence[Probe]) -> tuple[dict[str, object], HookSpec]:
    """Fold *probes* least-to-most-local into (payload, hooks).

    *probes* are ordered most-local-first (the caller's natural cascade order);
    the fold runs in reverse so more-local values overwrite. Reserved keys are
    stripped from the scalar payload: ``hooks`` accumulates into a single
    :class:`HookSpec` (builtins-first across scopes), ``projects`` /
    ``worktrees`` are child-descent containers handled elsewhere, and ``path`` /
    ``default_workspace`` are declaration keys that own their typed surfaces (so
    a workspace registry entry's own ``path`` never leaks into ``extra``). A kind
    mismatch (scalar / array / table) for the same key across scopes is a hard
    error.
    """
    payload: dict[str, object] = {}
    hooks = HookSpec()
    for probe in reversed(probes):
        table = read_table(store, probe)
        hooks = merge_hooks(hooks, parse_hooks(table.get("hooks")))
        for key, value in table.items():
            if key in RESERVED_KEYS:
                continue

            check_kind(key, payload.get(key), value, probe.file)
            payload[key] = deep_merge(payload.get(key), value) if key in payload else value

    return payload, hooks


def fold_tiers(
    store: Store, tiered: Sequence[TierProbe], key: str
) -> tuple[object | None, list[SourceRung]]:
    """Fold *key* over a tier-tagged probe list into (effective value, chain).

    Reads every rung, keeping those that carry *key* (skipping container tables),
    as :class:`SourceRung` records in most-local-first order -- the shared engine
    behind both ``config get`` and the inheritance-aware ``extra`` fold in the
    resolved view / registry. A scalar/array/table kind clash for *key* across
    rungs is a hard error, matching :func:`merge_locality` (one rule for every
    read surface).

    The *effective value* is the most-local rung's value for a scalar/array
    (most-local wins, no merge), or the deep-merge of every carrying rung for a
    table (least-to-most-local, most-local cell wins) -- so a section's cells
    combine across tiers instead of a whole-table override. The returned *chain*
    always carries each rung's own whole value (the material for ``--origin``
    per-cell provenance); its head is the most-local rung, not the merge.
    """
    chain: list[SourceRung] = []
    prev: object = None
    for tiered_probe in tiered:
        probe = tiered_probe.probe
        table = read_table(store, probe)
        if key in table and key not in CONTAINER_KEYS:
            value = table[key]
            check_kind(key, prev, value, probe.file)
            chain.append(SourceRung(probe.scope, value, probe.file, tiered_probe.tier))
            prev = value

    effective = chain[0].value if chain else None
    if chain and isinstance(effective, dict):
        effective = _deep_merge_chain(chain)

    return effective, chain


def deep_merge(base: object, override: object) -> object:
    """Recursively merge two config values; *override* wins per cell.

    Two tables combine cell-by-cell (a *override* cell replaces the *base* cell;
    a cell present in only one side is kept); any other pair (scalar/array vs
    anything) takes *override* wholesale. Kinds are assumed already validated
    by :func:`wtm.config.model.check_kind`. Returns a plain dict for a table
    result so downstream renderers never see a tomlkit container alias.
    """
    if isinstance(base, dict) and isinstance(override, dict):
        merged: dict[str, object] = dict(base)
        for key, value in override.items():
            merged[key] = deep_merge(merged.get(key), value) if key in merged else value

        return merged

    return override


def _deep_merge_chain(chain: Sequence[SourceRung]) -> object | None:
    """Deep-merge the values of *chain* least-to-most-local (most-local wins)."""
    if not chain:
        return None

    acc: object = chain[-1].value
    for rung in reversed(chain[:-1]):
        acc = deep_merge(rung.value, acc)

    return acc


def descend_value(value: object, parts: Sequence[str]) -> object | None:
    """Walk *parts* inside *value*, returning the cell or ``None`` when absent."""
    node: object = value
    for part in parts:
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None

    return node


def _cell_winner(chain: Sequence[SourceRung], rest: tuple[str, ...]) -> SourceRung | None:
    """The most-local rung whose whole value carries the cell at *rest*.

    Descends each rung's own value (most-local-first) by *rest*; the first
    non-``None`` hit is the cell's defining rung -- the per-cell provenance a
    deep completion label or ``--origin`` line prints. ``None`` when no rung
    carries the cell (a merged table's every cell comes from some rung, so this
    only happens for an out-of-tree *rest*).
    """
    for rung in chain:
        if descend_value(rung.value, rest) is not None:
            return rung

    return None


def dotted_candidates(
    table: dict[str, object], chain: list[SourceRung], rest: tuple[str, ...], prefix: str
) -> list[tuple[str, SourceRung]]:
    """Emit every node under *table* as a dotted path with its carrying rung.

    Walks *table* (a deep-merged effective value) in sorted key order, emitting
    each sub-table head and leaf cell as ``prefix.sub`` paired with the
    most-local rung that defines it (via :func:`_cell_winner`). Recurses into
    nested sub-tables, accumulating *rest* so a deep cell's winner descends the
    full path. Nodes no rung carries are skipped (defensive; the merge guarantees
    a carrier for every merged cell).
    """
    out: list[tuple[str, SourceRung]] = []
    for sub in sorted(table):
        value = table[sub]
        sub_rest = (*rest, sub)
        path = f"{prefix}.{sub}"
        winner = _cell_winner(chain, sub_rest)
        if winner is not None:
            out.append((path, winner))
        if isinstance(value, dict):
            out.extend(dotted_candidates(value, chain, sub_rest, path))

    return out
