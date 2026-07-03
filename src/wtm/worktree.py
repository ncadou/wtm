"""Worktree-level operations for wtm: create, install, enter, leave, delete, list, resolve.

CLI-agnostic orchestration over :mod:`wtm.git`, :mod:`wtm.core`, and
:mod:`wtm.hooks`. Pure of presentation: methods return data (paths, env dicts,
result objects describing what happened) and never print, log, or talk to the
shell; :mod:`wtm.cli` renders all output and drives the cd side-channel.
"""

from __future__ import annotations

__all__ = [
    "BranchStrategy",
    "Worktree",
    "WorktreeCreated",
    "WorktreeDeleted",
    "WorktreeLeft",
    "WorktreeRenamed",
]

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from wtm import git
from wtm.core import (
    Config,
    Context,
    WtmError,
    abspath,
    derive_context,
    reject_traversal,
    resolve_project_dir,
)
from wtm.hooks import run_hooks
from wtm.target import TargetMode, looks_like_path, resolve_target, split_project_name


class BranchStrategy(Enum):
    """How :meth:`Worktree.create` resolved the new worktree's branch."""

    EXISTING_LOCAL = "existing_local"
    TRACKING = "tracking"
    NEW_BRANCH = "new_branch"
    ORPHAN = "orphan"


@dataclass(frozen=True)
class WorktreeCreated:
    """Result of :meth:`Worktree.create`: the worktree and how its branch was resolved."""

    context: Context
    strategy: BranchStrategy
    branch: str
    base: str | None  # base ref for NEW_BRANCH, else None


@dataclass(frozen=True)
class WorktreeDeleted:
    """Result of :meth:`Worktree.delete`: the deleted paths and branch outcome."""

    worktree_dir: Path  # the worktree that was removed
    project_dir: Path  # the project holding it (.git + worktrees)
    branch: str | None  # branch we attempted to delete, or None if not requested
    branch_delete_failed: bool


@dataclass(frozen=True)
class WorktreeLeft:
    """Result of :meth:`Worktree.leave`: the worktree and where to land on cd-out."""

    worktree_dir: Path  # the worktree that was left (still exists)
    cd_dir: Path  # where a cd-out should land ($OLDPWD or the project dir)


@dataclass(frozen=True)
class WorktreeRenamed:
    """Result of :meth:`Worktree.rename`: the moved paths and branch outcome."""

    old_dir: Path  # the worktree's location before the move
    new_dir: Path  # its location after the move
    worktrees_dir: Path  # the project's worktrees/ (for relative naming)
    branch_renamed: bool  # whether the branch was renamed (-b)
    old_branch: str | None  # branch before the rename, when renamed
    new_branch: str | None  # branch after the rename, when renamed


