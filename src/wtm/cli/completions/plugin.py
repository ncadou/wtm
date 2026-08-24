"""Shell completion for plugin management verbs."""

from __future__ import annotations

from wtm.plugins import entry_point_names


def _complete_registered(ctx, incomplete: str) -> list[str]:
    """Registered plugin names (for remove/show)."""
    try:
        config = ctx.obj.config
    except Exception:
        return []

    return [n for n in config.registered_plugin_names() if n.startswith(incomplete)]


def _complete_discoverable(ctx, incomplete: str) -> list[str]:
    """Entry-point-visible plugin names (for add)."""
    try:
        names = entry_point_names()
    except Exception:
        return []

    return [n for n in names if n.startswith(incomplete)]
