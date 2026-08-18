"""Pure, subprocess-free tests for the layered config (:mod:`wtm.config`).

Config resolution -- the workspace registry, active-workspace precedence,
per-scope hook merging, and base-branch lookup -- is exercised directly against
:meth:`Config.load`, driven by TOML files under ``tmp_path`` and env vars, with
no ``wtm`` invocation and no git.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wtm.config import Config, Scope
from wtm.core import WtmError


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read the host's real config/env during a unit test."""
    for var in ("WTM_CONFIG", "WTM_SYSTEM_CONFIG", "WTM_WORKSPACE", "WTM_NO_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("WTM_NO_SYSTEM", "1")


def _write(path: Path, body: str) -> Path:
    path.write_text(body)

    return path


def _load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str, *, cwd: Path, **kw
) -> Config:
    """Write *body* as the user config and load it with *cwd* (and optional flag)."""
    monkeypatch.setenv("WTM_CONFIG", str(_write(tmp_path / "c.toml", body)))

    return Config.load(cwd=cwd, **kw)


# --- workspace registry ------------------------------------------------------


def test_no_workspaces_declared_errors_on_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _load(monkeypatch, tmp_path, "", cwd=tmp_path)
    assert cfg.workspaces == {}
    with pytest.raises(WtmError, match="no workspaces declared"):
        _ = cfg.projects_root


