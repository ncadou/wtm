"""Cross-layer shared helpers used by both completions and command groups.

The shared substrate that straddles the completion/command divide: the generic
write-scope resolver and its help text (hook + config), the worktree-name
projection (completion + project tree view), and the resolved-rung label
(completion + ``config get``). Kept import-free of group modules and
completions so both layers may import it without forming a cycle.
"""

from __future__ import annotations

from wtm.config import Config, GenericTarget, SourceRung, resolve_project_dir
from wtm.core import WtmError, worktree_name
from wtm.hooks import Scope
from wtm.worktree import Worktree

_GENERIC_SCOPE_HELP = (
    "Where to write (worktree|project|workspace|user|system); default: deepest existing dir"
)


def _generic_scope(target: GenericTarget, scope: Scope | None) -> Scope:
    """Resolve/validate the write scope for a generic key against *target*."""
    scope = scope if scope is not None else target.default_scope
    if scope not in target.allowed_scopes:
        allowed = ", ".join(s.value for s in target.allowed_scopes)
        raise WtmError(
            f"cannot write a key for {target.label} at '{scope.value}' scope "
            f"(choose one of {allowed})"
        )

    return scope


def _worktree_names(
    config: Config, workspace: str | None, project: str | None, *, strict: bool = False
) -> list[str]:
    """Return a project's in-tree worktree names, sorted, for completion/tree.

    The presentation-side projection of the single source of truth
    (:meth:`Worktree.list`): map each entry to its ``worktrees/``-relative name
    via :func:`worktree_name` and drop the out-of-tree ones (``None``). Lenient
    by default so shell completion never errors; *strict* re-raises (letting the
    tree view mark a project whose git query failed).
    """
    try:
        worktrees_dir = resolve_project_dir(config, workspace, project) / "worktrees"
    except Exception:
        if strict:
            raise

        return []

    names = [
        name
        for entry in Worktree(config).list(workspace, project, strict=strict)
        if (name := worktree_name(entry.path, worktrees_dir)) is not None
    ]

    return sorted(names)


def _rung_label(rung: SourceRung) -> str:
    """Label a resolved rung by its definition level: tier for an inheritable
    key, otherwise the scope (file tier) it was read from."""
    return rung.tier or rung.scope.value
