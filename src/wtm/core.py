"""Pure logic for wtm: validation, path resolution, worktree context.

No presentation here: all printing and the cd channel live in
:mod:`wtm.output`. Configuration (workspaces, scopes, TOML layers) lives in
:mod:`wtm.config`; this module stays free of it so it never imports config and
the two never form a cycle. This module only computes and returns values.
"""

from __future__ import annotations

__all__ = [
    "WtmError",
    "Context",
    "abspath",
    "child_dirs",
    "current_dir",
    "derive_context",
    "expand_path",
    "leave_landing",
    "project_dir_for",
    "reject_traversal",
    "remap_dir",
    "rescue_dir",
    "valid_project_name",
    "within",
    "worktree_name",
    "worktree_names_under",
]

import os
from dataclasses import dataclass
from pathlib import Path

from wtm import git
from wtm.exceptions import WtmError

# --- path helpers ------------------------------------------------------------


def abspath(value: str) -> Path:
    """Absolute path without requiring the leaf to exist (no symlink resolve).

    A relative *value* is resolved against the live cwd; if that cwd has been
    removed :func:`os.getcwd` raises, surfaced here as a clear :class:`WtmError`
    rather than a bare ``FileNotFoundError`` traceback.
    """
    path = Path(value)
    if path.is_absolute():
        return path

    try:
        cwd = os.getcwd()
    except OSError as exc:
        raise WtmError(f"cwd is unavailable; cannot resolve relative path: {value!r}") from exc

    return Path(cwd) / path


def expand_path(value: str) -> Path:
    """Expand ``~`` and ``$VAR``/``${VAR}`` in a config path; require it absolute.

    Used for workspace paths and hook-dir overrides read from config: a value
    like ``~/work`` or ``$HOME/workspace`` is expanded, then rejected if it is
    still relative after expansion (a loud misconfig rather than a silent
    resolution against the cwd).
    """
    expanded = os.path.expanduser(os.path.expandvars(value))
    path = Path(expanded)
    if not path.is_absolute():
        raise WtmError(f"config path must be absolute after expansion: {value!r} -> {expanded!r}")

    return path


# --- name / path validation --------------------------------------------------


def valid_project_name(name: str) -> None:
    """Validate that a project name is a single safe path component."""
    if name in ("", ".", ".."):
        raise WtmError(f"invalid project name: '{name}'")
    if "/" in name:
        raise WtmError(f"project name may not contain '/': {name}")


def reject_traversal(name: str) -> None:
    """Validate that a (possibly nested) NAME is relative and '..'-free."""
    if name == "":
        raise WtmError("empty NAME")
    if name.startswith("/"):
        raise WtmError(f"NAME must be relative: {name}")
    if ".." in f"/{name}/".split("/"):
        raise WtmError(f"NAME may not contain '..': {name}")


# --- project resolution ------------------------------------------------------


def project_dir_for(projects_root: Path, cwd: Path) -> Path | None:
    """Return the project dir if *cwd* is under *projects_root*, else ``None``.

    Pure: takes the projects root and the current directory explicitly so the
    rule is testable without touching the process cwd. :func:`project_dir_for_cwd`
    is the config-bound convenience that reads the live cwd.
    """
    try:
        rest = cwd.relative_to(projects_root)
    except ValueError:
        return None

    parts = rest.parts
    if not parts:
        return None

    return projects_root / parts[0]


def current_dir(*, resolve: bool = True) -> Path | None:
    """Return the cwd, or ``None`` if it is unavailable (e.g. deleted).

    Read it up front, before any destructive operation: once the cwd is removed
    ``os.getcwd()`` raises and the shell's location is lost.

    :param resolve: canonicalise via ``realpath`` (the default, for the
        delete/rename rescue callers that compare against a realpath'd worktree);
        pass ``False`` for the non-symlink-resolved form that matches how
        workspace/config paths are stored.
    """
    try:
        cwd = os.getcwd()
    except OSError:
        return None

    return Path(os.path.realpath(cwd)) if resolve else Path(cwd)


