"""Worktree-level commands (enter, leave, create, delete, rename, list, resolve, set).

The worktree-level commands are canonically ``wtm worktree <cmd>``; the seven
session/lifecycle/query verbs (enter, leave, create, delete, rename, list,
resolve) are additionally dual-homed as top-level ``wtm <cmd>`` shortcuts, and
``set`` is worktree-namespaced only. The handlers are defined here plain (not
decorated onto ``worktree_app``) so :mod:`wtm.cli.app` can register each under
both apps in a single ordered block -- keeping the help ordering in one place
and avoiding drift between the two homes.
"""

from __future__ import annotations

import typer

from wtm.cli.completions.locator import (
    _complete_locate,
    _complete_locate_project,
    _complete_locate_worktree,
)
from wtm.cli.completions.target import _complete_branch, _complete_target
from wtm.cli.locator import _locate_one, _locate_parent_args, _locate_project
from wtm.config import resolve_project_dir
from wtm.core import WtmError, current_dir, remap_dir, rescue_dir, worktree_name
from wtm.hooks import Scope
from wtm.locator import Location, Tier, build_index
from wtm.output import emit_env_line, emit_shell, log, warn, worktree_list_lines
from wtm.target import looks_like_path
from wtm.worktree import BranchStrategy, WorktreeCreated, WorktreeRenamed

worktree_app = typer.Typer(
    help="Worktree-level operations (enter, leave, create, delete, rename, list, resolve).",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def enter(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[[WORKSPACE] PROJECT] NAME | [DIR]",
        help="Locator: a name resolved across every workspace (worktree > project "
        "> workspace), qualified by leading tokens, or a DIR",
        autocompletion=_complete_locate,
    ),
    do_install: bool = typer.Option(False, "-i", "--install", help="Run install hooks first"),
    strict: bool = typer.Option(False, "--strict", help="Hook failures are fatal"),
) -> None:
    """cd into a worktree, project, or workspace, resolved from a locator.

    A single free NAME resolves across every declared workspace by tier cascade
    (a worktree named NAME beats a project beats a workspace); leading tokens
    qualify it (``PROJECT NAME`` / ``WORKSPACE NAME`` / ``WORKSPACE PROJECT
    NAME``). Exactly one match is entered with that tier's WTM_* contract;
    several print the qualified choices and exit non-zero. ``leave`` undoes this
    with the same locator. A single DIR (``/``, ``./``, ``../``, ``.``/``..``)
    or no arg (the cwd worktree) stays the worktree path, as before.
    """
    _enter_via_locator(ctx, args or [], do_install=do_install, strict=strict)


def _enter_via_locator(
    ctx: typer.Context,
    args: list[str],
    *,
    do_install: bool,
    strict: bool,
) -> None:
    """Resolve a locator and cd into the matched worktree/project/workspace.

    A DIR single arg or the no-arg cwd default keep the worktree path (they are
    not names to resolve). Otherwise the tokens are resolved by the unpinned tier
    cascade -- ``enter`` is tier-polymorphic -- and entered with that tier's
    WTM_* contract.
    """
    worktree = ctx.obj.worktree
    # No args (cwd worktree) or a single DIR arg stay the classic worktree path.
    if not args or (len(args) == 1 and looks_like_path(args[0])):
        dest = worktree.enter(args, do_install=do_install, strict=strict)
        emit_shell(cd=dest, env=worktree.env_for_dir(dest))

        return

    location = _locate_one(ctx, args, command="enter")
    _enter_location(ctx, location, do_install=do_install, strict=strict)


def _enter_location(
    ctx: typer.Context, location: Location, *, do_install: bool, strict: bool
) -> None:
    """Dispatch a resolved locator to its tier's enter, reconstructing a canonical call.

    Each tier is entered through its existing operation (no new mutation logic):
    a worktree via the fully-qualified ``WORKSPACE PROJECT NAME`` triple (so it
    resolves regardless of the active workspace -- the portability win), a project
    via :meth:`Project.enter`, a workspace via :meth:`Workspaces.enter`; each with
    that tier's WTM_* contract on the cd side-channel.
    """
    if location.tier is Tier.WORKTREE:
        worktree = ctx.obj.worktree
        triple = [location.workspace, location.project, location.name]
        dest = worktree.enter(triple, do_install=do_install, strict=strict)
        emit_shell(cd=dest, env=worktree.env_for_dir(dest))
    elif location.tier is Tier.PROJECT:
        result = ctx.obj.project.enter(location.workspace, location.project, strict=strict)
        emit_shell(
            cd=result.dir,
            env=ctx.obj.project.resolve_env(location.workspace, location.project),
        )
    else:
        result = ctx.obj.workspaces.enter(location.workspace, strict=strict)
        emit_shell(
            cd=result.dir,
            env={"WTM_WORKSPACE": result.name, "WTM_WORKSPACE_DIR": str(result.dir)},
        )


