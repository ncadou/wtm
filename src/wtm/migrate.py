"""Project migration for wtm: fold a plain clone into the bare-repo layout.

``wtm migrate`` takes a legacy working clone (the *source*) and folds it into
the ``<workspace>/<project>`` bare-repo + worktrees layout (the
*target*), carrying over remotes, references, tags, and the live working
checkout (modified + untracked files) recreated as a worktree.

CLI-agnostic orchestration over :mod:`wtm.git`, :mod:`wtm.core`,
:mod:`wtm.project`, and :mod:`wtm.worktree`. Like the rest of wtm, this module
is pure of presentation beyond the plan/step *records* it returns; the rsync
copy is the one place it shells out directly (git lives in :mod:`wtm.git`).
"""

from __future__ import annotations

__all__ = [
    "CreateMethod",
    "Migrate",
    "MigratePlan",
    "MigrateResult",
    "Resolution",
    "WorktreeSpec",
]

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from wtm import git
from wtm.config import Config
from wtm.core import (
    WtmError,
    abspath,
    current_dir,
    project_dir_for,
    reject_traversal,
    valid_project_name,
)
from wtm.worktree import Worktree


class CreateMethod(Enum):
    """How a missing target bare repo is created."""

    FROM_DISK = "from_disk"  # bare-clone the source on disk (fast, offline)
    FROM_ORIGIN = "from_origin"  # bare-clone the source's origin URL (network)
    INIT_BARE = "init_bare"  # init an empty bare repo (refs come from push)


@dataclass
class WorktreeSpec:
    """One worktree to reproduce on the target, derived from a source worktree.

    ``branch`` is the branch the source worktree is on, or ``None`` when it is
    detached; a detached spec is reproduced by pinning a branch named ``name``
    at ``head`` (see :meth:`Migrate._pin_detached`). ``name`` is the target
    worktree's name: under ``--all-worktrees`` it is the source worktree's
    directory basename; for the lone default worktree it is the branch name (or
    an explicit ``--name``).
    """

    name: str  # target worktree name (dir under worktrees/)
    branch: str | None  # source branch, or None when detached
    head: str  # the source worktree's checked-out commit sha
    source_path: Path  # the source worktree's top level (rsync source)
    is_main: bool  # whether this is the source's main worktree


@dataclass
class Resolution:
    """Resolved source/target inputs for a migration."""

    source: Path  # the source working clone's top level (the cwd worktree)
    project: str  # the target project name
    project_dir: Path  # <workspace>/<project>
    git_dir: Path  # <project_dir>/.git (the bare repo)
    target_exists: bool  # whether the target bare repo already exists
    branch: str  # the source's current branch ('' when detached)
    origin_url: str | None  # the source's origin fetch URL, if any
    worktrees: list[WorktreeSpec] = field(default_factory=list)  # --all-worktrees set
    current_name: str | None = None  # target name of the cwd source worktree
    prunable_skipped: list[str] = field(default_factory=list)  # missing source worktrees


@dataclass
class MigratePlan:
    """A description of what a migration will do (for --dry-run and logging)."""

    resolution: Resolution
    create_method: CreateMethod | None  # None when the target already exists
    all_worktrees: bool  # reproduce the source's whole worktree set
    worktrees: list[WorktreeSpec]  # the worktrees to recreate ([] with --no-worktree)
    do_remotes: bool
    do_refs: bool
    do_tags: bool
    do_fetch: bool
    force: bool
    include_ignored: bool


@dataclass
class MigrateResult:
    """What a migration actually did, for the CLI to render."""

    plan: MigratePlan
    created: bool = False
    remotes_added: list[str] = field(default_factory=list)
    remotes_skipped: list[str] = field(default_factory=list)
    refs_rejected: list[str] = field(default_factory=list)
    fetched: bool = False
    worktrees_created: list[tuple[str, Path]] = field(default_factory=list)
    worktrees_skipped: list[tuple[str, str]] = field(default_factory=list)  # name, reason
    worktrees_failed: list[tuple[str, str]] = field(default_factory=list)  # name, reason
    cd_dir: Path | None = None  # where -c/--cd should land


# The temporary remote added to the source to push references into the target.
_PUSH_REMOTE = "wtm-migrate-target"


