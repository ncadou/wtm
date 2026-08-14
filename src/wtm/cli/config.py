"""Config commands (show, base-branch, get, set, unset) and their render helpers."""

from __future__ import annotations

from pathlib import Path

import typer

from wtm.cli.common import _GENERIC_SCOPE_HELP, _generic_scope, _rung_label
from wtm.cli.completions.config import (
    _complete_config_get,
    _complete_config_set,
    _complete_config_show,
    _complete_config_unset,
    _complete_scope,
)
from wtm.cli.locator import _locate_one, _locate_optional
from wtm.config import (
    ConfigSlice,
    DepthTier,
    GenericTarget,
    ResolvedProject,
    ResolvedWorkspace,
    ResolvedWorktree,
    SliceTarget,
    validate_arbitrary_key,
    validate_inheritable_element,
)
from wtm.core import WtmError, current_dir
from wtm.hooks import HookSpec, Scope
from wtm.output import config_slice_json, log

config_app = typer.Typer(
    help="Inspect config (show), manage the global default (base-branch), "
    "and read/write arbitrary keys (get/set/unset).",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@config_app.command(name="show")
def config_show(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[LOCATOR]",
        help="Optional locator naming the target entity (worktree/project/workspace)",
        autocompletion=_complete_config_show,
    ),
    global_scope: bool = typer.Option(
        False, "-g", "--global", help="Target the global tier (mutually exclusive with a locator)"
    ),
    recurse: bool = typer.Option(
        False, "-r", "--recurse", help="Extend downward into the target's subtree"
    ),
    depth: DepthTier | None = typer.Option(
        None,
        "-d",
        "--depth",
        help="Cap the deepest tier rendered (global/workspace/project/worktree)",
    ),
    show_origin: bool = typer.Option(
        False, "-o", "--origin", help="Annotate where each value comes from"
    ),
    hooks: bool = typer.Option(False, "--hooks", help="Also show accumulated inline hooks"),
    as_json: bool = typer.Option(False, "-j", "--json", help="Emit as JSON instead of text"),
) -> None:
    """Show the effective merged config as a global-rooted pruned tree.

    One optional unpinned locator (or cwd) resolves a target entity. One pruned
    tree rooted at global is rendered. ``-r`` extends downward (additive);
    ``-d TIER`` caps the deepest tier rendered. ``--origin`` annotates where
    each value comes from; ``--hooks`` renders accumulated inline hooks. Both
    work in all modes.
    """
    config = ctx.obj.config
    args = args or []

    if as_json and (show_origin or hooks):
        raise WtmError("--json cannot be combined with --origin or --hooks")

    if global_scope and args:
        raise WtmError("-g/--global cannot be combined with a locator")

    if global_scope:
        target: SliceTarget | None = None
    elif args:
        location = _locate_one(ctx, args, command="config show", include_config_only=True)
        target = SliceTarget(
            tier=location.tier.value,
            workspace=location.workspace,
            project=location.project,
            name=location.name,
        )
    else:
        target = config.cwd_location()

    slice_ = config.resolve_slice(
        target=target, recurse=recurse, depth_limit=depth.depth if depth is not None else None
    )

    if as_json:
        print(config_slice_json(slice_))

        return
    _print_config_slice(slice_, show_origin, hooks)


def _print_extra(indent: str, extra: dict[str, object]) -> None:
    """Render an entity's arbitrary (non-reserved) keys, always in the cwd descent."""
    for key, value in sorted(extra.items()):
        print(f"{indent}{key}: {value}")


def _print_scope_hooks(indent: str, spec: HookSpec, show_origin: bool, scope: str) -> None:
    """Render a scope's inline hooks (``event = cmd``), skipping empty specs."""
    for event, commands in sorted(spec.on.items()):
        for command in commands:
            origin = f"  [{scope}]" if show_origin else ""
            print(f"{indent}hook {event} = {command}{origin}")


# --- unified slice renderer (global-rooted, ungated origin/hooks) -------------


