"""Typed-value coercion for the arbitrary-key facility.

Bare writes stay strings; these helpers are the one opt-in gate turning CLI
text into native TOML scalars -- either by an explicit type name
(:func:`parse_typed_value`) or by parsing the value as a TOML literal
(:func:`parse_toml_value`, scalars only until whole-table/array writes exist).
They also own the read side of the same coin: TOML-fidelity text rendering
(:func:`render_scalar`) and the JSON leaf encoder (:func:`json_leaf_default`).
"""

from __future__ import annotations

__all__ = [
    "TYPED_VALUE_FORMATS",
    "TomlValue",
    "json_leaf_default",
    "parse_toml_value",
    "parse_typed_value",
    "render_scalar",
]

import datetime

import tomlkit

from wtm.exceptions import WtmError

#: The native Python shapes a config scalar may take.
TomlValue = str | int | float | bool | datetime.datetime | datetime.date | datetime.time

#: The scalar shapes a removable array element may take (tables aside).
RemovableValue = str | int | float | bool

#: The type names ``--type`` accepts (date/time spellings route through ``--toml``).
TYPED_VALUE_FORMATS = ("int", "float", "bool", "str")

# The truthiness tokens ``--type bool`` accepts, mirroring click's BOOL param
# type: stripped, lower-cased, looked up verbatim ("" counts as false).
_BOOL_STATES: dict[str, bool] = {
    "1": True,
    "0": False,
    "yes": True,
    "no": False,
    "true": True,
    "false": False,
    "on": True,
    "off": False,
    "t": True,
    "f": False,
    "y": True,
    "n": False,
    # Absence of value is considered False.
    "": False,
}


def parse_typed_value(raw: str, fmt: str) -> TomlValue:
    """Coerce CLI *raw* text to the TOML scalar type *fmt* names.

    *fmt* is one of :data:`TYPED_VALUE_FORMATS`: ``int``/``float`` use plain
    numeric parsing, ``str`` passes through, and ``bool`` matches click's
    truthiness tokens (``1/true/t/yes/y/on`` vs ``0/false/f/no/n/off``, plus
    the empty string as false), case-insensitive. An unknown *fmt*, a bad
    number, or an unknown bool word raises ``WtmError`` pointing at
    ``--toml`` for anything its syntax cannot spell.
    """
    if fmt not in TYPED_VALUE_FORMATS:
        raise WtmError(
            f"unknown value type '{fmt}' (choose one of {', '.join(TYPED_VALUE_FORMATS)})"
        )
    if fmt == "str":
        return raw
    if fmt == "bool":
        state = _BOOL_STATES.get(raw.strip().lower())
        if state is None:
            known = ", ".join(sorted(token for token in _BOOL_STATES if token))

            raise WtmError(f"invalid --type bool value {raw!r} (recognized: {known})")

        return state
    try:
        return int(raw) if fmt == "int" else float(raw)
    except ValueError:
        raise WtmError(f"invalid --type {fmt} value {raw!r}") from None


def parse_toml_value(raw: str) -> TomlValue:
    """Parse *raw* as a TOML value literal, refusing non-scalars (for now).

    The value is parsed exactly as TOML would read it inside a document
    (``value = <raw>``), so every TOML scalar spelling works -- quoted
    strings, integers, floats, booleans, and the three date/time forms.
    Arrays and tables parse fine too but are refused until whole-value
    replacement exists (element-wise ``-a``/``-t`` remain the way to grow
    them). A malformed literal raises ``WtmError``.
    """
    try:
        doc = tomlkit.parse(f"value = {raw}")
    except ValueError as exc:
        raise WtmError(f"--toml value {raw!r} is not valid TOML: {exc}") from exc
    if "value" not in doc:
        raise WtmError(f"--toml value {raw!r} is not a single TOML value")

    node = doc["value"]
    unwrap = getattr(node, "unwrap", None)
    value = unwrap() if callable(unwrap) else node
    if isinstance(value, (dict, list)):
        raise WtmError(f"--toml value {raw!r}: tables/arrays are not writable yet (scalars only)")

    return value


def render_scalar(value: object) -> str:
    """Render a resolved scalar TOML-faithfully for text output.

    Booleans print lower-case (``true``/``false``) and date/time values their
    ISO-8601 spelling, so displayed values round-trip back through
    ``--toml``; everything else renders via ``str()`` (strings unquoted, as
    they always printed).
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()

    return str(value)


def json_leaf_default(value: object) -> object:
    """A ``json.dumps(default=...)`` encoding TOML leaves JSON lacks natively.

    Date/time scalars become their ISO-8601 string; anything else exotic
    falls back to ``str()``. Integers, floats, and booleans serialize
    natively and never reach here.
    """
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()

    return str(value)