class Migrate:
    """Project migration bound to a resolved :class:`Config`."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.worktree = Worktree(config)

    # --- resolution ----------------------------------------------------------

    def resolve(
        self,
        other: str | None,
        *,
        source: str | None,
        project: str | None,
        all_worktrees: bool = False,
        update: bool = False,
    ) -> Resolution:
        """Resolve source and target from cwd, a lone positional, and overrides.

        ``other`` is the single positional: the side *not* inferred from cwd
        (the target project name when standing in the source; the source path
        when standing under the projects root). ``--source`` / ``--project``
        override outright.

        Resolution order for the source: ``--source``; else the cwd when it is
        itself a git repo (and not under the projects root); else the positional
        treated as a path. The positional names the *project* only when it was
        not consumed as the source.

        With ``all_worktrees`` the source's whole worktree set is enumerated
        (from any of its worktrees) into :attr:`Resolution.worktrees`, prunable
        (missing-on-disk) entries dropped into :attr:`Resolution.prunable_skipped`.

        A target whose bare repo already exists is refused unless ``update``
        is set, since migrating into it mutates a project that may already be
        in active use.
        """
        cwd_project_dir = project_dir_for(self.config.projects_root, abspath("."))
        cwd_is_repo = cwd_project_dir is None and git.is_repo(abspath("."))

        # --- source path ---
        positional_is_source = False
        if source is not None:
            source_path = self._require_repo(abspath(source))
        elif cwd_is_repo:
            source_path = self._require_repo(abspath("."))
        elif other is not None:
            # Not in a repo (e.g. under the projects root): positional = source.
            source_path = self._require_repo(abspath(other))
            positional_is_source = True
        else:
            raise WtmError(
                "no source: run from inside the source repo, or pass a source "
                "path positionally or with --source"
            )

        # --- target project name ---
        if project is not None:
            name = project
        elif other is not None and not positional_is_source:
            name = other
        elif cwd_project_dir is not None:
            name = cwd_project_dir.name
        else:
            name = source_path.name
        valid_project_name(name)

        project_dir = self.config.projects_root / name
        git_dir = project_dir / ".git"

        resolution = Resolution(
            source=source_path,
            project=name,
            project_dir=project_dir,
            git_dir=git_dir,
            target_exists=git_dir.is_dir(),
            branch=git.current_branch(source_path),
            origin_url=git.remote_url(source_path, "origin"),
        )
        if resolution.target_exists and not update:
            raise WtmError(f"target already exists: {project_dir} (pass --update to modify it)")
        if all_worktrees:
            self._enumerate_worktrees(resolution)

        return resolution

    def _enumerate_worktrees(self, resolution: Resolution) -> None:
        """Populate *resolution* with the source's worktree set (--all-worktrees).

        Reads ``git worktree list`` from the cwd worktree (which lists every
        worktree of the repo), dropping the bare entry and prunable (missing)
        worktrees; locked worktrees are kept (the lock is a source-side concern
        that does not affect copying). Each target worktree is named after the
        source worktree's directory basename; the cwd worktree's name is recorded
        as :attr:`Resolution.current_name` for ``-c/--cd``.
        """
        cwd = current_dir()
        for entry in git.worktree_entries(resolution.source):
            if entry.bare:
                continue
            wt_name = entry.path.name
            if entry.prunable or not entry.path.is_dir():
                resolution.prunable_skipped.append(wt_name)
                continue
            is_main = (entry.path / ".git").is_dir()
            spec = WorktreeSpec(
                name=wt_name,
                branch=entry.branch,
                head=entry.head,
                source_path=entry.path,
                is_main=is_main,
            )
            resolution.worktrees.append(spec)
            if cwd is not None and _is_within(cwd, entry.path):
                resolution.current_name = wt_name

    def _require_repo(self, path: Path) -> Path:
        """Return the git top level for *path*, or raise if it is not a repo."""
        if not git.is_repo(path):
            raise WtmError(f"not a git repository: {path}")
        top = git.toplevel(path)
        if top is None:
            raise WtmError(f"could not resolve git top level: {path}")

        return top

    # --- planning ------------------------------------------------------------

    def plan(
        self,
        resolution: Resolution,
        *,
        create_method: CreateMethod,
        worktree_name: str | None,
        all_worktrees: bool,
        do_remotes: bool,
        do_refs: bool,
        do_tags: bool,
        do_fetch: bool,
        force: bool,
        include_ignored: bool,
        no_worktree: bool,
    ) -> MigratePlan:
        """Build the migration plan, resolving the worktree set and branches.

        Default (single) mode reproduces just the source's current checkout: a
        detached HEAD requires an explicit ``worktree_name`` (there is no branch
        to name it after), and the worktree is recreated on the source's current
        branch. With ``all_worktrees`` the resolved set
        (:attr:`Resolution.worktrees`) is used as-is: each worktree is named
        after its source directory basename, and detached worktrees are pinned
        at their commit (no ``--name`` needed). ``no_worktree`` yields an empty
        set.
        """
        worktrees: list[WorktreeSpec] = []
        if no_worktree:
            pass
        elif all_worktrees:
            worktrees = resolution.worktrees
            for spec in worktrees:
                reject_traversal(spec.name)
        else:
            if worktree_name is not None:
                name = worktree_name
            elif resolution.branch:
                name = resolution.branch
            else:
                raise WtmError("source has a detached HEAD; pass --name to name the worktree")
            reject_traversal(name)
            worktrees = [
                WorktreeSpec(
                    name=name,
                    branch=resolution.branch or None,
                    head="",
                    source_path=resolution.source,
                    is_main=True,
                )
            ]

        return MigratePlan(
            resolution=resolution,
            create_method=None if resolution.target_exists else create_method,
            all_worktrees=all_worktrees,
            worktrees=worktrees,
            do_remotes=do_remotes,
            do_refs=do_refs,
            do_tags=do_tags,
            do_fetch=do_fetch,
            force=force,
            include_ignored=include_ignored,
        )

    # --- execution -----------------------------------------------------------

    def run(
        self,
        plan: MigratePlan,
        *,
        exclude: list[str],
        filter_files: list[str],
        strict: bool,
    ) -> MigrateResult:
        """Execute *plan*, returning a record of what was done."""
        res = plan.resolution
        result = MigrateResult(plan=plan)

        if plan.create_method is not None:
            self._create_target(res, plan.create_method)
            result.created = True

        if plan.do_remotes:
            self._migrate_remotes(res, result)

        # References are imported by pushing from the source unless the target
        # was freshly created by a disk-clone, which already carries the
        # source's local heads + tags. An existing target, or one created via
        # --from-origin / --init-bare, needs the push to receive them.
        if plan.do_refs and plan.create_method is not CreateMethod.FROM_DISK:
            self._push_refs(res, plan, result)

        # Detached worktrees sit on no branch, so neither an --all push nor a
        # disk-clone carries their commit; pin a branch at each before creating
        # the worktrees, transferring the object in the same push.
        detached = [s for s in plan.worktrees if s.branch is None]
        if detached:
            self._pin_detached(res, detached, force=plan.force)

        if plan.do_fetch and res.origin_url is not None:
            git.fetch(res.git_dir, "origin")
            git.set_head(res.git_dir, "origin")
            result.fetched = True

        self._recreate_worktrees(
            res,
            plan,
            result,
            exclude=exclude,
            filter_files=filter_files,
            strict=strict,
        )

        return result

    def _create_target(self, res: Resolution, method: CreateMethod) -> None:
        """Create the target bare repo by the chosen method."""
        res.project_dir.mkdir(parents=True, exist_ok=True)
        (res.project_dir / "worktrees").mkdir(exist_ok=True)

        if method is CreateMethod.FROM_DISK:
            # Hard-linked, offline: carries the source's local heads + tags.
            git.clone(str(res.source), res.git_dir)
            # Repoint origin at the real upstream (clone set it to the source).
            if res.origin_url is not None:
                git.set_remote_url(res.git_dir, "origin", res.origin_url)
            else:
                git.remove_remote(res.git_dir, "origin")
        elif method is CreateMethod.FROM_ORIGIN:
            if res.origin_url is None:
                raise WtmError(
                    "source has no 'origin' remote; omit --from-origin to "
                    "bare-clone the source on disk, or use --init-bare"
                )
            git.clone(res.origin_url, res.git_dir)
        else:  # INIT_BARE
            git.init(res.git_dir)
            if res.origin_url is not None:
                git.add_remote(res.git_dir, "origin", res.origin_url)
                git.set_config_fetch(res.git_dir)

    def _migrate_remotes(self, res: Resolution, result: MigrateResult) -> None:
        """Copy source remotes into the target; add-missing, warn-skip clashes."""
        target_remotes = git.list_remotes(res.git_dir)
        for name, url in git.list_remotes(res.source).items():
            if name == "origin":
                # origin is authoritative from the source already.
                continue
            existing = target_remotes.get(name)
            if existing is None:
                git.add_remote(res.git_dir, name, url)
                result.remotes_added.append(name)
            elif existing != url:
                result.remotes_skipped.append(name)

    def _push_refs(self, res: Resolution, plan: MigratePlan, result: MigrateResult) -> None:
        """Import references by pushing from the source into the target.

        Skips branches checked out in a target worktree (git refuses those).
        Fast-forward-only unless ``force``; rejected branches are recorded.
        """
        checked_out = git.worktree_branches(res.git_dir)
        git.remove_remote(res.source, _PUSH_REMOTE)
        git.add_remote(res.source, _PUSH_REMOTE, str(res.git_dir))
        try:
            proc = git.push_refs(
                res.source,
                _PUSH_REMOTE,
                all_branches=True,
                force=plan.force,
            )
            result.refs_rejected = _parse_rejected(proc.stderr, checked_out)
            if plan.do_tags:
                git.push_refs(res.source, _PUSH_REMOTE, tags=True)
        finally:
            git.remove_remote(res.source, _PUSH_REMOTE)

    def _pin_detached(self, res: Resolution, specs: list[WorktreeSpec], *, force: bool) -> None:
        """Pin a branch at each detached worktree's commit in the target.

        Pushes ``<sha>:refs/heads/<name>`` from the source (where the commit's
        objects live) for every detached spec, so the subsequent worktree create
        finds it as an existing local branch and reproduces the exact commit.
        """
        git.remove_remote(res.source, _PUSH_REMOTE)
        git.add_remote(res.source, _PUSH_REMOTE, str(res.git_dir))
        try:
            for spec in specs:
                git.push_refs(
                    res.source,
                    _PUSH_REMOTE,
                    force=force,
                    refspec=f"{spec.head}:refs/heads/{spec.name}",
                )
                # The worktree is now created on this (named) branch.
                spec.branch = spec.name
        finally:
            git.remove_remote(res.source, _PUSH_REMOTE)

    def dry_run_rsync(
        self,
        plan: MigratePlan,
        *,
        exclude: list[str],
        filter_files: list[str],
    ) -> list[tuple[str, str]]:
        """Return per-worktree rsync ``--dry-run`` previews of the working copy.

        Side-effect free: never touches the target. Returns a ``(name, preview)``
        pair per planned worktree; an empty list when there is no worktree phase
        (``--no-worktree``).
        """
        previews: list[tuple[str, str]] = []
        for spec in plan.worktrees:
            proc = _rsync_worktree(
                spec.source_path,
                plan.resolution.project_dir / "worktrees" / spec.name,
                include_ignored=plan.include_ignored,
                exclude=exclude,
                filter_files=filter_files,
                dry_run=True,
            )
            previews.append((spec.name, proc.stdout))

        return previews

    def _recreate_worktrees(
        self,
        res: Resolution,
        plan: MigratePlan,
        result: MigrateResult,
        *,
        exclude: list[str],
        filter_files: list[str],
        strict: bool,
    ) -> None:
        """Recreate each planned worktree, best-effort, recording the outcomes.

        Per worktree: skip (recording a reason) when its target name already
        exists or its branch is already checked out in the target; otherwise
        create it on its branch and mirror its working tree. A hard failure on
        one worktree is recorded and the rest still attempted. ``-c/--cd`` lands
        in the worktree matching the cwd source worktree, else the main one.
        """
        checked_out = git.worktree_branches(res.git_dir)
        for spec in plan.worktrees:
            dest = res.project_dir / "worktrees" / spec.name
            if dest.exists():
                result.worktrees_skipped.append((spec.name, "target already exists"))
                continue
            if spec.branch is not None and spec.branch in checked_out:
                result.worktrees_skipped.append(
                    (spec.name, f"branch '{spec.branch}' already checked out")
                )
                continue
            try:
                created = self.worktree.create(
                    [res.project, spec.name],
                    branch=spec.branch,
                    base=None,
                    destdir=None,
                    do_install=False,
                    do_enter=False,
                    strict=strict,
                )
                _rsync_worktree(
                    spec.source_path,
                    created.context.dir,
                    include_ignored=plan.include_ignored,
                    exclude=exclude,
                    filter_files=filter_files,
                    dry_run=False,
                )
            except (WtmError, git.GitError, OSError) as exc:
                result.worktrees_failed.append((spec.name, str(exc)))
                continue
            result.worktrees_created.append((spec.name, created.context.dir))
            if spec.branch is not None:
                checked_out.add(spec.branch)

        result.cd_dir = _pick_cd_dir(res, plan, result)


# --- helpers -----------------------------------------------------------------


def _pick_cd_dir(res: Resolution, plan: MigratePlan, result: MigrateResult) -> Path | None:
    """Choose where -c/--cd should land among the created worktrees.

    Prefers the worktree matching the cwd source worktree
    (:attr:`Resolution.current_name`), then the main worktree, then the first
    created; ``None`` when nothing was created.
    """
    created = dict(result.worktrees_created)
    if not created:
        return None
    if res.current_name is not None and res.current_name in created:
        return created[res.current_name]
    for spec in plan.worktrees:
        if spec.is_main and spec.name in created:
            return created[spec.name]

    return next(iter(created.values()))


def _is_within(child: Path, parent: Path) -> bool:
    """Return whether *child* is *parent* or nested below it (realpath-based)."""
    child = Path(os.path.realpath(child))
    parent = Path(os.path.realpath(parent))

    return child == parent or parent in child.parents


def _parse_rejected(stderr: str, checked_out: set[str]) -> list[str]:
    """Extract branch names rejected by a non-force push (best effort).

    git prints '! [rejected] <branch> -> <branch> (...)' lines; we collect the
    local branch names, dropping any that are checked out in a target worktree
    (those are expected and reported separately upstream of this).
    """
    rejected: list[str] = []
    for line in stderr.splitlines():
        stripped = line.strip()
        if stripped.startswith("! [rejected]"):
            rest = stripped[len("! [rejected]") :].strip()
            local = rest.split("->", 1)[0].strip()
            if local and local not in checked_out:
                rejected.append(local)

    return rejected


def _rsync_worktree(
    source: Path,
    dest: Path,
    *,
    include_ignored: bool,
    exclude: list[str],
    filter_files: list[str],
    dry_run: bool,
) -> subprocess.CompletedProcess[str]:
    """Mirror *source*'s working tree onto *dest* with rsync.

    ``--archive --delete`` makes *dest*'s contents an exact copy of *source*
    (uncommitted deletions carry over), except ``.git`` (always excluded) and
    anything matched by the supplied filters. Git-ignored files are copied
    unless ``include_ignored`` is false, in which case git's own ignore list is
    fed to rsync as excludes.
    """
    if shutil.which("rsync") is None:
        raise WtmError("rsync not found on PATH (required to copy the working tree)")

    args = ["rsync", "--archive", "--delete"]
    if dry_run:
        args += ["--verbose", "--dry-run"]
    # .git is a mechanical necessity, never copied (and protected from --delete).
    args += ["--filter=protect /.git", "--exclude=/.git"]
    for path in filter_files:
        args.append(f"--filter=merge {path}")
    for pattern in exclude:
        args.append(f"--exclude={pattern}")
    if not include_ignored:
        for pattern in _gitignored_relpaths(source):
            args.append(f"--exclude=/{pattern}")
    # Trailing slash on the source copies its *contents* into dest.
    args += [f"{source}/", f"{dest}/"]

    return subprocess.run(args, capture_output=True, text=True, check=False)


def _gitignored_relpaths(source: Path) -> list[str]:
    """Return repo-relative paths git considers ignored under *source*.

    Used to drop ignored files from the rsync copy under ``--no-ignored``.
    """
    result = subprocess.run(
        ["git", "-C", str(source), "status", "--ignored", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    paths: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("!! "):
            paths.append(line[3:].strip().strip("/"))

    return paths
