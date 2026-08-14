"""Root typer app: bootstrap, command-group registration, and dual-homing.

Assembles the five command-group apps (:mod:`wtm.cli.worktree`, ...project,
...hook, ...workspace, ...config) onto the root ``app`` in panel order, and
dual-homes the worktree session/lifecycle/query verbs plus ``clone``/``import``
as top-level shortcuts. The group modules own their ``*_app`` definitions and
handlers; this module only wires them together so registration order lives in
one place.
"""

from __future__ import annotations

__all__ = ["app", "main"]

import sys
from dataclasses import dataclass

import typer
from typer.core import TyperGroup

from wtm import git
from wtm.cli.config import config_app
from wtm.cli.hook import hook_app
from wtm.cli.project import _clone, import_, project_app
from wtm.cli.workspace import workspace_app
from wtm.cli.worktree import (
    create,
    delete,
    enter,
    leave,
    list_,
    rename,
    resolve,
    set_,
    worktree_app,
)
from wtm.config import Config
from wtm.core import WtmError
from wtm.hooks import Hooks
from wtm.importer import Import
from wtm.project import Project
from wtm.workspace import Workspaces
from wtm.worktree import Worktree


@dataclass
class _Services:
    """Config-bound operation objects, built once per process."""

    config: Config
    project: Project
    worktree: Worktree
    workspaces: Workspaces
    importer: Import
    hooks: Hooks


_LAYOUT = """\
Manage git worktrees over one or more named workspaces (projects roots):

\b
  <workspace>/<project>/
      .git/                 bare repository
      worktrees/<name>/     worktrees

Workspaces are declared in config; projects are discovered on disk and only
annotated. The active workspace is picked by a leading WORKSPACE positional,
$WTM_WORKSPACE, the cwd, a default_workspace, or the sole declared one. Anything
project-specific (shared data/tools dirs, direnv, sesh/tmux) is the concern of
hooks, not wtm.

Worktree targeting for create/delete/rename/resolve/hook (a leading WORKSPACE
reaches another workspace; without it the active one is used):
  WORKSPACE PROJECT NAME -> <WORKSPACE>/PROJECT/worktrees/NAME
  PROJECT NAME -> <workspace>/PROJECT/worktrees/NAME
  NAME         -> <cwd-project>/worktrees/NAME (project inferred from cwd);
                  NAME may be nested (e.g. feature/x -> worktrees/feature/x)
  DIR          -> a single arg is a path only if it starts with '/', './',
                  '../' or is '.'/'..'; otherwise it is a NAME
  (no arg)     -> cwd (delete requires an explicit target)

enter/leave/list/project enter/project leave/hook list instead take a LOCATOR: a
name matched across every declared workspace, deepest tier first (worktree >
project > workspace), leading tokens qualifying its ancestors. One match wins,
several print the qualified choices. Destructive verbs and not-yet-created names
keep the positional grammar above.
"""


class _GroupsFirstTyperGroup(TyperGroup):
    """Root group that lists its sub-groups before its bare commands.

    Typer emits every bare command before any sub-group, which (with rich
    panels) forces the "Command groups" panel below the shortcut panels.
    Surfacing groups first here flips the rich panel order so Command groups
    heads the listing, while each subset keeps its own registration order.
    """

    def list_commands(self, ctx: typer.Context) -> list[str]:
        """Return sub-group names first, then bare command names."""
        names = super().list_commands(ctx)
        groups = [name for name in names if isinstance(self.get_command(ctx, name), TyperGroup)]
        group_set = set(groups)

        return groups + [name for name in names if name not in group_set]


app = typer.Typer(
    cls=_GroupsFirstTyperGroup,
    help=_LAYOUT,
    no_args_is_help=True,
    add_completion=True,
    pretty_exceptions_enable=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@app.callback()
def _bootstrap(ctx: typer.Context) -> None:
    """Load config once and bind the operation objects onto the context."""
    config = Config.load()
    ctx.obj = _Services(
        config=config,
        project=Project(config),
        worktree=Worktree(config),
        workspaces=Workspaces(config),
        importer=Import(config),
        hooks=Hooks(config),
    )


# Command groups, registered in panel order so the "Command groups" panel lists
# worktree first, then project, workspace, config, hook.
app.add_typer(worktree_app, name="worktree", rich_help_panel="Command groups")
app.add_typer(project_app, name="project", rich_help_panel="Command groups")
app.add_typer(workspace_app, name="workspace", rich_help_panel="Command groups")
app.add_typer(config_app, name="config", rich_help_panel="Command groups")
app.add_typer(hook_app, name="hook", rich_help_panel="Command groups")


# Worktree handlers, registered in one ordered block so the help ordering lives
# in one place. Order is common-first and grouped: the session pair, then the
# lifecycle mutations, then the read-only queries. Each is dual-homed:
# canonically 'wtm worktree <cmd>' plus a top-level 'wtm <cmd>' shortcut.
# (Install hooks are run via 'wtm hook run install'.)
_WORKTREE_COMMANDS = (
    ("enter", enter),
    ("leave", leave),
    ("create", create),
    ("delete", delete),
    ("rename", rename),
    ("list", list_),
    ("resolve", resolve),
)
for _name, _handler in _WORKTREE_COMMANDS:
    worktree_app.command(name=_name)(_handler)
    app.command(name=_name, rich_help_panel="Worktree shortcuts")(_handler)


# 'set' is worktree-namespaced only (no top-level shortcut), matching the
# subcommand-only 'project set' / 'workspace set': it is an annotation verb, not
# a session/lifecycle mover, so it reads better under 'wtm worktree'.
worktree_app.command(name="set")(set_)


# Top-level 'wtm clone' alias, registered after the worktree shortcuts so the
# "Worktree shortcuts" panel heads the listing and "Project shortcuts" follows.
app.command(name="clone", rich_help_panel="Project shortcuts")(_clone)


# 'import' is dual-homed: canonically 'wtm project import' (registered in
# wtm.cli.project) plus this top-level 'wtm import' alias. One handler, registered
# under both apps to avoid drift (as with 'clone').
app.command(name="import", rich_help_panel="Project shortcuts")(import_)


@app.command(name="shell-init")
def shell_init() -> None:
    """Print the shell wrapper so it can be sourced from a shell rc.

    Emits the packaged ``wtm/shell/wtm.sh`` to stdout, defining the ``wtm``
    function that lets movers like ``enter``/``create -c`` cd the calling shell.
    Source it directly (``eval "$(wtm shell-init)"``) or cache the output to a
    file and source that. Mirrors ``--show-completion``.
    """
    from importlib.resources import files

    print(files("wtm").joinpath("shell/wtm.sh").read_text(), end="")


def main() -> None:
    """Entry point: run the typer app, mapping WtmError to ``wtm: ...`` exit 1."""
    try:
        app()
    except WtmError as exc:
        print(f"wtm: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except git.GitError as exc:
        print(f"wtm: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
