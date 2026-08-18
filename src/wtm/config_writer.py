"""Entity-local config writes for wtm: the ``.wtm/config.toml`` write seam.

Alongside :mod:`wtm.registry` (which owns *all* user-file writes -- the
``[workspaces.*]`` registry entries, top-level ``default_workspace`` and
``base_branch``), this module owns the *entity-local* files:

    workspace: <workspace>/.wtm/config.toml   (top-level scalars)
    project:   <project>/.wtm/config.toml     ([project] table scalars)

Together the two modules are the only places that write config; both share the
same comment- and format-preserving discipline (re-read fresh with ``tomlkit``,
mutate, then write back atomically via a temp file in the same directory + an
``os.replace``). Untouched sections survive byte-for-byte; a missing file (and
its parent dirs) is created on first write. ``WTM_NO_CONFIG`` (ignore all
config) makes a write a hard error: mutating a file we are told to ignore is
contradictory.
"""

from __future__ import annotations

__all__ = [
    "load_doc",
    "set_scalar",
    "set_table_scalar",
    "set_top_level",
    "unset_scalar",
    "unset_table_scalar",
    "write_doc",
]

import os
import tempfile
import tomllib
from pathlib import Path

import tomlkit

from wtm.config import _flag_env
from wtm.core import WtmError


def set_top_level(doc: tomlkit.TOMLDocument, key: str, value: str) -> None:
    """Set a top-level *key* on *doc*, keeping it above any tables.

    A plain ``doc[key] = value`` on a document that ends in a table would append
    the bare key *inside* that trailing table (a tomlkit foot-gun); a new key is
    instead inserted at the document head so it stays a genuine top-level scalar.
    An existing key is updated in place.
    """
    if key in doc or not doc.body:
        doc[key] = value
    else:
        doc._insert_at(0, key, value)


def set_scalar(path: Path, key: str, value: str) -> None:
    """Set a top-level *key* in the file at *path* (creating it if absent)."""
    doc = load_doc(path)
    set_top_level(doc, key, value)
    write_doc(path, doc)


def unset_scalar(path: Path, key: str) -> None:
    """Remove a top-level *key* from the file at *path* (a no-op if absent)."""
    doc = load_doc(path)
    doc.pop(key, None)
    write_doc(path, doc)


def set_table_scalar(path: Path, table: str, key: str, value: str) -> None:
    """Set ``[table] key = value`` in the file at *path*, creating the table."""
    doc = load_doc(path)
    section = doc.get(table)
    if section is None:
        section = tomlkit.table()
        doc[table] = section
    section[key] = value
    write_doc(path, doc)


def unset_table_scalar(path: Path, table: str, key: str) -> None:
    """Remove ``[table] key`` from the file at *path* (a no-op if absent)."""
    doc = load_doc(path)
    section = doc.get(table)
    if section is not None:
        section.pop(key, None)
    write_doc(path, doc)


# --- document helpers --------------------------------------------------------


def load_doc(path: Path) -> tomlkit.TOMLDocument:
    """Read *path* fresh with tomlkit (empty document when absent).

    Refuses under ``WTM_NO_CONFIG`` (writing a file we are told to ignore is a
    contradiction). A parse error is fatal, mirroring :mod:`wtm.registry`.
    """
    if _flag_env("WTM_NO_CONFIG"):
        raise WtmError("cannot write config while WTM_NO_CONFIG is set")
    try:
        with open(path, encoding="utf-8") as handle:
            return tomlkit.parse(handle.read())
    except FileNotFoundError:
        return tomlkit.document()
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise WtmError(f"config error in {path}: {exc}") from exc


def write_doc(path: Path, doc: tomlkit.TOMLDocument) -> None:
    """Atomically write *doc* to *path* (creating parent dirs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = tomlkit.dumps(doc)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".wtm-config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