def _print_config_slice(slice: ConfigSlice, show_origin: bool, hooks: bool) -> None:
    """Render a global-rooted pruned config slice as text.

    One tree from global down to *depth_limit*, with ``--origin`` annotating
    each value's source and ``--hooks`` rendering accumulated inline hooks at
    every tier. Both flags are ungated (work in all modes, per Q6).
    """
    if slice.default_base_branch:
        origin = (
            f"  [{slice.base_branch_source}]" if show_origin and slice.base_branch_source else ""
        )
        print(f"default base_branch: {slice.default_base_branch}{origin}")
    _print_extra("", slice.global_extra)
    if hooks:
        _print_scope_hooks("", slice.global_hooks, show_origin, "global")
    for ws in slice.workspaces:
        _print_slice_workspace(ws, "", show_origin, hooks)


def _print_slice_workspace(
    ws: ResolvedWorkspace, indent: str, show_origin: bool, hooks: bool
) -> None:
    """Render one workspace node with origin/hooks, descending its pruned projects."""
    origin = f"  [{ws.entry.scope.value}]" if show_origin else ""
    print(f"{indent}workspace {ws.entry.name}: {ws.entry.path}{origin}")
    if ws.entry.description:
        print(f"{indent}  description: {ws.entry.description}")
    if ws.entry.base_branch:
        bb_origin = f"  [{ws.base_branch_source}]" if show_origin and ws.base_branch_source else ""
        print(f"{indent}  base_branch: {ws.entry.base_branch}{bb_origin}")
    _print_extra(f"{indent}  ", ws.entry.extra)
    if hooks:
        _print_scope_hooks(f"{indent}  ", ws.entry.hooks, show_origin, "workspace")
    for project in ws.projects:
        _print_slice_project(project, f"{indent}  ", show_origin, hooks)


def _print_slice_project(
    project: ResolvedProject, indent: str, show_origin: bool, hooks: bool
) -> None:
    """Render one project node with origin/hooks, descending its pruned worktrees."""
    inert = "" if project.exists else "  (no .git on disk)"
    print(f"{indent}project {project.name}: {project.dir}{inert}")
    if project.description:
        print(f"{indent}  description: {project.description}")
    if project.effective_base_branch:
        origin = f"  [{project.base_branch_source}]" if show_origin else ""
        print(f"{indent}  base_branch: {project.effective_base_branch}{origin}")
    _print_extra(f"{indent}  ", project.extra)
    if hooks:
        _print_scope_hooks(f"{indent}  ", project.hooks, show_origin, "project")
    for worktree in project.worktrees:
        _print_slice_worktree(worktree, f"{indent}  ", show_origin, hooks)


def _print_slice_worktree(
    worktree: ResolvedWorktree, indent: str, show_origin: bool, hooks: bool
) -> None:
    """Render one worktree leaf with origin/hooks (no base_branch tier)."""
    print(f"{indent}worktree {worktree.name}: {worktree.dir}")
    if worktree.description:
        print(f"{indent}  description: {worktree.description}")
    _print_extra(f"{indent}  ", worktree.extra)
    if hooks:
        _print_scope_hooks(f"{indent}  ", worktree.hooks, show_origin, "worktree")


def _tilde(path: Path) -> str:
    """Render *path* with the home dir collapsed to ``~`` (best-effort)."""
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


@config_app.command(name="base-branch")
def config_base_branch(
    ctx: typer.Context,
    value: str | None = typer.Argument(
        None, metavar="[VALUE]", help="New global base_branch (omit to show)"
    ),
    clear: bool = typer.Option(False, "--clear", help="Remove the top-level base_branch key"),
) -> None:
    """Get, set, or clear the global default base_branch (user config).

    With no VALUE it prints the effective global default a real command would
    take, annotated with the scope it comes from; setting or clearing writes the
    user-scope top-level ``base_branch``.
    """
    if clear:
        if value is not None:
            raise WtmError("--clear takes no VALUE")
        ctx.obj.config.clear_base_branch()
        log("cleared base_branch")

        return
    if value is None:
        current = ctx.obj.config.default_base_branch
        if not current:
            print("(none set)")

            return
        source = ctx.obj.config.base_branch_source()
        marker = f" [{source.value}]" if source is not None else ""
        print(f"{current}{marker}")

        return
    ctx.obj.config.set_base_branch(value)
    log(f"base_branch set to '{value}'")


