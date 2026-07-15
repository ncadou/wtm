"""Pure, subprocess-free tests for the registry write seam and workspace ops.

Exercises :mod:`wtm.registry` and :mod:`wtm.workspace` directly against a
``WTM_CONFIG`` file under ``tmp_path`` (mirroring :mod:`tests.test_config`), so
the round-trip writes, comment preservation, collision/scope policy, and the
``default_workspace`` fixups are covered without a ``wtm`` invocation. Lifecycle
(enter/leave) and CLI wiring live in :mod:`tests.test_wtm`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wtm import registry
from wtm.config import Config, Scope
from wtm.core import WtmError
from wtm.workspace import Workspaces


@pytest.fixture
def cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate config/env and point WTM_CONFIG at a fresh (absent) user file."""
    for var in ("WTM_CONFIG", "WTM_SYSTEM_CONFIG", "WTM_WORKSPACE", "WTM_NO_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("WTM_NO_SYSTEM", "1")
    path = tmp_path / "config.toml"
    monkeypatch.setenv("WTM_CONFIG", str(path))

    return path


def _ops() -> Workspaces:
    """Build a Workspaces bound to a freshly loaded config."""
    return Workspaces(Config.load())


def _system(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str) -> None:
    """Declare a system-scope config file and stop ignoring system scope."""
    sys_path = tmp_path / "system.toml"
    sys_path.write_text(body)
    monkeypatch.setenv("WTM_SYSTEM_CONFIG", str(sys_path))
    monkeypatch.delenv("WTM_NO_SYSTEM", raising=False)


# --- add ---------------------------------------------------------------------


def test_add_creates_file_and_entry(cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    result = _ops().add("work", str(ws), description="day job", base_branch="main", create=False)
    assert not result.missing and not result.created
    text = cfg.read_text()
    assert "[workspaces.work]" in text
    assert f'path = "{ws}"' in text
    assert 'description = "day job"' in text
    assert 'base_branch = "main"' in text


def test_add_defaults_path_to_cwd(
    cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    here = tmp_path / "here"
    here.mkdir()
    monkeypatch.chdir(here)
    result = _ops().add("h", None, description=None, base_branch=None, create=False)
    assert result.resolved == here
    assert f'path = "{here}"' in cfg.read_text()


def test_add_preserves_tilde_form(
    cfg: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _ops().add("h", "~/proj", description=None, base_branch=None, create=False)
    assert 'path = "~/proj"' in cfg.read_text()


def test_add_create_makes_dir(cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "made"
    result = _ops().add("m", str(ws), description=None, base_branch=None, create=True)
    assert result.created and ws.is_dir()


def test_add_missing_path_flagged(cfg: Path, tmp_path: Path) -> None:
    result = _ops().add(
        "m", str(tmp_path / "nope"), description=None, base_branch=None, create=False
    )
    assert result.missing and not result.created


def test_add_rejects_file_path(cfg: Path, tmp_path: Path) -> None:
    afile = tmp_path / "afile"
    afile.write_text("x")
    with pytest.raises(WtmError, match="not a directory"):
        _ops().add("m", str(afile), description=None, base_branch=None, create=False)


def test_add_rejects_relative_after_expansion(cfg: Path) -> None:
    with pytest.raises(WtmError, match="absolute"):
        _ops().add("m", "$UNSET_VAR_XYZ", description=None, base_branch=None, create=False)


def test_add_duplicate_user_name_errors(cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    _ops().add("w", str(ws), description=None, base_branch=None, create=False)
    with pytest.raises(WtmError, match="already declared"):
        _ops().add("w", str(ws), description=None, base_branch=None, create=False)


def test_add_duplicate_path_warns_not_errors(cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "shared"
    ws.mkdir()
    _ops().add("a", str(ws), description=None, base_branch=None, create=False)
    result = _ops().add("b", str(ws), description=None, base_branch=None, create=False)
    assert result.duplicate_of == "a"
    assert "[workspaces.b]" in cfg.read_text()


def test_add_shadows_system(cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    _system(monkeypatch, tmp_path, f'[workspaces.w]\npath = "{ws}"\n')
    result = _ops().add("w", str(ws), description=None, base_branch=None, create=False)
    assert result.shadows_system
    assert "[workspaces.w]" in cfg.read_text()


def test_add_rejects_bad_name(cfg: Path, tmp_path: Path) -> None:
    with pytest.raises(WtmError, match="may not contain"):
        _ops().add("a/b", str(tmp_path), description=None, base_branch=None, create=False)


# --- comment / format preservation -------------------------------------------


def test_add_preserves_comments_and_other_keys(cfg: Path, tmp_path: Path) -> None:
    cfg.write_text(
        "# my wtm config\n"
        'default_workspace = "keep"\n\n'
        '[workspaces.keep]  # note\npath = "/tmp/keep"\n'
    )
    ws = tmp_path / "new"
    ws.mkdir()
    _ops().add("new", str(ws), description=None, base_branch=None, create=False)
    text = cfg.read_text()
    assert "# my wtm config" in text
    assert 'default_workspace = "keep"' in text
    assert "[workspaces.keep]  # note" in text
    assert "[workspaces.new]" in text


# --- set ---------------------------------------------------------------------


def _seed_user(cfg: Path, name: str, path: Path, **extra: str) -> None:
    """Write a minimal user-scope entry directly, for edit/rename/delete tests."""
    body = f'[workspaces.{name}]\npath = "{path}"\n'
    for key, value in extra.items():
        body += f'{key} = "{value}"\n'
    prev = cfg.read_text() if cfg.exists() else ""
    cfg.write_text(prev + body)


def test_set_updates_fields(cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    _seed_user(cfg, "w", ws)
    _ops().set("w", path=None, description="edited", base_branch="dev")
    text = cfg.read_text()
    assert 'description = "edited"' in text
    assert 'base_branch = "dev"' in text


def test_set_empty_clears_field(cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    _seed_user(cfg, "w", ws, description="old")
    _ops().set("w", path=None, description="", base_branch=None)
    assert "description" not in cfg.read_text()


def test_set_empty_path_rejected(cfg: Path, tmp_path: Path) -> None:
    _seed_user(cfg, "w", tmp_path)
    with pytest.raises(WtmError, match="may not be empty"):
        _ops().set("w", path="", description=None, base_branch=None)


def test_set_requires_a_field(cfg: Path, tmp_path: Path) -> None:
    _seed_user(cfg, "w", tmp_path)
    with pytest.raises(WtmError, match="nothing to set"):
        _ops().set("w", path=None, description=None, base_branch=None)


def test_set_unknown_errors(cfg: Path) -> None:
    with pytest.raises(WtmError, match="not declared"):
        _ops().set("ghost", path=None, description="x", base_branch=None)


def test_set_system_only_refused(
    cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _system(monkeypatch, tmp_path, f'[workspaces.s]\npath = "{tmp_path}"\n')
    with pytest.raises(WtmError, match="system scope"):
        _ops().set("s", path=None, description="x", base_branch=None)


# --- rename ------------------------------------------------------------------


def test_rename_moves_key_and_fixes_default(cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    cfg.write_text(
        f'default_workspace = "old"\n[workspaces.old]\npath = "{ws}"\ndescription = "d"\n'
    )
    result = _ops().rename("old", "new")
    assert result.fixed_default
    text = cfg.read_text()
    assert "[workspaces.new]" in text and "[workspaces.old]" not in text
    assert 'description = "d"' in text  # subtable/scalars carried over
    assert 'default_workspace = "new"' in text


def test_rename_new_collision_errors(cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    _seed_user(cfg, "a", ws)
    _seed_user(cfg, "b", ws)
    with pytest.raises(WtmError, match="already declared"):
        _ops().rename("a", "b")


def test_rename_system_only_refused(
    cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _system(monkeypatch, tmp_path, f'[workspaces.s]\npath = "{tmp_path}"\n')
    with pytest.raises(WtmError, match="system scope"):
        _ops().rename("s", "t")


# --- delete ------------------------------------------------------------------


def test_delete_removes_entry_and_clears_default(cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    cfg.write_text(f'default_workspace = "w"\n[workspaces.w]\npath = "{ws}"\n')
    result = _ops().delete("w")
    assert result.cleared_default and not result.remains_at_system
    text = cfg.read_text()
    assert "[workspaces.w]" not in text
    assert "default_workspace" not in text


def test_delete_unknown_errors(cfg: Path) -> None:
    with pytest.raises(WtmError, match="not declared"):
        _ops().delete("ghost")


def test_delete_system_only_refused(
    cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _system(monkeypatch, tmp_path, f'[workspaces.s]\npath = "{tmp_path}"\n')
    with pytest.raises(WtmError, match="only at system scope"):
        _ops().delete("s")


def test_delete_both_scopes_warns_system_remains(
    cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    _system(monkeypatch, tmp_path, f'[workspaces.w]\npath = "{ws}"\n')
    _seed_user(cfg, "w", ws)
    result = _ops().delete("w")
    assert result.remains_at_system
    assert "[workspaces.w]" not in cfg.read_text()


# --- default -----------------------------------------------------------------


def test_default_set_requires_declared(cfg: Path) -> None:
    with pytest.raises(WtmError, match="not declared"):
        _ops().default_set("ghost")


def test_default_set_and_clear(cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    _seed_user(cfg, "w", ws)
    _ops().default_set("w")
    assert 'default_workspace = "w"' in cfg.read_text()
    _ops().default_clear()
    assert "default_workspace" not in cfg.read_text()


# --- write guards ------------------------------------------------------------


def test_write_refused_under_no_config(
    cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WTM_NO_CONFIG", "1")
    with pytest.raises(WtmError, match="WTM_NO_CONFIG"):
        registry.add_workspace("w", str(tmp_path))


def test_atomic_write_leaves_no_temp(cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    _ops().add("w", str(ws), description=None, base_branch=None, create=False)
    leftovers = [p.name for p in cfg.parent.iterdir() if p.name.startswith(".wtm-config.")]
    assert leftovers == []


# --- scope detection ---------------------------------------------------------


def test_declaring_scopes(cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    _system(
        monkeypatch,
        tmp_path,
        f'[workspaces.both]\npath = "{ws}"\n[workspaces.sysonly]\npath = "{ws}"\n',
    )
    _seed_user(cfg, "both", ws)
    _seed_user(cfg, "useronly", ws)
    assert registry.declaring_scopes("both") == {Scope.SYSTEM, Scope.USER}
    assert registry.declaring_scopes("sysonly") == {Scope.SYSTEM}
    assert registry.declaring_scopes("useronly") == {Scope.USER}
    assert registry.declaring_scopes("nobody") == set()
