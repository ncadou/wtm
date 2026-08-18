"""Project-level operations for wtm: create, clone, and list projects.

CLI-agnostic orchestration over :mod:`wtm.git` and :mod:`wtm.core`. Pure of
presentation: methods return data (paths, name lists) and never print or log;
:mod:`wtm.cli` renders all output.
"""

from __future__ import annotations

__all__ = ["Project", "ProjectEntered", "ProjectLeft", "ProjectSet", "ProjectShown"]

import os
from dataclasses import dataclass
from pathlib import Path

from wtm import git
from wtm.config import Config, ConfigAddress, ProjectAnnotation, Scope, resolve_project_dir
from wtm.core import (
    WtmError,
    current_dir,
    leave_landing,
    valid_project_name,
    within,
)
from wtm.hooks import run_project_hooks
from wtm.worktree import Worktree


@dataclass(frozen=True)
class ProjectEntered:
    """Result of :meth:`Project.enter`: the project dir to cd into."""

    name: str
    dir: Path


@dataclass(frozen=True)
class ProjectLeft:
    """Result of :meth:`Project.leave`: where to land, and whether a cascade fired."""

    name: str
    cd_dir: Path | None  # None means "stay put"
    cascaded: bool  # a worktree 'leave' fired first


@dataclass(frozen=True)
class ProjectSet:
    """Result of :meth:`Project.set`: what was edited and where."""

    name: str
    dir: Path
    local: bool  # True: wrote <project>/.wtm/config.toml; False: the registry annotation


@dataclass(frozen=True)
class ProjectShown:
    """Result of :meth:`Project.show`: a read-only per-project detail view."""

    name: str
    dir: Path
    exists: bool
    description: str | None
    base_branch: str | None  # the project-tier annotation value, if any
    effective_base_branch: str | None  # after the full resolution chain
    base_branch_source: str  # project | workspace | global | (none)
    worktree_count: int


