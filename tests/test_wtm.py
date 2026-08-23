"""End-to-end integration tests for wtm.

Each test builds a throwaway projects root and exercises the real ``wtm`` entry
point, so the typer wiring, exit codes, stdout/stderr discipline, git side
effects, and hook firing are all covered.

Commands run in-process by default (:func:`run`): the harness sets the isolated
environment, redirects the streams, and calls :func:`wtm.cli.main` directly, so
``main``'s top-level ``WtmError``/``GitError`` handler and click's usage errors
are exercised faithfully without paying interpreter + typer import startup per
call. The handful of tests that strand the shell -- the running process stands
inside a worktree the command deletes or moves -- use :func:`run_proc` instead,
which spawns a real child so destroying its cwd cannot disturb the test runner.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from wtm import cli
from wtm.output import MANAGED_ENV_VARS

WTM = [sys.executable, "-m", "wtm.cli"]


def _unsquote(token: str) -> str:
    """Reverse :func:`wtm.output._squote` (a POSIX single-quoted string)."""
    return token[1:-1].replace("'\\''", "'")


def _shell_cd(shell_file: Path) -> str | None:
    """Return the ``cd -- '...'`` target emitted to *shell_file*, or ``None``."""
    for line in shell_file.read_text().splitlines():
        if line.startswith("cd -- "):
            return _unsquote(line[len("cd -- ") :])

    return None


def _shell_exports(shell_file: Path) -> dict[str, str]:
    """Return the ``export NAME='...'`` pairs emitted to *shell_file*."""
    exports: dict[str, str] = {}
    for line in shell_file.read_text().splitlines():
        if line.startswith("export "):
            name, _, value = line[len("export ") :].partition("=")
            exports[name] = _unsquote(value)

    return exports


def _shell_unsets(shell_file: Path) -> set[str]:
    """Return the names on the ``unset ...`` line emitted to *shell_file*."""
    for line in shell_file.read_text().splitlines():
        if line.startswith("unset "):
            return set(line[len("unset ") :].split())

    return set()


@dataclass(frozen=True)
class _Result:
    """A subprocess-compatible view of an in-process wtm invocation."""

    returncode: int
    stdout: str
    stderr: str


def _write_config(root: Path, workspace_hooks_dir: Path | None = None) -> Path:
    """Write a minimal user config declaring a sole workspace at *root*.

    With no built-in default workspace, tests declare one explicitly (name
    ``ws``, path = the throwaway root). Being the sole workspace it is selected
    automatically, so commands run from anywhere resolve it without a flag.
    *workspace_hooks_dir* sets the workspace-scope builtin hooks dir (the
    config-native replacement for the old ``WTM_WORKSPACE_HOOKS_DIR``).
    """
    config_path = root / "_wtm_config.toml"
    body = f'[workspaces.ws]\npath = "{root}"\n'
    if workspace_hooks_dir is not None:
        body += f'[workspaces.ws.hooks]\ndir = "{workspace_hooks_dir}"\n'
    config_path.write_text(body)

    return config_path


def _config_env(root: Path, workspace_hooks_dir: Path | None = None) -> dict[str, str]:
    """Env deltas pointing wtm at the sole-workspace config for *root*."""
    return {
        "WTM_CONFIG": str(_write_config(root, workspace_hooks_dir)),
        "WTM_SYSTEM_CONFIG": str(root / "_no_system_config.toml"),
        "WTM_WORKSPACE": "ws",
    }


def _use_config(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Point an in-test ``Config.load()`` at the sole-workspace config for *root*."""
    root.mkdir(parents=True, exist_ok=True)
    for key, value in _config_env(root).items():
        monkeypatch.setenv(key, value)


