"""Pure, subprocess-free tests for the single config authority (:mod:`wtm.config`).

Both sides of the engine are exercised directly against :meth:`Config.load` and
its methods, driven by TOML files under ``tmp_path`` and env vars, with no
``wtm`` invocation and no git: config *resolution* (the workspace registry,
active-workspace precedence, per-scope hook merging, base-branch cascade) and
config *writes* (the generic value facility, the structural registry mutations,
comment preservation, scope policy, and the ``default_workspace`` fixups).
Lifecycle (enter/leave) and CLI wiring live in :mod:`tests.test_wtm`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wtm.config import Config, ConfigAddress, Scope
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
    # A non-reserved key resolves most-local-first and lands in `extra`.
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
    resolved = cfg.resolve_view(cwd=wt)
    worktree = resolved.project.worktree
    assert worktree is not None
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
    # A non-reserved workspace key resolves most-local-first into Workspace.extra
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
# Writes: the generic value facility + the structural registry mutations.
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


# --- generic value facility --------------------------------------------------


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
    resolved = Config.load(cwd=wt).resolve_view(cwd=wt)
    assert resolved.project.worktree is not None
    assert resolved.project.worktree.description == "round"
