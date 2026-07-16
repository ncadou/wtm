"""wtm command-line interface (typer).

A thin typer adapter: each command parses arguments and forwards to the
CLI-agnostic operations in :mod:`wtm.project` / :mod:`wtm.worktree`. All
diagnostics go to stderr; stdout carries only path output (``resolve`` and the
``cd`` fallback) so it stays clean for command substitution.
"""

from __future__ import annotations

__all__ = ["app", "main"]

import sys
from dataclasses import dataclass, replace

import typer

from wtm import git
from wtm.config import Config, resolve_project_dir
from wtm.core import (
    WtmError,
    current_dir,
    remap_dir,
    rescue_dir,
    worktree_name,
)
from wtm.hooks import EVENTS, Hooks, Scope
from wtm.migrate import CreateMethod, Migrate, MigratePlan, MigrateResult
from wtm.output import (
    emit_env_line,
    emit_shell,
    log,
    tree_children_lines,
    warn,
    worktree_flat_all_json,
    worktree_flat_all_lines,
    worktree_flat_json,
    worktree_flat_lines,
    worktree_list_lines,
)
from wtm.project import Project
from wtm.target import TargetMode, TokenKind, slot_kind
from wtm.workspace import Workspaces
from wtm.worktree import BranchStrategy, Worktree, WorktreeCreated, WorktreeRenamed


@dataclass
class _Services:
    """Config-bound operation objects, built once per process."""

    config: Config
    project: Project
    worktree: Worktree
    workspaces: Workspaces
    migrate: Migrate
    hooks: Hooks


_LAYOUT = """\
Manage git worktrees over one or more named workspaces (projects roots):

\b
  <workspace>/<project>/
      .git/                 bare repository
      worktrees/<name>/     worktrees

Workspaces are declared in config; projects are discovered on disk and only
annotated. The active workspace is picked by -w/--workspace, $WTM_WORKSPACE, the
cwd, a default_workspace, or the sole declared one. Anything project-specific
(shared data/tools dirs, direnv, sesh/tmux) is the concern of hooks, not wtm.

Worktree targeting for install/enter/leave/delete/rename/hook (within the
active workspace):
  PROJECT NAME -> <workspace>/PROJECT/worktrees/NAME
  NAME         -> <cwd-project>/worktrees/NAME (project inferred from cwd);
                  NAME may be nested (e.g. feature/x -> worktrees/feature/x)
  DIR          -> a single arg is a path only if it starts with '/', './',
                  '../' or is '.'/'..'; otherwise it is a NAME
  (no arg)     -> cwd (delete requires an explicit target)
"""

