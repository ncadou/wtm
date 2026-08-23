"""Project-level commands (new, clone, list, resolve, enter, leave, set, show, import)."""

from __future__ import annotations

import sys
from dataclasses import replace

import typer

from wtm import git
from wtm.cli.common import _worktree_names
from wtm.cli.completions.locator import _complete_locate_project
from wtm.cli.completions.target import (
    _complete_project,
    _complete_workspace,
    _complete_workspace_arg,
)
from wtm.cli.locator import _locate_project
from wtm.config import Config, resolve_project_dir
from wtm.core import WtmError, worktree_name
from wtm.hooks import Scope
from wtm.importer import CreateMethod, ImportPlan, ImportResult
from wtm.output import (
    emit_env_line,
    emit_shell,
    log,
    tree_children_lines,
    warn,
    worktree_flat_all_json,
    worktree_flat_all_lines,
    worktree_flat_json,
    worktree_flat_lines,
)
from wtm.project import Project
from wtm.target import split_workspace_project
from wtm.worktree import Worktree

project_app = typer.Typer(
    help="Project-level operations (enter, leave, new, clone, list, resolve, set, show).",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@project_app.command(name="new")
def project_new(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        ...,
        metavar="[WORKSPACE] NAME",
        help="New project name, optionally prefixed by its WORKSPACE",
        autocompletion=_complete_workspace_arg,
    ),
) -> None:
    """Create an empty project (.git/ + worktrees/)."""
    workspace, name = split_workspace_project(args)
    if name is None:
        raise WtmError("usage: wtm project new [WORKSPACE] NAME")
    project_dir = ctx.obj.project.new(workspace, name)
    log(f"created project '{project_dir.name}' at {project_dir} (.git/ + worktrees/)")


def _clone(
    ctx: typer.Context,
    url: str = typer.Argument(..., help="Repository URL"),
    args: list[str] = typer.Argument(
        None,
        metavar="[[WORKSPACE] NAME]",
        help="Project name (default: from URL), optionally prefixed by its WORKSPACE",
        autocompletion=_complete_workspace_arg,
    ),
) -> None:
    """Clone into a new project (bare)."""
    workspace, name = split_workspace_project(args or [])
    project_dir = ctx.obj.project.clone(url, workspace, name)
    log(f"cloned '{project_dir.name}' into {project_dir}/.git")


# 'clone' is dual-homed: the canonical 'wtm project clone' plus a top-level
# 'wtm clone' shorthand. One handler, registered under both apps to avoid drift.
# The top-level alias is registered in app.py after the worktree shortcuts so
# the "Worktree shortcuts" panel is encountered first in the help listing.
project_app.command(name="clone")(_clone)


@project_app.command(name="list")
def project_list(
    ctx: typer.Context,
    workspace: str | None = typer.Argument(
        None,
        metavar="[WORKSPACE]",
        help="Workspace to list (default: the active one)",
        autocompletion=_complete_workspace,
    ),
    tree: bool = typer.Option(
        False, "-t", "--tree", help="Show each project's worktrees beneath it"
    ),
    worktrees: bool = typer.Option(
        False,
        "-w",
        "--worktrees",
        help="Flatten to one worktree per line across all projects (tab-separated)",
    ),
    as_json: bool = typer.Option(False, "-j", "--json", help="Emit --worktrees rows as JSON"),
    all_ws: bool = typer.Option(
        False, "-a", "--all", help="Span every declared workspace (adds a workspace column)"
    ),
) -> None:
    """List a workspace's projects (WORKSPACE, or the active one; ``--all`` spans every workspace).

    Default: one project name per line. ``--tree`` nests each project's
    worktrees beneath it. ``--worktrees`` instead flattens to one worktree per
    line across every project as tab-separated ``project name path branch
    head`` (branch empty when detached; head is a short sha), with ``--json``
    for the same records as a JSON array. ``--all`` iterates every declared
    workspace, grouping the plain/tree output under a per-workspace header and
    prepending a ``workspace`` column to the flat/JSON rows. The listing carries
    only worktree facts — session naming and path rules are a consumer's concern.
    """
    if tree and worktrees:
        raise WtmError("--tree and --worktrees are mutually exclusive")
    if as_json and not worktrees:
        raise WtmError("--json requires --worktrees")
    if workspace is not None and all_ws:
        raise WtmError("a WORKSPACE and --all are mutually exclusive")

    config = ctx.obj.config
    if all_ws:
        names = sorted(config.workspaces)
    elif workspace is not None:
        if workspace not in config.workspaces:
            raise WtmError(f"unknown workspace: '{workspace}'")
        names = [workspace]
    else:
        names = [config.active_workspace.name]

    if worktrees:
        _list_worktrees(config, names, with_workspace=all_ws, as_json=as_json)

        return

    for ws_name in names:
        wcfg = replace(config, active=ws_name)
        if all_ws:
            print(f"{ws_name}:")
        for name in Project(wcfg).list():
            print(f"  {name}" if all_ws else name)
            if tree:
                _print_tree_children(wcfg, name, indent="  " if all_ws else "")