def _isolated_env(
    root: Path,
    hooks_dir: Path | None,
    system_hooks_dir: Path | None,
    shell_file: Path | None,
    extra_env: dict[str, str] | None,
    workspace_hooks_dir: Path | None = None,
) -> dict[str, str]:
    """Build the WTM_*/GIT_* environment deltas for one invocation."""
    root.mkdir(parents=True, exist_ok=True)
    env = {
        "WTM_CONFIG": str(_write_config(root, workspace_hooks_dir)),
        "WTM_SYSTEM_CONFIG": str(root / "_no_system_config.toml"),
        "WTM_USER_HOOKS_DIR": str(hooks_dir if hooks_dir is not None else root / "_no_user_hooks"),
        "WTM_SYSTEM_HOOKS_DIR": str(
            system_hooks_dir if system_hooks_dir is not None else root / "_no_system_hooks"
        ),
        # Deterministic git identity / no user config leakage.
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": str(root / "_gitconfig"),
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    # A stale WTM_WORKSPACE from the host would shadow the sole-workspace rule.
    env["WTM_WORKSPACE"] = "ws"
    if shell_file is not None:
        env["WTM_SHELL_FILE"] = str(shell_file)
    if extra_env:
        env.update(extra_env)

    return env


def run(
    *args: str,
    root: Path,
    cwd: Path | None = None,
    hooks_dir: Path | None = None,
    system_hooks_dir: Path | None = None,
    shell_file: Path | None = None,
    extra_env: dict[str, str] | None = None,
    workspace_hooks_dir: Path | None = None,
) -> _Result:
    """Invoke wtm in-process via :func:`wtm.cli.main`, isolated from the host.

    Sets the WTM_*/GIT_* environment, chdirs into *cwd*, redirects the streams,
    and runs the real entry point -- so exit codes, the ``wtm: ...`` error
    handler, and usage errors match a real invocation -- then restores the
    process state. Use :func:`run_proc` when the command destroys *cwd*.
    """
    env_delta = _isolated_env(
        root, hooks_dir, system_hooks_dir, shell_file, extra_env, workspace_hooks_dir
    )

    prev_environ = dict(os.environ)
    prev_argv = sys.argv
    prev_cwd = os.getcwd()
    out, err = io.StringIO(), io.StringIO()
    try:
        os.environ.update(env_delta)
        os.chdir(cwd or root)
        sys.argv = ["wtm", *args]
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    finally:
        os.chdir(prev_cwd)
        sys.argv = prev_argv
        os.environ.clear()
        os.environ.update(prev_environ)

    return _Result(returncode=code, stdout=out.getvalue(), stderr=err.getvalue())


def run_proc(
    *args: str,
    root: Path,
    cwd: Path | None = None,
    hooks_dir: Path | None = None,
    system_hooks_dir: Path | None = None,
    shell_file: Path | None = None,
    extra_env: dict[str, str] | None = None,
    workspace_hooks_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke wtm as a real subprocess with an isolated environment.

    Reserved for tests that strand the shell (the process stands inside a
    worktree the command deletes or moves): a child process keeps the doomed
    cwd off the test runner.
    """
    env = dict(os.environ)
    env.update(
        _isolated_env(
            root, hooks_dir, system_hooks_dir, shell_file, extra_env, workspace_hooks_dir
        )
    )

    return subprocess.run(
        WTM + list(args),
        cwd=str(cwd or root),
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """An empty projects root."""
    r = tmp_path / "projects"
    r.mkdir()
    (r / "_gitconfig").write_text("[init]\n\tdefaultBranch = main\n")

    return r


def _seed_repo(root: Path, project: str) -> Path:
    """Create a bare project with one commit on 'main' and return its .git dir.

    A non-bare source repo is built and bare-cloned into the project, so the
    resulting bare repo at ``<project>/.git`` has a commit to branch from.
    """
    git_env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(root / "_gitconfig"),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    source = root / "_sources" / project
    source.mkdir(parents=True)
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True, env=git_env)
    (source / "file.txt").write_text("hello\n")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True, env=git_env)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "init"], check=True, env=git_env)

    assert run("clone", str(source), project, root=root).returncode == 0
    git_dir = root / project / ".git"
    assert git_dir.is_dir()

    return git_dir


def _seed_repo_at(ws_root: Path, project: str) -> Path:
    """Create a bare project under an arbitrary workspace root (git-only, no wtm config).

    The multi-workspace analog of :func:`_seed_repo`: it scaffolds
    ``<ws_root>/<project>/{.git,worktrees}`` directly with git, so a
    :func:`_run_multi` invocation can target it. Returns the project's .git dir.
    """
    git_env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(ws_root / "_gitconfig"),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    source = ws_root / "_sources" / project
    source.mkdir(parents=True)
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True, env=git_env)
    (source / "file.txt").write_text("hello\n")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True, env=git_env)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "init"], check=True, env=git_env)

    project_dir = ws_root / project
    (project_dir / "worktrees").mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(source), str(project_dir / ".git")],
        check=True,
        env=git_env,
    )

    return project_dir / ".git"


# --- project new / clone / list ---------------------------------------------


def test_project_new_creates_layout(root: Path) -> None:
    res = run("project", "new", "proj", root=root)
    assert res.returncode == 0, res.stderr
    assert (root / "proj" / ".git").is_dir()
    assert (root / "proj" / "worktrees").is_dir()
    # Diagnostics go to stderr, not stdout.
    assert res.stdout == ""
    assert "created project" in res.stderr


def test_project_new_rejects_duplicate(root: Path) -> None:
    run("project", "new", "proj", root=root)
    res = run("project", "new", "proj", root=root)
    assert res.returncode == 1
    assert "already exists" in res.stderr


def test_project_new_rejects_slash(root: Path) -> None:
    res = run("project", "new", "a/b", root=root)
    assert res.returncode == 1
    assert "may not contain '/'" in res.stderr


def test_top_level_new_removed(root: Path) -> None:
    # 'wtm new' no longer exists; it is now 'wtm project new'.
    res = run("new", "proj", root=root)
    assert res.returncode == 2


def test_project_clone_from_local_origin(root: Path) -> None:
    origin = _seed_repo(root, "origin")
    res = run("project", "clone", str(origin), "cloned", root=root)
    assert res.returncode == 0, res.stderr
    assert (root / "cloned" / ".git").is_dir()


def test_clone_sets_origin_fetch_refspec(root: Path) -> None:
    # wtm clone must produce the dual-ref layout: upstream heads fetch into
    # refs/remotes/origin/* (not refs/heads/*), which create's resolution needs.
    origin = _seed_repo(root, "origin")
    run("project", "clone", str(origin), "cloned", root=root)
    refspec = subprocess.run(
        ["git", "--git-dir", str(root / "cloned" / ".git"), "config", "remote.origin.fetch"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert refspec == "+refs/heads/*:refs/remotes/origin/*"


def test_clone_shorthand_from_local_origin(root: Path) -> None:
    # 'wtm clone' is the documented shorthand for 'wtm project clone'.
    origin = _seed_repo(root, "origin")
    res = run("clone", str(origin), "cloned", root=root)
    assert res.returncode == 0, res.stderr
    assert (root / "cloned" / ".git").is_dir()


def test_clone_no_bare_flag_removed(root: Path) -> None:
    # Non-bare clones are no longer supported; the flag must be unknown.
    origin = _seed_repo(root, "origin")
    res = run("clone", str(origin), "cloned", "--no-bare", root=root)
    assert res.returncode == 2


def test_project_list_only_repo_dirs(root: Path) -> None:
    _seed_repo(root, "alpha")
    (root / "stray").mkdir()  # no .git/, must be ignored
    res = run("project", "list", root=root)
    assert res.returncode == 0, res.stderr
    assert res.stdout.split() == ["alpha"]


# --- project list --tree -----------------------------------------------------


def test_project_list_tree_shows_worktrees(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    run("create", "alpha", "feature/x", root=root)  # nested NAME -> flat leaf
    res = run("project", "list", "--tree", root=root)
    assert res.returncode == 0, res.stderr
    # Sorted children: 'feat' is non-last (tee), 'feature/x' is last (elbow).
    assert res.stdout == "alpha\n├── feat\n└── feature/x\n"


def test_project_list_tree_short_flag(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    res = run("project", "list", "-t", root=root)
    assert res.returncode == 0, res.stderr
    assert res.stdout == "alpha\n└── feat\n"


def test_project_list_tree_empty_project(root: Path) -> None:
    # A freshly-created project has no worktrees: just the bare name, no leaves.
    run("project", "new", "alpha", root=root)
    res = run("project", "list", "--tree", root=root)
    assert res.returncode == 0, res.stderr
    assert res.stdout == "alpha\n"


def test_project_list_tree_error_marker(root: Path) -> None:
    # A dir with an empty .git/ passes the project filter but fails the git
    # worktree query, so the tree marks it inline instead of looking empty.
    _seed_repo(root, "alpha")
    (root / "broken" / ".git").mkdir(parents=True)
    res = run("project", "list", "--tree", root=root)
    assert res.returncode == 0, res.stderr
    assert "broken\n└── <error reading worktrees>\n" in res.stdout


def test_project_list_default_unchanged(root: Path) -> None:
    # Without --tree, output stays the flat one-name-per-line listing.
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    res = run("project", "list", root=root)
    assert res.returncode == 0, res.stderr
    assert res.stdout == "alpha\n"


# --- project list --worktrees -----------------------------------------------


def test_project_list_worktrees_flat(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    run("create", "alpha", "feature/x", root=root)  # nested NAME -> flat leaf
    _seed_repo(root, "beta")
    run("create", "beta", "main", root=root)
    res = run("project", "list", "--worktrees", root=root)
    assert res.returncode == 0, res.stderr

    rows = [line.split("\t") for line in res.stdout.splitlines()]
    # Sorted by project, then by name within a project.
    assert [(r[0], r[1]) for r in rows] == [
        ("alpha", "feat"),
        ("alpha", "feature/x"),
        ("beta", "main"),
    ]
    # Five fields; path is absolute under the project's worktrees/; head short.
    for r in rows:
        assert len(r) == 5
        project, name, path, branch, head = r
        assert path == str(root / project / "worktrees" / name)
        assert branch == name  # -b defaults to NAME
        assert head and len(head) == 8


def test_project_list_worktrees_short_flag(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    res = run("project", "list", "-w", root=root)
    assert res.returncode == 0, res.stderr
    assert res.stdout.split("\t")[:2] == ["alpha", "feat"]


def test_project_list_worktrees_json(root: Path) -> None:
    import json

    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    res = run("project", "list", "--worktrees", "--json", root=root)
    assert res.returncode == 0, res.stderr

    data = json.loads(res.stdout)
    assert data == [
        {
            "project": "alpha",
            "name": "feat",
            "path": str(root / "alpha" / "worktrees" / "feat"),
            "branch": "feat",
            "head": data[0]["head"],
        }
    ]
    assert len(data[0]["head"]) == 8


def test_project_list_worktrees_detached_has_empty_branch(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    wt = root / "alpha" / "worktrees" / "feat"
    subprocess.run(["git", "-C", str(wt), "checkout", "--detach"], check=True, capture_output=True)
    res = run("project", "list", "--worktrees", root=root)
    assert res.returncode == 0, res.stderr
    assert res.stdout.splitlines()[0].split("\t")[3] == ""  # branch column empty


def test_project_list_worktrees_and_tree_exclusive(root: Path) -> None:
    _seed_repo(root, "alpha")
    res = run("project", "list", "--worktrees", "--tree", root=root)
    assert res.returncode == 1
    assert "mutually exclusive" in res.stderr


def test_project_list_json_requires_worktrees(root: Path) -> None:
    _seed_repo(root, "alpha")
    res = run("project", "list", "--json", root=root)
    assert res.returncode == 1
    assert "--json requires --worktrees" in res.stderr


def test_worktree_list_strict_raises_on_broken_project(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (root / "broken" / ".git").mkdir(parents=True)
    _use_config(monkeypatch, root)
    from wtm import git
    from wtm.config import Config
    from wtm.worktree import Worktree

    worktree = Worktree(Config.load())
    # Lenient (default) swallows the failure; strict surfaces it.
    assert worktree.list(None, "broken") == []
    with pytest.raises(git.GitError):
        worktree.list(None, "broken", strict=True)


# --- list (worktrees) --------------------------------------------------------


def test_list_project_worktrees(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    res = run("list", "alpha", root=root)
    assert res.returncode == 0, res.stderr
    # wtm-native: one line per worktree, named relative to worktrees/.
    assert [line.split()[0] for line in res.stdout.splitlines()] == ["feat"]


def test_list_excludes_bare_repo(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    res = run("list", "alpha", root=root)
    assert res.returncode == 0, res.stderr
    assert [line.split()[0] for line in res.stdout.splitlines()] == ["feat"]
    assert "(bare)" not in res.stdout


def test_list_infers_project_from_cwd(root: Path) -> None:
    repo = _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    res = run("list", root=root, cwd=repo)  # no PROJECT arg
    assert res.returncode == 0, res.stderr
    assert [line.split()[0] for line in res.stdout.splitlines()] == ["feat"]


def test_list_outside_root_errors(root: Path, tmp_path: Path) -> None:
    res = run("list", root=root, cwd=tmp_path)  # cwd not under projects root
    assert res.returncode == 1
    assert "not under" in res.stderr


# --- list as a project-pinned locator ----------------------------------------


def test_list_pin_ignores_a_same_named_worktree(root: Path) -> None:
    # 'feat' is a worktree, never a project: the PROJECT pin refuses to cascade
    # into the worktree tier (unpinned, 'wtm enter feat' would land there).
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    res = run("list", "feat", root=root, cwd=root)
    assert res.returncode == 1
    assert "no project matches: feat" in res.stderr


def test_list_cross_workspace_project(tmp_path: Path) -> None:
    # A project in another workspace is listed by bare name, from an unrelated
    # active workspace (the portability win, at the project tier).
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "onlyproj")
    _run_multi("create", "onlyproj", "feat", config=config, cwd=alpha, workspace="alpha")
    res = _run_multi("list", "onlyproj", config=config, cwd=beta, workspace="beta")
    assert res.returncode == 0, res.stderr
    assert [line.split()[0] for line in res.stdout.splitlines()] == ["feat"]


def test_list_workspace_qualified_project(tmp_path: Path) -> None:
    # The 2-token form is still WORKSPACE PROJECT, as under the target grammar.
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "shared")
    _seed_repo_at(beta, "shared")
    _run_multi("create", "shared", "onlyalpha", config=config, cwd=alpha, workspace="alpha")
    res = _run_multi("list", "alpha", "shared", config=config, cwd=beta, workspace="beta")
    assert res.returncode == 0, res.stderr
    assert [line.split()[0] for line in res.stdout.splitlines()] == ["onlyalpha"]


def test_list_ambiguous_project_lists_choices_as_list(tmp_path: Path) -> None:
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "shared")
    _seed_repo_at(beta, "shared")
    # cwd is the beta root (not inside 'shared'), so the context tiebreak has a
    # workspace but no project -- and beta holds exactly one 'shared', which
    # would silently win. Stand outside both workspaces to keep the tie.
    res = _run_multi("list", "shared", config=config, cwd=tmp_path, workspace=None)
    assert res.returncode == 1
    assert "matches 2 projects" in res.stderr
    assert "wtm list alpha shared" in res.stderr
    assert "wtm list beta shared" in res.stderr


# --- the qualifier-only locator -----------------------------------------------
#
# create / enter -c / resolve / worktree set / config show --worktree name a
# worktree that need not exist, so only its leading PROJECT qualifier is located.


def test_create_cross_workspace_project(tmp_path: Path) -> None:
    # A bare PROJECT qualifier reaches another workspace, so the new worktree
    # lands there rather than under a phantom path in the active workspace.
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "onlyproj")
    res = _run_multi("create", "onlyproj", "feat", config=config, cwd=beta, workspace="beta")
    assert res.returncode == 0, res.stderr
    assert (alpha / "onlyproj" / "worktrees" / "feat").is_dir()


def test_create_ambiguous_qualifier_keeps_the_name_in_each_choice(tmp_path: Path) -> None:
    # The tokens located are the qualifiers only, so every printed choice must
    # carry the trailing NAME back to stay runnable as typed.
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "shared")
    _seed_repo_at(beta, "shared")
    res = _run_multi("create", "shared", "feat", config=config, cwd=tmp_path, workspace=None)
    assert res.returncode == 1
    assert "matches 2 projects" in res.stderr
    assert "wtm create alpha shared feat" in res.stderr
    assert "wtm create beta shared feat" in res.stderr


def test_create_workspace_qualified_still_exact(tmp_path: Path) -> None:
    # The 3-token form keeps meaning WORKSPACE PROJECT NAME: the qualifier pair
    # goes in as one locator query, 'alpha' qualifying the 'shared' leaf.
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "shared")
    _seed_repo_at(beta, "shared")
    res = _run_multi(
        "create", "alpha", "shared", "feat", config=config, cwd=tmp_path, workspace=None
    )
    assert res.returncode == 0, res.stderr
    assert (alpha / "shared" / "worktrees" / "feat").is_dir()
    assert not (beta / "shared" / "worktrees" / "feat").exists()


def test_create_inert_declared_project_errors_with_reminder(tmp_path: Path) -> None:
    # 'ghost' is declared in config but has no .git on disk -> create errors with
    # a reminder to materialise the project first, not the generic git-dir error.
    ws = tmp_path / "alpha"
    ws.mkdir()
    config = tmp_path / "c.toml"
    config.write_text(
        f'[workspaces.alpha]\npath = "{ws}"\n'
        '[workspaces.alpha.projects.ghost]\ndescription = "gone"\n'
    )
    res = _run_multi("create", "ghost", "feat", config=config, cwd=ws, workspace="alpha")
    assert res.returncode == 1
    assert "declared in config but not on disk" in res.stderr
    assert "wtm project new alpha ghost" in res.stderr
    assert not (ws / "ghost" / "worktrees" / "feat").exists()


def test_create_inert_declared_project_cross_workspace_reminder(tmp_path: Path) -> None:
    # Declared in beta, run from alpha -> the reminder names the real workspace.
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()
    config = tmp_path / "c.toml"
    config.write_text(
        f'[workspaces.alpha]\npath = "{alpha}"\n'
        f'[workspaces.beta]\npath = "{beta}"\n'
        '[workspaces.beta.projects.ghost]\ndescription = "gone"\n'
    )
    res = _run_multi("create", "ghost", "feat", config=config, cwd=alpha, workspace="alpha")
    assert res.returncode == 1
    assert "declared in config but not on disk" in res.stderr
    assert "wtm project new beta ghost" in res.stderr


def test_create_wholly_absent_project_keeps_generic_error(tmp_path: Path) -> None:
    # Not declared anywhere -> the locator falls back to the literal, and the
    # core's generic 'project git dir not found' fires, not the reminder.
    ws = tmp_path / "alpha"
    ws.mkdir()
    config = tmp_path / "c.toml"
    config.write_text(f'[workspaces.alpha]\npath = "{ws}"\n')
    res = _run_multi("create", "newproj", "newwt", config=config, cwd=ws, workspace="alpha")
    assert res.returncode == 1
    assert "project git dir not found" in res.stderr
    assert "declared in config" not in res.stderr
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "onlyproj")
    _run_multi("create", "onlyproj", "newwt", config=config, cwd=beta, workspace="beta")
    res = _run_multi("resolve", "onlyproj", "newwt", config=config, cwd=beta, workspace="beta")
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == str(alpha / "onlyproj" / "worktrees" / "newwt")


def test_resolve_nonexistent_cross_workspace_errors(tmp_path: Path) -> None:
    # The full locator requires the target to exist; a nonexistent project
    # or worktree is an error, not path math.
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, _ = roots
    _seed_repo_at(alpha, "onlyproj")
    res = _run_multi("resolve", "ghost", "newwt", config=config, cwd=alpha, workspace="alpha")
    assert res.returncode == 1
    assert "no worktree, project, or workspace matches" in res.stderr


def test_set_cross_workspace_project(tmp_path: Path) -> None:
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "onlyproj")
    _run_multi("create", "onlyproj", "feat", config=config, cwd=alpha, workspace="alpha")
    res = _run_multi(
        "worktree",
        "set",
        "onlyproj",
        "feat",
        "-d",
        "noted",
        config=config,
        cwd=beta,
        workspace="beta",
    )
    assert res.returncode == 0, res.stderr
    written = (alpha / "onlyproj" / "worktrees" / "feat" / ".wtm" / "config.toml").read_text()
    assert "noted" in written


def test_config_show_worktree_cross_workspace_project(tmp_path: Path) -> None:
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "onlyproj")
    _run_multi("create", "onlyproj", "feat", config=config, cwd=alpha, workspace="alpha")
    res = _run_multi(
        "config",
        "show",
        "onlyproj",
        "feat",
        config=config,
        cwd=beta,
        workspace="beta",
    )
    assert res.returncode == 0, res.stderr
    assert "feat" in res.stdout


# --- config show --project locator (dual-mode) ------------------------------


def test_config_show_project_cross_workspace(tmp_path: Path) -> None:
    # --project resolves PROJECT across every workspace: a bare project name
    # reaches another workspace's project from any active workspace.
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "onlyproj")
    res = _run_multi(
        "config",
        "show",
        "onlyproj",
        config=config,
        cwd=beta,
        workspace="beta",
    )
    assert res.returncode == 0, res.stderr
    assert "project onlyproj:" in res.stdout


def test_config_show_project_inert_falls_back_to_literal(tmp_path: Path) -> None:
    # A declared-but-absent (inert) project is invisible to the locator index,
    # so --project falls back to the literal target-grammar path and dumps the
    # inert subtree (mirrors the --all inert behaviour at the project level).
    config, roots = _multi_workspace_config(tmp_path, ["alpha"])
    (roots[0],)
    # Declare an inert project (no .git on disk) in the alpha workspace config.
    config.write_text(
        f'[workspaces.alpha]\npath = "{roots[0]}"\n'
        '[workspaces.alpha.projects.ghost]\ndescription = "gone"\n'
    )
    res = _run_multi("config", "show", "ghost", config=config, cwd=tmp_path, workspace="alpha")
    assert res.returncode == 0, res.stderr
    assert "project ghost:" in res.stdout
    assert "(no .git on disk)" in res.stdout


def test_config_show_project_ambiguous_errors(tmp_path: Path) -> None:
    # A project name on disk in two workspaces, with no active workspace to break
    # the tie (cwd outside both workspaces, no WTM_WORKSPACE), is an unresolved
    # ambiguity -> non-zero exit and copy-runnable choices.
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    _seed_repo_at(roots[0], "shared")
    _seed_repo_at(roots[1], "shared")
    # Run from tmp_path (not inside either workspace) with no WTM_WORKSPACE so
    # the cwd context tiebreak cannot single one out.
    res = _run_multi("config", "show", "shared", config=config, cwd=tmp_path)
    assert res.returncode != 0
    assert "matches 2 projects" in res.stderr
    # Each choice is a fully-qualified, runnable command.
    assert "wtm config show alpha shared" in res.stderr
    assert "wtm config show beta shared" in res.stderr


def test_config_show_project_ambiguous_resolved_by_context(tmp_path: Path) -> None:
    # Same name clash across workspaces, but cwd is inside one workspace's
    # project -> the context tiebreak resolves it silently to the in-context one.
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    _seed_repo_at(roots[0], "shared")
    _seed_repo_at(roots[1], "shared")
    # cwd inside alpha/shared (a bare repo dir, but project_dir_for matches it).
    res = _run_multi(
        "config",
        "show",
        "shared",
        config=config,
        cwd=roots[0] / "shared",
        workspace="alpha",
    )
    assert res.returncode == 0, res.stderr
    assert "project shared:" in res.stdout


def test_config_show_project_completion_spans_workspaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # --project completion offers projects across *every* workspace (the
    # completion-side expression of cross-workspace reach), plus workspaces as
    # qualifiers.
    config, roots = _multi_workspace_config(tmp_path, ["dev", "prod"])
    _seed_index_worktree(roots[0], "datactl", "feat")  # dev/datactl
    _seed_index_worktree(roots[1], "api", "feat")  # prod/api
    monkeypatch.setenv("WTM_CONFIG", str(config))
    monkeypatch.setenv("WTM_SYSTEM_CONFIG", str(tmp_path / "_no_system_config.toml"))
    monkeypatch.delenv("WTM_WORKSPACE", raising=False)
    from wtm.cli.completions.config import _complete_config_show

    items = dict(_complete_config_show(_ConfigCtx(), ""))
    # Projects from both workspaces appear (locator reach), plus both workspaces
    # as qualifiers.
    assert "datactl" in items
    assert "api" in items
    assert "dev" in items
    assert "prod" in items


# --- list completion ---------------------------------------------------------


def _complete(root: Path, *, words: str, cword: str, cwd: Path | None = None) -> str:
    """Drive the real bash completion protocol against the installed console
    script (prog name 'wtm' -> deterministic _WTM_COMPLETE) and return stdout.
    """
    wtm_bin = Path(sys.executable).parent / "wtm"
    env = {
        **os.environ,
        **_config_env(root),
        "_WTM_COMPLETE": "complete_bash",
        "COMP_WORDS": words,
        "COMP_CWORD": cword,
    }
    res = subprocess.run(
        [str(wtm_bin)],
        env=env,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr

    return res.stdout


def test_complete_project_names_and_label(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_repo(root, "alpha")
    _seed_repo(root, "alto")
    _seed_repo(root, "beta")
    (root / "stray").mkdir()  # no .git/, must be excluded
    _use_config(monkeypatch, root)
    from wtm.cli.completions.target import _complete_project

    class _C:  # minimal stand-in: only .params is read
        params = {"args": ()}

    # Slot 0 of [[WORKSPACE] PROJECT]: declared workspaces plus the active
    # workspace's projects; Typer applies the prefix filter downstream.
    items = _complete_project(_C(), "")
    assert [value for value, _ in items] == ["alpha", "alto", "beta", "ws"]
    assert {label for _, label in items} == {"project", "workspace"}


def test_list_completion_emits_projects(root: Path) -> None:
    _seed_repo(root, "alpha")
    _seed_repo(root, "beta")
    (root / "stray").mkdir()  # no .git/, must be excluded
    out = _complete(root, words="wtm list ", cword="2")
    # Slot 0 unions the declared workspace ('ws') with the active projects.
    assert out.split() == ["alpha", "beta", "ws"]


@pytest.mark.parametrize("cmd", ["create", "delete", "resolve"])
def test_first_slot_completes_projects(root: Path, cmd: str) -> None:
    _seed_repo(root, "alpha")
    _seed_repo(root, "beta")
    (root / "stray").mkdir()  # no .git/, must be excluded
    out = _complete(root, words=f"wtm {cmd} ", cword="2")
    # Slot 0 unions the declared workspace ('ws') with the active projects.
    assert out.split() == ["alpha", "beta", "ws"]


def test_second_slot_no_project_completion(root: Path) -> None:
    _seed_repo(root, "alpha")
    out = _complete(root, words="wtm create alpha ", cword="3")
    assert out.split() == []


# --- worktree-name completion ------------------------------------------------


def test_worktree_names_lists_relative_names(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    run("create", "alpha", "feature/x", root=root)  # nested NAME
    (root / "alpha" / "worktrees" / "stray").mkdir()  # not a worktree -> excluded
    out = _complete(root, words="wtm enter alpha ", cword="3")
    # Full slashed names; the bare repo and the stray dir are excluded.
    assert out.split() == ["feat", "feature/x"]


def test_worktree_names_lenient_on_missing_project(root: Path) -> None:
    out = _complete(root, words="wtm enter nope ", cword="3")
    assert out.split() == []


def test_worktree_names_infers_project_from_cwd(root: Path) -> None:
    repo = _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    out = _complete(root, words="wtm enter ", cword="2", cwd=repo)
    assert "feat" in out.split()


@pytest.mark.parametrize("cmd", ["delete", "resolve"])
def test_second_slot_completes_worktrees(root: Path, cmd: str) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    run("create", "alpha", "fix", root=root)
    out = _complete(root, words=f"wtm {cmd} alpha ", cword="3")
    assert out.split() == ["feat", "fix"]


def test_first_slot_unions_projects_and_cwd_worktrees(root: Path) -> None:
    repo = _seed_repo(root, "alpha")
    _seed_repo(root, "beta")
    run("create", "alpha", "feat", root=root)
    # From inside project 'alpha': workspace ('ws') U projects (alpha, beta) U
    # alpha's worktrees (feat).
    out = _complete(root, words="wtm enter ", cword="2", cwd=repo)
    assert out.split() == ["alpha", "beta", "feat", "ws"]


def test_first_slot_outside_root_projects_only(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    # enter's slot 0 is a global locator: the whole worktree/project/workspace
    # universe, independent of cwd (the history-portability win), so 'feat' is
    # offered even from outside the projects root.
    out = _complete(root, words="wtm enter ", cword="2", cwd=tmp_path)
    assert out.split() == ["alpha", "feat", "ws"]


def test_pinned_completion_drops_the_tiers_it_cannot_resolve(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    # Unpinned (enter) offers the whole universe; the PROJECT pin (list) drops
    # worktree names but keeps workspaces, which still qualify a project.
    assert _complete(root, words="wtm enter ", cword="2").split() == ["alpha", "feat", "ws"]
    assert _complete(root, words="wtm list ", cword="2").split() == ["alpha", "ws"]
    # The WORKTREE pin (hook list) drops nothing: every tier is its leaf or an
    # ancestor of it.
    assert _complete(root, words="wtm hook list ", cword="3").split() == ["alpha", "feat", "ws"]


# --- completion kind labels --------------------------------------------------


class _Ctx:
    """Minimal typer.Context stand-in for positional completion callbacks."""

    def __init__(self, args: tuple[str, ...]) -> None:
        self.params = {"args": args}


def _labels(items: list[tuple[str, str]]) -> dict[str, str]:
    """Collapse (value, help) completion tuples to a {value: help} map."""
    return dict(items)


def test_target_labels_project_and_worktree(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _seed_repo(root, "alpha")
    _seed_repo(root, "beta")
    run("create", "alpha", "feat", root=root)
    _use_config(monkeypatch, root)
    monkeypatch.chdir(repo)  # inside 'alpha' -> its worktrees join the projects
    from wtm.cli.completions.target import _complete_target

    # Slot 0: workspaces ('ws') labelled 'workspace', projects (alpha, beta)
    # labelled 'project', cwd worktree 'feat' labelled 'worktree'.
    assert _labels(_complete_target(_Ctx(()), "")) == {
        "alpha": "project",
        "beta": "project",
        "feat": "worktree",
        "ws": "workspace",
    }
    # Slot 1: the named project's worktrees only -> homogeneous 'worktree'.
    assert _labels(_complete_target(_Ctx(("alpha",)), "")) == {"feat": "worktree"}


def test_target_label_collision_worktree_wins(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _seed_repo(root, "alpha")
    _seed_repo(root, "beta")
    run("create", "alpha", "beta", root=root)  # worktree named like project 'beta'
    _use_config(monkeypatch, root)
    monkeypatch.chdir(repo)
    from wtm.cli.completions.target import _complete_target

    # A lone 'beta' resolves to alpha's worktree, so worktree heads; the project
    # role (reachable only with a second token) is shadowed.
    assert _labels(_complete_target(_Ctx(()), ""))["beta"] == "worktree (also project)"


def test_locate_completion_global_union_and_labels(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_repo(root, "alpha")
    _seed_repo(root, "beta")
    run("create", "alpha", "feat", root=root)
    _use_config(monkeypatch, root)
    monkeypatch.chdir(root)  # the locator is global; cwd does not scope it
    from wtm.cli.completions.locator import _complete_locate

    # Slot 0 is the whole universe: every worktree, project, and workspace. A
    # worktree carries its project context; a single-workspace project stays bare.
    assert _labels(_complete_locate(_Ctx(()), "")) == {
        "alpha": "project",
        "beta": "project",
        "feat": "worktree in alpha",
        "ws": "workspace",
    }


def test_locate_completion_collision_hint(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_repo(root, "alpha")
    _seed_repo(root, "beta")
    run("create", "alpha", "shared", root=root)
    run("create", "beta", "shared", root=root)
    _use_config(monkeypatch, root)
    from wtm.cli.completions.locator import _complete_locate

    # 'shared' names two worktrees -> the winning tier enumerates their contexts.
    assert _labels(_complete_locate(_Ctx(()), ""))["shared"] == "worktree in alpha, beta"


def test_locate_completion_narrows_by_qualifier(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_repo(root, "alpha")
    _seed_repo(root, "beta")
    run("create", "alpha", "feat", root=root)
    run("create", "beta", "fix", root=root)
    _use_config(monkeypatch, root)
    from wtm.cli.completions.locator import _complete_locate

    # A leading project qualifier narrows the next slot to that project's worktrees.
    assert _labels(_complete_locate(_Ctx(("alpha",)), "")) == {"feat": "worktree in alpha"}


def _seed_index_worktree(ws_root: Path, project: str, name: str) -> None:
    """Scaffold a project + worktree on disk for the git-free locator index.

    :func:`build_index` only reads dirs holding a ``.git``, so a bare marker
    suffices -- no real git worktree needed. Used by the multi-workspace label
    tests, which exercise :func:`_complete_locate` (filesystem-only) directly.
    """
    (ws_root / project / ".git").mkdir(parents=True, exist_ok=True)
    (ws_root / project / "worktrees" / name / ".git").mkdir(parents=True, exist_ok=True)


def test_locate_completion_context_spans_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, roots = _multi_workspace_config(tmp_path, ["dev", "prod"])
    _seed_index_worktree(roots[0], "datactl", "feat")  # dev/datactl
    _seed_index_worktree(roots[1], "api", "feat")  # prod/api (same worktree name)
    monkeypatch.setenv("WTM_CONFIG", str(config))
    monkeypatch.setenv("WTM_SYSTEM_CONFIG", str(tmp_path / "_no_system_config.toml"))
    monkeypatch.delenv("WTM_WORKSPACE", raising=False)  # let the config's workspaces stand
    from wtm.cli.completions.locator import _complete_locate

    # >1 workspace declared -> the context carries workspace/project (slash form);
    # 'feat' collides across workspaces, so both contexts enumerate.
    labels = _labels(_complete_locate(_Ctx(()), ""))
    assert labels["datactl"] == "project in dev"
    assert labels["api"] == "project in prod"
    assert labels["feat"] == "worktree in dev/datactl, prod/api"


def test_locate_completion_context_caps_at_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, roots = _multi_workspace_config(tmp_path, ["ws"])
    for project in ("a", "b", "c", "d"):
        _seed_index_worktree(roots[0], project, "shared")
    monkeypatch.setenv("WTM_CONFIG", str(config))
    monkeypatch.setenv("WTM_SYSTEM_CONFIG", str(tmp_path / "_no_system_config.toml"))
    monkeypatch.delenv("WTM_WORKSPACE", raising=False)  # let the config's workspaces stand
    from wtm.cli.completions.locator import _complete_locate

    # A worktree name across four projects (single workspace) -> two contexts then
    # a '+N more' tail.
    assert _labels(_complete_locate(_Ctx(()), ""))["shared"] == "worktree in a, b +2 more"


def test_locate_completion_context_with_shadow(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_repo(root, "alpha")
    _seed_repo(root, "beta")
    run("create", "alpha", "beta", root=root)  # worktree named like project 'beta'
    _use_config(monkeypatch, root)
    from wtm.cli.completions.locator import _complete_locate

    # 'beta' wins as a worktree (context shown); the project role it shadows trails.
    assert _labels(_complete_locate(_Ctx(()), ""))["beta"] == "worktree in alpha (also project)"


def test_hook_target_labels_event(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    _use_config(monkeypatch, root)
    monkeypatch.chdir(repo)
    from wtm.cli.completions.target import _complete_hook_target

    labels = _labels(_complete_hook_target(_Ctx(()), ""))
    assert labels["alpha"] == "project"
    assert labels["feat"] == "worktree"
    assert labels["pre-create"] == "event"


def test_hook_target_label_collision_event_wins(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _seed_repo(root, "alpha")
    run("create", "alpha", "install", root=root)  # worktree named like an event
    _use_config(monkeypatch, root)
    monkeypatch.chdir(repo)
    from wtm.cli.completions.target import _complete_hook_target

    # A lone 'install' resolves to the event (last positional is the HOOK), so
    # event heads; the worktree role trails.
    assert _labels(_complete_hook_target(_Ctx(()), ""))["install"] == "event (also worktree)"


def test_locate_hook_target_unions_events_with_locator_names(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 'hook run' completes its target from the locator's name universe (so a
    # worktree carries its location context), with the events merged in.
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    _use_config(monkeypatch, root)
    monkeypatch.chdir(root)  # the locator is global; cwd does not scope it
    from wtm.cli.completions.locator import _complete_locate_hook_target

    labels = _labels(_complete_locate_hook_target(_Ctx(()), ""))
    assert labels["feat"] == "worktree in alpha"
    assert labels["alpha"] == "project"
    assert labels["pre-create"] == "event"


def _complete_zsh(root: Path, *, args: str, cwd: Path | None = None) -> str:
    """Drive the real zsh completion protocol and return stdout.

    Unlike the bash protocol, the zsh formatter renders each item as
    ``"value":"help"``, so this is the seam that proves the kind label surfaces.
    """
    wtm_bin = Path(sys.executable).parent / "wtm"
    env = {
        **os.environ,
        **_config_env(root),
        "_WTM_COMPLETE": "complete_zsh",
        "_TYPER_COMPLETE_ARGS": args,
    }
    res = subprocess.run(
        [str(wtm_bin)],
        env=env,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr

    return res.stdout


def test_zsh_completion_renders_kind_labels(root: Path) -> None:
    repo = _seed_repo(root, "alpha")
    _seed_repo(root, "beta")
    run("create", "alpha", "feat", root=root)
    out = _complete_zsh(root, args="wtm enter ", cwd=repo)
    # zsh format is "value":"help"; each candidate carries its kind label, and the
    # locator winner also carries its location context.
    assert '"alpha":"project"' in out
    assert '"beta":"project"' in out
    assert '"feat":"worktree in alpha"' in out


# --- branch completion (-b) --------------------------------------------------


def test_complete_branch_unit(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_repo(root, "alpha")  # local head: main
    run("create", "alpha", "feat", root=root)  # local head: feat
    run("create", "alpha", "fix", root=root)  # local head: fix
    _use_config(monkeypatch, root)
    from wtm.cli.completions.target import _complete_branch

    class _Ctx:  # minimal stand-in: .args (option-value completion) + .params
        def __init__(self, args: tuple[str, ...]) -> None:
            self.args = list(args)
            self.params = {"args": ()}

    # Two positionals -> PROJECT NAME: branches come from the first ('alpha').
    assert _complete_branch(_Ctx(("alpha", "new")), "") == ["feat", "fix", "main"]
    assert _complete_branch(_Ctx(("alpha", "new")), "f") == ["feat", "fix"]
    # A single positional is the NAME -> cwd inference; the test cwd is outside
    # the root, so it degrades leniently to nothing.
    assert _complete_branch(_Ctx(("feat",)), "") == []


def test_complete_branch_e2e_project_form(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    # `wtm create alpha newwt -b <TAB>`: two positionals -> PROJECT NAME, so the
    # option value completes with 'alpha' visible and branches come from it. This
    # verifies click populates ctx.params['args'] for an option value.
    out = _complete(root, words="wtm create alpha newwt -b ", cword="5")
    assert out.split() == ["feat", "main"]


def test_complete_branch_e2e_origin_bare_names(root: Path) -> None:
    git_dir = _seed_repo(root, "alpha")
    # Add a branch on the source and fetch it into refs/remotes/origin/*.
    source = root / "_sources" / "alpha"
    _git(source, "branch", "release", root=root)
    _git(git_dir, "fetch", "origin", root=root)
    out = _complete(root, words="wtm create alpha newwt -b ", cword="5").split()
    # origin/release shows as the bare name 'release'; origin/HEAD is dropped.
    assert "release" in out
    assert "main" in out
    assert "HEAD" not in out


def test_complete_branch_e2e_cwd_inference(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    project_dir = root / "alpha"
    # `-b` before any positional: only cwd inference is available.
    out = _complete(root, words="wtm create -b ", cword="3", cwd=project_dir)
    assert out.split() == ["feat", "main"]


# --- config get completion ---------------------------------------------------


def _seed_config_keys(root: Path, project: str) -> Path:
    """Write project-local and workspace-local arbitrary keys for completion tests.

    Project *project* gets ``owner`` (scalar), ``ports`` (array), ``labels``
    (table), and an ``inheritable`` allowlist naming ``deploy_env`` at project
    scope; the workspace gets ``base_branch`` at workspace scope (inheritable, so
    it resolves for the project even though the project does not set it).
    """
    proj_dir = root / project
    (proj_dir / ".wtm").mkdir(parents=True, exist_ok=True)
    (proj_dir / ".wtm" / "config.toml").write_text(
        'owner = "team-a"\nports = ["3001", "3002"]\n'
        'inheritable = ["deploy_env"]\n[labels]\nenv = "prod"\ntier = "web"\n'
    )
    (root / ".wtm").mkdir(parents=True, exist_ok=True)
    (root / ".wtm" / "config.toml").write_text('base_branch = "main"\n')

    return proj_dir


def test_complete_config_key_effective_set(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_repo(root, "alpha")
    proj_dir = _seed_config_keys(root, "alpha")
    _use_config(monkeypatch, root)
    monkeypatch.chdir(proj_dir)
    from wtm.cli.completions.config import _complete_config_key

    class _C:
        params = {}

    items = dict(_complete_config_key(_C(), ""))
    # base_branch inherits from the workspace tier; owner + labels are project-local.
    # Key provenance labels are prefixed ``config in <level>`` to avoid colliding
    # with the locator kind labels (``workspace``/``project``/...) they share a
    # column with in the union completion.
    assert items["base_branch"] == "config in workspace"
    assert items["owner"] == "config in project"
    assert items["labels"] == "config in project"
    # Reserved keys never appear (they own typed surfaces, not the arbitrary-key get).
    assert "path" not in items
    assert "projects" not in items
    assert "inheritable" not in items
    # Prefix filter.
    assert dict(_complete_config_key(_C(), "la")) == {
        "labels": "config in project",
        "labels.env": "config in project",
        "labels.tier": "config in project",
    }


def test_complete_config_subkey_table_cells(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_repo(root, "alpha")
    proj_dir = _seed_config_keys(root, "alpha")
    _use_config(monkeypatch, root)
    monkeypatch.chdir(proj_dir)
    from wtm.cli.completions.config import _complete_config_subkey

    class _C:
        def __init__(self, key: str) -> None:
            self.params = {"key": key}

    # A table KEY offers its cells; a scalar KEY offers nothing; a reserved key
    # is rejected upstream and yields nothing rather than erroring.
    assert _complete_config_subkey(_C("labels"), "") == ["env", "tier"]
    assert _complete_config_subkey(_C("labels"), "t") == ["tier"]
    assert _complete_config_subkey(_C("owner"), "") == []
    assert _complete_config_subkey(_C("path"), "") == []


def test_config_get_key_completion_e2e(root: Path) -> None:
    _seed_repo(root, "alpha")
    proj_dir = _seed_config_keys(root, "alpha")
    out = _complete(root, words="wtm config get ", cword="3", cwd=proj_dir)
    names = out.split()
    assert "base_branch" in names
    assert "owner" in names
    assert "labels" in names


def test_config_get_subkey_completion_e2e(root: Path) -> None:
    _seed_repo(root, "alpha")
    proj_dir = _seed_config_keys(root, "alpha")
    out = _complete(root, words="wtm config get labels ", cword="4", cwd=proj_dir)
    assert out.split() == ["env", "tier"]


def test_config_get_dotted_key_completion_e2e(root: Path) -> None:
    # Deep dotted completion: typing a dotted prefix offers every addressable
    # cell under that head (the space-form SUBKEY completion above stays one
    # level; the dotted KEY form descends the whole section).
    _seed_repo(root, "alpha")
    proj_dir = _seed_config_keys(root, "alpha")
    out = _complete(root, words="wtm config get labels.", cword="3", cwd=proj_dir)
    names = out.split()
    assert "labels.env" in names
    assert "labels.tier" in names
    # The head itself is a prefix of ``labels.``, so it drops out of the filter.
    assert "labels" not in names


def test_config_get_top_level_completion_offers_deep_paths(root: Path) -> None:
    # All deep paths always: a top-level TAB offers not just the head keys but
    # every dotted cell, so a table's interior is discoverable from the start.
    _seed_repo(root, "alpha")
    proj_dir = _seed_config_keys(root, "alpha")
    out = _complete(root, words="wtm config get ", cword="3", cwd=proj_dir)
    names = out.split()
    assert "labels" in names
    assert "labels.env" in names
    assert "labels.tier" in names
    # An array (ports) is not a table: it emits its bare key, no dotted cells.
    assert "ports" in names
    assert all(not n.startswith("ports.") for n in names)


def test_config_get_key_completion_labels_zsh(root: Path) -> None:
    _seed_repo(root, "alpha")
    proj_dir = _seed_config_keys(root, "alpha")
    out = _complete_zsh(root, args="wtm config get ", cwd=proj_dir)
    # zsh renders (value, help) as "value":"help"; the definition-tier label
    # surfaces (base_branch from workspace, owner from project), prefixed
    # ``config in <level>`` to avoid colliding with locator kind labels.
    assert '"base_branch":"config in workspace"' in out
    assert '"owner":"config in project"' in out


# --- config set/unset/show/scope completion ---------------------------------


class _ConfigCtx:
    """Minimal typer.Context stand-in for the config set/unset/show completers."""

    def __init__(
        self,
        args: tuple[str, ...] = (),
        array: bool = False,
        table: bool = False,
        **flags: bool,
    ) -> None:
        self.args = list(args)
        self.params: dict[str, object] = {"args": args, "array": array, "table": table}
        self.params.update(flags)


def test_complete_config_set_key_effective(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_repo(root, "alpha")
    proj_dir = _seed_config_keys(root, "alpha")
    _use_config(monkeypatch, root)
    monkeypatch.chdir(proj_dir)
    from wtm.cli.completions.config import _complete_config_set

    items = dict(_complete_config_set(_ConfigCtx(), ""))
    assert items["owner"] == "config in project"
    assert items["labels"] == "config in project"
    assert items["ports"] == "config in project"
    assert items["base_branch"] == "config in workspace"
    # No -a: inheritable is reserved, not offered.
    assert "inheritable" not in items


def test_complete_config_set_key_includes_inheritable_with_a(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_repo(root, "alpha")
    proj_dir = _seed_config_keys(root, "alpha")
    _use_config(monkeypatch, root)
    monkeypatch.chdir(proj_dir)
    from wtm.cli.completions.config import _complete_config_set

    items = dict(_complete_config_set(_ConfigCtx(array=True), ""))
    # With -a, the one reserved array a user may mutate is offered.
    assert items["inheritable"] == "allowlist"
    assert items["ports"] == "config in project"


def test_complete_config_set_subkey_for_table(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_repo(root, "alpha")
    proj_dir = _seed_config_keys(root, "alpha")
    _use_config(monkeypatch, root)
    monkeypatch.chdir(proj_dir)
    from wtm.cli.completions.config import _complete_config_set

    assert _complete_config_set(_ConfigCtx(("labels",), table=True), "") == ["env", "tier"]
    assert _complete_config_set(_ConfigCtx(("labels",), table=True), "t") == ["tier"]


def test_complete_config_set_skips_value_slot(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_repo(root, "alpha")
    proj_dir = _seed_config_keys(root, "alpha")
    _use_config(monkeypatch, root)
    monkeypatch.chdir(proj_dir)
    from wtm.cli.completions.config import _complete_config_set

    # Slot 1 without -t is the free-form VALUE: no completion.
    assert _complete_config_set(_ConfigCtx(("owner",)), "") == []
    # Slot 2 (VALUE for -t) is also free-form.
    assert _complete_config_set(_ConfigCtx(("labels", "env"), table=True), "") == []


def test_complete_config_unset_key_effective(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_repo(root, "alpha")
    proj_dir = _seed_config_keys(root, "alpha")
    _use_config(monkeypatch, root)
    monkeypatch.chdir(proj_dir)
    from wtm.cli.completions.config import _complete_config_unset

    items = dict(_complete_config_unset(_ConfigCtx(), ""))
    assert items["owner"] == "config in project"
    assert "inheritable" not in items


def test_complete_config_unset_key_includes_inheritable_with_a(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_repo(root, "alpha")
    proj_dir = _seed_config_keys(root, "alpha")
    _use_config(monkeypatch, root)
    monkeypatch.chdir(proj_dir)
    from wtm.cli.completions.config import _complete_config_unset

    items = dict(_complete_config_unset(_ConfigCtx(array=True), ""))
    assert items["inheritable"] == "allowlist"


def test_complete_config_unset_subkey_for_table(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_repo(root, "alpha")
    proj_dir = _seed_config_keys(root, "alpha")
    _use_config(monkeypatch, root)
    monkeypatch.chdir(proj_dir)
    from wtm.cli.completions.config import _complete_config_unset

    assert _complete_config_unset(_ConfigCtx(("labels",), table=True), "") == ["env", "tier"]


def test_complete_config_unset_array_elements(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_repo(root, "alpha")
    proj_dir = _seed_config_keys(root, "alpha")
    _use_config(monkeypatch, root)
    monkeypatch.chdir(proj_dir)
    from wtm.cli.completions.config import _complete_config_unset

    # -a offers the array's current elements (you can only remove what is there).
    assert _complete_config_unset(_ConfigCtx(("ports",), array=True), "") == ["3001", "3002"]
    assert _complete_config_unset(_ConfigCtx(("ports",), array=True), "3") == ["3001", "3002"]
    assert _complete_config_unset(_ConfigCtx(("ports",), array=True), "4") == []


def test_complete_config_unset_inheritable_array_elements(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_repo(root, "alpha")
    proj_dir = _seed_config_keys(root, "alpha")
    _use_config(monkeypatch, root)
    monkeypatch.chdir(proj_dir)
    from wtm.cli.completions.config import _complete_config_unset

    # unset -a inheritable VALUE: the reserved key is read directly.
    assert _complete_config_unset(_ConfigCtx(("inheritable",), array=True), "") == ["deploy_env"]


def test_complete_config_unset_scalar_key_only(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_repo(root, "alpha")
    proj_dir = _seed_config_keys(root, "alpha")
    _use_config(monkeypatch, root)
    monkeypatch.chdir(proj_dir)
    from wtm.cli.completions.config import _complete_config_unset

    # Scalar unset takes only KEY; slot 1 without flags has no completion.
    assert _complete_config_unset(_ConfigCtx(("owner",)), "") == []


def test_complete_config_show_no_level_no_completion(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_repo(root, "alpha")
    _seed_repo(root, "beta")
    _use_config(monkeypatch, root)
    from wtm.cli.completions.config import _complete_config_show

    items = dict(_complete_config_show(_ConfigCtx(), ""))
    assert "alpha" in items
    assert "beta" in items


def test_complete_config_show_all_no_completion(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_repo(root, "alpha")
    _use_config(monkeypatch, root)
    from wtm.cli.completions.config import _complete_config_show

    items = dict(_complete_config_show(_ConfigCtx(), ""))
    assert "alpha" in items


def test_complete_config_show_workspace_level(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_repo(root, "alpha")
    _use_config(monkeypatch, root)
    from wtm.cli.completions.config import _complete_config_show

    items = dict(_complete_config_show(_ConfigCtx(), ""))
    assert "ws" in items


def test_complete_config_show_project_level(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_repo(root, "alpha")
    _seed_repo(root, "beta")
    _use_config(monkeypatch, root)
    from wtm.cli.completions.config import _complete_config_show

    items = dict(_complete_config_show(_ConfigCtx(), ""))
    # Slot 0 offers workspaces + active workspace's projects.
    assert "alpha" in items
    assert "beta" in items
    assert "ws" in items


def test_complete_config_show_worktree_level(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    _use_config(monkeypatch, root)
    monkeypatch.chdir(root)
    from wtm.cli.completions.config import _complete_config_show

    items = dict(_complete_config_show(_ConfigCtx(), ""))
    # PROJECT-pinned locator: slot 0 offers projects (+ workspaces as qualifiers).
    assert "alpha" in items
    assert "ws" in items


def test_complete_config_show_project_level_slot1(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_repo(root, "alpha")
    _seed_repo(root, "beta")
    _use_config(monkeypatch, root)
    from wtm.cli.completions.config import _complete_config_show

    items = dict(_complete_config_show(_ConfigCtx(("ws",)), ""))
    # Slot 1 offers projects of workspace args[0]='ws'.
    assert "alpha" in items
    assert "beta" in items


def test_complete_scope_from_project_cwd(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_repo(root, "alpha")
    proj_dir = _seed_config_keys(root, "alpha")
    _use_config(monkeypatch, root)
    monkeypatch.chdir(proj_dir)
    from wtm.cli.completions.config import _complete_scope

    items = dict(_complete_scope(_ConfigCtx(), ""))
    # A project entity allows project/workspace/user/system (not worktree).
    assert set(items) == {"project", "workspace", "user", "system"}
    assert items["project"] == "default"
    assert items["workspace"] == ""


def test_complete_scope_from_worktree_cwd(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    _use_config(monkeypatch, root)
    monkeypatch.chdir(root / "alpha" / "worktrees" / "feat")
    from wtm.cli.completions.config import _complete_scope

    items = dict(_complete_scope(_ConfigCtx(), ""))
    # A worktree entity allows all 5 scopes; worktree is the default.
    assert set(items) == {"worktree", "project", "workspace", "user", "system"}
    assert items["worktree"] == "default"


def test_complete_scope_fallback_all_scopes(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_repo(root, "alpha")
    # No workspace declared and none selectable -> resolve_generic_target raises
    # -> fallback to all 5 scopes (preserving typer's default enum behaviour).
    monkeypatch.setenv("WTM_CONFIG", str(root / "_empty.toml"))
    monkeypatch.setenv("WTM_SYSTEM_CONFIG", str(root / "_no_system.toml"))
    monkeypatch.delenv("WTM_WORKSPACE", raising=False)
    from wtm.cli.completions.config import _complete_scope

    items = dict(_complete_scope(_ConfigCtx(), ""))
    assert set(items) == {"system", "user", "workspace", "project", "worktree"}
    # No "default" label on fallback.
    assert all(label == "" for label in items.values())


def test_config_set_key_completion_e2e(root: Path) -> None:
    _seed_repo(root, "alpha")
    proj_dir = _seed_config_keys(root, "alpha")
    out = _complete(root, words="wtm config set ", cword="3", cwd=proj_dir)
    names = out.split()
    assert "owner" in names
    assert "ports" in names
    assert "labels" in names
    assert "base_branch" in names


def test_config_set_inheritable_key_completion_e2e(root: Path) -> None:
    _seed_repo(root, "alpha")
    proj_dir = _seed_config_keys(root, "alpha")
    out = _complete(root, words="wtm config set -a ", cword="4", cwd=proj_dir)
    names = out.split()
    assert "inheritable" in names
    assert "ports" in names


def test_config_unset_array_element_completion_e2e(root: Path) -> None:
    _seed_repo(root, "alpha")
    proj_dir = _seed_config_keys(root, "alpha")
    out = _complete(root, words="wtm config unset -a ports ", cword="5", cwd=proj_dir)
    assert out.split() == ["3001", "3002"]


def test_config_show_project_completion_e2e(root: Path) -> None:
    _seed_repo(root, "alpha")
    _seed_repo(root, "beta")
    out = _complete(root, words="wtm config show ", cword="3")
    names = out.split()
    assert "alpha" in names
    assert "beta" in names


def test_config_show_no_level_no_completion_e2e(root: Path) -> None:
    _seed_repo(root, "alpha")
    out = _complete(root, words="wtm config show ", cword="3")
    assert "alpha" in out.split()


def test_scope_completion_e2e(root: Path) -> None:
    _seed_repo(root, "alpha")
    proj_dir = _seed_config_keys(root, "alpha")
    out = _complete(root, words="wtm config set owner nvim -s ", cword="6", cwd=proj_dir)
    names = out.split()
    # From a project cwd: project/workspace/user/system (not worktree).
    assert "project" in names
    assert "workspace" in names
    assert "worktree" not in names


def test_scope_completion_default_label_zsh(root: Path) -> None:
    _seed_repo(root, "alpha")
    proj_dir = _seed_config_keys(root, "alpha")
    out = _complete_zsh(root, args="wtm config set owner nvim -s ", cwd=proj_dir)
    # The auto-default scope (project) is labelled "default"; empty labels render
    # as bare "value" in zsh (no :"" suffix).
    assert '"project":"default"' in out
    assert '"workspace"' in out


def test_complete_config_set_global_tier_slots(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_repo(root, "alpha")
    cfg = root / "cfg.toml"
    cfg.write_text(f'editor = "nvim"\n[workspaces.ws]\npath = "{root}"\n')
    monkeypatch.setenv("WTM_CONFIG", str(cfg))
    from wtm.cli.completions.config import _complete_config_set

    items = dict(_complete_config_set(_ConfigCtx(**{"global": True}), ""))
    assert items["editor"] == "config in user"
    # No locator readings under -g: no workspaces/projects/worktrees offered.
    assert "alpha" not in items
    assert "ws" not in items


def test_complete_scope_global_tier(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("")
    monkeypatch.setenv("WTM_CONFIG", str(cfg))
    from wtm.cli.completions.config import _complete_scope

    items = dict(_complete_scope(_ConfigCtx(**{"global": True}), ""))
    assert set(items) == {"user", "system"}
    assert items["user"] == "default"


# --- create ------------------------------------------------------------------


def test_create_new_branch_and_worktree(root: Path) -> None:
    _seed_repo(root, "alpha")
    res = run("create", "alpha", "feat", root=root)
    assert res.returncode == 0, res.stderr
    wt = root / "alpha" / "worktrees" / "feat"
    assert (wt / "file.txt").exists()
    branch = subprocess.run(
        ["git", "-C", str(wt), "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == "feat"


def test_create_nested_name(root: Path) -> None:
    _seed_repo(root, "alpha")
    res = run("create", "alpha", "feature/x", root=root)
    assert res.returncode == 0, res.stderr
    assert (root / "alpha" / "worktrees" / "feature" / "x" / "file.txt").exists()


def test_create_infers_project_from_cwd(root: Path) -> None:
    repo = _seed_repo(root, "alpha")
    res = run("create", "feat", root=root, cwd=repo)
    assert res.returncode == 0, res.stderr
    assert (root / "alpha" / "worktrees" / "feat").is_dir()


def test_create_orphan_bootstraps_empty_project(root: Path) -> None:
    # 'project new' yields an empty bare repo (no commits); the first worktree
    # must bootstrap onto a new orphan branch (git >= 2.43).
    assert run("project", "new", "alpha", root=root).returncode == 0
    res = run("create", "alpha", "main", root=root)
    assert res.returncode == 0, res.stderr
    assert "orphan" in res.stderr
    wt = root / "alpha" / "worktrees" / "main"
    assert wt.is_dir()
    branch = subprocess.run(
        ["git", "-C", str(wt), "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == "main"
    # The orphan branch is unborn: no commits yet.
    assert (
        subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
        ).returncode
        != 0
    )


def test_create_existing_local_branch(root: Path) -> None:
    repo = _seed_repo(root, "alpha")
    subprocess.run(["git", "-C", str(repo), "branch", "existing"], check=True)
    res = run("create", "alpha", "wt", "-b", "existing", root=root)
    assert res.returncode == 0, res.stderr
    assert "existing local branch 'existing'" in res.stderr


def test_create_rejects_traversal(root: Path) -> None:
    _seed_repo(root, "alpha")
    res = run("create", "alpha", "../escape", root=root)
    assert res.returncode == 1
    assert "may not contain '..'" in res.stderr


def _advance_origin(root: Path, project: str) -> str:
    """Add a commit to *project*'s origin source repo; return its short sha.

    :func:`_seed_repo` bare-clones from ``_sources/<project>``, so that source
    is the project's origin. Advancing it simulates an origin that moved ahead
    of the local bare repo's ``origin/*`` refs -- the state ``create -f`` must
    catch up to before forking a new branch.
    """
    git_env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(root / "_gitconfig"),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    source = root / "_sources" / project
    (source / "later.txt").write_text("later\n")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True, env=git_env)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "later"], check=True, env=git_env)
    sha = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        env=git_env,
    ).stdout.strip()

    return sha


def test_create_fetch_forks_from_latest_origin(root: Path) -> None:
    # -f fetches origin before resolution, so a new branch includes the commit
    # the origin gained after the clone; without -f it would fork from the stale
    # origin/HEAD and miss it.
    _seed_repo(root, "alpha")
    _advance_origin(root, "alpha")
    res = run("create", "alpha", "feat", "-f", root=root)
    assert res.returncode == 0, res.stderr
    assert "fetched origin" in res.stderr
    wt = root / "alpha" / "worktrees" / "feat"
    assert (wt / "later.txt").exists()


def test_create_without_fetch_forks_from_stale_origin(root: Path) -> None:
    # The mirror of the above: no -f means no catch-up, so the post-clone commit
    # is absent from the new branch.
    _seed_repo(root, "alpha")
    _advance_origin(root, "alpha")
    res = run("create", "alpha", "feat", root=root)
    assert res.returncode == 0, res.stderr
    assert "fetched origin" not in res.stderr
    assert not (root / "alpha" / "worktrees" / "feat" / "later.txt").exists()


def test_create_fetch_without_origin_warns_and_continues(root: Path) -> None:
    # An origin-less project (project new) with -f: the fetch is a no-op, warned,
    # and creation proceeds normally.
    assert run("project", "new", "alpha", root=root).returncode == 0
    res = run("create", "alpha", "main", "-f", root=root)
    assert res.returncode == 0, res.stderr
    assert "no origin remote; skipped fetch" in res.stderr
    assert (root / "alpha" / "worktrees" / "main").is_dir()


def test_create_fetch_failure_aborts_without_residue(root: Path) -> None:
    # A broken origin makes the fetch fail; -f must abort before any filesystem
    # mutation, leaving no worktree dir behind.
    _seed_repo(root, "alpha")
    git_dir = root / "alpha" / ".git"
    subprocess.run(
        ["git", "-C", str(git_dir), "remote", "set-url", "origin", "/no/such/repo"],
        check=True,
    )
    res = run("create", "alpha", "feat", "-f", root=root)
    assert res.returncode == 1
    assert not (root / "alpha" / "worktrees" / "feat").exists()


def test_create_cd_emits_to_shell_file(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    shell_file = tmp_path / "cd"
    shell_file.touch()
    res = run("create", "alpha", "feat", "-c", root=root, shell_file=shell_file)
    assert res.returncode == 0, res.stderr
    wt = root / "alpha" / "worktrees" / "feat"
    assert _shell_cd(shell_file) == str(wt)
    # A move into a worktree exports the full WTM_* session contract (nothing unset).
    exports = _shell_exports(shell_file)
    assert exports["WTM_WORKTREE_DIR"] == str(wt)
    assert exports["WTM_PROJECT"] == "alpha"
    assert exports["WTM_PROJECT_DIR"] == str(root / "alpha")
    assert exports["WTM_WORKSPACE"] == "ws"
    assert exports["WTM_WORKSPACE_DIR"] == str(root)
    assert set(exports) == set(MANAGED_ENV_VARS)
    assert _shell_unsets(shell_file) == set()


# --- enter as a locator (experiment) -----------------------------------------


def test_enter_single_name_resolves_worktree_globally(root: Path, tmp_path: Path) -> None:
    # A bare NAME finds its worktree without a PROJECT, from anywhere in the root.
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    shell_file = tmp_path / "cd"
    shell_file.touch()
    res = run("enter", "feat", root=root, cwd=root, shell_file=shell_file)
    assert res.returncode == 0, res.stderr
    assert _shell_cd(shell_file) == str(root / "alpha" / "worktrees" / "feat")


def test_enter_single_name_resolves_project(root: Path, tmp_path: Path) -> None:
    # No worktree named 'alpha' -> the project tier wins (project-enter contract).
    _seed_repo(root, "alpha")
    shell_file = tmp_path / "cd"
    shell_file.touch()
    res = run("enter", "alpha", root=root, cwd=root, shell_file=shell_file)
    assert res.returncode == 0, res.stderr
    assert _shell_cd(shell_file) == str(root / "alpha")
    exports = _shell_exports(shell_file)
    assert exports["WTM_PROJECT"] == "alpha"
    assert "WTM_WORKTREE_DIR" not in exports  # project tier, not worktree


def test_enter_ambiguous_worktree_lists_choices(root: Path) -> None:
    _seed_repo(root, "alpha")
    _seed_repo(root, "beta")
    run("create", "alpha", "shared", root=root)
    run("create", "beta", "shared", root=root)
    res = run("enter", "shared", root=root, cwd=root)
    assert res.returncode == 1
    assert "matches 2 worktrees" in res.stderr
    assert "wtm enter ws alpha shared" in res.stderr
    assert "wtm enter ws beta shared" in res.stderr


def test_enter_project_qualified_worktree(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    _seed_repo(root, "beta")
    run("create", "alpha", "shared", root=root)
    run("create", "beta", "shared", root=root)
    shell_file = tmp_path / "cd"
    shell_file.touch()
    res = run("enter", "beta", "shared", root=root, cwd=root, shell_file=shell_file)
    assert res.returncode == 0, res.stderr
    assert _shell_cd(shell_file) == str(root / "beta" / "worktrees" / "shared")


def test_enter_no_match_errors(root: Path) -> None:
    _seed_repo(root, "alpha")
    res = run("enter", "ghost", root=root, cwd=root)
    assert res.returncode == 1
    assert "no worktree, project, or workspace matches" in res.stderr


def test_enter_dir_arg_still_a_path(root: Path, tmp_path: Path) -> None:
    # A path-looking single arg keeps the classic worktree-path behavior.
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    shell_file = tmp_path / "cd"
    shell_file.touch()
    wt = root / "alpha" / "worktrees" / "feat"
    res = run("enter", ".", root=root, cwd=wt, shell_file=shell_file)
    assert res.returncode == 0, res.stderr
    assert _shell_cd(shell_file) == str(wt)


def test_enter_cross_workspace_worktree(tmp_path: Path) -> None:
    # The headline: a worktree in another workspace resolves by name alone,
    # from an unrelated active workspace (the shell-history-portability win).
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "proj")
    _run_multi("create", "proj", "onlywt", config=config, cwd=alpha, workspace="alpha")
    shell_file = tmp_path / "cd"
    shell_file.touch()
    # Active workspace is beta, but 'onlywt' lives in alpha -> still found.
    res = _run_multi(
        "enter", "onlywt", config=config, cwd=beta, workspace="beta", shell_file=shell_file
    )
    assert res.returncode == 0, res.stderr
    assert _shell_cd(shell_file) == str(alpha / "proj" / "worktrees" / "onlywt")


def test_enter_workspace_tier_cross_workspace(tmp_path: Path) -> None:
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    shell_file = tmp_path / "cd"
    shell_file.touch()
    res = _run_multi(
        "enter", "alpha", config=config, cwd=beta, workspace="beta", shell_file=shell_file
    )
    assert res.returncode == 0, res.stderr
    assert _shell_cd(shell_file) == str(alpha)


def test_enter_context_resolves_ambiguous(root: Path, tmp_path: Path) -> None:
    # 'shared' spans two projects (globally ambiguous), but standing in one of
    # them the current-context tiebreak enters that project's worktree silently.
    _seed_repo(root, "alpha")
    _seed_repo(root, "beta")
    run("create", "alpha", "shared", root=root)
    run("create", "beta", "shared", root=root)
    shell_file = tmp_path / "cd"
    shell_file.touch()
    res = run("enter", "shared", root=root, cwd=root / "beta", shell_file=shell_file)
    assert res.returncode == 0, res.stderr
    assert _shell_cd(shell_file) == str(root / "beta" / "worktrees" / "shared")
    assert "matches" not in res.stderr


def test_enter_ambiguous_still_errors_out_of_context(root: Path) -> None:
    # Out of any matching project (at the workspace root, one workspace), the
    # context cannot single out a candidate -> the ambiguity error still fires.
    _seed_repo(root, "alpha")
    _seed_repo(root, "beta")
    run("create", "alpha", "shared", root=root)
    run("create", "beta", "shared", root=root)
    res = run("enter", "shared", root=root, cwd=root)
    assert res.returncode == 1
    assert "matches 2 worktrees" in res.stderr


# --- resolve -----------------------------------------------------------------


def test_resolve_prints_path(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    res = run("resolve", "alpha", "feat", root=root)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == str(root / "alpha" / "worktrees" / "feat")


def test_resolve_nonexistent_worktree_errors(root: Path) -> None:
    _seed_repo(root, "alpha")
    res = run("resolve", "alpha", "nowt", root=root)
    assert res.returncode == 1
    assert "no worktree, project, or workspace matches" in res.stderr


def test_resolve_rejects_path(root: Path) -> None:
    res = run("resolve", "./feat", root=root)
    assert res.returncode == 1
    assert "not a path" in res.stderr


def test_resolve_env_emits_exports(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    res = run("resolve", "alpha", "feat", "--env", root=root)
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()
    assert any(line.startswith("export WTM_WORKTREE_DIR='") for line in lines)
    assert any(line.startswith("export WTM_PROJECT='alpha'") for line in lines)
    assert any(line.startswith("export WTM_BRANCH='feat'") for line in lines)
    assert any(line.startswith("export WTM_GIT_COMMON_DIR='") for line in lines)
    # WTM_REPO_DIR was dropped from the contract.
    assert not any("WTM_REPO_DIR" in line for line in lines)


def test_resolve_env_quote_escaping(root: Path) -> None:
    # A project name with a single quote must be escaped for eval safety.
    _seed_repo(root, "o'brien")
    run("create", "o'brien", "feat", root=root)
    res = run("resolve", "o'brien", "feat", "--env", root=root)
    assert res.returncode == 0, res.stderr
    assert "export WTM_PROJECT='o'\\''brien'" in res.stdout


def test_resolve_no_arg_uses_cwd_worktree(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    wt = root / "alpha" / "worktrees" / "feat"
    res = run("resolve", root=root, cwd=wt)  # no target -> the cwd worktree
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == str(wt)


def test_resolve_no_arg_from_subdir_gives_worktree_root(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    wt = root / "alpha" / "worktrees" / "feat"
    sub = wt / "sub" / "dir"
    sub.mkdir(parents=True)
    res = run("resolve", root=root, cwd=sub)  # from a subdir -> worktree toplevel
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == str(wt)


def test_resolve_no_arg_outside_worktree_errors(root: Path, tmp_path: Path) -> None:
    res = run("resolve", root=root, cwd=tmp_path)  # not inside a worktree
    assert res.returncode == 1
    assert "not inside a worktree" in res.stderr


def test_resolve_no_arg_env_emits_full_contract(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    wt = root / "alpha" / "worktrees" / "feat"
    res = run("resolve", "--env", root=root, cwd=wt)  # no target, cwd worktree
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()
    assert any(line.startswith("export WTM_WORKTREE_DIR='") for line in lines)
    assert any(line.startswith("export WTM_BRANCH='feat'") for line in lines)
    assert any(line.startswith("export WTM_PROJECT='alpha'") for line in lines)


def test_resolve_single_name_resolves_project(root: Path) -> None:
    # No worktree named 'alpha' -> the project tier wins (project path).
    _seed_repo(root, "alpha")
    res = run("resolve", "alpha", root=root, cwd=root)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == str(root / "alpha")


def test_resolve_single_name_resolves_project_env(root: Path) -> None:
    # The project tier's --env emits the project-level contract (no
    # WTM_WORKTREE_DIR / WTM_BRANCH), by path math.
    _seed_repo(root, "alpha")
    res = run("resolve", "alpha", "--env", root=root, cwd=root)
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()
    assert any(line.startswith("export WTM_PROJECT='alpha'") for line in lines)
    assert any(line.startswith("export WTM_PROJECT_DIR='") for line in lines)
    assert not any("WTM_WORKTREE_DIR" in line for line in lines)
    assert not any("WTM_BRANCH" in line for line in lines)


def test_resolve_workspace_tier_cross_workspace(tmp_path: Path) -> None:
    # Neither a worktree nor a project named 'alpha' -> the workspace tier.
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    res = _run_multi("resolve", "alpha", config=config, cwd=beta, workspace="beta")
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == str(alpha)


def test_resolve_workspace_tier_env(tmp_path: Path) -> None:
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    res = _run_multi("resolve", "alpha", "--env", config=config, cwd=beta, workspace="beta")
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()
    assert any(line.startswith("export WTM_WORKSPACE='alpha'") for line in lines)
    assert any(line.startswith("export WTM_WORKSPACE_DIR='") for line in lines)
    assert not any("WTM_PROJECT" in line for line in lines)
    assert not any("WTM_WORKTREE_DIR" in line for line in lines)


# --- project resolve ---------------------------------------------------------


def test_project_resolve_prints_dir_without_git(root: Path) -> None:
    # Pure path math: no project need exist on disk.
    res = run("project", "resolve", "alpha", root=root)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == str(root / "alpha")


def test_project_resolve_infers_project_from_cwd(root: Path) -> None:
    repo = _seed_repo(root, "alpha")
    res = run("project", "resolve", root=root, cwd=repo)  # no PROJECT arg
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == str(root / "alpha")


def test_project_resolve_outside_root_errors(root: Path, tmp_path: Path) -> None:
    res = run("project", "resolve", root=root, cwd=tmp_path)  # cwd not under root
    assert res.returncode == 1
    assert "not under" in res.stderr


def test_project_resolve_env_emits_project_subset(root: Path) -> None:
    res = run("project", "resolve", "alpha", "--env", root=root)
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()
    assert any(line.startswith("export WTM_PROJECT='alpha'") for line in lines)
    assert any(line.startswith("export WTM_PROJECT_DIR='") for line in lines)
    assert any(line.startswith("export WTM_WORKTREES_DIR='") for line in lines)
    assert any(line.startswith("export WTM_GIT_COMMON_DIR='") for line in lines)
    assert any(line.startswith("export WTM_WORKSPACE='ws'") for line in lines)
    assert any(line.startswith("export WTM_WORKSPACE_DIR='") for line in lines)
    # A project has no single worktree/branch: those keys must be absent.
    assert not any("WTM_WORKTREE_DIR" in line for line in lines)
    assert not any("WTM_BRANCH" in line for line in lines)


def test_project_resolve_completion_emits_projects(root: Path) -> None:
    _seed_repo(root, "alpha")
    _seed_repo(root, "beta")
    (root / "stray").mkdir()  # no .git/, must be excluded
    out = _complete(root, words="wtm project resolve ", cword="3")
    # Slot 0 of [[WORKSPACE] PROJECT] unions the workspace ('ws') with projects.
    assert out.split() == ["alpha", "beta", "ws"]


# --- delete ------------------------------------------------------------------


def test_delete_requires_target(root: Path) -> None:
    _seed_repo(root, "alpha")
    wt = root / "alpha" / "worktrees" / "feat"
    run("create", "alpha", "feat", root=root)
    res = run("delete", root=root, cwd=wt)  # no explicit target
    assert res.returncode == 1
    assert "a target is required" in res.stderr


def test_delete_removes_worktree(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    wt = root / "alpha" / "worktrees" / "feat"
    assert wt.is_dir()
    res = run("delete", "alpha", "feat", root=root)
    assert res.returncode == 0, res.stderr
    assert not wt.exists()


def test_delete_prunes_nested_parents(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feature/x", root=root)
    res = run("delete", "alpha", "feature/x", root=root)
    assert res.returncode == 0, res.stderr
    # The intermediate worktrees/feature dir should be pruned.
    assert not (root / "alpha" / "worktrees" / "feature").exists()
    # But worktrees/ itself must survive.
    assert (root / "alpha" / "worktrees").is_dir()


def test_delete_branch_with_D(root: Path) -> None:
    repo = _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    run("delete", "alpha", "feat", "-D", root=root)
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--format=%(refname:short)"],
        capture_output=True,
        text=True,
    ).stdout.split()
    assert "feat" not in branches


# --- delete: rescue the stranded shell ---------------------------------------


def test_delete_cds_to_oldpwd_when_standing_in_worktree(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    wt = root / "alpha" / "worktrees" / "feat"
    prev = tmp_path / "prev"
    prev.mkdir()
    shell_file = tmp_path / "cd"
    shell_file.touch()
    # Shell stands in the deleted worktree: run as a real subprocess so the
    # doomed cwd cannot strand the test runner.
    res = run_proc(
        "delete",
        "alpha",
        "feat",
        root=root,
        cwd=wt,
        shell_file=shell_file,
        extra_env={"OLDPWD": str(prev)},
    )
    assert res.returncode == 0, res.stderr
    assert _shell_cd(shell_file) == str(prev)


def test_delete_cds_to_project_dir_when_oldpwd_gone(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    wt = root / "alpha" / "worktrees" / "feat"
    shell_file = tmp_path / "cd"
    shell_file.touch()
    res = run_proc(
        "delete",
        "alpha",
        "feat",
        root=root,
        cwd=wt,
        shell_file=shell_file,
        extra_env={"OLDPWD": str(tmp_path / "does-not-exist")},
    )
    assert res.returncode == 0, res.stderr
    assert _shell_cd(shell_file) == str(root / "alpha")


def test_delete_cds_from_subdir_of_worktree(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    wt = root / "alpha" / "worktrees" / "feat"
    sub = wt / "sub"
    sub.mkdir()
    prev = tmp_path / "prev"
    prev.mkdir()
    shell_file = tmp_path / "cd"
    shell_file.touch()
    res = run_proc(
        "delete",
        "alpha",
        "feat",
        root=root,
        cwd=sub,
        shell_file=shell_file,
        extra_env={"OLDPWD": str(prev)},
    )
    assert res.returncode == 0, res.stderr
    assert _shell_cd(shell_file) == str(prev)


def test_delete_no_cd_when_outside_deleted_worktree(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    run("create", "alpha", "other", root=root)
    other = root / "alpha" / "worktrees" / "other"
    prev = tmp_path / "prev"
    prev.mkdir()
    shell_file = tmp_path / "cd"
    shell_file.touch()
    # Standing in a different worktree: deleting 'feat' must not move the shell.
    res = run(
        "delete",
        "alpha",
        "feat",
        root=root,
        cwd=other,
        shell_file=shell_file,
        extra_env={"OLDPWD": str(prev)},
    )
    assert res.returncode == 0, res.stderr
    assert shell_file.read_text() == ""


def test_delete_silent_without_shell_wrapper(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    wt = root / "alpha" / "worktrees" / "feat"
    prev = tmp_path / "prev"
    prev.mkdir()
    # No WTM_SHELL_FILE (shell_file unset): no shell channel, so stay silent on stdout.
    res = run_proc("delete", "alpha", "feat", root=root, cwd=wt, extra_env={"OLDPWD": str(prev)})
    assert res.returncode == 0, res.stderr
    assert res.stdout == ""
    assert "cd manually" not in res.stderr


# --- core.rescue_dir (unit) --------------------------------------------------


def test_rescue_dir_prefers_existing_oldpwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtm.core import rescue_dir

    wt = tmp_path / "wt"
    wt.mkdir()
    prev = tmp_path / "prev"
    prev.mkdir()
    project = tmp_path / "project"
    monkeypatch.setenv("OLDPWD", str(prev))
    # cwd is the worktree (or below it) -> rescue to the existing $OLDPWD.
    assert rescue_dir(wt, wt, project) == prev
    assert rescue_dir(wt / "sub", wt, project) == prev


def test_rescue_dir_falls_back_to_project_when_oldpwd_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtm.core import rescue_dir

    wt = tmp_path / "wt"
    wt.mkdir()
    project = tmp_path / "project"
    # $OLDPWD missing -> project dir; $OLDPWD pointing to a gone dir -> project dir.
    monkeypatch.delenv("OLDPWD", raising=False)
    assert rescue_dir(wt, wt, project) == project
    monkeypatch.setenv("OLDPWD", str(tmp_path / "gone"))
    assert rescue_dir(wt, wt, project) == project


def test_rescue_dir_none_when_outside_or_no_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtm.core import rescue_dir

    wt = tmp_path / "wt"
    wt.mkdir()
    project = tmp_path / "project"
    monkeypatch.setenv("OLDPWD", str(tmp_path))
    # cwd outside the deleted worktree, or unavailable -> no rescue.
    assert rescue_dir(tmp_path / "elsewhere", wt, project) is None
    assert rescue_dir(None, wt, project) is None


# --- core.current_dir / abspath under a deleted cwd (unit) -------------------


def test_current_dir_none_when_getcwd_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from wtm.core import current_dir

    def _boom() -> str:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr("wtm.core.os.getcwd", _boom)
    assert current_dir() is None
    assert current_dir(resolve=False) is None


def test_current_dir_resolve_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from wtm.core import current_dir

    monkeypatch.setattr("wtm.core.os.getcwd", lambda: "/some/path")
    monkeypatch.setattr("wtm.core.os.path.realpath", lambda _p: "/real/path")
    assert current_dir(resolve=False) == Path("/some/path")
    assert current_dir() == Path("/real/path")


def test_abspath_relative_arg_deleted_cwd_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from wtm.core import WtmError, abspath

    def _boom() -> str:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr("wtm.core.os.getcwd", _boom)
    # An absolute arg never touches the cwd, so it still works.
    assert abspath("/already/absolute") == Path("/already/absolute")
    # A relative arg must read the (gone) cwd -> clear WtmError, not a traceback.
    with pytest.raises(WtmError, match="cwd is unavailable"):
        abspath("relative/path")


# --- rename ------------------------------------------------------------------


def _branch(wt: Path) -> str:
    """Return the short branch name checked out in worktree *wt*."""
    return subprocess.run(
        ["git", "-C", str(wt), "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_rename_moves_worktree_and_leaves_branch(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    old = root / "alpha" / "worktrees" / "feat"
    new = root / "alpha" / "worktrees" / "fix"
    res = run("rename", "alpha", "feat", "fix", root=root)
    assert res.returncode == 0, res.stderr
    assert not old.exists()
    assert (new / "file.txt").exists()
    # Without -b the branch is untouched: still 'feat', now living at worktrees/fix.
    assert _branch(new) == "feat"
    assert "renamed worktree 'feat' -> 'fix'" in res.stderr


def test_rename_two_arg_form_infers_project_from_cwd(root: Path) -> None:
    repo = _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    res = run("rename", "feat", "fix", root=root, cwd=repo)
    assert res.returncode == 0, res.stderr
    assert (root / "alpha" / "worktrees" / "fix").is_dir()
    assert not (root / "alpha" / "worktrees" / "feat").exists()


def test_rename_one_arg_form_renames_cwd_worktree(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    wt = root / "alpha" / "worktrees" / "feat"
    res = run("rename", "fix", root=root, cwd=wt)
    assert res.returncode == 0, res.stderr
    assert (root / "alpha" / "worktrees" / "fix").is_dir()
    assert not wt.exists()


def test_rename_nested_newname_creates_parents(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    res = run("rename", "alpha", "feat", "feature/x", root=root)
    assert res.returncode == 0, res.stderr
    assert (root / "alpha" / "worktrees" / "feature" / "x" / "file.txt").exists()


def test_rename_nested_source_prunes_old_parents(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feature/x", root=root)
    res = run("rename", "alpha", "feature/x", "flat", root=root)
    assert res.returncode == 0, res.stderr
    assert (root / "alpha" / "worktrees" / "flat").is_dir()
    # The now-empty intermediate worktrees/feature dir should be pruned.
    assert not (root / "alpha" / "worktrees" / "feature").exists()
    assert (root / "alpha" / "worktrees").is_dir()


def test_rename_branch_flag_renames_branch(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    new = root / "alpha" / "worktrees" / "fix"
    res = run("rename", "alpha", "feat", "fix", "-b", root=root)
    assert res.returncode == 0, res.stderr
    assert _branch(new) == "fix"
    assert "renamed branch 'feat' -> 'fix'" in res.stderr


def test_rename_branch_flag_on_detached_errors(root: Path) -> None:
    repo = _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    wt = root / "alpha" / "worktrees" / "feat"
    # Detach HEAD in the worktree so there is no branch to rename.
    subprocess.run(["git", "-C", str(wt), "checkout", "--detach"], check=True, capture_output=True)
    res = run("rename", "alpha", "feat", "fix", "-b", root=root)
    assert res.returncode == 1
    assert "detached" in res.stderr
    # The move must not have happened (validation precedes it).
    assert wt.exists()
    assert repo.is_dir()


def test_rename_destination_exists_is_hard_error(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    run("create", "alpha", "fix", root=root)
    res = run("rename", "alpha", "feat", "fix", root=root)
    assert res.returncode == 1
    assert "destination already exists" in res.stderr
    # Source is untouched.
    assert (root / "alpha" / "worktrees" / "feat").is_dir()


def test_rename_branch_collision_is_hard_error(root: Path) -> None:
    repo = _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    # A branch named 'fix' already exists (not checked out anywhere).
    subprocess.run(["git", "-C", str(repo), "branch", "fix"], check=True)
    res = run("rename", "alpha", "feat", "fix", "-b", root=root)
    assert res.returncode == 1
    assert "branch 'fix' already exists" in res.stderr


def test_rename_dirty_source_moves_without_force(root: Path) -> None:
    # git worktree move carries a dirty worktree without any force flag; the
    # uncommitted change rides along to the new location.
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    wt = root / "alpha" / "worktrees" / "feat"
    (wt / "file.txt").write_text("dirty\n")  # uncommitted change
    res = run("rename", "alpha", "feat", "fix", root=root)
    assert res.returncode == 0, res.stderr
    new = root / "alpha" / "worktrees" / "fix"
    assert new.is_dir()
    assert (new / "file.txt").read_text() == "dirty\n"


def test_rename_locked_source_refused(root: Path) -> None:
    # A locked worktree is refused by git (it would need --force twice); wtm has
    # no force flag, so the user must 'git worktree unlock' first.
    repo = _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    wt = root / "alpha" / "worktrees" / "feat"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "lock", str(wt)], check=True, capture_output=True
    )
    res = run("rename", "alpha", "feat", "fix", root=root)
    assert res.returncode == 1
    assert wt.exists()  # refused, still in place


def test_rename_rejects_path_newname(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    res = run("rename", "alpha", "feat", "./fix", root=root)
    assert res.returncode == 1
    assert "not a path" in res.stderr


def test_rename_rejects_traversal_newname(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    # A non-path NAME that still smuggles '..' inside a nested component.
    res = run("rename", "alpha", "feat", "foo/../escape", root=root)
    assert res.returncode == 1
    assert "may not contain '..'" in res.stderr


# --- rename: follow the moved shell ------------------------------------------


def test_rename_cds_to_mapped_path_when_standing_in_worktree(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    wt = root / "alpha" / "worktrees" / "feat"
    shell_file = tmp_path / "cd"
    shell_file.touch()
    res = run_proc("rename", "alpha", "feat", "fix", root=root, cwd=wt, shell_file=shell_file)
    assert res.returncode == 0, res.stderr
    assert _shell_cd(shell_file) == str(root / "alpha" / "worktrees" / "fix")


def test_rename_cds_to_mapped_subpath_from_subdir(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    wt = root / "alpha" / "worktrees" / "feat"
    sub = wt / "sub"
    sub.mkdir()
    shell_file = tmp_path / "cd"
    shell_file.touch()
    res = run_proc("rename", "alpha", "feat", "fix", root=root, cwd=sub, shell_file=shell_file)
    assert res.returncode == 0, res.stderr
    assert _shell_cd(shell_file) == str(root / "alpha" / "worktrees" / "fix" / "sub")


def test_rename_no_cd_when_outside_renamed_worktree(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    run("create", "alpha", "other", root=root)
    other = root / "alpha" / "worktrees" / "other"
    shell_file = tmp_path / "cd"
    shell_file.touch()
    # Standing in a different worktree: renaming 'feat' must not move the shell.
    res = run("rename", "alpha", "feat", "fix", root=root, cwd=other, shell_file=shell_file)
    assert res.returncode == 0, res.stderr
    assert shell_file.read_text() == ""


# --- rename: the rename event ------------------------------------------------


def test_rename_event_fires_after_move_with_old_and_new(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    hooks = tmp_path / "hooks"
    out = tmp_path / "ran"
    _write_hook(
        hooks / "wtm-post-rename",
        'printf "%s|%s|%s|%s|%s\\n" "$WTM_EVENT" "$WTM_WORKTREE_DIR" "$WTM_OLD_DIR"'
        f' "$WTM_BRANCH" "$WTM_OLD_BRANCH" > "{out}"\n',
    )
    res = run("rename", "alpha", "feat", "fix", "-b", root=root, hooks_dir=hooks)
    assert res.returncode == 0, res.stderr
    event, dir_, old_dir, branch, old_branch = out.read_text().strip().split("|")
    assert event == "post-rename"
    assert dir_ == str(root / "alpha" / "worktrees" / "fix")  # fires from the new dir
    assert old_dir == str(root / "alpha" / "worktrees" / "feat")
    assert branch == "fix"
    assert old_branch == "feat"


def test_rename_event_old_branch_equals_new_when_unchanged(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    hooks = tmp_path / "hooks"
    out = tmp_path / "ran"
    # No -b: branch unchanged, so WTM_OLD_BRANCH == WTM_BRANCH == 'feat'.
    body = f'printf "%s|%s" "$WTM_BRANCH" "$WTM_OLD_BRANCH" > "{out}"\n'
    _write_hook(hooks / "wtm-post-rename", body)
    res = run("rename", "alpha", "feat", "fix", root=root, hooks_dir=hooks)
    assert res.returncode == 0, res.stderr
    assert out.read_text().strip() == "feat|feat"


def test_rename_event_setup_scope_order(root: Path, tmp_path: Path) -> None:
    repo = _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    system = tmp_path / "system-hooks"
    hooks = tmp_path / "hooks"
    workspace = tmp_path / "workspace-hooks"
    order = tmp_path / "order"
    wt = root / "alpha" / "worktrees" / "feat"
    # post-rename is a setup event: fires system -> user -> workspace -> project -> worktree.
    _write_hook(system / "wtm-post-rename", f'echo system >> "{order}"\n')
    _write_hook(hooks / "wtm-post-rename", f'echo user >> "{order}"\n')
    _write_hook(workspace / "wtm-post-rename", f'echo workspace >> "{order}"\n')
    _write_hook(repo / "hooks" / "wtm-post-rename", f'echo project >> "{order}"\n')
    _write_hook(wt / ".wtm" / "hooks" / "wtm-post-rename", f'echo worktree >> "{order}"\n')
    res = run(
        "rename",
        "alpha",
        "feat",
        "fix",
        root=root,
        hooks_dir=hooks,
        system_hooks_dir=system,
        workspace_hooks_dir=workspace,
    )
    assert res.returncode == 0, res.stderr
    assert order.read_text().split() == ["system", "user", "workspace", "project", "worktree"]


# --- core.remap_dir (unit) ---------------------------------------------------


def test_remap_dir_maps_subpath(tmp_path: Path) -> None:
    from wtm.core import remap_dir

    old = tmp_path / "old"
    old.mkdir()
    new = tmp_path / "new"
    # cwd is the worktree itself, or a subdir -> mapped onto the new location.
    assert remap_dir(old, old, new) == new
    assert remap_dir(old / "src" / "pkg", old, new) == new / "src" / "pkg"


def test_remap_dir_none_when_outside_or_no_cwd(tmp_path: Path) -> None:
    from wtm.core import remap_dir

    old = tmp_path / "old"
    old.mkdir()
    new = tmp_path / "new"
    assert remap_dir(tmp_path / "elsewhere", old, new) is None
    assert remap_dir(None, old, new) is None


# --- hooks -------------------------------------------------------------------


def _write_hook(path: Path, body: str, *, executable: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env sh\n" + body)
    if executable:
        path.chmod(0o755)


def test_create_hook_fires_with_contract(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    hooks = tmp_path / "hooks"
    out = tmp_path / "hook.out"
    _write_hook(
        hooks / "wtm-post-create",
        'printf "%s|%s|%s|%s|%s\\n" "$WTM_EVENT" "$1" "$WTM_PROJECT" "$WTM_BRANCH"'
        f' "$WTM_WORKSPACE_DIR" > "{out}"\n',
    )
    res = run("hook", "run", "alpha", "feat", "post-create", root=root, hooks_dir=hooks)
    assert res.returncode == 0, res.stderr
    event, dir_, project, branch, workspace_dir = out.read_text().strip().split("|")
    assert event == "post-create"
    assert dir_ == str(root / "alpha" / "worktrees" / "feat")
    assert project == "alpha"
    assert branch == "feat"
    assert workspace_dir == str(root)


def test_hook_ordering_file_before_dir(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    hooks = tmp_path / "hooks"
    order = tmp_path / "order"
    _write_hook(hooks / "wtm-post-create", f'echo file >> "{order}"\n')
    _write_hook(hooks / "wtm-post-create.d" / "20-b", f'echo b >> "{order}"\n')
    _write_hook(hooks / "wtm-post-create.d" / "10-a", f'echo a >> "{order}"\n')
    res = run("hook", "run", "alpha", "feat", "post-create", root=root, hooks_dir=hooks)
    assert res.returncode == 0, res.stderr
    assert order.read_text().split() == ["file", "a", "b"]


def test_hook_failure_nonfatal_without_strict(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    hooks = tmp_path / "hooks"
    _write_hook(hooks / "wtm-post-create", "exit 3\n")
    res = run("hook", "run", "alpha", "feat", "post-create", root=root, hooks_dir=hooks)
    assert res.returncode == 0
    assert "hook failed" in res.stderr


def test_hook_failure_fatal_with_strict(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    hooks = tmp_path / "hooks"
    _write_hook(hooks / "wtm-post-create", "exit 3\n")
    res = run(
        "hook", "run", "alpha", "feat", "post-create", "--strict", root=root, hooks_dir=hooks
    )
    assert res.returncode == 1
    assert "hook failed" in res.stderr


def test_non_executable_hook_fatal_under_strict(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    hooks = tmp_path / "hooks"
    _write_hook(hooks / "wtm-post-create", "true\n", executable=False)
    res = run(
        "hook", "run", "alpha", "feat", "post-create", "--strict", root=root, hooks_dir=hooks
    )
    assert res.returncode == 1
    assert "not executable" in res.stderr


def test_init_command_removed(root: Path) -> None:
    # 'wtm init' is gone; running the create event's hooks is 'wtm hook run post-create'.
    res = run("init", "alpha", "feat", root=root)
    assert res.returncode == 2


def test_delete_hook_teardown_order(root: Path, tmp_path: Path) -> None:
    repo = _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    system = tmp_path / "system-hooks"
    hooks = tmp_path / "hooks"
    workspace = tmp_path / "workspace-hooks"
    order = tmp_path / "order"
    wt = root / "alpha" / "worktrees" / "feat"
    # One pre-delete hook per scope; teardown fires worktree -> project ->
    # workspace -> user -> system (most specific first).
    _write_hook(system / "wtm-pre-delete", f'echo system >> "{order}"\n')
    _write_hook(hooks / "wtm-pre-delete", f'echo user >> "{order}"\n')
    _write_hook(workspace / "wtm-pre-delete", f'echo workspace >> "{order}"\n')
    _write_hook(repo / "hooks" / "wtm-pre-delete", f'echo project >> "{order}"\n')
    _write_hook(wt / ".wtm" / "hooks" / "wtm-pre-delete", f'echo worktree >> "{order}"\n')
    res = run(
        "delete",
        "alpha",
        "feat",
        "-f",
        root=root,
        hooks_dir=hooks,
        system_hooks_dir=system,
        workspace_hooks_dir=workspace,
    )
    assert res.returncode == 0, res.stderr
    assert order.read_text().split() == ["worktree", "project", "workspace", "user", "system"]


def test_setup_hook_order(root: Path, tmp_path: Path) -> None:
    repo = _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    system = tmp_path / "system-hooks"
    hooks = tmp_path / "hooks"
    workspace = tmp_path / "workspace-hooks"
    order = tmp_path / "order"
    wt = root / "alpha" / "worktrees" / "feat"
    # Setup fires system -> user -> workspace -> project -> worktree (most general first).
    _write_hook(system / "wtm-post-create", f'echo system >> "{order}"\n')
    _write_hook(hooks / "wtm-post-create", f'echo user >> "{order}"\n')
    _write_hook(workspace / "wtm-post-create", f'echo workspace >> "{order}"\n')
    _write_hook(repo / "hooks" / "wtm-post-create", f'echo project >> "{order}"\n')
    _write_hook(wt / ".wtm" / "hooks" / "wtm-post-create", f'echo worktree >> "{order}"\n')
    res = run(
        "hook",
        "run",
        "alpha",
        "feat",
        "post-create",
        root=root,
        hooks_dir=hooks,
        system_hooks_dir=system,
        workspace_hooks_dir=workspace,
    )
    assert res.returncode == 0, res.stderr
    assert order.read_text().split() == ["system", "user", "workspace", "project", "worktree"]


def test_delete_hook_runs_before_worktree_removal(root: Path, tmp_path: Path) -> None:
    # A per-worktree pre-delete hook must see the live worktree: the pre-delete
    # event fires before 'git worktree remove'.
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    wt = root / "alpha" / "worktrees" / "feat"
    out = tmp_path / "alive"
    _write_hook(
        wt / ".wtm" / "hooks" / "wtm-pre-delete",
        f'test -f "$WTM_WORKTREE_DIR/file.txt" && echo alive > "{out}"\n',
    )
    res = run("delete", "alpha", "feat", "-f", root=root)
    assert res.returncode == 0, res.stderr
    assert out.read_text().strip() == "alive"
    assert not wt.exists()


# --- pre/post event pairs ----------------------------------------------------


def test_post_delete_fires_after_worktree_removal(root: Path, tmp_path: Path) -> None:
    # The post-delete event fires after 'git worktree remove', so the worktree
    # is already gone when the hook observes it (the inverse of pre-delete).
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    wt = root / "alpha" / "worktrees" / "feat"
    hooks = tmp_path / "hooks"
    out = tmp_path / "gone"
    _write_hook(
        hooks / "wtm-post-delete",
        f'test ! -e "$WTM_WORKTREE_DIR" && echo gone > "{out}"\n',
    )
    res = run("delete", "alpha", "feat", "-f", root=root, hooks_dir=hooks)
    assert res.returncode == 0, res.stderr
    assert out.read_text().strip() == "gone"
    assert not wt.exists()


def test_post_delete_runs_from_project_dir(root: Path, tmp_path: Path) -> None:
    # post-delete's cwd is the project dir (the worktree dir is gone), so the
    # subprocess must not fail with a missing cwd.
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    hooks = tmp_path / "hooks"
    out = tmp_path / "pwd"
    _write_hook(hooks / "wtm-post-delete", f'pwd -P > "{out}"\n')
    res = run("delete", "alpha", "feat", "-f", root=root, hooks_dir=hooks)
    assert res.returncode == 0, res.stderr
    assert Path(out.read_text().strip()).resolve() == (root / "alpha").resolve()


def test_pre_create_fires_before_creation(root: Path, tmp_path: Path) -> None:
    # pre-create fires before the worktree exists: WTM_WORKTREE_DIR is not yet a
    # directory, and the hook runs from the project dir.
    _seed_repo(root, "alpha")
    hooks = tmp_path / "hooks"
    out = tmp_path / "seen"
    _write_hook(
        hooks / "wtm-pre-create",
        f'printf "%s|%s|%s\\n" "$WTM_EVENT" '
        f'"$(test -e "$WTM_WORKTREE_DIR" && echo exists || echo absent)" '
        f'"$(pwd -P)" > "{out}"\n',
    )
    res = run("create", "alpha", "feat", root=root, hooks_dir=hooks)
    assert res.returncode == 0, res.stderr
    event, presence, cwd = out.read_text().strip().split("|")
    assert event == "pre-create"
    assert presence == "absent"
    assert Path(cwd).resolve() == (root / "alpha").resolve()


def test_pre_create_veto_under_strict_leaves_no_dir(root: Path, tmp_path: Path) -> None:
    # A failing pre-create hook aborts creation under --strict, before any
    # filesystem mutation: no worktree dir, no stray parent.
    _seed_repo(root, "alpha")
    hooks = tmp_path / "hooks"
    _write_hook(hooks / "wtm-pre-create", "exit 1\n")
    res = run("create", "alpha", "feature/x", "--strict", root=root, hooks_dir=hooks)
    assert res.returncode == 1
    assert not (root / "alpha" / "worktrees" / "feature").exists()


def test_pre_create_failure_nonfatal_without_strict(root: Path, tmp_path: Path) -> None:
    # Without --strict a failing pre-create is advisory: creation proceeds.
    _seed_repo(root, "alpha")
    hooks = tmp_path / "hooks"
    _write_hook(hooks / "wtm-pre-create", "exit 1\n")
    res = run("create", "alpha", "feat", root=root, hooks_dir=hooks)
    assert res.returncode == 0, res.stderr
    assert (root / "alpha" / "worktrees" / "feat" / "file.txt").exists()


def test_pre_rename_fires_before_move_with_new_env(root: Path, tmp_path: Path) -> None:
    # pre-rename fires before the move: the old dir still exists and the
    # forward-looking WTM_NEW_DIR/WTM_NEW_BRANCH point at the destination.
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    old = root / "alpha" / "worktrees" / "feat"
    new = root / "alpha" / "worktrees" / "fix"
    hooks = tmp_path / "hooks"
    out = tmp_path / "seen"
    _write_hook(
        hooks / "wtm-pre-rename",
        f'printf "%s|%s|%s|%s|%s\\n" "$WTM_EVENT" "$WTM_WORKTREE_DIR" "$WTM_NEW_DIR"'
        f' "$WTM_BRANCH" "$WTM_NEW_BRANCH" > "{out}"\n',
    )
    res = run("rename", "alpha", "feat", "fix", "-b", root=root, hooks_dir=hooks)
    assert res.returncode == 0, res.stderr
    event, dir_, new_dir, branch, new_branch = out.read_text().strip().split("|")
    assert event == "pre-rename"
    assert dir_ == str(old)  # fires from the still-present old dir
    assert new_dir == str(new)
    assert branch == "feat"
    assert new_branch == "fix"


def test_pre_rename_veto_under_strict_aborts_move(root: Path, tmp_path: Path) -> None:
    # A failing pre-rename under --strict aborts before the move: old dir intact.
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    old = root / "alpha" / "worktrees" / "feat"
    hooks = tmp_path / "hooks"
    _write_hook(hooks / "wtm-pre-rename", "exit 1\n")
    res = run("rename", "alpha", "feat", "fix", "--strict", root=root, hooks_dir=hooks)
    assert res.returncode == 1
    assert old.exists()
    assert not (root / "alpha" / "worktrees" / "fix").exists()


def test_post_delete_teardown_scope_order(root: Path, tmp_path: Path) -> None:
    # post-delete is a teardown event: fires most-specific-first. The worktree
    # scope is unavailable (worktree removed), so the order is
    # project -> workspace -> user -> system.
    repo = _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    system = tmp_path / "system-hooks"
    hooks = tmp_path / "hooks"
    workspace = tmp_path / "workspace-hooks"
    order = tmp_path / "order"
    _write_hook(system / "wtm-post-delete", f'echo system >> "{order}"\n')
    _write_hook(hooks / "wtm-post-delete", f'echo user >> "{order}"\n')
    _write_hook(workspace / "wtm-post-delete", f'echo workspace >> "{order}"\n')
    _write_hook(repo / "hooks" / "wtm-post-delete", f'echo project >> "{order}"\n')
    res = run(
        "delete",
        "alpha",
        "feat",
        "-f",
        root=root,
        hooks_dir=hooks,
        system_hooks_dir=system,
        workspace_hooks_dir=workspace,
    )
    assert res.returncode == 0, res.stderr
    assert order.read_text().split() == ["project", "workspace", "user", "system"]


# --- hook run scope filter ---------------------------------------------------


def test_hook_run_scope_filters_to_one_scope(root: Path, tmp_path: Path) -> None:
    repo = _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    hooks = tmp_path / "hooks"
    ran = tmp_path / "ran"
    _write_hook(hooks / "wtm-post-create", f'echo user >> "{ran}"\n')
    _write_hook(repo / "hooks" / "wtm-post-create", f'echo project >> "{ran}"\n')
    res = run(
        "hook",
        "run",
        "alpha",
        "feat",
        "post-create",
        "--scope",
        "project",
        root=root,
        hooks_dir=hooks,
    )
    assert res.returncode == 0, res.stderr
    assert ran.read_text().split() == ["project"]


def test_hook_run_rejects_unknown_event(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    res = run("hook", "run", "alpha", "feat", "bogus", root=root)
    assert res.returncode == 1
    assert "unknown event" in res.stderr


def test_hook_run_rejects_retired_event_names(root: Path) -> None:
    # The pre/post rename retired the bare 'create'/'delete'/'rename' events.
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    for retired in ("create", "delete", "rename"):
        res = run("hook", "run", "alpha", "feat", retired, root=root)
        assert res.returncode == 1, retired
        assert "unknown event" in res.stderr


def test_hook_run_install_runs_install_hooks(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    hooks = tmp_path / "hooks"
    out = tmp_path / "hook.out"
    _write_hook(hooks / "wtm-install", f'echo "$WTM_EVENT" > "{out}"\n')
    res = run("hook", "run", "alpha", "feat", "install", root=root, hooks_dir=hooks)
    assert res.returncode == 0, res.stderr
    assert out.read_text().strip() == "install"


# --- hook run as a worktree-pinned locator ------------------------------------


def test_hook_run_cross_workspace_worktree(tmp_path: Path) -> None:
    # The target before the trailing HOOK is a worktree-pinned locator (as for
    # 'hook list'), so a bare name reaches another workspace's worktree.
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "onlyproj")
    _run_multi("create", "onlyproj", "feat", config=config, cwd=alpha, workspace="alpha")
    out = tmp_path / "hook.out"
    _write_hook(
        alpha / "onlyproj" / ".git" / "hooks" / "wtm-enter", f'echo "$WTM_PROJECT" > "{out}"\n'
    )
    res = _run_multi("hook", "run", "feat", "enter", config=config, cwd=beta, workspace="beta")
    assert res.returncode == 0, res.stderr
    assert out.read_text().strip() == "onlyproj"


def test_hook_run_pin_ignores_a_same_named_project(tmp_path: Path) -> None:
    # The WORKTREE pin refuses to cascade into the project tier, so a name that
    # is only a project is a miss rather than a surprise target.
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "onlyproj")
    res = _run_multi("hook", "run", "onlyproj", "enter", config=config, cwd=beta, workspace="beta")
    assert res.returncode == 1
    assert "no worktree matches: onlyproj" in res.stderr


# --- hook install / delete / list --------------------------------------------


def test_hook_install_bare_file_infers_event(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    src = tmp_path / "wtm-post-create"
    _write_hook(src, "true\n")
    res = run("hook", "install", str(src), "alpha", "feat", root=root)
    assert res.returncode == 0, res.stderr
    dest = root / "alpha" / ".git" / "hooks" / "wtm-post-create"
    assert dest.is_file()
    assert os.access(dest, os.X_OK)  # copies are chmod 0755


def test_hook_install_named_file_needs_event_and_lands_in_dotd(root: Path, tmp_path: Path) -> None:
    repo = _seed_repo(root, "alpha")
    # Pre-create the .d so a plain-named file joins it as an entry.
    (repo / "hooks" / "wtm-pre-delete.d").mkdir(parents=True)
    src = tmp_path / "50-purge"
    _write_hook(src, "true\n")
    res = run(
        "hook",
        "install",
        str(src),
        "alpha",
        "feat",
        "--event",
        "pre-delete",
        root=root,
    )
    assert res.returncode == 0, res.stderr
    assert (repo / "hooks" / "wtm-pre-delete.d" / "50-purge").is_file()


def test_hook_install_requires_event_when_uninferrable(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    src = tmp_path / "50-purge"
    _write_hook(src, "true\n")
    res = run("hook", "install", str(src), "alpha", "feat", root=root)
    assert res.returncode == 1
    assert "cannot infer event" in res.stderr


def test_hook_install_dir_merges_into_dotd(root: Path, tmp_path: Path) -> None:
    repo = _seed_repo(root, "alpha")
    bundle = tmp_path / "bundle"
    _write_hook(bundle / "10-a", "true\n")
    _write_hook(bundle / "20-b", "true\n")
    res = run(
        "hook",
        "install",
        str(bundle),
        "alpha",
        "feat",
        "--event",
        "pre-delete",
        root=root,
    )
    assert res.returncode == 0, res.stderr
    dotd = repo / "hooks" / "wtm-pre-delete.d"
    assert (dotd / "10-a").is_file()
    assert (dotd / "20-b").is_file()


def test_hook_install_collision_needs_force(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    src = tmp_path / "wtm-post-create"
    _write_hook(src, "true\n")
    assert run("hook", "install", str(src), "alpha", "feat", root=root).returncode == 0
    res = run("hook", "install", str(src), "alpha", "feat", root=root)
    assert res.returncode == 1
    assert "exists" in res.stderr
    res = run("hook", "install", str(src), "alpha", "feat", "--force", root=root)
    assert res.returncode == 0, res.stderr


def test_hook_install_link_symlinks(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    src = tmp_path / "wtm-post-create"
    _write_hook(src, "true\n")
    res = run("hook", "install", str(src), "alpha", "feat", "--link", root=root)
    assert res.returncode == 0, res.stderr
    dest = root / "alpha" / ".git" / "hooks" / "wtm-post-create"
    assert dest.is_symlink()
    assert dest.resolve() == src.resolve()


def test_hook_install_worktree_scope(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    src = tmp_path / "wtm-pre-delete"
    _write_hook(src, "true\n")
    res = run(
        "hook",
        "install",
        str(src),
        "alpha",
        "feat",
        "--scope",
        "worktree",
        root=root,
    )
    assert res.returncode == 0, res.stderr
    dest = root / "alpha" / "worktrees" / "feat" / ".wtm" / "hooks" / "wtm-pre-delete"
    assert dest.is_file()


def test_hook_install_user_scope_needs_no_target(root: Path, tmp_path: Path) -> None:
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    src = tmp_path / "wtm-post-create"
    _write_hook(src, "true\n")
    res = run("hook", "install", str(src), "--scope", "user", root=root, hooks_dir=hooks)
    assert res.returncode == 0, res.stderr
    assert (hooks / "wtm-post-create").is_file()


def test_hook_install_system_scope_needs_no_target(root: Path, tmp_path: Path) -> None:
    system = tmp_path / "system-hooks"
    system.mkdir()
    src = tmp_path / "wtm-post-create"
    _write_hook(src, "true\n")
    res = run("hook", "install", str(src), "--scope", "system", root=root, system_hooks_dir=system)
    assert res.returncode == 0, res.stderr
    assert (system / "wtm-post-create").is_file()


def test_hook_delete_bare_file(root: Path, tmp_path: Path) -> None:
    repo = _seed_repo(root, "alpha")
    _write_hook(repo / "hooks" / "wtm-post-create", "true\n")
    res = run("hook", "delete", "alpha", "feat", "post-create", root=root)
    assert res.returncode == 0, res.stderr
    assert not (repo / "hooks" / "wtm-post-create").exists()


def test_hook_delete_dotd_requires_recursive(root: Path, tmp_path: Path) -> None:
    repo = _seed_repo(root, "alpha")
    _write_hook(repo / "hooks" / "wtm-pre-delete.d" / "10-a", "true\n")
    res = run("hook", "delete", "alpha", "feat", "pre-delete", root=root)
    assert res.returncode == 1
    assert "recursive" in res.stderr
    assert (repo / "hooks" / "wtm-pre-delete.d").is_dir()
    res = run("hook", "delete", "alpha", "feat", "pre-delete", "-r", root=root)
    assert res.returncode == 0, res.stderr
    assert not (repo / "hooks" / "wtm-pre-delete.d").exists()


def test_hook_delete_entry_level(root: Path, tmp_path: Path) -> None:
    repo = _seed_repo(root, "alpha")
    _write_hook(repo / "hooks" / "wtm-pre-delete.d" / "10-a", "true\n")
    _write_hook(repo / "hooks" / "wtm-pre-delete.d" / "20-b", "true\n")
    res = run("hook", "delete", "alpha", "feat", "pre-delete/10-a", root=root)
    assert res.returncode == 0, res.stderr
    assert not (repo / "hooks" / "wtm-pre-delete.d" / "10-a").exists()
    assert (repo / "hooks" / "wtm-pre-delete.d" / "20-b").is_file()


def test_hook_list_groups_by_event(root: Path, tmp_path: Path) -> None:
    repo = _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    _write_hook(repo / "hooks" / "wtm-post-create", "true\n")
    _write_hook(repo / "hooks" / "wtm-pre-delete.d" / "10-a", "true\n")
    res = run("hook", "list", "alpha", "feat", root=root)
    assert res.returncode == 0, res.stderr
    assert "post-create:" in res.stdout
    assert "pre-delete:" in res.stdout
    assert "wtm-post-create" in res.stdout
    assert "10-a" in res.stdout


def test_hook_list_types_summary(root: Path, tmp_path: Path) -> None:
    repo = _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    _write_hook(repo / "hooks" / "wtm-pre-delete.d" / "10-a", "true\n")
    _write_hook(repo / "hooks" / "wtm-pre-delete.d" / "20-b", "true\n")
    res = run("hook", "list", "alpha", "feat", "--types", root=root)
    assert res.returncode == 0, res.stderr
    assert "pre-delete: 2 hook(s)" in res.stdout
    assert "post-create: 0 hook(s)" in res.stdout


def test_hook_list_by_bare_worktree_name(root: Path) -> None:
    # 'hook list' takes a worktree-pinned locator: a bare NAME finds its worktree
    # from anywhere, no PROJECT needed.
    repo = _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    _write_hook(repo / "hooks" / "wtm-pre-delete", "true\n")
    res = run("hook", "list", "feat", root=root, cwd=root)
    assert res.returncode == 0, res.stderr
    assert "wtm-pre-delete" in res.stdout


def test_hook_list_unknown_worktree_errors(root: Path) -> None:
    # A name matching no worktree is now a miss rather than a silent fallback to
    # the system/user scopes only.
    _seed_repo(root, "alpha")
    res = run("hook", "list", "ghost", root=root, cwd=root)
    assert res.returncode == 1
    assert "no worktree matches: ghost" in res.stderr


# --- workspace scope ---------------------------------------------------------


def test_hook_install_workspace_scope_needs_no_target(root: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-hooks"
    src = tmp_path / "wtm-post-create"
    _write_hook(src, "true\n")
    res = run(
        "hook",
        "install",
        str(src),
        "--scope",
        "workspace",
        root=root,
        workspace_hooks_dir=workspace,
    )
    assert res.returncode == 0, res.stderr
    assert (workspace / "wtm-post-create").is_file()


def test_workspace_scope_default_location(root: Path, tmp_path: Path) -> None:
    # With WTM_WORKSPACE_HOOKS_DIR unset the workspace dir is $root/.wtm/hooks.
    src = tmp_path / "wtm-post-create"
    _write_hook(src, "true\n")
    res = run("hook", "install", str(src), "--scope", "workspace", root=root)
    assert res.returncode == 0, res.stderr
    assert (root / ".wtm" / "hooks" / "wtm-post-create").is_file()


def test_workspace_hook_fires_and_lists(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    workspace = tmp_path / "workspace-hooks"
    out = tmp_path / "ran"
    _write_hook(workspace / "wtm-post-create", f'echo "$WTM_EVENT" > "{out}"\n')
    res = run(
        "hook",
        "run",
        "alpha",
        "feat",
        "post-create",
        "--scope",
        "workspace",
        root=root,
        workspace_hooks_dir=workspace,
    )
    assert res.returncode == 0, res.stderr
    assert out.read_text().strip() == "post-create"
    listed = run(
        "hook",
        "list",
        "alpha",
        "feat",
        root=root,
        workspace_hooks_dir=workspace,
    )
    assert "workspace:" in listed.stdout


# --- enter / leave events ----------------------------------------------------


def test_enter_event_fires_on_enter(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    hooks = tmp_path / "hooks"
    out = tmp_path / "ran"
    shell_file = tmp_path / "cd"
    shell_file.touch()
    _write_hook(hooks / "wtm-enter", f'echo "$WTM_EVENT" > "{out}"\n')
    res = run("enter", "alpha", "feat", root=root, hooks_dir=hooks, shell_file=shell_file)
    assert res.returncode == 0, res.stderr
    assert out.read_text().strip() == "enter"


def test_enter_event_fires_on_create_cd(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    hooks = tmp_path / "hooks"
    out = tmp_path / "ran"
    shell_file = tmp_path / "cd"
    shell_file.touch()
    _write_hook(hooks / "wtm-enter", f'echo "$WTM_EVENT" >> "{out}"\n')
    res = run("create", "alpha", "feat", "-c", root=root, hooks_dir=hooks, shell_file=shell_file)
    assert res.returncode == 0, res.stderr
    assert out.read_text().strip() == "enter"


def test_create_without_cd_does_not_fire_enter(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    hooks = tmp_path / "hooks"
    out = tmp_path / "ran"
    _write_hook(hooks / "wtm-enter", f'echo enter >> "{out}"\n')
    res = run("create", "alpha", "feat", root=root, hooks_dir=hooks)
    assert res.returncode == 0, res.stderr
    assert not out.exists()


def test_enter_install_then_enter_order(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    hooks = tmp_path / "hooks"
    order = tmp_path / "order"
    shell_file = tmp_path / "cd"
    shell_file.touch()
    _write_hook(hooks / "wtm-install", f'echo install >> "{order}"\n')
    _write_hook(hooks / "wtm-enter", f'echo enter >> "{order}"\n')
    res = run("enter", "alpha", "feat", "-i", root=root, hooks_dir=hooks, shell_file=shell_file)
    assert res.returncode == 0, res.stderr
    assert order.read_text().split() == ["install", "enter"]


def test_create_cd_install_order(root: Path, tmp_path: Path) -> None:
    # 'wtm create -c -i' fires post-create -> install -> enter.
    _seed_repo(root, "alpha")
    hooks = tmp_path / "hooks"
    order = tmp_path / "order"
    shell_file = tmp_path / "cd"
    shell_file.touch()
    _write_hook(hooks / "wtm-post-create", f'echo post-create >> "{order}"\n')
    _write_hook(hooks / "wtm-install", f'echo install >> "{order}"\n')
    _write_hook(hooks / "wtm-enter", f'echo enter >> "{order}"\n')
    res = run(
        "create",
        "alpha",
        "feat",
        "-c",
        "-i",
        root=root,
        hooks_dir=hooks,
        shell_file=shell_file,
    )
    assert res.returncode == 0, res.stderr
    assert order.read_text().split() == ["post-create", "install", "enter"]


def test_leave_event_fires_with_cwd_default(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    wt = root / "alpha" / "worktrees" / "feat"
    hooks = tmp_path / "hooks"
    out = tmp_path / "ran"
    shell_file = tmp_path / "cd"
    shell_file.touch()
    # cwd default: bare 'wtm leave' acts on the worktree you stand in.
    _write_hook(
        hooks / "wtm-leave",
        f'printf "%s|%s\\n" "$WTM_EVENT" "$WTM_WORKTREE_DIR" > "{out}"\n',
    )
    res = run("leave", root=root, cwd=wt, hooks_dir=hooks, shell_file=shell_file)
    assert res.returncode == 0, res.stderr
    event, dir_ = out.read_text().strip().split("|")
    assert event == "leave"
    assert dir_ == str(wt)
    assert wt.exists()  # non-destructive


def test_leave_cd_lands_in_oldpwd(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    wt = root / "alpha" / "worktrees" / "feat"
    prev = tmp_path / "prev"
    prev.mkdir()
    shell_file = tmp_path / "cd"
    shell_file.touch()
    res = run(
        "leave",
        root=root,
        cwd=wt,
        shell_file=shell_file,
        extra_env={"OLDPWD": str(prev)},
    )
    assert res.returncode == 0, res.stderr
    assert _shell_cd(shell_file) == str(prev)
    # Leaving ends the session: the whole managed contract is unset.
    assert _shell_unsets(shell_file) == set(MANAGED_ENV_VARS)
    assert _shell_exports(shell_file) == {}


def test_leave_cd_falls_back_to_project_dir(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    wt = root / "alpha" / "worktrees" / "feat"
    gone = tmp_path / "gone"  # never created -> not a dir
    shell_file = tmp_path / "cd"
    shell_file.touch()
    res = run(
        "leave",
        root=root,
        cwd=wt,
        shell_file=shell_file,
        extra_env={"OLDPWD": str(gone)},
    )
    assert res.returncode == 0, res.stderr
    assert _shell_cd(shell_file) == str(root / "alpha")


def test_leave_no_cd_stays_put(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    wt = root / "alpha" / "worktrees" / "feat"
    prev = tmp_path / "prev"
    prev.mkdir()
    shell_file = tmp_path / "cd"
    shell_file.touch()
    # -C/--no-cd runs leave hooks and stays put, but the session still ends:
    # the WTM_* contract is unset even though the shell does not cd out.
    res = run(
        "leave",
        "-C",
        root=root,
        cwd=wt,
        shell_file=shell_file,
        extra_env={"OLDPWD": str(prev)},
    )
    assert res.returncode == 0, res.stderr
    assert _shell_cd(shell_file) is None
    assert _shell_unsets(shell_file) == set(MANAGED_ENV_VARS)


# --- leave as a locator ------------------------------------------------------
#
# leave takes enter's locator, so a session entered by name is left by that same
# name. The round-trip at each tier is the contract.


def test_leave_single_name_resolves_worktree_globally(root: Path, tmp_path: Path) -> None:
    # A bare NAME finds its worktree without a PROJECT, from anywhere in the root.
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    hooks = tmp_path / "hooks"
    out = tmp_path / "ran"
    shell_file = tmp_path / "cd"
    shell_file.touch()
    _write_hook(hooks / "wtm-leave", f'printf "%s\\n" "$WTM_WORKTREE_DIR" > "{out}"\n')
    res = run("leave", "feat", root=root, cwd=root, hooks_dir=hooks, shell_file=shell_file)
    assert res.returncode == 0, res.stderr
    assert out.read_text().strip() == str(root / "alpha" / "worktrees" / "feat")


def test_leave_single_name_resolves_project(root: Path, tmp_path: Path) -> None:
    # No worktree named 'alpha' -> the project tier wins, firing project-leave
    # (the mirror of 'enter alpha' landing on the project).
    _seed_repo(root, "alpha")
    hooks = tmp_path / "hooks"
    out = tmp_path / "ran"
    shell_file = tmp_path / "cd"
    shell_file.touch()
    _write_hook(hooks / "wtm-project-leave", f'printf "%s\\n" "$WTM_EVENT" > "{out}"\n')
    res = run("leave", "alpha", root=root, cwd=root, hooks_dir=hooks, shell_file=shell_file)
    assert res.returncode == 0, res.stderr
    assert out.read_text().strip() == "project-leave"
    assert "left project 'alpha'" in res.stderr


def test_leave_single_name_resolves_workspace(root: Path, tmp_path: Path) -> None:
    # Neither a worktree nor a project named 'ws' -> the workspace tier.
    _seed_repo(root, "alpha")
    hooks = tmp_path / "hooks"
    out = tmp_path / "ran"
    shell_file = tmp_path / "cd"
    shell_file.touch()
    _write_hook(hooks / "wtm-workspace-leave", f'printf "%s\\n" "$WTM_EVENT" > "{out}"\n')
    res = run("leave", "ws", root=root, cwd=root, hooks_dir=hooks, shell_file=shell_file)
    assert res.returncode == 0, res.stderr
    assert out.read_text().strip() == "workspace-leave"
    assert "left workspace 'ws'" in res.stderr


def test_enter_leave_round_trip_by_the_same_name(root: Path, tmp_path: Path) -> None:
    # The headline symmetry: whatever token entered a session leaves it.
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    shell_file = tmp_path / "cd"
    shell_file.touch()
    entered = run("enter", "feat", root=root, cwd=root, shell_file=shell_file)
    assert entered.returncode == 0, entered.stderr
    assert _shell_cd(shell_file) == str(root / "alpha" / "worktrees" / "feat")
    left = run("leave", "feat", root=root, cwd=root, shell_file=shell_file)
    assert left.returncode == 0, left.stderr
    assert _shell_unsets(shell_file) == set(MANAGED_ENV_VARS)


def test_leave_cross_workspace_worktree(tmp_path: Path) -> None:
    # A worktree in another workspace is left by name alone, as it is entered.
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "proj")
    _run_multi("create", "proj", "onlywt", config=config, cwd=alpha, workspace="alpha")
    shell_file = tmp_path / "cd"
    shell_file.touch()
    res = _run_multi(
        "leave", "onlywt", config=config, cwd=beta, workspace="beta", shell_file=shell_file
    )
    assert res.returncode == 0, res.stderr
    assert _shell_unsets(shell_file) == set(MANAGED_ENV_VARS)


def test_leave_ambiguous_lists_choices_as_leave(root: Path) -> None:
    # The ambiguity error is copy-runnable for the command that raised it.
    _seed_repo(root, "alpha")
    _seed_repo(root, "beta")
    run("create", "alpha", "shared", root=root)
    run("create", "beta", "shared", root=root)
    res = run("leave", "shared", root=root, cwd=root)
    assert res.returncode == 1
    assert "matches 2 worktrees" in res.stderr
    assert "wtm leave ws alpha shared" in res.stderr
    assert "wtm leave ws beta shared" in res.stderr


def test_leave_no_match_errors(root: Path) -> None:
    _seed_repo(root, "alpha")
    res = run("leave", "ghost", root=root, cwd=root)
    assert res.returncode == 1
    assert "no worktree, project, or workspace matches" in res.stderr


def test_leave_dir_arg_still_a_path(root: Path, tmp_path: Path) -> None:
    # A path-looking single arg keeps the classic worktree-path behavior.
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    wt = root / "alpha" / "worktrees" / "feat"
    shell_file = tmp_path / "cd"
    shell_file.touch()
    res = run(
        "leave", ".", root=root, cwd=wt, shell_file=shell_file, extra_env={"OLDPWD": str(wt)}
    )
    assert res.returncode == 0, res.stderr
    assert wt.exists()
    assert _shell_unsets(shell_file) == set(MANAGED_ENV_VARS)


# --- usage / dispatch --------------------------------------------------------


def test_no_args_shows_help(root: Path) -> None:
    res = run(root=root)
    assert res.returncode != 0
    assert "Usage:" in res.stdout or "Usage:" in res.stderr


def test_unknown_command(root: Path) -> None:
    res = run("frobnicate", root=root)
    assert res.returncode == 2


# --- worktree subcommand group -----------------------------------------------
#
# The worktree-level commands are canonically 'wtm worktree <cmd>' with a
# top-level '<cmd>' alias. The rest of the suite exercises the aliases; these
# assert the subcommand form is wired to the same handlers.


def test_worktree_group_no_args_shows_help(root: Path) -> None:
    res = run("worktree", root=root)
    assert res.returncode != 0
    assert "Usage:" in res.stdout or "Usage:" in res.stderr


def test_worktree_create_makes_worktree(root: Path) -> None:
    _seed_repo(root, "alpha")
    res = run("worktree", "create", "alpha", "feat", root=root)
    assert res.returncode == 0, res.stderr
    wt = root / "alpha" / "worktrees" / "feat"
    assert (wt / "file.txt").exists()
    branch = subprocess.run(
        ["git", "-C", str(wt), "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == "feat"


def test_worktree_list_matches_alias(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)  # via the top-level alias
    sub = run("worktree", "list", "alpha", root=root)
    alias = run("list", "alpha", root=root)
    assert sub.returncode == 0, sub.stderr
    assert [line.split()[0] for line in sub.stdout.splitlines()] == ["feat"]
    assert sub.stdout == alias.stdout  # same handler, identical output


def test_worktree_resolve_prints_path(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    res = run("worktree", "resolve", "alpha", "feat", root=root)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == str(root / "alpha" / "worktrees" / "feat")


def test_worktree_delete_removes_worktree(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("worktree", "create", "alpha", "feat", root=root)
    wt = root / "alpha" / "worktrees" / "feat"
    assert wt.is_dir()
    res = run("worktree", "delete", "alpha", "feat", root=root)
    assert res.returncode == 0, res.stderr
    assert not wt.exists()


def test_worktree_rename_matches_alias(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    res = run("worktree", "rename", "alpha", "feat", "fix", root=root)
    assert res.returncode == 0, res.stderr
    assert (root / "alpha" / "worktrees" / "fix").is_dir()
    assert not (root / "alpha" / "worktrees" / "feat").exists()


# --- import -------------------------------------------------------------------


def _git(cwd: Path, *args: str, root: Path) -> subprocess.CompletedProcess[str]:
    """Run git in *cwd* with the test's isolated identity/config."""
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(root / "_gitconfig"),
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }

    return subprocess.run(["git", "-C", str(cwd), *args], env=env, capture_output=True, text=True)


def _seed_source(root: Path, name: str = "src") -> Path:
    """Build a plain working clone (the import source) with dirty state.

    Returns the source top level, placed *outside* the projects root (sources
    live in a sibling tree, like ``~/workspace/src``). It has a real upstream (a
    bare repo) as ``origin``, a committed ``main``, a checked-out
    ``feature/foo`` branch, a modified tracked file, an untracked file, and a
    git-ignored ``build/`` dir.
    """
    outside = root.parent  # the tmp_path; siblings of the projects root
    upstream = outside / "_upstream" / f"{name}.git"
    upstream.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "--bare", str(upstream), root=root)

    source = outside / "_src" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "clone", "-q", str(upstream), str(source), root=root)
    (source / "file.txt").write_text("hello\n")
    (source / ".gitignore").write_text("build/\n")
    _git(source, "add", ".", root=root)
    _git(source, "commit", "-qm", "init", root=root)
    _git(source, "push", "-q", "origin", "main", root=root)

    _git(source, "checkout", "-qb", "feature/foo", root=root)
    (source / "file.txt").write_text("work-in-progress\n")  # modified tracked
    (source / "untracked.txt").write_text("untracked\n")  # untracked
    (source / "build").mkdir()
    (source / "build" / "artifact.bin").write_text("ignored\n")  # ignored

    return source


