# Configuration

`wtm` reads TOML config from up to five scopes, mirroring the
[hook scopes](hooks.md#scopes-and-discovery), most-general to most-specific:

| scope | file |
|---|---|
| system | `$WTM_SYSTEM_CONFIG` or `/etc/wtm/config.toml` |
| user | `$WTM_CONFIG` or `$XDG_CONFIG_HOME/wtm/config.toml` |
| workspace | `<workspace>/.wtm/config.toml` |
| project | `<project>/.wtm/config.toml` |
| worktree | `<worktree>/.wtm/config.toml` |

Scalars resolve most-specific-first (`CLI flag > env > worktree > project >
workspace > user > system > built-in default`); inline hooks *accumulate* across
scopes. Workspace **paths** are `~`/`$VAR`-expanded and must be absolute after
expansion.

Most config is hand-edited; a few defaults have management commands that write
config for you (a comment-preserving `tomlkit` round-trip with an atomic
replace). Two seams do the writing: `wtm.registry` owns every write to the
**user file** (the `[workspaces.*]` registry entries, top-level
`default_workspace` and `base_branch`), and `wtm.config_writer` owns the
**entity-local** `.wtm/config.toml` files (workspace and project scope). No
command writes system or worktree config.

| default | command | writes |
|---|---|---|
| global `base_branch` | `wtm config base-branch [VALUE] [--clear]` | user file, top-level |
| `default_workspace` | `wtm workspace default [NAME] [--clear]` | user file, top-level |
| workspace `path` | `wtm workspace add` / `set --path` | user file registry entry |
| workspace `description` / `base_branch` | `wtm workspace set -d/-b` | `<ws>/.wtm/config.toml` if the dir exists, else the registry entry |
| project `description` / `base_branch` | `wtm project set -d/-b` | `<project>/.wtm/config.toml` if the dir exists, else the user file annotation |

Scalar writes target the **most-local home that already exists**: `workspace
set`/`project set` write the entity's own `.wtm/config.toml` when its directory
is present, otherwise they fall back to the user-file registry entry/annotation.
One consequence: `wtm workspace add -b main` records `base_branch` in the
registry entry, but a later `wtm workspace set -b develop` (once the dir exists)
writes the workspace-local file, which *wins* on merge — and clearing it there
(`set -b ""`) lets the registry value resurface. `wtm config show [--sources]`
prints the effective merge and where each value comes from.

The **workspace registry** (name → path) lives at system/user scope; a
workspace's own metadata and its projects' annotations may also live in its
`<workspace>/.wtm/config.toml`. Projects are discovered on disk and only
annotated (by name) with a description, a `base_branch`, and hooks.

## Example

```toml
# ~/.config/wtm/config.toml
default_workspace = "work"          # active fallback when cwd matches none
base_branch = "main"                # global default fork point for new branches

[workspaces.work]
path = "~/work"
description = "day job"
[workspaces.work.hooks.on]          # workspace-scope inline hooks (event -> cmd(s))
post-create = "direnv allow"
enter = ["tmux new-window", "echo entered $WTM_PROJECT"]
[workspaces.work.projects.api]      # annotate a discovered project
base_branch = "develop"
[workspaces.work.projects.api.hooks.on]
install = "make deps"

[workspaces.oss]
path = "$HOME/oss"

[hooks]                             # this file's own (user) scope
builtins = false                    # disable user-scope builtin file discovery
[hooks.on]
leave = "echo bye"
```

## Environment variables

Inbound env vars are a curated, high-precedence override set (they beat file
scopes); per-project scalars are deliberately *not* env-settable.

- `WTM_WORKSPACE` — select the active workspace by name
- `WTM_CONFIG` — user config file path (replaces the `$XDG_CONFIG_HOME` default)
- `WTM_SYSTEM_CONFIG` — system config file path (default `/etc/wtm/config.toml`)
- `WTM_NO_SYSTEM` — ignore system-scope config + hooks
- `WTM_NO_CONFIG` — ignore all config files (env + CLI + defaults only)
- `WTM_SYSTEM_HOOKS_DIR` / `WTM_USER_HOOKS_DIR` — builtin hooks dir for that scope
- `WTM_NO_HOOKS` — disable all hook firing
- `XDG_CONFIG_HOME` — user config + user hooks location
- `WTM_SHELL_FILE` — the shell-wrapper directive channel (see
  [shell-integration.md](shell-integration.md))