def leave(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[[WORKSPACE] PROJECT] NAME | [DIR]",
        help="Locator: a name resolved across every workspace (worktree > project "
        "> workspace), qualified by leading tokens, or a DIR",
        autocompletion=_complete_locate,
    ),
    no_cd: bool = typer.Option(
        False, "-C", "--no-cd", help="don't cd out (just run leave hooks; stay in the worktree)"
    ),
    strict: bool = typer.Option(False, "--strict", help="Hook failures are fatal"),
) -> None:
    """Run leave hooks for a worktree, project, or workspace; cds out unless -C.

    ``enter``'s inverse, and it takes the same locator, so a session entered by
    name is left by that same name: the tier cascade picks what to tear down (a
    worktree beats a project beats a workspace) and each tier's own leave runs,
    cascading downward as it already does. A single DIR or no arg is the cwd
    worktree, as before. Ends the shell session either way: the WTM_* contract is
    unset even with -C (which only suppresses the cd-out, not the teardown).
    """
    args = args or []
    # No args (cwd worktree) or a single DIR arg stay the classic worktree path.
    if not args or (len(args) == 1 and looks_like_path(args[0])):
        result = ctx.obj.worktree.leave(args, strict=strict)
        emit_shell(cd=None if no_cd else result.cd_dir, env={})

        return

    _leave_location(ctx, _locate_one(ctx, args, command="leave"), no_cd=no_cd, strict=strict)


def _leave_location(ctx: typer.Context, location: Location, *, no_cd: bool, strict: bool) -> None:
    """Dispatch a resolved locator to its tier's leave -- the mirror of :func:`_enter_location`.

    Each tier is torn down through its existing operation, so the cascades already
    built into them (a project leave firing the worktree's leave first, a workspace
    leave the whole chain) are inherited rather than restated. The worktree tier
    goes through the fully-qualified triple, exactly as entering does.
    """
    if location.tier is Tier.WORKTREE:
        triple = [location.workspace, location.project, location.name]
        wt_result = ctx.obj.worktree.leave(triple, strict=strict)
        emit_shell(cd=None if no_cd else wt_result.cd_dir, env={})

        return

    if location.tier is Tier.PROJECT:
        result = ctx.obj.project.leave(location.workspace, location.project, strict=strict)
        log(f"left project '{result.name}'")
    else:
        result = ctx.obj.workspaces.leave(location.workspace, strict=strict)
        log(f"left workspace '{result.name}'")
    cd_dir = result.cd_dir if (not no_cd and result.cd_dir is not None) else None
    emit_shell(cd=cd_dir, env={}, fallback=False)


def create(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[[WORKSPACE] PROJECT] NAME",
        help="Target: NAME, or NAME qualified by a PROJECT locator ([[WORKSPACE] PROJECT] NAME)",
        autocompletion=_complete_locate_project,
    ),
    branch: str | None = typer.Option(
        None, "-b", "--branch", help="Branch name", autocompletion=_complete_branch
    ),
    base: str | None = typer.Option(
        None,
        "--base",
        "--off",
        help="Base ref a new branch forks from (overrides configured base-branch)",
        autocompletion=_complete_branch,
    ),
    destdir: str | None = typer.Option(None, "-d", "--dir", help="Destination dir"),
    do_cd: bool = typer.Option(False, "-c", "--cd", help="cd into the new worktree"),
    do_install: bool = typer.Option(False, "-i", "--install", help="Run install hooks too"),
    do_fetch: bool = typer.Option(
        False, "-f", "--fetch", help="Fetch origin first so a new branch forks from the latest"
    ),
    strict: bool = typer.Option(False, "--strict", help="Hook failures are fatal"),
) -> None:
    """Add a worktree (and branch) to a project.

    NAME is invented, so only its leading PROJECT qualifier is resolved -- as a
    project-pinned locator, so ``wtm create otherproj feat`` reaches another
    workspace's project.
    """
    result = ctx.obj.worktree.create(
        _locate_parent_args(
            ctx,
            args or [],
            command="create",
            include_config_only=True,
            require_project_on_disk=True,
        ),
        branch=branch,
        base=base,
        destdir=destdir,
        do_fetch=do_fetch,
        do_install=do_install,
        do_enter=do_cd,
        strict=strict,
    )
    if do_fetch and not result.fetched:
        warn("no origin remote; skipped fetch")
    elif result.fetched:
        log("fetched origin")
    log(_describe_create(result))
    if do_cd:
        dest = result.context.dir
        emit_shell(cd=dest, env=ctx.obj.worktree.env_for_dir(dest))