def test_import_creates_target_and_worktree(root: Path) -> None:
    source = _seed_source(root)
    res = run("import", "--project", "proj", root=root, cwd=source)
    assert res.returncode == 0, res.stderr

    git_dir = root / "proj" / ".git"
    assert git_dir.is_dir()
    # Dual-ref refspec present and origin points at the real upstream.
    assert (
        _git(
            root, "--git-dir", str(git_dir), "config", "remote.origin.fetch", root=root
        ).stdout.strip()
        == "+refs/heads/*:refs/remotes/origin/*"
    )
    assert _git(
        root, "--git-dir", str(git_dir), "config", "remote.origin.url", root=root
    ).stdout.strip() == str(root.parent / "_upstream" / "src.git")

    wt = root / "proj" / "worktrees" / "feature" / "foo"
    assert _git(wt, "symbolic-ref", "--short", "HEAD", root=root).stdout.strip() == "feature/foo"
    # Working-tree state reproduced: modified + untracked + ignored.
    assert (wt / "file.txt").read_text() == "work-in-progress\n"
    assert (wt / "untracked.txt").exists()
    assert (wt / "build" / "artifact.bin").exists()


def test_import_infers_project_from_source_basename(root: Path) -> None:
    source = _seed_source(root, "myrepo")
    res = run("import", root=root, cwd=source)  # no --project, no positional
    assert res.returncode == 0, res.stderr
    assert (root / "myrepo" / ".git").is_dir()


