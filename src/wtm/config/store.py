"""The TOML document cache backing every config read/write.

One comment-preserving :class:`Store` per invocation: documents are loaded
lazily per file path and cached; writes re-read fresh under a per-directory
lock and flush atomically. This module is the only place that touches
``tomlkit``.
"""

from __future__ import annotations

__all__ = [
    "Store",
    "existing_entry",
    "key_parts",
    "set_leaf",
    "workspaces_table",
]

import fcntl
import os
import tempfile
import tomllib
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path

import tomlkit

from wtm.config.addressing import user_config_path
from wtm.config.model import SUPER_TABLE_KEYS, kind
from wtm.exceptions import KindConflictError, WriteProhibitedError, WtmError


def key_parts(key: str | tuple[str, ...]) -> tuple[str, ...]:
    """Normalise a key into a tuple of path parts (a bare string is one part)."""
    return (key,) if isinstance(key, str) else tuple(key)


def _unwrap_scalar(node: object) -> object:
    """Return a tomlkit scalar as a plain Python value."""
    unwrap = getattr(node, "unwrap", None)

    return unwrap() if callable(unwrap) else node


def _descend_for_write(doc: tomlkit.TOMLDocument, parts: Sequence[str]):
    """Walk/create the intermediate tables for a nested write, returning the leaf table.

    A freshly created container level (``workspaces`` / ``projects`` /
    ``worktrees``) is a super-table so it renders as dotted ``[workspaces.NAME]``
    headers with no empty ``[workspaces]`` line -- matching the structural write
    tier; the name/entity segments in between stay normal (they hold scalars).
    """
    table: object = doc
    for part in parts:
        child = table.get(part)
        if child is None:
            child = tomlkit.table(is_super_table=part in SUPER_TABLE_KEYS)
            table[part] = child
        table = child

    return table


def set_leaf(table, key: str, value: str) -> None:
    """Set a scalar leaf, keeping a *new* top-level key above any trailing table.

    A plain ``doc[key] = value`` on a document that ends in a table appends the
    bare key *inside* that table (a tomlkit foot-gun); a brand-new top-level key
    is instead inserted at the document head. Nested tables and existing keys are
    set in place.
    """
    is_doc = isinstance(table, tomlkit.TOMLDocument)
    if is_doc and key not in table and table.body:
        table._insert_at(0, key, value)
    else:
        table[key] = value


def _writable_array(container, key: str, full_key: str | tuple[str, ...]):
    """Return the tomlkit array at *container[key]*, creating an empty one if absent.

    A present non-array value is a kind clash (hard error), aligning array
    mutation with the read-side :func:`wtm.config.model.check_kind` rule.
    *full_key* names the key for the error message.
    """
    existing = container.get(key)
    if existing is None:
        container[key] = tomlkit.array()

        return container[key]
    if not isinstance(existing, (list, tuple)):
        raise KindConflictError(
            f"config key '{_key_label(full_key)}' "
            f"is a {kind(_unwrap_scalar(existing))}, not an array; "
            f"cannot mutate it element-wise"
        )

    return existing


def _writable_table(container, key: str, full_key: str | tuple[str, ...]):
    """Return the tomlkit table at *container[key]*, creating an empty one if absent.

    A present non-table value is a kind clash (hard error), aligning table
    mutation with the read-side :func:`wtm.config.model.check_kind` rule.
    *full_key* names the key for the error message.
    """
    existing = container.get(key)
    if existing is None:
        container[key] = tomlkit.table()

        return container[key]
    if not isinstance(existing, dict):
        raise KindConflictError(
            f"config key '{_key_label(full_key)}' "
            f"is a {kind(_unwrap_scalar(existing))}, not a table; "
            f"cannot set a sub-key"
        )

    return existing


def _key_label(full_key: str | tuple[str, ...]) -> str:
    """Render a full key as dotted text for error messages."""
    return full_key if isinstance(full_key, str) else ".".join(full_key)


