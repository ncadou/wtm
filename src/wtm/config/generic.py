"""Arbitrary-key validation: reserved-name refusal at the write/read sites.

The arbitrary-key facility must not shadow the reserved structural keys; these
validators are the single gate every writer funnels through. This module also
parses ``inheritable`` array elements (a key name or ``{ key = ..., floor = ... }``
inline table) into their declaration form.
"""

from __future__ import annotations

__all__ = [
    "InheritableDecl",
    "parse_inheritable_element",
    "validate_arbitrary_key",
    "validate_arbitrary_key_path",
    "validate_inheritable_element",
]

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from wtm.config.model import (
    FLOOR_TIERS,
    NON_INHERITABLE_KEYS,
    RESERVED_KEYS,
    TYPED_INHERITABLE_KEYS,
)
from wtm.exceptions import ReservedKeyError, WtmError


def validate_arbitrary_key(key: str) -> None:
    """Reject a reserved name as an arbitrary ``config`` key (raises on clash).

    The container tables (``hooks`` / ``projects`` / ``worktrees``), the
    user-file declarations (``path`` / ``default-workspace``), the global
    registry table (``workspaces``), and the ``inheritable`` allowlist own their
    own typed surfaces; the arbitrary-key facility must not shadow them.
    """
    if key in RESERVED_KEYS:
        raise ReservedKeyError(
            f"'{key}' is a reserved key; use its dedicated command "
            f"(reserved: {', '.join(sorted(RESERVED_KEYS))})"
        )


def validate_arbitrary_key_path(key: str | tuple[str, ...]) -> None:
    """Reject a reserved *head* segment in a (possibly dotted) arbitrary key.

    A dotted path (``ci.runner.image``) names a cell inside a user section, so
    only its head segment (``ci``) can collide with a reserved container table
    -- deeper segments (``runner`` / ``image``) are harmless cells and may even
    be named ``workspaces`` or ``projects`` without shadowing the structural
    keys. A tuple *key* is already split; a string *key* is split on ``.`` here
    (the store treats a dotted string as a single literal part, but a user-typed
    key uses dots as separators). The bare-key case delegates to
    :func:`validate_arbitrary_key`.
    """
    head = key.split(".", 1)[0] if isinstance(key, str) else key[0]
    validate_arbitrary_key(head)


def validate_inheritable_element(key: str) -> None:
    """Reject a key that may not be opted into the ``inheritable`` allowlist.

    Anything hand-writable into ``inheritable`` is CLI-writable too, *except*
    where it contradicts a hardcoded rule: a reserved key (its own typed surface,
    :func:`validate_arbitrary_key`) or a hardcoded-non-inheritable builtin
    (:data:`NON_INHERITABLE_KEYS`, e.g. ``description``). The builtin
    :data:`INHERITABLE_KEYS` (``base-branch``) are already inheritable -- opting
    them in is harmless and allowed.
    """
    validate_arbitrary_key(key)
    if key in NON_INHERITABLE_KEYS:
        raise WtmError(
            f"'{key}' is never inheritable (it describes a single entity); "
            f"it cannot be added to the inheritable allowlist"
        )


@dataclass(frozen=True)
class InheritableDecl:
    """One parsed element of an ``inheritable`` array.

    :param key: the arbitrary key being opted into inheritance.
    :param floor: the deepest entity tier the key participates in, or ``None``
        for no floor (participates everywhere, including a worktree's own rung).
    """

    key: str
    floor: str | None = None


def parse_inheritable_element(element: object, file: Path | None = None) -> InheritableDecl:
    """Parse one ``inheritable`` array element into an :class:`InheritableDecl`.

    An element is either a bare key name (``"deploy_env"``, no floor) or a
    one-level inline table (``{ key = "region", floor = "project" }``); any
    other shape, unknown field, bad tier value, non-inheritable key, or a floor
    on a typed-surface builtin (:data:`TYPED_INHERITABLE_KEYS`, whose resolution
    is fixed to stay aligned with its typed read surface) raises ``WtmError``
    -- at declaration time via the CLI/handles, at read time for a malformed
    hand-edited array (the same hard-error treatment a kind clash gets).
    *file* only annotates error messages.
    """
    where = f" in {file}" if file is not None else ""
    if isinstance(element, str):
        decl = InheritableDecl(element)
    elif isinstance(element, Mapping):
        extra = set(element.keys()) - {"key", "floor"}
        if extra:
            raise WtmError(
                f"invalid inheritable element{where}: unknown field(s) "
                f"{', '.join(sorted(str(f) for f in extra))} (only 'key' and 'floor')"
            )
        if not isinstance(element.get("key"), str):
            raise WtmError(f"invalid inheritable element{where}: 'key' must be a string")

        floor = element.get("floor")
        if floor is not None and (not isinstance(floor, str) or floor not in FLOOR_TIERS):
            raise WtmError(
                f"invalid inheritable element{where}: 'floor' must be one of "
                f"{', '.join(FLOOR_TIERS)}"
            )

        decl = InheritableDecl(element["key"], floor)
    else:
        raise WtmError(
            f"invalid inheritable element{where}: expected a key name or "
            f"{{ key = ..., floor = ... }} table"
        )

    validate_inheritable_element(decl.key)
    if decl.floor is not None and decl.key in TYPED_INHERITABLE_KEYS:
        raise WtmError(
            f"'{decl.key}' owns a typed surface with fixed resolution; "
            f"it cannot take a floor{where}"
        )

    return decl
