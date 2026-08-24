"""Plugin discovery: turning registered names into manifests.

A plugin is *registered* by a ``[plugins]`` cell in config and *resolved*
here, once per invocation: the canonical name is looked up first as a
``wtm.plugins`` entry point (in-process; the richer form) and then as a
command -- ``<name> wtm-manifest --json`` on PATH (subprocess; works across
virtualenvs). An entry point commits the resolution: when one exists for the
name it wins even if it fails to load, so a broken install is reported
instead of silently masked by a stale executable.

Resolution failures never abort the invocation: :func:`load_active` collects
warn-and-skip problems and returns only the manifests that resolved, leaving
the registry cell (and ``plugin list``'s broken flag) to tell the story.
"""

from __future__ import annotations

__all__ = [
    "ENTRY_POINT_GROUP",
    "MANIFEST_SUBCOMMAND",
    "PLUGIN_API_VERSION",
    "attach_active_plugins",
    "entry_point_names",
    "load_active",
    "resolve_manifest",
]

import json
import shutil
import subprocess
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path

from wtm.config import Config, PluginManifest, Workspace, parse_manifest
from wtm.exceptions import ManifestError
from wtm.output import warn

#: The entry-point group a plugin package publishes its manifest under.
ENTRY_POINT_GROUP = "wtm.plugins"

#: The subcommand a command-form plugin answers with its JSON manifest.
MANIFEST_SUBCOMMAND = "wtm-manifest"

#: Re-exported from the manifest contract: the enforced API ceiling.
PLUGIN_API_VERSION = 1

_MANIFEST_TIMEOUT_S = 10


def resolve_manifest(name: str) -> PluginManifest:
    """Resolve plugin *name*'s manifest: entry point first, else command form.

    :raises ManifestError: when the plugin cannot be resolved or its manifest
        is malformed. The caller decides between fatal and warn-and-skip.
    """
    ep = _find_entry_point(name)
    if ep is not None:
        return _manifest_from_entry_point(name, ep)

    exe = shutil.which(name)
    if exe is None:
        raise ManifestError(
            f"plugin '{name}' is not resolvable: no '{ENTRY_POINT_GROUP}' entry point "
            f"and no '{name}' command on PATH"
        )

    return _manifest_from_command(name, Path(exe))


def entry_point_names() -> list[str]:
    """Every name published under :data:`ENTRY_POINT_GROUP` (completion material)."""
    try:
        eps = entry_points(group=ENTRY_POINT_GROUP)
    except Exception:  # defensive: metadata scanning must never break completion
        return []

    return sorted(ep.name for ep in eps)


def load_active(
    config: Config,
    *,
    ws: Workspace | None = None,
    project_dir: Path | None = None,
    worktree_dir: Path | None = None,
) -> tuple[list[PluginManifest], list[str]]:
    """Resolve every plugin active in this context into ``(manifests, problems)``.

    Active means the folded ``[plugins]`` registry says true for the given
    context dirs (defaulting to the cwd-derived view of *config* when none are
    supplied). Each unresolvable or malformed plugin becomes one problem
    string; the rest come back sorted by name, ready for
    :meth:`Config.attach_plugins`.
    """
    if project_dir is None and worktree_dir is None:
        project_dir, worktree_dir = config.cwd_plugin_dirs()

    names = sorted(
        name
        for name, active in config.plugin_registry(
            ws=ws, project_dir=project_dir, worktree_dir=worktree_dir
        ).items()
        if active
    )

    manifests: list[PluginManifest] = []
    problems: list[str] = []
    for name in names:
        try:
            manifests.append(resolve_manifest(name))
        except ManifestError as exc:
            problems.append(str(exc))

    return manifests, problems


def attach_active_plugins(config: Config) -> Config:
    """Resolve and attach the active plugins onto *config*, warning per problem."""
    manifests, problems = load_active(config)
    for problem in problems:
        warn(f"plugin problem, continuing without it: {problem}")

    return config.attach_plugins(manifests)


def _find_entry_point(name: str) -> EntryPoint | None:
    """The ``wtm.plugins`` entry point named *name*, or ``None``."""
    try:
        eps = entry_points(group=ENTRY_POINT_GROUP)
    except Exception:
        return None

    for ep in eps:
        if ep.name == name:
            return ep

    return None


def _manifest_from_entry_point(name: str, ep: EntryPoint) -> PluginManifest:
    """Load an entry-point manifest: a mapping, or a zero-arg callable returning one."""
    try:
        obj = ep.load()
        raw = obj() if callable(obj) else obj
    except ManifestError:
        raise
    except Exception as exc:
        raise ManifestError(f"plugin '{name}': entry point failed to load: {exc}") from exc

    origin = getattr(ep, "value", None) or str(ep)

    return parse_manifest(raw, name=name, source="entry-point", origin=str(origin))


def _manifest_from_command(name: str, exe: Path) -> PluginManifest:
    """Fetch a manifest by running ``<exe> wtm-manifest --json``."""
    try:
        result = subprocess.run(
            [str(exe), MANIFEST_SUBCOMMAND, "--json"],
            capture_output=True,
            text=True,
            timeout=_MANIFEST_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise ManifestError(f"plugin '{name}': '{name} {MANIFEST_SUBCOMMAND}' timed out") from exc
    except OSError as exc:
        raise ManifestError(f"plugin '{name}': cannot run {exe}: {exc}") from exc

    if result.returncode != 0:
        stderr_lines = (result.stderr or "").strip().splitlines()
        detail = f": {stderr_lines[-1]}" if stderr_lines else ""
        raise ManifestError(
            f"plugin '{name}': '{name} {MANIFEST_SUBCOMMAND}' exited {result.returncode}{detail}"
        )

    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"plugin '{name}': manifest is not valid JSON: {exc}") from exc

    return parse_manifest(raw, name=name, source="command", origin=str(exe))
