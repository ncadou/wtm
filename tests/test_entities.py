"""Tests for the entity-handle layer (Config.entity and its tiered handles)."""

from __future__ import annotations

from pathlib import Path

import pytest

from wtm.config import (
    Config,
    GlobalEntity,
    ProjectEntity,
    Scope,
    WorkspaceEntity,
    WorktreeEntity,
)
from wtm.exceptions import WtmError


def _make_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create a minimal workspace/project/worktree directory tree."""
    ws = tmp_path / "ws"
    project = ws / "alpha"
    wt = project / "worktrees" / "feat"
    wt.mkdir(parents=True)

    return ws, project, wt


def _use_config(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Point wtm at a sole-workspace config whose projects root is *root*/ws."""
    config = root / "_wtm_config.toml"
    config.write_text(f'[workspaces.ws]\npath = "{root / "ws"}"\n')
    monkeypatch.setenv("WTM_CONFIG", str(config))
    monkeypatch.setenv("WTM_SYSTEM_CONFIG", str(root / "_no_system.toml"))
    monkeypatch.delenv("WTM_WORKSPACE", raising=False)


# --- dispatcher ---------------------------------------------------------------


class TestDispatch:
    def test_explicit_tiers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _use_config(monkeypatch, tmp_path)
        config = Config.load()

        assert isinstance(config.entity("ws"), WorkspaceEntity)
        assert isinstance(config.entity("ws", "alpha"), ProjectEntity)
        assert isinstance(config.entity("ws", "alpha", "feat"), WorktreeEntity)

    def test_unknown_workspace_is_a_wtm_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _use_config(monkeypatch, tmp_path)
        config = Config.load()
        with pytest.raises(WtmError):
            config.entity("nope")

    def test_cwd_infers_deepest_entity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws, project, wt = _make_tree(tmp_path)
        _use_config(monkeypatch, tmp_path)
        config = Config.load()

        monkeypatch.chdir(wt)
        assert isinstance(config.entity(), WorktreeEntity)
        monkeypatch.chdir(project)
        assert isinstance(config.entity(), ProjectEntity)
        monkeypatch.chdir(ws)
        assert isinstance(config.entity(), WorkspaceEntity)

    def test_cwd_outside_every_workspace_is_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _use_config(monkeypatch, tmp_path)
        config = Config.load()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        monkeypatch.chdir(elsewhere)
        assert isinstance(config.entity(), GlobalEntity)


# --- reads and writes ---------------------------------------------------------


class TestHandleOps:
    def test_set_get_roundtrip_at_worktree_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, _, wt = _make_tree(tmp_path)
        _use_config(monkeypatch, tmp_path)
        config = Config.load()
        svc = config.entity("ws", "alpha", "feat")

        address = svc.set("wtm-server.api.port", "8081")
        assert address.file == wt / ".wtm" / "config.toml"
        # A dotted scalar write nests as TOML sections, like the CLI's rendering.
        assert '[wtm-server.api]\nport = "8081"' in address.file.read_text()
        assert svc.get("wtm-server.api.port") == "8081"

    def test_placement_default_lands_project_local(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, project, _ = _make_tree(tmp_path)
        _use_config(monkeypatch, tmp_path)
        config = Config.load()
        proj = config.entity("ws", "alpha")

        address = proj.set("description-ish", "x")
        assert address.file == project / ".wtm" / "config.toml"

    def test_table_ops_and_whole_key_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_tree(tmp_path)
        _use_config(monkeypatch, tmp_path)
        svc = Config.load().entity("ws", "alpha", "feat")

        # table_set mirrors the CLI's KEY SUBKEY VALUE -t: one cell at a time.
        svc.table_set("wtm-server", "port", "8081")
        assert svc.get("wtm-server.port") == "8081"
        svc.table_unset("wtm-server", "port")
        assert svc.get("wtm-server.port") is None

        svc.set("wtm-server.api.port", "8082")
        svc.unset("wtm-server")
        assert svc.get("wtm-server") is None
        assert svc.get("wtm-server.api.port") is None

    def test_scope_refusal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _make_tree(tmp_path)
        _use_config(monkeypatch, tmp_path)
        proj = Config.load().entity("ws", "alpha")

        with pytest.raises(WtmError):
            proj.set("k", "v", scope=Scope.WORKTREE)


# --- inheritance and inheritable ----------------------------------------------


class TestInheritance:
    def test_keys_and_get_see_inherited_base_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_tree(tmp_path)
        _use_config(monkeypatch, tmp_path)
        config_path = tmp_path / "_wtm_config.toml"
        with config_path.open("a") as handle:
            handle.write('base_branch = "mainline"\n')
        config = Config.load()
        feat = config.entity("ws", "alpha", "feat")

        assert feat.get("base_branch") == "mainline"
        assert "base_branch" in feat.keys()

    def test_inheritable_add_refuses_non_inheritable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_tree(tmp_path)
        _use_config(monkeypatch, tmp_path)
        proj = Config.load().entity("ws", "alpha")

        with pytest.raises(WtmError):
            proj.inheritable_add("description")

    def test_inheritable_add_descends_to_worktree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_tree(tmp_path)
        _use_config(monkeypatch, tmp_path)
        config = Config.load()
        proj = config.entity("ws", "alpha")
        feat = config.entity("ws", "alpha", "feat")

        address = proj.inheritable_add("wtm-server")
        assert "inheritable" in address.file.read_text()
        proj.set("wtm-server.api.http", "true")

        # Handles bind their creation-time cascade (the Config snapshot
        # contract), so a declaration written after binding needs a rebind.
        feat = config.entity("ws", "alpha", "feat")

        # The flag descends: the worktree sees it through its cascade.
        assert feat.get("wtm-server.api.http") == "true"


# --- typed accessors ----------------------------------------------------------


class TestTypedAccessors:
    def test_project_annotation_and_provenance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_tree(tmp_path)
        _use_config(monkeypatch, tmp_path)
        config = Config.load()
        proj = config.entity("ws", "alpha")
        proj.set("base_branch", "trunk")

        annotation = proj.annotation()
        assert annotation.base_branch == "trunk"
        value, source = proj.base_branch_provenance()
        assert (value, source) == ("trunk", "project")

    def test_worktree_annotation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _make_tree(tmp_path)
        _use_config(monkeypatch, tmp_path)
        config = Config.load()
        feat = config.entity("ws", "alpha", "feat")
        feat.set("description", "the feature worktree")

        assert feat.annotation().description == "the feature worktree"
