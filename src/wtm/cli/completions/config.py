"""Config shell completion: generic keys, table cells, array elements, scope."""

from __future__ import annotations

import typer

from wtm.cli.common import _config_show_level, _rung_label
from wtm.cli.completions.locator import _complete_locate_project
from wtm.cli.completions.target import _complete_project, _complete_workspace_arg
from wtm.config import Config, validate_arbitrary_key
from wtm.core import current_dir
from wtm.hooks import Scope


def _config_key_candidates(*, array: bool, incomplete: str) -> list[tuple[str, str]]:
    """KEY candidates for the generic key commands: effective keys, labelled.

    Shared by ``config get``/``set``/``unset``: the cwd-inferred entity's
    non-``(unset)`` keys (entity-local arbitrary keys unioned with inheritable
    keys set on an ancestor), each labelled by its definition level -- the same
    ``tier``/``scope`` ``config get --origin`` prints. With *array* (``-a``),
    the one reserved array a user may mutate via ``-a`` -- ``inheritable`` (the
    opt-in allowlist) -- is offered too, labelled ``allowlist``. Builds its own
    config (the app callback has not fired) and is lenient, so completion never
    errors. Sorted by key.
    """
    try:
        config = Config.load()
        target = config.resolve_generic_target(current_dir(resolve=False))
        keys = config.effective_keys(target)
    except Exception:
        return []
    items: dict[str, str] = {
        key: _rung_label(rung) for key, rung in keys if key.startswith(incomplete)
    }
    if array and "inheritable".startswith(incomplete):
        items["inheritable"] = "allowlist"

    return sorted(items.items())


def _config_subkey_candidates(key: str, incomplete: str) -> list[str]:
    """SUBKEY candidates: the cells of the table at *key*, prefix-filtered.

    Shared by ``config get`` (named ``key`` argument) and ``config set``/
    ``unset -t`` (variadic ``args[0]``): the table's keys are offered when *key*
    resolves to a dict and nothing otherwise. Lenient like every completer;
    reserved keys are rejected by :func:`validate_arbitrary_key`, so a reserved
    KEY yields nothing rather than erroring.
    """
    try:
        validate_arbitrary_key(key)
        config = Config.load()
        target = config.resolve_generic_target(current_dir(resolve=False))
        effective, _ = config.resolve_generic_value(target, key)
    except Exception:
        return []
    if not isinstance(effective, dict):
        return []

    return [cell for cell in sorted(effective) if cell.startswith(incomplete)]


def _config_array_element_candidates(key: str, incomplete: str) -> list[str]:
    """VALUE candidates for ``-a``: the elements of the array at *key*.

    Used by ``config set -a``/``unset -a``: the array's elements are offered
    when *key* resolves to a list/tuple and nothing otherwise. ``inheritable``
    (the one reserved array ``-a`` may touch) skips :func:`validate_arbitrary_key`
    and is read directly, mirroring the commands' own guard. Lenient; the
    most-local array is offered (arrays do not merge across scopes).
    """
    try:
        if key != "inheritable":
            validate_arbitrary_key(key)
        config = Config.load()
        target = config.resolve_generic_target(current_dir(resolve=False))
        effective, _ = config.resolve_generic_value(target, key)
    except Exception:
        return []
    if not isinstance(effective, (list, tuple)):
        return []

    return [str(el) for el in effective if str(el).startswith(incomplete)]