def rescue_dir(cwd: Path | None, worktree_dir: Path, project_dir: Path) -> Path | None:
    """Pick where to send a shell stranded by deleting *worktree_dir*.

    :param cwd: the shell's directory captured before deletion (see
        :func:`current_dir`), or ``None`` if it was already unavailable.
    :param worktree_dir: the worktree that was removed.
    :param project_dir: the fallback landing dir (holds ``.git`` + worktrees).
    :returns: ``None`` unless *cwd* is *worktree_dir* or nested below it;
        otherwise ``$OLDPWD`` when set and still a directory (a now-gone
        ``$OLDPWD`` inside the deleted tree falls through), else *project_dir*.
    """
    if cwd is None or not _is_within(cwd, Path(os.path.realpath(worktree_dir))):
        return None

    oldpwd = os.environ.get("OLDPWD")
    if oldpwd and Path(oldpwd).is_dir():
        return Path(oldpwd)

    return project_dir


def _is_within(child: Path, parent: Path) -> bool:
    """Return whether *child* is *parent* itself or nested below it."""
    return child == parent or parent in child.parents


def within(child: Path, parent: Path) -> bool:
    """Return whether *child* is *parent* itself or nested below it (public alias)."""
    return _is_within(child, parent)


def leave_landing(root: Path, *, inside: bool, explicit: bool, cascaded: bool) -> Path | None:
    """Pick the cd-out target for a tiered ``leave`` (``None`` means stay put).

    Shared by the project and workspace lifecycles, whose leave-landing rule is
    identical one tier apart: *root* is the tier's tree (a project dir or a
    projects root). You cannot cd out of a tree you are not standing in
    (*explicit* target, not *inside*). Otherwise land at ``$OLDPWD`` when it is
    still a directory outside *root*, else -- having *cascaded* out of an inner
    tier -- the tree's parent, else stay put.
    """
    if explicit and not inside:
        return None

    oldpwd = os.environ.get("OLDPWD")
    if oldpwd and Path(oldpwd).is_dir() and not within(Path(os.path.realpath(oldpwd)), root):
        return Path(oldpwd)
    if cascaded:
        return root.parent

    return None


def remap_dir(cwd: Path | None, old_dir: Path, new_dir: Path) -> Path | None:
    """Map *cwd* from under a renamed worktree *old_dir* onto *new_dir*.

    :param cwd: the shell's directory captured before the move (see
        :func:`current_dir`), or ``None`` if it was already unavailable.
    :param old_dir: the worktree's location before the rename.
    :param new_dir: its location after the rename.
    :returns: ``None`` unless *cwd* is *old_dir* or nested below it; otherwise
        the corresponding path under *new_dir* (the same relative subpath), so a
        shell standing in the moved tree follows it to the new location.
    """
    if cwd is None:
        return None

    old_real = Path(os.path.realpath(old_dir))
    if not _is_within(cwd, old_real):
        return None

    return new_dir / cwd.relative_to(old_real)


def worktree_name(path: Path, worktrees_dir: Path) -> str | None:
    """Return *path* as a POSIX name relative to *worktrees_dir*.

    The single source for the ``worktrees/``-relative naming rule. Yields
    ``None`` when *path* lies outside *worktrees_dir* (an out-of-tree ``-d``
    destination), which callers treat as the "no NAME to offer" signal.
    """
    try:
        return path.relative_to(worktrees_dir).as_posix()
    except ValueError:
        return None


# --- worktree discovery ------------------------------------------------------


def child_dirs(directory: Path) -> list[Path]:
    """Return *directory*'s immediate sub-directories, sorted; empty if unreadable."""
    try:
        return sorted(
            (entry for entry in directory.iterdir() if entry.is_dir()), key=lambda p: p.name
        )
    except OSError:
        return []


