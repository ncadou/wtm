"""The single write seam for wtm: user-scope registry mutations.

Everything else in :mod:`wtm` reads config (via :mod:`wtm.config`, using stdlib
``tomllib``); this module is the *only* place that writes it, and it writes only
the **user-scope** file -- its ``[workspaces.*]`` registry entries and the
top-level ``default_workspace`` key. System/project/worktree config stays
read-only and hand-edited.

Writes are comment- and format-preserving: the user file is re-read fresh with
``tomlkit``, the document mutated, and written back atomically (temp file in the
same directory, then ``os.replace``). Untouched sections -- comments, key order,
other tables -- survive byte-for-byte. A missing file (and its parent dirs) is
created on first write. ``WTM_NO_CONFIG`` (ignore all config) makes a write a
hard error: mutating a file we are told to ignore is contradictory.

Scope detection reads the raw user and system files directly (``tomllib``), so
the add/set/rename/delete verbs can tell a user-scope entry from a system-only
one and refuse to touch ``/etc``.
"""

from __future__ import annotations

__all__ = [
    "add_workspace",
    "clear_default",
    "declaring_scopes",
    "remove_workspace",
    "rename_workspace",
    "set_default",
    "set_workspace",
]

import os
import tempfile
import tomllib

import tomlkit

from wtm.config import Scope, _flag_env, _read_toml, _system_config_path, _user_config_path
from wtm.core import WtmError

# --- scope detection (reads) -------------------------------------------------


def declaring_scopes(name: str) -> set[Scope]:
    """Return the scopes (user and/or system) whose config declares *name*.

    Reads the raw user and system files, so it reflects the actual declaration
    sites -- unlike :attr:`Workspace.scope`, which records only the winning
    (most-specific) scope after the merge. ``WTM_NO_SYSTEM`` / ``WTM_NO_CONFIG``
    suppress the system / all files, mirroring :meth:`Config.load`.
    """
    scopes: set[Scope] = set()
    no_config = _flag_env("WTM_NO_CONFIG")
    no_system = no_config or _flag_env("WTM_NO_SYSTEM")
    if not no_system and name in (_read_toml(_system_config_path()).get("workspaces") or {}):
        scopes.add(Scope.SYSTEM)
    if not no_config and name in (_read_toml(_user_config_path()).get("workspaces") or {}):
        scopes.add(Scope.USER)

    return scopes


def default_workspace() -> str | None:
    """Return the user file's top-level ``default_workspace``, if any."""
    return _read_toml(_user_config_path()).get("default_workspace")


# --- registry mutations (writes) ---------------------------------------------


def add_workspace(
    name: str,
    path: str,
    *,
    description: str | None = None,
    base_branch: str | None = None,
) -> None:
    """Write a new ``[workspaces.NAME]`` entry into the user file.

    Only the caller-supplied scalars are written (``path`` always; the rest when
    given). The caller (``wtm.workspace``) owns collision/validation policy.
    """
    doc = _load_user_doc()
    workspaces = _workspaces_table(doc)
    entry = tomlkit.table()
    entry["path"] = path
    if description:
        entry["description"] = description
    if base_branch:
        entry["base_branch"] = base_branch
    workspaces[name] = entry
    _write_user_doc(doc)


def set_workspace(
    name: str,
    *,
    path: str | None = None,
    description: str | None = None,
    base_branch: str | None = None,
) -> None:
    """Edit an existing user-scope entry in place (partial update).

    Only the fields passed as non-``None`` are touched. A field passed as the
    empty string is *removed*; ``path`` may not be emptied (validated upstream).
    """
    doc = _load_user_doc()
    entry = _existing_entry(doc, name)
    for key, value in (
        ("path", path),
        ("description", description),
        ("base_branch", base_branch),
    ):
        if value is None:
            continue
        if value == "":
            entry.pop(key, None)
        else:
            entry[key] = value
    _write_user_doc(doc)


def rename_workspace(old: str, new: str) -> bool:
    """Rename the registry key *old* to *new*, preserving the entry's subtables.

    Fixes a top-level ``default_workspace`` that pointed at *old*. Returns
    whether that fixup happened.
    """
    doc = _load_user_doc()
    entry = _existing_entry(doc, old)
    workspaces = doc["workspaces"]
    workspaces[new] = entry
    del workspaces[old]
    fixed = doc.get("default_workspace") == old
    if fixed:
        doc["default_workspace"] = new
    _write_user_doc(doc)

    return fixed


def remove_workspace(name: str) -> bool:
    """Delete the ``[workspaces.NAME]`` entry from the user file.

    Also clears a top-level ``default_workspace`` that pointed at *name*.
    Returns whether that fixup happened.
    """
    doc = _load_user_doc()
    _existing_entry(doc, name)  # existence check
    del doc["workspaces"][name]
    fixed = doc.get("default_workspace") == name
    if fixed:
        del doc["default_workspace"]
    _write_user_doc(doc)

    return fixed


def set_default(name: str) -> None:
    """Set the top-level ``default_workspace`` key in the user file."""
    doc = _load_user_doc()
    doc["default_workspace"] = name
    _write_user_doc(doc)


def clear_default() -> None:
    """Remove the top-level ``default_workspace`` key from the user file."""
    doc = _load_user_doc()
    doc.pop("default_workspace", None)
    _write_user_doc(doc)


# --- document helpers --------------------------------------------------------


def _load_user_doc() -> tomlkit.TOMLDocument:
    """Read the user file fresh with tomlkit (empty document when absent).

    Refuses under ``WTM_NO_CONFIG`` (writing a file we are told to ignore is a
    contradiction). A parse error is fatal, mirroring the read path.
    """
    if _flag_env("WTM_NO_CONFIG"):
        raise WtmError("cannot write config while WTM_NO_CONFIG is set")
    path = _user_config_path()
    try:
        with open(path, encoding="utf-8") as handle:
            return tomlkit.parse(handle.read())
    except FileNotFoundError:
        return tomlkit.document()
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise WtmError(f"config error in {path}: {exc}") from exc


def _workspaces_table(doc: tomlkit.TOMLDocument):
    """Return the document's ``[workspaces]`` super-table, creating it if absent.

    A super-table renders its children as ``[workspaces.NAME]`` headers with no
    bare ``[workspaces]`` line.
    """
    existing = doc.get("workspaces")
    if existing is not None:
        return existing
    table = tomlkit.table(is_super_table=True)
    doc["workspaces"] = table

    return table


def _existing_entry(doc: tomlkit.TOMLDocument, name: str):
    """Return an existing ``[workspaces.NAME]`` entry, or raise if absent."""
    workspaces = doc.get("workspaces")
    if workspaces is None or name not in workspaces:
        raise WtmError(f"workspace '{name}' is not declared at user scope")

    return workspaces[name]


def _write_user_doc(doc: tomlkit.TOMLDocument) -> None:
    """Atomically write *doc* to the user config file (creating parent dirs)."""
    path = _user_config_path()
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
