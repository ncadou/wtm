"""wtm hook engine and hook management.

Events follow a ``pre-``/``post-`` naming convention: a prefix marks the side of
a state mutation (``pre-create``/``post-create``, ``pre-delete``/``post-delete``,
``pre-rename``/``post-rename``); a bare name is a mutation-free lifecycle moment
(``install``, ``enter``, ``leave``). A hook is either a **builtin** discovered
file or an **inline** ``sh -c`` command declared in config; inline hooks add to
the builtins. Builtin files are discovered across five scopes::

    system:    /etc/wtm/hooks/<event>                        and  .../<event>.d/*
    user:      $XDG_CONFIG_HOME/wtm/hooks/<event>            and  .../<event>.d/*
    workspace: <workspace>/.wtm/hooks/<event>                and  .../<event>.d/*
    project:   <project>/.wtm/hooks/<event>                  and  .../<event>.d/*
    worktree:  <worktree>/.wtm/hooks/<event>                 and  .../<event>.d/*

(system/user dirs are overridable via env or config; every scope's dir may be
overridden by its config ``[hooks] dir`` -- resolved through the single
:meth:`Config.hooks_dir <wtm.config.Config.hooks_dir>` authority, which both
firing and management consult.) Inline hooks come from the matching config
scope's ``[hooks.on]`` / entity ``hooks.on`` maps and accumulate across scopes.

A sixth, plugin tier rides alongside: every active plugin (a ``[plugins]``
registry cell naming a resolvable manifest -- see :mod:`wtm.plugins`)
contributes shell commands per event, fired first on setup events and last on
teardown (the plugin is machine-global like the system scope), each under the
same env contract, policy answer, and strict/non-fatal rules as an inline
hook.

Fire-time policy resolves through config too. Two tri-state booleans live in
each scope's ``[hooks]`` table and fold most-local-present-wins down the fired
scope ladder (one effective value per operation):

- ``[hooks] enabled = false`` -- fire nothing for the operation.
- ``[hooks] strict = true`` -- hook failures are fatal without ``--strict``.

The fourteenth event, ``post-config``, stands outside that lifecycle scheme:
it is a **broadcast** fired once per invocation, after any config file was
written (see :func:`run_config_hooks`).

The precedence tower is: ``WTM_NO_HOOKS`` (env, master off) > the CLI
``--strict`` flag (force-on only) > the config ladder > defaults
(enabled, non-strict). Both knobs are fire-time only -- ``hook install`` /
``delete`` / ``list`` always operate.

Setup events (pre-create, post-create, install, enter, pre-rename, post-rename)
run most-general-first: system, then user, then workspace, then project, then
worktree. Teardown events (leave, pre-delete, post-delete) run
most-specific-first: worktree, then project, then workspace, then user, then
system.
Within a scope, builtin files run first (a bare ``<event>`` file before its
``<event>.d/`` directory, entries sorted lexically), then inline commands in
declared order. Non-zero hook exits are logged but non-fatal unless the
operation is strict; under strict a present-but-non-executable file is also
fatal — so a failing ``pre-*`` hook aborts its operation only when strict.

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
    "CONFIG_EVENTS",
    "EVENTS",
    "HOOK_SENTINEL_VAR",
    "SETUP_EVENTS",
    "TEARDOWN_EVENTS",
    "Hooks",
    "Scope",
    "ScopeListing",
    "run_config_hooks",
    "run_hooks",
    "run_project_hooks",
    "run_workspace_hooks",
]

import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
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
from wtm.output import MANAGED_ENV_VARS, log, warn
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
    "project-enter",
    "project-leave",
    "workspace-enter",
    "workspace-leave",
    "post-config",
)
CONFIG_EVENTS = ("post-config",)
SETUP_EVENTS = (
    "pre-create",
    "post-create",
    "install",
    "enter",
    "pre-rename",
    "post-rename",
    "project-enter",
    "workspace-enter",
)
TEARDOWN_EVENTS = ("leave", "pre-delete", "post-delete", "project-leave", "workspace-leave")

# Exported to hook subprocesses so a hook that itself writes config (or runs
# wtm) never re-triggers the post-config event -- firing checks it on entry.
HOOK_SENTINEL_VAR = "WTM_IN_HOOK"


# Setup runs general->specific; teardown reverses to specific->general.
_SETUP_ORDER = (Scope.SYSTEM, Scope.USER, Scope.WORKSPACE, Scope.PROJECT, Scope.WORKTREE)
_TEARDOWN_ORDER = tuple(reversed(_SETUP_ORDER))
# The workspace lifecycle events have no project/worktree; only these apply.
_WORKSPACE_SCOPES = (Scope.SYSTEM, Scope.USER, Scope.WORKSPACE)
# The project lifecycle events have no live worktree; system..project apply.
_PROJECT_SCOPES = (Scope.SYSTEM, Scope.USER, Scope.WORKSPACE, Scope.PROJECT)


# --- firing engine -----------------------------------------------------------


def _ladder_policy(specs: list[HookSpec]) -> tuple[bool, bool]:
    """Fold (enabled, strict) most-local-present-wins across an operation's specs.

    *specs* are ordered most-local-first; the first present declaration of each
    knob wins and a less-local value fills a gap -- the same tri-state rule
    :func:`wtm.config.model.merge_hooks` applies, applied here across the fired
    scope ladder. Unset knobs fall back to their defaults: enabled, non-strict.
    """
    enabled: bool | None = None
    strict: bool | None = None
    for spec in specs:
        if enabled is None:
            enabled = spec.enabled
        if strict is None:
            strict = spec.strict
        if enabled is not None and strict is not None:
            break

    return enabled is not False, bool(strict)


def _fire(
    *,
    event: str,
    applicable: tuple[Scope, ...],
    config: Config,
    env: dict[str, str],
    hcwd: Path,
    arg1: Path,
    strict: bool,
    specs: dict[Scope, HookSpec],
    dir_of: Callable[[Scope], Path],
    plugin_cmds: Sequence[tuple[str, str]] = (),
) -> None:
    """The one fire loop behind every event tier.

    Resolves the operation's policy from the ladder (see :func:`_ladder_policy`),
    then fires each applicable scope in the event's natural order: builtin files
    (when the scope's spec enables them) under its discovery dir, then inline
    commands. ``WTM_NO_HOOKS`` (via ``config.no_hooks``) is the master off
    switch; ``[hooks] enabled = false`` anywhere on the ladder kills the whole
    operation; ``strict`` (the CLI force-on) or a ladder-resolved ``[hooks]
    strict = true`` makes failures fatal.

    :param applicable: the scopes this event tier may fire at all.
    :param specs: per-scope :class:`HookSpec`, resolved by the caller (which
        knows the target's dirs).
    :param dir_of: scope -> builtin discovery dir (delegates to
        :meth:`Config.hooks_dir <wtm.config.Config.hooks_dir>`).
    :param plugin_cmds: ``(plugin name, command)`` rows contributed by the
        active plugins -- a pseudo-tier alongside the five scopes, fired first
        on setup events and last on teardown (the plugin is machine-global),
        each command executed exactly like an inline hook under the same
        policy answer.
    """
    # The env master switch wins over everything, config included.
    if config.no_hooks:
        return

    # Mark hook subprocesses: a nested wtm invocation from inside a hook sees
    # this and suppresses its own post-config firing (no recursion).
    env.setdefault(HOOK_SENTINEL_VAR, "1")

    order = _TEARDOWN_ORDER if event in TEARDOWN_EVENTS else _SETUP_ORDER
    selected = tuple(s for s in order if s in applicable)

    # One policy answer per operation, folded most-local-first.
    enabled, config_strict = _ladder_policy([specs[s] for s in reversed(selected)])
    if not enabled:
        return

    fatal = strict or config_strict

    def _fire_plugins() -> None:
        for _name, command in plugin_cmds:
            _run_inline_hook(command, hcwd, arg1, env, strict=fatal)

    def _fire_scopes() -> None:
        for scope in selected:
            spec = specs[scope]
            if spec.run_builtins:
                base = dir_of(scope) / event
                _run_hook_source(base, hcwd, arg1, env, strict=fatal)
            for command in spec.on.get(event, []):
                _run_inline_hook(command, hcwd, arg1, env, strict=fatal)

    if event in TEARDOWN_EVENTS:
        _fire_scopes()
        _fire_plugins()
    else:
        _fire_plugins()
        _fire_scopes()


def run_config_hooks(config: Config, files: Sequence[Path], *, strict: bool = False) -> None:
    """Fire the ``post-config`` event for the config *files* an invocation wrote.

    A tier of its own rather than a lifecycle moment: there is no subject
    worktree or project, so the environment contract is minimal --
    ``WTM_EVENT`` + ``WTM_CONFIG_FILES`` (newline-separated absolute paths)
    plus the workspace pair when the changes imply one -- and both the hooks'
    ``$1`` and working directory are the deepest common ancestor of the
    changed files. Entity context for the scope ladder is *implied by the
    changed paths themselves* (a ``<dir>/.wtm/config.toml`` names its entity:
    a worktree when the dir sits under a project's ``worktrees/``, a project
    when it holds a bare ``.git``, else a declared workspace); scopes that
    cannot be resolved from that context are skipped silently.

    Firing is suppressed entirely when already running inside a hook
    subprocess (the :data:`HOOK_SENTINEL_VAR` environment sentinel), so a hook
    that writes config never recurses; the outer invocation's own firing (see
    :meth:`Config.flushed_config_files <wtm.config.Config.flushed_config_files>`)
    covers those writes instead.
    """
    # The env master switch, then nothing to announce, then the re-entry guard.
    if config.no_hooks or not files or os.environ.get(HOOK_SENTINEL_VAR):
        return

    anchor = _config_anchor(files)
    project_dir, worktree_dir = _implied_dirs(files)
    ws = config.workspace_of(worktree_dir or project_dir or anchor)

    env = dict(os.environ)
    for key in MANAGED_ENV_VARS:
        env.pop(key, None)
    env["WTM_EVENT"] = "post-config"
    # A file written several times in one invocation is announced once.
    env["WTM_CONFIG_FILES"] = "\n".join(str(f) for f in sorted(set(files)))
    if ws is not None:
        env["WTM_WORKSPACE"] = ws.name
        env["WTM_WORKSPACE_DIR"] = str(ws.path)

    selected = _SETUP_ORDER
    specs: dict[Scope, HookSpec] = {
        s: config.scope_hook_spec(s, project_dir or Path("/"), worktree_dir or Path("/"))
        for s in selected
    }
    dirs: dict[Scope, Path] = {}
    for scope in selected:
        try:
            dirs[scope] = config.hooks_dir(
                scope,
                ws=ws,
                project_dir=project_dir,
                worktree_dir=worktree_dir,
            )
        except WtmError:
            # No implied entity dir for this scope -- point its builtin
            # discovery at a path that cannot exist so nothing fires there.
            dirs[scope] = Path("/wtm-unresolvable-hook-dir")

    log("running post-config hooks")
    _fire(
        event="post-config",
        applicable=selected,
        config=config,
        env=env,
        hcwd=anchor,
        arg1=anchor,
        strict=strict,
        specs=specs,
        dir_of=dirs.__getitem__,
        plugin_cmds=config.plugin_commands(
            "post-config", ws=ws, project_dir=project_dir, worktree_dir=worktree_dir
        ),
    )


def _config_anchor(files: Sequence[Path]) -> Path:
    """The deepest common ancestor directory of *files*' parents (fallback cwd)."""
    parents = [str(Path(f).absolute().parent) for f in files]
    common = os.path.commonpath(parents)

    return Path(common)


def _implied_dirs(files: Sequence[Path]) -> tuple[Path | None, Path | None]:
    """Best-effort ``(project_dir, worktree_dir)`` implied by changed config paths.

    Each ``<dir>/.wtm/config.toml`` classifies its entity: a **worktree** when
    the dir sits directly under a project's ``worktrees/``, a **project** when
    the dir holds a bare ``.git``, anything else unclassified. The deepest
    classification wins, so one invocation touching several entities reports
    the most specific context to the scope ladder.
    """
    best: tuple[Path | None, Path | None] = (None, None)
    for raw in files:
        entity_dir = Path(raw).absolute().parent.parent
        if Path(raw).absolute().parent.name != ".wtm":
            continue
        parent = entity_dir.parent
        if parent.name == "worktrees" and (parent.parent / ".git").is_dir():
            best = (parent.parent, entity_dir)  # deeper than any project match
        elif best == (None, None) and (entity_dir / ".git").is_dir():
            best = (entity_dir, None)

    return best


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
    """Fire hooks for *event* using *context*, honouring ordering and policy.

    :param event: one of the :data:`EVENTS`.
    :param context: derived worktree context supplying the env contract.
    :param config: resolved configuration (hook dirs, specs, policy).
    :param strict: CLI ``--strict`` -- forces hook failures to be fatal on top
        of whatever the config ladder resolves.
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

    project_dir = context.project_dir
    worktree_dir = context.dir
    specs = {s: config.scope_hook_spec(s, project_dir, worktree_dir) for s in selected}

    _fire(
        event=event,
        applicable=selected,
        config=config,
        env=env,
        hcwd=hcwd,
        arg1=worktree_dir,
        strict=strict,
        specs=specs,
        dir_of=lambda scope: config.hooks_dir(
            scope, ws=ws, project_dir=project_dir, worktree_dir=worktree_dir
        ),
        plugin_cmds=config.plugin_commands(
            event, ws=ws, project_dir=project_dir, worktree_dir=worktree_dir
        ),
    )


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
    Builtin files are ``<event>`` / ``<event>.d/*`` under each scope's
    hooks dir; inline hooks come from the scope's ``[hooks.on]`` (system/user) or
    the workspace entry's ``hooks.on`` (workspace).
    """
    order = _TEARDOWN_ORDER if event in TEARDOWN_EVENTS else _SETUP_ORDER
    selected = tuple(s for s in order if s in _WORKSPACE_SCOPES)

    # Enforce the workspace-only contract: strip any inherited session vars (a
    # stale WTM_WORKTREE_DIR etc. from the parent wtm session would otherwise
    # leak into the hook), then set the three workspace-lifecycle vars.
    env = dict(os.environ)
    for key in MANAGED_ENV_VARS:
        env.pop(key, None)
    env["WTM_EVENT"] = event
    env["WTM_WORKSPACE"] = name
    env["WTM_WORKSPACE_DIR"] = str(path)

    ws = config.workspace_of(path)
    specs = {
        Scope.SYSTEM: config.system_scope_hooks,
        Scope.USER: config.user_scope_hooks,
        Scope.WORKSPACE: ws.hooks if ws is not None else HookSpec(),
    }

    _fire(
        event=event,
        applicable=selected,
        config=config,
        env=env,
        hcwd=path,
        arg1=path,
        strict=strict,
        specs=specs,
        dir_of=lambda scope: config.hooks_dir(scope, ws=ws),
        plugin_cmds=config.plugin_commands(event, ws=ws),
    )


def run_project_hooks(
    event: str,
    project_dir: Path,
    config: Config,
    *,
    strict: bool,
) -> None:
    """Fire a project-lifecycle event (``project-enter`` / ``project-leave``).

    The middle tier between :func:`run_workspace_hooks` and :func:`run_hooks`:
    there is no live worktree, so the system, user, workspace, and project
    scopes apply (never worktree). The environment contract is the project-level
    subset (``WTM_EVENT`` + the six project vars) with no ``WTM_WORKTREE_DIR`` /
    ``WTM_BRANCH``. *project_dir* is both the subprocess working directory and
    the hooks' ``$1``; the project-scope builtins live under
    ``<project>/.wtm/hooks`` and the workspace scope resolves against the
    workspace that contains the project (not necessarily the active one).
    """
    order = _TEARDOWN_ORDER if event in TEARDOWN_EVENTS else _SETUP_ORDER
    selected = tuple(s for s in order if s in _PROJECT_SCOPES)

    # Enforce the project-only contract: strip any inherited session vars (a
    # stale WTM_WORKTREE_DIR/WTM_BRANCH from a worktree session would otherwise
    # leak into the hook), then set WTM_EVENT plus the project-level contract.
    ws = config.workspace_of(project_dir)
    env = dict(os.environ)
    for key in MANAGED_ENV_VARS:
        env.pop(key, None)
    env["WTM_EVENT"] = event
    env["WTM_PROJECT"] = project_dir.name
    env["WTM_PROJECT_DIR"] = str(project_dir)
    env["WTM_WORKTREES_DIR"] = str(project_dir / "worktrees")
    env["WTM_GIT_COMMON_DIR"] = str(project_dir / ".git")
    env["WTM_WORKSPACE"] = ws.name if ws is not None else ""
    env["WTM_WORKSPACE_DIR"] = str(ws.path if ws is not None else project_dir.parent)

    worktrees_sentinel = project_dir / "worktrees"
    specs = {s: config.scope_hook_spec(s, project_dir, worktrees_sentinel) for s in selected}

    _fire(
        event=event,
        applicable=selected,
        config=config,
        env=env,
        hcwd=project_dir,
        arg1=project_dir,
        strict=strict,
        specs=specs,
        dir_of=lambda scope: config.hooks_dir(scope, ws=ws, project_dir=project_dir),
        plugin_cmds=config.plugin_commands(event, ws=ws, project_dir=project_dir),
    )


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
    file: Path | None  # the bare <event> file, if present
    entries: list[Path]  # sorted <event>.d/* entries
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

        The event is inferred from an ``<event>`` / ``<event>.d`` source name
        unless *event* overrides it. A directory's contents are merged into
        ``<event>.d/``. A file lands in ``<event>.d/`` when that directory
        already exists, else as the bare ``<event>`` file.
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
        base = hooks_dir / resolved_event
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

        *hook* is an event (removes the bare ``<event>`` file; its
        ``<event>.d/`` only with *recursive*) or ``event/entry`` (removes a
        single ``.d`` entry).
        """
        event, entry = _split_hook(hook)
        _validate_event(event)
        hooks_dir = self._scope_dir(scope, args)
        base = hooks_dir / event
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

        Includes both builtin ``<event>`` files and inline config hooks
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
                    base = hooks_dir / event
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

    def plugin_rows(self, args: list[str]) -> dict[str, list[tuple[str, str]]]:
        """Per-event ``(plugin name, command)`` rows contributed by active plugins.

        The listing-side view of the plugin pseudo-tier: best-effort like
        :meth:`list` -- when the target cannot be resolved, the context dirs
        are ``None`` and only the workspace/global tiers answer.
        """
        project_dir, worktree_dir = self._target_dirs(args)
        ws = self.config.workspace_of(project_dir) if project_dir is not None else None

        rows: dict[str, list[tuple[str, str]]] = {}
        for event in EVENTS:
            found = self.config.plugin_commands(
                event, ws=ws, project_dir=project_dir, worktree_dir=worktree_dir
            )
            if found:
                rows[event] = found

        return rows

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

        Delegates to the :meth:`Config.hooks_dir <wtm.config.Config.hooks_dir>`
        authority (so a ``[hooks] dir`` override is honored exactly as when
        firing). System, user, and workspace need no target -- workspace
        resolving against the *active* workspace; project resolves to
        ``<project>/.wtm/hooks`` without requiring a worktree to exist; worktree
        needs a live worktree.
        """
        if scope is Scope.SYSTEM or scope is Scope.USER:
            return self.config.hooks_dir(scope)
        if scope is Scope.WORKSPACE:
            return self.config.hooks_dir(scope, ws=self.config.active_workspace)
        if scope is Scope.PROJECT:
            # Hook target grammar is [[WORKSPACE] PROJECT] NAME: the trailing
            # NAME is irrelevant for a project-scope dir, so read the leading
            # WORKSPACE/PROJECT by count.
            if len(args) >= 3:
                workspace, project = args[0], args[1]
            elif len(args) == 2:
                workspace, project = None, args[0]
            else:
                workspace, project = None, None
            project_dir = resolve_project_dir(self.config, workspace, project)
            git_dir = project_dir / ".git"
            if not git_dir.is_dir():
                raise WtmError(f"project git dir not found: {git_dir}")

            return self.config.hooks_dir(scope, project_dir=project_dir)

        context = derive_context(resolve_target(self.config, TargetMode.WORKTREE, args))

        return self.config.hooks_dir(
            scope, project_dir=context.project_dir, worktree_dir=context.dir
        )


# --- management helpers ------------------------------------------------------


def _infer_event(src: Path) -> str | None:
    """Infer an event from an ``<event>`` or ``<event>.d`` source name."""
    name = src.name
    if name.endswith(".d"):
        name = name[: -len(".d")]
    if name in EVENTS:
        return name

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
