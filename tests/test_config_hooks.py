"""The post-config event: broadcast firing after an invocation wrote config.

Covers the contract (``WTM_CONFIG_FILES``, the ``WTM_IN_HOOK`` sentinel, the
anchor ``$1``), the no-write no-fire rule, re-entry suppression when a hook
itself writes config, and scope resolution implied by the changed paths.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _use_config_env(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    """Isolate wtm env and return a sole-workspace user config path (unwritten)."""
    root.mkdir(parents=True, exist_ok=True)
    for key in ("WTM_WORKSPACE", "WTM_WORKTREE_DIR", "WTM_BRANCH", "WTM_PROJECT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WTM_SYSTEM_CONFIG", str(root / "_no_system_config.toml"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(root / "_gitconfig"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")

    return root / "_wtm_config.toml"


def _run(*args: str) -> tuple[int, str]:
    """One in-process wtm invocation with redirected streams."""
    prev_argv, prev_cwd = sys.argv, os.getcwd()
    out, err = io.StringIO(), io.StringIO()
    try:
        sys.argv = ["wtm", *args]
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                from wtm.cli.app import main

                main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 0
    finally:
        os.chdir(prev_cwd)
        sys.argv = prev_argv

    return code, err.getvalue()


def _toml_str(value: str) -> str:
    """Render *value* as a TOML basic string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _read_env(path: Path) -> dict[str, str]:
    """Parse an ``env`` dump into a dict (last assignment wins)."""
    result: dict[str, str] = {}
    for line in path.read_text().splitlines():
        key, _, value = line.partition("=")
        if key:
            result[key] = value

    return result


def _seed_repo(root: Path, project: str) -> Path:
    """Scaffold ``<root>/<project>`` as a bare repo with one commit."""
    source = root / "_sources" / project
    source.mkdir(parents=True)
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
    (source / "file.txt").write_text("hello\n")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "init"], check=True)

    project_dir = root / project
    project_dir.mkdir()
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(source), str(project_dir / ".git")],
        check=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": str(root / "_gitconfig")},
    )

    return project_dir


def test_post_config_fires_once_with_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One flush fires once: FILES/SENTINEL/EVENT exported, $1 is the anchor."""
    config_path = _use_config_env(monkeypatch, tmp_path)
    marker = tmp_path / "hook.env"
    arg_file = tmp_path / "hook.arg"
    counter = tmp_path / "hook.count"
    hook = f'env > {marker}; echo "$1" > {arg_file}; echo run >> {counter}'
    config_path.write_text(
        f'[workspaces.ws]\npath = "{tmp_path}"\n[hooks.on]\npost-config = {_toml_str(hook)}\n'
    )
    monkeypatch.setenv("WTM_CONFIG", str(config_path))

    code, err = _run("config", "set", "-g", "ci.image", "ubuntu")

    assert code == 0, err
    assert counter.read_text() == "run\n", "post-config must fire exactly once"

    env = _read_env(marker)
    assert env["WTM_EVENT"] == "post-config"
    assert env["WTM_CONFIG_FILES"] == str(config_path)
    assert env["WTM_IN_HOOK"] == "1"
    assert "WTM_WORKTREE_DIR" not in env, "no subject vars on the broadcast contract"
    assert "WTM_PROJECT" not in env
    assert arg_file.read_text().strip() == str(tmp_path)


def test_no_write_no_fire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A read-only command leaves the event unfired."""
    config_path = _use_config_env(monkeypatch, tmp_path)
    counter = tmp_path / "hook.count"
    config_path.write_text(
        f'[workspaces.ws]\npath = "{tmp_path}"\n'
        "[hooks.on]\n"
        f'post-config = "echo run >> {counter}"\n'
    )
    monkeypatch.setenv("WTM_CONFIG", str(config_path))

    code, err = _run("config", "show", "-g")

    assert code == 0, err
    assert not counter.exists()


def test_sentinel_prevents_recursion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hook that itself writes config does not trigger a second firing."""
    config_path = _use_config_env(monkeypatch, tmp_path)
    counter = tmp_path / "hook.count"
    inner = " ".join([sys.executable, "-m", "wtm.cli", "config", "set", "-g", "nested", "1"])
    config_path.write_text(
        f'[workspaces.ws]\npath = "{tmp_path}"\n'
        "[hooks.on]\n"
        f'post-config = "{inner}; echo run >> {counter}"\n'
    )
    monkeypatch.setenv("WTM_CONFIG", str(config_path))

    code, err = _run("config", "set", "-g", "outer", "1")

    assert code == 0, err
    assert counter.read_text() == "run\n", "the sentinel must suppress the nested fire"

    from wtm.config import Config, ConfigAddress

    assert Config.load().get_value(ConfigAddress.user(), ("nested",)) == "1"


def test_implied_entity_scopes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Writing a project's config implies its workspace scope; no worktree tier."""
    config_path = _use_config_env(monkeypatch, tmp_path)
    project = _seed_repo(tmp_path, "api")
    ws_marker = tmp_path / "ws.count"
    proj_marker = tmp_path / "proj.count"

    config_path.write_text(
        f'[workspaces.ws]\npath = "{tmp_path}"\n'
        "[workspaces.ws.hooks.on]\n"
        f'post-config = "echo run >> {ws_marker}"\n'
    )
    monkeypatch.setenv("WTM_CONFIG", str(config_path))
    proj_config = project / ".wtm" / "config.toml"
    proj_config.parent.mkdir(parents=True, exist_ok=True)
    proj_config.write_text(f'[hooks.on]\npost-config = "echo run >> {proj_marker}"\n')

    code, err = _run("project", "set", "api", "-d", "desc")

    assert code == 0, err
    assert proj_marker.exists(), "project-scope inline hook must fire"
    assert ws_marker.exists(), "workspace-scope inline hook must fire via implication"


def test_store_records_flushed_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Config.flushed_config_files reports exactly the files written, in order."""
    config_path = _use_config_env(monkeypatch, tmp_path)
    config_path.write_text(f'[workspaces.ws]\npath = "{tmp_path}"\n')
    monkeypatch.setenv("WTM_CONFIG", str(config_path))

    from wtm.config import Config, ConfigAddress

    config = Config.load()
    assert config.flushed_config_files == []
    config.set_value(ConfigAddress.user(), ("a",), "1")
    config.set_value(ConfigAddress.user(), ("b",), "2")

    # The raw flush log keeps one entry per write; the event dedupes.
    assert config.flushed_config_files == [config_path, config_path]
