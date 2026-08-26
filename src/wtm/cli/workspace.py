"""Workspace commands: list, resolve, show, add, set, rename, remove, default, enter, leave."""

from __future__ import annotations

import typer

from wtm.cli.completions.target import _complete_workspace
from wtm.core import WtmError
from wtm.hooks import Scope
from wtm.output import emit_env_line, emit_shell, log, warn

workspace_app = typer.Typer(
    help=(
        "Workspace-level operations (list, resolve, show, add, set, rename, "
        "remove, default, enter, leave)."
    ),
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@workspace_app.command(name="list")
def workspace_list(ctx: typer.Context) -> None:
    """List declared workspaces (name, path, active marker, description)."""
    config = ctx.obj.config
    active = config.active
    for name, ws in sorted(config.workspaces.items()):
        marker = "*" if name == active else " "
        exists = "" if ws.path.is_dir() else "  (missing)"
        desc = f"  {ws.description}" if ws.description else ""
        print(f"{marker} {name}\t{ws.path}{exists}{desc}")


@workspace_app.command(name="resolve")
def workspace_resolve(
    ctx: typer.Context,
    name: str | None = typer.Argument(
        None,
        help="Workspace (default: the active workspace)",
        autocompletion=_complete_workspace,
    ),
    emit_env: bool = typer.Option(
        False, "-e", "--env", help="Emit WTM_WORKSPACE / WTM_WORKSPACE_DIR for eval"
    ),
) -> None:
    """Print a workspace's path (--env: emit the workspace WTM_* exports)."""
    config = ctx.obj.config
    if name is not None:
        if name not in config.workspaces:
            raise WtmError(f"unknown workspace: '{name}'")
        ws = config.workspaces[name]
    else:
        ws = config.active_workspace
    if emit_env:
        print(emit_env_line("WTM_WORKSPACE", ws.name))
        print(emit_env_line("WTM_WORKSPACE_DIR", str(ws.path)))
    else:
        print(ws.path)


@workspace_app.command(name="show")
def workspace_show(
    ctx: typer.Context,
    name: str | None = typer.Argument(
        None, help="Workspace (default: the active workspace)", autocompletion=_complete_workspace
    ),
) -> None:
    """Show a workspace's detail (path, description, base-branch, scope, project count)."""
    result = ctx.obj.workspaces.show(name)
    exists = "" if result.exists else "  (missing)"
    print(f"name: {result.name}")
    print(f"path: {result.path}{exists}")
    if result.description:
        print(f"description: {result.description}")
    if result.base_branch:
        print(f"base-branch: {result.base_branch}")
    print(f"scope: {result.scope.value}")
    print(f"projects: {result.project_count}")


@workspace_app.command(name="add")
def workspace_add(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Workspace name"),
    path: str | None = typer.Argument(None, help="Projects root path (default: cwd)"),
    description: str | None = typer.Option(
        None, "-d", "--description", help="Workspace description"
    ),
    base_branch: str | None = typer.Option(
        None, "-b", "--base-branch", help="Default fork point for new branches"
    ),
    create: bool = typer.Option(False, "-c", "--create", help="mkdir -p the path if absent"),
) -> None:
    """Add a workspace declaration to the user config (PATH defaults to the cwd)."""
    result = ctx.obj.workspaces.add(
        name, path, description=description, base_branch=base_branch, create=create
    )
    if result.duplicate_of is not None:
        warn(
            f"path {result.path} is already declared as '{result.duplicate_of}'; "
            "cwd-based resolution there will require -w"
        )
    if result.shadows_system:
        warn(f"'{name}' also declared at system scope; the user entry shadows it")
    log(f"registered '{name}' -> {result.path}")
    if result.created:
        log(f"created {result.resolved}")
    elif result.missing:
        warn(f"path does not exist yet: {result.resolved}")


@workspace_app.command(name="set")
def workspace_set(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Workspace name", autocompletion=_complete_workspace),
    path: str | None = typer.Option(None, "-p", "--path", help="New path (empty rejected)"),
    description: str | None = typer.Option(
        None, "-d", "--description", help="Description (empty clears it)"
    ),
    base_branch: str | None = typer.Option(
        None, "-b", "--base-branch", help="Base branch (empty clears it)"
    ),
    scope: Scope | None = typer.Option(
        None,
        "-s",
        "--scope",
        help="Where to write scalars (workspace|user|system); default: deepest existing dir",
    ),
) -> None:
    """Edit an existing workspace's scalars (partial update).

    ``--path`` always rewrites the user-scope registry entry. ``--description`` /
    ``--base-branch`` land at ``--scope`` (workspace/user/system); the default is
    the deepest scope whose directory exists.
    """
    result = ctx.obj.workspaces.set(
        name, path=path, description=description, base_branch=base_branch, scope=scope
    )
    where = f" [{result.scope.value}]" if result.scope is not None else ""
    log(f"updated '{result.name}'{where}")
    if result.resolved is not None and result.missing:
        warn(f"path does not exist yet: {result.resolved}")


@workspace_app.command(name="rename")
def workspace_rename(
    ctx: typer.Context,
    old: str = typer.Argument(..., help="Current name", autocompletion=_complete_workspace),
    new: str = typer.Argument(..., help="New name"),
) -> None:
    """Rename a workspace's registry key (does not move the directory)."""
    result = ctx.obj.workspaces.rename(old, new)
    log(f"renamed workspace '{result.old}' -> '{result.new}'")
    if result.fixed_default:
        log(f"default-workspace updated to '{result.new}'")


@workspace_app.command(name="remove")
def workspace_remove(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Workspace name", autocompletion=_complete_workspace),
) -> None:
    """Remove a workspace's declaration from the user config (never touches the projects root)."""
    result = ctx.obj.workspaces.remove(name)
    log(f"removed '{result.name}'")
    if result.cleared_default:
        log("cleared default-workspace (it pointed here)")
    if result.remains_at_system:
        warn(f"'{result.name}' is still declared at system scope; it remains resolvable")


@workspace_app.command(name="default")
def workspace_default(
    ctx: typer.Context,
    name: str | None = typer.Argument(
        None, help="Workspace to make default (omit to show)", autocompletion=_complete_workspace
    ),
    clear: bool = typer.Option(False, "--clear", help="Remove the default-workspace key"),
) -> None:
    """Get, set, or clear the default-workspace (user config)."""
    if clear:
        if name is not None:
            raise WtmError("--clear takes no NAME")
        ctx.obj.workspaces.default_clear()
        log("cleared default-workspace")

        return
    if name is None:
        current = ctx.obj.workspaces.default_show()
        print(current if current else "(none set)")

        return
    ctx.obj.workspaces.default_set(name)
    log(f"default-workspace set to '{name}'")


@workspace_app.command(name="enter")
def workspace_enter(
    ctx: typer.Context,
    name: str | None = typer.Argument(
        None, help="Workspace (default: the active workspace)", autocompletion=_complete_workspace
    ),
    strict: bool = typer.Option(False, "--strict", help="Hook failures are fatal"),
) -> None:
    """cd into a workspace's projects root, firing workspace-enter hooks."""
    result = ctx.obj.workspaces.enter(name, strict=strict)
    emit_shell(
        cd=result.dir,
        env={"WTM_WORKSPACE": result.name, "WTM_WORKSPACE_DIR": str(result.dir)},
    )


@workspace_app.command(name="leave")
def workspace_leave(
    ctx: typer.Context,
    name: str | None = typer.Argument(
        None, help="Workspace (default: the active workspace)", autocompletion=_complete_workspace
    ),
    no_cd: bool = typer.Option(False, "-C", "--no-cd", help="Run hooks without cd-ing out"),
    strict: bool = typer.Option(False, "--strict", help="Hook failures are fatal"),
) -> None:
    """Fire workspace-leave hooks (cascading the worktree leave when inside one); cd out.

    Ends the session: the WTM_* contract is unset even with -C (which only
    suppresses the cd-out).
    """
    result = ctx.obj.workspaces.leave(name, strict=strict)
    log(f"left workspace '{result.name}'")
    cd_dir = result.cd_dir if (not no_cd and result.cd_dir is not None) else None
    emit_shell(cd=cd_dir, env={}, fallback=False)
