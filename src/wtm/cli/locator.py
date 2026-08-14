"""The cli side of :mod:`wtm.locate`, shared by every locator-taking command.

Resolve the positionals to a single :class:`wtm.locate.Location` (or die with a
copy-runnable ambiguity), then hand it over in whichever shape the caller's
operation already speaks -- a ``(workspace, project)`` pair, or a
fully-qualified ``WORKSPACE PROJECT NAME`` triple. A command that accepts one
kind of target only passes a leaf-tier pin, so a project-level command never
resolves a same-named worktree.

Which commands take a locator (and which deliberately do not) is a policy, not
an accident: a locator reaches globally and only sees what exists on disk, so
how much of a command's target it can resolve depends on what already exists.

- Whole target (_locate_worktree_args / _locate_project): the leaf itself
  exists -- enter/leave, list, resolve, project enter/leave, hook list, hook run.
- Qualifiers only (_locate_parent_args): the leaf is being named into existence
  but its ancestors exist -- create, worktree set, config show --worktree.
  Global reach without pretending to see the leaf; a qualifier the index has
  never heard of falls back to the literal token, so these commands keep
  working for a wholly absent target.
- Neither: the destructive verbs (delete, rename, hook delete) stay bounded by
  the cwd, because irreversible reaching must be spelled out positionally; and
  the project-tier commands whose only qualifier is a WORKSPACE (project
  new/clone/resolve/set/show) gain nothing, a workspace name being exact.
"""

from __future__ import annotations

import typer

from wtm.core import WtmError, current_dir, project_dir_for
from wtm.locate import (
    Ambiguous,
    Location,
    NotFound,
    Tier,
    build_index,
    locate,
    narrow_by_context,
)
from wtm.target import looks_like_path


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
