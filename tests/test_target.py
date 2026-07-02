"""Pure, subprocess-free tests for the target grammar (:mod:`wtm.target`).

The grammar is the test surface: :func:`resolve` and :func:`slot_kind` take the
projects root and cwd explicitly, so every mode and edge case is exercised
without spawning ``wtm`` or touching git.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wtm.core import WtmError
from wtm.target import TargetMode, TokenKind, resolve, slot_kind, split_project_name

ROOT = Path("/projects")


def r(mode: TargetMode, args: list[str], *, cwd: Path = ROOT) -> Path:
    return resolve(mode, args, projects_root=ROOT, cwd=cwd)


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


def test_delete_zero_args_requires_target() -> None:
    with pytest.raises(WtmError, match="target is required"):
        r(TargetMode.DELETE, [], cwd=ROOT / "alpha")


def test_name_only_zero_args_errors() -> None:
    with pytest.raises(WtmError, match="NAME is required"):
        r(TargetMode.NAME_ONLY, [])


@pytest.mark.parametrize("mode", [TargetMode.WORKTREE, TargetMode.DELETE])
def test_too_many_args(mode: TargetMode) -> None:
    with pytest.raises(WtmError, match="too many arguments"):
        r(mode, ["a", "b", "c"])


def test_name_only_too_many_args() -> None:
    with pytest.raises(WtmError, match="too many arguments"):
        r(TargetMode.NAME_ONLY, ["a", "b", "c"])


# --- split_project_name ------------------------------------------------------


def test_split_one_arg_is_name() -> None:
    assert split_project_name(["feat"]) == (None, "feat")


def test_split_two_args_is_project_name() -> None:
    assert split_project_name(["alpha", "feat"]) == ("alpha", "feat")


def test_split_zero_args_errors() -> None:
    with pytest.raises(WtmError, match="NAME is required"):
        split_project_name([])


# --- slot_kind ---------------------------------------------------------------


@pytest.mark.parametrize("mode", [TargetMode.WORKTREE, TargetMode.DELETE])
def test_slot_kind_worktree_first_slot_is_project_or_name(mode: TargetMode) -> None:
    assert slot_kind(mode, []) == frozenset({TokenKind.PROJECT, TokenKind.WORKTREE_NAME})


@pytest.mark.parametrize("mode", [TargetMode.WORKTREE, TargetMode.DELETE])
def test_slot_kind_worktree_second_slot_is_name(mode: TargetMode) -> None:
    assert slot_kind(mode, ["alpha"]) == frozenset({TokenKind.WORKTREE_NAME})


@pytest.mark.parametrize("mode", [TargetMode.WORKTREE, TargetMode.DELETE])
def test_slot_kind_worktree_third_slot_empty(mode: TargetMode) -> None:
    assert slot_kind(mode, ["alpha", "feat"]) == frozenset()


def test_slot_kind_name_only_first_slot_project_only() -> None:
    assert slot_kind(TargetMode.NAME_ONLY, []) == frozenset({TokenKind.PROJECT})


def test_slot_kind_name_only_second_slot_empty() -> None:
    # A brand-new NAME being invented has nothing to complete.
    assert slot_kind(TargetMode.NAME_ONLY, ["alpha"]) == frozenset()
