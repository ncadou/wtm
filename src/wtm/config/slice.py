"""Slice building and serialization: the ``config show`` data model.

:func:`resolve_slice` materialises a global-rooted pruned config tree for a
target entity (or global), :func:`cwd_location` infers that target from the
cwd, and the discovery helpers union on-disk projects/worktrees with declared
ones. :func:`config_slice_json` serialises a slice as nested JSON -- the
machine-readable contract.
"""

from __future__ import annotations

__all__ = [
    "config_slice_json",
    "cwd_location",
    "declared_project_names",
    "declared_worktree_names",
    "project_names",
    "resolve_slice",
]

import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from wtm.config.addressing import SLICE_TIER_DEPTH
from wtm.config.engine import TierProbe, fold_tiers, merge_locality, read_table
from wtm.config.model import (
    ConfigSlice,
    ResolvedProject,
    ResolvedWorkspace,
    ResolvedWorktree,
    SliceTarget,
    Workspace,
)
from wtm.config.registry import global_probes
from wtm.core import abspath, current_dir, project_dir_for, within, worktree_name

if TYPE_CHECKING:
    from wtm.config.config import Config


def config_slice_json(slice: ConfigSlice) -> str:
    """Serialise a global-rooted pruned config slice as a nested JSON object.

    The machine-readable counterpart of the text slice renderer: always rooted
    at global (``default_base_branch`` / ``extra``), with workspaces, projects,
    and worktrees nested as pruned by the slice. Effective values only -- no
    provenance (``--origin``) and no hooks, per the JSON contract (``--json
    --origin``/``--json --hooks`` error).
    """
    root: dict[str, object] = {}
    if slice.default_base_branch:
        root["default_base_branch"] = slice.default_base_branch
    if slice.global_extra:
        root["extra"] = dict(sorted(slice.global_extra.items()))
    root["workspaces"] = {ws.entry.name: _workspace_node(ws) for ws in slice.workspaces}

    return json.dumps(root, default=str)


def _workspace_node(ws: ResolvedWorkspace) -> dict[str, object]:
    """One workspace object: scalars, extra, and its projects keyed by name."""
    node: dict[str, object] = {"name": ws.entry.name, "path": str(ws.entry.path)}
    if ws.entry.description:
        node["description"] = ws.entry.description
    if ws.entry.base_branch:
        node["base_branch"] = ws.entry.base_branch
    if ws.entry.extra:
        node["extra"] = dict(sorted(ws.entry.extra.items()))
    node["projects"] = {p.name: _project_node(p) for p in ws.projects}

    return node


def _project_node(project: ResolvedProject) -> dict[str, object]:
    """One project object: scalars, effective base_branch, extra, and worktrees."""
    node: dict[str, object] = {
        "name": project.name,
        "dir": str(project.dir),
        "exists": project.exists,
    }
    if project.description:
        node["description"] = project.description
    if project.effective_base_branch:
        node["base_branch"] = project.effective_base_branch
    if project.extra:
        node["extra"] = dict(sorted(project.extra.items()))
    node["worktrees"] = {w.name: _worktree_node(w) for w in project.worktrees}

    return node


def _worktree_node(worktree: ResolvedWorktree) -> dict[str, object]:
    """One worktree object: name, dir, description, extra (no base_branch tier)."""
    node: dict[str, object] = {"name": worktree.name, "dir": str(worktree.dir)}
    if worktree.description:
        node["description"] = worktree.description
    if worktree.extra:
        node["extra"] = dict(sorted(worktree.extra.items()))

    return node


# --- slice building ----------------------------------------------------------


