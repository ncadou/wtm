"""Tests for the library-facing API additions: Settings, reload, placement policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from wtm.config import (
    PROJECT_WRITE_SCOPES,
    WORKSPACE_WRITE_SCOPES,
    WORKTREE_WRITE_SCOPES,
    Scope,
    Settings,
    check_write_scope,
    project_default_scope,
    validate_arbitrary_key,
    workspace_default_scope,
    worktree_default_scope,
)
from wtm.config.addressing import ConfigAddress
from wtm.config.config import Config
from wtm.exceptions import (
    KindConflictError,
    NoActiveWorkspaceError,
    ReservedKeyError,
    WriteProhibitedError,
)


def _isolate(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    """Point wtm at a throwaway sole-workspace config under *root*."""
    root.mkdir(parents=True, exist_ok=True)
    config = root / "_wtm_config.toml"
    config.write_text(f'[workspaces.ws]\npath = "{root}"\n')
    monkeypatch.setenv("WTM_CONFIG", str(config))
    monkeypatch.setenv("WTM_SYSTEM_CONFIG", str(root / "_no_system.toml"))
    return config


# --- Settings -----------------------------------------------------------------


class TestSettingsFromEnv:
    def test_no_config_truthy_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for raw in ("1", "yes", "TRUE", " true "):
            monkeypatch.setenv("WTM_NO_CONFIG", raw)
            assert Settings.from_env().no_config is True

    def test_no_config_falsy_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for raw in ("", "0", "false", "no"):
            monkeypatch.setenv("WTM_NO_CONFIG", raw)
            assert Settings.from_env().no_config is False

    def test_workspace_selector(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WTM_WORKSPACE", raising=False)
        assert Settings.from_env().workspace_selector is None
        monkeypatch.setenv("WTM_WORKSPACE", "ws")
        assert Settings.from_env().workspace_selector == "ws"

    def test_hooks_dirs(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("WTM_SYSTEM_HOOKS_DIR", str(tmp_path / "sys"))
        monkeypatch.setenv("WTM_USER_HOOKS_DIR", str(tmp_path / "usr"))
        settings = Settings.from_env()
        assert settings.system_hooks_dir == tmp_path / "sys"
        assert settings.user_hooks_dir == tmp_path / "usr"

    def test_explicit_settings_need_no_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("WTM_SYSTEM_HOOKS_DIR", str(tmp_path / "from-env"))
        settings = Settings(system_hooks_dir=tmp_path / "explicit")
        assert settings.resolved_system_hooks_dir() == tmp_path / "explicit"


# --- explicit construction ----------------------------------------------------


class TestExplicitSettings:
    def test_no_config_reads_empty_and_refuses_writes(self, tmp_path: Path) -> None:
        target = tmp_path / "config.toml"
        target.write_text("[workspaces.ws]\npath = '/tmp'\n")
        # Bypass the env entirely: point the address at a real file but load
        # with no_config -- reads are empty and writes are refused.
        config = Config.load(settings=Settings(no_config=True))
        assert config.workspaces == {}
        with pytest.raises(WriteProhibitedError):
            config.set_value(ConfigAddress.user(), "some_key", "v")

    def test_no_active_workspace_raises_subclass(self) -> None:
        config = Config.load(settings=Settings(no_config=True))
        with pytest.raises(NoActiveWorkspaceError):
            _ = config.active_workspace


# --- reload -------------------------------------------------------------------


class TestReload:
    def test_reload_sees_external_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = _isolate(monkeypatch, tmp_path)
        config = Config.load()
        assert "added" not in config.workspaces

        with config_path.open("a") as handle:
            handle.write('\n[workspaces.added]\npath = "/tmp/added"\n')
        reloaded = config.reload()

        assert "added" in reloaded.workspaces
        assert "added" not in config.workspaces  # the old snapshot is untouched

    def test_reload_keeps_settings(self, tmp_path: Path) -> None:
        pinned = Settings(no_config=True)
        config = Config.load(settings=pinned)
        assert config.reload()._settings is pinned


# --- placement policy ---------------------------------------------------------


class TestPlacementPolicy:
    def test_project_default_scope(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        assert project_default_scope(tmp_path / "missing", ws) == Scope.WORKSPACE
        project = ws / "proj"
        project.mkdir()
        assert project_default_scope(project, ws) == Scope.PROJECT
        assert project_default_scope(tmp_path / "missing", tmp_path / "missing-ws") == Scope.USER

    def test_workspace_default_scope(self, tmp_path: Path) -> None:
        assert workspace_default_scope(tmp_path) == Scope.WORKSPACE
        assert workspace_default_scope(tmp_path / "missing") == Scope.USER

    def test_worktree_default_scope(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        project = ws / "proj"
        wt = project / "worktrees" / "feat"
        wt.mkdir(parents=True)
        assert worktree_default_scope(wt, project, ws) == Scope.WORKTREE
        assert worktree_default_scope(wt.parent / "gone", project, ws) == Scope.PROJECT
        assert worktree_default_scope(wt.parent / "gone", project / "g", ws) == Scope.WORKSPACE
        assert worktree_default_scope(wt.parent / "gone", project / "g", ws / "g") == Scope.USER

    def test_check_write_scope_message_parity(self) -> None:
        from wtm.exceptions import WtmError

        with pytest.raises(WtmError) as exc:
            check_write_scope(Scope.PROJECT, WORKSPACE_WRITE_SCOPES, "workspace")
        assert "cannot write a workspace annotation at 'project' scope" in str(exc.value)

        check_write_scope(Scope.SYSTEM, PROJECT_WRITE_SCOPES, "project")  # allowed
        check_write_scope(Scope.WORKTREE, WORKTREE_WRITE_SCOPES, "worktree")  # allowed


# --- error subclasses ---------------------------------------------------------


class TestErrorSubclasses:
    def test_reserved_key_error(self) -> None:
        with pytest.raises(ReservedKeyError):
            validate_arbitrary_key("hooks")

    def test_kind_conflict_error(self) -> None:
        from wtm.config.model import check_kind

        with pytest.raises(KindConflictError):
            check_kind("k", "scalar", {"table": 1}, Path("/f"))
