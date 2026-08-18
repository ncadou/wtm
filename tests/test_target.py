"""Pure, subprocess-free tests for the target grammar (:mod:`wtm.target`).

The grammar is the test surface: :func:`resolve` and :func:`slot_kind` take the
workspace registry and cwd explicitly, so every mode and edge case is exercised
without spawning ``wtm`` or touching git.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wtm.core import WtmError
from wtm.target import (
    TargetMode,
    TokenKind,
    resolve,
    slot_kind,
    split_target,
    split_workspace_project,
)

ROOT = Path("/projects")
OTHER = Path("/other")
ROOTS = {"main": ROOT, "other": OTHER}


def r(mode: TargetMode, args: list[str], *, cwd: Path = ROOT) -> Path:
    return resolve(mode, args, roots=ROOTS, active_root=ROOT, cwd=cwd)


# --- resolve: PROJECT NAME (two args) ----------------------------------------


@pytest.mark.parametrize("mode", [TargetMode.WORKTREE, TargetMode.DELETE, TargetMode.NAME_ONLY])
def test_project_name_resolves_under_worktrees(mode: TargetMode) -> None:
    assert r(mode, ["alpha", "feat"]) == ROOT / "alpha" / "worktrees" / "feat"


@pytest.mark.parametrize("mode", [TargetMode.WORKTREE, TargetMode.DELETE, TargetMode.NAME_ONLY])
def test_nested_name_kept(mode: TargetMode) -> None:
    assert r(mode, ["alpha", "feature/x"]) == ROOT / "alpha" / "worktrees" / "feature" / "x"


@pytest.mark.parametrize("mode", [TargetMode.WORKTREE, TargetMode.DELETE])
def test_traversal_rejected(mode: TargetMode) -> None:
    with pytest.raises(WtmError, match="'\\.\\.'"):
        r(mode, ["alpha", "../escape"])


def test_name_only_traversal_rejected_as_path_first() -> None:
    # NAME_ONLY path-checks the NAME before the traversal check (a '..' NAME
    # looks like a path), matching the original create/resolve grammar.
    with pytest.raises(WtmError, match="not a path"):
        r(TargetMode.NAME_ONLY, ["alpha", "../escape"])


@pytest.mark.parametrize("mode", [TargetMode.WORKTREE, TargetMode.DELETE, TargetMode.NAME_ONLY])
def test_bad_project_name_rejected(mode: TargetMode) -> None:
    with pytest.raises(WtmError, match="may not contain '/'"):
        r(mode, ["a/b", "feat"])


# --- resolve: WORKSPACE PROJECT NAME (three args) ----------------------------


@pytest.mark.parametrize("mode", [TargetMode.WORKTREE, TargetMode.DELETE, TargetMode.NAME_ONLY])
def test_workspace_project_name_resolves_under_named_root(mode: TargetMode) -> None:
    assert r(mode, ["other", "alpha", "feat"]) == OTHER / "alpha" / "worktrees" / "feat"


@pytest.mark.parametrize("mode", [TargetMode.WORKTREE, TargetMode.DELETE, TargetMode.NAME_ONLY])
def test_unknown_workspace_rejected(mode: TargetMode) -> None:
    with pytest.raises(WtmError, match="unknown workspace: 'nope'"):
        r(mode, ["nope", "alpha", "feat"])


@pytest.mark.parametrize("mode", [TargetMode.WORKTREE, TargetMode.DELETE])
def test_three_arg_traversal_rejected(mode: TargetMode) -> None:
    with pytest.raises(WtmError, match="'\\.\\.'"):
        r(mode, ["other", "alpha", "../escape"])


# --- resolve: single NAME (cwd inference) ------------------------------------


def test_worktree_single_name_infers_project_from_cwd() -> None:
    cwd = ROOT / "alpha" / "worktrees" / "old"
    assert r(TargetMode.WORKTREE, ["feat"], cwd=cwd) == ROOT / "alpha" / "worktrees" / "feat"


def test_worktree_single_name_outside_root_errors() -> None:
    with pytest.raises(WtmError, match="not under"):
        r(TargetMode.WORKTREE, ["feat"], cwd=Path("/somewhere/else"))


def test_name_only_single_name_infers_from_cwd() -> None:
    cwd = ROOT / "beta"
    assert r(TargetMode.NAME_ONLY, ["feat"], cwd=cwd) == ROOT / "beta" / "worktrees" / "feat"


def test_name_only_single_name_outside_root_errors() -> None:
    with pytest.raises(WtmError, match="no PROJECT given"):
        r(TargetMode.NAME_ONLY, ["feat"], cwd=Path("/elsewhere"))


# --- resolve: no active workspace (< 3 args) ---------------------------------


@pytest.mark.parametrize("args", [["feat"], ["alpha", "feat"]])
def test_no_active_workspace_errors_without_leading_workspace(args: list[str]) -> None:
    with pytest.raises(WtmError, match="no active workspace"):
        resolve(TargetMode.WORKTREE, args, roots=ROOTS, active_root=None, cwd=ROOT)


def test_three_arg_needs_no_active_workspace() -> None:
    # A full triple names its workspace, so it resolves even with no active one.
    got = resolve(
        TargetMode.WORKTREE, ["other", "alpha", "feat"], roots=ROOTS, active_root=None, cwd=ROOT
    )
    assert got == OTHER / "alpha" / "worktrees" / "feat"


# --- resolve: DIR / path handling --------------------------------------------


@pytest.mark.parametrize("arg", ["/abs/dir", "./rel", "../up", ".", ".."])
def test_worktree_accepts_path_arg(arg: str) -> None:
    # A path-looking single arg is taken verbatim (absolutised), never a NAME.
    got = r(TargetMode.WORKTREE, [arg], cwd=ROOT / "alpha")
    assert got.is_absolute()


@pytest.mark.parametrize("arg", ["/abs/dir", "./rel", "../up", ".", ".."])
def test_name_only_rejects_path_arg(arg: str) -> None:
    with pytest.raises(WtmError, match="not a path"):
        r(TargetMode.NAME_ONLY, [arg])


# --- resolve: cwd default / require-target -----------------------------------


def test_worktree_zero_args_defaults_to_cwd() -> None:
    cwd = ROOT / "alpha" / "worktrees" / "feat"
    assert r(TargetMode.WORKTREE, [], cwd=cwd) == cwd


def test_worktree_zero_args_none_cwd_errors() -> None:
    """A removed cwd (cwd=None) turns the cwd default into a clear error."""
    with pytest.raises(WtmError, match="cwd is unavailable"):
        resolve(TargetMode.WORKTREE, [], roots=ROOTS, active_root=ROOT, cwd=None)


def test_worktree_single_name_none_cwd_errors() -> None:
    """A 1-arg NAME cannot infer its project when the cwd is gone."""
    with pytest.raises(WtmError, match="cwd is unavailable"):
        resolve(TargetMode.WORKTREE, ["feat"], roots=ROOTS, active_root=ROOT, cwd=None)


def test_name_only_single_name_none_cwd_errors() -> None:
    """NAME_ONLY project inference from the cwd fails clearly when it is gone."""
    with pytest.raises(WtmError, match="cwd is unavailable"):
        resolve(TargetMode.NAME_ONLY, ["feat"], roots=ROOTS, active_root=ROOT, cwd=None)


def test_delete_zero_args_requires_target() -> None:
    with pytest.raises(WtmError, match="target is required"):
        r(TargetMode.DELETE, [], cwd=ROOT / "alpha")


def test_name_only_zero_args_errors() -> None:
    with pytest.raises(WtmError, match="NAME is required"):
        r(TargetMode.NAME_ONLY, [])


@pytest.mark.parametrize("mode", [TargetMode.WORKTREE, TargetMode.DELETE])
def test_too_many_args(mode: TargetMode) -> None:
    with pytest.raises(WtmError, match="too many arguments"):
        r(mode, ["w", "a", "b", "c"])


def test_name_only_too_many_args() -> None:
    with pytest.raises(WtmError, match="too many arguments"):
        r(TargetMode.NAME_ONLY, ["w", "a", "b", "c"])


# --- split_target ------------------------------------------------------------


def test_split_one_arg_is_name() -> None:
    assert split_target(["feat"]) == (None, None, "feat")


def test_split_two_args_is_project_name() -> None:
    assert split_target(["alpha", "feat"]) == (None, "alpha", "feat")


def test_split_three_args_is_workspace_project_name() -> None:
    assert split_target(["other", "alpha", "feat"]) == ("other", "alpha", "feat")


def test_split_zero_args_errors() -> None:
    with pytest.raises(WtmError, match="NAME is required"):
        split_target([])


def test_split_too_many_args_errors() -> None:
    with pytest.raises(WtmError, match="too many arguments"):
        split_target(["w", "a", "b", "c"])


# --- split_workspace_project -------------------------------------------------


def test_split_ws_project_zero_args() -> None:
    assert split_workspace_project([]) == (None, None)


def test_split_ws_project_one_arg_is_project() -> None:
    assert split_workspace_project(["alpha"]) == (None, "alpha")


def test_split_ws_project_two_args_is_workspace_project() -> None:
    assert split_workspace_project(["other", "alpha"]) == ("other", "alpha")


def test_split_ws_project_too_many_errors() -> None:
    with pytest.raises(WtmError, match="too many arguments"):
        split_workspace_project(["w", "p", "x"])


# --- slot_kind ---------------------------------------------------------------


@pytest.mark.parametrize("mode", [TargetMode.WORKTREE, TargetMode.DELETE])
def test_slot_kind_worktree_first_slot_is_workspace_project_or_name(mode: TargetMode) -> None:
    assert slot_kind(mode, []) == frozenset(
        {TokenKind.WORKSPACE, TokenKind.PROJECT, TokenKind.WORKTREE_NAME}
    )


@pytest.mark.parametrize("mode", [TargetMode.WORKTREE, TargetMode.DELETE])
def test_slot_kind_worktree_second_slot_is_project_or_name(mode: TargetMode) -> None:
    assert slot_kind(mode, ["alpha"]) == frozenset({TokenKind.PROJECT, TokenKind.WORKTREE_NAME})


@pytest.mark.parametrize("mode", [TargetMode.WORKTREE, TargetMode.DELETE])
def test_slot_kind_worktree_third_slot_is_name(mode: TargetMode) -> None:
    assert slot_kind(mode, ["other", "alpha"]) == frozenset({TokenKind.WORKTREE_NAME})


@pytest.mark.parametrize("mode", [TargetMode.WORKTREE, TargetMode.DELETE])
def test_slot_kind_worktree_fourth_slot_empty(mode: TargetMode) -> None:
    assert slot_kind(mode, ["other", "alpha", "feat"]) == frozenset()


def test_slot_kind_name_only_first_slot_workspace_or_project() -> None:
    assert slot_kind(TargetMode.NAME_ONLY, []) == frozenset(
        {TokenKind.WORKSPACE, TokenKind.PROJECT}
    )


def test_slot_kind_name_only_second_slot_project_only() -> None:
    # The WORKSPACE reading makes slot 1 a PROJECT; the invented NAME offers nothing.
    assert slot_kind(TargetMode.NAME_ONLY, ["other"]) == frozenset({TokenKind.PROJECT})


def test_slot_kind_name_only_third_slot_empty() -> None:
    assert slot_kind(TargetMode.NAME_ONLY, ["other", "alpha"]) == frozenset()