def _describe_create(result: WorktreeCreated) -> str:
    """Render the branch-resolution diagnostic for a create result."""
    if result.strategy is BranchStrategy.EXISTING_LOCAL:
        return f"worktree from existing local branch '{result.branch}'"
    if result.strategy is BranchStrategy.TRACKING:
        return f"worktree tracking 'origin/{result.branch}'"
    if result.strategy is BranchStrategy.NEW_BRANCH:
        return f"new branch '{result.branch}' off '{result.base}'"

    return f"new orphan branch '{result.branch}' (empty repository)"


def delete(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="WORKSPACE PROJECT NAME | PROJECT NAME | DIR",
        help="Worktree target (required)",
        autocompletion=_complete_target,
    ),
    force: bool = typer.Option(False, "-f", "--force", help="Force past local changes"),
    del_branch: bool = typer.Option(False, "-D", "--delete-branch", help="Delete the branch too"),
    strict: bool = typer.Option(False, "--strict", help="Hook failures are fatal"),
) -> None:
    """Remove a worktree (requires an explicit target)."""
    # Capture cwd before the destructive delete so a shell standing in the
    # doomed worktree can be sent somewhere that still exists afterwards.
    cwd = current_dir()
    result = ctx.obj.worktree.delete(args or [], force=force, del_branch=del_branch, strict=strict)
    if result.branch is not None:
        log(f"deleting branch '{result.branch}'")
        if result.branch_delete_failed:
            warn(f"could not delete branch '{result.branch}'")
    rescue = rescue_dir(cwd, result.worktree_dir, result.project_dir)
    if rescue is not None:
        emit_shell(cd=rescue, env={}, fallback=False)


def rename(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[[WORKSPACE] PROJECT] [OLDNAME] NEWNAME",
        help="Source worktree target followed by the new NAME (the last positional)",
        autocompletion=_complete_target,
    ),
    branch: bool = typer.Option(False, "-b", "--branch", help="Also rename the branch to NEWNAME"),
    strict: bool = typer.Option(False, "--strict", help="Hook failures are fatal"),
) -> None:
    """Rename (move) a worktree; -b also renames its branch to the new name.

    NEWNAME is the last positional; anything before it is the source target
    (PROJECT NAME | NAME | DIR | cwd).
    """
    # Capture cwd before the move so a shell standing in the renamed worktree
    # can follow it to the new location (the mapped subpath).
    cwd = current_dir()
    result = ctx.obj.worktree.rename(args or [], rename_branch=branch, strict=strict)
    log(_describe_rename(result))
    if result.branch_renamed:
        log(f"renamed branch '{result.old_branch}' -> '{result.new_branch}'")
    remapped = remap_dir(cwd, result.old_dir, result.new_dir)
    if remapped is not None:
        emit_shell(cd=remapped, env=ctx.obj.worktree.env_for_dir(remapped), fallback=False)


def _describe_rename(result: WorktreeRenamed) -> str:
    """Render the worktree-move diagnostic, naming both ends relative to worktrees/."""
    old = worktree_name(result.old_dir, result.worktrees_dir) or str(result.old_dir)
    new = worktree_name(result.new_dir, result.worktrees_dir) or str(result.new_dir)

    return f"renamed worktree '{old}' -> '{new}'"


