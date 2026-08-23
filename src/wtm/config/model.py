"""The config data model: entity/resolution dataclasses and schema vocabulary.

The typed read view of wtm config (:class:`Workspace`,
:class:`ProjectAnnotation`, the ``Resolved*`` tree, :class:`ConfigSlice`),
the hook spec, and the reserved/inheritable key vocabulary every engine
strips or folds by.
"""

from __future__ import annotations

__all__ = [
    "CONTAINER_KEYS",
    "ConfigSlice",
    "FLOOR_TIERS",
    "HookSpec",
    "INHERITABLE_DECL_KEY",
    "INHERITABLE_KEYS",
    "INHERIT_FLOORS",
    "NON_INHERITABLE_KEYS",
    "ProjectAnnotation",
    "RESERVED_KEYS",
    "ResolvedProject",
    "ResolvedWorkspace",
    "ResolvedWorktree",
    "SliceTarget",
    "SourceRung",
    "SUPER_TABLE_KEYS",
    "TYPED_INHERITABLE_KEYS",
    "Workspace",
]

from dataclasses import dataclass, field
from pathlib import Path

from wtm.config.addressing import Scope
from wtm.core import expand_path
from wtm.exceptions import KindConflictError, WtmError

RESERVED_KEYS = frozenset(
    {"hooks", "projects", "worktrees", "workspaces", "path", "default-workspace", "inheritable"}
)
# Keys whose tables are pure containers of named children.
CONTAINER_KEYS = frozenset({"hooks", "projects", "worktrees"})
# The policy cells inside the reserved ``[hooks]`` table that the arbitrary-key
# facility may address as dotted paths (``hooks.strict`` / ``hooks.enabled``).
# Every other cell stays owned by its dedicated surface (``hooks.on.*`` by
# ``hook set/unset``, ``builtins``/``dir`` by hand-editing).
HOOKS_POLICY_CELLS = frozenset({"strict", "enabled"})
# Keys whose tables are rendered as super-tables so a fresh nested write
# produces dotted headers, no empty parent.
SUPER_TABLE_KEYS = frozenset({"workspaces", "projects", "worktrees"})
# Builtin non-entity-specific keys: shared defaults whose read resolution
# inherits down the containment chain, rather than staying entity-local.
# The effective set is this builtin allowlist unioned with the user-declared
# `inheritable` array; a key's membership is always a curated decision --
# hardcoded here or opted in by the user -- never inferred from its name,
# type, or value.
INHERITABLE_KEYS = frozenset({"base-branch"})
# Builtin floors: the deepest entity tier an inheritable key participates in.
# Chains drop every rung below the floor and writes landing deeper are
# refused. ``base-branch`` is a project-tier concept (the fork point for a
# project's new branches), so a worktree-local value neither resolves nor
# writes -- the generalization of what used to be two hardcoded name-checks.
FLOOR_TIERS = {"worktree": 0, "project": 1, "workspace": 2, "global": 3}
INHERIT_FLOORS = {"base-branch": "project"}
# The inheritable keys that own a dedicated typed read surface (a provenance
# method), so they never also fold into an entity's ``extra`` payload -- that
# would report the same value on two surfaces -- and never take a user-declared
# floor (their resolution is fixed to keep it aligned with the typed surface).
TYPED_INHERITABLE_KEYS = frozenset({"base-branch"})
# Builtin entity-specific keys whose non-inheritance is hardcoded: a shared
# default they can never be. They must be refused as elements of a user's
# `inheritable` array (opting them in would contradict the hardcoded rule).
NON_INHERITABLE_KEYS = frozenset({"description"})
# The reserved key naming the user's own additional non-entity-specific keys. It
# is a TOML array of key names; it accumulates (unions) across scopes like
# `hooks.on`, and never appears in an entity's `extra` payload.
INHERITABLE_DECL_KEY = "inheritable"


# --- data model --------------------------------------------------------------


@dataclass(frozen=True)
class HookSpec:
    """A scope's hook configuration: inline commands plus builtin-file controls.

    :param on: event -> list of ``sh -c`` command strings (inline hooks).
    :param builtins: whether to also discover builtin ``wtm-<event>`` files;
        ``None`` means unset (treated as enabled), ``False`` disables them for
        this scope. Carried as tri-state so a more-specific declaration site can
        override a less-specific one.
    :param dir: an override for this scope's builtin hook-discovery directory.
    :param strict: fire-time policy -- hook failures in this scope are fatal
        (``None`` unset, ``True`` fatal). Resolved per operation by folding the
        fired scopes' specs most-local-first (see :func:`merge_hooks`).
    :param enabled: fire-time policy -- when ``False``, nothing fires for the
        whole operation. Resolved like *strict*: most-local present declaration
        wins down the fired ladder.
    """

    on: dict[str, list[str]] = field(default_factory=dict)
    builtins: bool | None = None
    dir: Path | None = None
    strict: bool | None = None
    enabled: bool | None = None

    @property
    def run_builtins(self) -> bool:
        """Whether builtin file hooks should fire (unset defaults to yes)."""
        return self.builtins is not False