def _print_tree_children(config: Config, name: str, *, indent: str) -> None:
    """Print a project's worktree leaves (tree view), or an error marker."""
    try:
        children = _worktree_names(config, None, name, strict=True)
    except git.GitError:
        print(f"{indent}└── <error reading worktrees>")

        return
    for line in tree_children_lines(children):
        print(f"{indent}{line}")


def _list_worktrees(
    config: Config, names: list[str], *, with_workspace: bool, as_json: bool
) -> None:
    """Emit flat/JSON worktree rows for *names* (optionally workspace-prefixed)."""
    if with_workspace:
        rows6: list[tuple[str, str, str, str, str, str]] = []
        for ws_name in names:
            wcfg = replace(config, active=ws_name)
            for row in _flat_worktree_rows(Worktree(wcfg), wcfg, Project(wcfg).list()):
                rows6.append((ws_name, *row))
        if as_json:
            print(worktree_flat_all_json(rows6))
        else:
            for line in worktree_flat_all_lines(rows6):
                print(line)

        return

    wcfg = replace(config, active=names[0])
    rows = _flat_worktree_rows(Worktree(wcfg), wcfg, Project(wcfg).list())
    if as_json:
        print(worktree_flat_json(rows))
    else:
        for line in worktree_flat_lines(rows):
            print(line)


def _flat_worktree_rows(
    worktree: Worktree, config: Config, projects: list[str]
) -> list[tuple[str, str, str, str, str]]:
    """Collect ``(project, name, path, branch, head)`` rows for every worktree.

    The flat projection behind ``project list --worktrees``: each project's
    worktrees named relative to its ``worktrees/`` (an out-of-tree ``-d``
    destination keeps its absolute path), branch blank when detached, head a
    short sha. Rows are sorted by name within each project. A project whose git
    query fails is warned about on stderr and skipped, so a single broken repo
    never aborts the machine-readable listing.
    """
    rows: list[tuple[str, str, str, str, str]] = []
    for name in projects:
        worktrees_dir = resolve_project_dir(config, None, name) / "worktrees"
        try:
            entries = worktree.list(None, name, strict=True)
        except git.GitError:
            warn(f"skipping '{name}': could not read worktrees")
            continue
        project_rows = [
            (
                name,
                worktree_name(entry.path, worktrees_dir) or str(entry.path),
                str(entry.path),
                entry.branch or "",
                entry.head[:8],
            )
            for entry in entries
        ]
        rows.extend(sorted(project_rows, key=lambda row: row[1]))

    return rows