def cwd_location(cfg: Config, *, cwd: Path | None = None) -> SliceTarget | None:
    """Infer the target entity from the cwd (worktree > project > workspace).

    Returns ``None`` when the cwd is outside every declared workspace (the
    global target). Otherwise the deepest entity the cwd sits inside: a
    worktree when inside one, else the cwd-bound project, else the active
    workspace. Returns a :class:`SliceTarget` for :func:`resolve_slice`.
    """
    cwd = cwd if cwd is not None else current_dir(resolve=False)
    active_ws = cfg.workspaces.get(cfg.active) if cfg.active is not None else None
    if active_ws is None or cwd is None:
        return None

    acwd = abspath(str(cwd))
    if not within(acwd, active_ws.path):
        return None

    project_dir = project_dir_for(active_ws.path, acwd)
    if project_dir is None:
        return SliceTarget(tier="workspace", workspace=active_ws.name)

    worktrees_dir = project_dir / "worktrees"
    if within(acwd, worktrees_dir) and acwd != worktrees_dir:
        rel = acwd.relative_to(worktrees_dir)
        wt_dir = worktrees_dir / rel.parts[0]
        name = worktree_name(wt_dir, worktrees_dir) or wt_dir.name

        return SliceTarget(
            tier="worktree", workspace=active_ws.name, project=project_dir.name, name=name
        )

    return SliceTarget(tier="project", workspace=active_ws.name, project=project_dir.name)


def resolve_slice(
    cfg: Config,
    *,
    target: SliceTarget | None,
    recurse: bool = False,
    depth_limit: int | None = None,
) -> ConfigSlice:
    """Materialise a global-rooted pruned config tree (the ``config show`` model).

    One branch from global to *target* is always rendered; with *recurse*
    the target's subtree expands downward. *depth_limit* (0=global,
    1=workspace, 2=project, 3=worktree) caps the deepest tier rendered
    anywhere; when ``None`` it defaults to the target's tier (branch only)
    or 3 (fullest descent with *recurse*). ``None`` *target* means global.
    """
    target_depth = 0 if target is None else SLICE_TIER_DEPTH[target.tier]
    if depth_limit is None:
        depth_limit = 3 if recurse else target_depth

    global_payload, global_hooks = merge_locality(cfg._store, global_probes())
    global_extra = {k: v for k, v in global_payload.items() if k != "base_branch"}
    bb_source = cfg.base_branch_source()
    # Global extra is locality-only (no ancestor to inherit from): each key's
    # chain is its fold over the user/system top-level probes (tier=None).
    global_tiered = [TierProbe(None, p) for p in global_probes()]
    global_extra_origins = {
        key: fold_tiers(cfg._store, global_tiered, key)[1] for key in global_extra
    }

    if depth_limit < 1:
        ws_entries: list[Workspace] = []
    elif target is None:
        ws_entries = [ws for _, ws in sorted(cfg.workspaces.items())]
    else:
        ws = cfg.workspaces.get(target.workspace)
        ws_entries = [ws] if ws is not None else []

    workspaces = tuple(
        _slice_workspace(cfg, ws_entry, target, recurse, depth_limit) for ws_entry in ws_entries
    )

    return ConfigSlice(
        default_base_branch=cfg.default_base_branch,
        base_branch_source=bb_source.value if bb_source is not None else None,
        global_extra=global_extra,
        global_hooks=global_hooks,
        workspaces=workspaces,
        depth_limit=depth_limit,
        global_extra_origins=global_extra_origins,
    )


def _slice_workspace(
    cfg: Config, ws: Workspace, target: SliceTarget | None, recurse: bool, depth_limit: int
) -> ResolvedWorkspace:
    """Fill one workspace node with the pruned set of projects for the slice."""
    bb_source = cfg.workspace_base_branch_source(ws)
    extra_origins = cfg._extra_origins_for(cfg.resolve_generic_target_for(ws.name), ws.extra)
    if depth_limit < 2:
        return ResolvedWorkspace(
            entry=ws, projects=(), base_branch_source=bb_source, extra_origins=extra_origins
        )

    if (
        target is not None
        and target.tier in ("project", "worktree")
        and target.workspace == ws.name
    ):
        project_names_ = [target.project]
    elif recurse and (target is None or target.tier == "workspace"):
        project_names_ = sorted(project_names(cfg, ws))
    elif recurse and target is not None and target.tier in ("project", "worktree"):
        project_names_ = [target.project] if target.workspace == ws.name else []
    else:
        project_names_ = []

    projects = tuple(
        _slice_project(cfg, ws, pname, target, recurse, depth_limit) for pname in project_names_
    )

    return ResolvedWorkspace(
        entry=ws, projects=projects, base_branch_source=bb_source, extra_origins=extra_origins
    )


