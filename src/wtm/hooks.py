"""wtm hook engine and hook management.

Events follow a ``pre-``/``post-`` naming convention: a prefix marks the side of
a state mutation (``pre-create``/``post-create``, ``pre-delete``/``post-delete``,
``pre-rename``/``post-rename``); a bare name is a mutation-free lifecycle moment
(``install``, ``enter``, ``leave``). A hook is either a **builtin** discovered
file or an **inline** ``sh -c`` command declared in config; inline hooks add to
the builtins. Builtin files are discovered across five scopes::

    system:    /etc/wtm/hooks/wtm-<event>                    and  .../wtm-<event>.d/*
    user:      $XDG_CONFIG_HOME/wtm/hooks/wtm-<event>        and  .../wtm-<event>.d/*
    workspace: <workspace>/.wtm/hooks/wtm-<event>            and  .../wtm-<event>.d/*
    project:   <project>/.git/hooks/wtm-<event>              and  .../wtm-<event>.d/*
    worktree:  <worktree>/.wtm/hooks/wtm-<event>             and  .../wtm-<event>.d/*

(system/user dirs are overridable via env or config; each scope's dir may be
overridden by its config ``[hooks] dir``, and ``builtins = false`` disables that
scope's file discovery.) Inline hooks come from the matching config scope's
``[hooks.on]`` / entity ``hooks.on`` maps and accumulate across scopes.

Setup events (pre-create, post-create, install, enter, pre-rename, post-rename)
run most-general-first: system, then user, then workspace, then project, then
worktree. Teardown events (leave, pre-delete, post-delete) run
most-specific-first: worktree, then project, then workspace, then user, then
system.
Within a scope, builtin files run first (a bare ``wtm-<event>`` file before its
``wtm-<event>.d/`` directory, entries sorted lexically), then inline commands in
declared order. Non-zero hook exits are logged but non-fatal unless strict is
set; under strict a present-but-non-executable file is also fatal — so a failing
``pre-*`` hook aborts its operation only under strict. ``WTM_NO_HOOKS`` disables
all firing.

The pre-delete event fires *before* the worktree is removed, so per-worktree
teardown hooks still exist when they run; its cwd is ``WTM_WORKTREE_DIR``. The
post-delete event fires *after* removal (worktree gone, empty parents pruned,
branch handled), so its ``WTM_WORKTREE_DIR`` no longer exists and its cwd is the
project dir. The leave event fires while the worktree still exists. The
pre-create event fires *before* the worktree is created, so its
``WTM_WORKTREE_DIR`` does not exist yet and its cwd is the project dir. The
pre-rename event fires *before* the move (from the old worktree), the
post-rename event *after* it (from the new worktree, whose per-worktree hooks
moved with it). Worktree scope is inapplicable to pre-create and post-delete
(there is no live worktree to hold ``.wtm/hooks``).

Hook environment contract (all events)::

    WTM_EVENT WTM_WORKTREE_DIR(=$1) WTM_BRANCH WTM_PROJECT WTM_PROJECT_DIR
    WTM_WORKTREES_DIR WTM_GIT_COMMON_DIR WTM_WORKSPACE WTM_WORKSPACE_DIR

The pre-rename event adds ``WTM_NEW_DIR``/``WTM_NEW_BRANCH`` (the destination);
the post-rename event adds ``WTM_OLD_DIR``/``WTM_OLD_BRANCH`` (the prior state;
equal to the new values when unchanged).

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
    "run_workspace_hooks",
]

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from wtm import git
from wtm.config import Config, HookSpec, Scope, resolve_project_dir
from wtm.core import (
    Context,
    WtmError,
    abspath,
    derive_context,
)
from wtm.output import log, warn
from wtm.target import TargetMode, resolve_target

EVENTS = (
    "pre-create",
    "post-create",
    "install",
    "enter",
    "leave",
    "pre-delete",
    "post-delete",
    "pre-rename",
    "post-rename",
    "workspace-enter",
    "workspace-leave",
)
SETUP_EVENTS = (
    "pre-create",
    "post-create",
    "install",
    "enter",
    "pre-rename",
    "post-rename",
    "workspace-enter",
)
TEARDOWN_EVENTS = ("leave", "pre-delete", "post-delete", "workspace-leave")


# Setup runs general->specific; teardown reverses to specific->general.
_SETUP_ORDER = (Scope.SYSTEM, Scope.USER, Scope.WORKSPACE, Scope.PROJECT, Scope.WORKTREE)
_TEARDOWN_ORDER = tuple(reversed(_SETUP_ORDER))
# The workspace lifecycle events have no project/worktree; only these apply.
_WORKSPACE_SCOPES = (Scope.SYSTEM, Scope.USER, Scope.WORKSPACE)


# --- firing engine -----------------------------------------------------------


def _env_flag(name: str) -> bool:
    """Return whether an env var is set to a truthy value."""
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no")


def run_hooks(
    event: str,
    context: Context,
    config: Config,
    *,
    strict: bool,
    scopes: tuple[Scope, ...] | None = None,
    extra_env: dict[str, str] | None = None,
    hcwd: Path | None = None,
) -> None:
    """Fire hooks for *event* using *context*, honouring ordering and strict.

    :param event: one of the :data:`EVENTS`.
    :param context: derived worktree context supplying the env contract.
    :param config: resolved configuration (for the system/user/workspace hooks dirs).
    :param strict: when true, hook failures and non-executable hooks are fatal.
    :param scopes: restrict firing to these scopes (default: all five, in the
        event's natural order).
    :param extra_env: extra environment variables layered on top of the standard
        contract (e.g. the ``pre-rename`` event's ``WTM_NEW_DIR``/``WTM_NEW_BRANCH``).
    :param hcwd: the subprocess working directory (default: ``context.dir``).
        Events whose worktree dir is not a live directory — ``pre-create`` (not
        created yet) and ``post-delete`` (already removed) — pass the project dir.
    """
    order = _TEARDOWN_ORDER if event in TEARDOWN_EVENTS else _SETUP_ORDER
    selected = order if scopes is None else tuple(s for s in order if s in scopes)

    # WTM_NO_HOOKS is a global escape hatch: fire nothing (builtin or inline).
    if _env_flag("WTM_NO_HOOKS"):
        return

    # Default to the worktree dir; callers whose worktree dir is absent override.
    if hcwd is None:
        hcwd = context.dir

    # Enrich the context with its active workspace so the WTM_WORKSPACE /
    # WTM_WORKSPACE_DIR contract is exported. The workspace name is empty when
    # the project is not under any declared workspace; the dir is still the
    # projects root (the project dir's parent) in that case.
    ws = config.workspace_of(context.project_dir)
    context.with_workspace(
        ws.name if ws is not None else "",
        ws.path if ws is not None else context.project_dir.parent,
    )

    env = dict(os.environ)
    env["WTM_EVENT"] = event
    env.update(context.env())
    if extra_env:
        env.update(extra_env)

    for scope in selected:
        spec = config.scope_hook_spec(scope, context.project_dir, context.dir)
        if spec.run_builtins:
            base = _firing_dir(scope, context, config, spec) / f"wtm-{event}"
            _run_hook_source(base, hcwd, context.dir, env, strict=strict)
        for command in spec.on.get(event, []):
            _run_inline_hook(command, hcwd, context.dir, env, strict=strict)


def run_workspace_hooks(
    event: str,
    name: str,
    path: Path,
    config: Config,
    *,
    strict: bool,
) -> None:
    """Fire a workspace-lifecycle event (``workspace-enter`` / ``workspace-leave``).

    Unlike :func:`run_hooks`, this has no worktree: only the system, user, and
    workspace scopes apply, and the environment contract is workspace-only
    (``WTM_EVENT`` + ``WTM_WORKSPACE`` + ``WTM_WORKSPACE_DIR``). The projects
    root *path* is both the subprocess working directory and the hooks' ``$1``.
    Builtin files are ``wtm-<event>`` / ``wtm-<event>.d/*`` under each scope's
    hooks dir; inline hooks come from the scope's ``[hooks.on]`` (system/user) or
    the workspace entry's ``hooks.on`` (workspace).
    """
    if _env_flag("WTM_NO_HOOKS"):
        return

    order = _TEARDOWN_ORDER if event in TEARDOWN_EVENTS else _SETUP_ORDER
    selected = tuple(s for s in order if s in _WORKSPACE_SCOPES)

    env = dict(os.environ)
    env["WTM_EVENT"] = event
    env["WTM_WORKSPACE"] = name
    env["WTM_WORKSPACE_DIR"] = str(path)

    for scope in selected:
        spec = _workspace_scope_spec(scope, config, path)
        if spec.run_builtins:
            base = _workspace_firing_dir(scope, config, spec, path) / f"wtm-{event}"
            _run_hook_source(base, path, path, env, strict=strict)
        for command in spec.on.get(event, []):
            _run_inline_hook(command, path, path, env, strict=strict)


def _workspace_scope_spec(scope: Scope, config: Config, path: Path) -> HookSpec:
    """The inline :class:`HookSpec` for a workspace-lifecycle fire at *scope*."""
    if scope is Scope.SYSTEM:
        return config.system_scope_hooks
    if scope is Scope.USER:
        return config.user_scope_hooks
    ws = config.workspace_of(path)

    return ws.hooks if ws is not None else HookSpec()


def _workspace_firing_dir(scope: Scope, config: Config, spec: HookSpec, path: Path) -> Path:
    """The builtin discovery dir for a workspace-lifecycle fire at *scope*."""
    if scope is Scope.SYSTEM:
        return config.system_hooks_dir
    if scope is Scope.USER:
        return config.user_hooks_dir

    return spec.dir or path / ".wtm" / "hooks"


def _firing_dir(scope: Scope, context: Context, config: Config, spec: HookSpec) -> Path:
    """The builtin ``wtm-<event>`` discovery directory for *scope* when firing.

    Honours a scope's ``hooks.dir`` override, then falls back to the scope
    default. The workspace scope resolves against the workspace that *contains*
    this context's project (not necessarily the active one), so hooks fire
    correctly even across workspaces.
    """
    if scope is Scope.SYSTEM:
        return config.system_hooks_dir
    if scope is Scope.USER:
        return config.user_hooks_dir
    if scope is Scope.WORKSPACE:
        if spec.dir is not None:
            return spec.dir
        ws = config.workspace_of(context.project_dir)

        return (
            ws.path / ".wtm" / "hooks"
            if ws is not None
            else (context.project_dir.parent / ".wtm" / "hooks")
        )
    if scope is Scope.PROJECT:
        return spec.dir or context.git_common_dir / "hooks"

    return spec.dir or context.dir / ".wtm" / "hooks"


def _scope_base(scope: Scope, context: Context, config: Config, event: str) -> Path:
    """Return the ``wtm-<event>`` base path for *scope* (its ``.d`` is base + '.d')."""
    return _scope_hooks_dir(scope, context, config) / f"wtm-{event}"


def _scope_hooks_dir(scope: Scope, context: Context | None, config: Config) -> Path:
    """Return the hooks directory for *scope*.

    Only the system, user, and workspace scopes work without a *context*; the
    project and worktree scopes derive their directories from the worktree's git
    layout.
    """
    if scope is Scope.SYSTEM:
        return config.system_hooks_dir
    if scope is Scope.USER:
        return config.user_hooks_dir
    if scope is Scope.WORKSPACE:
        return config.workspace_hooks_dir
    if context is None:
        raise WtmError(f"scope '{scope.value}' needs a worktree/project target")
    if scope is Scope.PROJECT:
        return context.git_common_dir / "hooks"

    return context.dir / ".wtm" / "hooks"


def _run_hook_source(
    base: Path, hcwd: Path, arg1: Path, env: dict[str, str], *, strict: bool
) -> None:
    """Run the file at *base* (if any), then ``base.d/*`` sorted lexically."""
    _run_one_hook(base, hcwd, arg1, env, strict=strict)

    directory = Path(f"{base}.d")
    if directory.is_dir():
        for entry in sorted(directory.iterdir(), key=lambda p: p.name):
            _run_one_hook(entry, hcwd, arg1, env, strict=strict)


def _run_one_hook(
    hook: Path, hcwd: Path, arg1: Path, env: dict[str, str], *, strict: bool
) -> None:
    """Execute a single hook, applying strict-mode and non-fatal-failure rules.

    *arg1* is passed as the hook's ``$1`` (the worktree dir for worktree events,
    the projects root for workspace-lifecycle events).
    """
    if not hook.exists():
        return

    if not os.access(hook, os.X_OK):
        if strict:
            raise WtmError(f"hook not executable: {hook}")
        warn(f"hook not executable, skipping: {hook}")

        return

    log(f"running hook: {hook}")
    result = subprocess.run([str(hook), str(arg1)], cwd=str(hcwd), env=env)
    if result.returncode == 0:
        return

    if strict:
        raise WtmError(f"hook failed (exit {result.returncode}): {hook}")
    warn(f"hook failed (exit {result.returncode}), continuing: {hook}")


def _run_inline_hook(
    command: str, hcwd: Path, arg1: Path, env: dict[str, str], *, strict: bool
) -> None:
    """Execute an inline config hook via ``sh -c``, mirroring file-hook rules.

    *arg1* is passed both as ``$1`` (matching file hooks) and, for worktree
    events, mirrors the ``WTM_WORKTREE_DIR`` already in *env*. Strict/non-fatal
    handling matches :func:`_run_one_hook`.
    """
    log(f"running hook (config): {command}")
    result = subprocess.run(
        ["/bin/sh", "-c", command, "wtm-hook", str(arg1)],
        cwd=str(hcwd),
        env=env,
    )
    if result.returncode == 0:
        return

    if strict:
        raise WtmError(f"hook failed (exit {result.returncode}): {command}")
    warn(f"hook failed (exit {result.returncode}), continuing: {command}")


# --- management --------------------------------------------------------------


@dataclass(frozen=True)
class ScopeListing:
    """The hooks found in one scope for one event: files plus inline config hooks."""

    scope: Scope
    file: Path | None  # the bare wtm-<event> file, if present
    entries: list[Path]  # sorted wtm-<event>.d/* entries
    inline: list[str] = field(default_factory=list)  # inline config commands


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

        Includes both builtin ``wtm-<event>`` files and inline config hooks
        (the latter marked in the CLI rendering). Scopes that cannot be resolved
        (e.g. no worktree context) are silently omitted so listing works from
        anywhere.
        """
        # Best-effort context for the inline (config) hooks: the workspace,
        # project, and worktree scopes need a target to resolve; when there is
        # none, only their builtin files show and inline hooks are skipped.
        project_dir, worktree_dir = self._target_dirs(args)

        result: dict[str, list[ScopeListing]] = {}
        for event in EVENTS:
            listings: list[ScopeListing] = []
            for scope in _SETUP_ORDER:
                try:
                    hooks_dir = self._scope_dir(scope, args)
                except (WtmError, git.GitError):
                    hooks_dir = None
                file: Path | None = None
                entries: list[Path] = []
                if hooks_dir is not None:
                    base = hooks_dir / f"wtm-{event}"
                    dotd = Path(f"{base}.d")
                    file = base if (base.exists() or base.is_symlink()) else None
                    entries = sorted(dotd.iterdir(), key=lambda p: p.name) if dotd.is_dir() else []
                inline = self._inline_for(scope, event, project_dir, worktree_dir)
                if file is not None or entries or inline:
                    listings.append(
                        ScopeListing(scope=scope, file=file, entries=entries, inline=inline)
                    )
            result[event] = listings

        return result

    def _target_dirs(self, args: list[str]) -> tuple[Path | None, Path | None]:
        """Best-effort (project_dir, worktree_dir) for inline-hook resolution."""
        try:
            context = derive_context(resolve_target(self.config, TargetMode.WORKTREE, args))

            return context.project_dir, context.dir
        except (WtmError, git.GitError):
            return None, None

    def _inline_for(
        self, scope: Scope, event: str, project_dir: Path | None, worktree_dir: Path | None
    ) -> list[str]:
        """Inline config commands for *scope*/*event* (empty when unresolvable)."""
        if scope in (Scope.WORKSPACE, Scope.PROJECT, Scope.WORKTREE) and project_dir is None:
            return []
        spec = self.config.scope_hook_spec(
            scope, project_dir or Path("/"), worktree_dir or Path("/")
        )

        return list(spec.on.get(event, []))

    def _scope_dir(self, scope: Scope, args: list[str]) -> Path:
        """Resolve the hooks directory for a write operation in *scope*.

        System, user, and workspace need no target; project resolves to
        ``<project>/.git/hooks`` without requiring a worktree to exist; worktree
        needs a live worktree.
        """
        if scope is Scope.SYSTEM:
            return self.config.system_hooks_dir
        if scope is Scope.USER:
            return self.config.user_hooks_dir
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
