"""The cli side of :mod:`wtm.locator`, shared by every locator-taking command.

Resolve the positionals to a single :class:`wtm.locator.Location` (or die with a
copy-runnable ambiguity), then hand it over in whichever shape the caller's
operation already speaks -- a ``(workspace, project)`` pair, or a
fully-qualified ``WORKSPACE PROJECT NAME`` triple. A command that accepts one
kind of target only passes a leaf-tier pin, so a project-level command never
resolves a same-named worktree.

A single *path* argument rides the locator too (:func:`wtm.locator.locate_path`):
anything starting with ``/``, ``~``, ``./``, ``../`` or ``.``/``..`` is read
path-only, while other tokens are matched by name first and fall back to a cwd
relative-path reading on a clean miss (never past an ambiguity). A path landing
on a tier shallower than the command's pin errors; a path outside every
declared workspace errors -- unmanaged trees are out of scope by design.

Which commands take a locator (and which deliberately do not) is a policy, not
an accident: a locator reaches globally and only sees what exists on disk, so
how much of a command's target it can resolve depends on what already exists.

- Whole target (_locate_worktree_args / _locate_project): the leaf itself
  exists -- enter/leave, list, resolve, project enter/leave, hook list, hook run.
- Qualifiers only (_locate_parent_args): the leaf is being named into existence
  but its ancestors exist -- create, worktree set. Global reach without
  pretending to see the leaf; a qualifier the index has never heard of falls
  back to the literal token, so these commands keep working for a wholly absent
  target.
- Neither: the destructive verbs (delete, rename, hook delete) stay bounded by
  the cwd, because irreversible reaching must be spelled out positionally; and
  the project-tier commands whose only qualifier is a WORKSPACE (project
  new/clone/resolve/set/show) gain nothing, a workspace name being exact.
"""

from __future__ import annotations

import typer

from wtm.core import WtmError, current_dir, project_dir_for
from wtm.locator import (
    Ambiguous,
    Location,
    Mismatch,
    NotFound,
    Resolution,
    Tier,
    build_index,
    locate,
    locate_path,
    narrow_by_context,
)
from wtm.target import looks_like_path


def _resolve_tokens(
    ctx: typer.Context,
    args: list[str],
    *,
    tier: Tier | None = None,
    include_config_only: bool = False,
) -> Resolution:
    """Resolution logic shared by :func:`_locate_one` and :func:`_locate_optional`.

    A single prefixed arg is a path (read path-only); everything else matches
    names first, falling back to a path reading only on a clean not-found -- an
    ambiguity stands rather than silently switching input disciplines.
    """
    index = build_index(ctx.obj.config, include_config_only=include_config_only)
    if len(args) == 1 and looks_like_path(args[0]):
        return locate_path(args[0], index, tier=tier)

    resolution = locate(args, index, tier=tier)
    if isinstance(resolution, NotFound) and len(args) == 1 and "/" in args[0]:
        # A nested-name query ('feature/x') may equally be a relative path tail.
        return locate_path(args[0], index, tier=tier)

    return resolution


def _locate_one(
    ctx: typer.Context,
    args: list[str],
    *,
    command: str,
    tier: Tier | None = None,
    include_config_only: bool = False,
) -> Location:
    """Resolve a locator's tokens to exactly one :class:`Location`, or raise.

    :param args: the locator tokens (non-empty).
    :param command: the ``wtm`` sub-command name, so an ambiguity prints choices
        that can be pasted straight back.
    :param tier: an optional leaf-tier pin (see :func:`wtm.locator.locate`).
    :param include_config_only: enrich the index with config-declared inert
        projects and absent worktrees (see :func:`wtm.locator.build_index`).
    :returns: the sole matching location.
    :raises WtmError: nothing matched (or the match lay outside every declared
        workspace), several did and the current context could not single one
        out, or a path named a shallower tier than *tier*.
    """
    resolution = _resolve_tokens(ctx, args, tier=tier, include_config_only=include_config_only)
    if isinstance(resolution, NotFound):
        raise WtmError(_not_found_message(tier, args))
    if isinstance(resolution, Mismatch):
        raise WtmError(_mismatch_message(resolution, args[0]))
    if isinstance(resolution, Ambiguous):
        picked = _context_pick(ctx, resolution.candidates)
        if picked is None:
            raise WtmError(_ambiguous_message(args, resolution.candidates, command))

        return picked

    return resolution.location


