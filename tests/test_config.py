"""Pure, subprocess-free tests for the single config authority (:mod:`wtm.config`).

Both sides of the engine are exercised directly against :meth:`Config.load` and
its methods, driven by TOML files under ``tmp_path`` and env vars, with no
``wtm`` invocation and no git: config *resolution* (the workspace registry,
active-workspace precedence, per-scope hook merging, base-branch cascade) and
config *writes* (the arbitrary-key facility, the structural registry mutations,
comment preservation, scope policy, and the ``default_workspace`` fixups).
Lifecycle (enter/leave) and CLI wiring live in :mod:`tests.test_wtm`.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from wtm.config import (
    TYPED_VALUE_FORMATS,
    Config,
    ConfigAddress,
    Scope,
    SliceTarget,
    json_leaf_default,
    parse_toml_value,
    parse_typed_value,
    render_scalar,
)
from wtm.core import WtmError
from wtm.project import Project
from wtm.workspace import Workspaces
from wtm.worktree import Worktree


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Never read the host's real config/env during a unit test."""
    for var in ("WTM_CONFIG", "WTM_WORKSPACE", "WTM_NO_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    # Point system scope at an absent file (there is no WTM_NO_SYSTEM knob).
    monkeypatch.setenv("WTM_SYSTEM_CONFIG", str(tmp_path / "no-system.toml"))


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


def test_none_cwd_falls_through_to_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A removed cwd (cwd=None) skips the cwd tier and uses default_workspace."""
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    body = f'default_workspace="B"\n[workspaces.A]\npath="{a}"\n[workspaces.B]\npath="{b}"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=None)
    assert cfg.active == "B"


def test_none_cwd_none_active_when_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A removed cwd with no default and several workspaces leaves no active one."""
    cfg = _load(monkeypatch, tmp_path, _two(tmp_path), cwd=None)
    assert cfg.active is None


def test_deleted_cwd_does_not_crash_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Config.load survives a cwd whose os.getcwd() raises (the deleted-dir case)."""

    def _boom() -> str:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr("wtm.core.os.getcwd", _boom)
    monkeypatch.setenv("WTM_CONFIG", str(_write(tmp_path / "c.toml", _two(tmp_path))))
    cfg = Config.load()  # no explicit cwd -> reads the (gone) live cwd
    assert cfg.active is None


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
    monkeypatch.setenv("WTM_SYSTEM_CONFIG", str(system))
    monkeypatch.setenv("WTM_CONFIG", str(user))
    cfg = Config.load(cwd=tmp_path)
    workspace = cfg.workspaces["a"]
    assert workspace.description == "usr"  # user scalar wins
    assert workspace.scope is Scope.USER
    assert workspace.hooks.on["enter"] == ["sys-cmd", "usr-cmd"]  # accumulate, sys first


def test_absent_system_file_leaves_no_workspaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With system scope pointed at an absent file, only user config is seen.
    cfg = _load(monkeypatch, tmp_path, "", cwd=tmp_path)
    assert cfg.workspaces == {}


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


def test_worktree_scope_hooks_accumulate_from_ancestor_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A worktree inline hook declared at the *project* scope (project-local
    # [worktrees.NAME.hooks.on]) must be seen at worktree scope -- the branch
    # used to read only the worktree-local file, so ancestor-declared worktree
    # hooks silently never fired.
    ws = tmp_path / "w"
    proj = ws / "proj"
    wt = proj / "worktrees" / "feat"
    wt.mkdir(parents=True)
    (proj / ".wtm").mkdir(parents=True)
    (proj / ".wtm" / "config.toml").write_text(
        '[worktrees.feat.hooks.on]\nenter = "from-project"\n'
    )
    (wt / ".wtm").mkdir(parents=True)
    (wt / ".wtm" / "config.toml").write_text('[hooks.on]\nleave = "from-worktree"\n')
    body = f'[workspaces.a]\npath="{ws}"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=tmp_path)
    spec = cfg.scope_hook_spec(Scope.WORKTREE, proj, wt)
    assert spec.on["enter"] == ["from-project"]  # inherited from the project tier
    assert spec.on["leave"] == ["from-worktree"]  # the worktree-local file


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
    (proj / ".wtm" / "config.toml").write_text('base_branch="local"\n')
    body = f'[workspaces.a]\npath="{ws}"\n[workspaces.a.projects.proj]\nbase_branch="registry"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=tmp_path)
    assert cfg.base_branch_for(cfg.workspaces["a"], "proj") == "local"


def test_workspace_local_project_base_branch_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A base_branch set only in the workspace-local [projects.NAME] table must be
    # both seen and correctly attributed to the project tier (the provenance bug).
    ws = tmp_path / "w"
    (ws / "proj").mkdir(parents=True)
    (ws / ".wtm").mkdir(parents=True)
    (ws / ".wtm" / "config.toml").write_text('[projects.proj]\nbase_branch="wslocal"\n')
    body = f'[workspaces.a]\npath="{ws}"\nbase_branch="wsdefault"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=tmp_path)
    workspace = cfg.workspaces["a"]
    value, tier = cfg.base_branch_provenance(workspace, "proj")
    assert value == "wslocal"
    assert tier == "project"


def test_project_arbitrary_key_cascades_into_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An arbitrary key resolves most-local-first and lands in `extra`.
    ws = tmp_path / "w"
    proj = ws / "proj"
    (proj / ".wtm").mkdir(parents=True)
    (proj / ".wtm" / "config.toml").write_text('editor = "vim"\n')
    body = (
        f'[workspaces.a]\npath="{ws}"\n[workspaces.a.projects.proj]\neditor="emacs"\nlint="ruff"\n'
    )
    cfg = _load(monkeypatch, tmp_path, body, cwd=tmp_path)
    annotation = cfg.project_annotation(cfg.workspaces["a"], "proj")
    assert annotation.extra["editor"] == "vim"  # project-local wins
    assert annotation.extra["lint"] == "ruff"  # inherited from the registry


def test_project_kind_clash_across_scopes_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same key declared as a scalar in one scope and a table in another is a
    # hard error naming the key.
    ws = tmp_path / "w"
    proj = ws / "proj"
    (proj / ".wtm").mkdir(parents=True)
    (proj / ".wtm" / "config.toml").write_text('[flags]\nx = "1"\n')
    body = f'[workspaces.a]\npath="{ws}"\n[workspaces.a.projects.proj]\nflags="scalar"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=tmp_path)
    with pytest.raises(WtmError, match="conflicting kinds"):
        cfg.project_annotation(cfg.workspaces["a"], "proj")


def test_generic_get_kind_clash_across_scopes_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `config get` (resolve_generic_value) enforces the same kind-clash rule as
    # `config show` (project_annotation): one rule for every read surface.
    ws = tmp_path / "w"
    proj = ws / "proj"
    (proj / ".wtm").mkdir(parents=True)
    (proj / ".wtm" / "config.toml").write_text('[flags]\nx = "1"\n')
    body = f'[workspaces.a]\npath="{ws}"\n[workspaces.a.projects.proj]\nflags="scalar"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=proj)
    target = cfg.resolve_generic_target(proj)
    with pytest.raises(WtmError, match="conflicting kinds"):
        cfg.resolve_generic_value(target, "flags")


def test_generic_get_base_branch_inherits_from_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `config get base_branch` from a project reports the inherited workspace-own
    # value -- matching what `create` uses (base_branch_for), not `(unset)`.
    ws = tmp_path / "w"
    proj = ws / "proj"
    proj.mkdir(parents=True)
    body = f'[workspaces.a]\npath="{ws}"\nbase_branch="wsdefault"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=proj)
    target = cfg.resolve_generic_target(proj)
    effective, chain = cfg.resolve_generic_value(target, "base_branch")
    assert effective == "wsdefault"
    assert effective == cfg.base_branch_for(cfg.workspaces["a"], "proj")
    # The winning rung is tagged with its definition tier, not just its scope.
    assert chain[0].tier == "workspace"


def test_generic_get_base_branch_inherits_from_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With nothing project- or workspace-local, the global top-level default is
    # the effective value, tagged `global`.
    ws = tmp_path / "w"
    proj = ws / "proj"
    proj.mkdir(parents=True)
    body = f'base_branch="global"\n[workspaces.a]\npath="{ws}"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=proj)
    target = cfg.resolve_generic_target(proj)
    effective, chain = cfg.resolve_generic_value(target, "base_branch")
    assert effective == "global"
    assert chain[0].tier == "global"


def test_generic_get_base_branch_project_local_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A project-local value outranks the inherited workspace and global tiers,
    # and the chain lists every present rung most-local-first.
    ws = tmp_path / "w"
    proj = ws / "proj"
    (proj / ".wtm").mkdir(parents=True)
    (proj / ".wtm" / "config.toml").write_text('base_branch="local"\n')
    body = f'base_branch="global"\n[workspaces.a]\npath="{ws}"\nbase_branch="wsdefault"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=proj)
    target = cfg.resolve_generic_target(proj)
    effective, chain = cfg.resolve_generic_value(target, "base_branch")
    assert effective == "local"
    assert [(r.tier, r.value) for r in chain] == [
        ("project", "local"),
        ("workspace", "wsdefault"),
        ("global", "global"),
    ]


def test_generic_get_base_branch_from_worktree_uses_project_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # From inside a worktree, base_branch (a project-tier concept) resolves the
    # containing project's inherited value.
    ws = tmp_path / "w"
    wt = ws / "proj" / "worktrees" / "feat"
    wt.mkdir(parents=True)
    body = f'[workspaces.a]\npath="{ws}"\nbase_branch="wsdefault"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=wt)
    target = cfg.resolve_generic_target(wt)
    effective, _ = cfg.resolve_generic_value(target, "base_branch")
    assert effective == "wsdefault"


def test_generic_get_base_branch_from_workspace_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # At the workspace root (no project), base_branch resolves workspace-own then
    # global -- there is no project tier to fold.
    ws = tmp_path / "w"
    ws.mkdir(parents=True)
    body = f'base_branch="global"\n[workspaces.a]\npath="{ws}"\nbase_branch="wsdefault"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=ws)
    target = cfg.resolve_generic_target(ws)
    effective, chain = cfg.resolve_generic_value(target, "base_branch")
    assert effective == "wsdefault"
    assert [r.tier for r in chain] == ["workspace", "global"]


def test_generic_get_non_inheritable_key_stays_entity_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A non-inheritable key does not fold ancestor tiers: a workspace-own scalar
    # is invisible to a project entity's `config get`.
    ws = tmp_path / "w"
    proj = ws / "proj"
    proj.mkdir(parents=True)
    body = f'[workspaces.a]\npath="{ws}"\neditor="emacs"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=proj)
    target = cfg.resolve_generic_target(proj)
    effective, chain = cfg.resolve_generic_value(target, "editor")
    assert effective is None
    assert chain == []


# --- user-declared inheritable keys (non-entity-specific opt-in) -------------


def test_user_declared_key_inherits_from_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An arbitrary key named in the `inheritable` array folds ancestor tiers just
    # like base_branch: a project without its own value reports the workspace one.
    ws = tmp_path / "w"
    proj = ws / "proj"
    proj.mkdir(parents=True)
    body = f'inheritable = ["deploy_env"]\n[workspaces.a]\npath="{ws}"\ndeploy_env="staging"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=proj)
    target = cfg.resolve_generic_target(proj)
    effective, chain = cfg.resolve_generic_value(target, "deploy_env")
    assert effective == "staging"
    assert chain[0].tier == "workspace"


def test_undeclared_arbitrary_key_stays_entity_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without an `inheritable` declaration, a workspace-own key is invisible to a
    # project entity (default is entity-local).
    ws = tmp_path / "w"
    proj = ws / "proj"
    proj.mkdir(parents=True)
    body = f'[workspaces.a]\npath="{ws}"\ndeploy_env="staging"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=proj)
    target = cfg.resolve_generic_target(proj)
    effective, chain = cfg.resolve_generic_value(target, "deploy_env")
    assert effective is None
    assert chain == []


def test_inheritable_declaration_unions_across_scopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The declaration accumulates: a workspace-local `inheritable` opts a key in
    # for its projects even when the global file does not name it.
    ws = tmp_path / "w"
    proj = ws / "proj"
    proj.mkdir(parents=True)
    (ws / ".wtm").mkdir(parents=True)
    (ws / ".wtm" / "config.toml").write_text('inheritable = ["team"]\nteam = "core"\n')
    body = f'[workspaces.a]\npath="{ws}"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=proj)
    target = cfg.resolve_generic_target(proj)
    effective, chain = cfg.resolve_generic_value(target, "team")
    assert effective == "core"
    assert chain[0].tier == "workspace"


def test_user_declared_key_project_local_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A project-local value outranks the inherited workspace tier for a declared
    # inheritable key (entity-own is the most-local rung).
    ws = tmp_path / "w"
    proj = ws / "proj"
    (proj / ".wtm").mkdir(parents=True)
    (proj / ".wtm" / "config.toml").write_text('deploy_env="prod"\n')
    body = f'inheritable = ["deploy_env"]\n[workspaces.a]\npath="{ws}"\ndeploy_env="staging"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=proj)
    target = cfg.resolve_generic_target(proj)
    effective, chain = cfg.resolve_generic_value(target, "deploy_env")
    assert effective == "prod"
    assert [(r.tier, r.value) for r in chain] == [("project", "prod"), ("workspace", "staging")]


def test_user_declared_key_worktree_local_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Unlike base_branch, a general inheritable key set worktree-local IS honoured
    # (the worktree's own tier participates in its chain).
    ws = tmp_path / "w"
    wt = ws / "proj" / "worktrees" / "feat"
    (wt / ".wtm").mkdir(parents=True)
    (wt / ".wtm" / "config.toml").write_text('deploy_env="scratch"\n')
    body = f'inheritable = ["deploy_env"]\n[workspaces.a]\npath="{ws}"\ndeploy_env="staging"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=wt)
    target = cfg.resolve_generic_target(wt)
    effective, _ = cfg.resolve_generic_value(target, "deploy_env")
    assert effective == "scratch"


def test_base_branch_worktree_local_still_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # base_branch stays a project-tier concept: a worktree-local value is ignored
    # by `config get`, keeping it aligned with create/base_branch_provenance.
    ws = tmp_path / "w"
    wt = ws / "proj" / "worktrees" / "feat"
    (wt / ".wtm").mkdir(parents=True)
    (wt / ".wtm" / "config.toml").write_text('base_branch="wt-local"\n')
    body = f'[workspaces.a]\npath="{ws}"\nbase_branch="wsdefault"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=wt)
    target = cfg.resolve_generic_target(wt)
    effective, _ = cfg.resolve_generic_value(target, "base_branch")
    assert effective == "wsdefault"


def test_inheritable_declaration_key_is_reserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `inheritable` owns a typed surface: it is a reserved key (never an arbitrary
    # one) and never leaks into an entity's extra payload.
    from wtm.config import validate_arbitrary_key

    with pytest.raises(WtmError, match="reserved key"):
        validate_arbitrary_key("inheritable")
    ws = tmp_path / "w"
    proj = ws / "proj"
    proj.mkdir(parents=True)
    body = (
        f'inheritable = ["x"]\n[workspaces.a]\npath="{ws}"\n[workspaces.a.projects.proj]\nx="1"\n'
    )
    cfg = _load(monkeypatch, tmp_path, body, cwd=proj)
    annotation = cfg.project_annotation(cfg.workspaces["a"], "proj")
    assert "inheritable" not in annotation.extra


def test_workspaces_registry_key_is_reserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``workspaces`` is the global registry table: a reserved key (never an
    # arbitrary one) and never leaked into the global extra payload.
    from wtm.config import validate_arbitrary_key

    with pytest.raises(WtmError, match="reserved key"):
        validate_arbitrary_key("workspaces")
    ws = tmp_path / "w"
    ws.mkdir()
    body = f'[workspaces.a]\npath="{ws}"\nbase_branch = "main"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=tmp_path)
    s = cfg.resolve_slice(target=None, recurse=True)
    assert "workspaces" not in s.global_extra


def test_validate_inheritable_element_rules() -> None:
    # Anything hand-writable into `inheritable` is CLI-writable too, except keys
    # whose (non-)inheritance is hardcoded: reserved keys and NON_INHERITABLE_KEYS.
    from wtm.config import validate_inheritable_element

    validate_inheritable_element("deploy_env")  # arbitrary key: allowed
    validate_inheritable_element("base_branch")  # already builtin-inheritable: harmless
    with pytest.raises(WtmError, match="never inheritable"):
        validate_inheritable_element("description")
    with pytest.raises(WtmError, match="reserved key"):
        validate_inheritable_element("hooks")


def test_project_annotation_extra_reflects_inherited_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `config show`'s read path (project_annotation.extra) agrees with `config
    # get`: a declared inheritable key absent project-locally surfaces inherited.
    ws = tmp_path / "w"
    proj = ws / "proj"
    proj.mkdir(parents=True)
    body = f'inheritable = ["deploy_env"]\n[workspaces.a]\npath="{ws}"\ndeploy_env="staging"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=proj)
    annotation = cfg.project_annotation(cfg.workspaces["a"], "proj")
    assert annotation.extra["deploy_env"] == "staging"


def test_worktree_extra_reflects_inherited_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The resolved worktree view inherits a declared key from the workspace tier.
    ws = tmp_path / "w"
    wt = ws / "proj" / "worktrees" / "feat"
    wt.mkdir(parents=True)
    body = f'inheritable = ["deploy_env"]\n[workspaces.a]\npath="{ws}"\ndeploy_env="staging"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=wt)
    target = cfg.cwd_location(cwd=wt)
    assert target is not None and target.tier == "worktree"
    slice_ = cfg.resolve_slice(target=target)
    worktree = slice_.workspaces[0].projects[0].worktrees[0]
    assert worktree.extra["deploy_env"] == "staging"


# --- floors (a key's deepest participating tier) --------------------------------


def test_floored_key_prunes_deeper_rungs_like_base_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A user-declared floor=project mirrors the base_branch carve-out: from a
    # worktree, a worktree-local value is ignored while the project chain folds.
    ws = tmp_path / "w"
    wt = ws / "proj" / "worktrees" / "feat"
    (wt / ".wtm").mkdir(parents=True)
    (wt / ".wtm" / "config.toml").write_text('region="wt-local"\n')
    body = (
        'inheritable = [{ key = "region", floor = "project" }]\n'
        f'[workspaces.a]\npath="{ws}"\n'
        '[workspaces.a.projects.proj]\nregion="proj-tier"\n'
    )
    cfg = _load(monkeypatch, tmp_path, body, cwd=wt)
    target = cfg.resolve_generic_target(wt)
    effective, chain = cfg.resolve_generic_value(target, "region")
    assert effective == "proj-tier"
    assert [r.tier for r in chain] == ["project"]
    # The target carries the floor for write-site enforcement.
    assert target.floors == {"base_branch": "project", "region": "project"}


def test_floor_workspace_drops_project_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # floor=workspace means project-tier values are out of band too: from a
    # project entity, only workspace-and-above rungs participate.
    ws = tmp_path / "w"
    proj = ws / "proj"
    (proj / ".wtm").mkdir(parents=True)
    (proj / ".wtm" / "config.toml").write_text('region="local"\n')
    body = (
        'inheritable = [{ key = "region", floor = "workspace" }]\n'
        f'[workspaces.a]\npath="{ws}"\nregion="ws-tier"\n'
    )
    cfg = _load(monkeypatch, tmp_path, body, cwd=proj)
    target = cfg.resolve_generic_target(proj)
    effective, chain = cfg.resolve_generic_value(target, "region")
    assert effective == "ws-tier"
    assert [r.tier for r in chain] == ["workspace"]


def test_floored_key_extra_fold_respects_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The resolved view's extra fold prunes by floor too: a floor=workspace key
    # does not surface a project-local value in project_annotation.extra.
    ws = tmp_path / "w"
    proj = ws / "proj"
    (proj / ".wtm").mkdir(parents=True)
    (proj / ".wtm" / "config.toml").write_text('region="local"\n')
    body = (
        'inheritable = [{ key = "region", floor = "workspace" }]\n'
        f'[workspaces.a]\npath="{ws}"\nregion="ws-tier"\n'
    )
    cfg = _load(monkeypatch, tmp_path, body, cwd=proj)
    annotation = cfg.project_annotation(cfg.workspaces["a"], "proj")
    assert annotation.extra["region"] == "ws-tier"


def test_floor_declaration_conflict_most_local_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two scopes declaring floors for one key resolve most-local-first: the
    # project-scope declaration wins over the global one.
    ws = tmp_path / "w"
    proj = ws / "proj"
    (proj / ".wtm").mkdir(parents=True)
    (proj / ".wtm" / "config.toml").write_text('inheritable = ["region"]\nregion="p"\n')
    body = (
        'inheritable = [{ key = "region", floor = "global" }]\n'
        f'region="global-val"\n[workspaces.a]\npath="{ws}"\n'
    )
    cfg = _load(monkeypatch, tmp_path, body, cwd=proj)
    target = cfg.resolve_generic_target(proj)
    # No pruning happened: the most-local declaration has no floor.
    effective, chain = cfg.resolve_generic_value(target, "region")
    assert effective == "p"
    assert [(r.tier, r.value) for r in chain] == [("project", "p"), ("global", "global-val")]


def test_malformed_inheritable_element_is_a_hard_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A malformed stored element errors at read time (the kind-clash treatment),
    # annotated with its file.
    ws = tmp_path / "w"
    proj = ws / "proj"
    proj.mkdir(parents=True)
    body = f'inheritable = [{{ key = "region", typo = "x" }}]\n[workspaces.a]\npath="{ws}"\n'
    with pytest.raises(WtmError, match="unknown field"):
        _ = _load(monkeypatch, tmp_path, body, cwd=proj)


def test_bad_floor_tier_is_rejected_at_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "w"
    proj = ws / "proj"
    proj.mkdir(parents=True)
    body = f'inheritable = [{{ key = "region", floor = "cellar" }}]\n[workspaces.a]\npath="{ws}"\n'
    with pytest.raises(WtmError, match="'floor' must be one of"):
        _ = _load(monkeypatch, tmp_path, body, cwd=proj)


def test_floor_on_base_branch_refused() -> None:
    # base_branch owns a typed surface with fixed resolution; re-flooring it
    # would split `config get` from `create` again.
    from wtm.config import parse_inheritable_element

    with pytest.raises(WtmError, match="cannot take a floor"):
        parse_inheritable_element({"key": "base_branch", "floor": "global"})
    parse_inheritable_element("base_branch")  # bare opt-in stays harmless
    parse_inheritable_element({"key": "deploy_env"})  # no floor: fine


def test_write_below_floor_refused_by_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtm.exceptions import WriteProhibitedError

    ws = tmp_path / "w"
    project = ws / "alpha"
    wt = project / "worktrees" / "feat"
    wt.mkdir(parents=True)
    config = _load(monkeypatch, tmp_path, f'[workspaces.a]\npath="{ws}"\n', cwd=tmp_path)

    # Declare region inheritable WITH a project floor via the handle API
    # (the workspace-local declaration is itself never floor-blocked).
    config.entity("a").inheritable_add("region", floor="project")

    config = config.reload()
    svc = config.entity("a", "alpha", "feat")

    with pytest.raises(WriteProhibitedError, match="has floor 'project'"):
        svc.set("region", "v", scope=Scope.WORKTREE)
    with pytest.raises(WriteProhibitedError):
        svc.table_set("region", "sub", "v", scope=Scope.WORKTREE)
    with pytest.raises(WriteProhibitedError):
        svc.array_add("region", "v", scope=Scope.WORKTREE)
    # At the floor tier (and above), the write goes through.
    address = svc.set("region", "v", scope=Scope.PROJECT)
    assert address.file == project / ".wtm" / "config.toml"
    # Removal below the floor is cleanup, never refused.
    (wt / ".wtm").mkdir(exist_ok=True)
    stray = wt / ".wtm" / "config.toml"
    stray.write_text('region="stray"\n')
    assert svc.unset("region", scope=Scope.WORKTREE).file == stray


def test_inline_table_inheritable_element_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The floored element is a real TOML inline table; adding twice is a no-op;
    # removing by bare name strips it.
    import tomllib

    ws = tmp_path / "w"
    ws.mkdir()
    config = _load(monkeypatch, tmp_path, f'[workspaces.a]\npath="{ws}"\n', cwd=tmp_path)

    handle = config.entity("a")
    handle.inheritable_add("region", floor="project")
    handle.inheritable_add("region", floor="project")  # idempotent

    ws_file = ws / ".wtm" / "config.toml"
    decls = tomllib.loads(ws_file.read_text())["inheritable"]
    assert decls == [{"key": "region", "floor": "project"}]

    Config.load(cwd=tmp_path).entity("a").inheritable_remove("region")
    assert tomllib.loads(ws_file.read_text())["inheritable"] == []


def test_tier_of_write_site_matrix() -> None:
    # The pure tier derivation behind floor enforcement.
    from wtm.config import tier_of_write_site

    assert tier_of_write_site(Scope.WORKTREE, ()) == "worktree"
    assert tier_of_write_site(Scope.PROJECT, ("k",)) == "project"
    assert tier_of_write_site(Scope.WORKSPACE, ("projects", "p")) == "workspace"
    assert tier_of_write_site(Scope.USER, ("k",)) == "global"
    assert tier_of_write_site(Scope.USER, ("ci", "runner")) == "global"
    assert tier_of_write_site(Scope.USER, ("workspaces", "a", "k")) == "workspace"
    assert tier_of_write_site(Scope.SYSTEM, ("workspaces", "a", "projects", "p")) == "project"
    assert tier_of_write_site(Scope.USER, ("workspaces", "a", "projects", "p", "k")) == "project"
    assert (
        tier_of_write_site(Scope.USER, ("workspaces", "a", "projects", "p", "worktrees", "f"))
        == "worktree"
    )


def test_workspace_extra_reflects_inherited_global_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A workspace inherits a declared key from the global top-level default.
    ws = tmp_path / "w"
    ws.mkdir(parents=True)
    body = f'inheritable = ["deploy_env"]\ndeploy_env="global"\n[workspaces.a]\npath="{ws}"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=tmp_path)
    assert cfg.workspaces["a"].extra["deploy_env"] == "global"


def test_project_declared_key_does_not_inherit_when_undeclared_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sanity: a declared inheritable key with no ancestor value resolves to unset,
    # and a non-declared key never inherits even if an ancestor sets it.
    ws = tmp_path / "w"
    proj = ws / "proj"
    proj.mkdir(parents=True)
    body = f'inheritable = ["team"]\n[workspaces.a]\npath="{ws}"\ndeploy_env="staging"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=proj)
    annotation = cfg.project_annotation(cfg.workspaces["a"], "proj")
    assert "team" not in annotation.extra  # declared inheritable, but no value anywhere
    assert "deploy_env" not in annotation.extra  # value exists but not declared inheritable


def test_project_presence_beats_registry_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An empty-string project-local value is a legitimate value that wins over a
    # non-empty ancestor (presence-based, not `x or y`).
    ws = tmp_path / "w"
    proj = ws / "proj"
    (proj / ".wtm").mkdir(parents=True)
    (proj / ".wtm" / "config.toml").write_text('description = ""\n')
    body = f'[workspaces.a]\npath="{ws}"\n[workspaces.a.projects.proj]\ndescription="registry"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=tmp_path)
    annotation = cfg.project_annotation(cfg.workspaces["a"], "proj")
    assert annotation.description == ""


def test_worktree_description_deep_cascade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A worktree's description resolves through the deep cascade: the project-local
    # [worktrees.NAME] table supplies it, the worktree-local top-level overrides.
    ws = tmp_path / "w"
    wt = ws / "proj" / "worktrees" / "feat"
    wt.mkdir(parents=True)
    (ws / "proj" / ".wtm").mkdir(parents=True)
    (ws / "proj" / ".wtm" / "config.toml").write_text(
        '[worktrees.feat]\ndescription = "from-project"\nowner = "team"\n'
    )
    (wt / ".wtm").mkdir(parents=True)
    (wt / ".wtm" / "config.toml").write_text('description = "from-worktree"\n')
    body = f'[workspaces.a]\npath="{ws}"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=wt)
    target = cfg.cwd_location(cwd=wt)
    assert target is not None and target.tier == "worktree"
    slice_ = cfg.resolve_slice(target=target)
    worktree = slice_.workspaces[0].projects[0].worktrees[0]
    assert worktree.description == "from-worktree"  # worktree-local wins
    assert worktree.extra["owner"] == "team"  # inherited from the project tier


# --- workspace-of lookup -----------------------------------------------------


def test_workspace_of_by_project_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    cfg = _load(monkeypatch, tmp_path, f'[workspaces.a]\npath="{ws}"\n', cwd=tmp_path)
    assert cfg.workspace_of(ws / "proj").name == "a"
    assert cfg.workspace_of(tmp_path / "elsewhere" / "proj") is None


# --- workspace-tier locality merge (unified probe engine) --------------------


def test_workspace_local_empty_string_clears_registry_scalar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A workspace-local empty-string value is a legitimate value that wins over a
    # non-empty registry declaration (presence-based, not `x or y`).
    ws = tmp_path / "w"
    (ws / ".wtm").mkdir(parents=True)
    (ws / ".wtm" / "config.toml").write_text('description = ""\n')
    body = f'[workspaces.a]\npath="{ws}"\ndescription="registry"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=tmp_path)
    assert cfg.workspaces["a"].description == ""


def test_workspace_arbitrary_key_populates_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An arbitrary workspace key resolves most-local-first into Workspace.extra
    # (the registry engine used to leave extra permanently empty).
    ws = tmp_path / "w"
    (ws / ".wtm").mkdir(parents=True)
    (ws / ".wtm" / "config.toml").write_text('editor = "vim"\n')
    body = f'[workspaces.a]\npath="{ws}"\neditor="emacs"\nlint="ruff"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=tmp_path)
    workspace = cfg.workspaces["a"]
    assert workspace.extra["editor"] == "vim"  # workspace-local wins
    assert workspace.extra["lint"] == "ruff"  # inherited from the registry
    # The reserved declaration key `path` owns its typed surface and must never
    # leak into `extra` (the registry entry's own `path = ...` is not arbitrary).
    assert "path" not in workspace.extra


def test_workspace_base_branch_matches_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The unified engine means Workspace.base_branch equals the workspace tier
    # base_branch_provenance resolves (no divergence between the two).
    ws = tmp_path / "w"
    (ws / "proj").mkdir(parents=True)
    (ws / ".wtm").mkdir(parents=True)
    (ws / ".wtm" / "config.toml").write_text('base_branch = "wslocal"\n')
    body = f'[workspaces.a]\npath="{ws}"\nbase_branch="registry"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=tmp_path)
    workspace = cfg.workspaces["a"]
    assert workspace.base_branch == "wslocal"  # workspace-local wins
    value, tier = cfg.base_branch_provenance(workspace, "other")
    assert (value, tier) == ("wslocal", "workspace")


def test_global_default_workspace_presence_based(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The global default tier is presence-based too: a user-file value present
    # (even alongside a system one) wins outright.
    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    ws_a.mkdir()
    ws_b.mkdir()
    sys_path = tmp_path / "system.toml"
    sys_path.write_text(f'default_workspace = "a"\n[workspaces.a]\npath="{ws_a}"\n')
    monkeypatch.setenv("WTM_SYSTEM_CONFIG", str(sys_path))
    body = f'default_workspace = "b"\n[workspaces.b]\npath="{ws_b}"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=tmp_path)
    assert cfg.default_workspace == "b"
    assert cfg.active == "b"


# =============================================================================
# Writes: the arbitrary-key facility + the structural registry mutations.
# =============================================================================


@pytest.fixture
def user_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point WTM_CONFIG at a fresh (absent) user file and return its path."""
    path = tmp_path / "config.toml"
    monkeypatch.setenv("WTM_CONFIG", str(path))

    return path


def _ops() -> Workspaces:
    """Build a Workspaces bound to a freshly loaded config."""
    return Workspaces(Config.load())


def _proj_ops() -> Project:
    """Build a Project bound to a freshly loaded config."""
    return Project(Config.load())


def _system(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str) -> None:
    """Declare a real system-scope config file (overriding the absent default)."""
    sys_path = tmp_path / "system.toml"
    sys_path.write_text(body)
    monkeypatch.setenv("WTM_SYSTEM_CONFIG", str(sys_path))


def _seed_user(cfg: Path, name: str, path: Path, **extra: str) -> None:
    """Write a minimal user-scope entry directly, for edit/rename/delete tests."""
    body = f'[workspaces.{name}]\npath = "{path}"\n'
    for key, value in extra.items():
        body += f'{key} = "{value}"\n'
    prev = cfg.read_text() if cfg.exists() else ""
    cfg.write_text(prev + body)


# --- arbitrary-key facility ---------------------------------------------------


def test_get_value_reads_scope_scalar(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    (ws / ".wtm").mkdir(parents=True)
    (ws / ".wtm" / "config.toml").write_text('description = "local"\n')
    config = Config.load()
    assert config.get_value(ConfigAddress.workspace(ws), "description") == "local"
    assert config.get_value(ConfigAddress.workspace(ws), "base_branch") is None


def test_set_value_roundtrip_preserves_comments(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    (ws / ".wtm").mkdir(parents=True)
    (ws / ".wtm" / "config.toml").write_text("# ws-local\ndescription = 'keep'\n")
    address = ConfigAddress.workspace(ws)
    Config.load().set_value(address, "base_branch", "main")
    text = (ws / ".wtm" / "config.toml").read_text()
    assert "# ws-local" in text
    assert "description = 'keep'" in text
    assert 'base_branch = "main"' in text
    Config.load().unset_value(address, "base_branch")
    assert "base_branch" not in (ws / ".wtm" / "config.toml").read_text()


def test_set_value_nested_table(user_cfg: Path, tmp_path: Path) -> None:
    proj = tmp_path / "w" / "proj"
    address = ConfigAddress.project(proj)
    Config.load().set_value(address, ("project", "base_branch"), "develop")
    text = (proj / ".wtm" / "config.toml").read_text()
    assert "[project]" in text
    assert 'base_branch = "develop"' in text
    Config.load().unset_value(address, ("project", "base_branch"))
    assert "develop" not in (proj / ".wtm" / "config.toml").read_text()


def test_set_value_writes_worktree_scope(user_cfg: Path, tmp_path: Path) -> None:
    # Worktree scope is now writable through the same facility.
    wt = tmp_path / "w" / "proj" / "worktrees" / "x"
    address = ConfigAddress.worktree(wt)
    Config.load().set_value(address, ("hooks", "on", "enter"), "echo hi")
    assert Config.load().get_value(address, ("hooks", "on", "enter")) == "echo hi"


def test_set_value_writes_system_scope(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # System scope is writable too (no WTM_NO_SYSTEM guard); it lands in the file.
    sys_path = tmp_path / "system.toml"
    monkeypatch.setenv("WTM_SYSTEM_CONFIG", str(sys_path))
    Config.load().set_value(ConfigAddress.system(), "base_branch", "trunk")
    assert 'base_branch = "trunk"' in sys_path.read_text()


def test_set_value_refused_under_no_config(
    user_cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WTM_NO_CONFIG", "1")
    config = Config.load()
    with pytest.raises(WtmError, match="WTM_NO_CONFIG"):
        config.set_value(ConfigAddress.workspace(tmp_path / "w"), "k", "v")


# --- array value facility ----------------------------------------------------


def test_add_to_array_creates_and_dedupes(user_cfg: Path, tmp_path: Path) -> None:
    # Set semantics: first add creates the array, a repeat add is a no-op.
    ws = tmp_path / "w"
    address = ConfigAddress.workspace(ws)
    Config.load().add_to_array(address, "worktree_ports", "3001")
    Config.load().add_to_array(address, "worktree_ports", "3002")
    Config.load().add_to_array(address, "worktree_ports", "3001")
    text = (ws / ".wtm" / "config.toml").read_text()
    assert 'worktree_ports = ["3001", "3002"]' in text


def test_remove_from_array_strips_all_occurrences(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    (ws / ".wtm").mkdir(parents=True)
    (ws / ".wtm" / "config.toml").write_text('ports = ["a", "b", "a"]\n')
    address = ConfigAddress.workspace(ws)
    Config.load().remove_from_array(address, "ports", "a")
    text = (ws / ".wtm" / "config.toml").read_text()
    assert 'ports = ["b"]' in text


def test_remove_from_array_absent_is_noop(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    address = ConfigAddress.workspace(ws)
    Config.load().remove_from_array(address, "ports", "x")  # file/key absent
    assert not (ws / ".wtm" / "config.toml").exists()


def test_add_to_array_kind_clash_on_scalar(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    (ws / ".wtm").mkdir(parents=True)
    (ws / ".wtm" / "config.toml").write_text('port = "3001"\n')
    address = ConfigAddress.workspace(ws)
    with pytest.raises(WtmError, match="not an array"):
        Config.load().add_to_array(address, "port", "x")


def test_set_table_value_creates_and_updates(user_cfg: Path, tmp_path: Path) -> None:
    # Set semantics: first write creates the table, a later write adds/updates a cell.
    ws = tmp_path / "w"
    address = ConfigAddress.workspace(ws)
    Config.load().set_table_value(address, "env", "REGION", "us-east")
    Config.load().set_table_value(address, "env", "TIER", "prod")
    Config.load().set_table_value(address, "env", "REGION", "eu-west")
    text = (ws / ".wtm" / "config.toml").read_text()
    assert 'REGION = "eu-west"' in text
    assert 'TIER = "prod"' in text


def test_unset_table_value_removes_cell_and_absent_is_noop(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    (ws / ".wtm").mkdir(parents=True)
    (ws / ".wtm" / "config.toml").write_text('[env]\nREGION = "us-east"\nTIER = "prod"\n')
    address = ConfigAddress.workspace(ws)
    Config.load().unset_table_value(address, "env", "REGION")
    text = (ws / ".wtm" / "config.toml").read_text()
    assert "REGION" not in text
    assert 'TIER = "prod"' in text
    Config.load().unset_table_value(address, "env", "NOPE")  # absent sub-key: no-op
    Config.load().unset_table_value(address, "absent", "x")  # absent key: no-op


def test_set_table_value_kind_clash_on_scalar(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    (ws / ".wtm").mkdir(parents=True)
    (ws / ".wtm" / "config.toml").write_text('env = "prod"\n')
    address = ConfigAddress.workspace(ws)
    with pytest.raises(WtmError, match="not a table"):
        Config.load().set_table_value(address, "env", "TIER", "prod")


# --- typed values (opt-in scalar writes) --------------------------------------


def test_set_value_typed_scalar_roundtrip(user_cfg: Path, tmp_path: Path) -> None:
    # A typed write lands as its native TOML kind and reads back native-typed.
    ws = tmp_path / "w"
    address = ConfigAddress.workspace(ws)
    config = Config.load()
    when = datetime.datetime(1979, 5, 27, 7, 32, tzinfo=datetime.UTC)
    config.set_value(address, "port", 8080)
    config.set_value(address, "ratio", 3.14)
    config.set_value(address, "flag", True)
    config.set_value(address, "when", when)
    text = (ws / ".wtm" / "config.toml").read_text()
    assert "port = 8080" in text
    assert "ratio = 3.14" in text
    assert "flag = true" in text
    assert "when = 1979-05-27T07:32:00Z" in text
    got = Config.load()
    port = got.get_value(address, "port")
    assert isinstance(port, int) and port == 8080
    assert isinstance(got.get_value(address, "flag"), bool)
    assert got.get_value(address, "when") == when


def test_set_value_bare_string_stays_string(user_cfg: Path, tmp_path: Path) -> None:
    # The default write is unchanged: VALUE lands quoted, whatever it looks like.
    ws = tmp_path / "w"
    address = ConfigAddress.workspace(ws)
    Config.load().set_value(address, "ver", "1.20")
    text = (ws / ".wtm" / "config.toml").read_text()
    assert 'ver = "1.20"' in text
    assert Config.load().get_value(address, "ver") == "1.20"


def test_typed_write_overwrites_kind_silently(user_cfg: Path, tmp_path: Path) -> None:
    # A kind change on overwrite is a silent replace (locality semantics untouched).
    ws = tmp_path / "w"
    address = ConfigAddress.workspace(ws)
    Config.load().set_value(address, "k", "text")
    Config.load().set_value(address, "k", 7)
    assert Config.load().get_value(address, "k") == 7


def test_array_add_typed_element_dedupes_across_kinds(user_cfg: Path, tmp_path: Path) -> None:
    # Identity is the unwrapped value: 8080 and "8080" stay distinct elements,
    # while a repeated typed add is idempotent.
    ws = tmp_path / "w"
    address = ConfigAddress.workspace(ws)
    Config.load().add_to_array(address, "nums", 8080)
    Config.load().add_to_array(address, "nums", "8080")
    Config.load().add_to_array(address, "nums", 8080)
    text = (ws / ".wtm" / "config.toml").read_text()
    assert 'nums = [8080, "8080"]' in text
    # Removal matches by native equality: the int goes, the string stays.
    Config.load().remove_from_array(address, "nums", 8080)
    assert Config.load().get_value(address, "nums") == ["8080"]


def test_set_table_value_typed_cell(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    address = ConfigAddress.workspace(ws)
    Config.load().set_table_value(address, ("limits",), "cpu", 4)
    text = (ws / ".wtm" / "config.toml").read_text()
    assert "cpu = 4" in text
    assert Config.load().get_value(address, ("limits", "cpu")) == 4


def test_parse_typed_value_formats() -> None:
    assert parse_typed_value("42", "int") == 42
    assert parse_typed_value("2.5", "float") == 2.5
    assert parse_typed_value("007", "str") == "007"
    for token in ("true", "TRUE", "1", "yes", "Y", "on", "t"):
        assert parse_typed_value(token, "bool") is True, token
    for token in ("false", "FALSE", "0", "no", "N", "off", "f", ""):
        assert parse_typed_value(token, "bool") is False, token
    with pytest.raises(WtmError, match="recognized"):
        parse_typed_value("maybe", "bool")
    with pytest.raises(WtmError, match="--type int"):
        parse_typed_value("12x", "int")
    with pytest.raises(WtmError, match="unknown value type"):
        parse_typed_value("1", "decimal")


def test_parse_toml_value_scalars_and_gate() -> None:
    when = parse_toml_value("1979-05-27T07:32:00Z")
    assert isinstance(when, datetime.datetime) and when.year == 1979
    assert parse_toml_value("'quoted'") == "quoted"
    assert parse_toml_value("-3") == -3
    for literal in ("[1, 2]", "{a = 1}", "'unclosed", "1 2"):
        with pytest.raises(WtmError):
            parse_toml_value(literal)


def test_render_scalar_toml_fidelity() -> None:
    assert render_scalar(True) == "true"
    assert render_scalar(False) == "false"
    assert render_scalar(8080) == "8080"
    assert render_scalar("text") == "text"
    when = datetime.datetime(1979, 5, 27, 7, 32, tzinfo=datetime.UTC)
    assert render_scalar(when) == "1979-05-27T07:32:00+00:00"
    assert render_scalar(datetime.date(1979, 5, 27)) == "1979-05-27"


def test_json_leaf_default_encodes_dates() -> None:
    when = datetime.datetime(1979, 5, 27, 7, 32, tzinfo=datetime.UTC)
    assert json.loads(json.dumps({"d": when}, default=json_leaf_default)) == {
        "d": "1979-05-27T07:32:00+00:00"
    }
    assert json.dumps({"b": True, "n": 8080}, default=json_leaf_default) == (
        '{"b": true, "n": 8080}'
    )


def test_typed_value_formats_are_the_documented_four() -> None:
    assert TYPED_VALUE_FORMATS == ("int", "float", "bool", "str")


# --- arbitrary-key sections (dotted paths + deep-merge) ----------------------


def test_validate_arbitrary_key_path_checks_head_only() -> None:
    # A dotted path's head segment is reserved-checked; deeper segments are
    # harmless cells (a `workspaces` cell under a user section is allowed).
    from wtm.config import validate_arbitrary_key_path

    validate_arbitrary_key_path("ci.runner.image")  # ok
    validate_arbitrary_key_path(("ci", "workspaces"))  # deep reserved segment ok
    with pytest.raises(WtmError, match="reserved key"):
        validate_arbitrary_key_path("hooks.on.x")
    with pytest.raises(WtmError, match="reserved key"):
        validate_arbitrary_key_path(("worktrees", "x"))


def test_table_extra_deep_merges_across_locality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A table-valued arbitrary key merges per-cell across one entity's locality
    # cascade (project-local file + workspace-file [projects.P]): the project's
    # `ci.registry` and the workspace's `ci.runner` both survive in `extra`.
    ws = tmp_path / "w"
    proj = ws / "proj"
    (proj / ".wtm").mkdir(parents=True)
    (proj / ".wtm" / "config.toml").write_text('[ci]\nregistry = "ghcr"\n')
    (ws / ".wtm").mkdir(parents=True)
    (ws / ".wtm" / "config.toml").write_text('[projects.proj.ci]\nrunner = "ubuntu"\n')
    body = f'[workspaces.a]\npath="{ws}"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=proj)
    annotation = cfg.project_annotation(cfg.workspaces["a"], "proj")
    ci = annotation.extra["ci"]
    assert ci == {"registry": "ghcr", "runner": "ubuntu"}


def test_nested_table_deep_merges_across_locality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Deep-merge recurses: cells under a shared sub-table combine across tiers.
    ws = tmp_path / "w"
    proj = ws / "proj"
    (proj / ".wtm").mkdir(parents=True)
    (proj / ".wtm" / "config.toml").write_text('[ci.runner]\nimage = "ubuntu"\n')
    (ws / ".wtm").mkdir(parents=True)
    (ws / ".wtm" / "config.toml").write_text('[projects.proj.ci.runner]\ntag = "latest"\n')
    body = f'[workspaces.a]\npath="{ws}"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=proj)
    annotation = cfg.project_annotation(cfg.workspaces["a"], "proj")
    assert annotation.extra["ci"] == {"runner": {"image": "ubuntu", "tag": "latest"}}


def test_table_subpath_kind_clash_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A scalar at `ci.runner` in one scope and a table at `ci.runner.image` in
    # another is a hard error (recursive kind check names the nested path).
    ws = tmp_path / "w"
    proj = ws / "proj"
    (proj / ".wtm").mkdir(parents=True)
    (proj / ".wtm" / "config.toml").write_text('[ci]\nrunner = "ubuntu"\n')
    (ws / ".wtm").mkdir(parents=True)
    (ws / ".wtm" / "config.toml").write_text('[projects.proj.ci.runner]\nimage = "x"\n')
    body = f'[workspaces.a]\npath="{ws}"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=proj)
    with pytest.raises(WtmError, match="ci.runner"):
        cfg.project_annotation(cfg.workspaces["a"], "proj")


def test_inheritable_table_deep_merges_from_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An inheritable table key merges cell-by-cell down the containment chain:
    # the project's `ci.registry` and the workspace's `ci.runner` combine.
    ws = tmp_path / "w"
    proj = ws / "proj"
    (proj / ".wtm").mkdir(parents=True)
    (proj / ".wtm" / "config.toml").write_text('[ci]\nregistry = "ghcr"\n')
    (ws / ".wtm").mkdir(parents=True)
    (ws / ".wtm" / "config.toml").write_text('[ci]\nrunner = "ubuntu"\n')
    body = f'inheritable = ["ci"]\n[workspaces.a]\npath="{ws}"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=proj)
    annotation = cfg.project_annotation(cfg.workspaces["a"], "proj")
    assert annotation.extra["ci"] == {"registry": "ghcr", "runner": "ubuntu"}


def test_inheritable_table_local_cell_wins_over_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # In a deep-merged inheritable table, the more-local cell overrides the
    # ancestor cell for the same sub-key (most-local wins per cell).
    ws = tmp_path / "w"
    proj = ws / "proj"
    (proj / ".wtm").mkdir(parents=True)
    (proj / ".wtm" / "config.toml").write_text('[ci]\nregistry = "local"\n')
    (ws / ".wtm").mkdir(parents=True)
    (ws / ".wtm" / "config.toml").write_text('[ci]\nregistry = "ws"\nrunner = "ubuntu"\n')
    body = f'inheritable = ["ci"]\n[workspaces.a]\npath="{ws}"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=proj)
    annotation = cfg.project_annotation(cfg.workspaces["a"], "proj")
    assert annotation.extra["ci"] == {"registry": "local", "runner": "ubuntu"}


def test_scalar_inheritable_key_still_skipped_when_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A scalar inheritable key present entity-locally still wins wholesale
    # (unchanged): the ancestor value is not injected.
    ws = tmp_path / "w"
    proj = ws / "proj"
    (proj / ".wtm").mkdir(parents=True)
    (proj / ".wtm" / "config.toml").write_text('deploy_env = "prod"\n')
    (ws / ".wtm").mkdir(parents=True)
    (ws / ".wtm" / "config.toml").write_text('deploy_env = "staging"\n')
    body = f'inheritable = ["deploy_env"]\n[workspaces.a]\npath="{ws}"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=proj)
    annotation = cfg.project_annotation(cfg.workspaces["a"], "proj")
    assert annotation.extra["deploy_env"] == "prod"


def test_effective_keys_deep_emits_dotted_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # deep=True walks table-valued keys, emitting every node (sub-table heads and
    # leaf cells) as a dotted path, each labelled by the most-local rung carrying
    # that cell -- the per-cell provenance material for deep dotted completion.
    ws = tmp_path / "w"
    proj = ws / "proj"
    (proj / ".wtm").mkdir(parents=True)
    (proj / ".wtm" / "config.toml").write_text('[ci.runner]\nimage = "ubuntu"\n')
    (ws / ".wtm").mkdir(parents=True)
    (ws / ".wtm" / "config.toml").write_text('[projects.proj.ci.runner]\ntag = "latest"\n')
    body = f'[workspaces.a]\npath="{ws}"\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=proj)
    target = cfg.resolve_generic_target(proj)
    deep = dict(cfg.effective_keys(target, deep=True))
    # Every node surfaces as a dotted path: the head, the sub-table, and both
    # leaves (the project's image, the workspace's tag).
    assert {"ci", "ci.runner", "ci.runner.image", "ci.runner.tag"} <= set(deep)
    # Per-cell winner: image lives in the project file, tag in the workspace file.
    assert deep["ci.runner.image"].scope == Scope.PROJECT
    assert deep["ci.runner.tag"].scope == Scope.WORKSPACE
    # The default (deep=False) stays top-level only: no dotted descendants.
    assert {k for k, _ in cfg.effective_keys(target)} == {"ci"}


def test_add_to_array_refused_under_no_config(
    user_cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WTM_NO_CONFIG", "1")
    with pytest.raises(WtmError, match="WTM_NO_CONFIG"):
        Config.load().add_to_array(ConfigAddress.workspace(tmp_path / "w"), "ports", "1")


def test_array_read_under_lock_preserves_concurrent_element(
    user_cfg: Path, tmp_path: Path
) -> None:
    # Two independent Configs (simulating two processes) both loaded before either
    # writes: the fresh re-read under the lock keeps both leases (no lost element).
    ws = tmp_path / "w"
    address = ConfigAddress.workspace(ws)
    a = Config.load()
    b = Config.load()
    a.add_to_array(address, "leased", "3001")
    b.add_to_array(address, "leased", "3002")
    text = (ws / ".wtm" / "config.toml").read_text()
    assert '"3001"' in text and '"3002"' in text


def test_scalar_write_does_not_clobber_concurrent_key(user_cfg: Path, tmp_path: Path) -> None:
    # Every write is a fresh read-modify-write: two Configs loaded before either
    # writes a *different* scalar to the same file -- both keys survive. The
    # workspace is declared so its local file is cached at load (a stale-cache
    # write would clobber; the fresh re-read under the lock does not).
    ws = tmp_path / "w"
    ws.mkdir()
    user_cfg.write_text(f'[workspaces.w]\npath = "{ws}"\n')
    address = ConfigAddress.workspace(ws)
    a = Config.load()
    b = Config.load()
    a.set_value(address, "k1", "v1")
    b.set_value(address, "k2", "v2")
    text = (ws / ".wtm" / "config.toml").read_text()
    assert 'k1 = "v1"' in text
    assert 'k2 = "v2"' in text


def test_mixed_scalar_and_array_writes_coexist(user_cfg: Path, tmp_path: Path) -> None:
    # A scalar write and an array mutation to the same file from two Configs both
    # loaded beforehand do not clobber one another.
    ws = tmp_path / "w"
    ws.mkdir()
    user_cfg.write_text(f'[workspaces.w]\npath = "{ws}"\n')
    address = ConfigAddress.workspace(ws)
    a = Config.load()
    b = Config.load()
    a.set_value(address, "editor", "nvim")
    b.add_to_array(address, "ports", "3001")
    text = (ws / ".wtm" / "config.toml").read_text()
    assert 'editor = "nvim"' in text
    assert 'ports = ["3001"]' in text


def test_structural_write_does_not_clobber_concurrent_key(user_cfg: Path, tmp_path: Path) -> None:
    # The structural tier (add_workspace) rides the same transaction: a concurrent
    # top-level base_branch write on the user file survives the workspace add.
    (tmp_path / "one").mkdir()
    a = Config.load()
    b = Config.load()
    a.set_base_branch("trunk")
    b.add_workspace("one", str(tmp_path / "one"))
    text = user_cfg.read_text()
    assert 'base_branch = "trunk"' in text
    assert "[workspaces.one]" in text


def test_arrays_do_not_merge_across_scopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An arbitrary-key array reads most-local-whole-array (replace), never a cross-scope
    # union -- so distinct per-tier names are the way to keep per-scope arrays.
    ws = tmp_path / "w"
    proj = ws / "proj"
    (proj / ".wtm").mkdir(parents=True)
    (proj / ".wtm" / "config.toml").write_text('ports = ["p1"]\n')
    body = f'[workspaces.a]\npath="{ws}"\n[workspaces.a.projects.proj]\nports=["w1"]\n'
    cfg = _load(monkeypatch, tmp_path, body, cwd=proj)
    target = cfg.resolve_generic_target(proj)
    effective, _ = cfg.resolve_generic_value(target, "ports")
    assert list(effective) == ["p1"]  # project-local whole array wins, no union


# --- add ---------------------------------------------------------------------


def test_add_creates_file_and_entry(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "work"
    ws.mkdir()
    result = _ops().add("work", str(ws), description="day job", base_branch="main", create=False)
    assert not result.missing and not result.created
    text = user_cfg.read_text()
    assert "[workspaces.work]" in text
    assert f'path = "{ws}"' in text
    assert 'description = "day job"' in text
    assert 'base_branch = "main"' in text


def test_add_defaults_path_to_cwd(
    user_cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    here = tmp_path / "here"
    here.mkdir()
    monkeypatch.chdir(here)
    result = _ops().add("h", None, description=None, base_branch=None, create=False)
    assert result.resolved == here
    assert f'path = "{here}"' in user_cfg.read_text()


def test_add_preserves_tilde_form(
    user_cfg: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _ops().add("h", "~/proj", description=None, base_branch=None, create=False)
    assert 'path = "~/proj"' in user_cfg.read_text()


def test_add_create_makes_dir(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "made"
    result = _ops().add("m", str(ws), description=None, base_branch=None, create=True)
    assert result.created and ws.is_dir()


def test_add_missing_path_flagged(user_cfg: Path, tmp_path: Path) -> None:
    result = _ops().add(
        "m", str(tmp_path / "nope"), description=None, base_branch=None, create=False
    )
    assert result.missing and not result.created


def test_add_rejects_file_path(user_cfg: Path, tmp_path: Path) -> None:
    afile = tmp_path / "afile"
    afile.write_text("x")
    with pytest.raises(WtmError, match="not a directory"):
        _ops().add("m", str(afile), description=None, base_branch=None, create=False)


def test_add_rejects_relative_after_expansion(user_cfg: Path) -> None:
    with pytest.raises(WtmError, match="absolute"):
        _ops().add("m", "$UNSET_VAR_XYZ", description=None, base_branch=None, create=False)


def test_add_duplicate_user_name_errors(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    _ops().add("w", str(ws), description=None, base_branch=None, create=False)
    with pytest.raises(WtmError, match="already declared"):
        _ops().add("w", str(ws), description=None, base_branch=None, create=False)


def test_add_duplicate_path_warns_not_errors(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "shared"
    ws.mkdir()
    _ops().add("a", str(ws), description=None, base_branch=None, create=False)
    result = _ops().add("b", str(ws), description=None, base_branch=None, create=False)
    assert result.duplicate_of == "a"
    assert "[workspaces.b]" in user_cfg.read_text()


def test_add_shadows_system(
    user_cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    _system(monkeypatch, tmp_path, f'[workspaces.w]\npath = "{ws}"\n')
    result = _ops().add("w", str(ws), description=None, base_branch=None, create=False)
    assert result.shadows_system
    assert "[workspaces.w]" in user_cfg.read_text()


def test_add_rejects_bad_name(user_cfg: Path, tmp_path: Path) -> None:
    with pytest.raises(WtmError, match="may not contain"):
        _ops().add("a/b", str(tmp_path), description=None, base_branch=None, create=False)


def test_add_preserves_comments_and_other_keys(user_cfg: Path, tmp_path: Path) -> None:
    user_cfg.write_text(
        "# my wtm config\n"
        'default_workspace = "keep"\n\n'
        '[workspaces.keep]  # note\npath = "/tmp/keep"\n'
    )
    ws = tmp_path / "new"
    ws.mkdir()
    _ops().add("new", str(ws), description=None, base_branch=None, create=False)
    text = user_cfg.read_text()
    assert "# my wtm config" in text
    assert 'default_workspace = "keep"' in text
    assert "[workspaces.keep]  # note" in text
    assert "[workspaces.new]" in text


def test_atomic_write_leaves_no_temp(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    _ops().add("w", str(ws), description=None, base_branch=None, create=False)
    leftovers = [p.name for p in user_cfg.parent.iterdir() if p.name.startswith(".wtm-config.")]
    assert leftovers == []


def test_add_refused_under_no_config(
    user_cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WTM_NO_CONFIG", "1")
    with pytest.raises(WtmError, match="WTM_NO_CONFIG"):
        Config.load().add_workspace("w", str(tmp_path))


# --- set ---------------------------------------------------------------------


def test_set_updates_fields_local(user_cfg: Path, tmp_path: Path) -> None:
    # The workspace dir exists, so description/base_branch land in its
    # workspace-local <ws>/.wtm/config.toml, not the user-scope registry entry.
    ws = tmp_path / "w"
    ws.mkdir()
    _seed_user(user_cfg, "w", ws)
    _ops().set("w", path=None, description="edited", base_branch="dev")
    local = (ws / ".wtm" / "config.toml").read_text()
    assert 'description = "edited"' in local
    assert 'base_branch = "dev"' in local
    assert "edited" not in user_cfg.read_text()


def test_set_falls_back_to_registry_when_dir_absent(user_cfg: Path, tmp_path: Path) -> None:
    # No workspace dir on disk -> scalars fall back to the user-scope entry.
    ws = tmp_path / "gone"
    _seed_user(user_cfg, "w", ws)
    _ops().set("w", path=None, description="edited", base_branch="dev")
    text = user_cfg.read_text()
    assert 'description = "edited"' in text
    assert 'base_branch = "dev"' in text


def test_set_empty_clears_local_field(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    _seed_user(user_cfg, "w", ws)
    _ops().set("w", path=None, description="first", base_branch=None)
    assert 'description = "first"' in (ws / ".wtm" / "config.toml").read_text()
    _ops().set("w", path=None, description="", base_branch=None)
    assert "description" not in (ws / ".wtm" / "config.toml").read_text()


def test_set_empty_path_rejected(user_cfg: Path, tmp_path: Path) -> None:
    _seed_user(user_cfg, "w", tmp_path)
    with pytest.raises(WtmError, match="may not be empty"):
        _ops().set("w", path="", description=None, base_branch=None)


def test_set_requires_a_field(user_cfg: Path, tmp_path: Path) -> None:
    _seed_user(user_cfg, "w", tmp_path)
    with pytest.raises(WtmError, match="nothing to set"):
        _ops().set("w", path=None, description=None, base_branch=None)


def test_set_unknown_errors(user_cfg: Path) -> None:
    with pytest.raises(WtmError, match="unknown workspace"):
        _ops().set("ghost", path=None, description="x", base_branch=None)


def test_set_system_only_local_allowed(
    user_cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A system-declared workspace whose dir exists can carry a workspace-local
    # override (the system-only refusal applies only to the registry fallback).
    ws = tmp_path / "s"
    ws.mkdir()
    _system(monkeypatch, tmp_path, f'[workspaces.s]\npath = "{ws}"\n')
    _ops().set("s", path=None, description="x", base_branch=None)
    assert 'description = "x"' in (ws / ".wtm" / "config.toml").read_text()


def test_set_system_only_registry_fallback_refused(
    user_cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # System-only + no dir on disk -> the registry fallback is refused.
    ws = tmp_path / "gone"
    _system(monkeypatch, tmp_path, f'[workspaces.s]\npath = "{ws}"\n')
    with pytest.raises(WtmError, match="system scope"):
        _ops().set("s", path=None, description="x", base_branch=None)


# --- rename ------------------------------------------------------------------


def test_rename_moves_key_and_fixes_default(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    user_cfg.write_text(
        f'default_workspace = "old"\n[workspaces.old]\npath = "{ws}"\ndescription = "d"\n'
    )
    result = _ops().rename("old", "new")
    assert result.fixed_default
    text = user_cfg.read_text()
    assert "[workspaces.new]" in text and "[workspaces.old]" not in text
    assert 'description = "d"' in text  # subtable/scalars carried over
    assert 'default_workspace = "new"' in text


def test_rename_new_collision_errors(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    _seed_user(user_cfg, "a", ws)
    _seed_user(user_cfg, "b", ws)
    with pytest.raises(WtmError, match="already declared"):
        _ops().rename("a", "b")


def test_rename_system_only_refused(
    user_cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _system(monkeypatch, tmp_path, f'[workspaces.s]\npath = "{tmp_path}"\n')
    with pytest.raises(WtmError, match="system scope"):
        _ops().rename("s", "t")


# --- delete ------------------------------------------------------------------


def test_delete_removes_entry_and_clears_default(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    user_cfg.write_text(f'default_workspace = "w"\n[workspaces.w]\npath = "{ws}"\n')
    result = _ops().delete("w")
    assert result.cleared_default and not result.remains_at_system
    text = user_cfg.read_text()
    assert "[workspaces.w]" not in text
    assert "default_workspace" not in text


def test_delete_unknown_errors(user_cfg: Path) -> None:
    with pytest.raises(WtmError, match="not declared"):
        _ops().delete("ghost")


def test_delete_system_only_refused(
    user_cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _system(monkeypatch, tmp_path, f'[workspaces.s]\npath = "{tmp_path}"\n')
    with pytest.raises(WtmError, match="only at system scope"):
        _ops().delete("s")


def test_delete_both_scopes_warns_system_remains(
    user_cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    _system(monkeypatch, tmp_path, f'[workspaces.w]\npath = "{ws}"\n')
    _seed_user(user_cfg, "w", ws)
    result = _ops().delete("w")
    assert result.remains_at_system
    assert "[workspaces.w]" not in user_cfg.read_text()


# --- default -----------------------------------------------------------------


def test_default_set_requires_declared(user_cfg: Path) -> None:
    with pytest.raises(WtmError, match="not declared"):
        _ops().default_set("ghost")


def test_default_set_and_clear(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    _seed_user(user_cfg, "w", ws)
    _ops().default_set("w")
    assert 'default_workspace = "w"' in user_cfg.read_text()
    _ops().default_clear()
    assert "default_workspace" not in user_cfg.read_text()


# --- scope detection ---------------------------------------------------------


def test_declaring_scopes(user_cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    _system(
        monkeypatch,
        tmp_path,
        f'[workspaces.both]\npath = "{ws}"\n[workspaces.sysonly]\npath = "{ws}"\n',
    )
    _seed_user(user_cfg, "both", ws)
    _seed_user(user_cfg, "useronly", ws)
    config = Config.load()
    assert config.declaring_scopes("both") == {Scope.SYSTEM, Scope.USER}
    assert config.declaring_scopes("sysonly") == {Scope.SYSTEM}
    assert config.declaring_scopes("useronly") == {Scope.USER}
    assert config.declaring_scopes("nobody") == set()


# --- global base_branch (top-level) ------------------------------------------


def test_base_branch_set_clear_and_source(user_cfg: Path) -> None:
    _ops().config.set_base_branch("main")
    assert 'base_branch = "main"' in user_cfg.read_text()
    assert Config.load().base_branch_source() is Scope.USER
    _ops().config.clear_base_branch()
    assert "base_branch" not in user_cfg.read_text()
    assert Config.load().base_branch_source() is None


def test_base_branch_source_prefers_user_over_system(
    user_cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _system(monkeypatch, tmp_path, 'base_branch = "trunk"\n')
    assert Config.load().base_branch_source() is Scope.SYSTEM
    _ops().config.set_base_branch("main")
    assert Config.load().base_branch_source() is Scope.USER


# --- project annotation (registry fallback) ----------------------------------


def test_project_set_writes_local_when_dir_exists(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    (ws / "api").mkdir(parents=True)
    _seed_user(user_cfg, "w", ws)
    result = _proj_ops().set("w", "api", base_branch="develop", description="the api")
    assert result.scope is Scope.PROJECT
    local = (ws / "api" / ".wtm" / "config.toml").read_text()
    assert 'base_branch = "develop"' in local
    assert 'description = "the api"' in local
    assert "develop" not in user_cfg.read_text()  # not the registry


def test_project_set_defaults_to_workspace_when_only_ws_dir_exists(
    user_cfg: Path, tmp_path: Path
) -> None:
    # Project dir absent, workspace dir present -> deepest existing = workspace-local.
    ws = tmp_path / "w"
    ws.mkdir()
    _seed_user(user_cfg, "w", ws)
    result = _proj_ops().set("w", "api", base_branch="develop", description=None)
    assert result.scope is Scope.WORKSPACE
    text = (ws / ".wtm" / "config.toml").read_text()
    assert "[projects.api]" in text
    assert 'base_branch = "develop"' in text
    assert "develop" not in user_cfg.read_text()


def test_project_set_scope_user_writes_registry(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    (ws / "api").mkdir(parents=True)
    _seed_user(user_cfg, "w", ws)
    result = _proj_ops().set("w", "api", base_branch="develop", description=None, scope=Scope.USER)
    assert result.scope is Scope.USER
    text = user_cfg.read_text()
    assert "[workspaces.w.projects.api]" in text
    assert 'base_branch = "develop"' in text


def test_project_set_scope_workspace_writes_local(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    (ws / "api").mkdir(parents=True)
    _seed_user(user_cfg, "w", ws)
    _proj_ops().set("w", "api", base_branch="develop", description=None, scope=Scope.WORKSPACE)
    text = (ws / ".wtm" / "config.toml").read_text()
    assert "[projects.api]" in text
    assert 'base_branch = "develop"' in text


def test_project_set_scope_user_requires_user_workspace_declaration(
    user_cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A system-only workspace cannot receive a user-scope project annotation.
    ws = tmp_path / "w"
    (ws / "api").mkdir(parents=True)
    _system(monkeypatch, tmp_path, f'[workspaces.w]\npath = "{ws}"\n')
    with pytest.raises(WtmError, match="not declared at user scope"):
        _proj_ops().set("w", "api", base_branch="develop", description=None, scope=Scope.USER)


def test_project_set_requires_a_field(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    _seed_user(user_cfg, "w", ws)
    with pytest.raises(WtmError, match="nothing to set"):
        _proj_ops().set("w", "api", base_branch=None, description=None)


def test_project_show_base_branch_provenance(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    (ws / "api").mkdir(parents=True)
    _seed_user(user_cfg, "w", ws, base_branch="wsdefault")
    # Workspace-tier default until the project overrides it.
    shown = _proj_ops().show("w", "api")
    assert shown.effective_base_branch == "wsdefault"
    assert shown.base_branch_source == "workspace"
    _proj_ops().set("w", "api", base_branch="develop", description=None)
    shown = _proj_ops().show("w", "api")
    assert shown.effective_base_branch == "develop"
    assert shown.base_branch_source == "project"


# --- worktree set ------------------------------------------------------------


def _wt_ops() -> Worktree:
    """Build a Worktree bound to a freshly loaded config."""
    return Worktree(Config.load())


def test_worktree_set_writes_local_when_wt_dir_exists(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    wt = ws / "api" / "worktrees" / "feat"
    wt.mkdir(parents=True)
    _seed_user(user_cfg, "w", ws)
    result = _wt_ops().set(["api", "feat"], description="the feature", scope=None)
    assert result.scope is Scope.WORKTREE
    local = (wt / ".wtm" / "config.toml").read_text()
    assert 'description = "the feature"' in local


def test_worktree_set_defaults_to_project_when_only_project_dir_exists(
    user_cfg: Path, tmp_path: Path
) -> None:
    # Worktree dir absent, project dir present -> deepest existing = project-local.
    ws = tmp_path / "w"
    (ws / "api").mkdir(parents=True)
    _seed_user(user_cfg, "w", ws)
    result = _wt_ops().set(["api", "feat"], description="planned", scope=None)
    assert result.scope is Scope.PROJECT
    text = (ws / "api" / ".wtm" / "config.toml").read_text()
    assert "[worktrees.feat]" in text
    assert 'description = "planned"' in text


def test_worktree_set_scope_workspace_writes_ws_local(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    (ws / "api").mkdir(parents=True)
    _seed_user(user_cfg, "w", ws)
    _wt_ops().set(["api", "feat"], description="deep", scope=Scope.WORKSPACE)
    text = (ws / ".wtm" / "config.toml").read_text()
    assert "[projects.api.worktrees.feat]" in text
    assert 'description = "deep"' in text


def test_worktree_set_scope_user_writes_registry(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    (ws / "api").mkdir(parents=True)
    _seed_user(user_cfg, "w", ws)
    result = _wt_ops().set(["api", "feat"], description="global", scope=Scope.USER)
    assert result.scope is Scope.USER
    text = user_cfg.read_text()
    assert "[workspaces.w.projects.api.worktrees.feat]" in text
    assert 'description = "global"' in text


def test_worktree_set_empty_clears(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    wt = ws / "api" / "worktrees" / "feat"
    wt.mkdir(parents=True)
    _seed_user(user_cfg, "w", ws)
    _wt_ops().set(["api", "feat"], description="temp", scope=None)
    _wt_ops().set(["api", "feat"], description="", scope=None)
    local = (wt / ".wtm" / "config.toml").read_text()
    assert "description" not in local


def test_worktree_set_requires_a_field(user_cfg: Path, tmp_path: Path) -> None:
    ws = tmp_path / "w"
    (ws / "api").mkdir(parents=True)
    _seed_user(user_cfg, "w", ws)
    with pytest.raises(WtmError, match="nothing to set"):
        _wt_ops().set(["api", "feat"], description=None, scope=None)


def test_worktree_set_scope_user_requires_user_workspace_declaration(
    user_cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A system-only workspace cannot receive a user-scope worktree annotation.
    ws = tmp_path / "w"
    (ws / "api").mkdir(parents=True)
    _system(monkeypatch, tmp_path, f'[workspaces.w]\npath = "{ws}"\n')
    with pytest.raises(WtmError, match="not declared at user scope"):
        _wt_ops().set(["api", "feat"], description="x", scope=Scope.USER)


def test_worktree_set_roundtrips_through_read_cascade(user_cfg: Path, tmp_path: Path) -> None:
    # A worktree-local write is seen by the deep read-cascade at the worktree tier.
    ws = tmp_path / "w"
    wt = ws / "api" / "worktrees" / "feat"
    wt.mkdir(parents=True)
    _seed_user(user_cfg, "w", ws)
    _wt_ops().set(["api", "feat"], description="round", scope=None)
    cfg = Config.load(cwd=wt)
    target = cfg.cwd_location(cwd=wt)
    assert target is not None and target.tier == "worktree"
    slice_ = cfg.resolve_slice(target=target)
    worktree = slice_.workspaces[0].projects[0].worktrees[0]
    assert worktree.description == "round"


# --- config slice (resolve_slice) ---------------------------------------------


def test_declared_worktree_names_unions_probe_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    body = (
        f'[workspaces.w]\npath = "{ws}"\n'
        '[workspaces.w.projects.api.worktrees.feat]\ndescription = "x"\n'
    )
    cfg = _load(monkeypatch, tmp_path, body, cwd=tmp_path)
    assert cfg.declared_worktree_names(cfg.workspaces["w"], "api") == {"feat"}


def _slice_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, cwd: Path | None = None
) -> Config:
    """Build a two-workspace, two-project, two-worktree config for slice tests."""
    ws = tmp_path / "w"
    (ws / "api" / ".git").mkdir(parents=True)
    (ws / "api" / "worktrees" / "main").mkdir(parents=True)
    (ws / "api" / "worktrees" / "feat").mkdir(parents=True)
    (ws / "web" / ".git").mkdir(parents=True)
    (ws / "web" / "worktrees" / "draft").mkdir(parents=True)
    body = (
        f'base_branch = "main"\n'
        f'[workspaces.w]\npath = "{ws}"\n'
        '[workspaces.w.projects.api]\nbase_branch = "develop"\n'
        '[workspaces.w.projects.api.worktrees.planned]\ndescription = "later"\n'
        '[workspaces.w.projects.ghost]\ndescription = "gone"\n'
    )
    return _load(monkeypatch, tmp_path, body, cwd=cwd or tmp_path)


def test_resolve_slice_global_branch_only(tmp_path, monkeypatch) -> None:
    cfg = _slice_setup(tmp_path, monkeypatch)
    s = cfg.resolve_slice(target=None)
    assert s.depth_limit == 0
    assert s.default_base_branch == "main"
    assert s.workspaces == ()


def test_resolve_slice_global_recurse_descends_all(tmp_path, monkeypatch) -> None:
    cfg = _slice_setup(tmp_path, monkeypatch)
    s = cfg.resolve_slice(target=None, recurse=True)
    assert s.depth_limit == 3
    (ws_node,) = s.workspaces
    names = {p.name for p in ws_node.projects}
    assert names == {"api", "ghost", "web"}
    api = next(p for p in ws_node.projects if p.name == "api")
    assert api.effective_base_branch == "develop"
    assert {w.name for w in api.worktrees} == {"main", "feat", "planned"}
    ghost = next(p for p in ws_node.projects if p.name == "ghost")
    assert ghost.exists is False


def test_resolve_slice_workspace_branch_only(tmp_path, monkeypatch) -> None:
    cfg = _slice_setup(tmp_path, monkeypatch)
    target = SliceTarget(tier="workspace", workspace="w")
    s = cfg.resolve_slice(target=target)
    assert s.depth_limit == 1
    (ws_node,) = s.workspaces
    assert ws_node.entry.name == "w"
    assert ws_node.projects == ()


def test_resolve_slice_workspace_recurse_all_projects(tmp_path, monkeypatch) -> None:
    cfg = _slice_setup(tmp_path, monkeypatch)
    target = SliceTarget(tier="workspace", workspace="w")
    s = cfg.resolve_slice(target=target, recurse=True)
    assert s.depth_limit == 3
    (ws_node,) = s.workspaces
    names = {p.name for p in ws_node.projects}
    assert names == {"api", "ghost", "web"}
    api = next(p for p in ws_node.projects if p.name == "api")
    assert {w.name for w in api.worktrees} == {"main", "feat", "planned"}


def test_resolve_slice_project_branch_only(tmp_path, monkeypatch) -> None:
    cfg = _slice_setup(tmp_path, monkeypatch)
    target = SliceTarget(tier="project", workspace="w", project="api")
    s = cfg.resolve_slice(target=target)
    assert s.depth_limit == 2
    (ws_node,) = s.workspaces
    (proj,) = ws_node.projects
    assert proj.name == "api"
    assert proj.worktrees == ()


def test_resolve_slice_project_recurse_all_worktrees(tmp_path, monkeypatch) -> None:
    cfg = _slice_setup(tmp_path, monkeypatch)
    target = SliceTarget(tier="project", workspace="w", project="api")
    s = cfg.resolve_slice(target=target, recurse=True)
    assert s.depth_limit == 3
    (ws_node,) = s.workspaces
    (proj,) = ws_node.projects
    assert {w.name for w in proj.worktrees} == {"main", "feat", "planned"}


def test_resolve_slice_worktree_branch_only(tmp_path, monkeypatch) -> None:
    cfg = _slice_setup(tmp_path, monkeypatch)
    target = SliceTarget(tier="worktree", workspace="w", project="api", name="feat")
    s = cfg.resolve_slice(target=target)
    assert s.depth_limit == 3
    (ws_node,) = s.workspaces
    (proj,) = ws_node.projects
    (wt,) = proj.worktrees
    assert wt.name == "feat"


def test_resolve_slice_worktree_recurse_no_op(tmp_path, monkeypatch) -> None:
    """-r from a worktree (leaf) adds nothing: no sibling worktrees."""
    cfg = _slice_setup(tmp_path, monkeypatch)
    target = SliceTarget(tier="worktree", workspace="w", project="api", name="feat")
    s = cfg.resolve_slice(target=target, recurse=True)
    (ws_node,) = s.workspaces
    (proj,) = ws_node.projects
    (wt,) = proj.worktrees
    assert wt.name == "feat"


def test_resolve_slice_depth_prunes_worktree_from_worktree_cwd(tmp_path, monkeypatch) -> None:
    """-d project from a worktree target prunes the worktree, leaving project+ws+global."""
    cfg = _slice_setup(tmp_path, monkeypatch)
    target = SliceTarget(tier="worktree", workspace="w", project="api", name="feat")
    s = cfg.resolve_slice(target=target, depth_limit=2)
    assert s.depth_limit == 2
    (ws_node,) = s.workspaces
    (proj,) = ws_node.projects
    assert proj.name == "api"
    assert proj.worktrees == ()


def test_resolve_slice_depth_global_cap(tmp_path, monkeypatch) -> None:
    """-d global renders only the global tier."""
    cfg = _slice_setup(tmp_path, monkeypatch)
    s = cfg.resolve_slice(target=None, recurse=True, depth_limit=0)
    assert s.workspaces == ()


def test_resolve_slice_depth_project_with_recurse(tmp_path, monkeypatch) -> None:
    """-d project with -r from a workspace: all projects, no worktrees."""
    cfg = _slice_setup(tmp_path, monkeypatch)
    target = SliceTarget(tier="workspace", workspace="w")
    s = cfg.resolve_slice(target=target, recurse=True, depth_limit=2)
    (ws_node,) = s.workspaces
    names = {p.name for p in ws_node.projects}
    assert names == {"api", "ghost", "web"}
    for p in ws_node.projects:
        assert p.worktrees == ()


def test_resolve_slice_depth_below_target_no_op(tmp_path, monkeypatch) -> None:
    """-d worktree (deeper than target=project) without -r is a silent no-op."""
    cfg = _slice_setup(tmp_path, monkeypatch)
    target = SliceTarget(tier="project", workspace="w", project="api")
    s = cfg.resolve_slice(target=target, depth_limit=3)
    (ws_node,) = s.workspaces
    (proj,) = ws_node.projects
    assert proj.name == "api"
    assert proj.worktrees == ()


def test_resolve_slice_cwd_inference_worktree(tmp_path, monkeypatch) -> None:
    wt_dir = tmp_path / "w" / "api" / "worktrees" / "feat"
    cfg = _slice_setup(tmp_path, monkeypatch, cwd=wt_dir)
    target = cfg.cwd_location(cwd=wt_dir)
    assert target is not None
    assert target.tier == "worktree"
    assert target.workspace == "w"
    assert target.project == "api"
    assert target.name == "feat"


def test_resolve_slice_cwd_inference_project(tmp_path, monkeypatch) -> None:
    proj_dir = tmp_path / "w" / "api"
    cfg = _slice_setup(tmp_path, monkeypatch, cwd=proj_dir)
    target = cfg.cwd_location(cwd=proj_dir)
    assert target is not None
    assert target.tier == "project"
    assert target.workspace == "w"
    assert target.project == "api"


def test_resolve_slice_cwd_inference_workspace(tmp_path, monkeypatch) -> None:
    ws_dir = tmp_path / "w"
    cfg = _slice_setup(tmp_path, monkeypatch, cwd=ws_dir)
    target = cfg.cwd_location(cwd=ws_dir)
    assert target is not None
    assert target.tier == "workspace"
    assert target.workspace == "w"


def test_resolve_slice_cwd_outside_workspace_is_global(tmp_path, monkeypatch) -> None:
    cfg = _slice_setup(tmp_path, monkeypatch, cwd=tmp_path)
    target = cfg.cwd_location(cwd=tmp_path)
    assert target is None


def test_resolve_slice_global_extra_and_hooks(tmp_path, monkeypatch) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    body = (
        f'base_branch = "main"\nteam = "alpha"\n'
        f'[hooks.on.enter]\ncommand = "echo hi"\n'
        f'[workspaces.w]\npath = "{ws}"\n'
    )
    cfg = _load(monkeypatch, tmp_path, body, cwd=tmp_path)
    s = cfg.resolve_slice(target=None, recurse=True)
    assert s.global_extra.get("team") == "alpha"
    assert "enter" in s.global_hooks.on
    assert s.base_branch_source == "user"