app = typer.Typer(
    help=_LAYOUT,
    no_args_is_help=True,
    add_completion=True,
    pretty_exceptions_enable=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@app.callback()
def _bootstrap(
    ctx: typer.Context,
    workspace: str | None = typer.Option(
        None,
        "-w",
        "--workspace",
        help="Active workspace (name); overrides $WTM_WORKSPACE and cwd inference",
        autocompletion=lambda: _complete_workspace(""),
    ),
) -> None:
    """Load config once and bind the operation objects onto the context."""
    config = Config.load(workspace=workspace)
    ctx.obj = _Services(
        config=config,
        project=Project(config),
        worktree=Worktree(config),
        workspaces=Workspaces(config),
        migrate=Migrate(config),
        hooks=Hooks(config),
    )


# --- completion helpers ------------------------------------------------------
#
# Completion candidates carry a kind label (shown as "-- <label>" in zsh/fish):
# a lone token's resolution winner heads the label, any roles it shadows follow
# in "(also ...)". The precedences below are descending resolution order, a cli
# presentation concern (events are not a target TokenKind), so the labelling
# lives here rather than in wtm.target.
#
# Typer's autocompletion contract attaches the label via a (value, help) tuple;
# it does the prefix filtering and CompletionItem wrapping itself.

_TARGET_PRECEDENCE = ("worktree", "project")
_HOOK_PRECEDENCE = ("event", "worktree", "project")


def _render_label(kinds: set[str], precedence: tuple[str, ...]) -> str:
    """Render a candidate's kind set as a label: winner heads, shadows in parens.

    *kinds* is ordered by *precedence* (descending resolution order); the first
    present kind is what a lone token resolves to and heads the label, the rest
    (reachable only by typing further tokens) trail as ``(also X, Y)``.
    """
    head, *rest = [kind for kind in precedence if kind in kinds]

    return head if not rest else f"{head} (also {', '.join(rest)})"


def _labelled(
    candidates: dict[str, set[str]], precedence: tuple[str, ...]
) -> list[tuple[str, str]]:
    """Turn a name->kinds map into sorted ``(value, label)`` completion tuples.

    The single seam that attaches metadata: each name pairs with its
    :func:`_render_label`. Returning ``(value, help)`` tuples (not bare strings)
    is what makes Typer keep the help text; zsh/fish then show ``value -- help``.
    Prefix filtering is Typer's job, so it is not repeated here.
    """
    return [(name, _render_label(candidates[name], precedence)) for name in sorted(candidates)]


def _complete_workspace(incomplete: str) -> list[str]:
    """Autocompletion: declared workspace names, prefix-filtered (lenient)."""
    try:
        names = sorted(Config.load().workspaces)
    except Exception:
        return []

    return [name for name in names if name.startswith(incomplete)]


def _complete_project(incomplete: str) -> list[tuple[str, str]]:
    """Autocompletion: project names under the root, prefix-filtered.

    Runs during shell completion, where the app callback has not fired, so it
    builds its own config instead of relying on ``ctx.obj``. Any failure yields
    an empty list so completion never errors.
    """
    try:
        names = Project(Config.load()).list()
    except Exception:
        return []

    return _labelled({name: {"project"} for name in names}, _TARGET_PRECEDENCE)


def _complete_project_arg(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Complete PROJECT for the create-style ('[PROJECT] NAME') commands.

    These use the ``NAME_ONLY`` target mode: only the first slot completes (a
    PROJECT); the new NAME being invented has nothing to offer. :func:`slot_kind`
    encodes that rule so this callback no longer counts args by hand.
    """
    if TokenKind.PROJECT not in slot_kind(TargetMode.NAME_ONLY, ctx.params.get("args") or []):
        return []

    return _complete_project(incomplete)


def _complete_branch(ctx: typer.Context, incomplete: str) -> list[str]:
    """Complete -b/--branch for create/enter: the project's local + origin branches.

    The project is create's own: two positionals -> ``PROJECT NAME`` (the project
    is the first); zero or one -> the cwd-inferred project (a lone arg is the
    NAME). When ``-b`` precedes the positionals none are visible yet, so only the
    cwd project is offered. Builds its own config (the app callback has not
    fired) and is lenient (:func:`_branch_names`), so it never errors.
    """
    # Completing an option value, click leaves the positionals unparsed in
    # ``ctx.args`` (``ctx.params["args"]`` is empty then); a positional callback
    # instead finds them in ``ctx.params``. Prefer whichever is populated, then
    # apply create's grammar: two positionals -> PROJECT NAME (project is the
    # first); zero or one -> the cwd-inferred project (a lone arg is the NAME).
    args = list(ctx.params.get("args") or ctx.args or [])
    project = args[0] if len(args) >= 2 else None
    names = _branch_names(Config.load(), project)

    return [name for name in names if name.startswith(incomplete)]


def _worktree_names(config: Config, project: str | None, *, strict: bool = False) -> list[str]:
    """Return a project's in-tree worktree names, sorted, for completion/tree.

    The presentation-side projection of the single source of truth
    (:meth:`Worktree.list`): map each entry to its ``worktrees/``-relative name
    via :func:`worktree_name` and drop the out-of-tree ones (``None``). Lenient
    by default so shell completion never errors; *strict* re-raises (letting the
    tree view mark a project whose git query failed).
    """
    try:
        worktrees_dir = resolve_project_dir(config, project) / "worktrees"
    except Exception:
        if strict:
            raise

        return []

    names = [
        name
        for entry in Worktree(config).list(project, strict=strict)
        if (name := worktree_name(entry.path, worktrees_dir)) is not None
    ]

    return sorted(names)


def _branch_names(config: Config, project: str | None) -> list[str]:
    """Return a project's ``create``-targetable branch names (local + origin), for completion.

    The presentation-side wrapper of :func:`wtm.git.list_branches`. Lenient: any
    failure (unknown project, missing git dir, git error) yields ``[]`` so shell
    completion never errors. Mirrors :func:`_worktree_names`.
    """
    try:
        return git.list_branches(resolve_project_dir(config, project) / ".git")
    except Exception:
        return []


def _target_candidates(
    kinds: frozenset[TokenKind], config: Config, args: list[str]
) -> dict[str, set[str]]:
    """Map the admissible token kinds at a slot to labelled completion candidates.

    Returns a name -> kind-label set map (a name reachable as both a project and
    a worktree carries both). ``PROJECT`` -> project names (label ``project``);
    ``WORKTREE_NAME`` -> the worktree names of the project already named at slot
    0, or (at slot 0) the cwd-inferred project's (label ``worktree``).
    """
    candidates: dict[str, set[str]] = {}
    if TokenKind.PROJECT in kinds:
        for name in Project(config).list():
            candidates.setdefault(name, set()).add("project")
    if TokenKind.WORKTREE_NAME in kinds:
        project = args[0] if args else None
        for name in _worktree_names(config, project):
            candidates.setdefault(name, set()).add("worktree")

    return candidates


def _complete_target(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Complete a worktree target for the existing-target ('WORKTREE' mode) commands.

    :func:`slot_kind` decides which token kinds are admissible at this slot (a
    PROJECT and/or a worktree NAME); :func:`_target_candidates` turns them into
    labelled names. Runs during shell completion (app callback has not fired), so
    it builds its own config; :class:`Project` and :meth:`Worktree.list` are
    lenient, so this never errors.
    """
    args = ctx.params.get("args") or []
    config = Config.load()
    candidates = _target_candidates(slot_kind(TargetMode.WORKTREE, args), config, args)

    return _labelled(candidates, _TARGET_PRECEDENCE)


def _complete_event(incomplete: str) -> list[str]:
    """Autocompletion: the known hook event names, prefix-filtered."""
    return [event for event in EVENTS if event.startswith(incomplete)]


def _complete_hook_target(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Complete the '[PROJECT] NAME | [DIR] HOOK' variadic for hook run/delete.

    HOOK (the event) is the trailing token, so any slot may be it: the target
    candidates admissible at this slot (via ``WORKTREE``-mode :func:`slot_kind`)
    are merged with the event names (label ``event``). Once ``PROJECT NAME`` is
    complete (two args) only the event can follow. The event union lives here,
    not in :mod:`wtm.target`: the hook-prefix grammar is a cli concern.

    Runs during shell completion (app callback has not fired), so it builds its
    own config; :class:`Project` and :meth:`Worktree.list` are lenient, so this
    never errors.
    """
    args = ctx.params.get("args") or []
    config = Config.load()
    kinds = slot_kind(TargetMode.WORKTREE, args)
    candidates = _target_candidates(kinds, config, args)
    if len(args) <= 2:
        for event in EVENTS:
            candidates.setdefault(event, set()).add("event")

    return _labelled(candidates, _HOOK_PRECEDENCE)


project_app = typer.Typer(
    help="Project-level operations (new, clone, list, resolve).",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(project_app, name="project")


@project_app.command(name="new")
def project_new(ctx: typer.Context, name: str = typer.Argument(..., help="Project name")) -> None:
    """Create an empty project (.git/ + worktrees/)."""
    project_dir = ctx.obj.project.new(name)
    log(f"created project '{project_dir.name}' at {project_dir} (.git/ + worktrees/)")


def _clone(
    ctx: typer.Context,
    url: str = typer.Argument(..., help="Repository URL"),
    name: str | None = typer.Argument(None, help="Project name (default: from URL)"),
) -> None:
    """Clone into a new project (bare)."""
    project_dir = ctx.obj.project.clone(url, name)
    log(f"cloned '{project_dir.name}' into {project_dir}/.git")


# 'clone' is dual-homed: the canonical 'wtm project clone' plus a top-level
# 'wtm clone' shorthand. One handler, registered under both apps to avoid drift.
project_app.command(name="clone")(_clone)
app.command(name="clone")(_clone)


@project_app.command(name="list")
def project_list(
    ctx: typer.Context,
    tree: bool = typer.Option(
        False, "-t", "--tree", help="Show each project's worktrees beneath it"
    ),
    worktrees: bool = typer.Option(
        False,
        "-w",
        "--worktrees",
        help="Flatten to one worktree per line across all projects (tab-separated)",
    ),
    as_json: bool = typer.Option(False, "-j", "--json", help="Emit --worktrees rows as JSON"),
    all_ws: bool = typer.Option(
        False, "-a", "--all", help="Span every declared workspace (adds a workspace column)"
    ),
) -> None:
    """List projects in the active workspace (``--all`` spans every workspace).

    Default: one project name per line. ``--tree`` nests each project's
    worktrees beneath it. ``--worktrees`` instead flattens to one worktree per
    line across every project as tab-separated ``project name path branch
    head`` (branch empty when detached; head is a short sha), with ``--json``
    for the same records as a JSON array. ``--all`` iterates every declared
    workspace, grouping the plain/tree output under a per-workspace header and
    prepending a ``workspace`` column to the flat/JSON rows. The listing carries
    only worktree facts — session naming and path rules are a consumer's concern.
    """
    if tree and worktrees:
        raise WtmError("--tree and --worktrees are mutually exclusive")
    if as_json and not worktrees:
        raise WtmError("--json requires --worktrees")

    config = ctx.obj.config
    names = sorted(config.workspaces) if all_ws else [config.active_workspace.name]

    if worktrees:
        _list_worktrees(config, names, with_workspace=all_ws, as_json=as_json)

        return

    for ws_name in names:
        wcfg = replace(config, active=ws_name)
        if all_ws:
            print(f"{ws_name}:")
        for name in Project(wcfg).list():
            print(f"  {name}" if all_ws else name)
            if tree:
                _print_tree_children(wcfg, name, indent="  " if all_ws else "")


def _print_tree_children(config: Config, name: str, *, indent: str) -> None:
    """Print a project's worktree leaves (tree view), or an error marker."""
    try:
        children = _worktree_names(config, name, strict=True)
    except git.GitError:
        print(f"{indent}└── <error reading worktrees>")

        return
    for line in tree_children_lines(children):
        print(f"{indent}{line}")


def _list_worktrees(
    config: Config, names: list[str], *, with_workspace: bool, as_json: bool
) -> None:
    """Emit flat/JSON worktree rows for *names* (optionally workspace-prefixed)."""
    if with_workspace:
        rows6: list[tuple[str, str, str, str, str, str]] = []
        for ws_name in names:
            wcfg = replace(config, active=ws_name)
            for row in _flat_worktree_rows(Worktree(wcfg), wcfg, Project(wcfg).list()):
                rows6.append((ws_name, *row))
        if as_json:
            print(worktree_flat_all_json(rows6))
        else:
            for line in worktree_flat_all_lines(rows6):
                print(line)

        return

    wcfg = replace(config, active=names[0])
    rows = _flat_worktree_rows(Worktree(wcfg), wcfg, Project(wcfg).list())
    if as_json:
        print(worktree_flat_json(rows))
    else:
        for line in worktree_flat_lines(rows):
            print(line)


def _flat_worktree_rows(
    worktree: Worktree, config: Config, projects: list[str]
) -> list[tuple[str, str, str, str, str]]:
    """Collect ``(project, name, path, branch, head)`` rows for every worktree.

    The flat projection behind ``project list --worktrees``: each project's
    worktrees named relative to its ``worktrees/`` (an out-of-tree ``-d``
    destination keeps its absolute path), branch blank when detached, head a
    short sha. Rows are sorted by name within each project. A project whose git
    query fails is warned about on stderr and skipped, so a single broken repo
    never aborts the machine-readable listing.
    """
    rows: list[tuple[str, str, str, str, str]] = []
    for name in projects:
        worktrees_dir = resolve_project_dir(config, name) / "worktrees"
        try:
            entries = worktree.list(name, strict=True)
        except git.GitError:
            warn(f"skipping '{name}': could not read worktrees")
            continue
        project_rows = [
            (
                name,
                worktree_name(entry.path, worktrees_dir) or str(entry.path),
                str(entry.path),
                entry.branch or "",
                entry.head[:8],
            )
            for entry in entries
        ]
        rows.extend(sorted(project_rows, key=lambda row: row[1]))

    return rows


@project_app.command(name="resolve")
def project_resolve(
    ctx: typer.Context,
    project: str | None = typer.Argument(
        None,
        help="Project (default: inferred from cwd)",
        autocompletion=_complete_project,
    ),
    emit_env: bool = typer.Option(False, "-e", "--env", help="Emit the project WTM_* contract"),
) -> None:
    """Print a project's directory (--env: emit project-level WTM_* exports for eval).

    A read-only path query: computes the path without touching git, so it works
    for a not-yet-created project. --env emits only the project-level contract
    (no WTM_WORKTREE_DIR / WTM_BRANCH), likewise by path math.
    """
    if emit_env:
        for key, value in ctx.obj.project.resolve_env(project).items():
            print(emit_env_line(key, value))
    else:
        print(ctx.obj.project.resolve_path(project))


# --- worktree management -----------------------------------------------------
#
# The worktree-level commands are canonically 'wtm worktree <cmd>'; each is also
# kept as a top-level 'wtm <cmd>' alias. One handler per command, registered
# under both apps to avoid drift (the same dual-homing idiom used by 'clone').


worktree_app = typer.Typer(
    help=(
        "Worktree-level operations (create, install, enter, leave, delete, rename, list, resolve)."
    ),
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(worktree_app, name="worktree")


def create(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[PROJECT] NAME",
        help="Target: NAME or PROJECT NAME",
        autocompletion=_complete_project_arg,
    ),
    branch: str | None = typer.Option(
        None, "-b", "--branch", help="Branch name", autocompletion=_complete_branch
    ),
    base: str | None = typer.Option(
        None,
        "--base",
        "--off",
        help="Base ref a new branch forks from (overrides configured base_branch)",
        autocompletion=_complete_branch,
    ),
    destdir: str | None = typer.Option(None, "-d", "--dir", help="Destination dir"),
    do_cd: bool = typer.Option(False, "-c", "--cd", help="cd into the new worktree"),
    do_install: bool = typer.Option(False, "-i", "--install", help="Run install hooks too"),
    strict: bool = typer.Option(False, "--strict", help="Hook failures are fatal"),
) -> None:
    """Add a worktree (and branch) to a project."""
    result = ctx.obj.worktree.create(
        args or [],
        branch=branch,
        base=base,
        destdir=destdir,
        do_install=do_install,
        do_enter=do_cd,
        strict=strict,
    )
    log(_describe_create(result))
    if do_cd:
        dest = result.context.dir
        emit_shell(cd=dest, env=ctx.obj.worktree.env_for_dir(dest))


worktree_app.command(name="create")(create)
app.command(name="create")(create)


def _describe_create(result: WorktreeCreated) -> str:
    """Render the branch-resolution diagnostic for a create result."""
    if result.strategy is BranchStrategy.EXISTING_LOCAL:
        return f"worktree from existing local branch '{result.branch}'"
    if result.strategy is BranchStrategy.TRACKING:
        return f"worktree tracking 'origin/{result.branch}'"
    if result.strategy is BranchStrategy.NEW_BRANCH:
        return f"new branch '{result.branch}' off '{result.base}'"

    return f"new orphan branch '{result.branch}' (empty repository)"


def install(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[PROJECT] NAME | [DIR]",
        help="Worktree target",
        autocompletion=_complete_target,
    ),
    strict: bool = typer.Option(False, "--strict", help="Hook failures are fatal"),
) -> None:
    """Run install hooks for a worktree (alias for 'wtm hook run install')."""
    ctx.obj.worktree.install(args or [], strict=strict)


worktree_app.command(name="install")(install)
app.command(name="install")(install)


# --- hook management ---------------------------------------------------------


hook_app = typer.Typer(
    help="Manage wtm hooks (install, run, delete, list) across scopes.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(hook_app, name="hook")


@hook_app.command(name="install")
def hook_install(
    ctx: typer.Context,
    source: str = typer.Argument(..., metavar="FILE | DIR", help="Hook source"),
    args: list[str] = typer.Argument(
        None,
        metavar="[PROJECT] NAME | [DIR]",
        help="Worktree target (unused for --scope system/user/workspace)",
        autocompletion=_complete_target,
    ),
    scope: Scope = typer.Option(Scope.PROJECT, "-s", "--scope", help="Where to install the hook"),
    event: str | None = typer.Option(
        None,
        "-e",
        "--event",
        help="Event (create|install|enter|leave|delete); inferred from a "
        "wtm-<event> name otherwise",
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
        metavar="[PROJECT] NAME | [DIR] HOOK",
        help="Worktree target followed by the event (HOOK) to run",
        autocompletion=_complete_hook_target,
    ),
    scope: Scope | None = typer.Option(
        None, "-s", "--scope", help="Restrict to one scope (default: all)"
    ),
    strict: bool = typer.Option(False, "--strict", help="Hook failures are fatal"),
) -> None:
    """Run an event's hooks for a worktree (all scopes unless --scope given).

    HOOK (the event) is the last positional; anything before it is the target.
    """
    if not args:
        raise WtmError("hook run requires an event (HOOK) as the last argument")
    *target, hook = args
    ctx.obj.hooks.run(hook, target, scope=scope, strict=strict)


@hook_app.command(name="delete")
def hook_delete(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[PROJECT] NAME | [DIR] HOOK",
        help="Worktree target followed by the event (or event/entry) to delete "
        "(target unused for --scope system/user/workspace)",
        autocompletion=_complete_hook_target,
    ),
    scope: Scope = typer.Option(
        Scope.PROJECT, "-s", "--scope", help="Where to delete the hook from"
    ),
    recursive: bool = typer.Option(
        False, "-r", "--recursive", help="Also remove the wtm-<event>.d/ directory"
    ),
) -> None:
    """Delete a hook: an event (bare file; .d/ needs -r) or event/entry.

    HOOK is the last positional; anything before it is the worktree target.
    """
    if not args:
        raise WtmError("hook delete requires an event (HOOK) as the last argument")
    *target, hook = args
    for path in ctx.obj.hooks.delete(hook, target, scope=scope, recursive=recursive):
        log(f"removed {path}")


@hook_app.command(name="list")
def hook_list(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[PROJECT] NAME | [DIR]",
        help="Worktree target (default: cwd)",
        autocompletion=_complete_target,
    ),
    types: bool = typer.Option(False, "-t", "--types", help="Show a compact per-event summary"),
) -> None:
    """List installed hooks, grouped by event across scopes."""
    grouped = ctx.obj.hooks.list(args or [])
    for line in _render_hook_list(grouped, types=types):
        print(line)


