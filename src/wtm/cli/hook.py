"""Hook management commands (install, run, delete, list, set, unset)."""

from __future__ import annotations

import typer

from wtm.cli.common import _GENERIC_SCOPE_HELP, _generic_scope
from wtm.cli.completions.config import _complete_scope
from wtm.cli.completions.locator import _complete_locate_hook_target, _complete_locate_worktree
from wtm.cli.completions.target import _complete_event, _complete_hook_target, _complete_target
from wtm.cli.locator import _locate_worktree_args
from wtm.core import WtmError, current_dir
from wtm.hooks import EVENTS, Scope
from wtm.output import log

hook_app = typer.Typer(
    help="Manage wtm hooks (install, run, delete, list) across scopes.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@hook_app.command(name="install")
def hook_install(
    ctx: typer.Context,
    source: str = typer.Argument(..., metavar="FILE | DIR", help="Hook source"),
    args: list[str] = typer.Argument(
        None,
        metavar="[[WORKSPACE] PROJECT] NAME | [DIR]",
        help="Worktree target (unused for --scope system/user/workspace)",
        autocompletion=_complete_target,
    ),
    scope: Scope = typer.Option(Scope.PROJECT, "-s", "--scope", help="Where to install the hook"),
    event: str | None = typer.Option(
        None,
        "-e",
        "--event",
        help="Event (create|install|enter|leave|delete); inferred from an "
        "<event>-named source otherwise",
        autocompletion=_complete_event,
    ),
    link: bool = typer.Option(False, "-l", "--link", help="Symlink instead of copy"),
    force: bool = typer.Option(False, "-f", "--force", help="Overwrite an existing hook"),
) -> None:
    """Install a hook file or directory into a scope."""
    dest = ctx.obj.hooks.install(
        source, args or [], scope=scope, event=event, link=link, force=force
    )
    verb = "linked" if link else "installed"
    log(f"{verb} hook at {dest}")


@hook_app.command(name="run")
def hook_run(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[[WORKSPACE] PROJECT] NAME | [PATH] HOOK",
        help="Locator: a worktree name resolved across every workspace (or a PATH "
        "truncated to its worktree), followed by the event (HOOK) to run",
        autocompletion=_complete_locate_hook_target,
    ),
    scope: Scope | None = typer.Option(
        None, "-s", "--scope", help="Restrict to one scope (default: all)"
    ),
    strict: bool = typer.Option(False, "--strict", help="Hook failures are fatal"),
) -> None:
    """Run an event's hooks for a worktree (all scopes unless --scope given).

    HOOK (the event) is the last positional; anything before it is the target, a
    worktree-pinned locator (as for ``hook list``), so a bare name reaches
    another workspace's worktree. No target is the cwd worktree.
    """
    if not args:
        raise WtmError("hook run requires an event (HOOK) as the last argument")
    *target, hook = args
    ctx.obj.hooks.run(
        hook, _locate_worktree_args(ctx, target, command="hook run"), scope=scope, strict=strict
    )


@hook_app.command(name="delete")
def hook_delete(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[[WORKSPACE] PROJECT] NAME | [DIR] HOOK",
        help="Worktree target followed by the event (or event/entry) to delete "
        "(target unused for --scope system/user/workspace)",
        autocompletion=_complete_hook_target,
    ),
    scope: Scope = typer.Option(
        Scope.PROJECT, "-s", "--scope", help="Where to delete the hook from"
    ),
    recursive: bool = typer.Option(
        False, "-r", "--recursive", help="Also remove the <event>.d/ directory"
    ),
) -> None:
    """Delete a hook: an event (bare file; .d/ needs -r) or event/entry.

    HOOK is the last positional; anything before it is the worktree target.
    """
    if not args:
        raise WtmError("hook delete requires an event (HOOK) as the last argument")
    *target, hook = args
    for path in ctx.obj.hooks.delete(hook, target, scope=scope, recursive=recursive):
        log(f"deleted {path}")


@hook_app.command(name="list")
def hook_list(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[[WORKSPACE] PROJECT] NAME | [PATH]",
        help="Locator: a worktree name resolved across every workspace, or a PATH "
        "truncated to its worktree (default: cwd)",
        autocompletion=_complete_locate_worktree,
    ),
    types: bool = typer.Option(False, "-t", "--types", help="Show a compact per-event summary"),
) -> None:
    """List installed hooks, grouped by event across scopes.

    A read-only query over an existing worktree, so it takes a worktree-pinned
    locator: a bare name reaches another workspace's worktree. No arg is the
    cwd worktree, as before.
    """
    grouped = ctx.obj.hooks.list(_locate_worktree_args(ctx, args or [], command="hook list"))
    plugin = ctx.obj.hooks.plugin_rows(args or [])
    for line in _render_hook_list(grouped, types=types, plugin=plugin):
        print(line)


