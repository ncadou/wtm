"""Config commands (show, base-branch, get, set, unset) and their render helpers."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import typer

from wtm.cli.common import _GENERIC_SCOPE_HELP, _generic_scope, _rung_label
from wtm.cli.completions.config import (
    _complete_config_get,
    _complete_config_set,
    _complete_config_show,
    _complete_config_unset,
    _complete_floor,
    _complete_scope,
    _complete_type,
)
from wtm.cli.locator import _locate_one, _locate_optional
from wtm.config import (
    TYPED_VALUE_FORMATS,
    ConfigSlice,
    DepthTier,
    GenericTarget,
    Key,
    ResolvedProject,
    ResolvedWorkspace,
    ResolvedWorktree,
    SliceTarget,
    SourceRung,
    TomlValue,
    check_key_floor,
    json_leaf_default,
    parse_inheritable_element,
    parse_toml_value,
    parse_typed_value,
    render_scalar,
    validate_arbitrary_key_path,
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
    ``-d TIER`` caps the deepest tier rendered. The text view shows each value
    at its defining tier only -- an inherited value is suppressed at the
    descendant and appears once, at its definition site on the same branch (no
    value is lost; the defining ancestor is always rendered above). ``--json``
    instead emits the full effective merged values (machine-readable, no
    provenance). ``--origin`` annotates each shown value with its source;
    ``--hooks`` renders accumulated inline hooks. Both work in all modes.
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


def _defined_here(tier: str | None, entity_tier: str) -> bool:
    """True when a value is defined at *entity_tier* (not inherited from an ancestor).

    A rung's ``tier`` is ``None`` for a plain (entity-local) key whose scope
    already names the source, or the inheritance level (``project`` /
    ``workspace`` / ``global``) it came from for an inheritable key. The value
    is shown only when that tier matches the entity being rendered -- so an
    inherited value is suppressed at the descendant and appears once, at its
    defining tier on the same global-rooted branch.
    """
    return tier is None or tier == entity_tier


def _print_extra(
    indent: str,
    extra: dict[str, object],
    origins: dict[str, list[SourceRung]],
    entity_tier: str,
    show_origin: bool,
) -> None:
    """Render an entity's arbitrary keys, showing each value where it is defined.

    A scalar prints as ``key: value``; a table (a section) prints as a ``key:``
    header followed by its cells indented (recursively for nested sections), so a
    deep-merged section reads as a tree rather than a Python dict repr. With
    *show_origin* (the ``--origin`` path), each value is annotated ``[label]``
    with its defining tier/scope -- per-cell for a table (each leaf its own
    carrier), mirroring ``config get --origin`` but in the ``config show`` colon
    style.

    Inherited values are suppressed: a key (or, for a table, a cell) is shown
    only when its most-local carrying rung is defined at *entity_tier* (see
    :func:`_defined_here`). The defining ancestor is always on the rendered
    global-rooted branch, so no value is lost -- it appears once, at its
    definition site. A table whose every cell is inherited is omitted entirely
    (header included), so no orphan empty-section header is left behind.
    """
    for key, value in sorted(extra.items()):
        chain = origins.get(key, [])
        if isinstance(value, dict):
            lines = _print_extra_table(f"{indent}  ", value, chain, (), entity_tier, show_origin)
            if lines:
                print(f"{indent}{key}:")
                print("\n".join(lines))
        else:
            if chain and not _defined_here(chain[0].tier, entity_tier):
                continue
            origin = f"  [{_rung_label(chain[0])}]" if show_origin and chain else ""
            print(f"{indent}{key}: {render_scalar(value)}{origin}")


def _print_extra_table(
    indent: str,
    table: dict[str, object],
    chain: list[SourceRung],
    rest: tuple[str, ...],
    entity_tier: str,
    show_origin: bool,
) -> list[str]:
    """Render one section's cells, returning the lines that survive suppression.

    Each leaf cell is kept only when its most-local carrying rung (found by
    descending *rest* + the cell's key into each rung's value) is defined at
    *entity_tier*; an inherited cell is dropped. A nested sub-table is emitted
    with its header only when at least one of its own cells survives, so no
    orphan empty-section header remains. With *show_origin*, each surviving leaf
    is annotated ``[label]`` with its carrying rung.
    """
    lines: list[str] = []
    for key in sorted(table):
        value = table[key]
        sub = (*rest, key)
        if isinstance(value, dict):
            child_lines = _print_extra_table(
                f"{indent}  ", value, chain, sub, entity_tier, show_origin
            )
            if child_lines:
                lines.append(f"{indent}{key}:")
                lines.extend(child_lines)
        else:
            winner: SourceRung | None = None
            for rung in chain:
                if _descend_path(rung.value, sub) is not None:
                    winner = rung

                    break
            if winner is not None and not _defined_here(winner.tier, entity_tier):
                continue
            origin = f"  [{_rung_label(winner)}]" if show_origin and winner is not None else ""
            lines.append(f"{indent}{key}: {render_scalar(value)}{origin}")

    return lines


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
    each shown value's source and ``--hooks`` rendering accumulated inline hooks
    at every tier. Both flags are ungated (work in all modes, per Q6). Inherited
    values are suppressed at descendants: each value is rendered only at its
    defining tier, so the text view shows where things are defined rather than
    echoing the effective (inherited) value at every descendant.
    """
    if slice.default_base_branch:
        origin = (
            f"  [{slice.base_branch_source}]" if show_origin and slice.base_branch_source else ""
        )
        print(f"default base_branch: {slice.default_base_branch}{origin}")
    _print_extra("", slice.global_extra, slice.global_extra_origins, "global", show_origin)
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
    _print_extra(f"{indent}  ", ws.entry.extra, ws.extra_origins, "workspace", show_origin)
    if hooks:
        _print_scope_hooks(f"{indent}  ", ws.entry.hooks, show_origin, "workspace")
    for project in ws.projects:
        _print_slice_project(project, f"{indent}  ", show_origin, hooks)


