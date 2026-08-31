"""Locator-cascade shell completion (free name query across every workspace).

The locator completion analog of :mod:`wtm.cli.completions.target`: a pin
narrows the universe to the tier the command can actually resolve, and the
label carries each target's location context ("worktree in datactl").

A token that looks like a partial *path* switches to **path mode**: one level
of directory children at a time, offering only the dirs whose subtree still
leads to an entity admissible under the command's pin -- so once the pin tier
is reached nothing further is offered and completion can never steer into a
truncation the runtime would have had to apply. Entity-bearing dirs are
labelled like their name-mode twins; intermediate waypoints get ``dir``.
Tilde tokens are not answered here at all: the shell's own path completion
takes them (typer's empty-case ``_files`` fallback on zsh, ``-o default`` on
bash), because zsh re-quotes a live ``~`` in a completion candidate on every
insertion round -- wtm could only ever offer the mangled form. Entity
filtering therefore applies to name and slash-anchored tokens.
"""

from __future__ import annotations

from pathlib import Path

import typer

from wtm.cli.completions.target import _TARGET_PRECEDENCE
from wtm.config import Config
from wtm.hooks import EVENTS
from wtm.locator import (
    _TIER_RANK,
    Location,
    LocationIndex,
    Tier,
    _entity_dirs,
    _geometry_relation,
    build_index,
    interpret_path,
)


def _complete_locate(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Complete an unpinned locator (``enter``/``leave``): the whole name universe.

    Slot 0 offers the flat union of every worktree, project, and workspace name
    across all workspaces (labelled by kind, each carrying its location context --
    see :func:`_locate_label`). A leading token narrows the next slot to the
    worktrees (and, for a workspace, the projects) it qualifies, mirroring the
    runtime cascade. A partial-path token switches to :func:`_path_completion`.
    """
    return _locate_completion(ctx, None, incomplete)


def _complete_locate_project(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Complete a PROJECT-pinned locator (``list``, ``project enter``/``leave``)."""
    return _locate_completion(ctx, Tier.PROJECT, incomplete)


def _complete_locate_worktree(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Complete a WORKTREE-pinned locator (``hook list``, ``hook run``)."""
    return _locate_completion(ctx, Tier.WORKTREE, incomplete)


def _locate_completion(
    ctx: typer.Context, tier: Tier | None, incomplete: str
) -> list[tuple[str, str]]:
    """Offer the locator candidates admissible at this slot under *tier*'s pin.

    The shared body of the ``_complete_locate*`` callbacks: a pin narrows the
    universe to the tier the command can actually resolve, so ``wtm list <TAB>``
    offers projects rather than every name in the tree. Builds its own index (the
    app callback has not fired) from the filesystem, so it never touches git and
    never errors. Tilde tokens fall through to the shell's own path completion
    (see the module docstring): wtm offers nothing for them.
    """
    if incomplete.startswith("~"):
        return []

    args = ctx.params.get("args") or []
    try:
        index = build_index(Config.load())
    except Exception:
        return []

    multi_ws = len(index.workspaces) > 1

    if _is_partial_path(incomplete):
        return _path_completion(incomplete, index, tier)

    return _labelled_locations(_locate_candidates(args, index, tier), multi_ws)


# --- path mode ----------------------------------------------------------------


def _is_partial_path(incomplete: str) -> bool:
    """Whether *incomplete* reads as a path being typed rather than a name query."""
    return (
        incomplete.startswith("/")
        or incomplete.startswith("~")
        or incomplete.startswith("./")
        or incomplete.startswith("../")
        or incomplete.startswith(".\\")
        or incomplete in (".", "..")
    )


def _path_completion(
    incomplete: str, index: LocationIndex, tier: Tier | None
) -> list[tuple[str, str]]:
    """Offer the child dirs of the typed base that lead to a pin-admissible entity.

    One level at a time: each offered value extends the typed text verbatim (so
    the shell keeps completing mid-word), followed by a trailing ``/``. A dir is
    offered when an entity equals it (arrival -- its own tier fitting the pin) or
    lies strictly inside it on the way down (descent -- same rule), so nothing
    past the pin tier is ever suggested. Off-tree bases complete nothing. Tilde
    tokens never reach here: the entry points fall them through to the shell's
    own path completion before path mode is consulted.
    """
    prefix, base = _typed_base(incomplete)
    if base is None:
        return []
    try:
        children = sorted(
            (entry for entry in base.iterdir() if entry.is_dir()), key=lambda p: p.name
        )
    except OSError:
        return []

    eff_rank = _TIER_RANK[tier] if tier is not None else None
    entities = _entity_dirs(index)
    multi_ws = len(index.workspaces) > 1

    offers: list[tuple[str, str]] = []
    for child in children:
        arrival: Location | None = None
        descends = False
        for rank, location, entity_dir in entities:
            relation = _geometry_relation(child, entity_dir)
            if relation is None:
                continue
            fits = eff_rank is None or rank == eff_rank
            if relation == "equal":
                if fits and arrival is None:
                    arrival = location
            elif fits:
                descends = True
        if arrival is not None:
            offers.append((f"{prefix}{child.name}/", _locate_label([arrival], multi_ws)))
        elif descends:
            offers.append((f"{prefix}{child.name}/", "dir"))

    return offers


def _typed_base(incomplete: str) -> tuple[str, Path | None]:
    """Split *incomplete* into (verbatim value prefix, absolute base dir).

    A trailing separator means the whole token is the base and the prefix
    reproduces it verbatim; otherwise everything through the last separator
    does, with the suffix left for the shell to overwrite. Bare ``.``/``..``
    (no separator yet) anchor on themselves -- ``.`` lists the cwd, ``..`` its
    parent -- while the prefix grows the shell-standard ``./``/``../`` shape.
    An unexpandable or unreadable base yields ``(prefix, None)``: completion
    simply stops rather than erroring.
    """
    try:
        if "/" in incomplete:
            cut = incomplete.rindex("/") + 1
            prefix, raw_base = incomplete[:cut], incomplete[:cut]
        else:
            prefix, raw_base = f"{incomplete}/", incomplete

        base = interpret_path(raw_base)
        if not base.is_dir():
            return prefix, None

        return prefix, base
    except Exception:
        return incomplete, None


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
    """Complete the 'LOCATOR | PATH HOOK' variadic for hook run.

    The locator analog of :func:`wtm.cli.completions.target._complete_hook_target`:
    ``hook run`` resolves its target as a worktree-pinned locator, so the target
    slots offer the locator's name universe (see :func:`_locate_candidates`)
    rather than the count-based grammar's. HOOK is still the trailing token and
    so may appear at any slot, so the event names are merged in as before -- but
    only in name mode: mid-path there is no slot an event could occupy. A name
    already offered as a target keeps its locator label. Tilde tokens fall
    through to the shell's own path completion (see the module docstring).
    """
    if incomplete.startswith("~"):
        return []

    args = ctx.params.get("args") or []
    try:
        index = build_index(Config.load())
    except Exception:
        return []
    if _is_partial_path(incomplete):
        return _path_completion(incomplete, index, Tier.WORKTREE)

    offered = _labelled_locations(
        _locate_candidates(args, index, Tier.WORKTREE), len(index.workspaces) > 1
    )
    names = {name for name, _ in offered}
    events = [(event, "event") for event in EVENTS if event not in names]

    return sorted(offered + events)