@dataclass(frozen=True)
class ProjectAnnotation:
    """Config overlay for a discovered project (never a declaration).

    :param extra: arbitrary keys resolved by the locality merge,
        beyond the known scalar fields.
    """

    description: str | None = None
    base_branch: str | None = None
    hooks: HookSpec = field(default_factory=HookSpec)
    extra: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Workspace:
    """A declared workspace: a named projects root plus its config overlay.

    :param extra: arbitrary keys resolved by the locality merge,
        beyond the known scalar fields.
    """

    name: str
    path: Path
    description: str | None = None
    base_branch: str | None = None  # scope-wide default for its projects
    hooks: HookSpec = field(default_factory=HookSpec)
    scope: Scope = Scope.USER  # where it was ultimately declared (for --origin)
    extra: dict[str, object] = field(default_factory=dict)


# --- resolved view -----------------------------------------------------------


@dataclass(frozen=True)
class ResolvedWorktree:
    """The worktree tier of the resolved view (locality-merged over the ladder).

    :param description: the worktree's scalar annotation, if any.
    :param extra: arbitrary keys resolved by the locality merge.
    :param extra_origins: per-head :class:`SourceRung` chains for ``extra``,
        the material ``config show --origin`` labels each cell with (empty when
        origin rendering is off; never serialized to JSON).
    """

    name: str
    dir: Path
    description: str | None = None
    hooks: HookSpec = field(default_factory=HookSpec)
    extra: dict[str, object] = field(default_factory=dict)
    extra_origins: dict[str, list[SourceRung]] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedProject:
    """The cwd-bound project's effective annotation with base-branch provenance."""

    name: str
    dir: Path
    exists: bool
    description: str | None
    base_branch: str | None  # the project-tier value, if any
    effective_base_branch: str | None  # after the full resolution chain
    base_branch_source: str  # project | workspace | global | (none)
    hooks: HookSpec = field(default_factory=HookSpec)
    extra: dict[str, object] = field(default_factory=dict)
    extra_origins: dict[str, list[SourceRung]] = field(default_factory=dict)
    worktree: ResolvedWorktree | None = None  # set only when cwd is in a worktree
    worktrees: tuple[ResolvedWorktree, ...] = ()  # every worktree; only the tree dump fills it


@dataclass(frozen=True)
class ResolvedWorkspace:
    """A workspace with every project (and its worktrees) fully descended.

    The tree-dump analog of the shallow :class:`Workspace` entry: *entry* is the
    declared workspace itself and *projects* its fully-resolved projects (on-disk
    unioned with declared), each carrying its own ``worktrees`` tuple.
    *base_branch_source* is the scope value (``"workspace"``/``"user"``/
    ``"system"``) carrying the workspace's own ``base-branch``, or ``None`` --
    used by ``config show --origin`` (text-only, never JSON). *extra_origins*
    likewise feeds ``config show --origin`` for the workspace's own ``extra``.
    """

    entry: Workspace
    projects: tuple[ResolvedProject, ...] = ()
    base_branch_source: str | None = None
    extra_origins: dict[str, list[SourceRung]] = field(default_factory=dict)


@dataclass(frozen=True)
class SliceTarget:
    """The target entity a config slice roots at (``None`` = global).

    A lightweight, locator-independent representation of the resolved target:
    *tier* is ``"workspace"`` / ``"project"`` / ``"worktree"``; *workspace* is
    always set; *project* and *name* are set for the project and worktree tiers.
    The CLI layer converts a locator :class:`wtm.locator.Location` (or the
    cwd-inferred target) to this before calling :meth:`Config.resolve_slice`.
    """

    tier: str
    workspace: str
    project: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class ConfigSlice:
    """A pruned config tree: one branch from global to the target, optional subtree.

    Rooted at global, rendered to *depth_limit* (0=global, 1=workspace,
    2=project, 3=worktree). The branch from global to the target is always
    present; with ``-r`` the target's subtree expands. *workspaces* carries the
    pruned workspace nodes (empty when depth-limited below the workspace tier).
    *base_branch_source* is the scope value (``"user"``/``"system"``) declaring
    the global default base-branch, or ``None``. *global_extra_origins* feeds
    ``config show --origin`` for the global node's ``extra``.
    """

    default_base_branch: str | None
    base_branch_source: str | None
    global_extra: dict[str, object]
    global_hooks: HookSpec
    workspaces: tuple[ResolvedWorkspace, ...]
    depth_limit: int
    global_extra_origins: dict[str, list[SourceRung]] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceRung:
    """One resolved rung of a ``config get --origin`` chain.

    :param scope: the config scope (file tier) the value was read from.
    :param value: the value present at this rung.
    :param file: the config file the value lives in.
    :param tier: for an inheritable key, the definition level it came from
        (``project`` / ``workspace`` / ``global``); ``None`` for a plain
        (entity-local) key, where the scope already names the source.
    """

    scope: Scope
    value: object
    file: Path
    tier: str | None = None


