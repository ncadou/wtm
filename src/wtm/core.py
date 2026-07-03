"""Pure logic for wtm: configuration, validation, path resolution, context.

No presentation here: all printing and the cd channel live in
:mod:`wtm.output`. This module only computes and returns values.
"""

from __future__ import annotations

__all__ = [
    "WtmError",
    "Config",
    "Context",
    "abspath",
    "current_dir",
    "derive_context",
    "project_dir_for",
    "project_dir_for_cwd",
    "reject_traversal",
    "remap_dir",
    "rescue_dir",
    "resolve_project_dir",
    "valid_project_name",
    "worktree_name",
]

import os
from dataclasses import dataclass
from pathlib import Path

from wtm import git


class WtmError(Exception):
    """A fatal, user-facing error. Carries the message printed as ``wtm: ...``."""


# --- configuration -----------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """Resolved configuration: the projects root and the hook scope directories."""

    projects_root: Path
    hooks_dir: Path
    workspace_hooks_dir: Path

    @classmethod
    def load(cls) -> Config:
        """Load configuration from the environment, applying defaults."""
        xdg_config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        projects_root = Path(
            os.environ.get("WTM_PROJECTS_ROOT") or Path.home() / "workspace" / "projects"
        )
        hooks_dir = Path(os.environ.get("WTM_HOOKS_DIR") or xdg_config / "wtm" / "hooks")
        workspace_hooks_dir = Path(
            os.environ.get("WTM_WORKSPACE_HOOKS_DIR") or projects_root / ".wtm" / "hooks"
        )

        return cls(
            projects_root=projects_root,
            hooks_dir=hooks_dir,
            workspace_hooks_dir=workspace_hooks_dir,
        )


# --- path helpers ------------------------------------------------------------


def abspath(value: str) -> Path:
    """Absolute path without requiring the leaf to exist (no symlink resolve)."""
    path = Path(value)
    if path.is_absolute():
        return path

    return Path(os.getcwd()) / path


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


def project_dir_for_cwd(config: Config) -> Path | None:
    """Return the project dir if the live cwd is under the projects root, else ``None``."""
    return project_dir_for(config.projects_root, abspath(os.getcwd()))


def current_dir() -> Path | None:
    """Return the canonical cwd, or ``None`` if it is unavailable (e.g. deleted).

    Read it up front, before any destructive operation: once the cwd is removed
    ``os.getcwd()`` raises and the shell's location is lost.
    """
    try:
        return Path(os.path.realpath(os.getcwd()))
    except OSError:
        return None


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


def resolve_project_dir(config: Config, project: str | None) -> Path:
    """Resolve a project dir from an explicit name or the current directory."""
    if project:
        valid_project_name(project)

        return config.projects_root / project

    project_dir = project_dir_for_cwd(config)
    if project_dir is None:
        raise WtmError(f"no PROJECT given and cwd is not under {config.projects_root}")

    return project_dir


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


# --- context derivation ------------------------------------------------------


@dataclass
class Context:
    """The ``WTM_*`` contract derived from a worktree directory.

    Derived from a worktree's git common dir so the layout works regardless of
    the projects root or a ``--dir`` override, and remains valid after the
    worktree directory has been removed (delete event). The bare repo lives at
    ``<project_dir>/.git``, which is the git common dir itself.
    """

    dir: Path
    branch: str
    project: str
    project_dir: Path
    worktrees_dir: Path
    git_common_dir: Path

    def env(self) -> dict[str, str]:
        """Return the exported ``WTM_*`` environment contract."""
        return {
            "WTM_WORKTREE_DIR": str(self.dir),
            "WTM_BRANCH": self.branch,
            "WTM_PROJECT": self.project,
            "WTM_PROJECT_DIR": str(self.project_dir),
            "WTM_WORKTREES_DIR": str(self.worktrees_dir),
            "WTM_GIT_COMMON_DIR": str(self.git_common_dir),
        }


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
