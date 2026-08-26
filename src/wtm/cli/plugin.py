"""Plugin management commands (add, remove, list, show).

A plugin is registered by writing a ``[plugins] NAME = true`` cell into a
scope's config file top level; its manifest (hook commands, owned namespaces,
inheritable declarations, seed defaults) is then resolved fresh per invocation
from the plugin itself -- ``wtm.plugins`` entry point first, else
``NAME wtm-manifest --json`` on PATH. Registration copies nothing: the cell is
the whole contract, so removal is one unset and a plugin upgrade takes effect
on the next invocation.
"""

from __future__ import annotations

import json

import typer

from wtm import plugins as plugin_api
from wtm.cli.completions.plugin import _complete_discoverable, _complete_registered
from wtm.config.manifest import PluginManifest
from wtm.exceptions import ManifestError, WtmError
from wtm.hooks import Scope
from wtm.output import log, warn

plugin_app = typer.Typer(
    help="Manage plugins (register manifests, seed defaults) via activation cells.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_PLUGIN_SCOPE_HELP = "Where to write the activation cell (worktree|project|workspace|user|system)"


def _reattach(ctx: typer.Context) -> None:
    """Re-resolve the active plugins onto the bootstrap config (post-write).

    Attaching after the mutation means the post-config flush at invocation end
    already sees the new activation set -- a freshly added plugin's own
    ``post-config`` hook fires for its registration write.
    """
    ctx.obj.config = plugin_api.attach_active_plugins(ctx.obj.config)


@plugin_app.command(name="add")
def plugin_add(
    ctx: typer.Context,
    name: str = typer.Argument(
        ..., metavar="NAME", help="Plugin name", autocompletion=_complete_discoverable
    ),
    seed: bool = typer.Option(
        False, "--seed", help="Also write the manifest's declared default values (once)"
    ),
    scope: Scope = typer.Option(Scope.USER, "-s", "--scope", help=_PLUGIN_SCOPE_HELP),
) -> None:
    """Add NAME: write its activation cell; optionally --seed its defaults.

    The plugin is resolved eagerly so a missing install warns now rather than
    on every later invocation -- but registration proceeds either way, and an
    unresolvable plugin simply stays dormant (and flagged) until it appears.
    """
    try:
        manifest = plugin_api.resolve_manifest(name)
    except ManifestError as exc:
        manifest = None
        warn(f"registered '{name}' anyway; it stays dormant until resolvable ({exc})")

    address = ctx.obj.config.set_plugin_cell(name, True, scope)
    log(f"registered plugin '{name}' [{scope.value}] ({address.file})")
    if manifest is not None:
        _summarize_manifest(manifest)
    if seed:
        if manifest is None:
            raise WtmError(f"--seed needs '{name}' to resolve (it does not right now)")
        for dotted, value in ctx.obj.config.seed_plugin_defaults(manifest, scope):
            log(f"seeded {dotted} = {value} [{scope.value}]")

    _reattach(ctx)


@plugin_app.command(name="remove")
def plugin_remove(
    ctx: typer.Context,
    name: str = typer.Argument(
        ..., metavar="NAME", help="Plugin name", autocompletion=_complete_registered
    ),
) -> None:
    """Remove NAME: clear its activation cell from the most-local scope.

    Seeded values are ordinary user-owned config afterwards and stay put;
    pass them to ``config unset`` by hand if unwanted.
    """
    project_dir, worktree_dir = ctx.obj.config.cwd_plugin_dirs()
    ws = ctx.obj.config.workspace_of(project_dir) if project_dir else None
    site = ctx.obj.config.unset_plugin_cell_found(
        name, ws=ws, project_dir=project_dir, worktree_dir=worktree_dir
    )
    log(f"unregistered plugin '{name}' from [{site.value}]")

    _reattach(ctx)


@plugin_app.command(name="list")
def plugin_list(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "-j", "--json", help="Emit JSON rows"),
) -> None:
    """List every registered plugin with its resolution status."""
    config = ctx.obj.config
    project_dir, worktree_dir = config.cwd_plugin_dirs()
    ws = config.workspace_of(project_dir) if project_dir else None
    active_names = {
        m.name
        for m in config.active_plugins(ws=ws, project_dir=project_dir, worktree_dir=worktree_dir)
    }

    rows = []
    for name in config.registered_plugin_names():
        sites = config.registration_sites(name)
        row: dict = {"name": name, "active": name in active_names, "sites": {}}
        for site_scope, value in sites.items():
            row["sites"][site_scope.value] = value
        try:
            manifest = plugin_api.resolve_manifest(name)
            row["source"] = manifest.source
            row["origin"] = manifest.origin
            row["events"] = sorted(manifest.hooks)
            row["namespaces"] = list(manifest.namespaces)
        except ManifestError as exc:
            row["source"] = "broken"
            row["error"] = str(exc)

        rows.append(row)

    if as_json:
        print(json.dumps(rows))

        return

    if not rows:
        log("no plugins registered ([plugins] cells in any scope's config)")

        return

    for row in rows:
        flag = "*" if row["active"] else " "
        source = row.get("source", "?")
        detail = f" ({row['error']})" if row.get("error") else ""
        sites = ",".join(f"{s}={'true' if v else 'false'}" for s, v in row["sites"].items())
        events = ",".join(row.get("events", [])) or "-"
        print(f"{flag} {row['name']}  [{source}{detail}]  events: {events}  sites: {sites}")
    print("(*) active in this context")


@plugin_app.command(name="show")
def plugin_show(
    ctx: typer.Context,
    name: str = typer.Argument(
        ..., metavar="NAME", help="Plugin name", autocompletion=_complete_registered
    ),
) -> None:
    """Show NAME's manifest and registration sites."""
    config = ctx.obj.config
    sites = config.registration_sites(name)
    if not sites:
        raise WtmError(f"plugin '{name}' is not registered in any known scope")

    try:
        manifest = plugin_api.resolve_manifest(name)
    except ManifestError as exc:
        warn(str(exc))
    else:
        payload = manifest.to_json()
        payload["source"] = manifest.source
        payload["origin"] = manifest.origin
        print(json.dumps(payload, indent=2))

    rendered = ", ".join(
        f"{scope.value}={'true' if value else 'false'}" for scope, value in sites.items()
    )
    print(f"sites: {rendered}")


def _summarize_manifest(manifest: PluginManifest) -> None:
    """One line per contribution channel (hooks/namespaces/inheritable)."""
    if manifest.hooks:
        events = ",".join(sorted(manifest.hooks))
        log(f"'{manifest.name}' contributes hooks for: {events} [{manifest.source}]")
    if manifest.namespaces:
        log(f"'{manifest.name}' owns namespace(s): {', '.join(manifest.namespaces)}")
    if manifest.inheritable:
        keys = ", ".join(decl.key for decl in manifest.inheritable)
        log(f"'{manifest.name}' declares inheritable: {keys}")
