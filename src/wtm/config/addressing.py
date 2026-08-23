"""Scope addressing for wtm config: scopes, tiers, addresses, and file paths.

A :class:`ConfigAddress` names one config file (a scope plus, for the local
tiers, the entity dir that pins it); a :class:`Key` names one slot inside it.
The system/user paths come from env/XDG and are read at call time.
"""

from __future__ import annotations

__all__ = [
    "ConfigAddress",
    "DepthTier",
    "Key",
    "Scope",
    "Settings",
]

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from wtm.exceptions import WtmError


class Scope(StrEnum):
    """A config/hook scope, from most general to most specific."""

    SYSTEM = "system"
    USER = "user"
    WORKSPACE = "workspace"
    PROJECT = "project"
    WORKTREE = "worktree"


class DepthTier(StrEnum):
    """The deepest tier rendered in a config slice (``-d`` flag).

    Maps to a depth integer: ``global``=0, ``workspace``=1, ``project``=2,
    ``worktree``=3. Caps the deepest tier rendered *anywhere* in the output.
    """

    GLOBAL = "global"
    WORKSPACE = "workspace"
    PROJECT = "project"
    WORKTREE = "worktree"

    @property
    def depth(self) -> int:
        """The integer depth this tier maps to (0-3)."""
        return _DEPTH_TIER_DEPTH[self]


_DEPTH_TIER_DEPTH = {
    DepthTier.GLOBAL: 0,
    DepthTier.WORKSPACE: 1,
    DepthTier.PROJECT: 2,
    DepthTier.WORKTREE: 3,
}

# String tier name -> depth for SliceTarget (no "global" key: global is None).
SLICE_TIER_DEPTH = {"workspace": 1, "project": 2, "worktree": 3}


# --- scope addressing --------------------------------------------------------


@dataclass(frozen=True)
class ConfigAddress:
    """A single config file, named by its scope (+ the entity dir when local).

    The system/user files come from env/XDG; the workspace/project/worktree
    files are all ``<entity_dir>/.wtm/config.toml`` -- the scope carries the
    semantic tier (used for provenance and the resolution cascades) while
    *entity_dir* pins the file. This is the address every read/write names.
    """

    scope: Scope
    entity_dir: Path | None = None

    @classmethod
    def system(cls) -> ConfigAddress:
        """The system-scope address (``$WTM_SYSTEM_CONFIG`` or ``/etc``)."""
        return cls(Scope.SYSTEM)

    @classmethod
    def user(cls) -> ConfigAddress:
        """The user-scope address (``$WTM_CONFIG`` or the XDG default)."""
        return cls(Scope.USER)

    @classmethod
    def workspace(cls, ws_dir: Path) -> ConfigAddress:
        """The workspace-local address for the workspace rooted at *ws_dir*."""
        return cls(Scope.WORKSPACE, ws_dir)

    @classmethod
    def project(cls, project_dir: Path) -> ConfigAddress:
        """The project-local address for the project at *project_dir*."""
        return cls(Scope.PROJECT, project_dir)

    @classmethod
    def worktree(cls, worktree_dir: Path) -> ConfigAddress:
        """The worktree-local address for the worktree at *worktree_dir*."""
        return cls(Scope.WORKTREE, worktree_dir)

    @property
    def file(self) -> Path:
        """The TOML file this address resolves to."""
        if self.scope is Scope.SYSTEM:
            return system_config_path()

        if self.scope is Scope.USER:
            return user_config_path()

        if self.entity_dir is None:
            raise WtmError(f"{self.scope} address needs an entity dir")

        return self.entity_dir / ".wtm" / "config.toml"


Key = str | tuple[str, ...]


# --- invocation settings -----------------------------------------------------


def _flag_env(name: str) -> bool:
    """Return whether an env var is set to a truthy value."""
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no")


@dataclass(frozen=True)
class Settings:
    """The load-time knobs of one config snapshot, explicit for embedders.

    ``Settings.from_env()`` resolves every knob from its environment variable
    (the CLI contract); a hand-built instance pins them for tests and
    embedding. Only *load-time* knobs live here: the system/user config paths
    stay environment-driven at call time everywhere (probes and addresses read
    them fresh), so passing explicit hook dirs or a selector cannot diverge
    from where reads actually land.
    """

    no_config: bool = False  # WTM_NO_CONFIG: empty reads, refused writes
    no_hooks: bool = False  # WTM_NO_HOOKS: fire nothing (builtin or inline)
    workspace_selector: str | None = None  # WTM_WORKSPACE
    system_hooks_dir: Path | None = None  # WTM_SYSTEM_HOOKS_DIR; None -> default
    user_hooks_dir: Path | None = None  # WTM_USER_HOOKS_DIR; None -> XDG default

    @classmethod
    def from_env(cls) -> Settings:
        """Resolve the settings from their environment variables."""
        return cls(
            no_config=_flag_env("WTM_NO_CONFIG"),
            no_hooks=_flag_env("WTM_NO_HOOKS"),
            workspace_selector=os.environ.get("WTM_WORKSPACE") or None,
            system_hooks_dir=Path(os.environ.get("WTM_SYSTEM_HOOKS_DIR") or "/etc/wtm/hooks"),
            user_hooks_dir=Path(os.environ.get("WTM_USER_HOOKS_DIR") or _default_user_hooks_dir()),
        )

    def resolved_system_hooks_dir(self) -> Path:
        """The system builtin-hook dir this setting names (default when unset)."""
        return (
            self.system_hooks_dir if self.system_hooks_dir is not None else Path("/etc/wtm/hooks")
        )

    def resolved_user_hooks_dir(self) -> Path:
        """The user builtin-hook dir this setting names (XDG default when unset)."""
        if self.user_hooks_dir is not None:
            return self.user_hooks_dir

        return _default_user_hooks_dir()


def _default_user_hooks_dir() -> Path:
    """The XDG-based user hooks dir used when neither env nor setting names one."""
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")

    return xdg / "wtm" / "hooks"


# --- config paths ------------------------------------------------------------


def system_config_path() -> Path:
    """The system config file path (env override or default)."""
    return Path(os.environ.get("WTM_SYSTEM_CONFIG") or "/etc/wtm/config.toml")


def user_config_path() -> Path:
    """The user config file path (env override or XDG default)."""
    if override := os.environ.get("WTM_CONFIG"):
        return Path(override)

    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")

    return xdg / "wtm" / "config.toml"


def global_address(scope: Scope) -> ConfigAddress:
    """The user/system ConfigAddress for *scope* (the two non-entity files)."""
    return ConfigAddress.system() if scope is Scope.SYSTEM else ConfigAddress.user()