def _render_hook_list(grouped: dict[str, list], *, types: bool) -> list[str]:
    """Render the grouped hook listing (full tree, or a compact per-event summary)."""
    lines: list[str] = []
    for event, listings in grouped.items():
        if types:
            count = sum(
                (1 if lst.file else 0) + len(lst.entries) + len(lst.inline) for lst in listings
            )
            scopes = ",".join(lst.scope.value for lst in listings) or "-"
            lines.append(f"{event}: {count} hook(s) [{scopes}]")
            continue
        if not listings:
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

    return lines


def enter(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[PROJECT] NAME | [DIR]",
        help="Worktree target",
        autocompletion=_complete_target,
    ),
    create: bool = typer.Option(
        False, "-c", "--create", help="Create the worktree if it doesn't exist"
    ),
    branch: str | None = typer.Option(
        None,
        "-b",
        "--branch",
        help="Branch for --create (default: NAME)",
        autocompletion=_complete_branch,
    ),
    base: str | None = typer.Option(
        None,
        "--base",
        "--off",
        help="Base ref a new branch forks from (with --create)",
        autocompletion=_complete_branch,
    ),
    do_install: bool = typer.Option(False, "-i", "--install", help="Run install hooks first"),
    strict: bool = typer.Option(False, "--strict", help="Hook failures are fatal"),
) -> None:
    """cd into a worktree (via the shell wrapper); -c creates it first if absent."""
    worktree = ctx.obj.worktree
    if not create:
        if branch is not None:
            raise WtmError("-b/--branch requires --create")
        if base is not None:
            raise WtmError("--base/--off requires --create")
        dest = worktree.enter(args or [], do_install=do_install, strict=strict)
        emit_shell(cd=dest, env=worktree.env_for_dir(dest))
        return

    # --create: restrict to PROJECT NAME / NAME and branch on existence.
    if worktree.target_path(args or []).exists():
        if branch is not None:
            warn(f"branch '{branch}' ignored: worktree already exists")
        dest = worktree.enter(args or [], do_install=do_install, strict=strict)
        emit_shell(cd=dest, env=worktree.env_for_dir(dest))
        return

    result = worktree.create(
        args or [],
        branch=branch,
        base=base,
        destdir=None,
        do_install=do_install,
        do_enter=True,
        strict=strict,
    )
    log(_describe_create(result))
    dest = result.context.dir
    emit_shell(cd=dest, env=worktree.env_for_dir(dest))


