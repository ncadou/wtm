"""Arbitrary-key validation: reserved-name refusal at the write/read sites.

The arbitrary-key facility must not shadow the reserved structural keys; these
validators are the single gate every writer funnels through.
"""

from __future__ import annotations

__all__ = [
    "validate_arbitrary_key",
    "validate_arbitrary_key_path",
    "validate_inheritable_element",
]

from wtm.config.model import NON_INHERITABLE_KEYS, RESERVED_KEYS
from wtm.exceptions import ReservedKeyError, WtmError


def validate_arbitrary_key(key: str) -> None:
    """Reject a reserved name as an arbitrary ``config`` key (raises on clash).

    The container tables (``hooks`` / ``projects`` / ``worktrees``), the
    user-file declarations (``path`` / ``default_workspace``), the global
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
    :data:`INHERITABLE_KEYS` (``base_branch``) are already inheritable -- opting
    them in is harmless and allowed.
    """
    validate_arbitrary_key(key)
    if key in NON_INHERITABLE_KEYS:
        raise WtmError(
            f"'{key}' is never inheritable (it describes a single entity); "
            f"it cannot be added to the inheritable allowlist"
        )
