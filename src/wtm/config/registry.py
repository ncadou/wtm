"""Workspace registry construction and active-workspace resolution.

Phase 1 of :func:`build_registry` discovers each workspace's *declaration*
(name, path, winning scope) from the system/user files; phase 2 fills its
payload through the locality-merge engine. :func:`resolve_active` implements
the documented precedence ``$WTM_WORKSPACE > cwd > default_workspace > sole
workspace``.
"""

from __future__ import annotations

__all__ = [
    "build_registry",
    "global_probes",
    "global_scalar",
    "resolve_active",
]

from pathlib import Path
from typing import TYPE_CHECKING

from wtm.config.addressing import ConfigAddress, Scope, system_config_path, user_config_path
from wtm.config.engine import (
    Probe,
    TierProbe,
    deep_merge,
    fold_tiers,
    merge_locality,
    read_table,
)
from wtm.config.generic import parse_inheritable_element
from wtm.config.model import (
    FLOOR_TIERS,
    INHERIT_FLOORS,
    INHERITABLE_DECL_KEY,
    INHERITABLE_KEYS,
    TYPED_INHERITABLE_KEYS,
    Workspace,
    str_or_none,
)
from wtm.core import expand_path
from wtm.exceptions import WtmError

if TYPE_CHECKING:
    from wtm.config.store import Store


def build_registry(system: dict, user: dict, store: Store) -> dict[str, Workspace]:
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
        probes = _workspace_probes_for(name, path)
        payload, hooks = merge_locality(store, probes)
        local = {k: v for k, v in payload.items() if k not in ("description", "base_branch")}
        registry[name] = Workspace(
            name=name,
            path=path,
            scope=scope,
            description=str_or_none(payload.get("description")),
            base_branch=str_or_none(payload.get("base_branch")),
            hooks=hooks,
            extra=_workspace_inherited_extra(store, probes, local),
        )

    return registry


def _workspace_inherited_extra(
    store: Store, ws_probes: list[Probe], local: dict[str, object]
) -> dict[str, object]:
    """Fold global-tier inherited values into a workspace's own ``extra``.

    A workspace's only ancestor is the global top-level default, so a
    user-declared inheritable key absent workspace-locally falls back to the
    global value. Mirrors ``Config._inherited_extra`` for the registry build,
    which runs before any :class:`Config` exists. Typed-surface keys
    (:data:`TYPED_INHERITABLE_KEYS`, i.e. ``base_branch``) are excluded (they
    own the typed base_branch-provenance surface), and a key's floor prunes
    the fold's rungs like everywhere else.
    """
    floors: dict[str, str | None] = {k: INHERIT_FLOORS.get(k) for k in INHERITABLE_KEYS}
    for probe in (*ws_probes, *global_probes()):
        declared = read_table(store, probe).get(INHERITABLE_DECL_KEY)
        if not isinstance(declared, (list, tuple)):
            continue

        for element in declared:
            decl = parse_inheritable_element(element, file=probe.file)
            if decl.key not in floors:
                floors[decl.key] = decl.floor

    own_tier = "workspace"
    merged = dict(local)
    for key, floor in floors.items():
        if key in TYPED_INHERITABLE_KEYS:
            continue

        below_floor = floor is not None and FLOOR_TIERS[own_tier] < FLOOR_TIERS[floor]
        if not below_floor and key in local and not isinstance(local[key], dict):
            continue

        rungs = [TierProbe("global", p) for p in global_probes()]
        if floor is not None:
            rungs = [tp for tp in rungs if FLOOR_TIERS[tp.tier] >= FLOOR_TIERS[floor]]

        effective, chain = fold_tiers(store, rungs, key)
        if not chain:
            if below_floor:
                merged.pop(key, None)
            continue

        keep_table = not below_floor and key in local and isinstance(local[key], dict)
        merged[key] = deep_merge(effective, local[key]) if keep_table else effective

    return merged


def _workspace_probes_for(name: str, path: Path) -> list[Probe]:
    """The workspace locality cascade for a bare (name, path), most-local-first.

    A free function mirroring the facade's per-workspace probe builder, usable
    during the registry build before any :class:`Workspace` object exists.
    """
    return [
        Probe(Scope.WORKSPACE, ConfigAddress.workspace(path).file, ()),
        Probe(Scope.USER, user_config_path(), ("workspaces", name)),
        Probe(Scope.SYSTEM, system_config_path(), ("workspaces", name)),
    ]


def global_probes() -> list[Probe]:
    """The global top-level cascade (user then system), most-local-first.

    The tail of an inheritable key's chain: the top-level default read from the
    two non-entity files (e.g. the global ``base_branch``).
    """
    return [
        Probe(Scope.USER, user_config_path(), ()),
        Probe(Scope.SYSTEM, system_config_path(), ()),
    ]


def resolve_active(
    registry: dict[str, Workspace],
    *,
    cwd: Path | None,
    default_workspace: str | None,
    selector: str | None = None,
) -> str | None:
    """Resolve the active workspace name per the documented precedence.

    ``selector > cwd (longest match) > default_workspace > sole workspace >
    None``. The *selector* is the ``$WTM_WORKSPACE`` value (passed in by the
    caller, from :class:`wtm.config.addressing.Settings`). A WORKSPACE named in
    a command's positionals overrides this per-target (see :mod:`wtm.target`);
    there is no ``-w`` flag. A ``None`` *cwd* (the shell's directory was
    removed) simply skips the cwd tier.
    """
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


def global_scalar(user: dict, system: dict, key: str) -> str | None:
    """Resolve a global top-level scalar presence-based (user present else system)."""
    if key in user:
        return user[key]

    if key in system:
        return system[key]

    return None