def _print_slice_project(
    project: ResolvedProject, indent: str, show_origin: bool, hooks: bool
) -> None:
    """Render one project node with origin/hooks, descending its pruned worktrees.

    ``base_branch`` is shown only when defined at the project tier (not
    inherited from the workspace or global default); the defining ancestor
    carries it above. Inherited arbitrary keys are likewise suppressed (see
    :func:`_print_extra`).
    """
    inert = "" if project.exists else "  (no .git on disk)"
    print(f"{indent}project {project.name}: {project.dir}{inert}")
    if project.description:
        print(f"{indent}  description: {project.description}")
    if project.effective_base_branch and project.base_branch_source == "project":
        origin = f"  [{project.base_branch_source}]" if show_origin else ""
        print(f"{indent}  base_branch: {project.effective_base_branch}{origin}")
    _print_extra(f"{indent}  ", project.extra, project.extra_origins, "project", show_origin)
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
    _print_extra(f"{indent}  ", worktree.extra, worktree.extra_origins, "worktree", show_origin)
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


# --- arbitrary-key surface (cwd-inferred entity) -------------------------------


@config_app.command(name="set")
def config_set(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        ...,
        metavar="[LOCATOR] KEY [SUBKEY] VALUE",
        help="Optional locator prefix, then key (plus SUBKEY for -t) and value",
        autocompletion=_complete_config_set,
    ),
    global_scope: bool = typer.Option(
        False,
        "-g",
        "--global",
        help="Target the global tier (top-level keys of the user/system file); "
        "mutually exclusive with a locator",
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
    floor: str | None = typer.Option(
        None,
        "-f",
        "--floor",
        autocompletion=_complete_floor,
        help="With -a on KEY 'inheritable': declare the element with a tier floor "
        "(worktree/project/workspace/global)",
    ),
    toml_literal: bool = typer.Option(
        False,
        "-x",
        "--toml",
        help="Parse VALUE as a TOML literal (scalars only: strings, ints, floats, "
        "booleans, date/times)",
    ),
    value_type: str | None = typer.Option(
        None,
        "--type",
        autocompletion=_complete_type,
        help="Coerce VALUE to a named scalar type: " + ", ".join(TYPED_VALUE_FORMATS),
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

    ``-g``/``--global`` instead targets the global tier -- the top level of the
    user file (``-s system``: the system file), with no structural prefix, so
    the key is a true global default rather than a ``[workspaces...]``-nested
    annotation. It is mutually exclusive with a locator; a global-tier key
    stays entity-local until opted into ``inheritable`` (see the array form).

    The write is scalar by default (``KEY VALUE``), stored as a **string**
    unless typed opt-in is requested: ``--toml`` parses VALUE as a TOML value
    literal (every scalar spelling, including dates; tables/arrays are not
    writable yet) and ``--type NAME`` coerces it to a named scalar (bool uses
    click's truthiness tokens). The two are mutually exclusive; both also work
    element-wise. ``-a``/``--array`` (``KEY VALUE``) adds VALUE to the string
    array at KEY, idempotently (set semantics); ``-t``/``--table`` (``KEY
    SUBKEY VALUE``) sets SUBKEY in the table at KEY. Both mutate element-wise
    under a per-file lock. The one reserved array a user may mutate via
    ``-a`` is ``inheritable`` (the opt-in allowlist of extra inheritable
    keys); its elements are key *declarations*, so ``--type``/``--toml`` are
    refused there. Each inheritable element is validated (a reserved or
    hardcoded-non-inheritable key such as ``description`` is refused). With
    ``--floor``, an ``inheritable`` element is stored as ``{ key = VALUE,
    floor = FLOOR }``: the key then only participates at FLOOR and above
    (worktree < project < workspace < global), so deeper values neither
    resolve nor write -- the same rule the builtin ``base_branch`` (floor
    ``project``) always had.

    A floored inheritable key refuses any write landing below its floor
    (`WriteProhibitedError`); removing one is never refused.

    KEY may be a dotted path (``ci.runner.image``) naming a cell inside a
    section; ``-t`` is the one-level shorthand for the same thing (``ci.runner
    image x -t`` equals ``ci.runner.image x``), and a bare KEY keeps its
    top-level meaning. Only the head segment of a dotted path is checked
    against the reserved names -- deeper segments are harmless cells.

    The locator prefix is ``args[:-N]`` (N flag-decided: 3 for ``-t``, else 2);
    it is absent when ``len(args) == N``. ``config set KEY VALUE`` (cwd-inferred)
    is unchanged.
    """
    if array and table:
        raise WtmError("-a/--array and -t/--table are mutually exclusive")
    if toml_literal and value_type is not None:
        raise WtmError("--type and --toml are mutually exclusive")
    if floor is not None and not array:
        raise WtmError("--floor requires -a/--array")

    n = 3 if table else 2
    if len(args) < n:
        raise WtmError(
            f"config set {'-t ' if table else '-a ' if array else ''}requires "
            f"{'KEY SUBKEY VALUE' if table else 'KEY VALUE'}"
        )
    loc_args = args[:-n]
    if global_scope and loc_args:
        raise WtmError("-g/--global cannot be combined with a locator")
    if table:
        key, subkey, value = args[-3:]
    else:
        key, value = args[-2:]

    element: str | dict[str, str] = value
    if (toml_literal or value_type is not None) and array and key == "inheritable":
        raise WtmError("inheritable elements are key declarations; --type/--toml do not apply")
    if key == "inheritable" and array:
        if floor is not None:
            element = {"key": value, "floor": floor}
        parse_inheritable_element(element)
    elif floor is not None:
        raise WtmError("--floor applies only to KEY 'inheritable' with -a/--array")
    else:
        validate_arbitrary_key_path(key)

    # Coerce the raw VALUE once, after validation; the bare form stays a string.
    coerced: TomlValue = value
    if toml_literal:
        coerced = parse_toml_value(value)
    elif value_type is not None:
        if value_type not in TYPED_VALUE_FORMATS:
            raise WtmError(
                f"unknown --type '{value_type}' (choose one of {', '.join(TYPED_VALUE_FORMATS)})"
            )
        coerced = parse_typed_value(value, value_type)

    config = ctx.obj.config
    if global_scope:
        target = config.global_generic_target()
    elif loc_args:
        location = _locate_one(ctx, loc_args, command="config set")
        target = config.resolve_generic_target_for(
            location.workspace, location.project, location.name
        )
    else:
        target = config.resolve_generic_target(current_dir(resolve=False))
    scope = _generic_scope(target, scope)
    address, prefix = target.write_target(scope)
    full_key = _join_key(prefix, key)
    head = key.split(".", 1)[0]
    check_key_floor(target.floors.get(head), head, scope, (*prefix, *key.split(".")))
    if array:
        written: TomlValue | dict[str, str] = coerced if key != "inheritable" else element
        config.add_to_array(address, full_key, written)
        log(f"{target.label}: {key} += {value} [{scope.value}] ({address.file})")

        return
    if table:
        config.set_table_value(address, full_key, subkey, coerced)
        log(f"{target.label}: {key}.{subkey} = {value} [{scope.value}] ({address.file})")

        return
    config.set_value(address, full_key, coerced)
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
    global_scope: bool = typer.Option(
        False,
        "-g",
        "--global",
        help="Resolve against the global tier (top-level keys of the user/system "
        "file); mutually exclusive with a locator or SUBKEY",
    ),
    show_origin: bool = typer.Option(
        False, "-o", "--origin", help="Print the full probe chain, most-local first"
    ),
    as_json: bool = typer.Option(
        False,
        "-j",
        "--json",
        help="Emit the effective value as bare JSON instead of text "
        "(null when unset; mutually exclusive with --origin)",
    ),
) -> None:
    """Resolve an arbitrary config KEY for the cwd-inferred or locator-named entity.

    Prints the winning (most-local) value, or ``(unset)`` when the key appears
    nowhere in the entity's cascade. An array prints one element per line and a
    table prints ``subkey = value`` per line (sorted, nested sub-tables
    indented); a scalar prints bare. Non-string scalars render TOML-faithfully
    (booleans lower-case, dates ISO-8601). Passing SUBKEY reads a single cell
    of the table at KEY. ``--origin`` instead lists where each value is
    defined: per-cell for a table (each leaf ``path = value`` annotated with
    its source) or one line per carrying rung for a scalar/array -- the
    definition level (``project``/``workspace``/``global``) for an inherited
    key such as ``base_branch``, otherwise its scope -- and the file it lives
    in. ``--json`` emits the effective value as bare JSON (``8080``, ``true``,
    ``[1, 2]``, a nested object; ``null`` when unset), machine-readable with
    no provenance; it cannot be combined with ``--origin``.

    With ``-g``/``--global`` the KEY resolves against the global tier alone --
    the top level of the user file (the system file is not probed), exactly one
    positional, no locator and no SUBKEY; a dotted KEY still reads a nested
    cell.

    KEY may be a dotted path (``ci.runner.image``) naming a nested cell; a
    dotted key is never a locator (entity names have no dots), so the 2-arg
    ``A B`` ambiguity only arises when ``A`` is bare. An optional **locator**
    prefix may name the entity to act on (resolved across every declared
    workspace). Because SUBKEY is bare-optional and the locator is
    variable-token, ``config get A B`` (two bare args) is ambiguous; the parse
    uses **data-type inference**: if ``A`` resolves to a table on the cwd
    entity, ``B`` is a SUBKEY (backward-compatible); otherwise ``A`` is tried
    as a locator and ``B`` as a KEY on that entity; if neither, the fallback is
    ``KEY=A SUBKEY=B`` (prints ``(unset)``). Three or more args require a
    resolving locator prefix.
    """
    if not args:
        raise WtmError("config get requires a KEY")
    if global_scope:
        if len(args) != 1:
            raise WtmError("-g/--global takes exactly a KEY")
        target = ctx.obj.config.global_generic_target()
        path = _key_path(args[0])
    else:
        target, path = _resolve_get_target(ctx, args)
    validate_arbitrary_key_path(path)
    config = ctx.obj.config
    if as_json and show_origin:
        raise WtmError("--json cannot be combined with --origin")
    effective, chain = config.resolve_generic_value(target, path)
    if show_origin:
        _print_origin(effective, chain, path)

        return
    if as_json:
        print(json.dumps(effective, default=json_leaf_default))

        return
    print(_render_config_value(effective))


def _resolve_get_target(ctx: typer.Context, args: list[str]) -> tuple[GenericTarget, Key]:
    """Parse ``config get`` args into ``(target, path)`` by data-type inference.

    The one wtm parse that depends on config content rather than count+flags
    alone: ``config get``'s SUBKEY is bare-optional, so ``A B`` is genuinely
    ambiguous (LOCATOR+KEY, or KEY+SUBKEY). *path* is the resolved dotted path
    (a bare string for a single segment, a tuple for a nested cell). The
    inference, most-local-first:

    - 1 arg: ``KEY=args[0]`` on the cwd entity (a dotted KEY splits into a
      tuple path).
    - 2 args ``[A, B]``: a dotted ``A`` is never a locator, so ``B`` is its
      SUBKEY (``A.B`` path). A bare ``A`` that is a table on the cwd entity
      takes ``B`` as its SUBKEY (table wins, backward-compatible); else ``A``
      is tried as a locator -- if it resolves, ``B`` is a KEY on that entity;
      else fall back to ``KEY=A SUBKEY=B`` (prints ``(unset)``,
      backward-compatible).
    - 3+ args: try the SUBKEY reading (``args[:-2]`` as locator, ``args[-2]``
      as KEY, ``args[-1]`` as SUBKEY) when ``args[-2]`` is a table on the
      located entity; else try the KEY reading (``args[:-1]`` as locator,
      ``args[-1]`` as KEY); else error.
    """
    config = ctx.obj.config
    cwd_target = config.resolve_generic_target(current_dir(resolve=False))

    if len(args) == 1:
        return cwd_target, _key_path(args[0])

    if len(args) == 2:
        a, b = args
        if "." in a:
            return cwd_target, _key_path(a, b)

        effective, _ = config.resolve_generic_value(cwd_target, a)
        if isinstance(effective, dict):
            return cwd_target, _key_path(a, b)

        location = _locate_optional(ctx, [a])
        if location is not None:
            target = config.resolve_generic_target_for(
                location.workspace, location.project, location.name
            )

            return target, _key_path(b)

        return cwd_target, _key_path(a, b)

    # 3+ args: locator required. Try SUBKEY reading first (table wins).
    location = _locate_optional(ctx, args[:-2])
    if location is not None:
        target = config.resolve_generic_target_for(
            location.workspace, location.project, location.name
        )
        effective, _ = config.resolve_generic_value(target, _key_path(args[-2]))
        if isinstance(effective, dict):
            return target, _key_path(args[-2], args[-1])

    # Try KEY reading.
    location = _locate_optional(ctx, args[:-1])
    if location is not None:
        target = config.resolve_generic_target_for(
            location.workspace, location.project, location.name
        )

        return target, _key_path(args[-1])

    raise WtmError(f"no entity matches: {' '.join(args[:-1])}")


def _key_path(key: str, subkey: str | None = None) -> Key:
    """Build a dotted path from a (possibly dotted) *key* and optional *subkey*."""
    parts = [*key.split("."), *([subkey] if subkey is not None else [])]

    return parts[0] if len(parts) == 1 else tuple(parts)


def _print_origin(effective: object, chain: list[SourceRung], path: Key) -> None:
    """Render ``config get --origin``: per-cell for a table, per-rung for a scalar.

    For a scalar/array effective value, each carrying rung (most-local first) is
    printed with its value, source label, and file -- descending the path past
    the head to reach the cell. For a table, every leaf cell is printed as
    ``full.path = value`` annotated with the most-local rung that defines it
    (deep-merge provenance), so a section merged across tiers shows each cell's
    own origin. ``(unset)`` when the chain is empty.
    """
    if not chain:
        print("(unset)")

        return
    parts = (path,) if isinstance(path, str) else path
    head, rest = parts[0], parts[1:]
    if isinstance(effective, dict):
        for sub, value, winner in _origin_leaves(effective, chain, rest):
            full = ".".join((head, *sub))
            label = f"  [{_rung_label(winner)}]  {_tilde(winner.file)}" if winner else ""
            print(f"{full} = {render_scalar(value)}{label}")

        return
    found = False
    for rung in chain:
        cell = _descend_path(rung.value, rest)
        if cell is not None:
            print(f"{render_scalar(cell)}  [{_rung_label(rung)}]  {_tilde(rung.file)}")
            found = True
    if not found:
        print("(unset)")


def _origin_leaves(
    table: dict[str, object], chain: list[SourceRung], rest: tuple[str, ...]
) -> Iterator[tuple[tuple[str, ...], object, SourceRung | None]]:
    """Yield each leaf cell of *table* with its sub-path and defining rung."""
    for key in sorted(table):
        value = table[key]
        sub = (*rest, key)
        if isinstance(value, dict):
            yield from _origin_leaves(value, chain, sub)
        else:
            winner: SourceRung | None = None
            for rung in chain:
                if _descend_path(rung.value, sub) is not None:
                    winner = rung

                    break
            yield sub, value, winner


def _descend_path(value: object, parts: tuple[str, ...]) -> object:
    """Walk *parts* inside *value*, returning the cell or ``None`` when absent."""
    node: object = value
    for part in parts:
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None

    return node


def _render_config_value(value: object) -> str:
    """Render a resolved config value by kind for ``config get``.

    A scalar prints bare (non-strings TOML-faithfully: booleans lower-case,
    dates ISO-8601, via :func:`~wtm.config.values.render_scalar`), an array
    one element per line, a table ``subkey = value`` per line (sorted); an
    unset value prints ``(unset)``. A nested table indents its sub-tables, so
    a section renders readably at any depth.
    """
    if value is None:
        return "(unset)"
    if isinstance(value, dict):
        return _render_table(value, "")
    if isinstance(value, (list, tuple)):
        return "\n".join(render_scalar(item) for item in value)

    return render_scalar(value)


def _render_table(table: dict[str, object], indent: str) -> str:
    """Render a (possibly nested) table as ``key = value`` / indented sub-tables."""
    lines: list[str] = []
    for key in sorted(table):
        value = table[key]
        if isinstance(value, dict):
            lines.append(f"{indent}{key}:")
            lines.append(_render_table(value, f"{indent}  "))
        else:
            lines.append(f"{indent}{key} = {render_scalar(value)}")

    return "\n".join(lines)


def _join_key(prefix: tuple[str, ...], key: str) -> Key:
    """Build a store key from a structural *prefix* and a (possibly dotted) *key*.

    A dotted *key* (``ci.runner.image``) splits into its section path; the
    *prefix* (the entity's table path, e.g. ``("workspaces", "ws")``) is prepended
    verbatim. A single-part result stays a bare string (the store treats both
    shapes via :func:`~wtm.config._key_parts`).
    """
    parts = (*prefix, *key.split("."))

    return parts[0] if len(parts) == 1 else parts


@config_app.command(name="unset")
def config_unset(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        ...,
        metavar="[LOCATOR] KEY [SUBKEY] [VALUE]",
        help="Optional locator prefix, then key, plus SUBKEY for -t or VALUE for -a",
        autocompletion=_complete_config_unset,
    ),
    global_scope: bool = typer.Option(
        False,
        "-g",
        "--global",
        help="Target the global tier (top-level keys of the user/system file); "
        "mutually exclusive with a locator",
    ),
    scope: Scope | None = typer.Option(
        None, "-s", "--scope", help=_GENERIC_SCOPE_HELP, autocompletion=_complete_scope
    ),
    array: bool = typer.Option(
        False, "-a", "--array", help="Remove every occurrence of VALUE from the array at KEY"
    ),
    table: bool = typer.Option(False, "-t", "--table", help="Remove SUBKEY from the table at KEY"),
    toml_literal: bool = typer.Option(
        False,
        "-x",
        "--toml",
        help="With -a: parse VALUE as a TOML literal (scalars only) so a typed "
        "element is removed by its own kind of value",
    ),
    value_type: str | None = typer.Option(
        None,
        "--type",
        autocompletion=_complete_type,
        help="With -a: coerce VALUE to a named scalar type before matching -- "
        + ", ".join(TYPED_VALUE_FORMATS),
    ),
) -> None:
    """Remove an arbitrary config KEY from one tier of the cwd-inferred or locator-named entity.

    Mirrors :func:`config_set`: the entity is cwd-inferred (or locator-named
    when an optional locator prefix precedes the key group) and ``--scope``
    picks the file (defaulting to the deepest existing dir). The removal is
    scalar by default (``KEY``); ``-a``/``--array`` (``KEY VALUE``) strips every
    occurrence of VALUE from the array at KEY, matching by native equality --
    with ``--type``/``--toml`` a typed element is removed by its own kind of
    value; ``-t``/``--table`` (``KEY SUBKEY``) removes SUBKEY from the table at
    KEY. The two flags are mutually exclusive. A no-op when the target is
    already absent at that tier.

    ``-g``/``--global`` targets the global tier instead -- the top level of the
    user file (``-s system``: the system file), mutually exclusive with a
    locator.

    KEY may be a dotted path (``ci.runner.image``); see :func:`config_set` for
    the head-segment reservation rule. The locator prefix is ``args[:-N]`` (N
    flag-decided: 2 for ``-a``/``-t``, else 1); it is absent when ``len(args) ==
    N``.
    """
    if array and table:
        raise WtmError("-a/--array and -t/--table are mutually exclusive")
    if (toml_literal or value_type is not None) and not array:
        raise WtmError("--type/--toml require -a/--array (nothing to match otherwise)")
    if toml_literal and value_type is not None:
        raise WtmError("--type and --toml are mutually exclusive")

    n = 2 if (array or table) else 1
    if len(args) < n:
        raise WtmError(
            f"config unset {'-a ' if array else '-t ' if table else ''}requires "
            f"{'KEY VALUE' if array else 'KEY SUBKEY' if table else 'KEY'}"
        )
    loc_args = args[:-n]
    if global_scope and loc_args:
        raise WtmError("-g/--global cannot be combined with a locator")
    if array:
        key, value = args[-2:]
    elif table:
        key, subkey = args[-2:]
    else:
        (key,) = args[-1:]

    if key == "inheritable" and array and (toml_literal or value_type is not None):
        raise WtmError("inheritable elements are key declarations; --type/--toml do not apply")
    coerced: TomlValue | None = None
    if toml_literal:
        coerced = parse_toml_value(value)
    elif value_type is not None:
        if value_type not in TYPED_VALUE_FORMATS:
            raise WtmError(
                f"unknown --type '{value_type}' (choose one of {', '.join(TYPED_VALUE_FORMATS)})"
            )
        coerced = parse_typed_value(value, value_type)
    if not (key == "inheritable" and array):
        validate_arbitrary_key_path(key)
    config = ctx.obj.config
    if global_scope:
        target = config.global_generic_target()
    elif loc_args:
        location = _locate_one(ctx, loc_args, command="config unset")
        target = config.resolve_generic_target_for(
            location.workspace, location.project, location.name
        )
    else:
        target = config.resolve_generic_target(current_dir(resolve=False))
    scope = _generic_scope(target, scope)
    address, prefix = target.write_target(scope)
    full_key = _join_key(prefix, key)
    if array:
        config.remove_from_array(address, full_key, coerced if coerced is not None else value)
        log(f"{target.label}: {key} -= {value} [{scope.value}] ({address.file})")

        return
    if table:
        config.unset_table_value(address, full_key, subkey)
        log(f"{target.label}: unset {key}.{subkey} [{scope.value}] ({address.file})")

        return
    config.unset_value(address, full_key)
    log(f"{target.label}: unset {key} [{scope.value}] ({address.file})")
