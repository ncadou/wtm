"""wtm hook engine and hook management.

Hooks (events: create, install, enter, leave, delete) are discovered across
four scopes::

    global:    $WTM_HOOKS_DIR/wtm-<event>                       and  .../wtm-<event>.d/*
    workspace: $WTM_WORKSPACE_HOOKS_DIR/wtm-<event>             and  .../wtm-<event>.d/*
    project:   $WTM_PROJECT_DIR/.git/hooks/wtm-<event>          and  .../wtm-<event>.d/*
    worktree:  $WTM_DIR/.wtm/hooks/wtm-<event>                  and  .../wtm-<event>.d/*

Setup events (create, install, enter) run most-general-first: global, then
workspace, then project, then worktree. Teardown events (leave, delete) run
most-specific-first: worktree, then project, then workspace, then global.
Within a scope a bare ``wtm-<event>`` file runs before its ``wtm-<event>.d/``
directory (entries sorted lexically). Non-zero hook exits are logged but
non-fatal unless strict is set; under strict a present-but-non-executable hook
is also fatal.

The delete event fires *before* the worktree is removed, so per-worktree
teardown hooks still exist when they run; its cwd is therefore ``WTM_DIR``. The
leave event likewise fires while the worktree still exists.

Hook environment contract (all events)::

    WTM_EVENT WTM_DIR(=$1) WTM_BRANCH WTM_PROJECT WTM_PROJECT_DIR
    WTM_WORKTREES_DIR WTM_GIT_COMMON_DIR WTM_PROJECTS_ROOT

Beyond firing hooks, this module manages them: :class:`Hooks` installs, runs,
lists, and deletes hook files across the scopes, staying CLI-agnostic (it
returns data; :mod:`wtm.cli` renders it).
"""

from __future__ import annotations

__all__ = [
    "EVENTS",
    "SETUP_EVENTS",
    "TEARDOWN_EVENTS",
    "Hooks",
    "Scope",
    "ScopeListing",
    "run_hooks",
]

import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from wtm import git
from wtm.core import (
    Config,
    Context,
    WtmError,
    abspath,
    derive_context,
    resolve_project_dir,
)
from wtm.output import log, warn
from wtm.target import TargetMode, resolve_target

EVENTS = ("create", "install", "enter", "leave", "delete")
SETUP_EVENTS = ("create", "install", "enter")
TEARDOWN_EVENTS = ("leave", "delete")


class Scope(str, Enum):
    """A hook discovery/installation scope, from most general to most specific."""

    GLOBAL = "global"
    WORKSPACE = "workspace"
    PROJECT = "project"
    WORKTREE = "worktree"


# Setup runs general->specific; teardown reverses to specific->general.
_SETUP_ORDER = (Scope.GLOBAL, Scope.WORKSPACE, Scope.PROJECT, Scope.WORKTREE)
_TEARDOWN_ORDER = tuple(reversed(_SETUP_ORDER))


# --- firing engine -----------------------------------------------------------


def run_hooks(
    event: str,
    context: Context,
    config: Config,
    *,
    strict: bool,
    scopes: tuple[Scope, ...] | None = None,
) -> None:
    """Fire hooks for *event* using *context*, honouring ordering and strict.

    :param event: one of the :data:`EVENTS`.
    :param context: derived worktree context supplying the env contract.
    :param config: resolved configuration (for the global/workspace hooks dirs).
    :param strict: when true, hook failures and non-executable hooks are fatal.
    :param scopes: restrict firing to these scopes (default: all four, in the
        event's natural order).
    """
    order = _TEARDOWN_ORDER if event in TEARDOWN_EVENTS else _SETUP_ORDER
    selected = order if scopes is None else tuple(s for s in order if s in scopes)

    # teardown events fire before removal, so the worktree still exists: run there.
    hcwd = context.dir

    env = dict(os.environ)
    env["WTM_EVENT"] = event
    env.update(context.env())
    env["WTM_PROJECTS_ROOT"] = str(config.projects_root)

    for scope in selected:
        base = _scope_base(scope, context, config, event)
        _run_hook_source(base, hcwd, context, env, strict=strict)


def _scope_base(scope: Scope, context: Context, config: Config, event: str) -> Path:
    """Return the ``wtm-<event>`` base path for *scope* (its ``.d`` is base + '.d')."""
    return _scope_hooks_dir(scope, context, config) / f"wtm-{event}"


