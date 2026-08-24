"""The plugin manifest contract: the declarative payload a plugin contributes.

A :class:`PluginManifest` is what every transport delivers in one shape: the
``wtm.plugins`` entry-point object (a mapping, or a zero-arg callable returning
one) or the parsed JSON stdout of ``<command> wtm-manifest --json``. It declares
the plugin's hook commands per event, the arbitrary-key namespaces it owns, the
inheritable keys it opts in (with optional floors), and optional default values
the ``plugin add --seed`` surface writes once.

Parsing here checks *shape* only -- kinds, required fields, unknown fields --
so this module stays free of the hook vocabulary (:mod:`wtm.hooks` validates
event names where manifests are actually loaded). Every parse failure raises
:class:`wtm.exceptions.ManifestError`; the loader turns those into
warn-and-skip problems rather than fatal errors.
"""

from __future__ import annotations

__all__ = [
    "PLUGIN_API_VERSION",
    "MANIFEST_FIELDS",
    "PluginManifest",
    "parse_manifest",
]

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from wtm.config.generic import InheritableDecl, parse_inheritable_element
from wtm.config.model import RESERVED_KEYS
from wtm.config.values import TomlValue
from wtm.exceptions import ManifestError, WtmError

#: The manifest API version this wtm speaks; higher versions are refused with
#: an upgrade hint rather than misread.
PLUGIN_API_VERSION = 1

#: The manifest fields accepted at API version 1 (unknown fields are refused,
#: since a newer field implies a newer API version anyway).
MANIFEST_FIELDS = frozenset(
    {"name", "api_version", "hooks", "namespaces", "inheritable", "defaults"}
)


@dataclass(frozen=True)
class PluginManifest:
    """What a registered plugin contributes to wtm's behavior.

    :param name: the canonical identity (matches the registry cell and the
        entry-point or command name).
    :param api_version: the manifest contract version the plugin was built for.
    :param source: how it was discovered: ``"entry-point"`` or ``"command"``.
    :param origin: display provenance (module/dist name or executable path).
    :param hooks: event -> commands fired as inline hooks on that event.
    :param namespaces: heads of arbitrary-key paths the plugin owns.
    :param inheritable: keys the plugin opts into inheritance while active,
        each carrying its floor (or ``None``).
    :param defaults: ``((key parts...), value)`` pairs written by
        ``plugin add --seed`` into the chosen scope's file top level.
    """

    name: str
    api_version: int = PLUGIN_API_VERSION
    source: str = "entry-point"
    origin: str | None = None
    hooks: dict[str, tuple[str, ...]] = field(default_factory=dict)
    namespaces: tuple[str, ...] = ()
    inheritable: tuple[InheritableDecl, ...] = ()
    defaults: tuple[tuple[tuple[str, ...], TomlValue], ...] = ()

    def to_json(self) -> dict:
        """The transport form: the plain mapping both transports carry."""
        result: dict = {"name": self.name, "api_version": self.api_version}
        if self.hooks:
            result["hooks"] = {event: list(cmds) for event, cmds in self.hooks.items()}
        if self.namespaces:
            result["namespaces"] = list(self.namespaces)
        if self.inheritable:
            result["inheritable"] = [
                {"key": decl.key, **({"floor": decl.floor} if decl.floor else {})}
                for decl in self.inheritable
            ]
        if self.defaults:
            result["defaults"] = [
                {"key": ".".join(parts), "value": value} for parts, value in self.defaults
            ]

        return result


