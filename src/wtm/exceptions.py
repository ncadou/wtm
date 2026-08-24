"""Fatal-error types shared across wtm: the base plus config-specific subclasses.

``WtmError`` is the app-wide fatal error (its message is printed as ``wtm:
...``); every other module raises it or one of its subclasses. The subclasses
mark the failure modes an embedder can branch on programmatically; all of them
carry a user-facing message exactly like the base.
"""

from __future__ import annotations

__all__ = [
    "KindConflictError",
    "ManifestError",
    "NoActiveWorkspaceError",
    "ReservedKeyError",
    "WriteProhibitedError",
    "WtmError",
]


class WtmError(Exception):
    """A fatal, user-facing error. Carries the message printed as ``wtm: ...``."""


class ReservedKeyError(WtmError):
    """A reserved key was used where only arbitrary keys are allowed."""


class KindConflictError(WtmError):
    """A key has conflicting value kinds (scalar/array/table) across scopes."""


class NoActiveWorkspaceError(WtmError):
    """No active workspace could be resolved for the operation."""


class WriteProhibitedError(WtmError):
    """A config write was refused (``WTM_NO_CONFIG``)."""


class ManifestError(WtmError):
    """A plugin manifest is missing, malformed, or unsupported."""
