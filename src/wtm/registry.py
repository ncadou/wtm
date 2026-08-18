"""The user-file write seam for wtm: registry + top-level default mutations.

This module owns *all* writes to the **user-scope** config file -- its
``[workspaces.*]`` registry entries (path/description/base_branch plus per-project
annotations) and the top-level ``default_workspace`` / ``base_branch`` keys. Its
sibling :mod:`wtm.config_writer` owns the *entity-local* ``.wtm/config.toml``
files (workspace and project scope); together they are the only places that write
config. System/worktree config stays read-only and hand-edited.

Writes are comment- and format-preserving: the user file is re-read fresh with
``tomlkit``, the document mutated, and written back atomically (temp file in the
same directory, then ``os.replace`` -- both via :mod:`wtm.config_writer`).
Untouched sections -- comments, key order, other tables -- survive byte-for-byte.
A missing file (and its parent dirs) is created on first write. ``WTM_NO_CONFIG``
(ignore all config) makes a write a hard error: mutating a file we are told to
ignore is contradictory.

Scope detection reads the raw user and system files directly (``tomllib``), so
the add/set/rename/delete verbs can tell a user-scope entry from a system-only
one and refuse to touch ``/etc``.
"""

from __future__ import annotations

__all__ = [
    "add_workspace",
    "base_branch_source",
    "clear_base_branch",
    "clear_default",
    "declaring_scopes",
    "remove_workspace",
    "rename_workspace",
    "set_base_branch",
    "set_default",
    "set_project_annotation",
    "set_workspace",
]

import tomlkit

from wtm.config import Scope, _flag_env, _read_toml, _system_config_path, _user_config_path
from wtm.config_writer import load_doc, set_top_level, write_doc
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


def base_branch_source() -> Scope | None:
    """Return the scope declaring the top-level ``base_branch`` (user wins), or ``None``.

    Reads the raw user and system files so the getter can annotate which scope a
    real command would take the global default from. ``WTM_NO_SYSTEM`` /
    ``WTM_NO_CONFIG`` suppress the system / all files, mirroring
    :meth:`Config.load`.
    """
    no_config = _flag_env("WTM_NO_CONFIG")
    no_system = no_config or _flag_env("WTM_NO_SYSTEM")
    if not no_config and _read_toml(_user_config_path()).get("base_branch"):
        return Scope.USER
    if not no_system and _read_toml(_system_config_path()).get("base_branch"):
        return Scope.SYSTEM

    return None


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
    set_top_level(doc, "default_workspace", name)
    _write_user_doc(doc)


def clear_default() -> None:
    """Remove the top-level ``default_workspace`` key from the user file."""
    doc = _load_user_doc()
    doc.pop("default_workspace", None)
    _write_user_doc(doc)


def set_base_branch(value: str) -> None:
    """Set the top-level ``base_branch`` (global default fork point) in the user file."""
    doc = _load_user_doc()
    set_top_level(doc, "base_branch", value)
    _write_user_doc(doc)


def clear_base_branch() -> None:
    """Remove the top-level ``base_branch`` key from the user file."""
    doc = _load_user_doc()
    doc.pop("base_branch", None)
    _write_user_doc(doc)


def set_project_annotation(workspace: str, project: str, key: str, value: str | None) -> None:
    """Set (or clear) one project-annotation scalar under a workspace's registry entry.

    Writes ``[workspaces.WORKSPACE.projects.PROJECT] key = value`` into the user
    file (the user-scope fallback for a project whose dir does not yet exist);
    a ``None``/empty *value* removes the key. The workspace must already be
    declared at user scope.
    """
    doc = _load_user_doc()
    entry = _existing_entry(doc, workspace)
    projects = entry.get("projects")
    if projects is None:
        projects = tomlkit.table(is_super_table=True)
        entry["projects"] = projects
    annotation = projects.get(project)
    if annotation is None:
        annotation = tomlkit.table()
        projects[project] = annotation
    if value:
        annotation[key] = value
    else:
        annotation.pop(key, None)
    _write_user_doc(doc)


# --- document helpers --------------------------------------------------------


def _load_user_doc() -> tomlkit.TOMLDocument:
    """Read the user file fresh with tomlkit (empty document when absent).

    Delegates to :mod:`wtm.config_writer`, which refuses under ``WTM_NO_CONFIG``
    (writing a file we are told to ignore is a contradiction) and treats a parse
    error as fatal, mirroring the read path.
    """
    return load_doc(_user_config_path())


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
    write_doc(_user_config_path(), doc)
