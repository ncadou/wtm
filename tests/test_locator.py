"""Pure, subprocess-free tests for the locator (:mod:`wtm.locator`).

The cascade is the test surface: :func:`locate` takes a :class:`LocationIndex`
snapshot explicitly, so every tier, qualifier reading, and ambiguity is exercised
without spawning ``wtm``, touching git, or hitting the filesystem. The path
reading (:func:`locate_path`) is the same over absolute paths against the
nonexistent fixture roots; only its symlink case needs a real tree. A couple of
:func:`build_index` tests cover the one filesystem-bound edge over a tmp tree.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from wtm.locator import (
    Ambiguous,
    Location,
    LocationIndex,
    Mismatch,
    NotFound,
    Resolved,
    Tier,
    build_index,
    locate,
    locate_path,
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

# INDEX plus a project named after an existing worktree ('docs'), so a leaf-tier
# pin has something to disagree with the cascade about.
PIN_INDEX = replace(INDEX, projects=[*INDEX.projects, _pr("work", "docs")])


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


# --- leaf-tier pin ------------------------------------------------------------


def test_pinned_project_skips_a_same_named_worktree() -> None:
    # 'docs' is a worktree (oss/wtm) and, in PIN_INDEX, also a project: unpinned
    # the worktree tier wins, pinned to PROJECT the project does.
    assert locate(["docs"], PIN_INDEX) == Resolved(_wt("oss", "wtm", "docs"))
    assert locate(["docs"], PIN_INDEX, tier=Tier.PROJECT) == Resolved(_pr("work", "docs"))


def test_pinned_worktree_skips_a_same_named_project() -> None:
    # 'web' has no worktree of that name, so the cascade falls to the project
    # tier; pinned to WORKTREE it is simply not found.
    assert locate(["web"], INDEX) == Resolved(_pr("work", "web"))
    assert locate(["web"], INDEX, tier=Tier.WORKTREE) == NotFound()


def test_pinned_project_skips_a_same_named_workspace() -> None:
    # 'oss' is a workspace only: the cascade reaches it, a PROJECT pin does not.
    assert locate(["oss"], INDEX) == Resolved(Location(tier=Tier.WORKSPACE, workspace="oss"))
    assert locate(["oss"], INDEX, tier=Tier.PROJECT) == NotFound()


def test_pinned_tier_still_reports_its_own_ambiguity() -> None:
    # A pin narrows which tier answers, never how a tie within it is reported.
    result = locate(["api"], INDEX, tier=Tier.PROJECT)
    assert isinstance(result, Ambiguous)
    assert {c.workspace for c in result.candidates} == {"work", "oss"}


def test_pinned_tier_honours_qualifiers() -> None:
    assert locate(["work", "api"], INDEX, tier=Tier.PROJECT) == Resolved(_pr("work", "api"))
    assert locate(["oss", "api"], INDEX, tier=Tier.PROJECT) == Resolved(_pr("oss", "api"))
    assert locate(["work", "api", "hotfix"], INDEX, tier=Tier.WORKTREE) == Resolved(
        _wt("work", "api", "hotfix")
    )


def test_pinned_workspace_rejects_qualifiers() -> None:
    assert locate(["oss"], INDEX, tier=Tier.WORKSPACE) == Resolved(
        Location(tier=Tier.WORKSPACE, workspace="oss")
    )
    assert locate(["work", "oss"], INDEX, tier=Tier.WORKSPACE) == NotFound()


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
    """A minimal stand-in exposing just the ``workspaces`` mapping build_index reads.

    When *declared_projects* / *declared_worktrees* are given, the fake also
    answers ``declared_project_names`` / ``declared_worktree_names``, keyed by
    workspace path, so the ``include_config_only`` flag path is unit-testable.
    """

    def __init__(
        self,
        workspaces: dict[str, Path],
        *,
        declared_projects: dict[str, set[str]] | None = None,
        declared_worktrees: dict[tuple[str, str], set[str]] | None = None,
    ) -> None:
        self.workspaces = {name: _FakeWorkspace(path) for name, path in workspaces.items()}
        self._declared_projects = declared_projects or {}
        self._declared_worktrees = declared_worktrees or {}

    def declared_project_names(self, workspace: _FakeWorkspace) -> set[str]:
        return self._declared_projects.get(str(workspace.path), set())

    def declared_worktree_names(self, workspace: _FakeWorkspace, project: str) -> set[str]:
        return self._declared_worktrees.get((str(workspace.path), project), set())


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


def test_build_index_include_config_only_adds_inert_project(tmp_path: Path) -> None:
    work = tmp_path / "work"
    _make_project(work, "api", ["main"])
    # 'ghost' is declared in config but has no .git on disk.
    config = _FakeConfig(
        {"work": work},
        declared_projects={str(work): {"ghost"}},
    )  # type: ignore[arg-type]

    index = build_index(config, include_config_only=True)

    assert {(p.workspace, p.project) for p in index.projects} == {
        ("work", "api"),
        ("work", "ghost"),
    }


def test_build_index_include_config_only_adds_absent_worktree(tmp_path: Path) -> None:
    work = tmp_path / "work"
    _make_project(work, "api", ["main"])
    # 'feat' is declared as a worktree of 'api' but does not exist on disk.
    config = _FakeConfig(
        {"work": work},
        declared_worktrees={(str(work), "api"): {"feat"}},
    )  # type: ignore[arg-type]

    index = build_index(config, include_config_only=True)

    assert {(w.workspace, w.project, w.name) for w in index.worktrees} == {
        ("work", "api", "main"),
        ("work", "api", "feat"),
    }


def test_build_index_include_config_only_dedups(tmp_path: Path) -> None:
    work = tmp_path / "work"
    _make_project(work, "api", ["main"])
    # 'api' and 'main' are already on disk -- the flag must not duplicate them.
    config = _FakeConfig(
        {"work": work},
        declared_projects={str(work): {"api"}},
        declared_worktrees={(str(work), "api"): {"main"}},
    )  # type: ignore[arg-type]

    index = build_index(config, include_config_only=True)

    assert [p.project for p in index.projects] == ["api"]
    assert [w.name for w in index.worktrees] == ["main"]


def test_build_index_default_excludes_config_only(tmp_path: Path) -> None:
    work = tmp_path / "work"
    _make_project(work, "api", ["main"])
    config = _FakeConfig(
        {"work": work},
        declared_projects={str(work): {"ghost"}},
        declared_worktrees={(str(work), "api"): {"feat"}},
    )  # type: ignore[arg-type]

    index = build_index(config)  # default: include_config_only=False

    assert {(p.workspace, p.project) for p in index.projects} == {("work", "api")}
    assert {(w.workspace, w.project, w.name) for w in index.worktrees} == {("work", "api", "main")}


# --- locate_path: geometry over a real tree -----------------------------------

# The existence gate means the index's roots must be real dirs: the fixture
# mirrors INDEX's shape on disk (same names, tmp-based roots).


@pytest.fixture
def tree(tmp_path: Path) -> tuple[LocationIndex, Path, Path]:
    """An on-disk mirror of INDEX plus its roots -- and the roots themselves."""
    work = tmp_path / "w"
    _make_project(work, "api", ["main", "feature/auth", "hotfix"])
    _make_project(work, "web", ["main", "hotfix"])
    (work / "web" / "docs").mkdir()
    (work / "scratch").mkdir()
    (work / "scratch" / "deep").mkdir()
    oss = tmp_path / "o"
    _make_project(oss, "wtm", ["main", "docs"])
    _make_project(oss, "api", ["main"])
    (oss / "not-a-project" / "inner").mkdir(parents=True)

    return build_index(_FakeConfig({"work": work, "oss": oss})), work, oss  # type: ignore[arg-type]


def test_worktree_dir_resolves_to_the_worktree(tree: tuple[LocationIndex, Path, Path]) -> None:
    index, work, _ = tree
    assert locate_path(work / "api" / "worktrees" / "hotfix", index) == Resolved(
        _wt("work", "api", "hotfix")
    )


def test_arbitrary_depth_inside_a_worktree_truncates_up(
    tree: tuple[LocationIndex, Path, Path],
) -> None:
    index, work, _ = tree
    leaf = work / "api" / "worktrees" / "main" / "src" / "pkg"
    leaf.mkdir(parents=True)
    (leaf / "mod.py").write_text("")
    assert locate_path(leaf / "mod.py", index) == Resolved(_wt("work", "api", "main"))


def test_nested_name_dir_is_one_entity(tree: tuple[LocationIndex, Path, Path]) -> None:
    # 'feature/auth' is a single worktree whose name contains '/'.
    index, work, _ = tree
    assert locate_path(work / "api" / "worktrees" / "feature" / "auth", index) == Resolved(
        _wt("work", "api", "feature/auth")
    )


def test_project_dir_resolves_to_the_project(tree: tuple[LocationIndex, Path, Path]) -> None:
    index, work, _ = tree
    assert locate_path(work / "web", index) == Resolved(_pr("work", "web"))


def test_depth_inside_a_project_outside_its_worktrees_is_the_project(
    tree: tuple[LocationIndex, Path, Path],
) -> None:
    index, work, _ = tree
    (work / "web" / "docs" / "readme.md").write_text("")
    assert locate_path(work / "web" / "docs" / "readme.md", index) == Resolved(_pr("work", "web"))


def test_projects_root_stray_resolves_to_the_workspace(
    tree: tuple[LocationIndex, Path, Path],
) -> None:
    # A dir inside the workspace but outside every project truncates to it.
    index, work, _ = tree
    assert locate_path(work / "scratch" / "deep", index) == Resolved(
        Location(tier=Tier.WORKSPACE, workspace="work")
    )


def test_workspace_root_itself_resolves(tree: tuple[LocationIndex, Path, Path]) -> None:
    index, _, oss = tree
    assert locate_path(oss, index) == Resolved(Location(tier=Tier.WORKSPACE, workspace="oss"))


def test_path_equal_to_a_projects_root_with_siblings_present(
    tree: tuple[LocationIndex, Path, Path],
) -> None:
    # The root holds api and web; the exact root is workspace 'work', not either.
    index, work, _ = tree
    assert locate_path(work, index) == Resolved(Location(tier=Tier.WORKSPACE, workspace="work"))


def test_absent_leaf_is_not_found_even_in_tree(tree: tuple[LocationIndex, Path, Path]) -> None:
    # A typo must not truncate onto an enclosing entity.
    index, work, _ = tree
    assert locate_path(work / "web" / "docs" / "ghost.md", index) == NotFound()
    assert locate_path(work / "ghost" / "deeper", index) == NotFound()


def test_out_of_tree_is_not_found() -> None:
    # Pure reading: no fs touched for paths that cannot exist anyway.
    assert locate_path("/elsewhere/x/y", INDEX) == NotFound()


def test_sibling_of_a_root_is_not_found(tree: tuple[LocationIndex, Path, Path]) -> None:
    # '<root>2' prefix-matches textually but not structurally.
    index, work, _ = tree
    assert locate_path(Path(f"{work}2/api"), index) == NotFound()


def test_pinned_project_truncates_a_worktree_path_up(
    tree: tuple[LocationIndex, Path, Path],
) -> None:
    index, _, oss = tree
    assert locate_path(oss / "wtm" / "worktrees" / "docs", index, tier=Tier.PROJECT) == Resolved(
        _pr("oss", "wtm")
    )


def test_pinned_worktree_rejects_a_project_path(
    tree: tuple[LocationIndex, Path, Path],
) -> None:
    index, work, _ = tree
    result = locate_path(work / "web", index, tier=Tier.WORKTREE)
    assert result == Mismatch(natural=Tier.PROJECT, wanted=Tier.WORKTREE)


def test_pinned_worktree_rejects_a_workspace_stray(
    tree: tuple[LocationIndex, Path, Path],
) -> None:
    index, work, _ = tree
    result = locate_path(work / "scratch" / "deep", index, tier=Tier.WORKTREE)
    assert result == Mismatch(natural=Tier.WORKSPACE, wanted=Tier.WORKTREE)


def test_pinned_project_rejects_a_workspace_stray(
    tree: tuple[LocationIndex, Path, Path],
) -> None:
    index, work, _ = tree
    result = locate_path(work / "scratch", index, tier=Tier.PROJECT)
    assert result == Mismatch(natural=Tier.WORKSPACE, wanted=Tier.PROJECT)


def test_pinned_project_rejects_an_inert_top_level_dir(
    tree: tuple[LocationIndex, Path, Path],
) -> None:
    index, _, oss = tree
    result = locate_path(oss / "not-a-project" / "inner", index, tier=Tier.PROJECT)
    assert result == Mismatch(natural=Tier.WORKSPACE, wanted=Tier.PROJECT)


def test_dotdot_in_typed_path_normalized_before_matching(
    tree: tuple[LocationIndex, Path, Path],
) -> None:
    index, work, _ = tree
    typed = f"{work}/api/../web/worktrees/main"
    assert locate_path(typed, index) == Resolved(_wt("work", "web", "main"))


# --- interpret_path -----------------------------------------------------------


def test_interpret_path_keeps_absolute(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from wtm.locator import interpret_path

    monkeypatch.chdir(tmp_path)
    assert interpret_path("/x/y") == Path("/x/y")


def test_interpret_path_expands_home_and_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    from wtm.locator import interpret_path

    monkeypatch.setenv("HOME", "/home/t")
    monkeypatch.setenv("BASE", "/b")
    assert interpret_path("~/r") == Path("/home/t/r")
    assert interpret_path("${BASE}/q") == Path("/b/q")


def test_interpret_path_anchors_relative_on_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from wtm.locator import interpret_path

    monkeypatch.chdir(tmp_path)
    assert interpret_path("./a/b") == tmp_path / "a" / "b"


# --- symlink handling (needs a real tree) -------------------------------------


def test_symlinked_root_matches_via_realpath(tmp_path: Path) -> None:
    # A shell standing under a resolved symlink reports realpath'd cwds; typing
    # one must still match an index whose roots are stored unresolved.
    work = tmp_path / "raw-work"
    _make_project(work, "api", ["main"])
    link = tmp_path / "linked"
    link.symlink_to(work)
    index = build_index(_FakeConfig({"work": work}))  # type: ignore[arg-type]

    through_link = link / "api" / "worktrees" / "main"
    try:
        through_link.relative_to(work)
        pytest.skip("filesystem does not exercise the symlink split")
    except ValueError:
        pass

    assert locate_path(through_link, index) == Resolved(_wt("work", "api", "main"))
