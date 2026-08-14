"""Locator-cascade shell completion (free name query across every workspace).

The locator completion analog of :mod:`wtm.cli.completions.target`: a pin
narrows the universe to the tier the command can actually resolve, and the
label carries each target's location context ("worktree in datactl").
"""

from __future__ import annotations

import typer

from wtm.cli.completions.target import _TARGET_PRECEDENCE
from wtm.config import Config
from wtm.hooks import EVENTS
from wtm.locator import Location, LocationIndex, Tier, build_index


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

    The locator analog of :func:`wtm.cli.completions.target._labelled`: the label
    is the winning kind (the tier a lone token resolves to, by target
    precedence) plus the location context of that tier's targets. *multi_ws*
    (more than one workspace declared) governs whether the workspace joins the
    context (see :func:`_locate_label`).
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


def _complete_locate_hook_target(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Complete the 'LOCATOR | DIR HOOK' variadic for hook run.

    The locator analog of :func:`wtm.cli.completions.target._complete_hook_target`:
    ``hook run`` resolves its target as a worktree-pinned locator, so the target
    slots offer the locator's name universe (see :func:`_locate_candidates`)
    rather than the count-based grammar's. HOOK is still the trailing token and
    so may appear at any slot, so the event names are merged in as before; a name
    already offered as a target keeps its locator label.
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