def worktree_names_under(worktrees_dir: Path) -> list[str]:
    """Return the ``worktrees/``-relative names of every worktree under *worktrees_dir*.

    A worktree is any dir holding a ``.git`` (a linked worktree's ``.git`` is a
    file, a nested-name worktree the same). Recurses so a nested name like
    ``feature/x`` is found and returned whole; the recursion stops at a worktree
    root (a worktree never contains another). Lenient: an unreadable or absent
    *worktrees_dir* yields ``[]``.
    """
    names: list[str] = []
    _collect_worktree_names(worktrees_dir, worktrees_dir, names)

    return sorted(names)


def _collect_worktree_names(directory: Path, worktrees_dir: Path, out: list[str]) -> None:
    """Walk *directory* for worktree roots, appending each's relative name to *out*."""
    for entry in child_dirs(directory):
        if (entry / ".git").exists():
            out.append(entry.relative_to(worktrees_dir).as_posix())
            continue
        _collect_worktree_names(entry, worktrees_dir, out)


def worktree_root_within(path: Path, worktrees_dir: Path) -> Path | None:
    """Return the nearest ancestor of *path* (inclusive) holding a ``.git``.

    The cwd-to-worktree inference rule: walk up from *path* until a dir holds
    a ``.git`` -- that is the worktree root the path sits in, possibly a
    nested-name one. ``None`` when *path* is not strictly under
    *worktrees_dir*, or no ancestor before *worktrees_dir* holds a ``.git``.
    """
    try:
        rel = path.relative_to(worktrees_dir)
    except ValueError:
        return None

    node = path
    for _ in rel.parts:
        if (node / ".git").exists():
            return node

        node = node.parent

    return None


# --- context derivation ------------------------------------------------------


@dataclass
class Context:
    """The ``WTM_*`` contract derived from a worktree directory.

    Derived from a worktree's git common dir so the layout works regardless of
    the active workspace or a ``--dir`` override, and remains valid after the
    worktree directory has been removed (delete event). The bare repo lives at
    ``<project_dir>/.git``, which is the git common dir itself.

    ``workspace`` / ``workspace_dir`` are the active workspace this project
    belongs to. :func:`derive_context` leaves them empty (it is config-free);
    config-aware callers enrich them via :meth:`with_workspace` so the pure
    derivation never imports :mod:`wtm.config`.
    """

    dir: Path
    branch: str
    project: str
    project_dir: Path
    worktrees_dir: Path
    git_common_dir: Path
    workspace: str = ""
    workspace_dir: Path | None = None

    def with_workspace(self, name: str, path: Path | None) -> Context:
        """Return this context with the active workspace name/dir filled in."""
        self.workspace = name
        self.workspace_dir = path

        return self

    def env(self) -> dict[str, str]:
        """Return the exported ``WTM_*`` environment contract."""
        env = {
            "WTM_WORKTREE_DIR": str(self.dir),
            "WTM_BRANCH": self.branch,
            "WTM_PROJECT": self.project,
            "WTM_PROJECT_DIR": str(self.project_dir),
            "WTM_WORKTREES_DIR": str(self.worktrees_dir),
            "WTM_GIT_COMMON_DIR": str(self.git_common_dir),
            "WTM_WORKSPACE": self.workspace,
        }
        if self.workspace_dir is not None:
            env["WTM_WORKSPACE_DIR"] = str(self.workspace_dir)

        return env


def derive_context(directory: str | Path) -> Context:
    """Derive the ``WTM_*`` context from a worktree directory.

    :param directory: a worktree directory (must currently be inside the repo).
    :returns: the derived :class:`Context`.
    """
    target = abspath(str(directory))
    if not git.is_repo(target):
        raise WtmError(f"not inside a git repository: {target}")

    toplevel = git.toplevel(target) or target
    common_dir = git.common_dir(target)
    branch = git.current_branch(target)

    # The bare repo is <project_dir>/.git, so the common dir IS the bare repo
    # and its parent is the project dir.
    project_dir = common_dir.parent
    project = project_dir.name
    worktrees_dir = project_dir / "worktrees"

    return Context(
        dir=toplevel,
        branch=branch,
        project=project,
        project_dir=project_dir,
        worktrees_dir=worktrees_dir,
        git_common_dir=common_dir,
    )
