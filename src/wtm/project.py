"""Project-level operations for wtm: create, clone, and list projects.

CLI-agnostic orchestration over :mod:`wtm.git` and :mod:`wtm.core`. Pure of
presentation: methods return data (paths, name lists) and never print or log;
:mod:`wtm.cli` renders all output.
"""

from __future__ import annotations

__all__ = ["Project"]

from pathlib import Path

from wtm import git
from wtm.core import Config, WtmError, valid_project_name


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


# --- helpers -----------------------------------------------------------------


def _scaffold_project(project_dir: Path) -> None:
    """Create the project's worktrees/ dir (and thus its parent)."""
    (project_dir / "worktrees").mkdir(parents=True, exist_ok=True)
