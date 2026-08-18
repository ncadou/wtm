"""Pure, subprocess-free tests for the locator (:mod:`wtm.locate`).

The cascade is the test surface: :func:`locate` takes a :class:`LocationIndex`
snapshot explicitly, so every tier, qualifier reading, and ambiguity is exercised
without spawning ``wtm``, touching git, or hitting the filesystem. A couple of
:func:`build_index` tests cover the one filesystem-bound edge over a tmp tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wtm.locate import (
    Ambiguous,
    Location,
    LocationIndex,
    NotFound,
    Resolved,
    Tier,
    build_index,
    locate,
    narrow_by_context,
)


def _wt(workspace: str, project: str, name: str) -> Location:
    return Location(tier=Tier.WORKTREE, workspace=workspace, project=project, name=name)


def _pr(workspace: str, project: str) -> Location:
    return Location(tier=Tier.PROJECT, workspace=workspace, project=project)


# An index mirroring the design example: worktree 'main' spans 4 projects,
# 'hotfix' spans work/api + work/web, project 'api' spans work + oss.
INDEX = LocationIndex(
    worktrees=[
        _wt("work", "api", "main"),
        _wt("work", "api", "feature/auth"),
        _wt("work", "api", "hotfix"),
        _wt("work", "web", "main"),
        _wt("work", "web", "hotfix"),
        _wt("oss", "wtm", "main"),
        _wt("oss", "wtm", "docs"),
        _wt("oss", "api", "main"),
    ],
    projects=[_pr("work", "api"), _pr("work", "web"), _pr("oss", "wtm"), _pr("oss", "api")],
    workspaces=["oss", "work"],
    roots={"work": Path("/w"), "oss": Path("/o")},
)


# --- single token: leaf-tier cascade -----------------------------------------


def test_unique_worktree_resolves() -> None:
    assert locate(["docs"], INDEX) == Resolved(_wt("oss", "wtm", "docs"))


def test_nested_worktree_name_matched_whole() -> None:
    assert locate(["feature/auth"], INDEX) == Resolved(_wt("work", "api", "feature/auth"))


def test_worktree_tier_wins_over_lower_tiers() -> None:
    # 'api' is a project (x2) but not a worktree; a name that is a worktree
    # would still beat a same-named project. Here worktrees named 'docs' win
    # outright; assert the project tier is not even consulted for 'hotfix'.
    result = locate(["hotfix"], INDEX)
    assert isinstance(result, Ambiguous)
    assert all(candidate.tier is Tier.WORKTREE for candidate in result.candidates)


def test_ambiguous_worktree_lists_every_match() -> None:
    result = locate(["hotfix"], INDEX)
    assert isinstance(result, Ambiguous)
    assert {(c.workspace, c.project) for c in result.candidates} == {
        ("work", "api"),
        ("work", "web"),
    }


def test_falls_through_to_project_when_no_worktree() -> None:
    assert locate(["web"], INDEX) == Resolved(_pr("work", "web"))


def test_ambiguous_project_across_workspaces() -> None:
    result = locate(["api"], INDEX)
    assert isinstance(result, Ambiguous)
    assert result.candidates[0].tier is Tier.PROJECT
    assert {c.workspace for c in result.candidates} == {"work", "oss"}


def test_falls_through_to_workspace_when_no_worktree_or_project() -> None:
    assert locate(["oss"], INDEX) == Resolved(Location(tier=Tier.WORKSPACE, workspace="oss"))


def test_no_match_is_not_found() -> None:
    assert locate(["nope"], INDEX) == NotFound()


# --- two tokens: qualifier readings -------------------------------------------


def test_project_qualified_worktree() -> None:
    # 'api' read as a project narrows 'hotfix' to work/api (oss/api has none).
    assert locate(["api", "hotfix"], INDEX) == Resolved(_wt("work", "api", "hotfix"))


def test_workspace_qualified_worktree_still_ambiguous() -> None:
    # 'work' read as a workspace: hotfix exists in two of its projects.
    result = locate(["work", "hotfix"], INDEX)
    assert isinstance(result, Ambiguous)
    assert {c.project for c in result.candidates} == {"api", "web"}


def test_two_token_falls_through_to_workspace_qualified_project() -> None:
    # No worktree named 'api' -> project leaf: workspace 'work' qualifies it.
    assert locate(["work", "api"], INDEX) == Resolved(_pr("work", "api"))


def test_two_token_project_leaf_wrong_workspace_not_found() -> None:
    assert locate(["oss", "web"], INDEX) == NotFound()


# --- three tokens: fully qualified --------------------------------------------


def test_three_token_fully_qualified() -> None:
    assert locate(["work", "api", "hotfix"], INDEX) == Resolved(_wt("work", "api", "hotfix"))


def test_three_token_wrong_ancestor_not_found() -> None:
    assert locate(["oss", "api", "hotfix"], INDEX) == NotFound()


def test_empty_tokens_rejected() -> None:
    with pytest.raises(ValueError, match="at least one token"):
        locate([], INDEX)


# --- narrow_by_context (caller-side ambiguity tiebreak) -----------------------


# The tied same-tier candidates 'main' resolves to across the design index.
MAIN_HITS = [_wt("work", "api", "main"), _wt("work", "web", "main"), _wt("oss", "wtm", "main")]


def test_project_context_narrows_worktree_tie() -> None:
    # Standing in work/api: the workspace+project pair names exactly one 'main'.
    assert narrow_by_context(MAIN_HITS, workspace="work", project="api") == _wt(
        "work", "api", "main"
    )


def test_relaxes_to_workspace_when_project_misses() -> None:
    # Standing in oss/wtm still resolves via the workspace alone (oss has one 'main').
    assert narrow_by_context(MAIN_HITS, workspace="oss", project="wtm") == _wt(
        "oss", "wtm", "main"
    )


def test_workspace_only_context_narrows() -> None:
    # No project (e.g. at the workspace root): oss holds a single 'main'.
    assert narrow_by_context(MAIN_HITS, workspace="oss", project=None) == _wt("oss", "wtm", "main")


def test_none_when_workspace_still_ambiguous() -> None:
    # work holds two 'main's and no project pins one -> the tie stands.
    assert narrow_by_context(MAIN_HITS, workspace="work", project=None) is None


def test_none_when_context_matches_no_candidate() -> None:
    # A workspace outside the tied set names nothing.
    assert narrow_by_context(MAIN_HITS, workspace="other", project=None) is None


def test_none_when_no_context() -> None:
    assert narrow_by_context(MAIN_HITS, workspace=None, project=None) is None


def test_project_tier_tie_narrows_by_workspace() -> None:
    # A project leaf carries no project ancestor; the same filter still fits,
    # its project field being the leaf name (equal to the current project when in it).
    api_hits = [_pr("work", "api"), _pr("oss", "api")]
    assert narrow_by_context(api_hits, workspace="work", project="api") == _pr("work", "api")
    assert narrow_by_context(api_hits, workspace="work", project="web") == _pr("work", "api")


# --- build_index (filesystem edge) --------------------------------------------


class _FakeWorkspace:
    def __init__(self, path: Path) -> None:
        self.path = path


class _FakeConfig:
    """A minimal stand-in exposing just the ``workspaces`` mapping build_index reads."""

    def __init__(self, workspaces: dict[str, Path]) -> None:
        self.workspaces = {name: _FakeWorkspace(path) for name, path in workspaces.items()}


def _make_project(root: Path, name: str, worktrees: list[str]) -> None:
    """Scaffold a project dir (.git/) with the given worktrees (each a .git-bearing dir)."""
    project = root / name
    (project / ".git").mkdir(parents=True)
    for wt in worktrees:
        wt_dir = project / "worktrees" / wt
        wt_dir.mkdir(parents=True)
        (wt_dir / ".git").write_text("gitdir: ...\n")  # a linked worktree's .git is a file


def test_build_index_enumerates_the_tree(tmp_path: Path) -> None:
    work = tmp_path / "work"
    oss = tmp_path / "oss"
    _make_project(work, "api", ["main", "feature/auth"])
    _make_project(oss, "wtm", ["main"])
    (oss / "not-a-project").mkdir()  # no .git -> ignored

    index = build_index(_FakeConfig({"work": work, "oss": oss}))  # type: ignore[arg-type]

    assert set(index.workspaces) == {"work", "oss"}
    assert {(p.workspace, p.project) for p in index.projects} == {("work", "api"), ("oss", "wtm")}
    assert {(w.workspace, w.project, w.name) for w in index.worktrees} == {
        ("work", "api", "main"),
        ("work", "api", "feature/auth"),
        ("oss", "wtm", "main"),
    }


def test_build_index_lenient_on_unreadable_root(tmp_path: Path) -> None:
    # A declared workspace whose path does not exist yields no projects, not an error.
    index = build_index(_FakeConfig({"ghost": tmp_path / "missing"}))  # type: ignore[arg-type]
    assert index.workspaces == ["ghost"]
    assert index.projects == []
    assert index.worktrees == []