def test_project_import_subcommand_form(root: Path) -> None:
    # 'import' is canonically 'wtm project import' with a 'wtm import' alias;
    # the subcommand form is wired to the same handler.
    source = _seed_source(root)
    res = run("project", "import", "--project", "proj", root=root, cwd=source)
    assert res.returncode == 0, res.stderr
    assert (root / "proj" / ".git").is_dir()
    assert (root / "proj" / "worktrees" / "feature" / "foo").is_dir()


def test_import_positional_names_project(root: Path) -> None:
    source = _seed_source(root)
    res = run("import", "chosen", root=root, cwd=source)
    assert res.returncode == 0, res.stderr
    assert (root / "chosen" / ".git").is_dir()


def test_import_no_ignored_drops_ignored_files(root: Path) -> None:
    source = _seed_source(root)
    res = run("import", "--project", "proj", "--no-ignored", root=root, cwd=source)
    assert res.returncode == 0, res.stderr
    wt = root / "proj" / "worktrees" / "feature" / "foo"
    assert (wt / "untracked.txt").exists()  # untracked kept
    assert not (wt / "build" / "artifact.bin").exists()  # ignored dropped


def test_import_exclude_pattern_honored(root: Path) -> None:
    source = _seed_source(root)
    res = run(
        "import",
        "--project",
        "proj",
        "--exclude",
        "untracked.txt",
        root=root,
        cwd=source,
    )
    assert res.returncode == 0, res.stderr
    wt = root / "proj" / "worktrees" / "feature" / "foo"
    assert not (wt / "untracked.txt").exists()


