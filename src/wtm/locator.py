"""The locator: resolve a free, scope-inferring name query to a target.

A **locator** is the sibling of the count-based target grammar in :mod:`wtm.target`:
same output (a worktree, a project, or a workspace), a different input discipline.
Where the target grammar is positional and active-workspace-relative, a locator is
name-based, scope-free, and global -- its tokens are matched against the name
universe of *every* declared workspace, so the same token works from anywhere
(the shell-history-portability motive). It is consumed by the commands that point
at an entity which must already *exist* and whose reach may safely be global;
naming a not-yet-created entity, and every destructive verb, stays on the target
grammar.

The tree has three levels -- workspace -> project -> worktree (a worktree name is
its ``worktrees/``-relative path, so nesting stays one name). A locator's tokens
name a suffix of that tree: the *last* token is the leaf you land on, the leading
tokens are ancestor qualifiers in top-down order (levels may be skipped, e.g. a
workspace qualifier with no project). Resolution is a **leaf-tier cascade** --
try the deepest leaf tier first, fall back only when it yields nothing:

1. leaf = worktree named ``tk``, ancestors matched as a project *or* a workspace;
2. else leaf = project named ``tk``, a leading token matched as a workspace;
3. else leaf = workspace named ``tk`` (only for a single token).

A caller that accepts just one kind of target passes a **leaf-tier pin**
(``locate(..., tier=...)``): the cascade is skipped and only that tier's matches
count, so a project-only command never resolves a same-named worktree. The
unpinned cascade is for the tier-polymorphic movers (``enter``/``leave``).

Within the winning tier: one match resolves, several are ambiguous (the caller
prints the qualified choices), none across all tiers is not-found. A caller with
a current context may first collapse an ambiguity with :func:`narrow_by_context`
(current project, then current workspace); only a unique in-context match resolves,
otherwise the tie stands and is reported as before.

:func:`locate` is pure -- it takes a :class:`LocationIndex` snapshot explicitly, so
the cascade is the test surface without a subprocess or git. :func:`build_index`
is the one config-bound edge: it enumerates the tree from the filesystem (no git),
so it is cheap and lenient enough for shell completion.
"""

from __future__ import annotations

__all__ = [
    "Ambiguous",
    "Location",
    "LocationIndex",
    "NotFound",
    "Resolution",
    "Resolved",
    "Tier",
    "build_index",
    "locate",
    "narrow_by_context",
]

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from wtm.config import Config


class Tier(Enum):
    """Which level of the workspace->project->worktree tree a locator resolved to."""

    WORKTREE = "worktree"
    PROJECT = "project"
    WORKSPACE = "workspace"


# The leaf-tier cascade order: deepest tier first, so a worktree named like a
# project wins outright. A leaf-tier pin (locate's `tier=`) short-circuits this.
_CASCADE = (Tier.WORKTREE, Tier.PROJECT, Tier.WORKSPACE)


@dataclass(frozen=True)
class Location:
    """A single resolved (or candidate) target: its tier and identifying names.

    ``project`` is ``None`` for a WORKSPACE tier; ``name`` (the worktree's
    ``worktrees/``-relative name) is set only for a WORKTREE tier. The names are
    enough for the caller to reconstruct a canonical command against the
    existing tier operations (``Worktree.enter`` / ``Project.enter`` /
    ``Workspaces.enter``), so the locator adds no new mutation logic.
    """

    tier: Tier
    workspace: str
    project: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class Resolved:
    """A locator that named exactly one target."""

    location: Location


@dataclass(frozen=True)
class Ambiguous:
    """A locator whose winning tier held several targets (the caller lists them)."""

    candidates: list[Location]


@dataclass(frozen=True)
class NotFound:
    """A locator that matched nothing at any tier."""


Resolution = Resolved | Ambiguous | NotFound


@dataclass(frozen=True)
class LocationIndex:
    """A pure snapshot of the tree a locator resolves against.

    Enumerated across every declared workspace by :func:`build_index`; passed to
    :func:`locate` explicitly so the cascade stays pure and testable. ``roots``
    maps a workspace name to its projects root, letting a caller turn a resolved
    :class:`Location` into a directory when it needs one.
    """

    worktrees: list[Location] = field(default_factory=list)  # Tier.WORKTREE entries
    projects: list[Location] = field(default_factory=list)  # Tier.PROJECT entries
    workspaces: list[str] = field(default_factory=list)
    roots: dict[str, Path] = field(default_factory=dict)