@project_app.command(name="resolve")
def project_resolve(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[[WORKSPACE] PROJECT]",
        help="Project (default: inferred from cwd), optionally prefixed by its WORKSPACE",
        autocompletion=_complete_project,
    ),
    emit_env: bool = typer.Option(False, "-e", "--env", help="Emit the project WTM_* contract"),
) -> None:
    """Print a project's directory (--env: emit project-level WTM_* exports for eval).

    A read-only path query: computes the path without touching git, so it works
    for a not-yet-created project. --env emits only the project-level contract
    (no WTM_WORKTREE_DIR / WTM_BRANCH), likewise by path math.
    """
    workspace, project = split_workspace_project(args or [])
    if emit_env:
        for key, value in ctx.obj.project.resolve_env(workspace, project).items():
            print(emit_env_line(key, value))
    else:
        print(ctx.obj.project.resolve_path(workspace, project))


@project_app.command(name="enter")
def project_enter(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[[WORKSPACE] PROJECT]",
        help="Locator: a project name resolved across every workspace "
        "(default: inferred from cwd)",
        autocompletion=_complete_locate_project,
    ),
    strict: bool = typer.Option(False, "--strict", help="Hook failures are fatal"),
) -> None:
    """cd into a project's directory, firing project-enter hooks.

    Lands beside the project's ``.git``/``worktrees`` and exports the
    project-level WTM_* contract (no WTM_WORKTREE_DIR/WTM_BRANCH), pinning the
    active workspace until leave. The PROJECT is a locator pinned to the project
    tier, so a bare name reaches another workspace's project; ``wtm enter`` finds
    the same target through the unpinned cascade.
    """
    workspace, project = _locate_project(ctx, args or [], command="project enter")
    result = ctx.obj.project.enter(workspace, project, strict=strict)
    emit_shell(cd=result.dir, env=ctx.obj.project.resolve_env(workspace, project))


@project_app.command(name="leave")
def project_leave(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[[WORKSPACE] PROJECT]",
        help="Locator: a project name resolved across every workspace "
        "(default: inferred from cwd)",
        autocompletion=_complete_locate_project,
    ),
    no_cd: bool = typer.Option(False, "-C", "--no-cd", help="Run hooks without cd-ing out"),
    strict: bool = typer.Option(False, "--strict", help="Hook failures are fatal"),
) -> None:
    """Fire project-leave hooks (cascading the worktree leave when inside one); cd out.

    Ends the project session: the WTM_* contract is unset even with -C (which
    only suppresses the cd-out). The PROJECT is a project-pinned locator, as for
    ``project enter``.
    """
    workspace, project = _locate_project(ctx, args or [], command="project leave")
    result = ctx.obj.project.leave(workspace, project, strict=strict)
    log(f"left project '{result.name}'")
    cd_dir = result.cd_dir if (not no_cd and result.cd_dir is not None) else None
    emit_shell(cd=cd_dir, env={}, fallback=False)


@project_app.command(name="set")
def project_set(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[[WORKSPACE] PROJECT]",
        help="Project (default: inferred from cwd), optionally prefixed by its WORKSPACE",
        autocompletion=_complete_project,
    ),
    base_branch: str | None = typer.Option(
        None, "-b", "--base-branch", help="Base branch (empty clears it)"
    ),
    description: str | None = typer.Option(
        None, "-d", "--description", help="Description (empty clears it)"
    ),
    scope: Scope | None = typer.Option(
        None,
        "-s",
        "--scope",
        help="Where to write (project|workspace|user|system); default: deepest existing dir",
    ),
) -> None:
    """Annotate a project's base-branch / description (partial update).

    Writes at ``--scope`` (project/workspace/user/system); the default is the
    deepest scope whose directory exists. A field passed as ``""`` clears it.
    """
    workspace, project = split_workspace_project(args or [])
    result = ctx.obj.project.set(
        workspace, project, base_branch=base_branch, description=description, scope=scope
    )
    log(f"updated project '{result.name}' [{result.scope.value}] ({result.address.file})")