def _slice_project(
    cfg: Config,
    ws: Workspace,
    pname: str,
    target: SliceTarget | None,
    recurse: bool,
    depth_limit: int,
) -> ResolvedProject:
    """Fill one project node with the pruned set of worktrees for the slice."""
    project_dir = ws.path / pname
    annotation = cfg.project_annotation(ws, pname)
    effective, source = cfg.base_branch_provenance(ws, pname)
    extra_origins = cfg._extra_origins_for(
        cfg.resolve_generic_target_for(ws.name, pname), annotation.extra
    )

    if depth_limit < 3:
        worktrees: tuple[ResolvedWorktree, ...] = ()
    elif (
        target is not None
        and target.tier == "worktree"
        and target.workspace == ws.name
        and target.project == pname
    ):
        worktrees = (_worktree_with_origins(cfg, ws.name, pname, project_dir, target.name),)
    elif recurse and (target is None or target.tier in ("workspace", "project")):
        wt_names = sorted(_worktree_names(cfg, project_dir, pname, ws))
        worktrees = tuple(
            _worktree_with_origins(cfg, ws.name, pname, project_dir, n) for n in wt_names
        )
    else:
        worktrees = ()

    return ResolvedProject(
        name=pname,
        dir=project_dir,
        exists=(project_dir / ".git").is_dir(),
        description=annotation.description,
        base_branch=annotation.base_branch,
        effective_base_branch=effective,
        base_branch_source=source,
        hooks=annotation.hooks,
        extra=annotation.extra,
        extra_origins=extra_origins,
        worktrees=worktrees,
    )


def _worktree_with_origins(
    cfg: Config, ws_name: str, pname: str, project_dir: Path, name: str
) -> ResolvedWorktree:
    """A slice worktree node with its ``extra`` origin chains attached."""
    worktree = cfg.resolve_worktree(project_dir, name)
    target = cfg.resolve_generic_target_for(ws_name, pname, name)

    return replace(worktree, extra_origins=cfg._extra_origins_for(target, worktree.extra))


# --- discovery ---------------------------------------------------------------


def project_names(cfg: Config, ws: Workspace) -> set[str]:
    """Projects under *ws*: on-disk ``<name>/.git`` dirs unioned with declared.

    The discovery basis of a tree dump (and ``config show``'s workspace
    listing): on-disk projects unioned with :func:`declared_project_names`,
    so annotated-but-inert projects still surface.
    """
    on_disk = {
        entry.name
        for entry in (ws.path.iterdir() if ws.path.is_dir() else [])
        if entry.is_dir() and (entry / ".git").is_dir()
    }

    return on_disk | declared_project_names(cfg, ws)


def declared_project_names(cfg: Config, ws: Workspace) -> set[str]:
    """Return the project names *declared* for *workspace* across its files.

    The keys of every ``projects`` table along the workspace probe cascade
    (workspace-local top-level, user/system ``[workspaces.NAME.projects]``).
    A *declaration* listing -- distinct from on-disk discovery -- so ``config
    show`` can surface annotated-but-inert projects.
    """
    names: set[str] = set()
    for probe in cfg._workspace_probes(ws):
        table = read_table(cfg._store, probe)
        projects = table.get("projects")
        if isinstance(projects, dict):
            names.update(projects)

    return names


def declared_worktree_names(cfg: Config, ws: Workspace, project: str) -> set[str]:
    """Return the worktree names *declared* for a project across its files.

    The keys of every ``worktrees`` table along the project probe cascade
    (project-local top-level, workspace ``[projects.P.worktrees]``,
    user/system ``[workspaces.W.projects.P.worktrees]``). A *declaration*
    listing -- the worktree-level analog of :func:`declared_project_names`,
    so a tree dump can surface annotated-but-inert worktrees.
    """
    names: set[str] = set()
    for probe in cfg._project_probes(ws, project):
        table = read_table(cfg._store, probe)
        worktrees = table.get("worktrees")
        if isinstance(worktrees, dict):
            names.update(worktrees)

    return names


def _worktree_names(cfg: Config, project_dir: Path, name: str, ws: Workspace) -> set[str]:
    """Worktrees under a project: on-disk ``worktrees/*`` dirs unioned with declared."""
    worktrees_dir = project_dir / "worktrees"
    on_disk = {
        entry.name
        for entry in (worktrees_dir.iterdir() if worktrees_dir.is_dir() else [])
        if entry.is_dir()
    }

    return on_disk | declared_worktree_names(cfg, ws, name)