def locate(tokens: list[str], index: LocationIndex, *, tier: Tier | None = None) -> Resolution:
    """Resolve a locator's *tokens* against *index* by leaf-tier cascade (pure).

    :param tokens: the free name query (at least one token; the last is the leaf).
    :param index: the enumerated tree snapshot.
    :param tier: an optional **leaf-tier pin** -- when given, only that tier is
        tried (a project-only command never resolves a same-named worktree);
        ``None`` runs the full cascade.
    :returns: a :class:`Resolved`, :class:`Ambiguous`, or :class:`NotFound`.

    The leaf token is tried deepest-tier-first (worktree, then project, then --
    for a lone token -- workspace); the first tier with any match is the winning
    tier, whose matches are the result (one resolves, several are ambiguous).
    Leading tokens qualify the leaf's ancestors: for a worktree leaf each may be
    the project or the workspace; for a project leaf a single leading token is
    the workspace.
    """
    if not tokens:
        raise ValueError("locate requires at least one token")

    *quals, leaf = tokens
    for candidate in (tier,) if tier is not None else _CASCADE:
        hits = _tier_matches(candidate, quals, leaf, index)
        if hits:
            return _finish(hits)

    return NotFound()


def narrow_by_context(
    candidates: list[Location], *, workspace: str | None, project: str | None
) -> Location | None:
    """Break an ambiguous locator by the current context, most-specific first (pure).

    A caller-side tiebreak over the same-tier *candidates* of an :class:`Ambiguous`
    result: keep the founding cascade pure and global, then let a command that has
    a current context (its active workspace and, from the cwd, its project) collapse
    a tie when that context names exactly one candidate. It never widens a result --
    only :class:`Ambiguous` candidates are passed in.

    :param candidates: the tied same-tier locations from an :class:`Ambiguous`.
    :param workspace: the current active workspace name, or ``None`` if none.
    :param project: the current project (from the cwd), or ``None`` if outside one.
    :returns: the sole candidate matching the current ``workspace`` *and* ``project``,
        else the sole one matching just the ``workspace``, else ``None`` (still
        ambiguous -- the caller reports it as before).

    The two stages mirror the tree top-down: the deepest context (workspace +
    project) is tried first, relaxing to the workspace alone only on a miss. A
    project leaf carries no project ancestor of its own, but the same filter still
    fits -- its ``project`` field is the leaf name, which equals the current project
    exactly when standing in it, so no tier special-casing is needed.
    """
    if workspace is not None and project is not None:
        hits = [c for c in candidates if c.workspace == workspace and c.project == project]
        if len(hits) == 1:
            return hits[0]

    if workspace is not None:
        hits = [c for c in candidates if c.workspace == workspace]
        if len(hits) == 1:
            return hits[0]

    return None


# --- helpers -----------------------------------------------------------------


def _tier_matches(tier: Tier, quals: list[str], leaf: str, index: LocationIndex) -> list[Location]:
    """Matches for *leaf* at a single tier, its ancestors qualified by *quals*.

    The one place a tier maps to its matcher, shared by the cascade and by a
    pinned :func:`locate` call so both read the tree the same way.
    """
    if tier is Tier.WORKTREE:
        return _worktree_matches(quals, leaf, index)
    if tier is Tier.PROJECT:
        return _project_matches(quals, leaf, index)

    return _workspace_matches(quals, leaf, index)


def _worktree_matches(quals: list[str], leaf: str, index: LocationIndex) -> list[Location]:
    """Worktrees named *leaf* whose ancestors match *quals* (project or workspace).

    Each leading qualifier must match the worktree's project *or* its workspace;
    a two-qualifier query pins both (in either order is rejected -- the tokens
    are top-down, so the workspace qualifier, if present, leads the project one).
    """
    return [wt for wt in index.worktrees if wt.name == leaf and _quals_match_worktree(quals, wt)]


def _quals_match_worktree(quals: list[str], wt: Location) -> bool:
    """Whether *quals* (0-2 leading tokens) address worktree *wt*'s ancestors.

    Zero qualifiers match anything. One matches the project or the workspace. Two
    are the full ``WORKSPACE PROJECT`` prefix (top-down order).
    """
    if len(quals) == 0:
        return True
    if len(quals) == 1:
        return quals[0] in (wt.project, wt.workspace)
    if len(quals) == 2:
        return quals[0] == wt.workspace and quals[1] == wt.project

    return False


def _project_matches(quals: list[str], leaf: str, index: LocationIndex) -> list[Location]:
    """Projects named *leaf* whose single leading qualifier (if any) is the workspace."""
    if len(quals) > 1:
        return []
    workspace = quals[0] if quals else None

    return [
        proj
        for proj in index.projects
        if proj.project == leaf and (workspace is None or proj.workspace == workspace)
    ]