@project_app.command(name="show")
def project_show(
    ctx: typer.Context,
    args: list[str] = typer.Argument(
        None,
        metavar="[[WORKSPACE] PROJECT]",
        help="Project (default: inferred from cwd), optionally prefixed by its WORKSPACE",
        autocompletion=_complete_project,
    ),
) -> None:
    """Show a project's detail (dir, description, base-branch + provenance, worktrees)."""
    workspace, project = split_workspace_project(args or [])
    result = ctx.obj.project.show(workspace, project)
    exists = "" if result.exists else "  (no .git on disk)"
    print(f"name: {result.name}")
    print(f"dir: {result.dir}{exists}")
    if result.description:
        print(f"description: {result.description}")
    if result.base_branch:
        print(f"base-branch: {result.base_branch}")
    effective = result.effective_base_branch or "(built-in: origin/HEAD > HEAD > orphan)"
    print(f"effective base-branch: {effective} [{result.base_branch_source}]")
    print(f"worktrees: {result.worktree_count}")


def import_(
    ctx: typer.Context,
    other: str | None = typer.Argument(
        None,
        metavar="[SOURCE|PROJECT]",
        help="The side not inferred from cwd: target PROJECT (from source) "
        "or SOURCE path (from the projects root)",
    ),
    source: str | None = typer.Option(
        None, "-s", "--source", help="Source repo path (default: inferred)"
    ),
    project: str | None = typer.Option(
        None, "-p", "--project", help="Target project name (default: inferred)"
    ),
    from_origin: bool = typer.Option(
        False, "-o", "--from-origin", help="Create target by cloning the origin URL (network)"
    ),
    init_bare: bool = typer.Option(
        False, "-i", "--init-bare", help="Create target as an empty bare repo (offline)"
    ),
    fetch: bool = typer.Option(
        False, "-f", "--fetch", help="Fetch origin afterwards (populate origin/* + HEAD)"
    ),
    force: bool = typer.Option(
        False, "--force", help="Force-push references into an existing target"
    ),
    update: bool = typer.Option(
        False,
        "-u",
        "--update",
        help="Allow importing into an existing target (required if .git is already present)",
    ),
    no_remotes: bool = typer.Option(False, "--no-remotes", help="Skip remote import"),
    no_refs: bool = typer.Option(
        False, "--no-refs", help="Skip pushing references to an existing target"
    ),
    no_worktree: bool = typer.Option(
        False, "--no-worktree", help="Skip recreating the working checkout"
    ),
    all_worktrees: bool = typer.Option(
        False,
        "-a",
        "--all-worktrees",
        help="Reproduce every linked worktree the source has (named after each "
        "source worktree's directory)",
    ),
    no_tags: bool = typer.Option(False, "--no-tags", help="Skip pushing tags"),
    no_ignored: bool = typer.Option(
        False, "--no-ignored", help="Drop git-ignored files from the worktree copy"
    ),
    exclude: list[str] = typer.Option([], "--exclude", help="rsync exclude pattern (repeatable)"),
    filter_file: list[str] = typer.Option(
        [], "--filter-file", help="rsync filter file to merge (repeatable)"
    ),
    name: str | None = typer.Option(
        None, "--name", help="Worktree name (required for a detached HEAD)"
    ),
    dry_run: bool = typer.Option(
        False, "-n", "--dry-run", help="Print the plan and an rsync preview; no changes"
    ),
    do_cd: bool = typer.Option(False, "-c", "--cd", help="cd into the new worktree"),
    strict: bool = typer.Option(False, "--strict", help="Hook failures are fatal"),
) -> None:
    """Fold a plain clone (source) into the bare-repo layout (target)."""
    if from_origin and init_bare:
        raise WtmError("--from-origin and --init-bare are mutually exclusive")
    if all_worktrees and name is not None:
        raise WtmError("--all-worktrees and --name are mutually exclusive")
    if all_worktrees and no_worktree:
        raise WtmError("--all-worktrees and --no-worktree are mutually exclusive")
    create_method = (
        CreateMethod.FROM_ORIGIN
        if from_origin
        else CreateMethod.INIT_BARE
        if init_bare
        else CreateMethod.FROM_DISK
    )

    importer = ctx.obj.importer
    resolution = importer.resolve(
        other, source=source, project=project, all_worktrees=all_worktrees, update=update
    )
    for skipped in resolution.prunable_skipped:
        warn(f"source worktree '{skipped}' is prunable/missing; skipped")
    plan = importer.plan(
        resolution,
        create_method=create_method,
        worktree_name=name,
        all_worktrees=all_worktrees,
        do_remotes=not no_remotes,
        do_refs=not no_refs,
        do_tags=not no_tags,
        do_fetch=fetch,
        force=force,
        include_ignored=not no_ignored,
        no_worktree=no_worktree,
    )

    if dry_run:
        _describe_plan(plan)
        for wt_name, preview in importer.dry_run_rsync(
            plan, exclude=exclude, filter_files=filter_file
        ):
            log(f"rsync preview (worktree '{wt_name}'):")
            print(preview, end="", file=sys.stderr)
        return

    result = importer.run(plan, exclude=exclude, filter_files=filter_file, strict=strict)
    _describe_result(result)
    if do_cd and result.cd_dir is not None:
        emit_shell(cd=result.cd_dir, env=ctx.obj.worktree.env_for_dir(result.cd_dir))
    if result.worktrees_failed:
        raise SystemExit(1)