# --- generic key surface (arbitrary keys, cwd-inferred entity) ---------------


@config_app.command(name="set")
def config_set(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        ...,
        metavar="[LOCATOR] KEY [SUBKEY] VALUE",
        help="Optional locator prefix, then key (plus SUBKEY for -t) and value",
        autocompletion=_complete_config_set,
    ),
    scope: Scope | None = typer.Option(
        None, "-s", "--scope", help=_GENERIC_SCOPE_HELP, autocompletion=_complete_scope
    ),
    array: bool = typer.Option(
        False, "-a", "--array", help="Add VALUE to the array at KEY (set semantics, idempotent)"
    ),
    table: bool = typer.Option(
        False, "-t", "--table", help="Set SUBKEY = VALUE in the table at KEY"
    ),
) -> None:
    """Set an arbitrary config KEY on the cwd-inferred or locator-named entity.

    The entity is the deepest one the cwd sits inside (worktree, else project,
    else the active workspace), or -- when an optional **locator** prefix
    precedes the key group -- whichever entity the locator resolves to across
    every declared workspace. ``--scope`` picks which file tier of that
    entity's cascade to write, defaulting to the deepest scope whose directory
    exists. Reserved keys (``hooks``/``projects``/``worktrees``/``path``/
    ``default_workspace``) have their own commands and are refused here.

    The write is scalar by default (``KEY VALUE``). ``-a``/``--array`` (``KEY
    VALUE``) instead adds VALUE to the string array at KEY, idempotently (set
    semantics); ``-t``/``--table`` (``KEY SUBKEY VALUE``) sets SUBKEY in the
    table at KEY. Both mutate element-wise under a per-file lock; the two flags
    are mutually exclusive. The one reserved array a user may mutate via ``-a``
    is ``inheritable`` (the opt-in allowlist of extra inheritable keys); each
    element it takes is validated (a reserved or hardcoded-non-inheritable key
    such as ``description`` is refused).

    The locator prefix is ``args[:-N]`` (N flag-decided: 3 for ``-t``, else 2);
    it is absent when ``len(args) == N``. ``config set KEY VALUE`` (cwd-inferred)
    is unchanged.
    """
    if array and table:
        raise WtmError("-a/--array and -t/--table are mutually exclusive")

    n = 3 if table else 2
    if len(args) < n:
        raise WtmError(
            f"config set {'-t ' if table else '-a ' if array else ''}requires "
            f"{'KEY SUBKEY VALUE' if table else 'KEY VALUE'}"
        )
    loc_args = args[:-n]
    if table:
        key, subkey, value = args[-3:]
    else:
        key, value = args[-2:]

    if key == "inheritable" and array:
        validate_inheritable_element(value)
    else:
        validate_arbitrary_key(key)
    config = ctx.obj.config
    if loc_args:
        location = _locate_one(ctx, loc_args, command="config set")
        target = config.resolve_generic_target_for(
            location.workspace, location.project, location.name
        )
    else:
        target = config.resolve_generic_target(current_dir(resolve=False))
    scope = _generic_scope(target, scope)
    address, prefix = target.write_target(scope)
    full_key = (*prefix, key) if prefix else key
    if array:
        config.add_to_array(address, full_key, value)
        log(f"{target.label}: {key} += {value} [{scope.value}] ({address.file})")

        return
    if table:
        config.set_table_value(address, full_key, subkey, value)
        log(f"{target.label}: {key}.{subkey} = {value} [{scope.value}] ({address.file})")

        return
    config.set_value(address, full_key, value)
    log(f"{target.label}: set {key} [{scope.value}] ({address.file})")


