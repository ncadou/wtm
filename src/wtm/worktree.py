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
    "WorktreeSet",
]

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from wtm import git
from wtm.config import (
    WORKTREE_WRITE_SCOPES,
    Config,
    ConfigAddress,
    Scope,
    check_write_scope,
    resolve_project_dir,
    worktree_default_scope,
)
from wtm.core import (
    Context,
    WtmError,
    abspath,
    current_dir,
    derive_context,
    reject_traversal,
    worktree_name,
)
from wtm.hooks import run_hooks
from wtm.target import TargetMode, looks_like_path, resolve_target, split_target


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
    fetched: bool  # whether -f/--fetch actually fetched origin (False if skipped)


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


@dataclass(frozen=True)
class WorktreeSet:
    """Result of :meth:`Worktree.set`: what was annotated and where."""

    name: str
    dir: Path
    scope: Scope  # the scope the annotation was written to
    address: ConfigAddress  # the file it landed in


class Worktree:
    """Worktree-level operations bound to a resolved :class:`Config`."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def create(
        self,
        args: list[str],
        *,
        branch: str | None,
        base: str | None,
        destdir: str | None,
        do_fetch: bool,
        do_install: bool,
        do_enter: bool,
        strict: bool,
    ) -> WorktreeCreated:
        """Add a worktree (and branch); return how the branch was resolved.

        Fires ``pre-create`` before any filesystem mutation, then (post-move)
        the ``post-create`` event, then ``install`` (when *do_install*), then
        ``enter`` (when *do_enter*, i.e. the caller cds into the new worktree).

        When *do_fetch*, ``origin`` is fetched (and its HEAD refreshed) after the
        ``pre-create`` veto but before any filesystem mutation, so branch
        resolution forks from the freshest origin state. A missing ``origin``
        remote is skipped (reported via :attr:`WorktreeCreated.fetched`); a
        failing fetch aborts before any residue is left.

        A brand-new branch forks from *base* if given (``--base/--off``), else
        the project's configured base branch, else ``origin/HEAD``/``HEAD``.
        """
        if not args:
            raise WtmError(
                "usage: wtm create [[WORKSPACE] PROJECT] NAME [-b BRANCH] [-d DIR] [-c] [-i]"
            )

        workspace, project, name = split_target(args)
        reject_traversal(name)

        project_dir = resolve_project_dir(self.config, workspace, project)
        git_dir = project_dir / ".git"
        if not git_dir.is_dir():
            raise WtmError(f"project git dir not found: {git_dir}")

        branch = branch or name
        configured_base = self._configured_base(project_dir)
        dest = abspath(destdir) if destdir else project_dir / "worktrees" / name
        if dest.exists():
            raise WtmError(f"destination already exists: {dest}")

        # pre-create fires before any filesystem mutation: the worktree does not
        # exist yet, so its context is synthesised by path math (no git call) and
        # hooks run from the project dir. A --strict veto aborts here, cleanly.
        pre_context = Context(
            dir=dest,
            branch=branch,
            project=project_dir.name,
            project_dir=project_dir,
            worktrees_dir=project_dir / "worktrees",
            git_common_dir=git_dir,
        )
        run_hooks("pre-create", pre_context, self.config, strict=strict, hcwd=project_dir)

        # Fetch after the pre-create veto but before any filesystem mutation, so
        # branch resolution below sees the freshest origin refs. A missing origin
        # is a no-op (fetched stays False); a failing fetch raises and aborts here,
        # leaving no dir behind.
        fetched = False
        if do_fetch and "origin" in git.list_remotes(git_dir):
            git.fetch(git_dir, "origin")
            git.set_head(git_dir, "origin")
            fetched = True

        dest.parent.mkdir(parents=True, exist_ok=True)

        # Branch resolution: local exists -> use; origin has it -> track; else create.
        resolved_base: str | None = None
        if git.ref_exists(git_dir, f"refs/heads/{branch}"):
            git.add_worktree(git_dir, dest, commitish=branch)
            strategy = BranchStrategy.EXISTING_LOCAL
        elif git.ref_exists(git_dir, f"refs/remotes/origin/{branch}"):
            git.add_worktree(git_dir, dest, new_branch=branch, track=f"origin/{branch}")
            strategy = BranchStrategy.TRACKING
        else:
            base_ref = base or configured_base
            if base_ref is not None:
                resolved_base = _resolve_base_ref(git_dir, base_ref)
            elif git.ref_exists(git_dir, "refs/remotes/origin/HEAD"):
                resolved_base = "origin/HEAD"
            else:
                resolved_base = "HEAD"
            if git.commit_exists(git_dir, resolved_base):
                git.add_worktree(git_dir, dest, new_branch=branch, commitish=resolved_base)
                strategy = BranchStrategy.NEW_BRANCH
            elif (base or configured_base) and git.commit_exists(git_dir, "HEAD"):
                # A populated repo whose explicitly requested base does not
                # resolve is a loud misconfig, not a silent orphan.
                raise WtmError(f"base ref does not exist: {base_ref}")
            else:
                # No commits at all: bootstrap on an orphan branch regardless of
                # any configured base (there is nothing to fork from yet).
                git.add_worktree(git_dir, dest, new_branch=branch, orphan=True)
                strategy = BranchStrategy.ORPHAN
                resolved_base = None

        context = derive_context(dest)
        run_hooks("post-create", context, self.config, strict=strict)
        if do_install:
            run_hooks("install", context, self.config, strict=strict)
        if do_enter:
            run_hooks("enter", context, self.config, strict=strict)

        return WorktreeCreated(
            context=context, strategy=strategy, branch=branch, base=resolved_base, fetched=fetched
        )

    def _configured_base(self, project_dir: Path) -> str | None:
        """The project's configured base branch, if any (via the scope chain)."""
        ws = self.config.workspace_of(project_dir)
        if ws is None:
            return None

        return self.config.base_branch_for(ws, project_dir.name)

    # --- annotation ----------------------------------------------------------

    def set(self, args: list[str], *, description: str | None, scope: Scope | None) -> WorktreeSet:
        """Annotate a worktree's ``description`` (partial update).

        The target is a ``WORKTREE``-mode grammar (``[[WORKSPACE] PROJECT] NAME |
        DIR | cwd``). Writes at *scope* (worktree / project / workspace / user /
        system); when *scope* is ``None`` the auto-default is the deepest scope
        whose directory exists: worktree-local ``<worktree>/.wtm/config.toml``
        when the worktree dir exists, else project-local ``[worktrees.NAME]``
        when the project dir exists, else workspace-local, else the user-scope
        registry. A field passed as ``""`` clears it; at least one field must be
        given.
        """
        if description is None:
            raise WtmError("nothing to set (pass --description)")

        wt_dir, project_dir, name = self._resolve_worktree_target(args)
        ws = self.config.workspace_of(project_dir)

        if scope is None:
            scope = worktree_default_scope(
                wt_dir, project_dir, ws.path if ws is not None else None
            )
        check_write_scope(scope, WORKTREE_WRITE_SCOPES, "worktree")
        if scope in (Scope.WORKSPACE, Scope.USER, Scope.SYSTEM) and ws is None:
            raise WtmError(f"project is not under a declared workspace: {project_dir}")
        if scope in (Scope.USER, Scope.SYSTEM) and scope not in self.config.declaring_scopes(
            ws.name
        ):
            raise WtmError(
                f"workspace '{ws.name}' is not declared at {scope} scope; "
                f"cannot place a worktree annotation there"
            )

        address, prefix = self.config.worktree_write_target(ws, project_dir, name, scope)
        full = (*prefix, "description") if prefix else "description"
        if description == "":
            self.config.unset_value(address, full)
        else:
            self.config.set_value(address, full, description)

        return WorktreeSet(name=name, dir=wt_dir, scope=scope, address=address)

    def _resolve_worktree_target(self, args: list[str]) -> tuple[Path, Path, str]:
        """Resolve a worktree target into (worktree dir, project dir, name).

        Pure path math over the ``WORKTREE`` grammar: the resolved worktree dir
        is ``<project>/worktrees/<name>``, so the project dir and the (possibly
        nested) name fall out of the ``worktrees/`` split. Rejects an out-of-tree
        ``-d`` destination, which has no project/worktrees anchor to annotate.
        """
        wt_dir = resolve_target(self.config, TargetMode.WORKTREE, args)
        for parent in wt_dir.parents:
            if parent.name == "worktrees":
                name = worktree_name(wt_dir, parent)
                if name is not None:
                    return wt_dir, parent.parent, name

        raise WtmError(f"not a worktree under a project's worktrees/: {wt_dir}")

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

    def list(
        self, workspace: str | None, project: str | None, *, strict: bool = False
    ) -> list[git.WorktreeEntry]:
        """Return a project's non-bare worktrees as structured entries.

        The single source of truth for worktree enumeration: presentation
        (``wtm list``), shell completion, and the project tree view all derive
        from these entries. Only the bare repository is filtered here; an
        out-of-tree ``-d`` destination is a real worktree and stays in the
        list, later recognised by :func:`wtm.core.worktree_name` returning
        ``None``. Project is explicit or inferred from cwd; a named *workspace*
        is looked up in the registry (else the active fallback).

        With *strict*, failures re-raise (``WtmError`` for a missing project git
        dir, :class:`git.GitError` for a failed query); otherwise they collapse
        to an empty list so shell completion never errors.
        """
        try:
            project_dir = resolve_project_dir(self.config, workspace, project)
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
        # paths, and fire pre-delete hooks while the worktree still exists (so
        # per-worktree teardown hooks under $WTM_WORKTREE_DIR/.wtm/hooks can run).
        target = resolve_target(self.config, TargetMode.DELETE, args)
        context = derive_context(target)
        branch = context.branch

        run_hooks("pre-delete", context, self.config, strict=strict)

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

        # post-delete fires once the deletion is fully settled (worktree gone,
        # empty parents pruned, branch handled). Its worktree dir no longer
        # exists, so hooks run from the project dir.
        run_hooks("post-delete", context, self.config, strict=strict, hcwd=context.project_dir)

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
        detached HEAD, skipped as a no-op when it already matches). Fires
        ``pre-rename`` before the move (from the old worktree, exposing the
        destination as ``WTM_NEW_DIR``/``WTM_NEW_BRANCH``), then ``post-rename``
        after it (from the new worktree, exposing the prior state as
        ``WTM_OLD_DIR``/``WTM_OLD_BRANCH``).
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

        old_branch = context.branch
        will_rename_branch = rename_branch and old_branch != newname
        if rename_branch and not old_branch:
            raise WtmError("cannot rename branch: worktree is detached")

        # All validation precedes both the pre-rename fire and the move: git
        # branch -m fails if NEWNAME is already taken, so surface it up front.
        if will_rename_branch and git.branch_exists(context.git_common_dir, newname):
            raise WtmError(f"branch '{newname}' already exists")

        # pre-rename fires before the move, from the still-present old worktree;
        # a --strict veto aborts before anything moves.
        run_hooks(
            "pre-rename",
            context,
            self.config,
            strict=strict,
            extra_env={
                "WTM_NEW_DIR": str(dest),
                "WTM_NEW_BRANCH": newname if rename_branch else old_branch,
            },
        )

        dest.parent.mkdir(parents=True, exist_ok=True)
        git.move_worktree(context.git_common_dir, context.dir, dest)

        did_rename_branch = False
        if will_rename_branch:
            git.rename_branch(context.git_common_dir, old_branch, newname)
            did_rename_branch = True

        new_context = derive_context(dest)
        run_hooks(
            "post-rename",
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
        :meth:`resolve_path` and the ``create`` command.
        """
        return resolve_target(self.config, TargetMode.NAME_ONLY, args)

    def resolve_path(self, args: list[str]) -> Path:
        """Return a worktree's computed path.

        With an explicit target (``PROJECT NAME`` or ``NAME``; no ``DIR``) this
        is pure path math, so it works for a not-yet-created worktree. With no
        target it resolves the worktree the cwd is in (inspecting git for the
        toplevel); absent a worktree it asks for a NAME.
        """
        if not args:
            # No target: resolve the worktree we're standing in (touches git,
            # unlike the explicit-NAME forms). Absent a worktree, ask for a NAME.
            cwd = current_dir(resolve=False)
            if cwd is None:
                raise WtmError(
                    "cwd is unavailable (the directory was removed); pass PROJECT NAME or NAME"
                )
            if not git.is_repo(cwd):
                raise WtmError(
                    "no NAME given and cwd is not inside a worktree (pass PROJECT NAME or NAME)"
                )

            return derive_context(cwd).dir

        return self.target_path(args)

    def resolve_env(self, args: list[str]) -> dict[str, str]:
        """Return the ``WTM_*`` contract for a worktree.

        Inspects git, so the worktree must already exist (unlike
        :meth:`resolve_path`). The active workspace (name + dir) is folded in so
        the emitted contract matches what hooks receive.
        """
        return self.env_for_dir(self.resolve_path(args))

    def env_for_dir(self, directory: Path) -> dict[str, str]:
        """Return the ``WTM_*`` contract for an existing worktree *directory*.

        The dir-keyed core of :meth:`resolve_env`: derives the context from
        *directory* (must be inside the worktree) and folds in the active
        workspace, so the shell wrapper's session env matches what hooks and
        ``resolve --env`` emit. Used by the movers (enter/create/rename) that
        already know their destination dir.
        """
        context = derive_context(directory)
        ws = self.config.workspace_of(context.project_dir)
        context.with_workspace(
            ws.name if ws is not None else "",
            ws.path if ws is not None else context.project_dir.parent,
        )

        return context.env()


# --- helpers -----------------------------------------------------------------


def _resolve_base_ref(git_dir: Path, base: str) -> str:
    """Resolve a configured/requested base branch to a concrete ref.

    Prefers ``origin/<base>`` (the tracked upstream), then a local
    ``refs/heads/<base>``; otherwise returns *base* unchanged so a bare commit
    or tag still works and an unresolvable value surfaces as a loud error at the
    ``commit_exists`` gate.
    """
    if git.ref_exists(git_dir, f"refs/remotes/origin/{base}"):
        return f"origin/{base}"
    if git.ref_exists(git_dir, f"refs/heads/{base}"):
        return base

    return base


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
