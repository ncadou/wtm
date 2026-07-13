"""Project-level operations for wtm: create, clone, and list projects.

CLI-agnostic orchestration over :mod:`wtm.git` and :mod:`wtm.core`. Pure of
presentation: methods return data (paths, name lists) and never print or log;
:mod:`wtm.cli` renders all output.
"""

from __future__ import annotations

__all__ = ["Project"]

from pathlib import Path

from wtm import git
from wtm.config import Config, resolve_project_dir
from wtm.core import WtmError, valid_project_name


class Project:
    """Project-level operations bound to a resolved :class:`Config`."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def new(self, name: str) -> Path:
        """Create an empty project (.git/ + worktrees/); return its directory."""
        valid_project_name(name)
        project_dir = self.config.projects_root / name
        if project_dir.exists():
            raise WtmError(f"project already exists: {project_dir}")
        _scaffold_project(project_dir)
        git.init(project_dir / ".git")

        return project_dir

    def clone(self, url: str, name: str | None) -> Path:
        """Clone *url* into a new project (bare); return its directory."""
        if not name:
            name = Path(url).name
            if name.endswith(".git"):
                name = name[: -len(".git")]
        valid_project_name(name)
        project_dir = self.config.projects_root / name
        if project_dir.exists():
            raise WtmError(f"project already exists: {project_dir}")
        _scaffold_project(project_dir)
        git.clone(url, project_dir / ".git")

        return project_dir

    def list(self) -> list[str]:
        """Return sorted project names under the projects root.

        Lenient by design: a missing projects root yields an empty list so
        shell completion never errors.
        """
        root = self.config.projects_root
        if not root.is_dir():
            return []

        return [
            entry.name
            for entry in sorted(root.iterdir(), key=lambda p: p.name)
            if entry.is_dir() and (entry / ".git").is_dir()
        ]

    def resolve_path(self, project: str | None) -> Path:
        """Return a project's computed directory (explicit name or cwd-inferred).

        Pure path math: it never checks that the project exists, so it works for
        a not-yet-created project (mirroring :meth:`Worktree.resolve_path`).
        """
        return resolve_project_dir(self.config, project)

    def resolve_env(self, project: str | None) -> dict[str, str]:
        """Return the project-level ``WTM_*`` contract (a subset of a worktree's).

        Every key is derived by path math from the resolved project dir, so no
        git query is needed: there is no ``WTM_WORKTREE_DIR`` / ``WTM_BRANCH``
        (a project has no single worktree or branch). The active workspace
        (name + dir) is folded in; the name is empty when the project is not
        under a declared workspace.
        """
        project_dir = resolve_project_dir(self.config, project)
        ws = self.config.workspace_of(project_dir)

        return {
            "WTM_PROJECT": project_dir.name,
            "WTM_PROJECT_DIR": str(project_dir),
            "WTM_WORKTREES_DIR": str(project_dir / "worktrees"),
            "WTM_GIT_COMMON_DIR": str(project_dir / ".git"),
            "WTM_WORKSPACE": ws.name if ws is not None else "",
            "WTM_WORKSPACE_DIR": str(ws.path if ws is not None else project_dir.parent),
        }


# --- helpers -----------------------------------------------------------------


def _scaffold_project(project_dir: Path) -> None:
    """Create the project's worktrees/ dir (and thus its parent)."""
    (project_dir / "worktrees").mkdir(parents=True, exist_ok=True)
