"""Config shell completion: arbitrary keys, table cells, array elements, scope.

The arbitrary-key commands (``config set``/``unset``/``get``) accept an optional
**locator** prefix naming the target entity. Because both the locator and the
key group are variable-token positionals sharing a single variadic, every slot
unions two readings (three for ``get``, whose SUBKEY is bare-optional):

- **Continuation**: the typed prefix is a *partial* locator; offer the next tier
  via :func:`_locate_candidates`.
- **Completion**: the typed prefix is a *complete* locator (or empty = cwd);
  resolve it and offer the next post-locator positional (KEY / SUBKEY / VALUE)
  from that entity's probe cascade.
"""

from __future__ import annotations

import typer

from wtm.cli.common import _rung_label
from wtm.cli.completions.locator import _labelled_locations, _locate_candidates
from wtm.config import FLOOR_TIERS, Config, GenericTarget, validate_arbitrary_key
from wtm.core import current_dir
from wtm.hooks import Scope
from wtm.locator import LocationIndex, Resolved, build_index, locate

# --- target-parameterized helpers (shared by every reading) ----------------


def _config_key_items(
    config: Config, target: GenericTarget, *, array: bool, incomplete: str
) -> list[tuple[str, str]]:
    """KEY candidates for one entity: effective keys, labelled by definition level.

    The target-parameterized core of the KEY completion: every non-``(unset)``
    key (entity-local arbitrary keys unioned with inheritable keys set on an
    ancestor), each labelled ``config in <level>`` (its ``tier``/``scope``, the
    same definition level ``config get --origin`` prints) -- prefixed to avoid
    colliding with the locator kind labels (``workspace``/``project``/...
    ``in <context>``) that share this completion's label column. Table-valued
    keys are walked deep, so every addressable cell also appears as a dotted
    path (``ci.runner.image``) labelled by its own carrying rung -- the dotted
    counterpart of the one-level SUBKEY completion. With *array* (``-a``),
    ``inheritable`` (the opt-in allowlist) is offered too, labelled
    ``allowlist`` (a role, not a provenance tier). Lenient; sorted by key.
    """
    try:
        keys = config.effective_keys(target, deep=True)
    except Exception:
        return []
    items: dict[str, str] = {
        key: f"config in {_rung_label(rung)}" for key, rung in keys if key.startswith(incomplete)
    }
    if array and "inheritable".startswith(incomplete):
        items["inheritable"] = "allowlist"

    return sorted(items.items())


def _config_subkey_items(
    config: Config, target: GenericTarget, key: str, incomplete: str
) -> list[str]:
    """SUBKEY candidates: the cells of the table at *key* on *target*, prefix-filtered."""
    try:
        validate_arbitrary_key(key)
        effective, _ = config.resolve_generic_value(target, key)
    except Exception:
        return []
    if not isinstance(effective, dict):
        return []

    return [cell for cell in sorted(effective) if cell.startswith(incomplete)]


def _config_array_element_items(
    config: Config, target: GenericTarget, key: str, incomplete: str
) -> list[str]:
    """VALUE candidates for ``-a``: the elements of the array at *key* on *target*."""
    try:
        if key != "inheritable":
            validate_arbitrary_key(key)
        effective, _ = config.resolve_generic_value(target, key)
    except Exception:
        return []
    if not isinstance(effective, (list, tuple)):
        return []

    return [str(el) for el in effective if str(el).startswith(incomplete)]


def _resolve_target_for_completion(
    config: Config, index: LocationIndex, loc_args: list[str]
) -> GenericTarget | None:
    """Build a :class:`GenericTarget` from locator tokens, leniently (for completion).

    Returns ``None`` on not-found or ambiguity (the completion reading is
    skipped, so the user sees only the continuation reading and can type more
    to disambiguate). Empty *loc_args* yields the cwd-inferred entity.
    """
    if not loc_args:
        return config.resolve_generic_target(current_dir(resolve=False))
    resolution = locate(loc_args, index, tier=None)
    if isinstance(resolution, Resolved):
        loc = resolution.location

        return config.resolve_generic_target_for(loc.workspace, loc.project, loc.name)

    return None


def _merge_union(
    *groups: list[tuple[str, str] | str],
) -> list[tuple[str, str] | str]:
    """Merge completion groups, deduping by name (first occurrence wins)."""
    seen: set[str] = set()
    result: list[tuple[str, str] | str] = []
    for group in groups:
        for item in group:
            name = item[0] if isinstance(item, tuple) else item
            if name not in seen:
                seen.add(name)
                result.append(item)

    return result


# --- cwd-only wrappers (backward-compatible, used by tests) ----------------