def _scope_hooks_dir(scope: Scope, context: Context | None, config: Config) -> Path:
    """Return the hooks directory for *scope*.

    Only the global and workspace scopes work without a *context*; the project
    and worktree scopes derive their directories from the worktree's git layout.
    """
    if scope is Scope.GLOBAL:
        return config.hooks_dir
    if scope is Scope.WORKSPACE:
        return config.workspace_hooks_dir
    if context is None:
        raise WtmError(f"scope '{scope.value}' needs a worktree/project target")
    if scope is Scope.PROJECT:
        return context.git_common_dir / "hooks"

    return context.dir / ".wtm" / "hooks"


def _run_hook_source(
    base: Path, hcwd: Path, context: Context, env: dict[str, str], *, strict: bool
) -> None:
    """Run the file at *base* (if any), then ``base.d/*`` sorted lexically."""
    _run_one_hook(base, hcwd, context, env, strict=strict)

    directory = Path(f"{base}.d")
    if directory.is_dir():
        for entry in sorted(directory.iterdir(), key=lambda p: p.name):
            _run_one_hook(entry, hcwd, context, env, strict=strict)


def _run_one_hook(
    hook: Path, hcwd: Path, context: Context, env: dict[str, str], *, strict: bool
) -> None:
    """Execute a single hook, applying strict-mode and non-fatal-failure rules."""
    if not hook.exists():
        return

    if not os.access(hook, os.X_OK):
        if strict:
            raise WtmError(f"hook not executable: {hook}")
        warn(f"hook not executable, skipping: {hook}")

        return

    log(f"running hook: {hook}")
    result = subprocess.run([str(hook), str(context.dir)], cwd=str(hcwd), env=env)
    if result.returncode == 0:
        return

    if strict:
        raise WtmError(f"hook failed (exit {result.returncode}): {hook}")
    warn(f"hook failed (exit {result.returncode}), continuing: {hook}")


# --- management --------------------------------------------------------------


@dataclass(frozen=True)
class ScopeListing:
    """The hooks found in one scope for one event: the bare file and ``.d`` entries."""

    scope: Scope
    file: Path | None  # the bare wtm-<event> file, if present
    entries: list[Path]  # sorted wtm-<event>.d/* entries