def _workspace_matches(quals: list[str], leaf: str, index: LocationIndex) -> list[Location]:
    """The workspace named *leaf*; a workspace has no ancestor, so any qualifier misses."""
    if quals:
        return []

    return [
        Location(tier=Tier.WORKSPACE, workspace=name) for name in index.workspaces if name == leaf
    ]


def _finish(hits: list[Location]) -> Resolution:
    """Wrap a tier's matches: one is :class:`Resolved`, several :class:`Ambiguous`."""
    if len(hits) == 1:
        return Resolved(location=hits[0])

    return Ambiguous(candidates=hits)


def build_index(config: Config, *, include_config_only: bool = False) -> LocationIndex:
    """Enumerate the tree across every declared workspace, from the filesystem.

    The one config-bound edge of the locator. Deliberately git-free: projects are
    ``<root>/*`` dirs holding a ``.git``; worktrees are the dirs under a project's
    ``worktrees/`` that hold a ``.git`` (named relative to ``worktrees/``, so
    nesting is preserved). Lenient throughout -- an unreadable dir is skipped, not
    raised -- so shell completion never errors. An out-of-tree ``-d`` worktree is
    not indexed (it is reached by ``DIR``, unchanged).

    When *include_config_only* is set, the index also carries config-declared
    entities that are absent on disk: inert projects (declared in config but with
    no ``.git`` dir) and absent worktrees (declared in config but with no
    worktree dir). Dedup is structural (set subtraction), so an on-disk entity is
    never duplicated. The default (``False``) keeps the index disk-only -- the
    existence-bound commands resolve only what is actually there.
    """
    worktrees: list[Location] = []
    projects: list[Location] = []
    workspace_names: list[str] = []
    roots: dict[str, Path] = {}

    for ws_name, ws in sorted(config.workspaces.items()):
        workspace_names.append(ws_name)
        roots[ws_name] = ws.path
        on_disk_projects: set[str] = set()
        on_disk_worktrees: dict[str, set[str]] = {}
        for project_dir in _child_dirs(ws.path):
            if not (project_dir / ".git").is_dir():
                continue
            pname = project_dir.name
            on_disk_projects.add(pname)
            projects.append(Location(tier=Tier.PROJECT, workspace=ws_name, project=pname))
            worktrees_dir = project_dir / "worktrees"
            wt_names = _worktree_names(worktrees_dir)
            on_disk_worktrees[pname] = set(wt_names)
            for name in wt_names:
                worktrees.append(
                    Location(
                        tier=Tier.WORKTREE,
                        workspace=ws_name,
                        project=pname,
                        name=name,
                    )
                )

        if include_config_only:
            declared_projects = config.declared_project_names(ws)
            for pname in sorted(declared_projects - on_disk_projects):
                projects.append(Location(tier=Tier.PROJECT, workspace=ws_name, project=pname))
            for pname in sorted(on_disk_projects | declared_projects):
                for wt_name in sorted(
                    config.declared_worktree_names(ws, pname) - on_disk_worktrees.get(pname, set())
                ):
                    worktrees.append(
                        Location(
                            tier=Tier.WORKTREE,
                            workspace=ws_name,
                            project=pname,
                            name=wt_name,
                        )
                    )

    return LocationIndex(
        worktrees=worktrees, projects=projects, workspaces=workspace_names, roots=roots
    )


def _child_dirs(directory: Path) -> list[Path]:
    """Return *directory*'s immediate sub-directories, sorted; empty if unreadable."""
    try:
        return sorted(
            (entry for entry in directory.iterdir() if entry.is_dir()), key=lambda p: p.name
        )
    except OSError:
        return []


def _worktree_names(worktrees_dir: Path) -> list[str]:
    """Return the ``worktrees/``-relative names of every worktree under *worktrees_dir*.

    A worktree is any dir holding a ``.git`` (a linked worktree's ``.git`` is a
    file, a nested-name worktree the same). Recurses so a nested name like
    ``feature/x`` is found and returned whole; the recursion stops at a worktree
    root (a worktree never contains another).
    """
    names: list[str] = []
    _collect_worktrees(worktrees_dir, worktrees_dir, names)

    return sorted(names)


def _collect_worktrees(directory: Path, worktrees_dir: Path, out: list[str]) -> None:
    """Walk *directory* for worktree roots, appending each's relative name to *out*."""
    for entry in _child_dirs(directory):
        if (entry / ".git").exists():
            out.append(entry.relative_to(worktrees_dir).as_posix())
            continue
        _collect_worktrees(entry, worktrees_dir, out)