def _config_key_candidates(*, array: bool, incomplete: str) -> list[tuple[str, str]]:
    """KEY candidates for the cwd-inferred entity (backward-compatible wrapper)."""
    try:
        config = Config.load()
        target = config.resolve_generic_target(current_dir(resolve=False))
    except Exception:
        return []

    return _config_key_items(config, target, array=array, incomplete=incomplete)


def _config_subkey_candidates(key: str, incomplete: str) -> list[str]:
    """SUBKEY candidates for the cwd-inferred entity (backward-compatible wrapper)."""
    try:
        config = Config.load()
        target = config.resolve_generic_target(current_dir(resolve=False))
    except Exception:
        return []

    return _config_subkey_items(config, target, key, incomplete)


def _config_array_element_candidates(key: str, incomplete: str) -> list[str]:
    """Array element candidates for the cwd-inferred entity (backward-compatible wrapper)."""
    try:
        config = Config.load()
        target = config.resolve_generic_target(current_dir(resolve=False))
    except Exception:
        return []

    return _config_array_element_items(config, target, key, incomplete)


def _complete_config_key(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Complete KEY for the cwd-inferred entity (legacy named-arg callback)."""
    return _config_key_candidates(array=False, incomplete=incomplete)


def _complete_config_subkey(ctx: typer.Context, incomplete: str) -> list[str]:
    """Complete SUBKEY for the cwd-inferred entity (legacy named-arg callback)."""
    key = ctx.params.get("key")
    if not key:
        return []

    return _config_subkey_candidates(key, incomplete)


# --- union completion callbacks (locator + key group) ----------------------


def _complete_config_set(ctx: typer.Context, incomplete: str) -> list[tuple[str, str] | str]:
    """Complete ``config set [LOCATOR] KEY [SUBKEY] VALUE``.

    Every slot unions a **continuation reading** (the typed prefix is a partial
    locator; offer the next tier) with a **completion reading** (some prefix is
    a complete locator; offer the next key-group positional from that entity).
    The key-group trailing count N is flag-decided (3 for ``-t``, else 2), so
    the split point is inferred from count + flags. VALUE is never completed.
    """
    args = list(ctx.params.get("args") or ctx.args or [])
    array = bool(ctx.params.get("array", False))
    table = bool(ctx.params.get("table", False))

    try:
        config = Config.load()
        index = build_index(config)
    except Exception:
        return []
    multi_ws = len(index.workspaces) > 1

    # --- continuation: all typed args are a partial locator ---
    continuation = _labelled_locations(_locate_candidates(args, index, None), multi_ws)

    # --- completion readings ---
    completion: list[tuple[str, str] | str] = []
    cwd_target = config.resolve_generic_target(current_dir(resolve=False))

    # k=0 (no locator, cwd entity): which key-group slot are we at?
    if not args:
        completion.extend(
            _config_key_items(config, cwd_target, array=array, incomplete=incomplete)
        )
    elif table and len(args) == 1:
        completion.extend(_config_subkey_items(config, cwd_target, args[0], incomplete))

    # k=len(args) (all typed args as locator): current slot = KEY
    if args:
        target = _resolve_target_for_completion(config, index, args)
        if target is not None:
            completion.extend(
                _config_key_items(config, target, array=array, incomplete=incomplete)
            )

    # table, k=len(args)-1 (all but last as locator): current slot = SUBKEY
    if table and len(args) >= 2:
        target = _resolve_target_for_completion(config, index, args[:-1])
        if target is not None:
            completion.extend(_config_subkey_items(config, target, args[-1], incomplete))

    return _merge_union(continuation, completion)


def _complete_config_unset(ctx: typer.Context, incomplete: str) -> list[tuple[str, str] | str]:
    """Complete ``config unset [LOCATOR] KEY [SUBKEY | VALUE]``.

    Mirrors :func:`_complete_config_set` with the same union; the ``-a`` VALUE
    slot additionally offers the array's current elements (you can only remove
    what is there). N is 2 for ``-a``/``-t``, else 1.
    """
    args = list(ctx.params.get("args") or ctx.args or [])
    array = bool(ctx.params.get("array", False))
    table = bool(ctx.params.get("table", False))

    try:
        config = Config.load()
        index = build_index(config)
    except Exception:
        return []
    multi_ws = len(index.workspaces) > 1

    # --- continuation ---
    continuation = _labelled_locations(_locate_candidates(args, index, None), multi_ws)

    # --- completion readings ---
    completion: list[tuple[str, str] | str] = []
    cwd_target = config.resolve_generic_target(current_dir(resolve=False))

    # k=0 (cwd entity)
    if not args:
        completion.extend(
            _config_key_items(config, cwd_target, array=array, incomplete=incomplete)
        )
    elif len(args) == 1:
        if table:
            completion.extend(_config_subkey_items(config, cwd_target, args[0], incomplete))
        elif array:
            completion.extend(_config_array_element_items(config, cwd_target, args[0], incomplete))

    # k=len(args) (all typed args as locator): current slot = KEY
    if args:
        target = _resolve_target_for_completion(config, index, args)
        if target is not None:
            completion.extend(
                _config_key_items(config, target, array=array, incomplete=incomplete)
            )

    # table/array, k=len(args)-1 (all but last as locator): current slot = SUBKEY/VALUE
    if len(args) >= 2:
        target = _resolve_target_for_completion(config, index, args[:-1])
        if target is not None:
            if table:
                completion.extend(_config_subkey_items(config, target, args[-1], incomplete))
            elif array:
                completion.extend(
                    _config_array_element_items(config, target, args[-1], incomplete)
                )

    return _merge_union(continuation, completion)


def _complete_config_get(ctx: typer.Context, incomplete: str) -> list[tuple[str, str] | str]:
    """Complete ``config get [LOCATOR] KEY [SUBKEY]``.

    A three-way union at every slot: **continuation** (partial locator),
    **KEY reading** (complete locator, current slot = KEY), and **SUBKEY
    reading** (the last typed arg is a table KEY on the located or cwd entity,
    current slot = SUBKEY). The SUBKEY reading mirrors the command's data-type
    inference: a name that is a table on the entity wins as a SUBKEY parent.
    """
    args = list(ctx.params.get("args") or ctx.args or [])

    try:
        config = Config.load()
        index = build_index(config)
    except Exception:
        return []
    multi_ws = len(index.workspaces) > 1

    # --- continuation ---
    continuation = _labelled_locations(_locate_candidates(args, index, None), multi_ws)

    # --- completion readings ---
    completion: list[tuple[str, str] | str] = []
    cwd_target = config.resolve_generic_target(current_dir(resolve=False))

    # k=0 (cwd entity)
    if not args:
        completion.extend(
            _config_key_items(config, cwd_target, array=False, incomplete=incomplete)
        )
    elif len(args) == 1:
        completion.extend(_config_subkey_items(config, cwd_target, args[0], incomplete))

    # k=len(args) (all typed args as locator): current slot = KEY
    if args:
        target = _resolve_target_for_completion(config, index, args)
        if target is not None:
            completion.extend(
                _config_key_items(config, target, array=False, incomplete=incomplete)
            )

    # k=len(args)-1 (all but last as locator, last as KEY): current slot = SUBKEY
    if len(args) >= 2:
        target = _resolve_target_for_completion(config, index, args[:-1])
        if target is not None:
            completion.extend(_config_subkey_items(config, target, args[-1], incomplete))

    return _merge_union(continuation, completion)


# --- config show completion (union, no level dispatch) -----------------------


def _complete_config_show(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Complete the positional for ``config show``: union locator candidates.

    The positional is an unpinned locator (all tiers: worktree, project,
    workspace), enriched with config-only entities (``include_config_only=True``)
    so declared-but-absent projects and worktrees resolve too. No level flags
    -- the old per-level dispatch is gone.
    """
    args = list(ctx.params.get("args") or ctx.args or [])

    try:
        config = Config.load()
        index = build_index(config, include_config_only=True)
    except Exception:
        return []
    multi_ws = len(index.workspaces) > 1

    return _labelled_locations(_locate_candidates(args, index, None), multi_ws)