def _complete_config_key(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Complete KEY for ``config get``: the cwd-inferred entity's effective keys.

    Every non-``(unset)`` key (entity-local present arbitrary keys unioned with
    inheritable keys set on an ancestor), each labelled by its definition level
    -- the same ``tier``/``scope`` ``config get --origin`` prints. Builds its own
    config (the app callback has not fired) and is lenient, so completion never
    errors. ``config get`` is cwd-inferred only (no locator prefix), so the
    entity is fixed by the working directory and the KEY slot needs no ``ctx``.
    """
    return _config_key_candidates(array=False, incomplete=incomplete)


def _complete_config_subkey(ctx: typer.Context, incomplete: str) -> list[str]:
    """Complete SUBKEY for ``config get``: the cells of the table at typed KEY.

    The fixed ``key`` argument is already in ``ctx.params`` (verified against the
    real bash protocol), so the table's keys are offered when KEY resolves to a
    dict and nothing otherwise. Lenient like every completer; reserved keys are
    rejected upstream by :func:`validate_arbitrary_key`, so a reserved KEY yields
    nothing rather than erroring.
    """
    key = ctx.params.get("key")
    if not key:
        return []

    return _config_subkey_candidates(key, incomplete)


def _complete_config_set(ctx: typer.Context, incomplete: str) -> list[tuple[str, str] | str]:
    """Complete ``config set KEY [SUBKEY] VALUE``: KEY (and SUBKEY for ``-t``).

    Slot 0 (KEY) offers the effective keys plus ``inheritable`` when ``-a`` (the
    one reserved array ``-a`` may touch); a new key is still freely typed.
    Slot 1 with ``-t`` offers the cells of the table at ``args[0]``; without
    ``-t`` it is the free-form VALUE, which has no completion source. VALUE (the
    trailing slot, scalar or ``-t`` cell) is never completed. Reads the variadic
    ``args`` and the ``-a``/``-t`` flags from ``ctx.params`` (or ``ctx.args``
    during option-value completion).
    """
    args = list(ctx.params.get("args") or ctx.args or [])
    array = bool(ctx.params.get("array", False))
    table = bool(ctx.params.get("table", False))
    if not args:
        return _config_key_candidates(array=array, incomplete=incomplete)
    if len(args) == 1 and table:
        return _config_subkey_candidates(args[0], incomplete)

    return []


def _complete_config_unset(ctx: typer.Context, incomplete: str) -> list[tuple[str, str] | str]:
    """Complete ``config unset KEY [SUBKEY | VALUE]``: only what is removable.

    Mirrors :func:`_complete_config_set` for KEY (effective keys plus
    ``inheritable`` when ``-a``) and ``-t`` SUBKEY (table cells). The ``-a``
    VALUE slot -- the element to strip from the array at KEY -- additionally
    offers the array's current elements (you can only remove what is there);
    the scalar VALUE slot has no completion source (only KEY is needed).
    """
    args = list(ctx.params.get("args") or ctx.args or [])
    array = bool(ctx.params.get("array", False))
    table = bool(ctx.params.get("table", False))
    if not args:
        return _config_key_candidates(array=array, incomplete=incomplete)
    if len(args) == 1:
        if table:
            return _config_subkey_candidates(args[0], incomplete)
        if array:
            return _config_array_element_candidates(args[0], incomplete)

    return []


def _complete_config_show(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Complete TARGET for ``config show``: per-level dispatch on the level flag.

    The four mutually-exclusive level flags (``--all``/``--workspace``/
    ``--project``/``--worktree``) are read from ``ctx.params`` and routed to the
    existing completer that matches that level's target grammar: no level or
    ``--all`` take no TARGET (nothing completes); ``--workspace`` reuses the
    workspace-only slot-0 completer; ``--project`` reuses the count-based
    ``[[WORKSPACE] PROJECT]`` completer; ``--worktree`` reuses the PROJECT-pinned
    qualifier-only locator (the dumped NAME may be absent, so the leaf stays
    literal). Lenient via the delegated completers.
    """
    level = _config_show_level(
        bool(ctx.params.get("all_workspaces", False)),
        bool(ctx.params.get("workspace", False)),
        bool(ctx.params.get("project", False)),
        bool(ctx.params.get("worktree", False)),
    )
    if level in (None, "all"):
        return []
    if level == "workspace":
        return _complete_workspace_arg(ctx, incomplete)
    if level == "project":
        return _complete_project(ctx, incomplete)

    return _complete_locate_project(ctx, incomplete)


def _complete_scope(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Complete ``-s/--scope`` for the generic-key/hook commands: allowed scopes.

    The cwd-inferred entity's :attr:`GenericTarget.allowed_scopes` (what
    :func:`wtm.cli.common._generic_scope` would accept) are offered, the
    auto-default scope (the deepest existing dir, used when ``--scope`` is
    omitted) labelled ``default``. On any failure (no active workspace, cwd
    outside one, ...) the full enum degrades gracefully -- preserving typer's
    default behaviour -- rather than the empty list every other completer
    returns, because ``--scope`` is an enum option whose all-5 default is already
    visible to users outside a workspace.
    """
    try:
        config = Config.load()
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