class Worktree:
    """Worktree-level operations bound to a resolved :class:`Config`."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def create(
        self,
        args: list[str],
        *,
        branch: str | None,
        destdir: str | None,
        do_install: bool,
        do_enter: bool,
        strict: bool,
    ) -> WorktreeCreated:
        """Add a worktree (and branch); return how the branch was resolved.

        Fires the ``create`` event, then ``install`` (when *do_install*), then
        ``enter`` (when *do_enter*, i.e. the caller cds into the new worktree).
        """
        if not args:
            raise WtmError("usage: wtm create [PROJECT] NAME [-b BRANCH] [-d DIR] [-c] [-i]")

        project, name = split_project_name(args)
        reject_traversal(name)

        project_dir = resolve_project_dir(self.config, project)
        git_dir = project_dir / ".git"
        if not git_dir.is_dir():
            raise WtmError(f"project git dir not found: {git_dir}")

        branch = branch or name
        dest = abspath(destdir) if destdir else project_dir / "worktrees" / name
        if dest.exists():
            raise WtmError(f"destination already exists: {dest}")

        dest.parent.mkdir(parents=True, exist_ok=True)

        # Branch resolution: local exists -> use; origin has it -> track; else create.
        base: str | None = None
        if git.ref_exists(git_dir, f"refs/heads/{branch}"):
            git.add_worktree(git_dir, dest, commitish=branch)
            strategy = BranchStrategy.EXISTING_LOCAL
        elif git.ref_exists(git_dir, f"refs/remotes/origin/{branch}"):
            git.add_worktree(git_dir, dest, new_branch=branch, track=f"origin/{branch}")
            strategy = BranchStrategy.TRACKING
        else:
            if git.ref_exists(git_dir, "refs/remotes/origin/HEAD"):
                base = "origin/HEAD"
            else:
                base = "HEAD"
            if git.commit_exists(git_dir, base):
                git.add_worktree(git_dir, dest, new_branch=branch, commitish=base)
                strategy = BranchStrategy.NEW_BRANCH
            else:
                git.add_worktree(git_dir, dest, new_branch=branch, orphan=True)
                strategy = BranchStrategy.ORPHAN
                base = None

        context = derive_context(dest)
        run_hooks("create", context, self.config, strict=strict)
        if do_install:
            run_hooks("install", context, self.config, strict=strict)
        if do_enter:
            run_hooks("enter", context, self.config, strict=strict)

        return WorktreeCreated(context=context, strategy=strategy, branch=branch, base=base)

    def install(self, args: list[str], *, strict: bool) -> None:
        """Run install hooks for a worktree."""
        target = resolve_target(self.config, TargetMode.WORKTREE, args)
        context = derive_context(target)
        run_hooks("install", context, self.config, strict=strict)

    def enter(self, args: list[str], *, do_install: bool, strict: bool) -> Path:
        """Resolve a worktree (running install then enter hooks); return its dir.

        Fires ``install`` first (when *do_install*), then the ``enter`` event.
        The caller (shell wrapper via cli) is responsible for the actual cd.
        """
        target = resolve_target(self.config, TargetMode.WORKTREE, args)
        context = derive_context(target)
        if do_install:
            run_hooks("install", context, self.config, strict=strict)
        run_hooks("enter", context, self.config, strict=strict)

        return context.dir

    def leave(self, args: list[str], *, strict: bool) -> WorktreeLeft:
        """Fire ``leave`` hooks for a worktree; return where a cd-out should land.

        Non-destructive: the worktree still exists (leave hooks run with cwd set
        to ``WTM_WORKTREE_DIR``). The cd-out target is ``$OLDPWD`` when it is still a
        directory, else the project dir.
        """
        target = resolve_target(self.config, TargetMode.WORKTREE, args)
        context = derive_context(target)
        run_hooks("leave", context, self.config, strict=strict)

        oldpwd = os.environ.get("OLDPWD")
        cd_dir = Path(oldpwd) if oldpwd and Path(oldpwd).is_dir() else context.project_dir

        return WorktreeLeft(worktree_dir=context.dir, cd_dir=cd_dir)

    def list(self, project: str | None, *, strict: bool = False) -> list[git.WorktreeEntry]:
        """Return a project's non-bare worktrees as structured entries.

        The single source of truth for worktree enumeration: presentation
        (``wtm list``), shell completion, and the project tree view all derive
        from these entries. Only the bare repository is filtered here; an
        out-of-tree ``-d`` destination is a real worktree and stays in the
        list, later recognised by :func:`wtm.core.worktree_name` returning
        ``None``. Project is explicit or inferred from cwd.

        With *strict*, failures re-raise (``WtmError`` for a missing project git
        dir, :class:`git.GitError` for a failed query); otherwise they collapse
        to an empty list so shell completion never errors.
        """
        try:
            project_dir = resolve_project_dir(self.config, project)
            git_dir = project_dir / ".git"
            if not git_dir.is_dir():
                raise WtmError(f"project git dir not found: {git_dir}")

            return [entry for entry in git.worktree_entries(git_dir) if not entry.bare]
        except Exception:
            if strict:
                raise

            return []

    def delete(
        self, args: list[str], *, force: bool, del_branch: bool, strict: bool
    ) -> WorktreeDeleted:
        """Remove a worktree (requires an explicit target).

        Return the deleted paths and the branch-deletion outcome.
        """
        # Derive context before removal so hooks (e.g. cache purge) still get
        # paths, and fire delete hooks while the worktree still exists (so
        # per-worktree teardown hooks under $WTM_WORKTREE_DIR/.wtm/hooks can run).
        target = resolve_target(self.config, TargetMode.DELETE, args)
        context = derive_context(target)
        branch = context.branch

        run_hooks("delete", context, self.config, strict=strict)

        try:
            git.remove_worktree(context.git_common_dir, context.dir, force=force)
        except git.GitError:
            if not force:
                raise WtmError("worktree has changes; re-run with -f to force") from None
            raise

        _prune_empty_parents(context.dir, context.worktrees_dir)

        attempted_branch: str | None = None
        branch_delete_failed = False
        if del_branch and branch:
            attempted_branch = branch
            try:
                git.delete_branch(context.git_common_dir, branch)
            except git.GitError:
                branch_delete_failed = True

        return WorktreeDeleted(
            worktree_dir=context.dir,
            project_dir=context.project_dir,
            branch=attempted_branch,
            branch_delete_failed=branch_delete_failed,
        )

    def rename(self, args: list[str], *, rename_branch: bool, strict: bool) -> WorktreeRenamed:
        """Rename (move) a worktree; optionally rename its branch to the new name.

        The new name is the trailing positional; anything before it is a
        ``WORKTREE``-mode source target (``PROJECT NAME | NAME | DIR | cwd``).
        The directory is always moved to ``<project>/worktrees/NEWNAME``; with
        *rename_branch* the branch is renamed to NEWNAME too (rejected on a
        detached HEAD, skipped as a no-op when it already matches). Fires the
        ``rename`` event after the move, from the new worktree, exposing the
        prior state as ``WTM_OLD_DIR``/``WTM_OLD_BRANCH``.
        """
        if not args:
            raise WtmError("usage: wtm rename [PROJECT] [OLDNAME] NEWNAME [-b]")

        *source_args, newname = args
        if looks_like_path(newname):
            raise WtmError(f"NEWNAME takes a name, not a path: {newname}")

        reject_traversal(newname)

        source = resolve_target(self.config, TargetMode.WORKTREE, source_args)
        context = derive_context(source)
        dest = context.worktrees_dir / newname
        if dest.exists():
            raise WtmError(f"destination already exists: {dest}")

        if rename_branch and not context.branch:
            raise WtmError("cannot rename branch: worktree is detached")

        # Move the directory, then (optionally) the branch: git branch -m fails
        # if NEWNAME is already taken, which we surface as a hard error.
        dest.parent.mkdir(parents=True, exist_ok=True)
        git.move_worktree(context.git_common_dir, context.dir, dest)

        did_rename_branch = False
        old_branch = context.branch
        if rename_branch and old_branch != newname:
            if git.branch_exists(context.git_common_dir, newname):
                raise WtmError(f"branch '{newname}' already exists")

            git.rename_branch(context.git_common_dir, old_branch, newname)
            did_rename_branch = True

        new_context = derive_context(dest)
        run_hooks(
            "rename",
            new_context,
            self.config,
            strict=strict,
            extra_env={"WTM_OLD_DIR": str(context.dir), "WTM_OLD_BRANCH": old_branch},
        )
        _prune_empty_parents(context.dir, context.worktrees_dir)

        return WorktreeRenamed(
            old_dir=context.dir,
            new_dir=dest,
            worktrees_dir=context.worktrees_dir,
            branch_renamed=did_rename_branch,
            old_branch=old_branch if did_rename_branch else None,
            new_branch=newname if did_rename_branch else None,
        )

    def target_path(self, args: list[str]) -> Path:
        """Compute a worktree path for ``PROJECT NAME`` or ``NAME`` (no git).

        The ``NAME_ONLY`` target mode: rejects ``DIR`` targets and the cwd
        default so only the create-style grammar is accepted. Shared by
        :meth:`resolve_path` and the ``enter --create`` orchestration.
        """
        return resolve_target(self.config, TargetMode.NAME_ONLY, args)

    def resolve_path(self, args: list[str]) -> Path:
        """Return a worktree's computed path without touching git.

        Works for not-yet-created targets. Accepts only ``PROJECT NAME`` or
        ``NAME`` (no ``DIR``, no cwd default).
        """
        if not args:
            raise WtmError("usage: wtm resolve [PROJECT] NAME [-e | --env]")

        return self.target_path(args)

    def resolve_env(self, args: list[str]) -> dict[str, str]:
        """Return the ``WTM_*`` contract for a worktree.

        Inspects git, so the worktree must already exist (unlike
        :meth:`resolve_path`). ``WTM_PROJECTS_ROOT`` is added from config so the
        emitted contract matches what hooks receive.
        """
        env = derive_context(self.resolve_path(args)).env()
        env["WTM_PROJECTS_ROOT"] = str(self.config.projects_root)

        return env


# --- helpers -----------------------------------------------------------------


def _prune_empty_parents(worktree: Path, worktrees_dir: Path) -> None:
    """Remove now-empty intermediate dirs left by a nested NAME.

    Bounded strictly inside *worktrees_dir*, so non-nested worktrees and custom
    ``-d`` destinations outside it are never touched.
    """
    parent = worktree.parent
    while parent != worktrees_dir:
        try:
            parent.relative_to(worktrees_dir)
        except ValueError:
            break
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