class Project:
    """Project-level operations bound to a resolved :class:`Config`."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def new(self, workspace: str | None, name: str) -> Path:
        """Create an empty project (.git/ + worktrees/); return its directory."""
        valid_project_name(name)
        project_dir = self.config.workspace_root(workspace) / name
        if project_dir.exists():
            raise WtmError(f"project already exists: {project_dir}")
        _scaffold_project(project_dir)
        git.init(project_dir / ".git")

        return project_dir

    def clone(self, url: str, workspace: str | None, name: str | None) -> Path:
        """Clone *url* into a new project (bare); return its directory."""
        if not name:
            name = Path(url).name
            if name.endswith(".git"):
                name = name[: -len(".git")]
        valid_project_name(name)
        project_dir = self.config.workspace_root(workspace) / name
        if project_dir.exists():
            raise WtmError(f"project already exists: {project_dir}")
        _scaffold_project(project_dir)
        git.clone(url, project_dir / ".git")

        return project_dir

    def list(self, workspace: str | None = None) -> list[str]:
        """Return sorted project names under a workspace's projects root.

        A named *workspace* is looked up in the registry; ``None`` uses the
        active workspace. Lenient by design: a missing projects root yields an
        empty list so shell completion never errors.
        """
        try:
            root = self.config.workspace_root(workspace)
        except WtmError:
            return []
        if not root.is_dir():
            return []

        return [
            entry.name
            for entry in sorted(root.iterdir(), key=lambda p: p.name)
            if entry.is_dir() and (entry / ".git").is_dir()
        ]

    def resolve_path(self, workspace: str | None, project: str | None) -> Path:
        """Return a project's computed directory (explicit name or cwd-inferred).

        Pure path math: it never checks that the project exists, so it works for
        a not-yet-created project (mirroring :meth:`Worktree.resolve_path`).
        """
        return resolve_project_dir(self.config, workspace, project)

    def resolve_env(self, workspace: str | None, project: str | None) -> dict[str, str]:
        """Return the project-level ``WTM_*`` contract (a subset of a worktree's).

        Every key is derived by path math from the resolved project dir, so no
        git query is needed: there is no ``WTM_WORKTREE_DIR`` / ``WTM_BRANCH``
        (a project has no single worktree or branch). The active workspace
        (name + dir) is folded in; the name is empty when the project is not
        under a declared workspace.
        """
        project_dir = resolve_project_dir(self.config, workspace, project)
        ws = self.config.workspace_of(project_dir)

        return {
            "WTM_PROJECT": project_dir.name,
            "WTM_PROJECT_DIR": str(project_dir),
            "WTM_WORKTREES_DIR": str(project_dir / "worktrees"),
            "WTM_GIT_COMMON_DIR": str(project_dir / ".git"),
            "WTM_WORKSPACE": ws.name if ws is not None else "",
            "WTM_WORKSPACE_DIR": str(ws.path if ws is not None else project_dir.parent),
        }

    # --- annotation ----------------------------------------------------------

    def set(
        self,
        workspace: str | None,
        project: str | None,
        *,
        base_branch: str | None,
        description: str | None,
    ) -> ProjectSet:
        """Edit a project's annotation scalars (base_branch / description).

        Writes the project-local ``<project>/.wtm/config.toml`` ``[project]``
        table when the project dir exists, else falls back to the user-scope
        registry annotation ``[workspaces.WS.projects.PROJECT]`` -- the most-local
        home that exists. The fallback requires the containing workspace to be
        declared at user scope. A field passed as ``""`` clears it; at least one
        field must be given.
        """
        if base_branch is None and description is None:
            raise WtmError("nothing to set (pass --base-branch or --description)")

        project_dir = resolve_project_dir(self.config, workspace, project)
        fields = (("description", description), ("base_branch", base_branch))

        if project_dir.is_dir():
            address = ConfigAddress.project(project_dir)
            for key, value in fields:
                if value is None:
                    continue
                if value == "":
                    self.config.unset_value(address, ("project", key))
                else:
                    self.config.set_value(address, ("project", key), value)

            return ProjectSet(name=project_dir.name, dir=project_dir, local=True)

        ws = self.config.workspace_of(project_dir)
        if ws is None:
            raise WtmError(
                f"project dir does not exist and is not under a declared workspace: {project_dir}"
            )
        if Scope.USER not in self.config.declaring_scopes(ws.name):
            raise WtmError(
                f"project dir does not exist and workspace '{ws.name}' is not "
                "declared at user scope; create the project dir first"
            )
        for key, value in fields:
            if value is not None:
                self.config.set_project_annotation(ws.name, project_dir.name, key, value or None)

        return ProjectSet(name=project_dir.name, dir=project_dir, local=False)

    def show(self, workspace: str | None, project: str | None) -> ProjectShown:
        """Return a read-only detail view for a project (its resolved annotation)."""
        project_dir = resolve_project_dir(self.config, workspace, project)
        ws = (
            self.config.workspaces.get(workspace)
            if workspace is not None
            else self.config.workspace_of(project_dir)
        )
        annotation = (
            self.config.project_annotation(ws, project_dir.name)
            if ws is not None
            else ProjectAnnotation()
        )
        effective, source = self.config.base_branch_provenance(ws, project_dir.name)

        return ProjectShown(
            name=project_dir.name,
            dir=project_dir,
            exists=(project_dir / ".git").is_dir(),
            description=annotation.description,
            base_branch=annotation.base_branch,
            effective_base_branch=effective,
            base_branch_source=source,
            worktree_count=len(self._worktrees(workspace, project)),
        )

    def _worktrees(self, workspace: str | None, project: str | None) -> list:
        """Lenient worktree listing for the count (empty on any git failure)."""
        try:
            return Worktree(self.config).list(workspace, project, strict=False)
        except (WtmError, git.GitError):
            return []

    # --- lifecycle -----------------------------------------------------------

    def enter(self, workspace: str | None, project: str | None, *, strict: bool) -> ProjectEntered:
        """cd into a project's directory, firing ``project-enter`` hooks.

        The middle tier between ``workspace enter`` and ``worktree enter``: it
        lands in ``<workspace>/<project>`` (beside ``.git`` and ``worktrees/``)
        and exports the project-level contract, pinning the active workspace.
        The project must exist on disk (its ``.git`` is where project-scope
        hooks live).
        """
        project_dir = resolve_project_dir(self.config, workspace, project)
        if not (project_dir / ".git").is_dir():
            raise WtmError(f"project git dir not found: {project_dir / '.git'}")
        run_project_hooks("project-enter", project_dir, self.config, strict=strict)

        return ProjectEntered(name=project_dir.name, dir=project_dir)

    def leave(self, workspace: str | None, project: str | None, *, strict: bool) -> ProjectLeft:
        """Fire ``project-leave`` hooks; cascade the worktree ``leave`` when inside one.

        When cwd is inside a worktree of the target project, the worktree's
        ``leave`` event fires first (worktree->project teardown order), then
        ``project-leave``. The single cd-out lands at ``$OLDPWD`` if it is
        outside the project tree, else (having cascaded out of a worktree) the
        projects root, else stays put. An explicit PROJECT you are not standing
        in fires only ``project-leave`` and never cds.
        """
        explicit = project is not None or workspace is not None
        project_dir = resolve_project_dir(self.config, workspace, project)
        root = Path(os.path.realpath(project_dir))
        cwd = current_dir()
        inside = cwd is not None and within(cwd, root)

        # Cascade only when standing in an actual worktree (under worktrees/),
        # never the bare project dir itself.
        cascaded = False
        if inside and within(cwd, root / "worktrees") and git.is_repo(cwd):
            try:
                Worktree(self.config).leave([], strict=strict)
                cascaded = True
            except (WtmError, git.GitError):
                cascaded = False

        run_project_hooks("project-leave", project_dir, self.config, strict=strict)

        cd_dir = leave_landing(root, inside=inside, explicit=explicit, cascaded=cascaded)

        return ProjectLeft(name=project_dir.name, cd_dir=cd_dir, cascaded=cascaded)


# --- helpers -----------------------------------------------------------------


def _scaffold_project(project_dir: Path) -> None:
    """Create the project's worktrees/ dir (and thus its parent)."""
    (project_dir / "worktrees").mkdir(parents=True, exist_ok=True)