def test_sole_workspace_is_active(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    cfg = _load(monkeypatch, tmp_path, f'[workspaces.a]\npath="{ws}"\n', cwd=tmp_path)
    assert cfg.active == "a"
    assert cfg.projects_root == ws


def test_path_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MYROOT", str(tmp_path / "expanded"))
    cfg = _load(monkeypatch, tmp_path, '[workspaces.a]\npath="$MYROOT/sub"\n', cwd=tmp_path)
    assert cfg.workspaces["a"].path == tmp_path / "expanded" / "sub"


def test_relative_path_after_expansion_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(WtmError, match="must be absolute"):
        _load(monkeypatch, tmp_path, '[workspaces.a]\npath="rel/dir"\n', cwd=tmp_path)


# --- active-workspace precedence ---------------------------------------------


def _two(tmp_path: Path) -> str:
    """Two declared workspaces A and B (dirs created); return the config body."""
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()

    return f'[workspaces.A]\npath="{a}"\n[workspaces.B]\npath="{b}"\n'


def test_active_none_when_ambiguous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _load(monkeypatch, tmp_path, _two(tmp_path), cwd=tmp_path)
    assert cfg.active is None
    with pytest.raises(WtmError, match="no active workspace"):
        _ = cfg.projects_root


def test_env_beats_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WTM_WORKSPACE", "A")
    cfg = _load(monkeypatch, tmp_path, _two(tmp_path), cwd=tmp_path / "B")
    assert cfg.active == "A"


def test_cwd_longest_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    body = f'[workspaces.outer]\npath="{outer}"\n[workspaces.inner]\npath="{inner}"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=inner / "proj" / "worktrees" / "x")
    assert cfg.active == "inner"


def test_cwd_identical_path_ambiguous_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "shared"
    ws.mkdir()
    body = f'[workspaces.A]\npath="{ws}"\n[workspaces.B]\npath="{ws}"\n'
    with pytest.raises(WtmError, match="matches multiple workspaces"):
        _load(monkeypatch, tmp_path, body, cwd=ws)


def test_default_workspace_used_when_cwd_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    body = f'default_workspace="B"\n[workspaces.A]\npath="{a}"\n[workspaces.B]\npath="{b}"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=tmp_path)
    assert cfg.active == "B"


def test_unknown_selected_workspace_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WTM_WORKSPACE", "Z")
    with pytest.raises(WtmError, match="unknown workspace"):
        _load(monkeypatch, tmp_path, _two(tmp_path), cwd=tmp_path)


# --- system + user registry merge --------------------------------------------


def test_user_wins_and_hooks_accumulate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    system = _write(
        tmp_path / "sys.toml",
        f'[workspaces.a]\npath="{ws}"\ndescription="sys"\n'
        '[workspaces.a.hooks.on]\nenter="sys-cmd"\n',
    )
    user = _write(
        tmp_path / "usr.toml",
        '[workspaces.a]\ndescription="usr"\n[workspaces.a.hooks.on]\nenter="usr-cmd"\n',
    )
    monkeypatch.delenv("WTM_NO_SYSTEM", raising=False)
    monkeypatch.setenv("WTM_SYSTEM_CONFIG", str(system))
    monkeypatch.setenv("WTM_CONFIG", str(user))
    cfg = Config.load(cwd=tmp_path)
    workspace = cfg.workspaces["a"]
    assert workspace.description == "usr"  # user scalar wins
    assert workspace.scope is Scope.USER
    assert workspace.hooks.on["enter"] == ["sys-cmd", "usr-cmd"]  # accumulate, sys first


def test_no_system_suppresses_system_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    system = _write(tmp_path / "sys.toml", f'[workspaces.sysonly]\npath="{ws}"\n')
    monkeypatch.setenv("WTM_SYSTEM_CONFIG", str(system))
    monkeypatch.setenv("WTM_NO_SYSTEM", "1")
    cfg = _load(monkeypatch, tmp_path, "", cwd=tmp_path)
    assert "sysonly" not in cfg.workspaces


# --- hooks & builtins toggle -------------------------------------------------


def test_hook_command_string_or_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    body = (
        f'[workspaces.a]\npath="{ws}"\n'
        '[workspaces.a.hooks.on]\nenter="one"\npost-create=["two","three"]\n'
    )
    cfg = _load(monkeypatch, tmp_path, body, cwd=tmp_path)
    spec = cfg.scope_hook_spec(Scope.WORKSPACE, ws / "proj", ws / "proj" / "worktrees" / "x")
    assert spec.on["enter"] == ["one"]
    assert spec.on["post-create"] == ["two", "three"]
    assert spec.run_builtins is True


def test_builtins_toggle_default_and_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    body = f'[workspaces.a]\npath="{ws}"\n[workspaces.a.hooks]\nbuiltins=false\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=tmp_path)
    spec = cfg.scope_hook_spec(Scope.WORKSPACE, ws / "proj", ws / "proj" / "x")
    assert spec.run_builtins is False


# --- base branch resolution --------------------------------------------------


def test_base_branch_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "w"
    (ws / "proj").mkdir(parents=True)
    body = (
        f'base_branch="global"\n[workspaces.a]\npath="{ws}"\nbase_branch="wsdefault"\n'
        '[workspaces.a.projects.proj]\nbase_branch="projann"\n'
    )
    cfg = _load(monkeypatch, tmp_path, body, cwd=tmp_path)
    workspace = cfg.workspaces["a"]
    # Per-project annotation wins over the workspace default and global default.
    assert cfg.base_branch_for(workspace, "proj") == "projann"
    # A project with no annotation falls back to the workspace default.
    assert cfg.base_branch_for(workspace, "other") == "wsdefault"


def test_project_local_beats_registry_annotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "w"
    proj = ws / "proj"
    (proj / ".wtm").mkdir(parents=True)
    (proj / ".wtm" / "config.toml").write_text('[project]\nbase_branch="local"\n')
    body = f'[workspaces.a]\npath="{ws}"\n[workspaces.a.projects.proj]\nbase_branch="registry"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=tmp_path)
    assert cfg.base_branch_for(cfg.workspaces["a"], "proj") == "local"


# --- workspace-of lookup -----------------------------------------------------


def test_workspace_of_by_project_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    cfg = _load(monkeypatch, tmp_path, f'[workspaces.a]\npath="{ws}"\n', cwd=tmp_path)
    assert cfg.workspace_of(ws / "proj").name == "a"
    assert cfg.workspace_of(tmp_path / "elsewhere" / "proj") is None