# --- locator-aware scope completion ----------------------------------------


def _complete_floor(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Complete ``--floor``: the four entity tiers, deepest-participation first."""
    return [
        (tier, f"key participates at {tier} tier and above")
        for tier in FLOOR_TIERS
        if tier.startswith(incomplete)
    ]


def _complete_scope(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Complete ``-s/--scope``: allowed scopes for the locator-named or cwd entity.

    When the calling command has a variadic ``args`` (``config set``/``unset``),
    the locator prefix is extracted and the entity resolved; otherwise the
    cwd-inferred entity is used (``hook set``/``unset``, which stay cwd-bound).
    On any failure the full enum degrades gracefully -- preserving typer's
    default behaviour -- rather than the empty list every other completer
    returns, because ``--scope`` is an enum option whose all-5 default is
    already visible to users outside a workspace.
    """
    try:
        config = Config.load()
        args = list(ctx.params.get("args") or [])
        if args:
            index = build_index(config)
            target = _resolve_target_for_completion(config, index, args)
            if target is None:
                target = config.resolve_generic_target(current_dir(resolve=False))
        else:
            target = config.resolve_generic_target(current_dir(resolve=False))
        allowed = target.allowed_scopes
        default = target.default_scope
    except Exception:
        return [(s.value, "") for s in Scope]

    return [
        (s.value, "default" if s == default else "")
        for s in allowed
        if s.value.startswith(incomplete)
    ]