worktree_app.command(name="enter")(enter)
app.command(name="enter")(enter)


def leave(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[PROJECT] NAME | [DIR]",
        help="Worktree target (default: cwd)",
        autocompletion=_complete_target,
    ),
    no_cd: bool = typer.Option(
        False, "-C", "--no-cd", help="don't cd out (just run leave hooks; stay in the worktree)"
    ),
    strict: bool = typer.Option(False, "--strict", help="Hook failures are fatal"),
) -> None:
    """Run leave hooks for a worktree; cds out ($OLDPWD or project dir) unless -C.

    Ends the shell session either way: the WTM_* contract is unset even with -C
    (which only suppresses the cd-out, not the session teardown).
    """
    result = ctx.obj.worktree.leave(args or [], strict=strict)
    emit_shell(cd=None if no_cd else result.cd_dir, env={})


worktree_app.command(name="leave")(leave)
app.command(name="leave")(leave)


def delete(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="PROJECT NAME | DIR",
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


worktree_app.command(name="delete")(delete)
app.command(name="delete")(delete)


def rename(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[PROJECT] [OLDNAME] NEWNAME",
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


worktree_app.command(name="rename")(rename)
app.command(name="rename")(rename)


def _describe_rename(result: WorktreeRenamed) -> str:
    """Render the worktree-move diagnostic, naming both ends relative to worktrees/."""
    old = worktree_name(result.old_dir, result.worktrees_dir) or str(result.old_dir)
    new = worktree_name(result.new_dir, result.worktrees_dir) or str(result.new_dir)

    return f"renamed worktree '{old}' -> '{new}'"


def list_(
    ctx: typer.Context,
    project: str | None = typer.Argument(
        None,
        help="Project (default: inferred from cwd)",
        autocompletion=_complete_project,
    ),
) -> None:
    """List a project's worktrees (PROJECT explicit or inferred from cwd)."""
    config = ctx.obj.worktree.config
    worktrees_dir = resolve_project_dir(config, project) / "worktrees"
    rows: list[tuple[str, str]] = []
    for entry in ctx.obj.worktree.list(project, strict=True):
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


worktree_app.command(name="list")(list_)
app.command(name="list")(list_)


def resolve(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[[PROJECT] NAME]",
        help="Target: NAME, PROJECT NAME, or none (the cwd worktree)",
        autocompletion=_complete_target,
    ),
    emit_env: bool = typer.Option(False, "-e", "--env", help="Emit the WTM_* contract for eval"),
) -> None:
    """Print a worktree's path (--env: emit WTM_* exports for eval).

    With an explicit target it prints the computed path without touching git, so
    it works for not-yet-created targets; with no target it resolves the worktree
    the cwd is in (inspecting git). With --env it always inspects git, so the
    worktree must already exist.
    """
    if emit_env:
        for key, value in ctx.obj.worktree.resolve_env(args or []).items():
            print(emit_env_line(key, value))
    else:
        print(ctx.obj.worktree.resolve_path(args or []))


worktree_app.command(name="resolve")(resolve)
app.command(name="resolve")(resolve)


# 'migrate' is dual-homed: canonically 'wtm project migrate' (it folds a repo
# into the project layout) plus a top-level 'wtm migrate' alias. One handler,
# registered under both apps to avoid drift (as with 'clone').
def migrate(
    ctx: typer.Context,
    other: str | None = typer.Argument(
        None,
        metavar="[SOURCE|PROJECT]",
        help="The side not inferred from cwd: target PROJECT (from source) "
        "or SOURCE path (from the projects root)",
    ),
    source: str | None = typer.Option(
        None, "-s", "--source", help="Source repo path (default: inferred)"
    ),
    project: str | None = typer.Option(
        None, "-p", "--project", help="Target project name (default: inferred)"
    ),
    from_origin: bool = typer.Option(
        False, "-o", "--from-origin", help="Create target by cloning the origin URL (network)"
    ),
    init_bare: bool = typer.Option(
        False, "-i", "--init-bare", help="Create target as an empty bare repo (offline)"
    ),
    fetch: bool = typer.Option(
        False, "-f", "--fetch", help="Fetch origin afterwards (populate origin/* + HEAD)"
    ),
    force: bool = typer.Option(
        False, "--force", help="Force-push references into an existing target"
    ),
    update: bool = typer.Option(
        False,
        "-u",
        "--update",
        help="Allow migrating into an existing target (required if .git is already present)",
    ),
    no_remotes: bool = typer.Option(False, "--no-remotes", help="Skip remote migration"),
    no_refs: bool = typer.Option(
        False, "--no-refs", help="Skip pushing references to an existing target"
    ),
    no_worktree: bool = typer.Option(
        False, "--no-worktree", help="Skip recreating the working checkout"
    ),
    all_worktrees: bool = typer.Option(
        False,
        "-a",
        "--all-worktrees",
        help="Reproduce every linked worktree the source has (named after each "
        "source worktree's directory)",
    ),
    no_tags: bool = typer.Option(False, "--no-tags", help="Skip pushing tags"),
    no_ignored: bool = typer.Option(
        False, "--no-ignored", help="Drop git-ignored files from the worktree copy"
    ),
    exclude: list[str] = typer.Option([], "--exclude", help="rsync exclude pattern (repeatable)"),
    filter_file: list[str] = typer.Option(
        [], "--filter-file", help="rsync filter file to merge (repeatable)"
    ),
    name: str | None = typer.Option(
        None, "--name", help="Worktree name (required for a detached HEAD)"
    ),
    dry_run: bool = typer.Option(
        False, "-n", "--dry-run", help="Print the plan and an rsync preview; no changes"
    ),
    do_cd: bool = typer.Option(False, "-c", "--cd", help="cd into the new worktree"),
    strict: bool = typer.Option(False, "--strict", help="Hook failures are fatal"),
) -> None:
    """Fold a plain clone (source) into the bare-repo layout (target)."""
    if from_origin and init_bare:
        raise WtmError("--from-origin and --init-bare are mutually exclusive")
    if all_worktrees and name is not None:
        raise WtmError("--all-worktrees and --name are mutually exclusive")
    if all_worktrees and no_worktree:
        raise WtmError("--all-worktrees and --no-worktree are mutually exclusive")
    create_method = (
        CreateMethod.FROM_ORIGIN
        if from_origin
        else CreateMethod.INIT_BARE
        if init_bare
        else CreateMethod.FROM_DISK
    )

    migrator = ctx.obj.migrate
    resolution = migrator.resolve(
        other, source=source, project=project, all_worktrees=all_worktrees, update=update
    )
    for skipped in resolution.prunable_skipped:
        warn(f"source worktree '{skipped}' is prunable/missing; skipped")
    plan = migrator.plan(
        resolution,
        create_method=create_method,
        worktree_name=name,
        all_worktrees=all_worktrees,
        do_remotes=not no_remotes,
        do_refs=not no_refs,
        do_tags=not no_tags,
        do_fetch=fetch,
        force=force,
        include_ignored=not no_ignored,
        no_worktree=no_worktree,
    )

    if dry_run:
        _describe_plan(plan)
        for wt_name, preview in migrator.dry_run_rsync(
            plan, exclude=exclude, filter_files=filter_file
        ):
            log(f"rsync preview (worktree '{wt_name}'):")
            print(preview, end="", file=sys.stderr)
        return

    result = migrator.run(plan, exclude=exclude, filter_files=filter_file, strict=strict)
    _describe_result(result)
    if do_cd and result.cd_dir is not None:
        emit_shell(cd=result.cd_dir, env=ctx.obj.worktree.env_for_dir(result.cd_dir))
    if result.worktrees_failed:
        raise SystemExit(1)


project_app.command(name="migrate")(migrate)
app.command(name="migrate")(migrate)


def _describe_plan(plan: MigratePlan) -> None:
    """Render a migration plan to stderr (used by --dry-run)."""
    res = plan.resolution
    log(f"source:  {res.source}")
    log(f"target:  {res.project_dir} (.git bare)")
    if plan.create_method is None:
        log("target exists; references imported by push from source")
    else:
        log(f"create:  {plan.create_method.value}")
    pushes_refs = plan.do_refs and plan.create_method is not CreateMethod.FROM_DISK
    if plan.all_worktrees:
        worktree_label = f"worktrees={len(plan.worktrees)}"
    elif plan.worktrees:
        worktree_label = f"worktree={plan.worktrees[0].name}"
    else:
        worktree_label = ""
    phases = ", ".join(
        label
        for label, on in (
            ("remotes", plan.do_remotes),
            ("refs", pushes_refs),
            ("tags", pushes_refs and plan.do_tags),
            ("fetch", plan.do_fetch),
            (worktree_label, bool(worktree_label)),
        )
        if on
    )
    log(f"phases:  {phases or '(none)'}")
    if plan.all_worktrees:
        for spec in plan.worktrees:
            on = f"branch '{spec.branch}'" if spec.branch else f"detached {spec.head[:8]}"
            log(f"  worktree '{spec.name}' <- {on}")


def _describe_result(result: MigrateResult) -> None:
    """Render what a migration actually did to stderr."""
    res = result.plan.resolution
    if result.created:
        log(f"created target {res.project_dir}")
    for remote_name in result.remotes_added:
        log(f"added remote '{remote_name}'")
    for remote_name in result.remotes_skipped:
        warn(f"remote '{remote_name}' exists with a different URL; skipped")
    if result.refs_rejected:
        warn(
            "rejected (non-fast-forward; re-run with --force to overlay): "
            + ", ".join(result.refs_rejected)
        )
    if result.fetched:
        log("fetched origin (origin/* and origin/HEAD updated)")
    for wt_name, wt_dir in result.worktrees_created:
        log(f"worktree '{wt_name}' ready at {wt_dir}")
    for wt_name, reason in result.worktrees_skipped:
        warn(f"worktree '{wt_name}' skipped: {reason}")
    for wt_name, reason in result.worktrees_failed:
        warn(f"worktree '{wt_name}' failed: {reason}")


# --- workspace & config surface ----------------------------------------------


workspace_app = typer.Typer(
    help=(
        "Workspace-level operations (list, resolve, show, add, set, rename, "
        "delete, default, enter, leave)."
    ),
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(workspace_app, name="workspace")


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
    """Show a workspace's detail (path, description, base_branch, scope, project count)."""
    result = ctx.obj.workspaces.show(name)
    exists = "" if result.exists else "  (missing)"
    print(f"name: {result.name}")
    print(f"path: {result.path}{exists}")
    if result.description:
        print(f"description: {result.description}")
    if result.base_branch:
        print(f"base_branch: {result.base_branch}")
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
    """Register a workspace in the user config (PATH defaults to the cwd)."""
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
) -> None:
    """Edit an existing user-scope workspace entry in place (partial update)."""
    result = ctx.obj.workspaces.set(
        name, path=path, description=description, base_branch=base_branch
    )
    log(f"updated '{result.name}'")
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
        log(f"default_workspace updated to '{result.new}'")


@workspace_app.command(name="delete")
def workspace_delete(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Workspace name", autocompletion=_complete_workspace),
) -> None:
    """Unregister a workspace from the user config (never touches the projects root)."""
    result = ctx.obj.workspaces.delete(name)
    log(f"unregistered '{result.name}'")
    if result.cleared_default:
        log("cleared default_workspace (it pointed here)")
    if result.remains_at_system:
        warn(f"'{result.name}' is still declared at system scope; it remains resolvable")


@workspace_app.command(name="default")
def workspace_default(
    ctx: typer.Context,
    name: str | None = typer.Argument(
        None, help="Workspace to make default (omit to show)", autocompletion=_complete_workspace
    ),
    clear: bool = typer.Option(False, "--clear", help="Remove the default_workspace key"),
) -> None:
    """Get, set, or clear the default_workspace (user config)."""
    if clear:
        if name is not None:
            raise WtmError("--clear takes no NAME")
        ctx.obj.workspaces.default_clear()
        log("cleared default_workspace")

        return
    if name is None:
        current = ctx.obj.workspaces.default_show()
        print(current if current else "(none set)")

        return
    ctx.obj.workspaces.default_set(name)
    log(f"default_workspace set to '{name}'")


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


config_app = typer.Typer(
    help="Inspect the effective wtm configuration (read-only).",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(config_app, name="config")


@config_app.command(name="show")
def config_show(
    ctx: typer.Context,
    sources: bool = typer.Option(
        False, "--sources", help="Annotate where each workspace was declared"
    ),
) -> None:
    """Show the effective merged config (workspaces, defaults, annotations).

    A read-only view of the resolved configuration for debugging the layered
    merge. ``--sources`` marks the scope each workspace was declared in and
    flags inert annotations (projects with no directory on disk).
    """
    config = ctx.obj.config
    print(f"active workspace: {config.active or '(none)'}")
    if config.default_base_branch:
        print(f"default base_branch: {config.default_base_branch}")

    print("workspaces:")
    for name, ws in sorted(config.workspaces.items()):
        origin = f"  [{ws.scope.value}]" if sources else ""
        print(f"  {name}: {ws.path}{origin}")
        if ws.base_branch:
            print(f"    base_branch: {ws.base_branch}")
        for pname, ann in sorted(ws.projects.items()):
            inert = ""
            if sources and not (ws.path / pname / ".git").is_dir():
                inert = "  (no such project on disk)"
            bits = [b for b in (ann.description, ann.base_branch) if b]
            detail = f" ({', '.join(bits)})" if bits else ""
            print(f"    project {pname}{detail}{inert}")


def main() -> None:
    """Entry point: run the typer app, mapping WtmError to ``wtm: ...`` exit 1."""
    try:
        app()
    except WtmError as exc:
        print(f"wtm: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except git.GitError as exc:
        print(f"wtm: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
