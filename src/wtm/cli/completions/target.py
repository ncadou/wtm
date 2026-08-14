"""Target-grammar shell completion (count-based positional slots).

Completion candidates carry a kind label (shown as "-- <label>" in zsh/fish):
a lone token's resolution winner heads the label, any roles it shadows follow
in "(also ...)". The precedences below are descending resolution order, a cli
presentation concern (events are not a target TokenKind), so the labelling
lives here rather than in wtm.target.

The locator (``enter``) label diverges: being global, its winner also carries
the target's location context ("worktree in datactl") -- see
:mod:`wtm.cli.completions.locator`.

Typer's autocompletion contract attaches the label via a (value, help) tuple;
it does the prefix filtering and CompletionItem wrapping itself.
"""

from __future__ import annotations

import typer

from wtm import git
from wtm.cli.common import _worktree_names
from wtm.config import Config, resolve_project_dir
from wtm.hooks import EVENTS
from wtm.project import Project
from wtm.target import TargetMode, TokenKind, slot_kind

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