@config_app.command(name="get")
def config_get(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        ...,
        metavar="[LOCATOR] KEY [SUBKEY]",
        help="Optional locator prefix, then an arbitrary key (plus SUBKEY for a table cell)",
        autocompletion=_complete_config_get,
    ),
    show_origin: bool = typer.Option(
        False, "-o", "--origin", help="Print the full probe chain, most-local first"
    ),
) -> None:
    """Resolve an arbitrary config KEY for the cwd-inferred or locator-named entity.

    Prints the winning (most-local) value, or ``(unset)`` when the key appears
    nowhere in the entity's cascade. An array prints one element per line and a
    table prints ``subkey = value`` per line (sorted); a scalar prints bare.
    Passing SUBKEY reads a single cell of the table at KEY. ``--origin`` instead
    lists every rung that carries the key, most-local first, each showing where
    the value comes from -- its definition level (``project``/``workspace``/
    ``global``) for an inherited key such as ``base_branch``, otherwise its scope
    -- and the file it lives in.

    An optional **locator** prefix may name the entity to act on (resolved
    across every declared workspace). Because SUBKEY is bare-optional and the
    locator is variable-token, ``config get A B`` (two args) is ambiguous; the
    parse uses **data-type inference**: if ``A`` resolves to a table on the
    cwd entity, ``B`` is a SUBKEY (backward-compatible); otherwise ``A`` is
    tried as a locator and ``B`` as a KEY on that entity; if neither, the
    fallback is ``KEY=A SUBKEY=B`` (prints ``(unset)``). Three or more args
    require a resolving locator prefix.
    """
    if not args:
        raise WtmError("config get requires a KEY")
    target, key, subkey = _resolve_get_target(ctx, args)
    validate_arbitrary_key(key)
    config = ctx.obj.config
    effective, chain = config.resolve_generic_value(target, key)
    if show_origin:
        if not chain:
            print("(unset)")

            return
        for rung in chain:
            print(f"{rung.value}  [{_rung_label(rung)}]  {_tilde(rung.file)}")

        return
    if subkey is not None:
        if not isinstance(effective, dict):
            print("(unset)")

            return
        cell = effective.get(subkey)
        print("(unset)" if cell is None else str(cell))

        return
    print(_render_config_value(effective))


def _resolve_get_target(
    ctx: typer.Context, args: list[str]
) -> tuple[GenericTarget, str, str | None]:
    """Parse ``config get`` args into ``(target, key, subkey)`` by data-type inference.

    The one wtm parse that depends on config content rather than count+flags
    alone: ``config get``'s SUBKEY is bare-optional, so ``A B`` is genuinely
    ambiguous (LOCATOR+KEY, or KEY+SUBKEY). The inference, most-local-first:

    - 1 arg: ``KEY=args[0]`` on the cwd entity (unchanged).
    - 2 args ``[A, B]``: if ``A`` is a table on the cwd entity, ``B`` is its
      SUBKEY (table wins, backward-compatible); else try ``A`` as a locator --
      if it resolves, ``B`` is a KEY on that entity; else fall back to
      ``KEY=A SUBKEY=B`` (prints ``(unset)``, backward-compatible).
    - 3+ args: try the SUBKEY reading (``args[:-2]`` as locator, ``args[-2]``
      as KEY, ``args[-1]`` as SUBKEY) when ``args[-2]`` is a table on the
      located entity; else try the KEY reading (``args[:-1]`` as locator,
      ``args[-1]`` as KEY); else error.
    """
    config = ctx.obj.config
    cwd_target = config.resolve_generic_target(current_dir(resolve=False))

    if len(args) == 1:
        return cwd_target, args[0], None

    if len(args) == 2:
        a, b = args
        effective, _ = config.resolve_generic_value(cwd_target, a)
        if isinstance(effective, dict):
            return cwd_target, a, b

        location = _locate_optional(ctx, [a])
        if location is not None:
            target = config.resolve_generic_target_for(
                location.workspace, location.project, location.name
            )

            return target, b, None

        return cwd_target, a, b

    # 3+ args: locator required. Try SUBKEY reading first (table wins).
    location = _locate_optional(ctx, args[:-2])
    if location is not None:
        target = config.resolve_generic_target_for(
            location.workspace, location.project, location.name
        )
        effective, _ = config.resolve_generic_value(target, args[-2])
        if isinstance(effective, dict):
            return target, args[-2], args[-1]

    # Try KEY reading.
    location = _locate_optional(ctx, args[:-1])
    if location is not None:
        target = config.resolve_generic_target_for(
            location.workspace, location.project, location.name
        )

        return target, args[-1], None

    raise WtmError(f"no entity matches: {' '.join(args[:-1])}")


