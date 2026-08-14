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
from pathlib import Path

import typer
from typer.core import TyperGroup

from wtm import git
from wtm.config import (
    Config,
    ConfigTree,
    GenericTarget,
    HookSpec,
    ResolvedConfig,
    ResolvedProject,
    ResolvedWorkspace,
    ResolvedWorktree,
    SourceRung,
    Workspace,
    resolve_project_dir,
    validate_arbitrary_key,
    validate_inheritable_element,
)
from wtm.core import (
    WtmError,
    current_dir,
    project_dir_for,
    remap_dir,
    rescue_dir,
    worktree_name,
)
from wtm.hooks import EVENTS, Hooks, Scope
from wtm.importer import CreateMethod, Import, ImportPlan, ImportResult
from wtm.locate import (
    Ambiguous,
    Location,
    LocationIndex,
    NotFound,
    Tier,
    build_index,
    locate,
    narrow_by_context,
)
from wtm.output import (
    config_tree_json,
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
from wtm.target import (
    TargetMode,
    TokenKind,
    looks_like_path,
    slot_kind,
    split_target,
    split_workspace_project,
)
from wtm.workspace import Workspaces
from wtm.worktree import BranchStrategy, Worktree, WorktreeCreated, WorktreeRenamed


@dataclass
class _Services:
    """Config-bound operation objects, built once per process."""

    config: Config
    project: Project
    worktree: Worktree
    workspaces: Workspaces
    importer: Import
    hooks: Hooks


_LAYOUT = """\
Manage git worktrees over one or more named workspaces (projects roots):

\b
  <workspace>/<project>/
      .git/                 bare repository
      worktrees/<name>/     worktrees

Workspaces are declared in config; projects are discovered on disk and only
annotated. The active workspace is picked by a leading WORKSPACE positional,
$WTM_WORKSPACE, the cwd, a default_workspace, or the sole declared one. Anything
project-specific (shared data/tools dirs, direnv, sesh/tmux) is the concern of
hooks, not wtm.

Worktree targeting for create/delete/rename/resolve/hook (a leading WORKSPACE
reaches another workspace; without it the active one is used):
  WORKSPACE PROJECT NAME -> <WORKSPACE>/PROJECT/worktrees/NAME
  PROJECT NAME -> <workspace>/PROJECT/worktrees/NAME
  NAME         -> <cwd-project>/worktrees/NAME (project inferred from cwd);
                  NAME may be nested (e.g. feature/x -> worktrees/feature/x)
  DIR          -> a single arg is a path only if it starts with '/', './',
                  '../' or is '.'/'..'; otherwise it is a NAME
  (no arg)     -> cwd (delete requires an explicit target)

enter/leave/list/project enter/project leave/hook list instead take a LOCATOR: a
name matched across every declared workspace, deepest tier first (worktree >
project > workspace), leading tokens qualifying its ancestors. One match wins,
several print the qualified choices. Destructive verbs and not-yet-created names
keep the positional grammar above.
"""


class _GroupsFirstTyperGroup(TyperGroup):
    """Root group that lists its sub-groups before its bare commands.

    Typer emits every bare command before any sub-group, which (with rich
    panels) forces the "Command groups" panel below the shortcut panels.
    Surfacing groups first here flips the rich panel order so Command groups
    heads the listing, while each subset keeps its own registration order.
    """

    def list_commands(self, ctx: typer.Context) -> list[str]:
        """Return sub-group names first, then bare command names."""
        names = super().list_commands(ctx)
        groups = [name for name in names if isinstance(self.get_command(ctx, name), TyperGroup)]
        group_set = set(groups)

        return groups + [name for name in names if name not in group_set]


app = typer.Typer(
    cls=_GroupsFirstTyperGroup,
    help=_LAYOUT,
    no_args_is_help=True,
    add_completion=True,
    pretty_exceptions_enable=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@app.callback()
def _bootstrap(ctx: typer.Context) -> None:
    """Load config once and bind the operation objects onto the context."""
    config = Config.load()
    ctx.obj = _Services(
        config=config,
        project=Project(config),
        worktree=Worktree(config),
        workspaces=Workspaces(config),
        importer=Import(config),
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
# The locator (`enter`) label diverges: being global, its winner also carries the
# target's location context ("worktree in datactl") -- see _locate_label.
#
# Typer's autocompletion contract attaches the label via a (value, help) tuple;
# it does the prefix filtering and CompletionItem wrapping itself.

_TARGET_PRECEDENCE = ("worktree", "project", "workspace")
_HOOK_PRECEDENCE = ("event", "worktree", "project", "workspace")


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


def _complete_workspace_arg(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Complete the optional leading WORKSPACE for the ``[WORKSPACE] NAME`` commands.

    Used by ``project new`` and ``clone``: only slot 0 completes (a WORKSPACE);
    the invented project NAME being typed has nothing to offer, so once one
    positional is present nothing more completes.
    """
    args = ctx.params.get("args") or []
    if args:
        return []
    try:
        workspaces = sorted(Config.load().workspaces)
    except Exception:
        return []

    return _labelled({name: {"workspace"} for name in workspaces}, _TARGET_PRECEDENCE)


def _complete_project(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Autocompletion for the project-level ``[[WORKSPACE] PROJECT]`` slot.

    Slot 0 offers declared WORKSPACE names plus the active workspace's projects;
    slot 1 offers the projects of ``args[0]`` read as a WORKSPACE. Runs during
    shell completion, where the app callback has not fired, so it builds its own
    config instead of relying on ``ctx.obj``. Any failure yields an empty list
    so completion never errors.
    """
    args = ctx.params.get("args") or []
    if len(args) >= 2:
        return []
    try:
        config = Config.load()
    except Exception:
        return []

    candidates: dict[str, set[str]] = {}
    if not args:
        for name in config.workspaces:
            candidates.setdefault(name, set()).add("workspace")
    workspace = args[0] if args else None
    for name in Project(config).list(workspace):
        candidates.setdefault(name, set()).add("project")

    return _labelled(candidates, _TARGET_PRECEDENCE)


def _complete_branch(ctx: typer.Context, incomplete: str) -> list[str]:
    """Complete -b/--branch for create: the project's local + origin branches.

    The project is create's own: the ``[[WORKSPACE] PROJECT] NAME`` grammar puts
    PROJECT at ``args[1]`` when a WORKSPACE leads (three positionals) and at
    ``args[0]`` for two; zero or one positional is the cwd-inferred project (a
    lone arg is the NAME). When ``-b`` precedes the positionals none are visible
    yet, so only the cwd project is offered. Builds its own config (the app
    callback has not fired) and is lenient (:func:`_branch_names`), so it never
    errors.
    """
    # Completing an option value, click leaves the positionals unparsed in
    # ``ctx.args`` (``ctx.params["args"]`` is empty then); a positional callback
    # instead finds them in ``ctx.params``. Prefer whichever is populated.
    args = list(ctx.params.get("args") or ctx.args or [])
    if len(args) >= 3:
        workspace, project = args[0], args[1]
    elif len(args) == 2:
        workspace, project = None, args[0]
    else:
        workspace, project = None, None
    names = _branch_names(Config.load(), workspace, project)

    return [name for name in names if name.startswith(incomplete)]


def _worktree_names(
    config: Config, workspace: str | None, project: str | None, *, strict: bool = False
) -> list[str]:
    """Return a project's in-tree worktree names, sorted, for completion/tree.

    The presentation-side projection of the single source of truth
    (:meth:`Worktree.list`): map each entry to its ``worktrees/``-relative name
    via :func:`worktree_name` and drop the out-of-tree ones (``None``). Lenient
    by default so shell completion never errors; *strict* re-raises (letting the
    tree view mark a project whose git query failed).
    """
    try:
        worktrees_dir = resolve_project_dir(config, workspace, project) / "worktrees"
    except Exception:
        if strict:
            raise

        return []

    names = [
        name
        for entry in Worktree(config).list(workspace, project, strict=strict)
        if (name := worktree_name(entry.path, worktrees_dir)) is not None
    ]

    return sorted(names)


def _branch_names(config: Config, workspace: str | None, project: str | None) -> list[str]:
    """Return a project's ``create``-targetable branch names (local + origin), for completion.

    The presentation-side wrapper of :func:`wtm.git.list_branches`. Lenient: any
    failure (unknown project, missing git dir, git error) yields ``[]`` so shell
    completion never errors. Mirrors :func:`_worktree_names`.
    """
    try:
        return git.list_branches(resolve_project_dir(config, workspace, project) / ".git")
    except Exception:
        return []


def _target_candidates(
    kinds: frozenset[TokenKind], config: Config, args: list[str]
) -> dict[str, set[str]]:
    """Map the admissible token kinds at a slot to labelled completion candidates.

    Returns a name -> kind-label set map (a name reachable as several roles
    carries all of them). Because the grammar is count-based and the three
    namespaces are flat, a slot's candidates are the *union* over how the
    already-typed ``args`` might be read:

    - ``WORKSPACE`` (slot 0 only) -> declared workspace names.
    - ``PROJECT`` -> at slot 0, the active workspace's projects; at slot 1, the
      projects of ``args[0]`` read as a WORKSPACE.
    - ``WORKTREE_NAME`` -> at slot 0, the cwd-inferred project's worktrees; at
      slot 1, ``args[0]`` read as a PROJECT (active workspace); at slot 2,
      ``args[1]`` in workspace ``args[0]`` (the WORKSPACE PROJECT reading).
    """
    candidates: dict[str, set[str]] = {}
    if TokenKind.WORKSPACE in kinds:
        for name in config.workspaces:
            candidates.setdefault(name, set()).add("workspace")
    if TokenKind.PROJECT in kinds:
        # slot 0 -> active workspace; slot 1 -> args[0] read as a WORKSPACE.
        workspace = args[0] if args else None
        for name in Project(config).list(workspace):
            candidates.setdefault(name, set()).add("project")
    if TokenKind.WORKTREE_NAME in kinds:
        # slot 0 -> cwd project; slot 1 -> args[0] as PROJECT; slot 2 -> args[0]
        # WORKSPACE + args[1] PROJECT.
        if len(args) >= 2:
            ws_read, proj_read = args[0], args[1]
        elif len(args) == 1:
            ws_read, proj_read = None, args[0]
        else:
            ws_read, proj_read = None, None
        for name in _worktree_names(config, ws_read, proj_read):
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


def _complete_locate(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Complete an unpinned locator (``enter``/``leave``): the whole name universe.

    Slot 0 offers the flat union of every worktree, project, and workspace name
    across all workspaces (labelled by kind, each carrying its location context --
    see :func:`_locate_label`). A leading token narrows the next slot to the
    worktrees (and, for a workspace, the projects) it qualifies, mirroring the
    runtime cascade.
    """
    return _locate_completion(ctx, None)


def _complete_locate_project(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Complete a PROJECT-pinned locator (``list``, ``project enter``/``leave``)."""
    return _locate_completion(ctx, Tier.PROJECT)


def _complete_locate_worktree(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Complete a WORKTREE-pinned locator (``hook list``)."""
    return _locate_completion(ctx, Tier.WORKTREE)


def _locate_completion(ctx: typer.Context, tier: Tier | None) -> list[tuple[str, str]]:
    """Offer the locator candidates admissible at this slot under *tier*'s pin.

    The shared body of the ``_complete_locate*`` callbacks: a pin narrows the
    universe to the tier the command can actually resolve, so ``wtm list <TAB>``
    offers projects rather than every name in the tree. Builds its own index (the
    app callback has not fired) from the filesystem, so it never touches git and
    never errors.
    """
    args = ctx.params.get("args") or []
    try:
        index = build_index(Config.load())
    except Exception:
        return []

    multi_ws = len(index.workspaces) > 1

    return _labelled_locations(_locate_candidates(args, index, tier), multi_ws)


def _locate_candidates(
    args: list[str], index: LocationIndex, tier: Tier | None = None
) -> dict[str, list[Location]]:
    """Map the next locator slot to a name -> matching locations from *index*.

    Slot 0 (no args) is the whole universe (every worktree, project, workspace).
    A single qualifier ``A`` offers the worktrees it qualifies (``A`` read as a
    project or a workspace) plus, when ``A`` is a workspace, its projects. Two
    qualifiers ``A B`` (workspace, project) offer that project's worktrees.
    Beyond that nothing completes. The locations (not just kinds) are kept so the
    label can show each target's location context.

    A *tier* pin drops only the tiers that can be neither the leaf nor an
    ancestor qualifier of it -- so a PROJECT pin drops worktree names (``wtm list
    <TAB>`` offers projects, not every name in the tree) while still offering
    workspaces, which qualify a project. A WORKTREE pin drops nothing: every tier
    is either its leaf or one of its ancestors.
    """
    candidates: dict[str, list[Location]] = {}
    want_worktree = tier is not Tier.PROJECT

    def add(name: str, location: Location) -> None:
        candidates.setdefault(name, []).append(location)

    if not args:
        if want_worktree:
            for wt in index.worktrees:
                add(wt.name, wt)  # type: ignore[arg-type]
        for proj in index.projects:
            add(proj.project, proj)  # type: ignore[arg-type]
        for name in index.workspaces:
            add(name, Location(tier=Tier.WORKSPACE, workspace=name))

        return candidates

    quals = args
    if want_worktree:
        for wt in index.worktrees:
            if _quals_prefix_worktree(quals, wt):
                add(wt.name, wt)  # type: ignore[arg-type]
    if len(quals) == 1:
        # A read as a workspace also offers that workspace's projects (project entry).
        for proj in index.projects:
            if proj.workspace == quals[0]:
                add(proj.project, proj)  # type: ignore[arg-type]

    return candidates


def _quals_prefix_worktree(quals: list[str], wt: Location) -> bool:
    """Whether *quals* address worktree *wt*'s ancestors (one: project/workspace; two: both)."""
    if len(quals) == 1:
        return quals[0] in (wt.project, wt.workspace)
    if len(quals) == 2:
        return quals[0] == wt.workspace and quals[1] == wt.project

    return False


def _labelled_locations(
    candidates: dict[str, list[Location]], multi_ws: bool
) -> list[tuple[str, str]]:
    """Turn a locator name -> locations map into sorted ``(value, label)`` tuples.

    The locator analog of :func:`_labelled`: the label is the winning kind (the
    tier a lone token resolves to, by target precedence) plus the location context
    of that tier's targets. *multi_ws* (more than one workspace declared) governs
    whether the workspace joins the context (see :func:`_locate_label`).
    """
    return [(name, _locate_label(candidates[name], multi_ws)) for name in sorted(candidates)]


def _location_context(location: Location, multi_ws: bool) -> str:
    """The ``in <...>`` qualifier for a location: where in the tree it lives.

    A worktree is qualified by its project (``workspace/project`` when several
    workspaces are declared); a project by its workspace (only when several are
    declared); a workspace has nothing above it, so returns ``""``.
    """
    if location.tier is Tier.WORKTREE:
        return f"{location.workspace}/{location.project}" if multi_ws else location.project or ""
    if location.tier is Tier.PROJECT:
        return location.workspace if multi_ws else ""

    return ""


def _locate_label(locations: list[Location], multi_ws: bool) -> str:
    """Render a name's label: its winning kind, that tier's context, then shadows.

    The winning kind (heads by :data:`_TARGET_PRECEDENCE`) is followed by the
    location context of its targets -- ``worktree in datactl`` -- so you see where
    a lone token would land before entering it. A name that spans several targets
    of the winning tier enumerates their contexts, capped at two with a ``+N more``
    tail. Tiers reachable only via a further token trail as ``(also X, Y)``.
    """
    kinds = {location.tier.value for location in locations}
    head, *rest = [kind for kind in _TARGET_PRECEDENCE if kind in kinds]
    winners = [location for location in locations if location.tier.value == head]
    contexts = [ctx for location in winners if (ctx := _location_context(location, multi_ws))]

    label = head
    if contexts:
        shown = ", ".join(contexts[:2])
        if len(contexts) > 2:
            shown += f" +{len(contexts) - 2} more"
        label = f"{head} in {shown}"
    elif len(winners) > 1:
        # Defensive: winning tier collides but carries no context (not reachable
        # given unique dir names) -- fall back to a bare count.
        label = f"{head} ({len(winners)})"
    if rest:
        label += f" (also {', '.join(rest)})"

    return label


def _complete_event(incomplete: str) -> list[str]:
    """Autocompletion: the known hook event names, prefix-filtered."""
    return [event for event in EVENTS if event.startswith(incomplete)]


def _complete_hook_target(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Complete the '[PROJECT] NAME | [DIR] HOOK' variadic for hook delete.

    HOOK (the event) is the trailing token, so any slot may be it: the target
    candidates admissible at this slot (via ``WORKTREE``-mode :func:`slot_kind`)
    are merged with the event names (label ``event``). Once the target is
    complete (three args: WORKSPACE PROJECT NAME) only the event can follow. The
    event union lives here, not in :mod:`wtm.target`: the hook-prefix grammar is
    a cli concern.

    Runs during shell completion (app callback has not fired), so it builds its
    own config; :class:`Project` and :meth:`Worktree.list` are lenient, so this
    never errors.
    """
    args = ctx.params.get("args") or []
    config = Config.load()
    kinds = slot_kind(TargetMode.WORKTREE, args)
    candidates = _target_candidates(kinds, config, args)
    if len(args) <= 3:
        for event in EVENTS:
            candidates.setdefault(event, set()).add("event")

    return _labelled(candidates, _HOOK_PRECEDENCE)


def _complete_locate_hook_target(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Complete the 'LOCATOR | DIR HOOK' variadic for hook run.

    The locator analog of :func:`_complete_hook_target`: ``hook run`` resolves its
    target as a worktree-pinned locator, so the target slots offer the locator's
    name universe (see :func:`_locate_candidates`) rather than the count-based
    grammar's. HOOK is still the trailing token and so may appear at any slot, so
    the event names are merged in as before; a name already offered as a target
    keeps its locator label.
    """
    args = ctx.params.get("args") or []
    try:
        index = build_index(Config.load())
    except Exception:
        return []
    offered = _labelled_locations(
        _locate_candidates(args, index, Tier.WORKTREE), len(index.workspaces) > 1
    )
    names = {name for name, _ in offered}
    events = [(event, "event") for event in EVENTS if event not in names]

    return sorted(offered + events)


# --- config/scope completion -------------------------------------------------


def _rung_label(rung: SourceRung) -> str:
    """Label a resolved rung by its definition level: tier for an inheritable
    key, otherwise the scope (file tier) it was read from."""
    return rung.tier or rung.scope.value


def _config_key_candidates(*, array: bool, incomplete: str) -> list[tuple[str, str]]:
    """KEY candidates for the generic key commands: effective keys, labelled.

    Shared by ``config get``/``set``/``unset``: the cwd-inferred entity's
    non-``(unset)`` keys (entity-local arbitrary keys unioned with inheritable
    keys set on an ancestor), each labelled by its definition level -- the same
    ``tier``/``scope`` ``config get --origin`` prints. With *array* (``-a``),
    the one reserved array a user may mutate via ``-a`` -- ``inheritable`` (the
    opt-in allowlist) -- is offered too, labelled ``allowlist``. Builds its own
    config (the app callback has not fired) and is lenient, so completion never
    errors. Sorted by key.
    """
    try:
        config = Config.load()
        target = config.resolve_generic_target(current_dir(resolve=False))
        keys = config.effective_keys(target)
    except Exception:
        return []
    items: dict[str, str] = {
        key: _rung_label(rung) for key, rung in keys if key.startswith(incomplete)
    }
    if array and "inheritable".startswith(incomplete):
        items["inheritable"] = "allowlist"

    return sorted(items.items())


def _config_subkey_candidates(key: str, incomplete: str) -> list[str]:
    """SUBKEY candidates: the cells of the table at *key*, prefix-filtered.

    Shared by ``config get`` (named ``key`` argument) and ``config set``/
    ``unset -t`` (variadic ``args[0]``): the table's keys are offered when *key*
    resolves to a dict and nothing otherwise. Lenient like every completer;
    reserved keys are rejected by :func:`validate_arbitrary_key`, so a reserved
    KEY yields nothing rather than erroring.
    """
    try:
        validate_arbitrary_key(key)
        config = Config.load()
        target = config.resolve_generic_target(current_dir(resolve=False))
        effective, _ = config.resolve_generic_value(target, key)
    except Exception:
        return []
    if not isinstance(effective, dict):
        return []

    return [cell for cell in sorted(effective) if cell.startswith(incomplete)]


def _config_array_element_candidates(key: str, incomplete: str) -> list[str]:
    """VALUE candidates for ``-a``: the elements of the array at *key*.

    Used by ``config set -a``/``unset -a``: the array's elements are offered
    when *key* resolves to a list/tuple and nothing otherwise. ``inheritable``
    (the one reserved array ``-a`` may touch) skips :func:`validate_arbitrary_key`
    and is read directly, mirroring the commands' own guard. Lenient; the
    most-local array is offered (arrays do not merge across scopes).
    """
    try:
        if key != "inheritable":
            validate_arbitrary_key(key)
        config = Config.load()
        target = config.resolve_generic_target(current_dir(resolve=False))
        effective, _ = config.resolve_generic_value(target, key)
    except Exception:
        return []
    if not isinstance(effective, (list, tuple)):
        return []

    return [str(el) for el in effective if str(el).startswith(incomplete)]


def _complete_config_key(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Complete KEY for ``config get``: the cwd-inferred entity's effective keys.

    Every non-``(unset)`` key (entity-local present arbitrary keys unioned with
    inheritable keys set on an ancestor), each labelled by its definition level
    -- the same ``tier``/``scope`` ``config get --origin`` prints. Builds its own
    config (the app callback has not fired) and is lenient, so completion never
    errors. ``config get`` is cwd-inferred only (no locator prefix), so the
    entity is fixed by the working directory and the KEY slot needs no ``ctx``.
    """
    return _config_key_candidates(array=False, incomplete=incomplete)


def _complete_config_subkey(ctx: typer.Context, incomplete: str) -> list[str]:
    """Complete SUBKEY for ``config get``: the cells of the table at typed KEY.

    The fixed ``key`` argument is already in ``ctx.params`` (verified against the
    real bash protocol), so the table's keys are offered when KEY resolves to a
    dict and nothing otherwise. Lenient like every completer; reserved keys are
    rejected upstream by :func:`validate_arbitrary_key`, so a reserved KEY yields
    nothing rather than erroring.
    """
    key = ctx.params.get("key")
    if not key:
        return []

    return _config_subkey_candidates(key, incomplete)


def _complete_config_set(ctx: typer.Context, incomplete: str) -> list[tuple[str, str] | str]:
    """Complete ``config set KEY [SUBKEY] VALUE``: KEY (and SUBKEY for ``-t``).

    Slot 0 (KEY) offers the effective keys plus ``inheritable`` when ``-a`` (the
    one reserved array ``-a`` may touch); a new key is still freely typed.
    Slot 1 with ``-t`` offers the cells of the table at ``args[0]``; without
    ``-t`` it is the free-form VALUE, which has no completion source. VALUE (the
    trailing slot, scalar or ``-t`` cell) is never completed. Reads the variadic
    ``args`` and the ``-a``/``-t`` flags from ``ctx.params`` (or ``ctx.args``
    during option-value completion).
    """
    args = list(ctx.params.get("args") or ctx.args or [])
    array = bool(ctx.params.get("array", False))
    table = bool(ctx.params.get("table", False))
    if not args:
        return _config_key_candidates(array=array, incomplete=incomplete)
    if len(args) == 1 and table:
        return _config_subkey_candidates(args[0], incomplete)

    return []


def _complete_config_unset(ctx: typer.Context, incomplete: str) -> list[tuple[str, str] | str]:
    """Complete ``config unset KEY [SUBKEY | VALUE]``: only what is removable.

    Mirrors :func:`_complete_config_set` for KEY (effective keys plus
    ``inheritable`` when ``-a``) and ``-t`` SUBKEY (table cells). The ``-a``
    VALUE slot -- the element to strip from the array at KEY -- additionally
    offers the array's current elements (you can only remove what is there);
    the scalar VALUE slot has no completion source (only KEY is needed).
    """
    args = list(ctx.params.get("args") or ctx.args or [])
    array = bool(ctx.params.get("array", False))
    table = bool(ctx.params.get("table", False))
    if not args:
        return _config_key_candidates(array=array, incomplete=incomplete)
    if len(args) == 1:
        if table:
            return _config_subkey_candidates(args[0], incomplete)
        if array:
            return _config_array_element_candidates(args[0], incomplete)

    return []


def _complete_config_show(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Complete TARGET for ``config show``: per-level dispatch on the level flag.

    The four mutually-exclusive level flags (``--all``/``--workspace``/
    ``--project``/``--worktree``) are read from ``ctx.params`` and routed to the
    existing completer that matches that level's target grammar: no level or
    ``--all`` take no TARGET (nothing completes); ``--workspace`` reuses the
    workspace-only slot-0 completer; ``--project`` reuses the count-based
    ``[[WORKSPACE] PROJECT]`` completer; ``--worktree`` reuses the PROJECT-pinned
    qualifier-only locator (the dumped NAME may be absent, so the leaf stays
    literal). Lenient via the delegated completers.
    """
    level = _config_show_level(
        bool(ctx.params.get("all_workspaces", False)),
        bool(ctx.params.get("workspace", False)),
        bool(ctx.params.get("project", False)),
        bool(ctx.params.get("worktree", False)),
    )
    if level in (None, "all"):
        return []
    if level == "workspace":
        return _complete_workspace_arg(ctx, incomplete)
    if level == "project":
        return _complete_project(ctx, incomplete)

    return _complete_locate_project(ctx, incomplete)


def _complete_scope(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Complete ``-s/--scope`` for the generic-key/hook commands: allowed scopes.

    The cwd-inferred entity's :attr:`GenericTarget.allowed_scopes` (what
    :func:`_generic_scope` would accept) are offered, the auto-default scope (the
    deepest existing dir, used when ``--scope`` is omitted) labelled ``default``.
    On any failure (no active workspace, cwd outside one, ...) the full enum
    degrades gracefully -- preserving typer's default behaviour -- rather than
    the empty list every other completer returns, because ``--scope`` is an enum
    option whose all-5 default is already visible to users outside a workspace.
    """
    try:
        config = Config.load()
        target = config.resolve_generic_target(current_dir(resolve=False))
        allowed = target.allowed_scopes
        default = target.default_scope
    except Exception:
        return [(s.value, "") for s in Scope]

    return [
        (s.value, "default" if s == default else "")
        for s in allowed
        if s.value.startswith(incomplete)
    ]


# --- locator edge ------------------------------------------------------------
#
# The cli side of wtm.locate, shared by every locator-taking command: resolve the
# positionals to a single Location (or die with a copy-runnable ambiguity), then
# hand it over in whichever shape the caller's operation already speaks -- a
# (workspace, project) pair, or a fully-qualified WORKSPACE PROJECT NAME triple.
# A command that accepts one kind of target only passes a leaf-tier pin, so a
# project-level command never resolves a same-named worktree.
#
# Which commands take a locator (and which deliberately do not) is a policy, not
# an accident: a locator reaches globally and only sees what exists on disk, so
# how much of a command's target it can resolve depends on what already exists.
#
# - Whole target (_locate_worktree_args / _locate_project): the leaf itself
#   exists -- enter/leave, list, resolve, project enter/leave, hook list, hook run.
# - Qualifiers only (_locate_parent_args): the leaf is being named into existence
#   but its ancestors exist -- create, worktree set, config show --worktree.
#   Global reach without pretending to see the leaf; a qualifier the index has
#   never heard of falls back to the literal token, so these commands keep
#   working for a wholly absent target.
# - Neither: the destructive verbs (delete, rename, hook delete) stay bounded by
#   the cwd, because irreversible reaching must be spelled out positionally; and
#   the project-tier commands whose only qualifier is a WORKSPACE (project
#   new/clone/resolve/set/show) gain nothing, a workspace name being exact.


def _locate_one(
    ctx: typer.Context, args: list[str], *, command: str, tier: Tier | None = None
) -> Location:
    """Resolve a locator's tokens to exactly one :class:`Location`, or raise.

    :param args: the locator tokens (non-empty).
    :param command: the ``wtm`` sub-command name, so an ambiguity prints choices
        that can be pasted straight back.
    :param tier: an optional leaf-tier pin (see :func:`wtm.locate.locate`).
    :returns: the sole matching location.
    :raises WtmError: nothing matched, or several did and the current context
        could not single one out.
    """
    resolution = locate(args, build_index(ctx.obj.config), tier=tier)
    if isinstance(resolution, NotFound):
        raise WtmError(f"no {_tier_noun(tier)} matches: {' '.join(args)}")
    if isinstance(resolution, Ambiguous):
        picked = _context_pick(ctx, resolution.candidates)
        if picked is None:
            raise WtmError(_ambiguous_message(args, resolution.candidates, command))

        return picked

    return resolution.location


def _locate_project(
    ctx: typer.Context, args: list[str], *, command: str
) -> tuple[str | None, str | None]:
    """Resolve a project-pinned locator to ``(workspace, project)``; no tokens is the cwd.

    The shape every project-level operation already takes. Both ``None`` keeps
    their cwd-inference path, so a bare command still means "the project I am in".
    """
    if not args:
        return None, None
    location = _locate_one(ctx, args, command=command, tier=Tier.PROJECT)

    return location.workspace, location.project


def _locate_worktree_args(ctx: typer.Context, args: list[str], *, command: str) -> list[str]:
    """Resolve a worktree-pinned locator to a fully-qualified ``WORKSPACE PROJECT NAME``.

    Returns positionals rather than a :class:`Location` so the existing
    target-grammar operations can be called unchanged; the triple resolves
    regardless of the active workspace, which is the portability win. A single
    ``DIR`` and the no-token cwd default are not names to resolve, so they pass
    straight through.
    """
    if not args or (len(args) == 1 and looks_like_path(args[0])):
        return args
    location = _locate_one(ctx, args, command=command, tier=Tier.WORKTREE)

    return [location.workspace, location.project, location.name]


def _locate_parent_args(ctx: typer.Context, args: list[str], *, command: str) -> list[str]:
    """Resolve a target's leading qualifiers by locator, keeping the last token literal.

    The **qualifier-only locator**: the absent-leaf commands (``create``,
    ``worktree set``, ``config show --worktree``) name a worktree that need not
    exist, so the leaf is not in the index -- but its ancestors usually are.
    Locating just the qualifiers buys those commands the same global reach as a
    full locator while leaving the leaf alone.

    The qualifiers go in as a whole project-pinned locator query, so a lone
    ``PROJECT`` and a ``WORKSPACE PROJECT`` pair both resolve through the ordinary
    cascade. Two cases opt out, so the change is strictly additive:

    - fewer than two tokens carry no qualifier at all (a ``DIR``, a lone ``NAME``
      against the cwd project, or the cwd default);
    - a qualifier matching *no* project anywhere is handed back untouched, because
      these commands are path math and legitimately name an absent project too
      (``wtm create newproj newwt``). Only a name the index has never heard of
      falls through, so the literal reading is the sole possible one.

    An ambiguous qualifier still errors, its choices carrying *leaf* so each
    printed line stays runnable as typed.
    """
    if len(args) < 2:
        return args

    *quals, leaf = args
    resolution = locate(quals, build_index(ctx.obj.config), tier=Tier.PROJECT)
    if isinstance(resolution, NotFound):
        return args
    if isinstance(resolution, Ambiguous):
        location = _context_pick(ctx, resolution.candidates)
        if location is None:
            raise WtmError(
                _ambiguous_message(quals, resolution.candidates, command, suffix=f" {leaf}")
            )
    else:
        location = resolution.location

    return [location.workspace, location.project, leaf]


def _context_pick(ctx: typer.Context, candidates: list[Location]) -> Location | None:
    """Collapse an ambiguous locator by the current context, or ``None`` if it can't.

    The current context is the active workspace and, from the cwd, its project;
    :func:`narrow_by_context` keeps the sole candidate they name (project first,
    then workspace alone) so a unique in-context match resolves silently instead
    of erroring. A match never changes an already-unique resolution -- only the
    ambiguous set is passed in -- so this can only turn a former error into a hit.
    """
    config = ctx.obj.config
    workspace = config.active
    project: str | None = None
    if workspace is not None:
        cwd = current_dir(resolve=False)
        if cwd is not None:
            project_dir = project_dir_for(config.workspaces[workspace].path, cwd)
            if project_dir is not None:
                project = project_dir.name

    return narrow_by_context(candidates, workspace=workspace, project=project)


def _ambiguous_message(
    args: list[str], candidates: list[Location], command: str, *, suffix: str = ""
) -> str:
    """Render the ambiguity error: a header plus one copy-runnable command per candidate.

    *suffix* is appended to each choice so a qualifier-only locator (whose tokens
    are only the leading qualifiers) still prints the trailing positionals the
    user typed, keeping every line runnable as-is.
    """
    tier = candidates[0].tier.value
    lines = [f"'{' '.join(args)}' matches {len(candidates)} {tier}s:"]
    lines.extend(f"  wtm {command} {_qualified(candidate)}{suffix}" for candidate in candidates)
    lines.append("qualify it with a workspace and/or project to disambiguate")

    return "\n".join(lines)


def _qualified(location: Location) -> str:
    """Return the fully-qualified positional form of a location (always portable)."""
    if location.tier is Tier.WORKTREE:
        return f"{location.workspace} {location.project} {location.name}"
    if location.tier is Tier.PROJECT:
        return f"{location.workspace} {location.project}"

    return location.workspace


def _tier_noun(tier: Tier | None) -> str:
    """Name what a locator was looking for, for the not-found message."""
    return tier.value if tier is not None else "worktree, project, or workspace"


project_app = typer.Typer(
    help="Project-level operations (enter, leave, new, clone, list, resolve, set, show).",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
# project_app is added to the top-level app after worktree_app (below) so the
# "Command groups" panel lists worktree first, then project.


@project_app.command(name="new")
def project_new(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        ...,
        metavar="[WORKSPACE] NAME",
        help="New project name, optionally prefixed by its WORKSPACE",
        autocompletion=_complete_workspace_arg,
    ),
) -> None:
    """Create an empty project (.git/ + worktrees/)."""
    workspace, name = split_workspace_project(args)
    if name is None:
        raise WtmError("usage: wtm project new [WORKSPACE] NAME")
    project_dir = ctx.obj.project.new(workspace, name)
    log(f"created project '{project_dir.name}' at {project_dir} (.git/ + worktrees/)")


def _clone(
    ctx: typer.Context,
    url: str = typer.Argument(..., help="Repository URL"),
    args: list[str] = typer.Argument(
        None,
        metavar="[[WORKSPACE] NAME]",
        help="Project name (default: from URL), optionally prefixed by its WORKSPACE",
        autocompletion=_complete_workspace_arg,
    ),
) -> None:
    """Clone into a new project (bare)."""
    workspace, name = split_workspace_project(args or [])
    project_dir = ctx.obj.project.clone(url, workspace, name)
    log(f"cloned '{project_dir.name}' into {project_dir}/.git")


# 'clone' is dual-homed: the canonical 'wtm project clone' plus a top-level
# 'wtm clone' shorthand. One handler, registered under both apps to avoid drift.
# The top-level alias is registered after the worktree shortcuts (see below) so
# the "Worktree shortcuts" panel is encountered first in the help listing.
project_app.command(name="clone")(_clone)


@project_app.command(name="list")
def project_list(
    ctx: typer.Context,
    workspace: str | None = typer.Argument(
        None,
        metavar="[WORKSPACE]",
        help="Workspace to list (default: the active one)",
        autocompletion=_complete_workspace,
    ),
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
    """List a workspace's projects (WORKSPACE, or the active one; ``--all`` spans every workspace).

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
    if workspace is not None and all_ws:
        raise WtmError("a WORKSPACE and --all are mutually exclusive")

    config = ctx.obj.config
    if all_ws:
        names = sorted(config.workspaces)
    elif workspace is not None:
        if workspace not in config.workspaces:
            raise WtmError(f"unknown workspace: '{workspace}'")
        names = [workspace]
    else:
        names = [config.active_workspace.name]

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
        children = _worktree_names(config, None, name, strict=True)
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
        worktrees_dir = resolve_project_dir(config, None, name) / "worktrees"
        try:
            entries = worktree.list(None, name, strict=True)
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
    args: list[str] = typer.Argument(
        None,
        metavar="[[WORKSPACE] PROJECT]",
        help="Project (default: inferred from cwd), optionally prefixed by its WORKSPACE",
        autocompletion=_complete_project,
    ),
    emit_env: bool = typer.Option(False, "-e", "--env", help="Emit the project WTM_* contract"),
) -> None:
    """Print a project's directory (--env: emit project-level WTM_* exports for eval).

    A read-only path query: computes the path without touching git, so it works
    for a not-yet-created project. --env emits only the project-level contract
    (no WTM_WORKTREE_DIR / WTM_BRANCH), likewise by path math.
    """
    workspace, project = split_workspace_project(args or [])
    if emit_env:
        for key, value in ctx.obj.project.resolve_env(workspace, project).items():
            print(emit_env_line(key, value))
    else:
        print(ctx.obj.project.resolve_path(workspace, project))


@project_app.command(name="enter")
def project_enter(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[[WORKSPACE] PROJECT]",
        help="Locator: a project name resolved across every workspace "
        "(default: inferred from cwd)",
        autocompletion=_complete_locate_project,
    ),
    strict: bool = typer.Option(False, "--strict", help="Hook failures are fatal"),
) -> None:
    """cd into a project's directory, firing project-enter hooks.

    Lands beside the project's ``.git``/``worktrees`` and exports the
    project-level WTM_* contract (no WTM_WORKTREE_DIR/WTM_BRANCH), pinning the
    active workspace until leave. The PROJECT is a locator pinned to the project
    tier, so a bare name reaches another workspace's project; ``wtm enter`` finds
    the same target through the unpinned cascade.
    """
    workspace, project = _locate_project(ctx, args or [], command="project enter")
    result = ctx.obj.project.enter(workspace, project, strict=strict)
    emit_shell(cd=result.dir, env=ctx.obj.project.resolve_env(workspace, project))


@project_app.command(name="leave")
def project_leave(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[[WORKSPACE] PROJECT]",
        help="Locator: a project name resolved across every workspace "
        "(default: inferred from cwd)",
        autocompletion=_complete_locate_project,
    ),
    no_cd: bool = typer.Option(False, "-C", "--no-cd", help="Run hooks without cd-ing out"),
    strict: bool = typer.Option(False, "--strict", help="Hook failures are fatal"),
) -> None:
    """Fire project-leave hooks (cascading the worktree leave when inside one); cd out.

    Ends the project session: the WTM_* contract is unset even with -C (which
    only suppresses the cd-out). The PROJECT is a project-pinned locator, as for
    ``project enter``.
    """
    workspace, project = _locate_project(ctx, args or [], command="project leave")
    result = ctx.obj.project.leave(workspace, project, strict=strict)
    log(f"left project '{result.name}'")
    cd_dir = result.cd_dir if (not no_cd and result.cd_dir is not None) else None
    emit_shell(cd=cd_dir, env={}, fallback=False)


@project_app.command(name="set")
def project_set(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[[WORKSPACE] PROJECT]",
        help="Project (default: inferred from cwd), optionally prefixed by its WORKSPACE",
        autocompletion=_complete_project,
    ),
    base_branch: str | None = typer.Option(
        None, "-b", "--base-branch", help="Base branch (empty clears it)"
    ),
    description: str | None = typer.Option(
        None, "-d", "--description", help="Description (empty clears it)"
    ),
    scope: Scope | None = typer.Option(
        None,
        "-s",
        "--scope",
        help="Where to write (project|workspace|user|system); default: deepest existing dir",
    ),
) -> None:
    """Annotate a project's base_branch / description (partial update).

    Writes at ``--scope`` (project/workspace/user/system); the default is the
    deepest scope whose directory exists. A field passed as ``""`` clears it.
    """
    workspace, project = split_workspace_project(args or [])
    result = ctx.obj.project.set(
        workspace, project, base_branch=base_branch, description=description, scope=scope
    )
    log(f"updated project '{result.name}' [{result.scope.value}] ({result.address.file})")


@project_app.command(name="show")
def project_show(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[[WORKSPACE] PROJECT]",
        help="Project (default: inferred from cwd), optionally prefixed by its WORKSPACE",
        autocompletion=_complete_project,
    ),
) -> None:
    """Show a project's detail (dir, description, base_branch + provenance, worktrees)."""
    workspace, project = split_workspace_project(args or [])
    result = ctx.obj.project.show(workspace, project)
    exists = "" if result.exists else "  (no .git on disk)"
    print(f"name: {result.name}")
    print(f"dir: {result.dir}{exists}")
    if result.description:
        print(f"description: {result.description}")
    if result.base_branch:
        print(f"base_branch: {result.base_branch}")
    effective = result.effective_base_branch or "(built-in: origin/HEAD > HEAD > orphan)"
    print(f"effective base_branch: {effective} [{result.base_branch_source}]")
    print(f"worktrees: {result.worktree_count}")


# --- worktree management -----------------------------------------------------
#
# The worktree-level commands are canonically 'wtm worktree <cmd>'; each is also
# kept as a top-level 'wtm <cmd>' alias. One handler per command, registered
# under both apps in a single ordered block (below the handlers) to avoid drift
# and to keep the help ordering in one place.


worktree_app = typer.Typer(
    help="Worktree-level operations (enter, leave, create, delete, rename, list, resolve).",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(worktree_app, name="worktree", rich_help_panel="Command groups")
app.add_typer(project_app, name="project", rich_help_panel="Command groups")


# The worktree handlers are registered under both worktree_app and app in a
# single ordered block below (after every handler is defined), so ordering
# lives in one place. See the note at that block.


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
        help="Base ref a new branch forks from (overrides configured base_branch)",
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
        _locate_parent_args(ctx, args or [], command="create"),
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


# --- hook management ---------------------------------------------------------


hook_app = typer.Typer(
    help="Manage wtm hooks (install, run, delete, list) across scopes.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
# hook_app is added to the top-level app last (below, after config) so the
# "Command groups" panel lists it after project/workspace/config.


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
        metavar="[[WORKSPACE] PROJECT] NAME | [DIR] HOOK",
        help="Locator: a worktree name resolved across every workspace (or a DIR), "
        "followed by the event (HOOK) to run",
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
    another workspace's worktree. A DIR or no target is the cwd worktree.
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
        metavar="[[WORKSPACE] PROJECT] NAME | [DIR]",
        help="Locator: a worktree name resolved across every workspace, or a DIR (default: cwd)",
        autocompletion=_complete_locate_worktree,
    ),
    types: bool = typer.Option(False, "-t", "--types", help="Show a compact per-event summary"),
) -> None:
    """List installed hooks, grouped by event across scopes.

    A read-only query over an existing worktree, so it takes a worktree-pinned
    locator: a bare name reaches another workspace's worktree. A DIR or no arg is
    the cwd worktree, as before.
    """
    grouped = ctx.obj.hooks.list(_locate_worktree_args(ctx, args or [], command="hook list"))
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


def _validate_event(event: str) -> None:
    """Reject an unknown event name (guards the inline-hook writer)."""
    if event not in EVENTS:
        raise WtmError(f"unknown event '{event}' (expected {' | '.join(EVENTS)})")


_GENERIC_SCOPE_HELP = (
    "Where to write (worktree|project|workspace|user|system); default: deepest existing dir"
)


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
    reusing the generic-key prefix machinery. Replaces any inline command
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


# Worktree handlers, registered in one ordered block (after every handler is
# defined) so the help ordering lives in one place. Order is common-first and
# grouped: the session pair, then the lifecycle mutations, then the read-only
# queries. Each is dual-homed: canonically 'wtm worktree <cmd>' plus a top-level
# 'wtm <cmd>' shortcut. (Install hooks are run via 'wtm hook run install'.)
_WORKTREE_COMMANDS = (
    ("enter", enter),
    ("leave", leave),
    ("create", create),
    ("delete", delete),
    ("rename", rename),
    ("list", list_),
    ("resolve", resolve),
)
for _name, _handler in _WORKTREE_COMMANDS:
    worktree_app.command(name=_name)(_handler)
    app.command(name=_name, rich_help_panel="Worktree shortcuts")(_handler)


# 'set' is worktree-namespaced only (no top-level shortcut), matching the
# subcommand-only 'project set' / 'workspace set': it is an annotation verb, not
# a session/lifecycle mover, so it reads better under 'wtm worktree'.
worktree_app.command(name="set")(set_)


# Top-level 'wtm clone' alias, registered after the worktree shortcuts so the
# "Worktree shortcuts" panel heads the listing and "Project shortcuts" follows.
app.command(name="clone", rich_help_panel="Project shortcuts")(_clone)


# 'import' is dual-homed: canonically 'wtm project import' (it folds a repo
# into the project layout) plus a top-level 'wtm import' alias. One handler,
# registered under both apps to avoid drift (as with 'clone'). The Python
# keyword 'import' forbids it as a function name, so the handler is 'import_'.
def import_(
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
        help="Allow importing into an existing target (required if .git is already present)",
    ),
    no_remotes: bool = typer.Option(False, "--no-remotes", help="Skip remote import"),
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

    importer = ctx.obj.importer
    resolution = importer.resolve(
        other, source=source, project=project, all_worktrees=all_worktrees, update=update
    )
    for skipped in resolution.prunable_skipped:
        warn(f"source worktree '{skipped}' is prunable/missing; skipped")
    plan = importer.plan(
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
        for wt_name, preview in importer.dry_run_rsync(
            plan, exclude=exclude, filter_files=filter_file
        ):
            log(f"rsync preview (worktree '{wt_name}'):")
            print(preview, end="", file=sys.stderr)
        return

    result = importer.run(plan, exclude=exclude, filter_files=filter_file, strict=strict)
    _describe_result(result)
    if do_cd and result.cd_dir is not None:
        emit_shell(cd=result.cd_dir, env=ctx.obj.worktree.env_for_dir(result.cd_dir))
    if result.worktrees_failed:
        raise SystemExit(1)


project_app.command(name="import")(import_)
app.command(name="import", rich_help_panel="Project shortcuts")(import_)


def _describe_plan(plan: ImportPlan) -> None:
    """Render an import plan to stderr (used by --dry-run)."""
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


def _describe_result(result: ImportResult) -> None:
    """Render what an import actually did to stderr."""
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
app.add_typer(workspace_app, name="workspace", rich_help_panel="Command groups")


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
    help="Inspect config (show), manage the global default (base-branch), "
    "and read/write arbitrary keys (get/set/unset).",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(config_app, name="config", rich_help_panel="Command groups")


@config_app.command(name="show")
def config_show(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[TARGET]",
        help="Entity to root a level dump at (grammar per the level flag)",
        autocompletion=_complete_config_show,
    ),
    all_workspaces: bool = typer.Option(
        False, "--all", help="Dump every workspace, fully descended (no TARGET)"
    ),
    workspace: bool = typer.Option(
        False, "--workspace", help="Dump a whole workspace: [WORKSPACE], else active"
    ),
    project: bool = typer.Option(
        False, "--project", help="Dump a whole project + its worktrees: [[WORKSPACE] PROJECT]"
    ),
    worktree: bool = typer.Option(
        False, "--worktree", help="Dump one worktree: [[WORKSPACE] PROJECT] NAME"
    ),
    show_origin: bool = typer.Option(
        False, "-o", "--origin", help="Annotate where each value comes from (default view only)"
    ),
    hooks: bool = typer.Option(
        False, "--hooks", help="Also show accumulated inline hooks (default view only)"
    ),
    as_json: bool = typer.Option(False, "-j", "--json", help="Emit as JSON instead of text"),
) -> None:
    """Show the effective merged config for the current context.

    With no level flag, a read-only cwd-oriented view: all declared workspaces
    are listed; the active one descends only as far as the cwd sits -- the
    cwd-bound project's effective annotation and its worktree tier when the cwd
    is inside one. ``--origin`` annotates the tier each value resolves from and
    flags inert annotations; ``--hooks`` renders accumulated inline hooks.

    A level flag instead dumps a full subtree, rooted at the chosen entity and
    descending every project and worktree below it (on-disk unioned with
    declared): ``--all`` every workspace, ``--workspace`` one workspace,
    ``--project`` one project, ``--worktree`` one worktree. The entity is named
    by TARGET (grammar per the level; cwd-inferred when omitted). A subtree dump
    is effective-values-only: ``--origin``/``--hooks`` are rejected with it, and
    ``--json`` (orthogonal, valid on the default view too) emits the nested tree.
    """
    config = ctx.obj.config
    args = args or []
    level = _config_show_level(all_workspaces, workspace, project, worktree)

    if level is None:
        if args:
            raise WtmError(
                "a TARGET requires a level flag (--all/--workspace/--project/--worktree)"
            )
        _config_show_default(config, show_origin, hooks, as_json)

        return

    if show_origin or hooks:
        raise WtmError("--origin/--hooks apply to the default view, not a level dump")
    tree = _config_show_tree(ctx, level, args)
    if as_json:
        print(config_tree_json(tree))

        return
    _print_config_tree(tree)


def _config_show_level(
    all_workspaces: bool, workspace: bool, project: bool, worktree: bool
) -> str | None:
    """Resolve the mutually-exclusive level flags to a level name (or ``None``)."""
    chosen = [
        name
        for name, on in (
            ("all", all_workspaces),
            ("workspace", workspace),
            ("project", project),
            ("worktree", worktree),
        )
        if on
    ]
    if len(chosen) > 1:
        raise WtmError("--all/--workspace/--project/--worktree are mutually exclusive")

    return chosen[0] if chosen else None


def _config_show_tree(ctx: typer.Context, level: str, args: list[str]) -> ConfigTree:
    """Decode TARGET per *level* and resolve the subtree.

    Each level keeps its own slice of the target grammar. The worktree level
    additionally resolves its leading PROJECT qualifier as a locator (the dumped
    NAME may be declared-but-absent, so the leaf stays literal), which is why this
    takes the ``ctx`` rather than a bare :class:`Config`.
    """
    config = ctx.obj.config
    if level == "all":
        if args:
            raise WtmError("--all takes no TARGET")

        return config.resolve_tree("all")

    if level == "workspace":
        if len(args) > 1:
            raise WtmError("--workspace takes at most a WORKSPACE")

        return config.resolve_tree("workspace", workspace=args[0] if args else None)

    if level == "project":
        ws, proj = split_workspace_project(args)

        return config.resolve_tree("project", workspace=ws, project=proj)

    # worktree: [[WORKSPACE] PROJECT] NAME (cwd-inferred when no tokens)
    args = _locate_parent_args(ctx, args, command="config show --worktree")
    ws, proj, name = split_target(args) if args else (None, None, None)

    return config.resolve_tree("worktree", workspace=ws, project=proj, name=name)


def _config_show_default(config: Config, show_origin: bool, hooks: bool, as_json: bool) -> None:
    """The cwd-oriented default view (unchanged behavior; JSON variant orthogonal)."""
    view = config.resolve_view()
    if as_json:
        print(config_tree_json(_default_view_as_tree(config, view)))

        return

    print(f"active workspace: {config.active or '(none)'}")
    if config.default_base_branch:
        origin = f"  [{config.base_branch_source().value}]" if show_origin else ""
        print(f"default base_branch: {config.default_base_branch}{origin}")

    print("workspaces:")
    for name, ws in sorted(config.workspaces.items()):
        origin = f"  [{ws.scope.value}]" if show_origin else ""
        print(f"  {name}: {ws.path}{origin}")
        if ws.base_branch:
            print(f"    base_branch: {ws.base_branch}")
        if name == config.active:
            _print_extra("    ", ws.extra)
        for pname in sorted(_workspace_project_names(config, ws)):
            _print_workspace_project(config, ws, pname, show_origin)
        if hooks:
            _print_scope_hooks("    ", ws.hooks, show_origin, "workspace")

    _print_effective_project(view, show_origin, hooks)


def _default_view_as_tree(config: Config, view: ResolvedConfig) -> ConfigTree:
    """Wrap the cwd-capped view as a single-project ``ConfigTree`` for ``--json``.

    Only the cwd-bound project (and its worktree) are resolved in the default
    view, so JSON of it is that project node, or the empty ``all`` root when the
    cwd is not inside a project.
    """
    if view.project is None:
        return ConfigTree(level="all", default_base_branch=config.default_base_branch)

    return ConfigTree(level="project", project=view.project)


def _workspace_project_names(config: Config, ws: Workspace) -> set[str]:
    """Projects to list under *ws*: on-disk dirs unioned with declared names."""
    return config.project_names(ws)


def _print_workspace_project(config: Config, ws: Workspace, pname: str, show_origin: bool) -> None:
    """Render one project line under a workspace, resolved via the probe engine."""
    annotation = config.project_annotation(ws, pname)
    effective, source = config.base_branch_provenance(ws, pname)
    branch = f"{effective}  [{source}]" if (effective and show_origin) else effective
    bits = [b for b in (annotation.description, branch) if b]
    detail = f" ({', '.join(bits)})" if bits else ""
    inert = ""
    if show_origin and not (ws.path / pname / ".git").is_dir():
        inert = "  (no such project on disk)"
    print(f"    project {pname}{detail}{inert}")


def _print_effective_project(view: ResolvedConfig, show_origin: bool, hooks: bool) -> None:
    """Render the cwd-bound project (and its worktree) from the resolved view."""
    project = view.project
    if project is None:
        return

    print(f"project {project.name}: {project.dir}")
    if project.description:
        print(f"  description: {project.description}")
    if project.effective_base_branch:
        origin = f"  [{project.base_branch_source}]" if show_origin else ""
        print(f"  base_branch: {project.effective_base_branch}{origin}")
    _print_extra("  ", project.extra)
    if hooks:
        _print_scope_hooks("  ", project.hooks, show_origin, "project")

    worktree = project.worktree
    if worktree is None:
        return

    print(f"  worktree {worktree.name}: {worktree.dir}")
    if worktree.description:
        print(f"    description: {worktree.description}")
    _print_extra("    ", worktree.extra)
    if hooks:
        _print_scope_hooks("    ", worktree.hooks, show_origin, "worktree")


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


def _print_config_tree(tree: ConfigTree) -> None:
    """Render a scoped subtree dump as text, rooted at the requested level.

    Effective-values-only (no ``--origin``/``--hooks``): each entity prints its
    scalars plus arbitrary keys, then descends every child. The workspace/project
    headers only appear when they are part of the dumped subtree.
    """
    if tree.level == "all":
        if tree.default_base_branch:
            print(f"default base_branch: {tree.default_base_branch}")
        for ws in tree.workspaces:
            _print_workspace_tree(ws, "")

        return

    if tree.workspace is not None:
        _print_workspace_tree(tree.workspace, "")
    elif tree.project is not None:
        _print_project_tree(tree.project, "")
    elif tree.worktree is not None:
        _print_worktree_tree(tree.worktree, "")


def _print_workspace_tree(ws: ResolvedWorkspace, indent: str) -> None:
    """Render one workspace node and descend its projects."""
    print(f"{indent}workspace {ws.entry.name}: {ws.entry.path}")
    if ws.entry.description:
        print(f"{indent}  description: {ws.entry.description}")
    if ws.entry.base_branch:
        print(f"{indent}  base_branch: {ws.entry.base_branch}")
    _print_extra(f"{indent}  ", ws.entry.extra)
    for project in ws.projects:
        _print_project_tree(project, f"{indent}  ")


def _print_project_tree(project: ResolvedProject, indent: str) -> None:
    """Render one project node and descend its worktrees."""
    inert = "" if project.exists else "  (no .git on disk)"
    print(f"{indent}project {project.name}: {project.dir}{inert}")
    if project.description:
        print(f"{indent}  description: {project.description}")
    if project.effective_base_branch:
        print(f"{indent}  base_branch: {project.effective_base_branch}")
    _print_extra(f"{indent}  ", project.extra)
    for worktree in project.worktrees:
        _print_worktree_tree(worktree, f"{indent}  ")


def _print_worktree_tree(worktree: ResolvedWorktree, indent: str) -> None:
    """Render one worktree leaf (no base_branch tier)."""
    print(f"{indent}worktree {worktree.name}: {worktree.dir}")
    if worktree.description:
        print(f"{indent}  description: {worktree.description}")
    _print_extra(f"{indent}  ", worktree.extra)


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
        metavar="KEY [SUBKEY] VALUE",
        help="Key (plus SUBKEY for -t) and the value to write",
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
    """Set an arbitrary config KEY on the cwd-inferred entity.

    The entity is the deepest one the cwd sits inside (worktree, else project,
    else the active workspace); ``--scope`` picks which file tier of that
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
    """
    if array and table:
        raise WtmError("-a/--array and -t/--table are mutually exclusive")

    if table:
        if len(args) != 3:
            raise WtmError("config set -t requires KEY SUBKEY VALUE")
        key, subkey, value = args
    else:
        if len(args) != 2:
            raise WtmError(f"config set {'-a ' if array else ''}requires KEY VALUE")
        key, value = args

    if key == "inheritable" and array:
        validate_inheritable_element(value)
    else:
        validate_arbitrary_key(key)
    config = ctx.obj.config
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
    key: str = typer.Argument(
        ...,
        metavar="KEY",
        help="Arbitrary (non-reserved) config key",
        autocompletion=_complete_config_key,
    ),
    subkey: str | None = typer.Argument(
        None,
        metavar="[SUBKEY]",
        help="Read a single cell of the table at KEY",
        autocompletion=_complete_config_subkey,
    ),
    show_origin: bool = typer.Option(
        False, "-o", "--origin", help="Print the full probe chain, most-local first"
    ),
) -> None:
    """Resolve an arbitrary config KEY for the cwd-inferred entity.

    Prints the winning (most-local) value, or ``(unset)`` when the key appears
    nowhere in the entity's cascade. An array prints one element per line and a
    table prints ``subkey = value`` per line (sorted); a scalar prints bare.
    Passing SUBKEY reads a single cell of the table at KEY. ``--origin`` instead
    lists every rung that carries the key, most-local first, each showing where
    the value comes from -- its definition level (``project``/``workspace``/
    ``global``) for an inherited key such as ``base_branch``, otherwise its scope
    -- and the file it lives in.
    """
    validate_arbitrary_key(key)
    config = ctx.obj.config
    target = config.resolve_generic_target(current_dir(resolve=False))
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
        metavar="KEY [SUBKEY] [VALUE]",
        help="Key, plus SUBKEY for -t or VALUE for -a",
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
    """Remove an arbitrary config KEY from one tier of the cwd-inferred entity.

    Mirrors :func:`config_set`: the entity is cwd-inferred and ``--scope`` picks
    the file (defaulting to the deepest existing dir). The removal is scalar by
    default (``KEY``); ``-a``/``--array`` (``KEY VALUE``) strips every occurrence
    of VALUE from the array at KEY; ``-t``/``--table`` (``KEY SUBKEY``) removes
    SUBKEY from the table at KEY. The two flags are mutually exclusive. A no-op
    when the target is already absent at that tier.
    """
    if array and table:
        raise WtmError("-a/--array and -t/--table are mutually exclusive")

    if array:
        if len(args) != 2:
            raise WtmError("config unset -a requires KEY VALUE")
        key, value = args
    elif table:
        if len(args) != 2:
            raise WtmError("config unset -t requires KEY SUBKEY")
        key, subkey = args
    else:
        if len(args) != 1:
            raise WtmError("config unset requires KEY")
        (key,) = args

    if not (key == "inheritable" and array):
        validate_arbitrary_key(key)
    config = ctx.obj.config
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


def _generic_scope(target: GenericTarget, scope: Scope | None) -> Scope:
    """Resolve/validate the write scope for a generic key against *target*."""
    scope = scope if scope is not None else target.default_scope
    if scope not in target.allowed_scopes:
        allowed = ", ".join(s.value for s in target.allowed_scopes)
        raise WtmError(
            f"cannot write a key for {target.label} at '{scope.value}' scope "
            f"(choose one of {allowed})"
        )

    return scope


# Register the hook group last so the "Command groups" panel lists it after
# project/workspace/config (see the note at the hook_app definition).
app.add_typer(hook_app, name="hook", rich_help_panel="Command groups")


@app.command(name="shell-init")
def shell_init() -> None:
    """Print the shell wrapper so it can be sourced from a shell rc.

    Emits the packaged ``wtm/shell/wtm.sh`` to stdout, defining the ``wtm``
    function that lets movers like ``enter``/``create -c`` cd the calling shell.
    Source it directly (``eval "$(wtm shell-init)"``) or cache the output to a
    file and source that. Mirrors ``--show-completion``.
    """
    from importlib.resources import files

    print(files("wtm").joinpath("shell/wtm.sh").read_text(), end="")


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