def list_(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[[WORKSPACE] PROJECT]",
        help="Locator: a project name resolved across every workspace "
        "(default: inferred from cwd)",
        autocompletion=_complete_locate_project,
    ),
) -> None:
    """List a project's worktrees (PROJECT resolved by locator, or inferred from cwd)."""
    config = ctx.obj.worktree.config
    workspace, project = _locate_project(ctx, args or [], command="list")
    worktrees_dir = resolve_project_dir(config, workspace, project) / "worktrees"
    rows: list[tuple[str, str]] = []
    for entry in ctx.obj.worktree.list(workspace, project, strict=True):
        name = worktree_name(entry.path, worktrees_dir) or str(entry.path)
        if entry.detached:
            annotation = f"{entry.head[:8]} (detached)"
        elif entry.branch:
            annotation = f"{entry.head[:8]} [{entry.branch}]"
        else:
            annotation = entry.head[:8]
        rows.append((name, annotation))
    for line in worktree_list_lines(rows):
        print(line)


def resolve(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[[WORKSPACE] PROJECT] NAME",
        help="Locator: a name resolved across every workspace (worktree > project "
        "> workspace), or none (the cwd worktree)",
        autocompletion=_complete_locate,
    ),
    emit_env: bool = typer.Option(False, "-e", "--env", help="Emit the WTM_* contract for eval"),
) -> None:
    """Print a worktree's, project's, or workspace's path (--env: emit WTM_* exports).

    A read-only twin of ``enter``: the same unpinned tier cascade (worktree >
    project > workspace) resolves the target, and the resolved tier's path is
    printed. With no target it resolves the worktree the cwd is in (inspecting
    git). With --env it emits that tier's WTM_* contract for eval -- the
    worktree contract inspects git, the project and workspace contracts are
    pure path math (no WTM_WORKTREE_DIR / WTM_BRANCH).
    """
    worktree = ctx.obj.worktree
    args = args or []
    # No args: the cwd worktree (git inspection), as before.
    if not args:
        if emit_env:
            for key, value in worktree.resolve_env([]).items():
                print(emit_env_line(key, value))
        else:
            print(worktree.resolve_path([]))

        return

    if looks_like_path(args[0]) and len(args) == 1:
        raise WtmError(f"takes a NAME, not a path: {args[0]}")

    location = _locate_one(ctx, args, command="resolve")
    if location.tier is Tier.WORKTREE:
        index = build_index(ctx.obj.config)
        dest = index.roots[location.workspace] / location.project / "worktrees" / location.name
        env = worktree.env_for_dir(dest) if emit_env else None
    elif location.tier is Tier.PROJECT:
        dest = ctx.obj.project.resolve_path(location.workspace, location.project)
        if emit_env:
            env = ctx.obj.project.resolve_env(location.workspace, location.project)
        else:
            env = None
    else:
        ws = ctx.obj.config.workspaces[location.workspace]
        dest = ws.path
        env = {"WTM_WORKSPACE": ws.name, "WTM_WORKSPACE_DIR": str(ws.path)} if emit_env else None
    if env is not None:
        for key, value in env.items():
            print(emit_env_line(key, value))
    else:
        print(dest)


def set_(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[[[WORKSPACE] PROJECT] NAME]",
        help="Target: NAME (its PROJECT qualifier resolved as a locator), a DIR, or "
        "none (the cwd worktree)",
        autocompletion=_complete_locate_worktree,
    ),
    description: str | None = typer.Option(
        None, "-d", "--description", help="Description (empty clears it)"
    ),
    scope: Scope | None = typer.Option(
        None,
        "-s",
        "--scope",
        help="Where to write (worktree|project|workspace|user|system); "
        "default: deepest existing dir",
    ),
) -> None:
    """Annotate a worktree's description (partial update).

    Writes at ``--scope`` (worktree/project/workspace/user/system); the default
    is the deepest scope whose directory exists. A field passed as ``""`` clears
    it. An annotation may precede the worktree, so the NAME is literal and only
    its PROJECT qualifier is resolved as a locator.
    """
    result = ctx.obj.worktree.set(
        _locate_parent_args(ctx, args or [], command="set"), description=description, scope=scope
    )
    log(f"updated worktree '{result.name}' [{result.scope.value}] ({result.address.file})")