def parse_manifest(
    raw: object, *, name: str, source: str, origin: str | None = None
) -> PluginManifest:
    """Parse and validate a raw manifest payload into a :class:`PluginManifest`.

    *name* is the identity the plugin is registered under; a ``name`` field in
    the payload must agree with it. Unknown fields are refused (a newer field
    implies a newer ``api_version``, which is refused separately), as is an
    ``api_version`` above :data:`PLUGIN_API_VERSION`.
    """
    label = f"plugin '{name}'"
    if not isinstance(raw, Mapping):
        raise ManifestError(f"{label}: manifest must be a JSON/TOML object")

    unknown = set(raw) - MANIFEST_FIELDS
    if unknown:
        raise ManifestError(
            f"{label}: unknown manifest field(s) {', '.join(sorted(unknown))} "
            f"(known: {', '.join(sorted(MANIFEST_FIELDS))})"
        )

    raw_name = raw.get("name")
    if raw_name is not None and raw_name != name:
        raise ManifestError(f"{label}: manifest names itself '{raw_name}'")

    api_version = raw.get("api_version", PLUGIN_API_VERSION)
    if not isinstance(api_version, int) or isinstance(api_version, bool):
        raise ManifestError(f"{label}: 'api_version' must be an integer")
    if api_version > PLUGIN_API_VERSION:
        raise ManifestError(
            f"{label}: manifest requires API version {api_version} "
            f"(this wtm supports {PLUGIN_API_VERSION}); upgrade wtm"
        )

    return PluginManifest(
        name=name,
        api_version=api_version,
        source=source,
        origin=origin,
        hooks=_parse_hooks(raw.get("hooks"), label),
        namespaces=_parse_namespaces(raw.get("namespaces"), label),
        inheritable=_parse_inheritable(raw.get("inheritable"), label),
        defaults=_parse_defaults(raw.get("defaults"), label),
    )


def _parse_hooks(value: object, label: str) -> dict[str, tuple[str, ...]]:
    """Parse the ``hooks`` map: event -> one command or a list of commands.

    Event names are validated against the lifecycle vocabulary by the loader;
    here only shape and non-empty command strings are enforced.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ManifestError(f"{label}: 'hooks' must be an object of event -> command(s)")

    hooks: dict[str, tuple[str, ...]] = {}
    for event, commands in value.items():
        if not isinstance(event, str) or not event:
            raise ManifestError(f"{label}: 'hooks' event names must be non-empty strings")
        items = (commands,) if isinstance(commands, str) else commands
        if not isinstance(items, Sequence) or isinstance(items, str):
            raise ManifestError(
                f"{label}: hooks.{event} must be a command string or array of strings"
            )
        for command in items:
            if not isinstance(command, str) or not command.strip():
                raise ManifestError(
                    f"{label}: hooks.{event} entries must be non-empty command strings"
                )
        hooks[event] = tuple(items)

    return hooks


def _parse_namespaces(value: object, label: str) -> tuple[str, ...]:
    """Parse the owned-key namespace heads: one string or an array of them."""
    if value is None:
        return ()
    items = (value,) if isinstance(value, str) else value
    if not isinstance(items, Sequence) or isinstance(items, str):
        raise ManifestError(f"{label}: 'namespaces' must be a string or array of strings")

    for head in items:
        if not isinstance(head, str) or not head or "." in head:
            raise ManifestError(
                f"{label}: 'namespaces' entries must be non-empty bare key heads "
                f"(no dots): {head!r}"
            )
        try:
            validate_namespace_head(head)
        except WtmError as exc:
            raise ManifestError(f"{label}: {exc}") from exc

    return tuple(items)


def _parse_inheritable(value: object, label: str) -> tuple[InheritableDecl, ...]:
    """Parse declared inheritable elements through the shared element parser."""
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ManifestError(f"{label}: 'inheritable' must be an array of elements")

    decls: list[InheritableDecl] = []
    for element in value:
        try:
            decls.append(parse_inheritable_element(element))
        except WtmError as exc:
            raise ManifestError(f"{label}: {exc}") from exc

    return tuple(decls)


def _parse_defaults(value: object, label: str) -> tuple[tuple[tuple[str, ...], TomlValue], ...]:
    """Parse seed defaults: ``{ key = "dotted.path", value = <scalar> }`` rows."""
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ManifestError(f"{label}: 'defaults' must be an array of tables")

    defaults: list[tuple[tuple[str, ...], TomlValue]] = []
    for entry in value:
        if not isinstance(entry, Mapping) or set(entry) != {"key", "value"}:
            raise ManifestError(
                f"{label}: each default must be exactly {{ key = ..., value = ... }}"
            )
        key = entry["key"]
        if not isinstance(key, str) or not key:
            raise ManifestError(f"{label}: default 'key' must be a non-empty string")
        val = entry["value"]
        if isinstance(val, (dict, list)) or val is None:
            raise ManifestError(f"{label}: default '{key}' value must be a scalar")

        defaults.append((tuple(key.split(".")), val))

    return tuple(defaults)


def validate_namespace_head(head: str) -> None:
    """Reject a namespace head that could never own arbitrary keys."""
    if head in RESERVED_KEYS:
        raise WtmError(f"'{head}' is a reserved key; it cannot be a plugin namespace")