class Hooks:
    """Hook management (install/run/delete/list) bound to a resolved :class:`Config`."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def install(
        self,
        source: str,
        args: list[str],
        *,
        scope: Scope,
        event: str | None,
        link: bool,
        force: bool,
    ) -> Path:
        """Install *source* (a file or directory) into *scope*; return the destination.

        The event is inferred from a ``wtm-<event>`` / ``wtm-<event>.d`` source
        name unless *event* overrides it. A directory's contents are merged into
        ``wtm-<event>.d/``. A file lands in ``wtm-<event>.d/`` when that
        directory already exists, else as the bare ``wtm-<event>`` file.
        """
        src = abspath(source)
        if not src.exists():
            raise WtmError(f"source does not exist: {src}")

        resolved_event = event or _infer_event(src)
        if resolved_event is None:
            raise WtmError(
                f"cannot infer event from '{src.name}'; pass --event " + "|".join(EVENTS)
            )
        _validate_event(resolved_event)

        hooks_dir = self._scope_dir(scope, args)
        base = hooks_dir / f"wtm-{resolved_event}"
        dotd = Path(f"{base}.d")

        if src.is_dir():
            return _install_dir(src, dotd, link=link, force=force)

        # A file joins an existing .d/, else becomes the bare hook file.
        if dotd.is_dir():
            dest = dotd / src.name
        else:
            dest = base

        return _place(src, dest, link=link, force=force)

    def run(
        self,
        event: str,
        args: list[str],
        *,
        scope: Scope | None,
        strict: bool,
    ) -> None:
        """Fire *event* hooks for a worktree; *scope* ``None`` runs all scopes."""
        _validate_event(event)
        context = derive_context(resolve_target(self.config, TargetMode.WORKTREE, args))
        scopes = None if scope is None else (scope,)
        run_hooks(event, context, self.config, strict=strict, scopes=scopes)

    def delete(
        self,
        hook: str,
        args: list[str],
        *,
        scope: Scope,
        recursive: bool,
    ) -> list[Path]:
        """Delete a hook from *scope*; return the paths removed.

        *hook* is an event (removes the bare ``wtm-<event>`` file; its
        ``wtm-<event>.d/`` only with *recursive*) or ``event/entry`` (removes a
        single ``.d`` entry).
        """
        event, entry = _split_hook(hook)
        _validate_event(event)
        hooks_dir = self._scope_dir(scope, args)
        base = hooks_dir / f"wtm-{event}"
        dotd = Path(f"{base}.d")

        if entry is not None:
            target = dotd / entry
            if not target.exists() and not target.is_symlink():
                raise WtmError(f"no such hook entry: {target}")

            return [_remove(target)]

        removed: list[Path] = []
        if base.exists() or base.is_symlink():
            removed.append(_remove(base))
        if dotd.is_dir():
            if not recursive:
                raise WtmError(f"{dotd} is a directory; pass -r/--recursive to remove it")
            shutil.rmtree(dotd)
            removed.append(dotd)
        if not removed:
            raise WtmError(f"no hooks for event '{event}' in scope '{scope.value}'")

        return removed

    def list(self, args: list[str]) -> dict[str, list[ScopeListing]]:
        """Return installed hooks grouped by event, then scope.

        Scopes that cannot be resolved (e.g. no worktree context) are silently
        omitted so listing works from anywhere.
        """
        result: dict[str, list[ScopeListing]] = {}
        for event in EVENTS:
            listings: list[ScopeListing] = []
            for scope in _SETUP_ORDER:
                try:
                    hooks_dir = self._scope_dir(scope, args)
                except (WtmError, git.GitError):
                    continue
                base = hooks_dir / f"wtm-{event}"
                dotd = Path(f"{base}.d")
                file = base if (base.exists() or base.is_symlink()) else None
                entries = sorted(dotd.iterdir(), key=lambda p: p.name) if dotd.is_dir() else []
                if file is not None or entries:
                    listings.append(ScopeListing(scope=scope, file=file, entries=entries))
            result[event] = listings

        return result

    def _scope_dir(self, scope: Scope, args: list[str]) -> Path:
        """Resolve the hooks directory for a write operation in *scope*.

        Global and workspace need no target; project resolves to
        ``<project>/.git/hooks`` without requiring a worktree to exist; worktree
        needs a live worktree.
        """
        if scope is Scope.GLOBAL:
            return self.config.hooks_dir
        if scope is Scope.WORKSPACE:
            return self.config.workspace_hooks_dir
        if scope is Scope.PROJECT:
            project = args[0] if len(args) == 2 else None
            project_dir = resolve_project_dir(self.config, project)
            git_dir = project_dir / ".git"
            if not git_dir.is_dir():
                raise WtmError(f"project git dir not found: {git_dir}")

            return git_dir / "hooks"

        context = derive_context(resolve_target(self.config, TargetMode.WORKTREE, args))

        return _scope_hooks_dir(scope, context, self.config)


# --- management helpers ------------------------------------------------------


def _infer_event(src: Path) -> str | None:
    """Infer an event from a ``wtm-<event>`` or ``wtm-<event>.d`` source name."""
    name = src.name
    if name.endswith(".d"):
        name = name[: -len(".d")]
    if name.startswith("wtm-"):
        candidate = name[len("wtm-") :]
        if candidate in EVENTS:
            return candidate

    return None


def _validate_event(event: str) -> None:
    """Reject an unknown event name."""
    if event not in EVENTS:
        raise WtmError(f"unknown event '{event}' (expected {' | '.join(EVENTS)})")


def _split_hook(hook: str) -> tuple[str, str | None]:
    """Split a ``HOOK`` argument into (event, entry-or-None)."""
    event, sep, entry = hook.partition("/")
    if not sep:
        return event, None
    if not entry or "/" in entry:
        raise WtmError(f"invalid hook entry: '{hook}'")

    return event, entry


def _install_dir(src: Path, dotd: Path, *, link: bool, force: bool) -> Path:
    """Merge *src*'s contents into the ``.d`` directory *dotd*; return *dotd*."""
    dotd.mkdir(parents=True, exist_ok=True)
    for child in sorted(src.iterdir(), key=lambda p: p.name):
        _place(child, dotd / child.name, link=link, force=force)

    return dotd


def _place(src: Path, dest: Path, *, link: bool, force: bool) -> Path:
    """Copy or symlink *src* to *dest*; return *dest*.

    Copies are made executable (0755); symlinks point at *src*'s absolute path
    and take their mode from the target. Refuses to clobber unless *force*.
    """
    if (dest.exists() or dest.is_symlink()) and not force:
        raise WtmError(f"destination exists (use --force): {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        _remove(dest)

    if link:
        dest.symlink_to(src)
    else:
        shutil.copyfile(src, dest)
        dest.chmod(0o755)

    return dest


def _remove(path: Path) -> Path:
    """Remove a file, symlink, or directory tree at *path*; return it."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()

    return path