def _atomic_write(path: Path, text: str) -> None:
    """Atomically write *text* to *path* (creating parent dirs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
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


# --- structural write helpers ------------------------------------------------


def workspaces_table(doc: tomlkit.TOMLDocument):
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


def existing_entry(doc: tomlkit.TOMLDocument, name: str):
    """Return an existing ``[workspaces.NAME]`` entry, or raise if absent."""
    workspaces = doc.get("workspaces")
    if workspaces is None or name not in workspaces:
        raise WtmError(f"workspace '{name}' is not declared at user scope")

    return workspaces[name]


class Store:
    """The comment-preserving ``tomlkit`` document cache backing every read/write.

    One document per file path, loaded lazily and cached; a write mutates the
    cached document and rewrites the file atomically. ``no_config`` (the
    ``WTM_NO_CONFIG`` knob) makes reads return an empty document and any write a
    hard error.
    """

    def __init__(self, *, no_config: bool) -> None:
        self._no_config = no_config
        self._docs: dict[Path, tomlkit.TOMLDocument] = {}

    def doc(self, path: Path) -> tomlkit.TOMLDocument:
        """Return the cached document for *path* (parsing it on first access)."""
        if self._no_config:
            return tomlkit.document()

        cached = self._docs.get(path)
        if cached is not None:
            return cached

        try:
            with open(path, encoding="utf-8") as handle:
                doc = tomlkit.parse(handle.read())
        except FileNotFoundError:
            doc = tomlkit.document()
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise WtmError(f"config error in {path}: {exc}") from exc

        self._docs[path] = doc

        return doc

    def read(self, path: Path) -> dict:
        """Return *path*'s contents as a plain (unwrapped) dict, ``{}`` when absent."""
        return self.doc(path).unwrap()

    def get(self, path: Path, key: str | tuple[str, ...]) -> object | None:
        """Return the scalar at *key* in *path* (unwrapped), or ``None`` when absent."""
        node: object = self.doc(path)
        for part in key_parts(key):
            if not isinstance(node, dict) or part not in node:
                return None

            node = node[part]
        if isinstance(node, dict):
            return None

        return _unwrap_scalar(node)

    def set(self, path: Path, key: str | tuple[str, ...], value: str) -> None:
        """Set *key* = *value* in *path*, creating tables and the file as needed."""
        with self._transaction(path) as doc:
            parts = key_parts(key)
            table = _descend_for_write(doc, parts[:-1])
            set_leaf(table if parts[:-1] else doc, parts[-1], value)

    def unset(self, path: Path, key: str | tuple[str, ...]) -> None:
        """Remove *key* from *path* (a no-op when it is absent)."""
        with self._transaction(path) as doc:
            parts = key_parts(key)
            node: object = doc
            for part in parts[:-1]:
                if not isinstance(node, dict) or part not in node:
                    return

                node = node[part]
            if isinstance(node, dict):
                node.pop(parts[-1], None)

    def array_add(self, path: Path, key: str | tuple[str, ...], value: str) -> None:
        """Add *value* (a string) to the array at *key* in *path* (set semantics).

        Creates an empty array on first add; the add is idempotent (a value
        already present is a no-op). A non-array value already at *key* is a kind
        clash (hard error), matching :func:`wtm.config.model.check_kind`.
        """
        with self._transaction(path) as doc:
            parts = key_parts(key)
            table = _descend_for_write(doc, parts[:-1])
            container = table if parts[:-1] else doc
            array = _writable_array(container, parts[-1], key)
            if value not in [_unwrap_scalar(item) for item in array]:
                array.append(value)

    def array_remove(self, path: Path, key: str | tuple[str, ...], value: str) -> None:
        """Strip every occurrence of *value* from the array at *key* in *path*.

        The mirror of :meth:`array_add`: a no-op when the key or the value is
        absent. A non-array value at *key* is a kind clash (hard error).
        """
        with self._transaction(path) as doc:
            parts = key_parts(key)
            node: object = doc
            for part in parts[:-1]:
                if not isinstance(node, dict) or part not in node:
                    return

                node = node[part]
            if not isinstance(node, dict) or parts[-1] not in node:
                return

            array = _writable_array(node, parts[-1], key)
            for item in [i for i in array if _unwrap_scalar(i) == value]:
                array.remove(item)

    def table_set(self, path: Path, key: str | tuple[str, ...], subkey: str, value: str) -> None:
        """Set *subkey* = *value* in the table at *key* in *path* (set semantics).

        Creates an empty table on first write; a non-table value already at *key*
        is a kind clash (hard error), matching
        :func:`wtm.config.model.check_kind`.
        """
        with self._transaction(path) as doc:
            parts = key_parts(key)
            container = _descend_for_write(doc, parts[:-1]) if parts[:-1] else doc
            table = _writable_table(container, parts[-1], key)
            table[subkey] = value

    def table_unset(self, path: Path, key: str | tuple[str, ...], subkey: str) -> None:
        """Remove *subkey* from the table at *key* in *path* (a no-op when absent).

        The mirror of :meth:`table_set`: a no-op when the key or the sub-key is
        absent. A non-table value at *key* is a kind clash (hard error).
        """
        with self._transaction(path) as doc:
            parts = key_parts(key)
            node: object = doc
            for part in parts[:-1]:
                if not isinstance(node, dict) or part not in node:
                    return

                node = node[part]
            if not isinstance(node, dict) or parts[-1] not in node:
                return

            table = _writable_table(node, parts[-1], key)
            table.pop(subkey, None)

    @contextmanager
    def user_transaction(self):
        """A write transaction over the user file, for the structural write tier.

        Yields the freshly re-read user-file document; the mutation happens inside
        the ``with`` block and is flushed on clean exit (see :meth:`_transaction`).
        """
        with self._transaction(user_config_path()) as doc:
            yield doc

    @contextmanager
    def _transaction(self, path: Path):
        """Hold the per-file lock, yield *path* re-read fresh, flush on clean exit.

        The single write primitive behind every mutation. Because :meth:`_flush`
        rewrites the *whole* document, a write must base itself on the current
        on-disk state, not the load-once snapshot: the fresh re-read
        (cache-bypassed) under the lock lets a concurrent writer's *other* keys
        survive this read-modify-write. The flush is elided when the mutation left
        the document unchanged, so a no-op (e.g. removing an absent key) neither
        creates an empty file nor churns an existing one. An exception inside the
        block skips the flush (no partial write) and still releases the lock.
        Refused under ``WTM_NO_CONFIG`` (before any lock is acquired).
        """
        if self._no_config:
            raise WriteProhibitedError("cannot write config while WTM_NO_CONFIG is set")

        with self._locked(path):
            self._docs.pop(path, None)
            doc = self.doc(path)
            before = tomlkit.dumps(doc)
            yield doc
            if tomlkit.dumps(doc) != before:
                self._flush(path, doc)

    @contextmanager
    def _locked(self, path: Path):
        """Hold an exclusive lock over *path*'s containing directory for a write.

        ``flock`` the config file's parent dir rather than a sidecar: the dir
        pre-exists (created here on first write) and is *never* replaced, whereas
        the file itself is swapped by ``os.replace`` (a new inode), so a lock on
        it would not span the write. Locking the stable directory leaves no lock
        artifact to clean up, and the kernel drops the lock on process exit (no
        stale lock on crash). Granularity is per-config-dir (~ per-scope), which
        is effectively per-file here.
        """
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _flush(self, path: Path, doc: tomlkit.TOMLDocument) -> None:
        """Cache *doc* and write it to *path* atomically (call inside :meth:`_transaction`)."""
        self._docs[path] = doc
        _atomic_write(path, tomlkit.dumps(doc))