def test_import_mirrors_uncommitted_deletion(root: Path) -> None:
    source = _seed_source(root)
    # Delete a committed file without committing: the mirror must drop it too.
    (source / "file.txt").unlink()
    res = run("import", "--project", "proj", root=root, cwd=source)
    assert res.returncode == 0, res.stderr
    wt = root / "proj" / "worktrees" / "feature" / "foo"
    assert not (wt / "file.txt").exists()


def test_import_no_worktree_skips_checkout(root: Path) -> None:
    source = _seed_source(root)
    res = run("import", "--project", "proj", "--no-worktree", root=root, cwd=source)
    assert res.returncode == 0, res.stderr
    assert (root / "proj" / ".git").is_dir()
    assert list((root / "proj" / "worktrees").iterdir()) == []


def test_import_copies_remotes(root: Path) -> None:
    source = _seed_source(root)
    _git(source, "remote", "add", "fork", "git@example.com:me/fork.git", root=root)
    res = run("import", "--project", "proj", "--no-worktree", root=root, cwd=source)
    assert res.returncode == 0, res.stderr
    remotes = _git(
        root, "--git-dir", str(root / "proj" / ".git"), "remote", root=root
    ).stdout.split()
    assert "fork" in remotes


def test_import_existing_target_refused_without_update(root: Path) -> None:
    source = _seed_source(root)
    run("import", "--project", "proj", "--no-worktree", root=root, cwd=source)
    # A new branch added after the first import must NOT reach the target
    # without --update.
    _git(source, "checkout", "-qb", "another", root=root)
    (source / "a.txt").write_text("a\n")
    _git(source, "add", ".", root=root)
    _git(source, "commit", "-qm", "a", root=root)

    res = run("import", "--project", "proj", "--no-worktree", root=root, cwd=source)
    assert res.returncode == 1
    assert "already exists" in res.stderr
    assert (
        _git(
            root,
            "--git-dir",
            str(root / "proj" / ".git"),
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/another",
            root=root,
        ).returncode
        != 0
    )


def test_import_existing_target_pushes_new_branch(root: Path) -> None:
    source = _seed_source(root)
    run("import", "--project", "proj", "--no-worktree", root=root, cwd=source)
    # A new branch added after the first import must reach the existing target.
    _git(source, "checkout", "-qb", "another", root=root)
    (source / "a.txt").write_text("a\n")
    _git(source, "add", ".", root=root)
    _git(source, "commit", "-qm", "a", root=root)

    res = run(
        "import",
        "--project",
        "proj",
        "--no-worktree",
        "--update",
        root=root,
        cwd=source,
    )
    assert res.returncode == 0, res.stderr
    assert (
        _git(
            root,
            "--git-dir",
            str(root / "proj" / ".git"),
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/another",
            root=root,
        ).returncode
        == 0
    )
    # The temporary push remote is cleaned up from the source.
    assert "wtm-import-target" not in _git(source, "remote", root=root).stdout.split()


def test_import_init_bare_imports_refs_via_push(root: Path) -> None:
    # --init-bare makes an empty repo, so references must arrive by push from
    # the source (the disk-clone shortcut does not apply).
    source = _seed_source(root)
    res = run("import", "--project", "proj", "--init-bare", root=root, cwd=source)
    assert res.returncode == 0, res.stderr
    git_dir = root / "proj" / ".git"
    for branch in ("main", "feature/foo"):
        assert (
            _git(
                root,
                "--git-dir",
                str(git_dir),
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
                root=root,
            ).returncode
            == 0
        )
    # Fetch refspec still set on the init'd bare repo.
    assert (
        _git(
            root, "--git-dir", str(git_dir), "config", "remote.origin.fetch", root=root
        ).stdout.strip()
        == "+refs/heads/*:refs/remotes/origin/*"
    )
    wt = root / "proj" / "worktrees" / "feature" / "foo"
    assert (wt / "file.txt").read_text() == "work-in-progress\n"


def test_import_dry_run_makes_no_changes(root: Path) -> None:
    source = _seed_source(root)
    res = run("import", "--project", "proj", "--dry-run", root=root, cwd=source)
    assert res.returncode == 0, res.stderr
    assert not (root / "proj").exists()
    assert "from_disk" in res.stderr


def test_import_cd_emits_worktree_path(root: Path, tmp_path: Path) -> None:
    source = _seed_source(root)
    shell_file = tmp_path / "cd"
    shell_file.touch()
    res = run("import", "--project", "proj", "-c", root=root, cwd=source, shell_file=shell_file)
    assert res.returncode == 0, res.stderr
    assert _shell_cd(shell_file) == str(root / "proj" / "worktrees" / "feature" / "foo")


def test_import_detached_head_requires_name(root: Path) -> None:
    source = _seed_source(root)
    head = _git(source, "rev-parse", "HEAD", root=root).stdout.strip()
    _git(source, "checkout", "-q", head, root=root)  # detach
    res = run("import", "--project", "proj", root=root, cwd=source)
    assert res.returncode == 1
    assert "detached HEAD" in res.stderr


def test_import_under_projects_root_needs_source(root: Path) -> None:
    # Standing under the projects root with no source given is an error.
    run("project", "new", "proj", root=root)
    res = run("import", root=root, cwd=root / "proj")
    assert res.returncode == 1
    assert "source" in res.stderr


# --- import --all-worktrees ---------------------------------------------------


def _add_linked_worktree(
    source: Path, name: str, branch: str, root: Path, *, content: str = "linked\n"
) -> Path:
    """Add a linked worktree on a new *branch* beside *source*; return its path."""
    path = source.parent / name
    _git(source, "worktree", "add", "-q", "-b", branch, str(path), root=root)
    (path / "wt.txt").write_text(content)

    return path


def _detach_worktree(source: Path, name: str, root: Path) -> tuple[Path, str]:
    """Add a linked worktree detached at HEAD; return its path and the sha."""
    path = source.parent / name
    head = _git(source, "rev-parse", "HEAD", root=root).stdout.strip()
    _git(source, "worktree", "add", "-q", "--detach", str(path), head, root=root)

    return path, head


def test_import_all_worktrees_reproduces_set(root: Path) -> None:
    source = _seed_source(root)  # main worktree on feature/foo
    _add_linked_worktree(source, "wt-bar", "bar", root)
    res = run("import", "--project", "proj", "--all-worktrees", root=root, cwd=source)
    assert res.returncode == 0, res.stderr

    # Main worktree named after the source dir basename (not its branch).
    main_wt = root / "proj" / "worktrees" / "src"
    assert (
        _git(main_wt, "symbolic-ref", "--short", "HEAD", root=root).stdout.strip() == "feature/foo"
    )
    assert (main_wt / "untracked.txt").exists()  # working tree mirrored

    # Linked worktree reproduced under its source dir name, on its branch.
    bar_wt = root / "proj" / "worktrees" / "wt-bar"
    assert _git(bar_wt, "symbolic-ref", "--short", "HEAD", root=root).stdout.strip() == "bar"
    assert (bar_wt / "wt.txt").read_text() == "linked\n"


def test_import_all_worktrees_detached_pins_branch(root: Path) -> None:
    source = _seed_source(root)
    _path, head = _detach_worktree(source, "wt-detached", root)
    res = run("import", "--project", "proj", "--all-worktrees", root=root, cwd=source)
    assert res.returncode == 0, res.stderr

    wt = root / "proj" / "worktrees" / "wt-detached"
    # A branch named after the source dir was pinned at the detached commit.
    assert _git(wt, "symbolic-ref", "--short", "HEAD", root=root).stdout.strip() == "wt-detached"
    assert _git(wt, "rev-parse", "HEAD", root=root).stdout.strip() == head


def test_import_all_worktrees_skips_existing_collision(root: Path) -> None:
    source = _seed_source(root)
    _add_linked_worktree(source, "wt-bar", "bar", root)
    # First import creates both worktrees.
    run("import", "--project", "proj", "--all-worktrees", root=root, cwd=source)
    # Re-run: both targets already exist, so both are skipped, run still succeeds.
    res = run(
        "import",
        "--project",
        "proj",
        "--all-worktrees",
        "--update",
        root=root,
        cwd=source,
    )
    assert res.returncode == 0, res.stderr
    assert "skipped" in res.stderr


def test_import_all_worktrees_rejects_name(root: Path) -> None:
    source = _seed_source(root)
    res = run(
        "import",
        "--project",
        "proj",
        "--all-worktrees",
        "--name",
        "x",
        root=root,
        cwd=source,
    )
    assert res.returncode == 1
    assert "mutually exclusive" in res.stderr


def test_import_all_worktrees_rejects_no_worktree(root: Path) -> None:
    source = _seed_source(root)
    res = run(
        "import",
        "--project",
        "proj",
        "--all-worktrees",
        "--no-worktree",
        root=root,
        cwd=source,
    )
    assert res.returncode == 1
    assert "mutually exclusive" in res.stderr


def test_import_all_worktrees_skips_prunable(root: Path) -> None:
    source = _seed_source(root)
    path = _add_linked_worktree(source, "wt-gone", "gone", root)
    shutil.rmtree(path)  # leave a registered-but-missing worktree
    res = run("import", "--project", "proj", "--all-worktrees", root=root, cwd=source)
    assert res.returncode == 0, res.stderr
    assert not (root / "proj" / "worktrees" / "wt-gone").exists()
    assert "prunable" in res.stderr or "missing" in res.stderr
    # The healthy main worktree is still reproduced.
    assert (root / "proj" / "worktrees" / "src").is_dir()


def test_import_all_worktrees_cd_lands_in_cwd_worktree(root: Path, tmp_path: Path) -> None:
    source = _seed_source(root)
    linked = _add_linked_worktree(source, "wt-bar", "bar", root)
    shell_file = tmp_path / "cd"
    shell_file.touch()
    # Invoke from inside the linked worktree: -c should land there, not in main.
    res = run(
        "import",
        "--project",
        "proj",
        "--all-worktrees",
        "-c",
        root=root,
        cwd=linked,
        shell_file=shell_file,
    )
    assert res.returncode == 0, res.stderr
    assert _shell_cd(shell_file) == str(root / "proj" / "worktrees" / "wt-bar")


def test_import_all_worktrees_dry_run_previews_each(root: Path) -> None:
    source = _seed_source(root)
    _add_linked_worktree(source, "wt-bar", "bar", root)
    res = run(
        "import",
        "--project",
        "proj",
        "--all-worktrees",
        "--dry-run",
        root=root,
        cwd=source,
    )
    assert res.returncode == 0, res.stderr
    assert not (root / "proj").exists()  # no changes
    assert "worktree 'src'" in res.stderr
    assert "worktree 'wt-bar'" in res.stderr


# --- workspaces & config (integration) ---------------------------------------


def _multi_workspace_config(base: Path, names: list[str]) -> tuple[Path, list[Path]]:
    """Write a config declaring several workspaces; return (config_path, paths)."""
    roots = []
    body = ""
    for name in names:
        ws = base / name
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "_gitconfig").write_text("[init]\n\tdefaultBranch = main\n")
        roots.append(ws)
        body += f'[workspaces.{name}]\npath = "{ws}"\n'
    config = base / "multi.toml"
    config.write_text(body)

    return config, roots