# 'import' is dual-homed: canonically 'wtm project import' (it folds a repo
# into the project layout) plus a top-level 'wtm import' alias. One handler,
# registered under both apps to avoid drift (as with 'clone'). The Python
# keyword 'import' forbids it as a function name, so the handler is 'import_'.
project_app.command(name="import")(import_)


def _describe_plan(plan: ImportPlan) -> None:
    """Render an import plan to stderr (used by --dry-run)."""
    res = plan.resolution
    log(f"source:  {res.source}")
    log(f"target:  {res.project_dir} (.git bare)")
    if plan.create_method is None:
        log("target exists; references imported by push from source")
    else:
        log(f"create:  {plan.create_method.value}")
    pushes_refs = plan.do_refs and plan.create_method is not CreateMethod.FROM_DISK
    if plan.all_worktrees:
        worktree_label = f"worktrees={len(plan.worktrees)}"
    elif plan.worktrees:
        worktree_label = f"worktree={plan.worktrees[0].name}"
    else:
        worktree_label = ""
    phases = ", ".join(
        label
        for label, on in (
            ("remotes", plan.do_remotes),
            ("refs", pushes_refs),
            ("tags", pushes_refs and plan.do_tags),
            ("fetch", plan.do_fetch),
            (worktree_label, bool(worktree_label)),
        )
        if on
    )
    log(f"phases:  {phases or '(none)'}")
    if plan.all_worktrees:
        for spec in plan.worktrees:
            on = f"branch '{spec.branch}'" if spec.branch else f"detached {spec.head[:8]}"
            log(f"  worktree '{spec.name}' <- {on}")


def _describe_result(result: ImportResult) -> None:
    """Render what an import actually did to stderr."""
    res = result.plan.resolution
    if result.created:
        log(f"created target {res.project_dir}")
    for remote_name in result.remotes_added:
        log(f"added remote '{remote_name}'")
    for remote_name in result.remotes_skipped:
        warn(f"remote '{remote_name}' exists with a different URL; skipped")
    if result.refs_rejected:
        warn(
            "rejected (non-fast-forward; re-run with --force to overlay): "
            + ", ".join(result.refs_rejected)
        )
    if result.fetched:
        log("fetched origin (origin/* and origin/HEAD updated)")
    for wt_name, wt_dir in result.worktrees_created:
        log(f"worktree '{wt_name}' ready at {wt_dir}")
    for wt_name, reason in result.worktrees_skipped:
        warn(f"worktree '{wt_name}' skipped: {reason}")
    for wt_name, reason in result.worktrees_failed:
        warn(f"worktree '{wt_name}' failed: {reason}")
