"""Plugin system tests: manifest contract, discovery, registry fold, firing, CLI."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from wtm.cli.app import main as cli_main
from wtm.config import Settings
from wtm.config.manifest import parse_manifest
from wtm.exceptions import ManifestError, WtmError
from wtm.hooks import run_workspace_hooks

# --- fixtures and helpers -----------------------------------------------------


def _isolate(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    """Point wtm at a throwaway sole-workspace config under *root*."""
    root.mkdir(parents=True, exist_ok=True)
    config = root / "_wtm_config.toml"
    config.write_text(f'[workspaces.ws]\npath = "{root / "ws"}"\n')
    monkeypatch.setenv("WTM_CONFIG", str(config))
    monkeypatch.setenv("WTM_SYSTEM_CONFIG", str(root / "_no_system.toml"))
    monkeypatch.setenv("WTM_SYSTEM_HOOKS_DIR", str(root / "_no_system_hooks"))
    monkeypatch.setenv("WTM_USER_HOOKS_DIR", str(root / "_no_user_hooks"))

    return config


def _write_user(config: Path, text: str) -> None:
    with config.open("a") as handle:
        handle.write(text)


@dataclass
class _FakeEP:
    """A stand-in for an importlib.metadata EntryPoint."""

    name: str
    payload: object
    fail: bool = False
    value: str = "fake:mod"

    def load(self) -> object:
        if self.fail:
            raise RuntimeError("boom")

        return self.payload


def _patch_eps(monkeypatch: pytest.MonkeyPatch, eps: list[_FakeEP]) -> None:
    monkeypatch.setattr(
        "wtm.plugins.entry_points", lambda group=None: iter(eps) if group else iter(eps)
    )


@dataclass
class _Result:
    returncode: int
    stdout: str
    stderr: str


def _run_cli(*args: str, root: Path, monkeypatch: pytest.MonkeyPatch) -> _Result:
    """Run the CLI against the isolated config (in-process, like test_wtm)."""
    prev_env = dict(os.environ)
    prev_argv = sys.argv
    prev_cwd = os.getcwd()
    out, err = io.StringIO(), io.StringIO()
    try:
        os.environ["WTM_CONFIG"] = str(root / "_wtm_config.toml")
        os.environ["WTM_SYSTEM_CONFIG"] = str(root / "_no_system.toml")
        os.environ["WTM_SYSTEM_HOOKS_DIR"] = str(root / "_no_system_hooks")
        os.environ["WTM_USER_HOOKS_DIR"] = str(root / "_no_user_hooks")
        os.environ.pop("WTM_WORKSPACE", None)
        os.chdir(root)
        sys.argv = ["wtm", *args]
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli_main()
            except SystemExit as exc:  # typer exits non-zero on usage errors
                code = exc.code if isinstance(exc.code, int) else 0
            except WtmError:
                code = 1
    finally:
        os.chdir(prev_cwd)
        sys.argv = prev_argv
        os.environ.clear()
        os.environ.update(prev_env)

    return _Result(returncode=code, stdout=out.getvalue(), stderr=err.getvalue())


def _command_plugin(root: Path, name: str, manifest: dict) -> Path:
    """An executable on PATH answering ``<name> wtm-manifest --json``."""
    bin_dir = root / "_bin"
    bin_dir.mkdir(exist_ok=True)
    exe = bin_dir / name
    exe.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "wtm-manifest" ]; then\n'
        f"printf '%s' '{json.dumps(manifest)}'\n"
        "exit 0\nfi\nexit 1\n"
    )
    exe.chmod(0o755)

    return bin_dir


# --- manifest contract --------------------------------------------------------


class TestParseManifest:
    def test_full_roundtrip(self) -> None:
        raw = {
            "name": "p",
            "api_version": 1,
            "hooks": {"post-create": ["cmd a", "cmd b"], "enter": "single"},
            "namespaces": ["p"],
            "inheritable": [{"key": "p.tier", "floor": "project"}, "plain"],
            "defaults": [{"key": "p.enabled", "value": True}],
        }
        manifest = parse_manifest(raw, name="p", source="entry-point")
        assert manifest.name == "p"
        assert manifest.source == "entry-point"
        assert manifest.hooks == {"post-create": ("cmd a", "cmd b"), "enter": ("single",)}
        assert manifest.namespaces == ("p",)
        floors = [(d.key, d.floor) for d in manifest.inheritable]
        assert floors == [("p.tier", "project"), ("plain", None)]
        assert manifest.defaults == ((("p", "enabled"), True),)

    def test_unknown_field_refused(self) -> None:
        with pytest.raises(ManifestError, match="unknown manifest field"):
            parse_manifest({"name": "p", "wat": 1}, name="p", source="command")

    def test_api_version_ceiling(self) -> None:
        with pytest.raises(ManifestError, match="upgrade wtm"):
            parse_manifest({"name": "p", "api_version": 99}, name="p", source="command")

    def test_name_mismatch_refused(self) -> None:
        with pytest.raises(ManifestError, match="names itself"):
            parse_manifest({"name": "other"}, name="p", source="command")

    def test_non_object_refused(self) -> None:
        with pytest.raises(ManifestError, match="must be a"):
            parse_manifest([1, 2], name="p", source="command")

    def test_bad_hooks_shape_refused(self) -> None:
        with pytest.raises(ManifestError, match="hooks.enter"):
            parse_manifest({"hooks": {"enter": 3}}, name="p", source="command")

    def test_non_scalar_default_refused(self) -> None:
        with pytest.raises(ManifestError, match="scalar"):
            parse_manifest({"defaults": [{"key": "p", "value": []}]}, name="p", source="command")

    def test_to_json_omits_empty_sections(self) -> None:
        manifest = parse_manifest({"name": "p"}, name="p", source="command")
        assert manifest.to_json() == {"name": "p", "api_version": 1}


# --- resolution ---------------------------------------------------------------


class TestResolve:
    def test_entry_point_wins_over_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate(monkeypatch, tmp_path)
        bin_dir = _command_plugin(
            tmp_path, "dual", {"name": "dual", "hooks": {"enter": ["from-command"]}}
        )
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
        _patch_eps(
            monkeypatch,
            [_FakeEP("dual", {"name": "dual", "hooks": {"enter": ["from-entry-point"]}})],
        )

        from wtm.plugins import resolve_manifest

        manifest = resolve_manifest("dual")
        assert manifest.source == "entry-point"
        assert manifest.hooks["enter"] == ("from-entry-point",)

    def test_broken_entry_point_does_not_fall_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate(monkeypatch, tmp_path)
        bin_dir = _command_plugin(
            tmp_path, "dual", {"name": "dual", "hooks": {"enter": ["from-command"]}}
        )
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
        _patch_eps(monkeypatch, [_FakeEP("dual", {"name": "dual"}, fail=True)])

        from wtm.plugins import resolve_manifest

        with pytest.raises(ManifestError, match="failed to load"):
            resolve_manifest("dual")

    def test_command_form_used_without_entry_point(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate(monkeypatch, tmp_path)
        bin_dir = _command_plugin(tmp_path, "cmdplug", {"name": "cmdplug"})
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

        from wtm.plugins import resolve_manifest

        manifest = resolve_manifest("cmdplug")
        assert manifest.source == "command"
        assert manifest.origin.endswith("cmdplug")

    def test_unresolvable_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolate(monkeypatch, tmp_path)
        _patch_eps(monkeypatch, [])

        from wtm.plugins import resolve_manifest

        with pytest.raises(ManifestError, match="not resolvable"):
            resolve_manifest("nowhere")

    def test_failing_command_is_a_problem(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate(monkeypatch, tmp_path)
        bin_dir = tmp_path / "_bin"
        bin_dir.mkdir()
        bad = bin_dir / "badplug"
        bad.write_text("#!/bin/sh\nexit 3\n")
        bad.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
        _patch_eps(monkeypatch, [])

        from wtm.plugins import resolve_manifest

        with pytest.raises(ManifestError, match="exited 3"):
            resolve_manifest("badplug")

    def test_load_active_skips_and_reports(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = _isolate(monkeypatch, tmp_path)
        _patch_eps(monkeypatch, [])
        (tmp_path / "ws").mkdir()

        _write_user(config_file, "[plugins]\ngood = true\nghost = true\n")
        bin_dir = _command_plugin(tmp_path, "good", {"name": "good", "namespaces": ["good"]})
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

        from wtm.config.config import Config
        from wtm.plugins import load_active

        config = Config.load(settings=Settings.from_env())
        manifests, problems = load_active(config)

        assert [m.name for m in manifests] == ["good"]
        assert len(problems) == 1 and "ghost" in problems[0]


# --- registry fold ------------------------------------------------------------


class TestRegistryFold:
    def _config_with_cells(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user: str, ws_local: str = ""
    ):
        config_file = _isolate(monkeypatch, tmp_path)
        _write_user(config_file, user)
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir(exist_ok=True)
        if ws_local:
            (ws_dir / ".wtm").mkdir(exist_ok=True)
            (ws_dir / ".wtm" / "config.toml").write_text(ws_local)

        from wtm.config.config import Config

        return Config.load(settings=Settings.from_env()), ws_dir

    def test_workspace_disable_shadows_user_enable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config, ws_dir = self._config_with_cells(
            tmp_path, monkeypatch, "[plugins]\na = true\nb = true\n", "[plugins]\nb = false\n"
        )

        assert config.plugin_registry() == {"a": True, "b": True}
        assert config.plugin_registry(ws=config.workspaces["ws"]) == {"a": True, "b": False}

    def test_per_cell_independence(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config, ws_dir = self._config_with_cells(
            tmp_path, monkeypatch, "[plugins]\na = true\n", "[plugins]\nc = true\n"
        )
        merged = config.plugin_registry(ws=config.workspaces["ws"])

        assert merged == {"a": True, "c": True}

    def test_non_boolean_cell_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config, _ws = self._config_with_cells(tmp_path, monkeypatch, '[plugins]\na = "yes"\n')

        with pytest.raises(WtmError, match="must be a boolean"):
            config.plugin_registry()

    def test_non_table_plugins_key_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A bare scalar must sit before any table headers, so rewrite wholesale.
        root = tmp_path
        root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("WTM_CONFIG", str(root / "_wtm_config.toml"))
        monkeypatch.setenv("WTM_SYSTEM_CONFIG", str(root / "_no_system.toml"))
        (root / "_wtm_config.toml").write_text(
            f'plugins = 3\n[workspaces.ws]\npath = "{root / "ws"}"\n'
        )

        from wtm.config.config import Config

        config = Config.load(settings=Settings.from_env())
        with pytest.raises(Exception, match="must be a table"):
            config.plugin_registry()


# --- contributions ------------------------------------------------------------


class TestContributions:
    def _attached(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        manifests: list[dict],
        names: list[str],
        user_extra: str = "",
    ):
        config_file = _isolate(monkeypatch, tmp_path)
        _write_user(config_file, "[plugins]\n" + "".join(f"{n} = true\n" for n in names))
        if user_extra:
            _write_user(config_file, user_extra)
        parsed = [
            parse_manifest(m | {"name": n}, name=n, source="command")
            for n, m in zip(names, manifests, strict=True)
        ]

        from wtm.config.config import Config

        return Config.load(settings=Settings.from_env()).attach_plugins(parsed)

    def test_commands_ordered_by_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = self._attached(
            tmp_path,
            monkeypatch,
            [{"hooks": {"enter": ["z-cmd"]}}, {"hooks": {"enter": ["a-cmd"]}}],
            ["zed", "alpha"],
        )
        rows = config.plugin_commands("enter")

        assert rows == [("alpha", "a-cmd"), ("zed", "z-cmd")]

    def test_namespaces_union_actives_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = _isolate(monkeypatch, tmp_path)
        _write_user(config_file, "[plugins]\non = true\noff = true\n")
        parsed = [
            parse_manifest({"name": "on", "namespaces": "wtm-on"}, name="on", source="command"),
            parse_manifest({"name": "off", "namespaces": "wtm-off"}, name="off", source="command"),
        ]
        # Deactivate 'off' at the workspace tier.
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        (ws_dir / ".wtm").mkdir()
        (ws_dir / ".wtm" / "config.toml").write_text("[plugins]\noff = false\n")

        from wtm.config.config import Config

        config = Config.load(settings=Settings.from_env()).attach_plugins(parsed)

        assert config.plugin_namespaces() == frozenset({"wtm-on", "wtm-off"})
        assert config.plugin_namespaces(ws=config.workspaces["ws"]) == frozenset({"wtm-on"})

    def test_inactive_plugin_contributes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = _isolate(monkeypatch, tmp_path)
        _write_user(config_file, "[plugins]\np = true\n")
        manifest = parse_manifest(
            {"name": "p", "hooks": {"enter": ["x"]}}, name="p", source="command"
        )

        from wtm.config.config import Config

        config = Config.load(settings=Settings.from_env()).attach_plugins([manifest])
        assert config.plugin_commands("enter") == [("p", "x")]

        # Flip the cell off; a fresh snapshot with the same manifest attached
        # contributes nothing.
        text = config_file.read_text().replace("p = true", "p = false")
        config_file.write_text(text)
        fresh = Config.load(settings=Settings.from_env()).attach_plugins([manifest])

        assert fresh.plugin_commands("enter") == []


class TestInheritableUnion:
    def _seeded_tree(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        pre: str = "",
        post: str = "",
        ws_local: str = "",
    ):
        """A declared workspace with one project dir; returns (config, ws, proj).

        *pre* holds top-level scalars (they must precede every table header);
        *post* holds additional top-level tables.
        """
        config_file = _isolate(monkeypatch, tmp_path)
        ws_dir = tmp_path / "ws"
        proj_dir = ws_dir / "proj"
        proj_dir.mkdir(parents=True)
        (proj_dir / ".git").mkdir()
        wt_dir = proj_dir / "worktrees" / "main"
        wt_dir.mkdir(parents=True)
        config_file.write_text(f'{pre}[workspaces.ws]\npath = "{ws_dir}"\n{post}')
        if ws_local:
            (ws_dir / ".wtm").mkdir()
            (ws_dir / ".wtm" / "config.toml").write_text(ws_local)

        from wtm.config.config import Config

        return Config.load(settings=Settings.from_env()), ws_dir, proj_dir

    def test_plugin_declared_key_inherits_into_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config, ws_dir, proj_dir = self._seeded_tree(
            tmp_path,
            monkeypatch,
            # A global-tier default reaches the project only through the
            # inheritance the plugin's declaration enables.
            pre='[svc]\nregion = "eu"\n',
            post="[plugins]\np = true\n",
        )
        manifest = parse_manifest(
            {"name": "p", "inheritable": ["svc"]}, name="p", source="command"
        )
        config = config.attach_plugins([manifest])

        annotation = config.project_annotation(config.workspaces["ws"], "proj")

        assert annotation.extra.get("svc") == {"region": "eu"}

    def test_deactivation_removes_the_baseline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config, ws_dir, proj_dir = self._seeded_tree(
            tmp_path,
            monkeypatch,
            pre='[svc]\nregion = "eu"\n',
            ws_local="[plugins]\np = false\n",
        )
        manifest = parse_manifest(
            {"name": "p", "inheritable": ["svc"]}, name="p", source="command"
        )
        config = config.attach_plugins([manifest])

        annotation = config.project_annotation(config.workspaces["ws"], "proj")

        assert "svc" not in annotation.extra

    def test_user_floor_outranks_plugin_floor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config, ws_dir, proj_dir = self._seeded_tree(
            tmp_path,
            monkeypatch,
            pre='inheritable = [{ key = "svc", floor = "workspace" }]\n',
            post="[plugins]\np = true\n",
        )
        manifest = parse_manifest(
            {"name": "p", "inheritable": [{"key": "svc", "floor": "project"}]},
            name="p",
            source="command",
        )
        config = config.attach_plugins([manifest])

        decls = config.inheritable_decls(config.workspaces["ws"], "proj")

        assert decls["svc"] == "workspace"


# --- firing -------------------------------------------------------------------


class TestFiring:
    def _ws_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        marker: Path,
        *,
        system_hook: bool = False,
        user_extra: str = "",
    ):
        config_file = _isolate(monkeypatch, tmp_path)
        monkeypatch.setenv("WTM_SYSTEM_HOOKS_DIR", str(tmp_path / "syshooks"))
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir(exist_ok=True)
        config_file.write_text(
            f'[workspaces.ws]\npath = "{ws_dir}"\n[plugins]\nplug = true\n{user_extra}'
        )
        if system_hook:
            syshooks = tmp_path / "syshooks"
            syshooks.mkdir(exist_ok=True)
            hook = syshooks / "workspace-enter"
            hook.write_text(f"#!/bin/sh\necho system >> {marker}\n")
            hook.chmod(0o755)

        manifest = parse_manifest(
            {
                "name": "plug",
                "hooks": {
                    "workspace-enter": [f"echo plugin >> {marker}"],
                    "workspace-leave": [f"echo plugin >> {marker}"],
                },
            },
            name="plug",
            source="command",
        )

        from wtm.config.config import Config

        config = Config.load(settings=Settings.from_env())

        return config.attach_plugins([manifest]), ws_dir

    def test_setup_fires_plugin_before_system_scopes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        marker = tmp_path / "log"
        config, ws_dir = self._ws_config(tmp_path, monkeypatch, marker, system_hook=True)
        run_workspace_hooks("workspace-enter", "ws", ws_dir, config, strict=False)

        assert marker.read_text().splitlines() == ["plugin", "system"]

    def test_teardown_fires_plugin_last(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        marker = tmp_path / "log"
        config, ws_dir = self._ws_config(tmp_path, monkeypatch, marker)
        run_workspace_hooks("workspace-leave", "ws", ws_dir, config, strict=False)

        assert marker.read_text().splitlines() == ["plugin"]

    def test_enabled_false_kills_plugin_tier(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        marker = tmp_path / "log"
        config, ws_dir = self._ws_config(
            tmp_path, monkeypatch, marker, user_extra="[hooks]\nenabled = false\n"
        )
        run_workspace_hooks("workspace-enter", "ws", ws_dir, config, strict=False)

        assert not marker.exists()

    def test_strict_makes_failing_plugin_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = _isolate(monkeypatch, tmp_path)
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir(exist_ok=True)
        config_file.write_text(f'[workspaces.ws]\npath = "{ws_dir}"\n[plugins]\nplug = true\n')
        manifest = parse_manifest(
            {"name": "plug", "hooks": {"workspace-enter": ["false"]}},
            name="plug",
            source="command",
        )

        from wtm.config.config import Config

        config = Config.load(settings=Settings.from_env()).attach_plugins([manifest])
        with pytest.raises(WtmError, match="hook failed"):
            run_workspace_hooks("workspace-enter", "ws", ws_dir, config, strict=True)


# --- CLI surface --------------------------------------------------------------


class TestCliPlugins:
    def test_add_writes_cell_and_list_shows_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate(monkeypatch, tmp_path)
        _patch_eps(monkeypatch, [_FakeEP("epplug", {"name": "epplug", "namespaces": ["epplug"]})])
        (tmp_path / "ws").mkdir()

        res = _run_cli("plugin", "add", "epplug", root=tmp_path, monkeypatch=monkeypatch)
        assert res.returncode == 0
        assert "registered plugin 'epplug'" in res.stderr
        assert "owns namespace(s): epplug" in res.stderr

        config_file = tmp_path / "_wtm_config.toml"
        assert "[plugins]" in config_file.read_text()
        assert "epplug = true" in config_file.read_text()

        res = _run_cli("plugin", "list", root=tmp_path, monkeypatch=monkeypatch)
        assert res.returncode == 0
        assert "epplug" in res.stdout

        res = _run_cli("plugin", "remove", "epplug", root=tmp_path, monkeypatch=monkeypatch)
        assert res.returncode == 0
        assert "epplug = true" not in config_file.read_text()

    def test_add_unresolvable_registers_dormant_and_list_flags_broken(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate(monkeypatch, tmp_path)
        _patch_eps(monkeypatch, [])
        (tmp_path / "ws").mkdir()

        res = _run_cli("plugin", "add", "ghost", root=tmp_path, monkeypatch=monkeypatch)
        assert res.returncode == 0
        assert "stays dormant" in res.stderr

        res = _run_cli("plugin", "list", root=tmp_path, monkeypatch=monkeypatch)
        assert "broken" in res.stdout
        assert res.returncode == 0

    def test_seed_writes_defaults_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate(monkeypatch, tmp_path)
        _patch_eps(
            monkeypatch,
            [
                _FakeEP(
                    "seeded",
                    {
                        "name": "seeded",
                        "defaults": [{"key": "seeded.enabled", "value": True}],
                    },
                )
            ],
        )
        (tmp_path / "ws").mkdir()

        res = _run_cli("plugin", "add", "seeded", "--seed", root=tmp_path, monkeypatch=monkeypatch)
        assert res.returncode == 0
        assert "seeded seeded.enabled = True" in res.stderr
        text = (tmp_path / "_wtm_config.toml").read_text()
        assert "[seeded]" in text or "seeded.enabled" in text.replace(".", "")

        # Seeding is one-shot: remove leaves the value in place.
        res = _run_cli("plugin", "remove", "seeded", root=tmp_path, monkeypatch=monkeypatch)
        assert res.returncode == 0
        assert "seeded" in (tmp_path / "_wtm_config.toml").read_text()

    def test_remove_unregistered_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate(monkeypatch, tmp_path)
        _patch_eps(monkeypatch, [])

        res = _run_cli("plugin", "remove", "nope", root=tmp_path, monkeypatch=monkeypatch)
        assert res.returncode == 1
        assert "not registered" in res.stderr

    def test_show_rejects_unregistered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate(monkeypatch, tmp_path)
        _patch_eps(monkeypatch, [])

        res = _run_cli("plugin", "show", "nope", root=tmp_path, monkeypatch=monkeypatch)
        assert res.returncode == 1

    def test_post_config_dogfood_new_plugin_fires_on_registration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        marker = tmp_path / "dogfood"
        manifest = {
            "name": "dogfood",
            "hooks": {"post-config": [f"echo fired >> {marker}"]},
        }
        _isolate(monkeypatch, tmp_path)
        _patch_eps(monkeypatch, [_FakeEP("dogfood", manifest)])
        (tmp_path / "ws").mkdir()

        res = _run_cli("plugin", "add", "dogfood", root=tmp_path, monkeypatch=monkeypatch)
        assert res.returncode == 0
        # The freshly attached plugin sees its own registration write.
        assert marker.exists() and marker.read_text().strip() == "fired"

    def test_reserved_plugins_key_refused_for_arbitrary_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate(monkeypatch, tmp_path)
        _patch_eps(monkeypatch, [])

        res = _run_cli(
            "config",
            "set",
            "plugins",
            "x",
            "-g",
            root=tmp_path,
            monkeypatch=monkeypatch,
        )
        assert res.returncode == 1
        assert "reserved" in res.stderr


# --- namespace ownership warnings --------------------------------------------


class TestNamespaceWarning:
    def test_inactive_owner_warns_on_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = _isolate(monkeypatch, tmp_path)
        (tmp_path / "ws").mkdir()
        _write_user(config_file, "[plugins]\nwtm-server = false\n")
        _patch_eps(monkeypatch, [_FakeEP("wtm-server", {"name": "wtm-server"})])

        res = _run_cli(
            "config",
            "set",
            "wtm-server.web.port",
            "8080",
            "-g",
            root=tmp_path,
            monkeypatch=monkeypatch,
        )
        assert res.returncode == 0
        assert "not active" in res.stderr

    def test_no_warning_for_unknown_heads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate(monkeypatch, tmp_path)
        _patch_eps(monkeypatch, [])

        res = _run_cli(
            "config",
            "set",
            "ci.runner",
            "ubuntu",
            "-g",
            root=tmp_path,
            monkeypatch=monkeypatch,
        )
        assert res.returncode == 0
        assert "not active" not in res.stderr