def _run_multi(
    *args: str,
    config: Path,
    cwd: Path,
    workspace: str | None = None,
    shell_file: Path | None = None,
):
    """Run wtm against a hand-written multi-workspace config."""
    env = {
        "WTM_CONFIG": str(config),
        "WTM_SYSTEM_CONFIG": str(config.parent / "_no_system_config.toml"),
        "WTM_USER_HOOKS_DIR": str(config.parent / "_no_user_hooks"),
        "WTM_SYSTEM_HOOKS_DIR": str(config.parent / "_no_system_hooks"),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": str(cwd / "_gitconfig"),
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    if shell_file is not None:
        env["WTM_SHELL_FILE"] = str(shell_file)
    if workspace is not None:
        env["WTM_WORKSPACE"] = workspace
    else:
        env.pop("WTM_WORKSPACE", None)
    prev = dict(os.environ)
    prev_argv = sys.argv
    prev_cwd = os.getcwd()
    out, err = io.StringIO(), io.StringIO()
    # Purge any inherited WTM_WORKSPACE so cwd inference is exercised.
    os.environ.pop("WTM_WORKSPACE", None)
    try:
        os.environ.update(env)
        os.chdir(cwd)
        sys.argv = ["wtm", *args]
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    finally:
        os.chdir(prev_cwd)
        sys.argv = prev_argv
        os.environ.clear()
        os.environ.update(prev)

    return _Result(returncode=code, stdout=out.getvalue(), stderr=err.getvalue())


def test_workspace_list_marks_active(tmp_path: Path) -> None:
    config, _ = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    res = _run_multi("workspace", "list", config=config, cwd=tmp_path, workspace="beta")
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()
    assert any(line.startswith("* beta") for line in lines)
    assert any(line.startswith("  alpha") for line in lines)


def test_workspace_resolve_default_and_named(tmp_path: Path) -> None:
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    res = _run_multi(
        "workspace", "resolve", "beta", config=config, cwd=tmp_path, workspace="alpha"
    )
    assert res.stdout.strip() == str(roots[1])
    # No arg -> the active (selected) workspace.
    res = _run_multi("workspace", "resolve", config=config, cwd=tmp_path, workspace="alpha")
    assert res.stdout.strip() == str(roots[0])


def test_workspace_selection_by_cwd(tmp_path: Path) -> None:
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    # cwd inside beta's root -> beta is active without a flag/env.
    res = _run_multi("config", "show", config=config, cwd=roots[1])
    assert "workspace beta:" in res.stdout


def test_no_active_workspace_errors(tmp_path: Path) -> None:
    config, _ = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    res = _run_multi("project", "list", config=config, cwd=tmp_path)  # ambiguous
    assert res.returncode == 1
    assert "no active workspace" in res.stderr


def test_config_show_sources_flags_inert_annotation(tmp_path: Path) -> None:
    ws = tmp_path / "alpha"
    ws.mkdir()
    config = tmp_path / "c.toml"
    config.write_text(
        f'[workspaces.alpha]\npath = "{ws}"\n'
        '[workspaces.alpha.projects.ghost]\ndescription = "gone"\n'
    )
    res = _run_multi(
        "config", "show", "-g", "-r", "--origin", config=config, cwd=tmp_path, workspace="alpha"
    )
    assert res.returncode == 0, res.stderr
    assert "[user]" in res.stdout
    assert "no .git on disk" in res.stdout


def test_config_show_lists_project_local_only_annotation(tmp_path: Path) -> None:
    # A project annotated *only* in its own <project>/.wtm/config.toml (declared
    # nowhere in the registry) still appears in the workspace listing, resolved
    # via the probe engine -- the old registry-only enumeration missed it.
    ws = tmp_path / "alpha"
    proj = ws / "api"
    (proj / ".git").mkdir(parents=True)
    (proj / ".wtm").mkdir(parents=True)
    (proj / ".wtm" / "config.toml").write_text('base_branch = "develop"\n')
    config = tmp_path / "c.toml"
    config.write_text(f'[workspaces.alpha]\npath = "{ws}"\n')
    res = _run_multi("config", "show", "-g", "-r", config=config, cwd=tmp_path, workspace="alpha")
    assert res.returncode == 0, res.stderr
    assert "project api" in res.stdout
    assert "develop" in res.stdout


def test_project_list_all_spans_workspaces(root: Path, tmp_path: Path) -> None:
    # Two workspaces, each with one project; --all --worktrees prepends the ws.
    config, roots = _multi_workspace_config(tmp_path, ["A", "B"])
    for name, ws in zip(["A", "B"], roots, strict=True):
        _run_multi("project", "new", f"proj{name}", config=config, cwd=ws, workspace=name)
        _run_multi("create", f"proj{name}", "feat", config=config, cwd=ws, workspace=name)
    res = _run_multi(
        "project", "list", "-a", "--worktrees", config=config, cwd=tmp_path, workspace="A"
    )
    rows = [line.split("\t") for line in res.stdout.splitlines()]
    workspaces = {row[0] for row in rows}
    assert workspaces == {"A", "B"}
    assert all(len(row) == 6 for row in rows)  # workspace column prepended


# --- inline config hooks (integration) ---------------------------------------


def _config_with_inline_hook(root: Path, event: str, command: str) -> Path:
    """Write a config declaring the 'ws' workspace plus a workspace-scope hook."""
    config = root / "_custom.toml"
    config.write_text(
        f'[workspaces.ws]\npath = "{root}"\n[workspaces.ws.hooks.on]\n{event} = "{command}"\n'
    )

    return config


def test_inline_hook_fires_after_builtin(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    out = tmp_path / "log"
    hooks = tmp_path / "hooks"
    _write_hook(hooks / "wtm-post-create", f'echo builtin >> "{out}"\n')
    config = root / "_custom.toml"
    config.write_text(
        f'[workspaces.ws]\npath = "{root}"\n'
        f'[workspaces.ws.hooks]\ndir = "{hooks}"\n'
        f'[workspaces.ws.hooks.on]\npost-create = "echo inline >> {out}"\n'
    )
    res = run("create", "alpha", "feat", root=root, extra_env={"WTM_CONFIG": str(config)})
    assert res.returncode == 0, res.stderr
    # Builtin file runs before the inline config command within the scope.
    assert out.read_text().splitlines() == ["builtin", "inline"]


def test_inline_hook_gets_contract(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    out = tmp_path / "contract"
    config = root / "_custom.toml"
    config.write_text(
        f'[workspaces.ws]\npath = "{root}"\n'
        "[workspaces.ws.hooks.on]\n"
        f'post-create = "printf %s:%s:%s $WTM_EVENT $WTM_PROJECT $WTM_WORKSPACE > {out}"\n'
    )
    res = run("create", "alpha", "feat", root=root, extra_env={"WTM_CONFIG": str(config)})
    assert res.returncode == 0, res.stderr
    assert out.read_text() == "post-create:alpha:ws"


def test_inline_hook_strict_veto_aborts_create(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    config = root / "_custom.toml"
    config.write_text(
        f'[workspaces.ws]\npath = "{root}"\n[workspaces.ws.hooks.on]\npre-create = "exit 3"\n'
    )
    res = run(
        "create", "alpha", "feat", "--strict", root=root, extra_env={"WTM_CONFIG": str(config)}
    )
    assert res.returncode == 1
    assert not (root / "alpha" / "worktrees" / "feat").exists()


def test_hook_list_shows_inline(root: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    config = _config_with_inline_hook(root, "enter", "true")
    res = run("hook", "list", "alpha", "feat", root=root, extra_env={"WTM_CONFIG": str(config)})
    assert res.returncode == 0, res.stderr
    assert "(config) true" in res.stdout


# --- configured base branch (integration) ------------------------------------


def test_configured_base_branch_used_for_new_branch(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")  # has a 'main' branch with a commit
    # Make a 'develop' branch to fork from.
    git_dir = root / "alpha" / ".git"
    wt = root / "alpha" / "worktrees" / "seed"
    subprocess.run(
        ["git", "-C", str(git_dir), "worktree", "add", "-q", str(wt), "-b", "develop"], check=True
    )
    subprocess.run(["git", "-C", str(git_dir), "worktree", "remove", str(wt)], check=True)
    config = root / "_custom.toml"
    config.write_text(
        f'[workspaces.ws]\npath = "{root}"\n'
        '[workspaces.ws.projects.alpha]\nbase_branch = "develop"\n'
    )
    res = run("create", "alpha", "feat", root=root, extra_env={"WTM_CONFIG": str(config)})
    assert res.returncode == 0, res.stderr
    assert "off 'develop'" in res.stderr


def test_base_flag_overrides_configured(root: Path) -> None:
    _seed_repo(root, "alpha")
    config = root / "_custom.toml"
    config.write_text(
        f'[workspaces.ws]\npath = "{root}"\n[workspaces.ws.projects.alpha]\nbase_branch = "nope"\n'
    )
    res = run(
        "create",
        "alpha",
        "feat",
        "--base",
        "main",
        root=root,
        extra_env={"WTM_CONFIG": str(config)},
    )
    assert res.returncode == 0, res.stderr
    # A configured/requested base prefers the tracked upstream (origin/main).
    assert "off 'origin/main'" in res.stderr


def test_unresolvable_configured_base_errors(root: Path) -> None:
    _seed_repo(root, "alpha")  # populated repo
    config = root / "_custom.toml"
    config.write_text(
        f'[workspaces.ws]\npath = "{root}"\n'
        '[workspaces.ws.projects.alpha]\nbase_branch = "ghost"\n'
    )
    res = run("create", "alpha", "feat", root=root, extra_env={"WTM_CONFIG": str(config)})
    assert res.returncode == 1
    assert "base ref does not exist" in res.stderr


def test_no_hooks_env_disables_firing(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    hooks = tmp_path / "hooks"
    out = tmp_path / "ran"
    _write_hook(hooks / "wtm-post-create", f'echo ran >> "{out}"\n')
    res = run(
        "create",
        "alpha",
        "feat",
        root=root,
        hooks_dir=hooks,
        extra_env={"WTM_NO_HOOKS": "1"},
    )
    assert res.returncode == 0, res.stderr
    assert not out.exists()  # WTM_NO_HOOKS suppressed the builtin hook


# --- workspace add / set / rename / delete / default / show (CLI wiring) ------
#
# The run() harness rewrites WTM_CONFIG on every call, so these assert the
# single-invocation effect by reading that file back after the call. The file
# always starts as '[workspaces.ws] path = <root>' (the sole declared workspace).


def _config_text(root: Path) -> str:
    """Read back the user config the harness wrote (and the command mutated)."""
    return (root / "_wtm_config.toml").read_text()


def test_workspace_add_writes_entry(root: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    res = run("workspace", "add", "extra", str(other), "-d", "notes", root=root)
    assert res.returncode == 0, res.stderr
    assert "registered 'extra'" in res.stderr
    text = _config_text(root)
    assert "[workspaces.extra]" in text
    assert 'description = "notes"' in text
    assert "[workspaces.ws]" in text  # existing entry preserved


def test_workspace_add_create_makes_dir(root: Path, tmp_path: Path) -> None:
    target = tmp_path / "fresh"
    res = run("workspace", "add", "f", str(target), "-c", root=root)
    assert res.returncode == 0, res.stderr
    assert target.is_dir()


def test_workspace_add_missing_path_warns(root: Path, tmp_path: Path) -> None:
    res = run("workspace", "add", "f", str(tmp_path / "ghost"), root=root)
    assert res.returncode == 0, res.stderr
    assert "does not exist yet" in res.stderr


def test_workspace_add_duplicate_user_name_errors(root: Path) -> None:
    # 'ws' is already declared by the harness config -> user-scope collision.
    res = run("workspace", "add", "ws", str(root), root=root)
    assert res.returncode == 1
    assert "already declared" in res.stderr


def test_workspace_set_edits_entry(root: Path) -> None:
    # The 'ws' dir exists, so scalar edits land in its workspace-local file.
    res = run("workspace", "set", "ws", "-d", "edited", root=root)
    assert res.returncode == 0, res.stderr
    assert 'description = "edited"' in (root / ".wtm" / "config.toml").read_text()


def test_workspace_set_scope_user_writes_registry(root: Path) -> None:
    # Explicit -s user routes scalars to the user-scope registry entry, even
    # though the workspace dir exists (which would auto-default to workspace).
    res = run("workspace", "set", "ws", "-d", "edited", "-s", "user", root=root)
    assert res.returncode == 0, res.stderr
    assert "[user]" in res.stderr
    assert 'description = "edited"' in _config_text(root)
    local = root / ".wtm" / "config.toml"
    assert not local.exists() or "edited" not in local.read_text()


def test_workspace_set_scope_rejects_project(root: Path) -> None:
    res = run("workspace", "set", "ws", "-d", "x", "-s", "project", root=root)
    assert res.returncode == 1
    assert "project" in res.stderr.lower()


def test_config_base_branch_set_show_clear(root: Path) -> None:
    # A test-owned config file so writes persist across invocations (the default
    # harness config is rewritten on every run).
    cfg = root / "cfg.toml"
    cfg.write_text(f'[workspaces.ws]\npath = "{root}"\n')
    env = {"WTM_CONFIG": str(cfg)}
    assert "(none set)" in run("config", "base-branch", root=root, extra_env=env).stdout
    assert run("config", "base-branch", "main", root=root, extra_env=env).returncode == 0
    assert 'base_branch = "main"' in cfg.read_text()
    assert "main [user]" in run("config", "base-branch", root=root, extra_env=env).stdout
    assert run("config", "base-branch", "--clear", root=root, extra_env=env).returncode == 0
    assert "base_branch =" not in cfg.read_text()


# --- config set/get typed values (opt-in scalar writes) -----------------------


def test_config_set_typed_scalar_writes_native_kind(root: Path) -> None:
    cfg = root / "cfg.toml"
    cfg.write_text(f'[workspaces.ws]\npath = "{root}"\n')
    env = {"WTM_CONFIG": str(cfg)}
    assert (
        run("config", "set", "-g", "port", "8080", "--type", "int", root=root, extra_env=env)
    ).returncode == 0
    assert (
        run(
            "config",
            "set",
            "-g",
            "when",
            "1979-05-27T07:32:00Z",
            "-x",
            root=root,
            extra_env=env,
        )
    ).returncode == 0
    text = cfg.read_text()
    assert "port = 8080" in text
    assert "when = 1979-05-27T07:32:00Z" in text
    # Bare writes stay strings, whatever the value looks like.
    assert run("config", "set", "-g", "ver", "1.20", root=root, extra_env=env).returncode == 0
    text = cfg.read_text()
    assert 'ver = "1.20"' in text


def test_config_get_renders_and_json_encodes_typed_values(root: Path) -> None:
    cfg = root / "cfg.toml"
    # Scalars go above the [workspaces] header so they are true global-tier keys.
    cfg.write_text(f'port = 8080\nflag = true\n[workspaces.ws]\npath = "{root}"\n')
    env = {"WTM_CONFIG": str(cfg)}
    res = run("config", "get", "-g", "flag", root=root, extra_env=env)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "true"
    res = run("config", "get", "-g", "port", "-j", root=root, extra_env=env)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "8080"
    res = run("config", "get", "-g", "absent", "-j", root=root, extra_env=env)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "null"


def test_config_set_type_bool_truthiness_tokens(root: Path) -> None:
    cfg = root / "cfg.toml"
    cfg.write_text(f'[workspaces.ws]\npath = "{root}"\n')
    env = {"WTM_CONFIG": str(cfg)}
    for token in ("YES", "on", "1"):
        assert (
            run("config", "set", "-g", "flag", token, "--type", "bool", root=root, extra_env=env)
        ).returncode == 0
        assert "flag = true" in cfg.read_text()
    res = run("config", "set", "-g", "flag", "maybe", "--type", "bool", root=root, extra_env=env)
    assert res.returncode != 0
    assert "recognized" in (res.stderr + res.stdout)


def test_config_set_typed_flag_guards(root: Path) -> None:
    cfg = root / "cfg.toml"
    cfg.write_text(f'[workspaces.ws]\npath = "{root}"\n')
    env = {"WTM_CONFIG": str(cfg)}

    def set_res(*args: str) -> _Result:
        return run("config", "set", *args, root=root, extra_env=env)

    res = set_res("-g", "k", "v", "--type", "int", "--toml")
    assert res.returncode != 0 and "--toml are mutually exclusive" in res.stderr
    res = set_res("-g", "k", "v", "--type", "decimal")
    assert res.returncode != 0 and "unknown --type" in res.stderr
    res = set_res("-g", "k", "[1,2]", "--toml")
    assert res.returncode != 0 and "not writable yet" in res.stderr
    res = set_res("-g", "inheritable", "ci", "-a", "--type", "int")
    assert res.returncode != 0 and "key declarations" in res.stderr


def test_config_set_unset_typed_array_elements(root: Path) -> None:
    cfg = root / "cfg.toml"
    cfg.write_text(f'[workspaces.ws]\npath = "{root}"\n')
    env = {"WTM_CONFIG": str(cfg)}
    assert (
        run("config", "set", "-g", "nums", "8080", "--type", "int", "-a", root=root, extra_env=env)
    ).returncode == 0
    assert (
        run("config", "set", "-g", "nums", "8080", "-a", root=root, extra_env=env)
    ).returncode == 0
    assert 'nums = [8080, "8080"]' in cfg.read_text()
    # Removal matches by native kind: the typed element goes, the string stays.
    res = run(
        "config", "unset", "-g", "nums", "8080", "--type", "int", "-a", root=root, extra_env=env
    )
    assert res.returncode == 0
    assert 'nums = ["8080"]' in cfg.read_text()


def test_config_set_typed_table_cell(root: Path) -> None:
    cfg = root / "cfg.toml"
    cfg.write_text(f'[workspaces.ws]\npath = "{root}"\n')
    env = {"WTM_CONFIG": str(cfg)}
    res = run(
        "config",
        "set",
        "-g",
        "limits",
        "cpu",
        "4",
        "--type",
        "int",
        "-t",
        root=root,
        extra_env=env,
    )
    assert res.returncode == 0, res.stderr
    assert "cpu = 4" in cfg.read_text()
    res = run("config", "get", "-g", "limits", "-j", root=root, extra_env=env)
    assert res.stdout.strip() == '{"cpu": 4}'


def test_config_show_reports_base_branch_source(root: Path) -> None:
    cfg = root / "cfg.toml"
    cfg.write_text(f'base_branch = "main"\n[workspaces.ws]\npath = "{root}"\n')
    res = run("config", "show", "--origin", root=root, extra_env={"WTM_CONFIG": str(cfg)})
    assert res.returncode == 0, res.stderr
    assert "default base_branch: main  [user]" in res.stdout


def test_config_show_workspace_base_branch_origin(root: Path) -> None:
    # A workspace-tier base_branch is annotated with its declaring scope under
    # --origin, mirroring the global and project tiers (Q6: ungated). The
    # workspace-local file is a different scope from the workspace's declaration
    # scope (user), so this also guards against regressing to ws.entry.scope.
    cfg = root / "cfg.toml"
    cfg.write_text(f'[workspaces.ws]\npath = "{root}"\n')
    env = {"WTM_CONFIG": str(cfg)}
    assert run("workspace", "set", "ws", "-b", "develop", root=root, extra_env=env).returncode == 0
    res = run("config", "show", "--origin", root=root, extra_env=env)
    assert res.returncode == 0, res.stderr
    assert "base_branch: develop  [workspace]" in res.stdout


def test_config_show_surfaces_cwd_project_local_base_branch(root: Path) -> None:
    # A project-local base_branch is invisible in the workspace listing but must
    # surface (with a [project] source) when the cwd is inside that project.
    _seed_repo(root, "proj")
    run("project", "set", "proj", "-b", "develop", root=root)
    res = run("config", "show", "--origin", root=root, cwd=root / "proj")
    assert res.returncode == 0, res.stderr
    assert "project proj:" in res.stdout
    assert "base_branch: develop  [project]" in res.stdout


def test_config_show_no_descent_from_workspace_root(root: Path) -> None:
    # Standing at the workspace root, no project block is rendered even when a
    # project carries local settings.
    _seed_repo(root, "proj")
    run("project", "set", "proj", "-b", "develop", root=root)
    res = run("config", "show", root=root, cwd=root)
    assert res.returncode == 0, res.stderr
    assert "project proj:" not in res.stdout


def test_config_show_worktree_tier_only_when_cwd_inside(root: Path) -> None:
    # A worktree-local hook shows under --hooks only when cwd is in that worktree.
    _seed_repo(root, "proj")
    run("create", "proj", "feat", root=root)
    wt = root / "proj" / "worktrees" / "feat"
    (wt / ".wtm").mkdir(parents=True, exist_ok=True)
    (wt / ".wtm" / "config.toml").write_text('[hooks.on]\nenter = "echo hi"\n')

    from_wt = run("config", "show", "--hooks", "--origin", root=root, cwd=wt)
    assert from_wt.returncode == 0, from_wt.stderr
    assert "worktree feat:" in from_wt.stdout
    assert "hook enter = echo hi  [worktree]" in from_wt.stdout

    from_proj = run("config", "show", "--hooks", root=root, cwd=root / "proj")
    assert "worktree feat:" not in from_proj.stdout


def test_config_show_arbitrary_keys_shown_in_cwd_descent(root: Path) -> None:
    # Arbitrary keys resolved along the cwd descent are always shown (no flag),
    # for the project and its worktree tiers.
    _seed_repo(root, "proj")
    run("config", "set", "editor", "nvim", root=root, cwd=root / "proj")
    run("create", "proj", "feat", root=root)
    wt = root / "proj" / "worktrees" / "feat"
    run("config", "set", "runner", "pytest", root=root, cwd=wt)

    from_proj = run("config", "show", root=root, cwd=root / "proj")
    assert "editor: nvim" in from_proj.stdout

    from_wt = run("config", "show", root=root, cwd=wt)
    assert "runner: pytest" in from_wt.stdout
    # The worktree descent still surfaces the project-tier key too.
    assert "editor: nvim" in from_wt.stdout


def test_config_show_origin_scalar_extra(root: Path) -> None:
    # `config show --origin` annotates a scalar arbitrary key with its source
    # tier, like it already does for base_branch/headers (terse [label]).
    _seed_repo(root, "proj")
    cwd = root / "proj"
    (cwd / ".wtm").mkdir(parents=True, exist_ok=True)
    (cwd / ".wtm" / "config.toml").write_text('editor = "nvim"\n')
    res = run("config", "show", "--origin", root=root, cwd=cwd)
    assert res.returncode == 0, res.stderr
    assert "editor: nvim  [project]" in res.stdout
    # Without --origin the extra stays label-free (no provenance appended).
    plain = run("config", "show", root=root, cwd=cwd)
    assert "editor: nvim\n" in plain.stdout
    assert "[project]" not in plain.stdout


def test_config_show_origin_per_cell_for_merged_table(root: Path) -> None:
    # A table extra deep-merged across locality: each cell is annotated with its
    # own source tier under --origin (per-cell, mirroring `config get --origin`),
    # keeping the `config show` colon tree rather than the get command's `=` form.
    _seed_repo(root, "proj")
    cwd = root / "proj"
    (cwd / ".wtm").mkdir(parents=True, exist_ok=True)
    (cwd / ".wtm" / "config.toml").write_text('[ci]\nregistry = "ghcr"\n')
    (root / ".wtm").mkdir(parents=True, exist_ok=True)
    (root / ".wtm" / "config.toml").write_text('[projects.proj.ci]\nrunner = "ubuntu"\n')
    res = run("config", "show", "--origin", root=root, cwd=cwd)
    assert res.returncode == 0, res.stderr
    assert "ci:" in res.stdout
    assert "registry: ghcr  [project]" in res.stdout
    assert "runner: ubuntu  [workspace]" in res.stdout


def test_config_show_origin_per_cell_for_nested_table(root: Path) -> None:
    # Per-cell provenance recurses into nested sub-tables: each leaf keeps its
    # own tier, indented under its sub-table header.
    _seed_repo(root, "proj")
    cwd = root / "proj"
    (cwd / ".wtm").mkdir(parents=True, exist_ok=True)
    (cwd / ".wtm" / "config.toml").write_text('[ci.runner]\nimage = "ubuntu"\n')
    (root / ".wtm").mkdir(parents=True, exist_ok=True)
    (root / ".wtm" / "config.toml").write_text('[projects.proj.ci.runner]\ntag = "latest"\n')
    res = run("config", "show", "--origin", root=root, cwd=cwd)
    assert res.returncode == 0, res.stderr
    assert "ci:" in res.stdout
    assert "runner:" in res.stdout
    assert "image: ubuntu  [project]" in res.stdout
    assert "tag: latest  [workspace]" in res.stdout


def test_config_show_origin_inherited_table_per_cell(root: Path) -> None:
    # An inheritable table merges down the containment chain; each cell keeps its
    # own defining tier under --origin (the project cell vs the workspace-ancestor
    # cell), not a single section-level label. Inherited cells are suppressed at
    # the descendant, so the project block carries only its own cell.
    cfg = root / "cfg.toml"
    cfg.write_text(f'inheritable = ["ci"]\n[workspaces.ws]\npath = "{root}"\n')
    _seed_repo(root, "proj")
    cwd = root / "proj"
    (cwd / ".wtm").mkdir(parents=True, exist_ok=True)
    (cwd / ".wtm" / "config.toml").write_text('[ci]\nregistry = "ghcr"\n')
    (root / ".wtm").mkdir(parents=True, exist_ok=True)
    (root / ".wtm" / "config.toml").write_text('[ci]\nrunner = "ubuntu"\n')
    env = {"WTM_CONFIG": str(cfg)}
    res = run("config", "show", "--origin", root=root, cwd=cwd, extra_env=env)
    assert res.returncode == 0, res.stderr
    assert "ci:" in res.stdout
    assert "registry: ghcr  [project]" in res.stdout
    assert "runner: ubuntu  [workspace]" in res.stdout
    # The workspace-inherited cell is not echoed at the project tier.
    proj_block = res.stdout.split("project proj:", 1)[1]
    assert "runner" not in proj_block


def test_config_show_origin_global_extra(root: Path) -> None:
    # A global-tier arbitrary key is annotated with its declaring scope
    # (user/system), the global node having no ancestor to inherit from.
    cfg = root / "cfg.toml"
    cfg.write_text(f'editor = "nvim"\n[workspaces.ws]\npath = "{root}"\n')
    res = run("config", "show", "--origin", root=root, extra_env={"WTM_CONFIG": str(cfg)})
    assert res.returncode == 0, res.stderr
    assert "editor: nvim  [user]" in res.stdout


def test_config_show_hooks_flag_gates_hook_output(root: Path) -> None:
    _seed_repo(root, "proj")
    run("project", "set", "proj", "-b", "develop", root=root)
    (root / "proj" / ".wtm" / "config.toml").write_text(
        'base_branch = "develop"\n[hooks.on]\ninstall = "make deps"\n'
    )
    without = run("config", "show", root=root, cwd=root / "proj")
    assert "hook install" not in without.stdout
    with_hooks = run("config", "show", "--hooks", root=root, cwd=root / "proj")
    assert "hook install = make deps" in with_hooks.stdout


def test_config_show_all_descends_every_project_and_worktree(root: Path) -> None:
    # Unlike the default cwd view, -g -r fully descends every project and every
    # worktree from the workspace root, regardless of cwd.
    _seed_repo(root, "proj")
    run("create", "proj", "feat", root=root)
    run("project", "set", "proj", "-b", "develop", root=root)
    res = run("config", "show", "-g", "-r", root=root, cwd=root)
    assert res.returncode == 0, res.stderr
    assert "workspace" in res.stdout
    assert "project proj:" in res.stdout
    assert "base_branch: develop" in res.stdout
    assert "worktree feat:" in res.stdout
    assert "worktree main:" not in res.stdout


def test_config_show_all_surfaces_inert_and_declared_worktree(root: Path) -> None:
    # -g -r unions on-disk with declared: an inert (no .git) project and a
    # declared-only worktree both appear.
    _seed_repo(root, "proj")
    (root / ".wtm").mkdir(parents=True, exist_ok=True)
    (root / ".wtm" / "config.toml").write_text(
        '[projects.ghost]\ndescription = "gone"\n'
        '[projects.proj.worktrees.planned]\ndescription = "not yet"\n'
    )
    res = run("config", "show", "-g", "-r", root=root, cwd=root)
    assert res.returncode == 0, res.stderr
    assert "project ghost:" in res.stdout
    assert "(no .git on disk)" in res.stdout
    assert "worktree planned:" in res.stdout


def test_config_show_project_from_cwd_and_worktree_base_branch_carveout(root: Path) -> None:
    # Bare config show roots at the cwd's project and (with -r) descends its
    # worktrees; the base_branch carve-out means no base_branch line ever prints
    # on a worktree.
    _seed_repo(root, "proj")
    run("create", "proj", "feat", root=root)
    run("project", "set", "proj", "-b", "develop", root=root)
    res = run("config", "show", "-r", root=root, cwd=root / "proj")
    assert res.returncode == 0, res.stderr
    assert "project proj:" in res.stdout
    assert "base_branch: develop" in res.stdout
    # The worktree block carries no base_branch line.
    wt_block = res.stdout.split("worktree feat:", 1)[1]
    assert "base_branch" not in wt_block


def test_config_show_worktree_by_name(root: Path) -> None:
    _seed_repo(root, "proj")
    run("create", "proj", "feat", root=root)
    res = run("config", "show", "proj", "feat", root=root, cwd=root)
    assert res.returncode == 0, res.stderr
    assert "worktree feat:" in res.stdout


def test_config_show_all_json_is_nested_tree(root: Path) -> None:
    _seed_repo(root, "proj")
    run("create", "proj", "feat", root=root)
    run("project", "set", "proj", "-b", "develop", root=root)
    res = run("config", "show", "-g", "-r", "--json", root=root, cwd=root)
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    # The single seeded workspace holds proj -> feat.
    (ws_node,) = payload["workspaces"].values()
    proj_node = ws_node["projects"]["proj"]
    assert proj_node["base_branch"] == "develop"
    assert set(proj_node["worktrees"]) == {"feat"}
    # base_branch carve-out: worktree nodes never carry base_branch.
    assert "base_branch" not in proj_node["worktrees"]["feat"]


def test_config_show_project_json_extra_nested(root: Path) -> None:
    _seed_repo(root, "proj")
    run("config", "set", "editor", "nvim", root=root, cwd=root / "proj")
    res = run("config", "show", "--json", root=root, cwd=root / "proj")
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    proj_node = payload["workspaces"]["ws"]["projects"]["proj"]
    assert proj_node["extra"] == {"editor": "nvim"}


def test_config_show_json_orthogonal_on_default_view(root: Path) -> None:
    # --json works on the plain (no-level) cwd view too.
    _seed_repo(root, "proj")
    res = run("config", "show", "--json", root=root, cwd=root / "proj")
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    assert "ws" in payload["workspaces"]
    assert "proj" in payload["workspaces"]["ws"]["projects"]


def test_config_show_global_with_locator_errors(root: Path) -> None:
    res = run("config", "show", "-g", "foo", root=root, cwd=root)
    assert res.returncode == 1
    assert "cannot be combined with a locator" in res.stderr


def test_config_show_json_with_origin_errors(root: Path) -> None:
    res = run("config", "show", "--json", "--origin", root=root, cwd=root)
    assert res.returncode == 1
    assert "cannot be combined" in res.stderr


def test_config_show_json_with_hooks_errors(root: Path) -> None:
    res = run("config", "show", "--json", "--hooks", root=root, cwd=root)
    assert res.returncode == 1
    assert "cannot be combined" in res.stderr


def test_config_show_no_workspaces_registry_leak(root: Path) -> None:
    # The global [workspaces] registry table is reserved: it must not leak into
    # global_extra as a raw ``workspaces: {...}`` dict line. Only the pruned
    # workspace node for the target renders.
    other = root.parent / "other_ws"
    other.mkdir()
    cfg = root / "cfg.toml"
    cfg.write_text(f'[workspaces.ws]\npath = "{root}"\n[workspaces.other]\npath = "{other}"\n')
    _seed_repo(root, "proj")
    res = run("config", "show", root=root, cwd=root / "proj", extra_env={"WTM_CONFIG": str(cfg)})
    assert res.returncode == 0, res.stderr
    assert not any(line.startswith("workspaces:") for line in res.stdout.splitlines())
    assert "workspace ws:" in res.stdout


def test_config_show_json_no_workspaces_in_extra(root: Path) -> None:
    # JSON mirrors text: the registry table is structured under ``workspaces``,
    # never dumped into the global ``extra`` payload.
    other = root.parent / "other_ws"
    other.mkdir()
    cfg = root / "cfg.toml"
    cfg.write_text(f'[workspaces.ws]\npath = "{root}"\n[workspaces.other]\npath = "{other}"\n')
    _seed_repo(root, "proj")
    res = run(
        "config",
        "show",
        "-g",
        "-r",
        "--json",
        root=root,
        cwd=root,
        extra_env={"WTM_CONFIG": str(cfg)},
    )
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    assert "workspaces" in payload  # the structured node
    assert "workspaces" not in payload.get("extra", {})


def test_config_show_json_no_base_branch_source(root: Path) -> None:
    # --json is effective-values-only (per Q13): the global default base_branch
    # ships as ``default_base_branch``, but its provenance (``base_branch_source``)
    # is text-only (``--origin``) and never appears in JSON.
    cfg = root / "cfg.toml"
    cfg.write_text(f'base_branch = "main"\n[workspaces.ws]\npath = "{root}"\n')
    _seed_repo(root, "proj")
    res = run("config", "show", "--json", root=root, cwd=root, extra_env={"WTM_CONFIG": str(cfg)})
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    assert payload["default_base_branch"] == "main"
    assert "base_branch_source" not in payload


def test_config_set_workspaces_refused(root: Path) -> None:
    # ``workspaces`` is the global registry table (reserved): the arbitrary-key
    # surface refuses it, pointing at the dedicated ``wtm workspace`` verbs.
    _seed_repo(root, "proj")
    res = run("config", "set", "workspaces", "bar", root=root, cwd=root / "proj")
    assert res.returncode == 1
    assert "reserved key" in res.stderr


def test_config_show_global_alone_is_global_tier_only(root: Path) -> None:
    # -g with no locator targets the global tier; depth caps at 0, so no
    # workspace block is rendered even when a project exists on disk.
    cfg = root / "cfg.toml"
    cfg.write_text(f'base_branch = "main"\n[workspaces.ws]\npath = "{root}"\n')
    _seed_repo(root, "proj")
    res = run("config", "show", "-g", root=root, cwd=root, extra_env={"WTM_CONFIG": str(cfg)})
    assert res.returncode == 0, res.stderr
    assert "default base_branch: main" in res.stdout
    assert "workspace ws:" not in res.stdout
    assert "project proj:" not in res.stdout


def test_config_show_depth_project_prunes_worktree_from_worktree_cwd(root: Path) -> None:
    # -d project from a worktree cwd caps the deepest tier at project (2), so the
    # worktree tier is pruned while project + workspace + global remain. This
    # exercises the DepthTier enum -> depth wiring through the CLI.
    _seed_repo(root, "proj")
    run("create", "proj", "feat", root=root)
    wt = root / "proj" / "worktrees" / "feat"
    res = run("config", "show", "-d", "project", root=root, cwd=wt)
    assert res.returncode == 0, res.stderr
    assert "workspace ws:" in res.stdout
    assert "project proj:" in res.stdout
    assert "worktree feat:" not in res.stdout


def test_config_show_depth_global_caps_at_global(root: Path) -> None:
    # -d global (0) from a project cwd prunes the workspace and project tiers,
    # leaving only the global tier (the target itself is capped when shallower).
    cfg = root / "cfg.toml"
    cfg.write_text(f'base_branch = "main"\n[workspaces.ws]\npath = "{root}"\n')
    _seed_repo(root, "proj")
    res = run(
        "config",
        "show",
        "-d",
        "global",
        root=root,
        cwd=root / "proj",
        extra_env={"WTM_CONFIG": str(cfg)},
    )
    assert res.returncode == 0, res.stderr
    assert "default base_branch: main" in res.stdout
    assert "workspace ws:" not in res.stdout
    assert "project proj:" not in res.stdout


def test_config_show_recurse_with_depth_caps_subtree(root: Path) -> None:
    # myproj -r -d project: the project's subtree would expand with -r, but the
    # -d project cap prunes every worktree, leaving global -> workspace -> project.
    _seed_repo(root, "proj")
    run("create", "proj", "feat", root=root)
    res = run("config", "show", "proj", "-r", "-d", "project", root=root, cwd=root)
    assert res.returncode == 0, res.stderr
    assert "workspace ws:" in res.stdout
    assert "project proj:" in res.stdout
    assert "worktree feat:" not in res.stdout


def test_config_show_depth_below_target_is_silent_no_op(root: Path) -> None:
    # -d worktree (3) on a project target (2) without -r changes nothing: the
    # default depth is already the target tier, and without -r no worktrees
    # expand anyway. The output is identical to omitting -d.
    _seed_repo(root, "proj")
    run("create", "proj", "feat", root=root)
    res_capped = run("config", "show", "proj", "-d", "worktree", root=root, cwd=root)
    res_plain = run("config", "show", "proj", root=root, cwd=root)
    assert res_capped.returncode == 0, res_capped.stderr
    assert res_capped.stdout == res_plain.stdout


def test_config_show_worktree_target_recurse_is_no_op(root: Path) -> None:
    # A worktree is the leaf tier, so -r has nothing below to expand into: the
    # output matches the plain (no -r) worktree-target view.
    _seed_repo(root, "proj")
    run("create", "proj", "feat", root=root)
    res_recurse = run("config", "show", "proj", "feat", "-r", root=root, cwd=root)
    res_plain = run("config", "show", "proj", "feat", root=root, cwd=root)
    assert res_recurse.returncode == 0, res_recurse.stderr
    assert "worktree feat:" in res_recurse.stdout
    assert res_recurse.stdout == res_plain.stdout


def test_config_show_default_view_unchanged(root: Path) -> None:
    # From the workspace root, the pruned tree lists workspaces without descending
    # into the cwd project's worktrees.
    _seed_repo(root, "proj")
    run("create", "proj", "feat", root=root)
    res = run("config", "show", root=root, cwd=root)
    assert res.returncode == 0, res.stderr
    assert "workspace" in res.stdout
    assert "worktree feat:" not in res.stdout  # no descent from the ws root


def test_project_set_writes_local_config(root: Path) -> None:
    _seed_repo(root, "proj")
    res = run("project", "set", "proj", "-b", "develop", "-d", "the proj", root=root)
    assert res.returncode == 0, res.stderr
    local = (root / "proj" / ".wtm" / "config.toml").read_text()
    assert 'base_branch = "develop"' in local
    assert 'description = "the proj"' in local


def test_project_set_scope_workspace_writes_ws_local(root: Path) -> None:
    # -s workspace lands the annotation in the workspace-local [projects.proj].
    _seed_repo(root, "proj")
    res = run("project", "set", "proj", "-b", "develop", "-s", "workspace", root=root)
    assert res.returncode == 0, res.stderr
    assert "[workspace]" in res.stderr
    text = (root / ".wtm" / "config.toml").read_text()
    assert "[projects.proj]" in text
    assert 'base_branch = "develop"' in text
    assert not (root / "proj" / ".wtm" / "config.toml").exists()


def test_project_set_scope_user_writes_registry(root: Path) -> None:
    _seed_repo(root, "proj")
    res = run("project", "set", "proj", "-b", "develop", "-s", "user", root=root)
    assert res.returncode == 0, res.stderr
    assert "[user]" in res.stderr
    assert "[workspaces.ws.projects.proj]" in _config_text(root)


def test_project_set_scope_rejects_worktree(root: Path) -> None:
    _seed_repo(root, "proj")
    res = run("project", "set", "proj", "-b", "develop", "-s", "worktree", root=root)
    assert res.returncode == 1
    assert "worktree" in res.stderr.lower()


def test_project_show_outputs_detail(root: Path) -> None:
    _seed_repo(root, "proj")
    run("project", "set", "proj", "-b", "develop", root=root)
    res = run("project", "show", "proj", root=root)
    assert res.returncode == 0, res.stderr
    assert "name: proj" in res.stdout
    assert "effective base_branch: develop [project]" in res.stdout


def test_worktree_set_writes_local_config(root: Path) -> None:
    # A created worktree exists on disk, so the default scope is worktree-local.
    _seed_repo(root, "proj")
    assert run("create", "proj", "feat", root=root).returncode == 0
    res = run("worktree", "set", "proj", "feat", "-d", "the feature", root=root)
    assert res.returncode == 0, res.stderr
    assert "[worktree]" in res.stderr
    local = (root / "proj" / "worktrees" / "feat" / ".wtm" / "config.toml").read_text()
    assert 'description = "the feature"' in local


def test_worktree_set_scope_project_writes_project_local(root: Path) -> None:
    _seed_repo(root, "proj")
    assert run("create", "proj", "feat", root=root).returncode == 0
    res = run("worktree", "set", "proj", "feat", "-d", "planned", "-s", "project", root=root)
    assert res.returncode == 0, res.stderr
    text = (root / "proj" / ".wtm" / "config.toml").read_text()
    assert "[worktrees.feat]" in text
    assert 'description = "planned"' in text


def test_worktree_set_scope_workspace_writes_ws_local(root: Path) -> None:
    _seed_repo(root, "proj")
    assert run("create", "proj", "feat", root=root).returncode == 0
    res = run("worktree", "set", "proj", "feat", "-d", "deep", "-s", "workspace", root=root)
    assert res.returncode == 0, res.stderr
    text = (root / ".wtm" / "config.toml").read_text()
    assert "[projects.proj.worktrees.feat]" in text
    assert 'description = "deep"' in text


def test_worktree_set_scope_user_writes_registry(root: Path) -> None:
    _seed_repo(root, "proj")
    assert run("create", "proj", "feat", root=root).returncode == 0
    res = run("worktree", "set", "proj", "feat", "-d", "global", "-s", "user", root=root)
    assert res.returncode == 0, res.stderr
    assert "[workspaces.ws.projects.proj.worktrees.feat]" in _config_text(root)


def test_worktree_set_requires_a_field(root: Path) -> None:
    _seed_repo(root, "proj")
    assert run("create", "proj", "feat", root=root).returncode == 0
    res = run("worktree", "set", "proj", "feat", root=root)
    assert res.returncode == 1
    assert "nothing to set" in res.stderr


def test_worktree_set_has_no_top_level_shortcut(root: Path) -> None:
    # 'set' is worktree-namespaced only (unlike enter/create/etc.).
    _seed_repo(root, "proj")
    res = run("set", "proj", "feat", "-d", "x", root=root)
    assert res.returncode != 0


# --- arbitrary-key config set / get / unset (cwd-inferred entity) ---


def test_config_set_get_project_local(root: Path) -> None:
    # From inside a project, the arbitrary key lands in the project-local file
    # and reads back through the cascade.
    _seed_repo(root, "proj")
    res = run("config", "set", "editor", "nvim", root=root, cwd=root / "proj")
    assert res.returncode == 0, res.stderr
    assert "[project]" in res.stderr
    assert 'editor = "nvim"' in (root / "proj" / ".wtm" / "config.toml").read_text()
    got = run("config", "get", "editor", root=root, cwd=root / "proj")
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip().splitlines()[0] == "nvim"


def test_config_set_scope_workspace_from_project_cwd(root: Path) -> None:
    # -s workspace lands the project's key in the workspace-local [projects.proj].
    _seed_repo(root, "proj")
    res = run("config", "set", "editor", "emacs", "-s", "workspace", root=root, cwd=root / "proj")
    assert res.returncode == 0, res.stderr
    text = (root / ".wtm" / "config.toml").read_text()
    assert "[projects.proj]" in text
    assert 'editor = "emacs"' in text


def test_config_get_sources_shows_full_chain(root: Path) -> None:
    # A key set at two tiers: --origin lists both, most-local first, and the
    # most-local one wins. Each rung also names the file it lives in.
    _seed_repo(root, "proj")
    run("config", "set", "editor", "vi", "-s", "workspace", root=root, cwd=root / "proj")
    run("config", "set", "editor", "nvim", "-s", "project", root=root, cwd=root / "proj")
    res = run("config", "get", "editor", "--origin", root=root, cwd=root / "proj")
    assert res.returncode == 0, res.stderr
    assert "nvim  [project]" in res.stdout
    assert "vi  [workspace]" in res.stdout
    assert res.stdout.index("[project]") < res.stdout.index("[workspace]")
    # The file each value lives in is shown too.
    assert str(root / "proj" / ".wtm" / "config.toml") in res.stdout
    assert str(root / ".wtm" / "config.toml") in res.stdout


def test_config_get_base_branch_reports_inherited_value(root: Path) -> None:
    # `config get base_branch` from a project reports the inherited workspace-own
    # default a real `create` would use -- not `(unset)`.
    cfg = root / "cfg.toml"
    cfg.write_text(f'[workspaces.ws]\npath = "{root}"\nbase_branch = "develop"\n')
    _seed_repo(root, "proj")
    res = run(
        "config",
        "get",
        "base_branch",
        root=root,
        cwd=root / "proj",
        extra_env={"WTM_CONFIG": str(cfg)},
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "develop"


def test_config_get_base_branch_sources_lists_inheritance_tiers(root: Path) -> None:
    # --origin labels each rung by its definition level (project/workspace/
    # global), most-local-first, so the two `user`-file rungs read distinctly.
    cfg = root / "cfg.toml"
    cfg.write_text(
        f'base_branch = "main"\n'
        f'[workspaces.ws]\npath = "{root}"\nbase_branch = "develop"\n'
        f'[workspaces.ws.projects.proj]\nbase_branch = "feature"\n'
    )
    _seed_repo(root, "proj")
    res = run(
        "config",
        "get",
        "base_branch",
        "--origin",
        root=root,
        cwd=root / "proj",
        extra_env={"WTM_CONFIG": str(cfg)},
    )
    assert res.returncode == 0, res.stderr
    assert "feature  [project]" in res.stdout
    assert "develop  [workspace]" in res.stdout
    assert "main  [global]" in res.stdout
    assert (
        res.stdout.index("[project]")
        < res.stdout.index("[workspace]")
        < res.stdout.index("[global]")
    )


def test_config_get_user_declared_key_inherits(root: Path) -> None:
    # A key named in the top-level `inheritable` array folds ancestor tiers: from
    # a project, `config get` reports the workspace-own value, tagged by tier.
    cfg = root / "cfg.toml"
    cfg.write_text(
        f'inheritable = ["deploy_env"]\n[workspaces.ws]\npath = "{root}"\ndeploy_env = "staging"\n'
    )
    _seed_repo(root, "proj")
    res = run(
        "config",
        "get",
        "deploy_env",
        "--origin",
        root=root,
        cwd=root / "proj",
        extra_env={"WTM_CONFIG": str(cfg)},
    )
    assert res.returncode == 0, res.stderr
    assert "staging  [workspace]" in res.stdout


def test_config_show_reflects_inherited_user_key(root: Path) -> None:
    # A declared inheritable key absent project-locally is suppressed at the
    # project and shown only at its defining ancestor (the workspace).
    cfg = root / "cfg.toml"
    cfg.write_text(
        f'inheritable = ["deploy_env"]\n[workspaces.ws]\npath = "{root}"\ndeploy_env = "staging"\n'
    )
    _seed_repo(root, "proj")
    res = run(
        "config",
        "show",
        root=root,
        cwd=root / "proj",
        extra_env={"WTM_CONFIG": str(cfg)},
    )
    assert res.returncode == 0, res.stderr
    ws_block = res.stdout.split("workspace ws:", 1)[1].split("project proj:", 1)[0]
    proj_block = res.stdout.split("project proj:", 1)[1]
    assert "deploy_env: staging" in ws_block
    assert "deploy_env" not in proj_block


def test_config_show_suppresses_inherited_base_branch_at_project(root: Path) -> None:
    # The headline case: a workspace-wide base_branch is shown at the workspace
    # tier only, not echoed under every project. The global default and the
    # workspace value each appear once, at their definition sites.
    cfg = root / "cfg.toml"
    cfg.write_text(
        f'base_branch = "master"\n[workspaces.ws]\npath = "{root}"\nbase_branch = "main"\n'
    )
    _seed_repo(root, "proj")
    res = run("config", "show", "-r", root=root, cwd=root, extra_env={"WTM_CONFIG": str(cfg)})
    assert res.returncode == 0, res.stderr
    assert "default base_branch: master" in res.stdout
    ws_block = res.stdout.split("workspace ws:", 1)[1].split("project proj:", 1)[0]
    assert "base_branch: main" in ws_block
    proj_block = res.stdout.split("project proj:", 1)[1]
    assert "base_branch" not in proj_block


def test_config_show_suppresses_inherited_arbitrary_scalar_at_project(root: Path) -> None:
    # An inheritable scalar defined at the workspace is suppressed at the project.
    cfg = root / "cfg.toml"
    cfg.write_text(
        f'inheritable = ["deploy_env"]\n[workspaces.ws]\npath = "{root}"\ndeploy_env = "staging"\n'
    )
    _seed_repo(root, "proj")
    res = run("config", "show", "-r", root=root, cwd=root, extra_env={"WTM_CONFIG": str(cfg)})
    assert res.returncode == 0, res.stderr
    ws_block = res.stdout.split("workspace ws:", 1)[1].split("project proj:", 1)[0]
    proj_block = res.stdout.split("project proj:", 1)[1]
    assert "deploy_env: staging" in ws_block
    assert "deploy_env" not in proj_block


def test_config_show_suppresses_inherited_table_cells_per_cell(root: Path) -> None:
    # An inheritable table deep-merges cell-by-cell; each cell is shown only at
    # its defining tier. The project cell appears under the project, the
    # workspace-ancestor cell under the workspace -- neither is echoed at the
    # other tier.
    cfg = root / "cfg.toml"
    cfg.write_text(f'inheritable = ["ci"]\n[workspaces.ws]\npath = "{root}"\n')
    _seed_repo(root, "proj")
    cwd = root / "proj"
    (cwd / ".wtm").mkdir(parents=True, exist_ok=True)
    (cwd / ".wtm" / "config.toml").write_text('[ci]\nregistry = "ghcr"\n')
    (root / ".wtm").mkdir(parents=True, exist_ok=True)
    (root / ".wtm" / "config.toml").write_text('[ci]\nrunner = "ubuntu"\n')
    env = {"WTM_CONFIG": str(cfg)}
    res = run("config", "show", "-r", root=root, cwd=root, extra_env=env)
    assert res.returncode == 0, res.stderr
    ws_block = res.stdout.split("workspace ws:", 1)[1].split("project proj:", 1)[0]
    proj_block = res.stdout.split("project proj:", 1)[1]
    assert "registry: ghcr" in proj_block
    assert "runner" not in proj_block
    assert "runner: ubuntu" in ws_block
    assert "registry" not in ws_block


def test_config_show_suppresses_inherited_keys_at_worktree(root: Path) -> None:
    # Suppression is uniform across all tiers: a worktree block shows only
    # worktree-local arbitrary keys; inherited values (from project, workspace,
    # or global) are pushed up to their defining ancestors.
    cfg = root / "cfg.toml"
    cfg.write_text(
        f'inheritable = ["deploy_env"]\n[workspaces.ws]\npath = "{root}"\ndeploy_env = "staging"\n'
    )
    _seed_repo(root, "proj")
    run("create", "proj", "feat", root=root)
    (root / "proj" / ".wtm").mkdir(parents=True, exist_ok=True)
    (root / "proj" / ".wtm" / "config.toml").write_text('deploy_env = "prod"\n')
    res = run("config", "show", "-r", root=root, cwd=root, extra_env={"WTM_CONFIG": str(cfg)})
    assert res.returncode == 0, res.stderr
    ws_block = res.stdout.split("workspace ws:", 1)[1].split("project proj:", 1)[0]
    proj_block = res.stdout.split("project proj:", 1)[1].split("worktree feat:", 1)[0]
    wt_block = res.stdout.split("worktree feat:", 1)[1]
    assert "deploy_env: staging" in ws_block
    assert "deploy_env: prod" in proj_block
    assert "deploy_env" not in wt_block


def test_config_show_omits_empty_inherited_table_header(root: Path) -> None:
    # When every cell of an inheritable table is inherited (the project defines
    # no cell of its own), the section header itself is suppressed -- no orphan
    # ``ci:`` line is left at the project tier.
    cfg = root / "cfg.toml"
    cfg.write_text(f'inheritable = ["ci"]\n[workspaces.ws]\npath = "{root}"\n')
    _seed_repo(root, "proj")
    (root / ".wtm").mkdir(parents=True, exist_ok=True)
    (root / ".wtm" / "config.toml").write_text('[ci]\nrunner = "ubuntu"\n')
    res = run("config", "show", "-r", root=root, cwd=root, extra_env={"WTM_CONFIG": str(cfg)})
    assert res.returncode == 0, res.stderr
    proj_block = res.stdout.split("project proj:", 1)[1]
    assert "ci:" not in proj_block


def test_config_set_user_scope_renders_dotted_headers(root: Path) -> None:
    # A fresh deep user-scope arbitrary-key write produces dotted [workspaces.W.
    # projects.P] headers -- no empty [workspaces] / [projects] container lines.
    cfg = root / "cfg.toml"
    cfg.write_text(f'[workspaces.ws]\npath = "{root}"\n')
    _seed_repo(root, "proj")
    res = run(
        "config",
        "set",
        "editor",
        "nvim",
        "-s",
        "user",
        root=root,
        cwd=root / "proj",
        extra_env={"WTM_CONFIG": str(cfg)},
    )
    assert res.returncode == 0, res.stderr
    text = cfg.read_text()
    assert "[workspaces.ws.projects.proj]" in text
    assert 'editor = "nvim"' in text
    lines = [line.strip() for line in text.splitlines()]
    assert "[projects]" not in lines  # no empty container header


def test_config_unset_removes_key(root: Path) -> None:
    _seed_repo(root, "proj")
    run("config", "set", "editor", "nvim", root=root, cwd=root / "proj")
    res = run("config", "unset", "editor", root=root, cwd=root / "proj")
    assert res.returncode == 0, res.stderr
    assert "editor" not in (root / "proj" / ".wtm" / "config.toml").read_text()


def test_config_set_array_add_and_remove(root: Path) -> None:
    # `set -a` builds a string array (set semantics: the repeat is a no-op);
    # `unset -a` strips the element again.
    _seed_repo(root, "proj")
    cwd = root / "proj"
    run("config", "set", "worktree_ports", "3001", "-a", root=root, cwd=cwd)
    run("config", "set", "worktree_ports", "3002", "-a", root=root, cwd=cwd)
    run("config", "set", "worktree_ports", "3001", "-a", root=root, cwd=cwd)
    text = (cwd / ".wtm" / "config.toml").read_text()
    assert 'worktree_ports = ["3001", "3002"]' in text
    res = run("config", "unset", "worktree_ports", "3001", "-a", root=root, cwd=cwd)
    assert res.returncode == 0, res.stderr
    assert 'worktree_ports = ["3002"]' in (cwd / ".wtm" / "config.toml").read_text()


def test_config_set_table_and_unset(root: Path) -> None:
    # `set -t` builds a table one sub-key at a time; `unset -t` removes a cell.
    _seed_repo(root, "proj")
    cwd = root / "proj"
    run("config", "set", "env", "REGION", "us-east", "-t", root=root, cwd=cwd)
    run("config", "set", "env", "TIER", "prod", "-t", root=root, cwd=cwd)
    text = (cwd / ".wtm" / "config.toml").read_text()
    assert 'REGION = "us-east"' in text
    assert 'TIER = "prod"' in text
    res = run("config", "unset", "env", "REGION", "-t", root=root, cwd=cwd)
    assert res.returncode == 0, res.stderr
    text = (cwd / ".wtm" / "config.toml").read_text()
    assert "REGION" not in text
    assert 'TIER = "prod"' in text


def test_config_get_readback_array_table_and_cell(root: Path) -> None:
    # `get` renders arrays one-per-line, tables as sorted `subkey = value`, and
    # a trailing SUBKEY reads a single cell.
    _seed_repo(root, "proj")
    cwd = root / "proj"
    run("config", "set", "ports", "3001", "-a", root=root, cwd=cwd)
    run("config", "set", "ports", "3002", "-a", root=root, cwd=cwd)
    run("config", "set", "env", "TIER", "prod", "-t", root=root, cwd=cwd)
    run("config", "set", "env", "REGION", "us-east", "-t", root=root, cwd=cwd)
    arr = run("config", "get", "ports", root=root, cwd=cwd)
    assert arr.stdout.split() == ["3001", "3002"]
    tbl = run("config", "get", "env", root=root, cwd=cwd)
    assert tbl.stdout == "REGION = us-east\nTIER = prod\n"
    cell = run("config", "get", "env", "TIER", root=root, cwd=cwd)
    assert cell.stdout.strip() == "prod"
    missing = run("config", "get", "env", "NOPE", root=root, cwd=cwd)
    assert missing.stdout.strip() == "(unset)"


# --- arbitrary-key sections: dotted paths + deep-merge -----------------------


def test_config_set_dotted_path_creates_nested_section(root: Path) -> None:
    # `config set ci.runner.image x` writes a nested [ci.runner] section.
    _seed_repo(root, "proj")
    cwd = root / "proj"
    res = run("config", "set", "ci.runner.image", "ubuntu", root=root, cwd=cwd)
    assert res.returncode == 0, res.stderr
    text = (cwd / ".wtm" / "config.toml").read_text()
    assert "[ci.runner]" in text
    assert 'image = "ubuntu"' in text
    # Round-trip: `get` reads the dotted cell back.
    cell = run("config", "get", "ci.runner.image", root=root, cwd=cwd)
    assert cell.stdout.strip() == "ubuntu"


def test_config_dotted_path_equals_table_flag(root: Path) -> None:
    # `ci.runner image x -t` (KEY=ci.runner SUBKEY=image) and `ci.runner.image x`
    # (dotted) write the same cell.
    _seed_repo(root, "proj")
    cwd = root / "proj"
    run("config", "set", "ci.runner.image", "ubuntu", root=root, cwd=cwd)
    run("config", "set", "ci.runner", "tag", "latest", "-t", root=root, cwd=cwd)
    res = run("config", "get", "ci.runner", root=root, cwd=cwd)
    assert "image = ubuntu" in res.stdout
    assert "tag = latest" in res.stdout


def test_config_dotted_path_equals_space_form(root: Path) -> None:
    # `config get ci.runner` (dotted) == `config get ci runner` (space form).
    _seed_repo(root, "proj")
    cwd = root / "proj"
    run("config", "set", "env", "TIER", "prod", "-t", root=root, cwd=cwd)
    dotted = run("config", "get", "env.TIER", root=root, cwd=cwd)
    spaced = run("config", "get", "env", "TIER", root=root, cwd=cwd)
    assert dotted.stdout == spaced.stdout
    assert dotted.stdout.strip() == "prod"


def test_config_get_dotted_cell_unset(root: Path) -> None:
    # A dotted path to an absent cell prints (unset).
    _seed_repo(root, "proj")
    cwd = root / "proj"
    res = run("config", "get", "ci.runner.nope", root=root, cwd=cwd)
    assert res.stdout.strip() == "(unset)"


def test_config_table_deep_merge_across_locality(root: Path) -> None:
    # A section merges per-cell across one entity's locality cascade: the
    # project-local [ci] and the workspace-file [projects.proj.ci] combine.
    _seed_repo(root, "proj")
    cwd = root / "proj"
    (cwd / ".wtm").mkdir(parents=True, exist_ok=True)
    (cwd / ".wtm" / "config.toml").write_text('[ci]\nregistry = "ghcr"\n')
    (root / ".wtm").mkdir(parents=True, exist_ok=True)
    (root / ".wtm" / "config.toml").write_text('[projects.proj.ci]\nrunner = "ubuntu"\n')
    res = run("config", "get", "ci", root=root, cwd=cwd)
    assert res.returncode == 0, res.stderr
    assert "registry = ghcr" in res.stdout
    assert "runner = ubuntu" in res.stdout
    # A dotted cell reads from the merged table whichever tier defined it.
    assert run("config", "get", "ci.runner", root=root, cwd=cwd).stdout.strip() == "ubuntu"


def test_config_table_deep_merge_across_inheritance(root: Path) -> None:
    # An inheritable section merges cell-by-cell down the containment chain.
    cfg = root / "cfg.toml"
    cfg.write_text(f'inheritable = ["ci"]\n[workspaces.ws]\npath = "{root}"\n')
    _seed_repo(root, "proj")
    cwd = root / "proj"
    (cwd / ".wtm").mkdir(parents=True, exist_ok=True)
    (cwd / ".wtm" / "config.toml").write_text('[ci]\nregistry = "ghcr"\n')
    (root / ".wtm").mkdir(parents=True, exist_ok=True)
    (root / ".wtm" / "config.toml").write_text('[ci]\nrunner = "ubuntu"\n')
    env = {"WTM_CONFIG": str(cfg)}
    res = run("config", "get", "ci", root=root, cwd=cwd, extra_env=env)
    assert res.returncode == 0, res.stderr
    assert "registry = ghcr" in res.stdout
    assert "runner = ubuntu" in res.stdout


def test_config_origin_per_cell_for_merged_table(root: Path) -> None:
    # `--origin` on a merged table labels each cell with its own source tier.
    cfg = root / "cfg.toml"
    cfg.write_text(f'inheritable = ["ci"]\n[workspaces.ws]\npath = "{root}"\n')
    _seed_repo(root, "proj")
    cwd = root / "proj"
    (cwd / ".wtm").mkdir(parents=True, exist_ok=True)
    (cwd / ".wtm" / "config.toml").write_text('[ci]\nregistry = "ghcr"\n')
    (root / ".wtm").mkdir(parents=True, exist_ok=True)
    (root / ".wtm" / "config.toml").write_text('[ci]\nrunner = "ubuntu"\n')
    env = {"WTM_CONFIG": str(cfg)}
    res = run("config", "get", "ci", "--origin", root=root, cwd=cwd, extra_env=env)
    assert res.returncode == 0, res.stderr
    assert "ci.registry = ghcr" in res.stdout
    assert "ci.runner = ubuntu" in res.stdout
    # Each cell is labelled by its defining tier (project vs workspace).
    assert "ci.registry = ghcr  [project]" in res.stdout
    assert "ci.runner = ubuntu  [workspace]" in res.stdout


def test_config_origin_dotted_scalar_reports_carrier(root: Path) -> None:
    # `--origin` on a dotted scalar lists only rungs carrying that exact cell.
    cfg = root / "cfg.toml"
    cfg.write_text(f'inheritable = ["ci"]\n[workspaces.ws]\npath = "{root}"\n')
    _seed_repo(root, "proj")
    cwd = root / "proj"
    (root / ".wtm").mkdir(parents=True, exist_ok=True)
    (root / ".wtm" / "config.toml").write_text('[ci]\nrunner = "ubuntu"\n')
    env = {"WTM_CONFIG": str(cfg)}
    res = run("config", "get", "ci.runner", "--origin", root=root, cwd=cwd, extra_env=env)
    assert res.returncode == 0, res.stderr
    assert "ubuntu  [workspace]" in res.stdout
    # A cell no rung carries prints (unset).
    missing = run("config", "get", "ci.image", "--origin", root=root, cwd=cwd, extra_env=env)
    assert missing.stdout.strip() == "(unset)"


def test_config_origin_scalar_unchanged(root: Path) -> None:
    # A bare scalar's `--origin` keeps its one-line-per-rung format.
    cfg = root / "cfg.toml"
    cfg.write_text(
        f'inheritable = ["deploy_env"]\n[workspaces.ws]\npath = "{root}"\ndeploy_env = "staging"\n'
    )
    _seed_repo(root, "proj")
    res = run(
        "config",
        "get",
        "deploy_env",
        "--origin",
        root=root,
        cwd=root / "proj",
        extra_env={"WTM_CONFIG": str(cfg)},
    )
    assert res.returncode == 0, res.stderr
    assert "staging  [workspace]" in res.stdout


def test_config_nested_table_origin_per_cell(root: Path) -> None:
    # Per-cell origin recurses into nested sub-tables.
    cfg = root / "cfg.toml"
    cfg.write_text(f'inheritable = ["ci"]\n[workspaces.ws]\npath = "{root}"\n')
    _seed_repo(root, "proj")
    cwd = root / "proj"
    (cwd / ".wtm").mkdir(parents=True, exist_ok=True)
    (cwd / ".wtm" / "config.toml").write_text('[ci.runner]\nimage = "ubuntu"\n')
    (root / ".wtm").mkdir(parents=True, exist_ok=True)
    (root / ".wtm" / "config.toml").write_text('[ci.runner]\ntag = "latest"\n')
    env = {"WTM_CONFIG": str(cfg)}
    res = run("config", "get", "ci", "--origin", root=root, cwd=cwd, extra_env=env)
    assert res.returncode == 0, res.stderr
    assert "ci.runner.image = ubuntu  [project]" in res.stdout
    assert "ci.runner.tag = latest  [workspace]" in res.stdout


def test_config_table_subpath_kind_clash(root: Path) -> None:
    # A scalar at `ci.runner` in one tier and a table at `ci.runner.image` in
    # another is a hard error naming the nested path.
    cfg = root / "cfg.toml"
    cfg.write_text(f'inheritable = ["ci"]\n[workspaces.ws]\npath = "{root}"\n')
    _seed_repo(root, "proj")
    cwd = root / "proj"
    (cwd / ".wtm").mkdir(parents=True, exist_ok=True)
    (cwd / ".wtm" / "config.toml").write_text('[ci]\nrunner = "ubuntu"\n')
    (root / ".wtm").mkdir(parents=True, exist_ok=True)
    (root / ".wtm" / "config.toml").write_text('[ci.runner]\nimage = "x"\n')
    env = {"WTM_CONFIG": str(cfg)}
    res = run("config", "get", "ci", root=root, cwd=cwd, extra_env=env)
    assert res.returncode == 1
    assert "ci.runner" in res.stderr


def test_config_reserved_head_rejected_dotted(root: Path) -> None:
    # A dotted key's head segment may not be a reserved container name.
    _seed_repo(root, "proj")
    cwd = root / "proj"
    res = run("config", "set", "hooks.on.x", "y", root=root, cwd=cwd)
    assert res.returncode == 1
    assert "reserved key" in res.stderr.lower()
    # A deeper reserved segment under a user section is a harmless cell.
    ok = run("config", "set", "ci.workspaces", "ok", root=root, cwd=cwd)
    assert ok.returncode == 0, ok.stderr


def test_config_inheritable_dotted_rejected(root: Path) -> None:
    # `inheritable` stays bare-only: a dotted form is rejected (head reserved).
    _seed_repo(root, "proj")
    cwd = root / "proj"
    res = run("config", "set", "inheritable.x", "y", root=root, cwd=cwd)
    assert res.returncode == 1
    assert "reserved key" in res.stderr.lower()


def test_config_arrays_still_do_not_merge(root: Path) -> None:
    # Arrays remain non-merging across tiers: the most-local array wins wholesale.
    cfg = root / "cfg.toml"
    cfg.write_text(f'inheritable = ["ports"]\n[workspaces.ws]\npath = "{root}"\n')
    _seed_repo(root, "proj")
    cwd = root / "proj"
    (cwd / ".wtm").mkdir(parents=True, exist_ok=True)
    (cwd / ".wtm" / "config.toml").write_text('ports = ["3001"]\n')
    (root / ".wtm").mkdir(parents=True, exist_ok=True)
    (root / ".wtm" / "config.toml").write_text('ports = ["3002", "3003"]\n')
    env = {"WTM_CONFIG": str(cfg)}
    res = run("config", "get", "ports", root=root, cwd=cwd, extra_env=env)
    assert res.returncode == 0, res.stderr
    assert res.stdout.split() == ["3001"]


def test_config_unset_dotted_cell(root: Path) -> None:
    # `config unset ci.runner.image` removes one nested cell.
    _seed_repo(root, "proj")
    cwd = root / "proj"
    run("config", "set", "ci.runner.image", "ubuntu", root=root, cwd=cwd)
    run("config", "set", "ci.runner.tag", "latest", root=root, cwd=cwd)
    res = run("config", "unset", "ci.runner.image", root=root, cwd=cwd)
    assert res.returncode == 0, res.stderr
    text = (cwd / ".wtm" / "config.toml").read_text()
    assert "image" not in text
    assert 'tag = "latest"' in text


def test_config_dotted_array_set(root: Path) -> None:
    # `-a` works at a dotted path: adds to an array inside a section.
    _seed_repo(root, "proj")
    cwd = root / "proj"
    run("config", "set", "ci.ports", "3001", "-a", root=root, cwd=cwd)
    run("config", "set", "ci.ports", "3002", "-a", root=root, cwd=cwd)
    res = run("config", "get", "ci.ports", root=root, cwd=cwd)
    assert res.stdout.split() == ["3001", "3002"]


def test_config_set_table_kind_clash_on_scalar(root: Path) -> None:
    # `-t` over a key already holding a scalar is a hard kind clash.
    _seed_repo(root, "proj")
    cwd = root / "proj"
    run("config", "set", "env", "prod", root=root, cwd=cwd)
    res = run("config", "set", "env", "TIER", "prod", "-t", root=root, cwd=cwd)
    assert res.returncode == 1
    assert "not a table" in res.stderr.lower()


def test_config_set_array_table_flags_mutually_exclusive(root: Path) -> None:
    _seed_repo(root, "proj")
    res = run("config", "set", "x", "1", "-a", "-t", root=root, cwd=root / "proj")
    assert res.returncode == 1
    assert "mutually exclusive" in res.stderr.lower()


def test_config_set_bad_arity(root: Path) -> None:
    # `-t` needs KEY SUBKEY VALUE; plain set needs KEY VALUE.
    _seed_repo(root, "proj")
    cwd = root / "proj"
    res = run("config", "set", "env", "TIER", "-t", root=root, cwd=cwd)
    assert res.returncode == 1
    assert "requires KEY SUBKEY VALUE" in res.stderr
    res = run("config", "set", "only-key", root=root, cwd=cwd)
    assert res.returncode == 1
    assert "requires KEY VALUE" in res.stderr


def test_config_set_inheritable_array_opts_key_in(root: Path) -> None:
    # `config set inheritable KEY -a` maintains the reserved allowlist,
    # and the opted-in key then actually inherits (config get from a project).
    cfg = root / "cfg.toml"
    cfg.write_text(f'[workspaces.ws]\npath = "{root}"\ndeploy_env = "staging"\n')
    _seed_repo(root, "proj")
    env = {"WTM_CONFIG": str(cfg)}
    add = run(
        "config",
        "set",
        "inheritable",
        "deploy_env",
        "-a",
        "-s",
        "user",
        root=root,
        cwd=root,
        extra_env=env,
    )
    assert add.returncode == 0, add.stderr
    assert 'inheritable = ["deploy_env"]' in cfg.read_text()
    got = run(
        "config",
        "get",
        "deploy_env",
        "--origin",
        root=root,
        cwd=root / "proj",
        extra_env=env,
    )
    assert got.returncode == 0, got.stderr
    assert "staging  [workspace]" in got.stdout


def test_config_unset_inheritable_array_opts_key_out(root: Path) -> None:
    # `config unset inheritable KEY -a` removes the reserved-key element (the
    # reserved name is not refused on this path).
    cfg = root / "cfg.toml"
    cfg.write_text(f'[workspaces.ws]\npath = "{root}"\ninheritable = ["deploy_env"]\n')
    _seed_repo(root, "proj")
    env = {"WTM_CONFIG": str(cfg)}
    res = run(
        "config",
        "unset",
        "inheritable",
        "deploy_env",
        "-a",
        "-s",
        "user",
        root=root,
        cwd=root,
        extra_env=env,
    )
    assert res.returncode == 0, res.stderr
    assert "deploy_env" not in cfg.read_text()


def test_config_set_inheritable_rejects_hardcoded_non_inheritable(root: Path) -> None:
    # `description` is hardcoded never-inheritable: opting it in is refused.
    _seed_repo(root, "proj")
    res = run("config", "set", "inheritable", "description", "-a", root=root, cwd=root)
    assert res.returncode == 1
    assert "never inheritable" in res.stderr.lower()


def test_config_set_inheritable_floor_declares_and_enforces(root: Path) -> None:
    # `--floor` stores { key = ..., floor = ... }; the floored key then refuses
    # writes landing below its floor and resolves only from the floor up.
    cfg = root / "cfg.toml"
    cfg.write_text(f'[workspaces.ws]\npath = "{root}"\nregion = "ws-val"\n')
    wt = root / "proj" / "worktrees" / "feat"
    wt.mkdir(parents=True)
    add = run(
        "config",
        "set",
        "inheritable",
        "region",
        "-a",
        "--floor",
        "project",
        "-s",
        "user",
        root=root,
        cwd=root,
        extra_env={"WTM_CONFIG": str(cfg)},
    )
    assert add.returncode == 0, add.stderr
    assert (
        '{ key = "region", floor = "project" }' in cfg.read_text().replace("{key =", "{ key =")
        or 'floor = "project"' in cfg.read_text()
    )

    # From the worktree, the resolved value comes from the workspace tier
    # (the floor prunes nothing here -- but a worktree-local write refuses).
    refused = run(
        "config",
        "set",
        "region",
        "x",
        "-s",
        "worktree",
        root=root,
        cwd=wt,
        extra_env={"WTM_CONFIG": str(cfg)},
    )
    assert refused.returncode == 1
    assert "has floor 'project'" in refused.stderr

    # At the floor tier the same write goes through.
    allowed = run(
        "config",
        "set",
        "region",
        "x",
        "-s",
        "project",
        root=root,
        cwd=wt,
        extra_env={"WTM_CONFIG": str(cfg)},
    )
    assert allowed.returncode == 0, allowed.stderr


def test_config_set_floor_flag_misuse(root: Path) -> None:
    # --floor belongs to `-a` on KEY 'inheritable' alone.
    _seed_repo(root, "proj")
    scalar = run("config", "set", "region", "x", "--floor", "project", root=root, cwd=root)
    assert scalar.returncode == 1
    assert "--floor requires" in scalar.stderr

    array_other_key = run(
        "config", "set", "tags", "x", "-a", "--floor", "project", root=root, cwd=root
    )
    assert array_other_key.returncode == 1
    assert "only to KEY 'inheritable'" in array_other_key.stderr

    bad_tier = run(
        "config",
        "set",
        "inheritable",
        "region",
        "-a",
        "--floor",
        "cellar",
        root=root,
        cwd=root,
    )
    assert bad_tier.returncode == 1
    assert "'floor' must be one of" in bad_tier.stderr


def test_config_set_workspace_entity_from_root_cwd(root: Path) -> None:
    # Standing at the workspace root (no project), the entity floor is the
    # active workspace; the key lands workspace-local top-level.
    res = run("config", "set", "editor", "nvim", root=root, cwd=root)
    assert res.returncode == 0, res.stderr
    assert "workspace 'ws'" in res.stderr
    assert 'editor = "nvim"' in (root / ".wtm" / "config.toml").read_text()


def test_config_set_rejects_reserved_key(root: Path) -> None:
    _seed_repo(root, "proj")
    res = run("config", "set", "hooks", "x", root=root, cwd=root / "proj")
    assert res.returncode == 1
    assert "reserved" in res.stderr.lower()


def test_config_get_missing_key_reports_unset(root: Path) -> None:
    _seed_repo(root, "proj")
    res = run("config", "get", "editor", root=root, cwd=root / "proj")
    assert res.returncode == 0, res.stderr
    assert "(unset)" in res.stdout


def test_config_set_refused_under_no_config(root: Path) -> None:
    # WTM_NO_CONFIG empties the registry, so the write never lands: whether the
    # error is the write guard or the (consequent) no-active-workspace, nothing
    # is written to disk.
    res = run(
        "config",
        "set",
        "editor",
        "nvim",
        root=root,
        cwd=root,
        extra_env={"WTM_NO_CONFIG": "1"},
    )
    assert res.returncode == 1
    assert not (root / ".wtm" / "config.toml").exists()


def test_config_set_rejects_unreachable_scope(root: Path) -> None:
    # Standing in the workspace (no project), a -s project write has no project
    # entity to attach to.
    res = run("config", "set", "editor", "nvim", "-s", "project", root=root, cwd=root)
    assert res.returncode == 1
    assert "project" in res.stderr.lower()


# --- global-tier arbitrary keys (-g/--global) --------------------------------


def test_config_set_get_unset_global_top_level(root: Path) -> None:
    # -g writes the user file's top level -- beside [workspaces...], not inside
    # it; get reads it back; unset removes it again.
    cfg = root / "cfg.toml"
    cfg.write_text(f'[workspaces.ws]\npath = "{root}"\n')
    env = {"WTM_CONFIG": str(cfg)}
    assert (
        run("config", "set", "-g", "editor", "nvim", root=root, cwd=root, extra_env=env).returncode
        == 0
    )
    text = cfg.read_text()
    assert 'editor = "nvim"' in text
    assert text.index("editor") < text.index("[workspaces.ws]")
    got = run("config", "get", "-g", "editor", root=root, cwd=root, extra_env=env)
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == "nvim"
    unset = run("config", "unset", "-g", "editor", root=root, cwd=root, extra_env=env)
    assert unset.returncode == 0
    assert "editor" not in cfg.read_text()


def test_config_set_global_system_scope_top_level(root: Path) -> None:
    # -g -s system lands the key at the top level of the system file instead.
    sys_cfg = root / "sys.toml"
    env = {
        "WTM_CONFIG": str(root / "_absent_user.toml"),
        "WTM_SYSTEM_CONFIG": str(sys_cfg),
        "WTM_WORKSPACE": "",
    }
    res = run("config", "set", "-g", "-s", "system", "editor", "vi", root=root, extra_env=env)
    assert res.returncode == 0, res.stderr
    assert 'editor = "vi"' in sys_cfg.read_text()


def test_config_global_flags_reject_locator_args(root: Path) -> None:
    # Extra positionals beyond the key group are locator tokens; under -g they
    # are refused, like config show's -g.
    _seed_repo(root, "alpha")
    for cmd, group in (("set", ("editor", "nvim")), ("unset", ("editor",))):
        res = run("config", cmd, "-g", "alpha", *group, root=root)
        assert res.returncode == 1
        assert "--global" in res.stderr
    res = run("config", "get", "-g", "alpha", "editor", root=root)
    assert res.returncode == 1
    assert "--global" in res.stderr


def test_config_get_global_works_without_active_workspace(root: Path) -> None:
    # The carve-out: with nothing declared or selected the bare form errors,
    # while -g still reaches the global tier.
    cfg = root / "cfg.toml"
    cfg.write_text('editor = "nvim"\n')
    env = {"WTM_CONFIG": str(cfg), "WTM_WORKSPACE": ""}
    bare = run("config", "get", "editor", root=root, extra_env=env)
    assert bare.returncode == 1
    got = run("config", "get", "-g", "editor", root=root, extra_env=env)
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == "nvim"


def test_config_get_global_origin_labels_user_scope(root: Path) -> None:
    cfg = root / "cfg.toml"
    cfg.write_text('editor = "nvim"\n')
    res = run(
        "config",
        "get",
        "-g",
        "editor",
        "--origin",
        root=root,
        extra_env={"WTM_CONFIG": str(cfg), "WTM_WORKSPACE": ""},
    )
    assert res.returncode == 0, res.stderr
    assert "[user]" in res.stdout


def test_config_global_inheritable_key_cascades_to_project(root: Path) -> None:
    # The payoff scenario: declare + define a key at the global tier, opt it
    # into inheritance, and a project resolves it (entity-local until declared).
    _seed_repo(root, "proj")
    cfg = root / "cfg.toml"
    cfg.write_text(f'[workspaces.ws]\npath = "{root}"\n')
    env = {"WTM_CONFIG": str(cfg)}
    assert (
        run(
            "config",
            "set",
            "-g",
            "inheritable",
            "deploy_env",
            "-a",
            root=root,
            extra_env=env,
        ).returncode
        == 0
    )
    assert (
        run("config", "set", "-g", "deploy_env", "staging", root=root, extra_env=env).returncode
        == 0
    )
    got = run("config", "get", "deploy_env", root=root, cwd=root / "proj", extra_env=env)
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == "staging"


# --- inline hook writer: hook set / unset (phase 5) --------------------------


def test_hook_set_writes_project_local_inline(root: Path) -> None:
    # From inside a project, an inline hook lands in the project-local file under
    # [hooks.on] and reads back through the merged cascade (config show --hooks).
    _seed_repo(root, "proj")
    res = run("hook", "set", "install", "make deps", root=root, cwd=root / "proj")
    assert res.returncode == 0, res.stderr
    assert "[project]" in res.stderr
    text = (root / "proj" / ".wtm" / "config.toml").read_text()
    assert "[hooks.on]" in text
    assert 'install = "make deps"' in text
    shown = run("config", "show", "--hooks", root=root, cwd=root / "proj")
    assert "hook install = make deps" in shown.stdout


def test_hook_set_scope_workspace_from_project_cwd(root: Path) -> None:
    # -s workspace nests the inline hook under [projects.proj.hooks.on].
    _seed_repo(root, "proj")
    res = run("hook", "set", "enter", "echo hi", "-s", "workspace", root=root, cwd=root / "proj")
    assert res.returncode == 0, res.stderr
    text = (root / ".wtm" / "config.toml").read_text()
    assert "[projects.proj.hooks.on]" in text
    assert 'enter = "echo hi"' in text
    assert not (root / "proj" / ".wtm" / "config.toml").exists()


def test_hook_set_workspace_entity_from_root_cwd(root: Path) -> None:
    # Standing at the workspace root (no project), the entity floor is the active
    # workspace; the inline hook nests under the workspace entry's hooks.on.
    res = run("hook", "set", "workspace-enter", "echo in", root=root, cwd=root)
    assert res.returncode == 0, res.stderr
    assert "workspace 'ws'" in res.stderr
    text = (root / ".wtm" / "config.toml").read_text()
    assert "[hooks.on]" in text
    assert 'workspace-enter = "echo in"' in text


def test_hook_unset_removes_inline(root: Path) -> None:
    _seed_repo(root, "proj")
    run("hook", "set", "install", "make deps", root=root, cwd=root / "proj")
    res = run("hook", "unset", "install", root=root, cwd=root / "proj")
    assert res.returncode == 0, res.stderr
    text = (root / "proj" / ".wtm" / "config.toml").read_text()
    assert "install" not in text
    shown = run("config", "show", "--hooks", root=root, cwd=root / "proj")
    assert "hook install" not in shown.stdout


def test_hook_set_replaces_at_same_scope(root: Path) -> None:
    # A second set at the same scope replaces the command in that file.
    _seed_repo(root, "proj")
    run("hook", "set", "install", "old", root=root, cwd=root / "proj")
    run("hook", "set", "install", "new", root=root, cwd=root / "proj")
    text = (root / "proj" / ".wtm" / "config.toml").read_text()
    assert 'install = "new"' in text
    assert '"old"' not in text


def test_hook_set_rejects_unknown_event(root: Path) -> None:
    _seed_repo(root, "proj")
    res = run("hook", "set", "not-an-event", "x", root=root, cwd=root / "proj")
    assert res.returncode == 1
    assert "unknown event" in res.stderr.lower()


def test_hook_set_refused_under_no_config(root: Path) -> None:
    res = run(
        "hook",
        "set",
        "enter",
        "echo hi",
        root=root,
        cwd=root,
        extra_env={"WTM_NO_CONFIG": "1"},
    )
    assert res.returncode == 1
    assert not (root / ".wtm" / "config.toml").exists()


def test_workspace_rename_moves_key(root: Path) -> None:
    res = run("workspace", "rename", "ws", "renamed", root=root)
    assert res.returncode == 0, res.stderr
    text = _config_text(root)
    assert "[workspaces.renamed]" in text and "[workspaces.ws]" not in text


def test_workspace_delete_unregisters(root: Path) -> None:
    res = run("workspace", "delete", "ws", root=root)
    assert res.returncode == 0, res.stderr
    assert "[workspaces.ws]" not in _config_text(root)


def test_workspace_default_set_and_show(root: Path) -> None:
    res = run("workspace", "default", "ws", root=root)
    assert res.returncode == 0, res.stderr
    assert 'default_workspace = "ws"' in _config_text(root)


def test_workspace_show_outputs_detail(root: Path) -> None:
    res = run("workspace", "show", "ws", root=root)
    assert res.returncode == 0, res.stderr
    assert "name: ws" in res.stdout
    assert f"path: {root}" in res.stdout
    assert "scope: user" in res.stdout


def test_workspace_write_refused_under_no_config(root: Path) -> None:
    # WTM_WORKSPACE="" so the ignored-config active-workspace resolution does not
    # error before we reach the write guard.
    res = run(
        "workspace",
        "add",
        "x",
        str(root),
        root=root,
        extra_env={"WTM_NO_CONFIG": "1", "WTM_WORKSPACE": ""},
    )
    assert res.returncode == 1
    assert "WTM_NO_CONFIG" in res.stderr


# --- workspace enter / leave (cd channel + lifecycle hooks) -------------------


def test_workspace_enter_emits_cd_and_fires_hook(root: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "wshooks"
    shell_file = tmp_path / "cd"
    out = tmp_path / "entered"
    _write_hook(
        workspace / "wtm-workspace-enter",
        f'printf "%s|%s|%s|%s\\n" "$WTM_EVENT" "$WTM_WORKSPACE" "$1"'
        f' "$WTM_WORKTREE_DIR" > "{out}"\n',
    )
    res = run(
        "workspace", "enter", root=root, shell_file=shell_file, workspace_hooks_dir=workspace
    )
    assert res.returncode == 0, res.stderr
    assert _shell_cd(shell_file) == str(root)
    # Workspace-level session: only the workspace vars are exported; the six
    # worktree/project vars are unset.
    assert _shell_exports(shell_file) == {"WTM_WORKSPACE": "ws", "WTM_WORKSPACE_DIR": str(root)}
    assert _shell_unsets(shell_file) == {
        "WTM_WORKTREE_DIR",
        "WTM_BRANCH",
        "WTM_PROJECT",
        "WTM_PROJECT_DIR",
        "WTM_WORKTREES_DIR",
        "WTM_GIT_COMMON_DIR",
    }
    event, ws, arg1, wtdir = out.read_text().strip().split("|")
    assert event == "workspace-enter"
    assert ws == "ws"
    assert arg1 == str(root)
    assert wtdir == ""  # workspace-only contract: no worktree fields


def test_workspace_leave_from_root_lands_on_oldpwd(root: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "wshooks"
    shell_file = tmp_path / "cd"
    fired = tmp_path / "left"
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_hook(workspace / "wtm-workspace-leave", f'echo "$WTM_WORKSPACE" > "{fired}"\n')
    res = run(
        "workspace",
        "leave",
        root=root,
        cwd=root,
        shell_file=shell_file,
        workspace_hooks_dir=workspace,
        extra_env={"OLDPWD": str(outside)},
    )
    assert res.returncode == 0, res.stderr
    assert fired.read_text().strip() == "ws"
    assert _shell_cd(shell_file) == str(outside)  # OLDPWD is outside the tree


def test_workspace_leave_cascades_worktree_teardown(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    workspace = tmp_path / "wshooks"
    shell_file = tmp_path / "cd"
    order = tmp_path / "order"
    # worktree-scope 'leave' (project .git/hooks) + workspace-scope 'workspace-leave'
    _write_hook(
        root / "alpha" / ".git" / "hooks" / "wtm-leave", f'echo "wt:$WTM_BRANCH" >> "{order}"\n'
    )
    _write_hook(workspace / "wtm-workspace-leave", f'echo "ws:$WTM_WORKSPACE" >> "{order}"\n')
    wt = root / "alpha" / "worktrees" / "feat"
    res = run(
        "workspace",
        "leave",
        root=root,
        cwd=wt,
        shell_file=shell_file,
        workspace_hooks_dir=workspace,
        extra_env={"OLDPWD": "/nonexistent-oldpwd"},
    )
    assert res.returncode == 0, res.stderr
    # Teardown is inside-out: worktree leave first, then workspace-leave.
    assert order.read_text().splitlines() == ["wt:feat", "ws:ws"]
    # OLDPWD unusable -> cascaded out of the worktree tree to the projects-root parent.
    assert _shell_cd(shell_file) == str(root.parent)


# --- project enter / leave (cd channel + lifecycle hooks) --------------------


def test_project_enter_emits_cd_and_project_contract(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    shell_file = tmp_path / "cd"
    out = tmp_path / "entered"
    # project-scope builtin lives under the project's .git/hooks.
    _write_hook(
        root / "alpha" / ".git" / "hooks" / "wtm-project-enter",
        f'printf "%s|%s|%s|%s|%s\\n" "$WTM_EVENT" "$WTM_PROJECT" "$1"'
        f' "$WTM_WORKSPACE" "$WTM_WORKTREE_DIR" > "{out}"\n',
    )
    res = run("project", "enter", "alpha", root=root, shell_file=shell_file)
    assert res.returncode == 0, res.stderr
    assert _shell_cd(shell_file) == str(root / "alpha")
    # Project-level session: the six project vars are exported; the two worktree
    # vars are unset.
    assert _shell_exports(shell_file) == {
        "WTM_PROJECT": "alpha",
        "WTM_PROJECT_DIR": str(root / "alpha"),
        "WTM_WORKTREES_DIR": str(root / "alpha" / "worktrees"),
        "WTM_GIT_COMMON_DIR": str(root / "alpha" / ".git"),
        "WTM_WORKSPACE": "ws",
        "WTM_WORKSPACE_DIR": str(root),
    }
    assert _shell_unsets(shell_file) == {"WTM_WORKTREE_DIR", "WTM_BRANCH"}
    event, project, arg1, workspace, wtdir = out.read_text().strip().split("|")
    assert event == "project-enter"
    assert project == "alpha"
    assert arg1 == str(root / "alpha")
    assert workspace == "ws"
    assert wtdir == ""  # project-only contract: no worktree field


def test_project_enter_infers_project_from_cwd(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    shell_file = tmp_path / "cd"
    wt = root / "alpha" / "worktrees" / "feat"
    res = run("project", "enter", root=root, cwd=wt, shell_file=shell_file)
    assert res.returncode == 0, res.stderr
    assert _shell_cd(shell_file) == str(root / "alpha")
    assert _shell_exports(shell_file)["WTM_PROJECT"] == "alpha"


def test_project_enter_missing_git_errors(root: Path, tmp_path: Path) -> None:
    # A project *is* a <name>/.git, so a dir without one is not a project at all:
    # the project-pinned locator reports a miss rather than an unenterable target.
    (root / "empty" / "worktrees").mkdir(parents=True)
    shell_file = tmp_path / "cd"
    res = run("project", "enter", "empty", root=root, shell_file=shell_file)
    assert res.returncode == 1
    assert "no project matches: empty" in res.stderr


def test_project_enter_from_cwd_missing_git_errors(root: Path, tmp_path: Path) -> None:
    # Cwd inference bypasses the locator (no name to resolve), so the
    # enterability guard is still what reports a .git-less project dir.
    empty = root / "empty"
    (empty / "worktrees").mkdir(parents=True)
    shell_file = tmp_path / "cd"
    res = run("project", "enter", root=root, cwd=empty, shell_file=shell_file)
    assert res.returncode == 1
    assert "project git dir not found" in res.stderr


def test_project_leave_fires_hook_and_unsets_contract(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    shell_file = tmp_path / "cd"
    fired = tmp_path / "left"
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_hook(
        root / "alpha" / ".git" / "hooks" / "wtm-project-leave",
        f'echo "$WTM_PROJECT" > "{fired}"\n',
    )
    res = run(
        "project",
        "leave",
        "alpha",
        root=root,
        cwd=root / "alpha",
        shell_file=shell_file,
        extra_env={"OLDPWD": str(outside)},
    )
    assert res.returncode == 0, res.stderr
    assert fired.read_text().strip() == "alpha"
    # Leaving ends the session: the whole managed set is unset.
    assert _shell_exports(shell_file) == {}
    assert _shell_unsets(shell_file) == set(MANAGED_ENV_VARS)
    assert _shell_cd(shell_file) == str(outside)  # OLDPWD outside the project tree


def test_project_leave_cascades_worktree_teardown(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    shell_file = tmp_path / "cd"
    order = tmp_path / "order"
    _write_hook(
        root / "alpha" / ".git" / "hooks" / "wtm-leave", f'echo "wt:$WTM_BRANCH" >> "{order}"\n'
    )
    _write_hook(
        root / "alpha" / ".git" / "hooks" / "wtm-project-leave",
        f'echo "proj:$WTM_PROJECT" >> "{order}"\n',
    )
    wt = root / "alpha" / "worktrees" / "feat"
    res = run(
        "project",
        "leave",
        root=root,
        cwd=wt,
        shell_file=shell_file,
        extra_env={"OLDPWD": "/nonexistent-oldpwd"},
    )
    assert res.returncode == 0, res.stderr
    # Teardown inside-out: worktree leave first, then project-leave.
    assert order.read_text().splitlines() == ["wt:feat", "proj:alpha"]
    # OLDPWD unusable -> cascaded out of the worktree tree to the projects root.
    assert _shell_cd(shell_file) == str(root)


def test_project_leave_explicit_not_inside_no_cd(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    shell_file = tmp_path / "cd"
    fired = tmp_path / "left"
    _write_hook(
        root / "alpha" / ".git" / "hooks" / "wtm-project-leave",
        f'echo "$WTM_PROJECT" > "{fired}"\n',
    )
    # Standing at the projects root, leaving an explicit project we're not in.
    res = run("project", "leave", "alpha", root=root, cwd=root, shell_file=shell_file)
    assert res.returncode == 0, res.stderr
    assert fired.read_text().strip() == "alpha"  # hook still fires
    assert _shell_cd(shell_file) is None  # never cds out of a tree we're not in


def test_project_leave_no_cd_flag_stays_but_ends_session(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    shell_file = tmp_path / "cd"
    outside = tmp_path / "outside"
    outside.mkdir()
    res = run(
        "project",
        "leave",
        "alpha",
        "-C",
        root=root,
        cwd=root / "alpha",
        shell_file=shell_file,
        extra_env={"OLDPWD": str(outside)},
    )
    assert res.returncode == 0, res.stderr
    assert _shell_cd(shell_file) is None  # -C suppresses the cd-out
    assert _shell_unsets(shell_file) == set(MANAGED_ENV_VARS)  # session still ends


# --- project enter/leave as project-pinned locators ---------------------------


def test_project_enter_cross_workspace(tmp_path: Path) -> None:
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "onlyproj")
    shell_file = tmp_path / "cd"
    shell_file.touch()
    res = _run_multi(
        "project",
        "enter",
        "onlyproj",
        config=config,
        cwd=beta,
        workspace="beta",
        shell_file=shell_file,
    )
    assert res.returncode == 0, res.stderr
    assert _shell_cd(shell_file) == str(alpha / "onlyproj")
    assert _shell_exports(shell_file)["WTM_WORKSPACE"] == "alpha"


def test_project_enter_pin_ignores_a_same_named_worktree(root: Path, tmp_path: Path) -> None:
    # The pin keeps 'project enter' project-only even though 'wtm enter' would
    # cascade the same token into the worktree tier.
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    res = run("project", "enter", "feat", root=root, cwd=root, shell_file=tmp_path / "cd")
    assert res.returncode == 1
    assert "no project matches: feat" in res.stderr


def test_project_leave_cross_workspace(tmp_path: Path) -> None:
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "onlyproj")
    shell_file = tmp_path / "cd"
    shell_file.touch()
    res = _run_multi(
        "project",
        "leave",
        "onlyproj",
        config=config,
        cwd=beta,
        workspace="beta",
        shell_file=shell_file,
    )
    assert res.returncode == 0, res.stderr
    assert "left project 'onlyproj'" in res.stderr


def test_workspace_leave_full_chain_worktree_project_workspace(root: Path, tmp_path: Path) -> None:
    _seed_repo(root, "alpha")
    run("create", "alpha", "feat", root=root)
    workspace = tmp_path / "wshooks"
    shell_file = tmp_path / "cd"
    order = tmp_path / "order"
    _write_hook(
        root / "alpha" / ".git" / "hooks" / "wtm-leave", f'echo "wt:$WTM_BRANCH" >> "{order}"\n'
    )
    _write_hook(
        root / "alpha" / ".git" / "hooks" / "wtm-project-leave",
        f'echo "proj:$WTM_PROJECT" >> "{order}"\n',
    )
    _write_hook(workspace / "wtm-workspace-leave", f'echo "ws:$WTM_WORKSPACE" >> "{order}"\n')
    wt = root / "alpha" / "worktrees" / "feat"
    res = run(
        "workspace",
        "leave",
        root=root,
        cwd=wt,
        shell_file=shell_file,
        workspace_hooks_dir=workspace,
        extra_env={"OLDPWD": "/nonexistent-oldpwd"},
    )
    assert res.returncode == 0, res.stderr
    # Full inside-out chain: worktree -> project -> workspace.
    assert order.read_text().splitlines() == ["wt:feat", "proj:alpha", "ws:ws"]
    assert _shell_cd(shell_file) == str(root.parent)


# --- config locator: set/get/unset via locator prefix ----------------------


def test_config_set_via_locator_cross_workspace(tmp_path: Path) -> None:
    """config set with a locator prefix writes to the named entity, not cwd."""
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "proj")
    # Standing in beta, set a key on alpha/proj via locator.
    res = _run_multi(
        "config",
        "set",
        "alpha",
        "proj",
        "editor",
        "nvim",
        config=config,
        cwd=beta,
        workspace="beta",
    )
    assert res.returncode == 0, res.stderr
    assert (alpha / "proj" / ".wtm" / "config.toml").exists()
    text = (alpha / "proj" / ".wtm" / "config.toml").read_text()
    assert 'editor = "nvim"' in text


def test_config_get_via_locator_cross_workspace(tmp_path: Path) -> None:
    """config get with a locator prefix reads from the named entity."""
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "proj")
    # Set a key on alpha/proj, then read it from beta.
    _run_multi(
        "config",
        "set",
        "alpha",
        "proj",
        "editor",
        "nvim",
        config=config,
        cwd=alpha,
        workspace="alpha",
    )
    res = _run_multi(
        "config",
        "get",
        "alpha",
        "proj",
        "editor",
        config=config,
        cwd=beta,
        workspace="beta",
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "nvim"


def test_config_unset_via_locator_cross_workspace(tmp_path: Path) -> None:
    """config unset with a locator prefix removes from the named entity."""
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "proj")
    _run_multi(
        "config",
        "set",
        "alpha",
        "proj",
        "editor",
        "nvim",
        config=config,
        cwd=alpha,
        workspace="alpha",
    )
    assert (alpha / "proj" / ".wtm" / "config.toml").exists()
    # Unset from beta via locator.
    res = _run_multi(
        "config",
        "unset",
        "alpha",
        "proj",
        "editor",
        config=config,
        cwd=beta,
        workspace="beta",
    )
    assert res.returncode == 0, res.stderr
    text = (alpha / "proj" / ".wtm" / "config.toml").read_text()
    assert "editor" not in text


def test_config_set_locator_worktree_tier(tmp_path: Path) -> None:
    """config set via a worktree-tier locator writes to the worktree's config."""
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "proj")
    _run_multi("create", "proj", "feat", config=config, cwd=alpha, workspace="alpha")
    # From beta, set a key on alpha/proj/feat via the worktree name.
    res = _run_multi(
        "config",
        "set",
        "feat",
        "editor",
        "vim",
        config=config,
        cwd=beta,
        workspace="beta",
    )
    assert res.returncode == 0, res.stderr
    wt_config = alpha / "proj" / "worktrees" / "feat" / ".wtm" / "config.toml"
    assert wt_config.exists()
    assert 'editor = "vim"' in wt_config.read_text()


def test_config_get_locator_worktree_tier(tmp_path: Path) -> None:
    """config get via a worktree name resolves across workspaces."""
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "proj")
    _run_multi("create", "proj", "feat", config=config, cwd=alpha, workspace="alpha")
    _run_multi(
        "config",
        "set",
        "feat",
        "editor",
        "vim",
        config=config,
        cwd=alpha,
        workspace="alpha",
    )
    res = _run_multi(
        "config",
        "get",
        "feat",
        "editor",
        config=config,
        cwd=beta,
        workspace="beta",
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "vim"


def test_config_get_data_type_table_wins(tmp_path: Path) -> None:
    """config get A B where A is a table on cwd: SUBKEY reading (backward compat)."""
    config, roots = _multi_workspace_config(tmp_path, ["alpha"])
    alpha = roots[0]
    _seed_repo_at(alpha, "proj")
    # Set a table key on the project.
    proj_config = alpha / "proj" / ".wtm"
    proj_config.mkdir(parents=True, exist_ok=True)
    (proj_config / "config.toml").write_text('[labels]\nenv = "prod"\ntier = "web"\n')
    # config get labels env → table cells (backward compat, no locator).
    res = _run_multi(
        "config",
        "get",
        "labels",
        "env",
        config=config,
        cwd=alpha / "proj",
        workspace="alpha",
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "prod"


def test_config_get_data_type_non_table_uses_locator(tmp_path: Path) -> None:
    """config get A B where A is a scalar on cwd but a valid locator: LOCATOR+KEY."""
    config, roots = _multi_workspace_config(tmp_path, ["alpha"])
    alpha = roots[0]
    _seed_repo_at(alpha, "proj")
    _seed_repo_at(alpha, "other")
    # Set a scalar key 'other' on proj (cwd), and a key on project 'other'.
    proj_config = alpha / "proj" / ".wtm"
    proj_config.mkdir(parents=True, exist_ok=True)
    (proj_config / "config.toml").write_text('other = "scalar-value"\n')
    other_config = alpha / "other" / ".wtm"
    other_config.mkdir(parents=True, exist_ok=True)
    (other_config / "config.toml").write_text('editor = "nvim"\n')
    # config get other editor: 'other' is a scalar on cwd (not a table),
    # and 'other' is a valid locator (project name). → LOCATOR+KEY reading.
    res = _run_multi(
        "config",
        "get",
        "other",
        "editor",
        config=config,
        cwd=alpha / "proj",
        workspace="alpha",
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "nvim"


def test_config_get_data_type_neither_falls_back(tmp_path: Path) -> None:
    """config get A B where A is neither a table nor a locator: KEY+SUBKEY fallback."""
    config, roots = _multi_workspace_config(tmp_path, ["alpha"])
    alpha = roots[0]
    _seed_repo_at(alpha, "proj")
    # 'nonexistent' is not a table on cwd and not a locator name.
    res = _run_multi(
        "config",
        "get",
        "nonexistent",
        "sub",
        config=config,
        cwd=alpha / "proj",
        workspace="alpha",
    )
    assert res.returncode == 0, res.stderr
    # Falls back to KEY+SUBKEY: the table doesn't exist, so the cell is unset.
    assert res.stdout.strip() == "(unset)"


def test_config_get_three_args_locator_and_key(tmp_path: Path) -> None:
    """config get LOCATOR KEY with 3+ args resolves the locator."""
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "proj")
    _run_multi(
        "config",
        "set",
        "alpha",
        "proj",
        "editor",
        "nvim",
        config=config,
        cwd=alpha,
        workspace="alpha",
    )
    # 3 args: WORKSPACE PROJECT KEY
    res = _run_multi(
        "config",
        "get",
        "alpha",
        "proj",
        "editor",
        config=config,
        cwd=beta,
        workspace="beta",
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "nvim"


def test_config_get_three_args_subkey_reading(tmp_path: Path) -> None:
    """config get LOCATOR KEY SUBKEY: 4 args, SUBKEY reading when KEY is a table."""
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "proj")
    proj_config = alpha / "proj" / ".wtm"
    proj_config.mkdir(parents=True, exist_ok=True)
    (proj_config / "config.toml").write_text('[labels]\nenv = "prod"\n')
    # 4 args: WORKSPACE PROJECT KEY SUBKEY
    res = _run_multi(
        "config",
        "get",
        "alpha",
        "proj",
        "labels",
        "env",
        config=config,
        cwd=beta,
        workspace="beta",
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "prod"


def test_config_get_three_args_no_locator_error(tmp_path: Path) -> None:
    """config get with 3+ args where the prefix doesn't resolve: error."""
    config, roots = _multi_workspace_config(tmp_path, ["alpha"])
    alpha = roots[0]
    _seed_repo_at(alpha, "proj")
    res = _run_multi(
        "config",
        "get",
        "nonexistent",
        "proj",
        "editor",
        config=config,
        cwd=alpha,
        workspace="alpha",
    )
    assert res.returncode == 1
    assert "no entity matches" in res.stderr.lower()


def test_config_set_backward_compat_no_locator(root: Path) -> None:
    """config set KEY VALUE (no locator) is unchanged."""
    _seed_repo(root, "proj")
    res = run("config", "set", "editor", "nvim", root=root, cwd=root / "proj")
    assert res.returncode == 0, res.stderr
    got = run("config", "get", "editor", root=root, cwd=root / "proj")
    assert got.stdout.strip() == "nvim"


def test_config_get_backward_compat_key_only(root: Path) -> None:
    """config get KEY (1 arg, no locator) is unchanged."""
    _seed_repo(root, "proj")
    run("config", "set", "editor", "nvim", root=root, cwd=root / "proj")
    got = run("config", "get", "editor", root=root, cwd=root / "proj")
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == "nvim"


def test_config_get_backward_compat_key_subkey(root: Path) -> None:
    """config get KEY SUBKEY (2 args, table on cwd) is unchanged."""
    _seed_repo(root, "proj")
    (root / "proj" / ".wtm").mkdir(exist_ok=True)
    (root / "proj" / ".wtm" / "config.toml").write_text('[labels]\nenv = "prod"\n')
    got = run("config", "get", "labels", "env", root=root, cwd=root / "proj")
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == "prod"


def test_config_set_table_via_locator(tmp_path: Path) -> None:
    """config set -t with a locator prefix writes a table cell on the named entity."""
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "proj")
    res = _run_multi(
        "config",
        "set",
        "-t",
        "alpha",
        "proj",
        "labels",
        "env",
        "prod",
        config=config,
        cwd=beta,
        workspace="beta",
    )
    assert res.returncode == 0, res.stderr
    text = (alpha / "proj" / ".wtm" / "config.toml").read_text()
    assert "[labels]" in text
    assert 'env = "prod"' in text


def test_config_unset_table_via_locator(tmp_path: Path) -> None:
    """config unset -t with a locator prefix removes a table cell."""
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "proj")
    _run_multi(
        "config",
        "set",
        "-t",
        "alpha",
        "proj",
        "labels",
        "env",
        "prod",
        config=config,
        cwd=alpha,
        workspace="alpha",
    )
    res = _run_multi(
        "config",
        "unset",
        "-t",
        "alpha",
        "proj",
        "labels",
        "env",
        config=config,
        cwd=beta,
        workspace="beta",
    )
    assert res.returncode == 0, res.stderr
    text = (alpha / "proj" / ".wtm" / "config.toml").read_text()
    assert "env" not in text


# --- config locator: completion --------------------------------------------


def test_config_set_completion_locator_union(tmp_path: Path) -> None:
    """config set <TAB> offers both locator names and cwd entity keys."""
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "proj")
    _seed_repo_at(beta, "other")
    # Seed a key on beta/other (cwd entity).
    other_config = beta / "other" / ".wtm"
    other_config.mkdir(parents=True, exist_ok=True)
    (other_config / "config.toml").write_text('editor = "nvim"\n')
    # Complete from inside beta/other.
    env = {
        **os.environ,
        "WTM_CONFIG": str(config),
        "WTM_SYSTEM_CONFIG": str(config.parent / "_no_system_config.toml"),
        "WTM_WORKSPACE": "beta",
        "_WTM_COMPLETE": "complete_bash",
        "COMP_WORDS": "wtm config set ",
        "COMP_CWORD": "3",
    }
    res = subprocess.run(
        [str(Path(sys.executable).parent / "wtm")],
        env=env,
        cwd=str(beta / "other"),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    names = res.stdout.split()
    # Locator universe: projects + workspaces.
    assert "alpha" in names
    assert "beta" in names
    assert "proj" in names
    assert "other" in names
    # Cwd entity keys.
    assert "editor" in names


def test_config_get_completion_locator_union(tmp_path: Path) -> None:
    """config get <TAB> offers both locator names and cwd entity keys."""
    config, roots = _multi_workspace_config(tmp_path, ["alpha", "beta"])
    alpha, beta = roots
    _seed_repo_at(alpha, "proj")
    _seed_repo_at(beta, "other")
    other_config = beta / "other" / ".wtm"
    other_config.mkdir(parents=True, exist_ok=True)
    (other_config / "config.toml").write_text('editor = "nvim"\n')
    env = {
        **os.environ,
        "WTM_CONFIG": str(config),
        "WTM_SYSTEM_CONFIG": str(config.parent / "_no_system_config.toml"),
        "WTM_WORKSPACE": "beta",
        "_WTM_COMPLETE": "complete_bash",
        "COMP_WORDS": "wtm config get ",
        "COMP_CWORD": "3",
    }
    res = subprocess.run(
        [str(Path(sys.executable).parent / "wtm")],
        env=env,
        cwd=str(beta / "other"),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    names = res.stdout.split()
    assert "proj" in names
    assert "other" in names
    assert "editor" in names


def test_config_get_completion_subkey_reading(tmp_path: Path) -> None:
    """config get A <TAB> offers SUBKEYs when A is a table on cwd."""
    config, roots = _multi_workspace_config(tmp_path, ["alpha"])
    alpha = roots[0]
    _seed_repo_at(alpha, "proj")
    proj_config = alpha / "proj" / ".wtm"
    proj_config.mkdir(parents=True, exist_ok=True)
    (proj_config / "config.toml").write_text('[labels]\nenv = "prod"\ntier = "web"\n')
    env = {
        **os.environ,
        "WTM_CONFIG": str(config),
        "WTM_SYSTEM_CONFIG": str(config.parent / "_no_system_config.toml"),
        "WTM_WORKSPACE": "alpha",
        "_WTM_COMPLETE": "complete_bash",
        "COMP_WORDS": "wtm config get labels ",
        "COMP_CWORD": "4",
    }
    res = subprocess.run(
        [str(Path(sys.executable).parent / "wtm")],
        env=env,
        cwd=str(alpha / "proj"),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    names = res.stdout.split()
    assert "env" in names
    assert "tier" in names