def _locate_cwd(ctx: typer.Context, *, command: str) -> Location:
    """Resolve the cwd itself as a PATH locator to exactly one :class:`Location`.

    The no-args edge shared by the tier-polymorphic commands (``enter``,
    ``leave``, ``resolve``): standing anywhere inside a declared workspace
    answers with the nearest upward entity (worktree > project > workspace) --
    the same path geometry an explicit PATH arg rides. Outside every declared
    workspace it raises the dedicated no-target message, and a removed cwd the
    shell-directory-gone one.

    :param command: the ``wtm`` sub-command name, so an ambiguity prints choices
        that can be pasted straight back.
    """
    cwd = current_dir(resolve=False)
    if cwd is None:
        raise WtmError(
            "cwd is unavailable (the directory was removed); "
            "pass an explicit target (WORKSPACE PROJECT NAME | PROJECT NAME | NAME | DIR)"
        )

    resolution = _resolve_tokens(ctx, [str(cwd)])
    if isinstance(resolution, NotFound):
        raise WtmError(
            "no NAME given and cwd is not inside a declared workspace "
            "(pass WORKSPACE PROJECT NAME, PROJECT NAME, or NAME)"
        )
    if isinstance(resolution, Mismatch):
        raise WtmError(_mismatch_message(resolution, str(cwd)))
    if isinstance(resolution, Ambiguous):
        picked = _context_pick(ctx, resolution.candidates)
        if picked is None:
            raise WtmError(_ambiguous_message([str(cwd)], resolution.candidates, command))

        return picked

    return resolution.location


def _locate_optional(
    ctx: typer.Context,
    args: list[str],
    *,
    tier: Tier | None = None,
    include_config_only: bool = False,
) -> Location | None:
    """Resolve a locator, returning ``None`` on not-found or unresolved ambiguity.

    The non-raising counterpart to :func:`_locate_one`: for ``config get``'s
    data-type inference, where an unresolved locator at N=2 falls back to the
    KEY+SUBKEY reading rather than erroring. Applies the same context tiebreak
    as :func:`_locate_one` before giving up.
    """
    resolution = _resolve_tokens(ctx, args, tier=tier, include_config_only=include_config_only)
    if isinstance(resolution, NotFound | Mismatch):
        return None
    if isinstance(resolution, Ambiguous):
        return _context_pick(ctx, resolution.candidates)

    return resolution.location


def _not_found_message(tier: Tier | None, args: list[str]) -> str:
    """Shape the not-found error by what was typed: name-first or path-first."""
    noun = _tier_noun(tier)
    if len(args) == 1 and looks_like_path(args[0]):
        return (
            f"path '{args[0]}' names no entity "
            "(it does not exist, or lies outside every declared workspace)"
        )
    if len(args) == 1 and "/" in args[0]:
        return (
            f"no {noun} matches: {' '.join(args)} "
            f"(and '{args[0]}' is not a path under any workspace)"
        )

    return f"no {noun} matches: {' '.join(args)}"


def _mismatch_message(mismatch: Mismatch, value: str) -> str:
    """Render the pin-mismatch error: truncation only climbs."""
    return f"'{value}' names a {mismatch.natural.value}, not a {mismatch.wanted.value}"


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
    path arg resolves through the locator too (truncated to its worktree); the
    empty-token cwd default passes straight through.
    """
    if not args:
        return args
    location = _locate_one(ctx, args, command=command, tier=Tier.WORKTREE)

    return [location.workspace, location.project, location.name]


def _locate_parent_args(
    ctx: typer.Context,
    args: list[str],
    *,
    command: str,
    include_config_only: bool = False,
    require_project_on_disk: bool = False,
) -> list[str]:
    """Resolve a target's leading qualifiers by locator, keeping the last token literal.

    The **qualifier-only locator**: the absent-leaf commands (``create``,
    ``worktree set``) name a worktree that need not exist, so the leaf is not
    in the index -- but its ancestors usually are. Locating just the qualifiers
    buys those commands the same global reach as a full locator while leaving
    the leaf alone.

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

    :param include_config_only: enrich the index with config-declared inert
        projects (see :func:`wtm.locator.build_index`), so a declared-but-absent
        project resolves instead of falling back to the literal.
    :param require_project_on_disk: after a successful resolution, raise a
        reminder if the resolved project has no ``.git`` on disk. Without
        ``include_config_only`` the guard is a no-op (an on-disk project always
        has ``.git``). Used by ``create`` to catch an inert-declared project
        before the core's generic ``project git dir not found`` error.
    """
    if len(args) < 2:
        return args

    *quals, leaf = args
    resolution = locate(
        quals,
        build_index(ctx.obj.config, include_config_only=include_config_only),
        tier=Tier.PROJECT,
    )
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

    if require_project_on_disk:
        ws = ctx.obj.config.workspaces[location.workspace]
        if not (ws.path / location.project / ".git").is_dir():
            raise WtmError(
                f"project '{location.project}' is declared in config but not on disk; "
                f"create it first with 'wtm project new {location.workspace} {location.project}'"
            )

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