# --- TOML parsing helpers ----------------------------------------------------


def parse_hooks(table: dict | None) -> HookSpec:
    """Parse a ``[hooks]``-shaped table into a :class:`HookSpec`.

    Unknown sub-keys are tolerated (forward compatibility); the known policy
    booleans (``strict`` / ``enabled``) must be TOML booleans -- any other kind
    is a hard error, matching the kind-clash rule everywhere else.
    """
    if not table:
        return HookSpec()

    on: dict[str, list[str]] = {}
    for event, value in (table.get("on") or {}).items():
        on[event] = [value] if isinstance(value, str) else list(value)
    dir_value = table.get("dir")

    return HookSpec(
        on=on,
        builtins=table.get("builtins"),
        dir=expand_path(dir_value) if dir_value else None,
        strict=_policy_bool(table.get("strict"), "strict"),
        enabled=_policy_bool(table.get("enabled"), "enabled"),
    )


def _policy_bool(value: object, name: str) -> bool | None:
    """Coerce a ``[hooks]`` policy cell to ``bool | None``, refusing other kinds."""
    if value is None or isinstance(value, bool):
        return value

    raise WtmError(f"[hooks] {name} must be a boolean, got {kind(value)}")


def annotation_from_payload(payload: dict[str, object], hooks: HookSpec) -> ProjectAnnotation:
    """Split a locality-merged payload into the known scalars plus ``extra``."""
    extra = {k: v for k, v in payload.items() if k not in ("description", "base-branch")}

    return ProjectAnnotation(
        description=str_or_none(payload.get("description")),
        base_branch=str_or_none(payload.get("base-branch")),
        hooks=hooks,
        extra=extra,
    )


def str_or_none(value: object) -> str | None:
    """Coerce a present scalar to ``str``; ``None`` stays ``None``."""
    return None if value is None else str(value)


def merge_hooks(a: HookSpec, b: HookSpec) -> HookSpec:
    """Merge two same-scope hook specs: accumulate commands, more-specific wins.

    ``on`` accumulates (builtins-first across scopes); every other cell is
    tri-state -- the more-specific *present* value wins, a less-local value
    fills a gap. This is also the fire-time ladder rule: folding an operation's
    specs most-local-first with :func:`merge_hooks` resolves one effective
    policy per knob.
    """
    on: dict[str, list[str]] = {event: list(cmds) for event, cmds in a.on.items()}
    for event, cmds in b.on.items():
        on.setdefault(event, []).extend(cmds)

    return HookSpec(
        on=on,
        builtins=b.builtins if b.builtins is not None else a.builtins,
        dir=b.dir or a.dir,
        strict=b.strict if b.strict is not None else a.strict,
        enabled=b.enabled if b.enabled is not None else a.enabled,
    )


# --- value classification ----------------------------------------------------


def check_kind(key: str, prev: object, value: object, file: Path) -> None:
    """Raise on a scalar/array/table kind clash for *key* across scopes.

    Recurses into matching sub-keys when both *prev* and *value* are tables, so
    a scalar-vs-table clash at a nested path (e.g. ``ci.runner`` a scalar in one
    scope and a table in another) is caught -- not just a top-level mismatch.
    """
    if prev is None:
        return

    if kind(prev) != kind(value):
        raise KindConflictError(
            f"config key '{key}' has conflicting kinds across scopes "
            f"({kind(prev)} vs {kind(value)} at {file})"
        )

    if isinstance(prev, dict) and isinstance(value, dict):
        for sub, sub_value in value.items():
            check_kind(f"{key}.{sub}", prev.get(sub), sub_value, file)


def kind(value: object) -> str:
    """Classify a config value as ``table`` / ``array`` / ``scalar``."""
    if isinstance(value, dict):
        return "table"

    if isinstance(value, (list, tuple)):
        return "array"

    return "scalar"