def _render_hook_list(
    grouped: dict[str, list],
    *,
    types: bool,
    plugin: dict[str, list[tuple[str, str]]] | None = None,
) -> list[str]:
    """Render the grouped hook listing (full tree, or a compact per-event summary).

    *plugin* carries the active plugins' contributed commands (the pseudo-tier
    alongside the five scopes), rendered as ``(plugin:<name>)`` rows after the
    scopes -- setup-first/teardown-last placement noted in the docs.
    """
    plugin = plugin or {}
    lines: list[str] = []
    for event, listings in grouped.items():
        plugin_rows = plugin.get(event, [])
        if types:
            count = sum(
                (1 if lst.file else 0) + len(lst.entries) + len(lst.inline) for lst in listings
            ) + len(plugin_rows)
            scopes = ",".join(lst.scope.value for lst in listings) or "-"
            lines.append(f"{event}: {count} hook(s) [{scopes}]")
            continue
        if not listings and not plugin_rows:
            continue
        lines.append(f"{event}:")
        for lst in listings:
            lines.append(f"  {lst.scope.value}:")
            if lst.file is not None:
                lines.append(f"    {lst.file}")
            for entry in lst.entries:
                lines.append(f"    {entry}")
            for command in lst.inline:
                lines.append(f"    (config) {command}")
        if plugin_rows:
            lines.append("  plugin:")
            for name, command in plugin_rows:
                lines.append(f"    (plugin:{name}) {command}")

    return lines


def _validate_event(event: str) -> None:
    """Reject an unknown event name (guards the inline-hook writer)."""
    if event not in EVENTS:
        raise WtmError(f"unknown event '{event}' (expected {' | '.join(EVENTS)})")


@hook_app.command(name="set")
def hook_set(
    ctx: typer.Context,
    event: str = typer.Argument(
        ..., metavar="EVENT", help="Lifecycle event", autocompletion=_complete_event
    ),
    command: str = typer.Argument(..., metavar="CMD", help="Inline sh -c command to run"),
    scope: Scope | None = typer.Option(
        None, "-s", "--scope", help=_GENERIC_SCOPE_HELP, autocompletion=_complete_scope
    ),
) -> None:
    """Set an inline hook for EVENT on the cwd-inferred entity.

    Writes ``[...hooks.on] EVENT = "CMD"`` at the entity's table-path for the
    chosen ``--scope`` (defaulting to the deepest scope whose directory exists),
    reusing the arbitrary-key prefix machinery. Replaces any inline command
    already declared for EVENT at that scope's file; commands still accumulate
    across scopes at read time.
    """
    _validate_event(event)
    config = ctx.obj.config
    target = config.resolve_generic_target(current_dir(resolve=False))
    scope = _generic_scope(target, scope)
    address, prefix = target.write_target(scope)
    config.set_value(address, (*prefix, "hooks", "on", event), command)
    log(f"{target.label}: set hook {event} [{scope.value}] ({address.file})")


@hook_app.command(name="unset")
def hook_unset(
    ctx: typer.Context,
    event: str = typer.Argument(
        ..., metavar="EVENT", help="Lifecycle event", autocompletion=_complete_event
    ),
    scope: Scope | None = typer.Option(
        None, "-s", "--scope", help=_GENERIC_SCOPE_HELP, autocompletion=_complete_scope
    ),
) -> None:
    """Remove the inline hook for EVENT from one tier of the cwd-inferred entity.

    Mirrors :func:`hook_set`: the entity is cwd-inferred and ``--scope`` picks
    the file (defaulting to the deepest existing dir). A no-op when no inline
    command is declared for EVENT at that tier.
    """
    _validate_event(event)
    config = ctx.obj.config
    target = config.resolve_generic_target(current_dir(resolve=False))
    scope = _generic_scope(target, scope)
    address, prefix = target.write_target(scope)
    config.unset_value(address, (*prefix, "hooks", "on", event))
    log(f"{target.label}: unset hook {event} [{scope.value}] ({address.file})")