def _render_config_value(value: object) -> str:
    """Render a resolved config value by kind for ``config get``.

    A scalar prints bare, an array one element per line, a table ``subkey =
    value`` per line (sorted); an unset value prints ``(unset)``.
    """
    if value is None:
        return "(unset)"
    if isinstance(value, dict):
        return "\n".join(f"{k} = {value[k]}" for k in sorted(value))
    if isinstance(value, (list, tuple)):
        return "\n".join(str(item) for item in value)

    return str(value)


@config_app.command(name="unset")
def config_unset(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        ...,
        metavar="[LOCATOR] KEY [SUBKEY] [VALUE]",
        help="Optional locator prefix, then key, plus SUBKEY for -t or VALUE for -a",
        autocompletion=_complete_config_unset,
    ),
    scope: Scope | None = typer.Option(
        None, "-s", "--scope", help=_GENERIC_SCOPE_HELP, autocompletion=_complete_scope
    ),
    array: bool = typer.Option(
        False, "-a", "--array", help="Remove every occurrence of VALUE from the array at KEY"
    ),
    table: bool = typer.Option(False, "-t", "--table", help="Remove SUBKEY from the table at KEY"),
) -> None:
    """Remove an arbitrary config KEY from one tier of the cwd-inferred or locator-named entity.

    Mirrors :func:`config_set`: the entity is cwd-inferred (or locator-named
    when an optional locator prefix precedes the key group) and ``--scope``
    picks the file (defaulting to the deepest existing dir). The removal is
    scalar by default (``KEY``); ``-a``/``--array`` (``KEY VALUE``) strips every
    occurrence of VALUE from the array at KEY; ``-t``/``--table`` (``KEY
    SUBKEY``) removes SUBKEY from the table at KEY. The two flags are mutually
    exclusive. A no-op when the target is already absent at that tier.

    The locator prefix is ``args[:-N]`` (N flag-decided: 2 for ``-a``/``-t``,
    else 1); it is absent when ``len(args) == N``.
    """
    if array and table:
        raise WtmError("-a/--array and -t/--table are mutually exclusive")

    n = 2 if (array or table) else 1
    if len(args) < n:
        raise WtmError(
            f"config unset {'-a ' if array else '-t ' if table else ''}requires "
            f"{'KEY VALUE' if array else 'KEY SUBKEY' if table else 'KEY'}"
        )
    loc_args = args[:-n]
    if array:
        key, value = args[-2:]
    elif table:
        key, subkey = args[-2:]
    else:
        (key,) = args[-1:]

    if not (key == "inheritable" and array):
        validate_arbitrary_key(key)
    config = ctx.obj.config
    if loc_args:
        location = _locate_one(ctx, loc_args, command="config unset")
        target = config.resolve_generic_target_for(
            location.workspace, location.project, location.name
        )
    else:
        target = config.resolve_generic_target(current_dir(resolve=False))
    scope = _generic_scope(target, scope)
    address, prefix = target.write_target(scope)
    full_key = (*prefix, key) if prefix else key
    if array:
        config.remove_from_array(address, full_key, value)
        log(f"{target.label}: {key} -= {value} [{scope.value}] ({address.file})")

        return
    if table:
        config.unset_table_value(address, full_key, subkey)
        log(f"{target.label}: unset {key}.{subkey} [{scope.value}] ({address.file})")

        return
    config.unset_value(address, full_key)
    log(f"{target.label}: unset {key} [{scope.value}] ({address.file})")
