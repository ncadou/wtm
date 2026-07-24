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

Config resolves by a single, uniform rule: **increased locality always
outranks lesser locality**. Every scope is folded by a locality-ordered,
recursive merge (`CLI flag > env > worktree > project > workspace > user >
system > built-in default`); the most-local *present* value of a key wins
(`false`/`0`/`""` are real values — clearing a key *removes* it rather than
writing an empty string). Inline hooks (`hooks.on`) are the one exception: they
*accumulate* across scopes. Workspace **paths** are `~`/`$VAR`-expanded and must
be absolute after expansion.

An entity stores its *own* config at the **top level** of its own file; a
containing file nests a child under `<child-plural>.<name>`. So the same key —
say a project's `base_branch` — is writable (and correctly resolved) at every
scope above the project:

| where written | table in that file |
|---|---|
| project-local | top level |
| workspace-local | `[projects.NAME]` |
| user / system | `[workspaces.W.projects.NAME]` |

Worktrees add one more rung (`[worktrees.T]` project-local,
`[projects.P.worktrees.T]` workspace-local, and so on).

Most config is hand-edited; management commands write it for you (a
comment-preserving `tomlkit` round-trip with an atomic replace). All reads and
writes go through one authority — `wtm.config` — the only module that touches a
`.wtm/config.toml` file. It offers a generic value facility (read/write any
scalar at any named scope), a typed read view, and named structural registry
mutations (`add`/`set`/`rename`/`delete` workspace, `default_workspace`, global
`base_branch`) that write the **user file** and own their cross-key fixups (a
rename follows a dangling `default_workspace`).

## Write placement (`-s/--scope`)

Annotation writes name their target scope explicitly with `-s/--scope`
(mirroring `hook install`); the auto-default is the **deepest scope whose
directory exists**. The allowed scopes narrow by entity:

| command | writes | allowed `--scope` |
|---|---|---|
| `wtm workspace set -d/-b` | workspace `description` / `base_branch` | workspace, user, system |
| `wtm project set -d/-b` | project `description` / `base_branch` | project, workspace, user, system |
| `wtm worktree set -d` | worktree `description` | worktree, project, workspace, user, system |
| `wtm config base-branch [VALUE] [--clear]` | global `base_branch` (user file, top-level) | — |
| `wtm workspace default [NAME] [--clear]` | `default_workspace` (user file, top-level) | — |
| `wtm workspace add` / `set --path` | workspace `path` (user file registry entry) | — user-file only |

Workspace **declarations** (`add`/`set --path`/`rename`/`default`) stay
user-file-only. `WTM_NO_CONFIG` refuses all writes. Because a workspace-local
value *wins* on merge, clearing it there (`set -b ""`) lets a lesser-locality
value (e.g. the user-file registry annotation) resurface.

## Arbitrary keys (`config set/get/unset`)

Any non-reserved key cascades too. `wtm config set/get/unset KEY [VALUE]` acts
on the **cwd-inferred entity** (the deepest one the cwd sits inside — worktree,
else project, else the active workspace); `-s/--scope` picks the file tier the
same way annotations do.

- `wtm config set KEY VALUE [-s SCOPE]` — write an arbitrary key.
- `wtm config get KEY [--sources]` — resolve it (most-local wins); `--sources`
  lists the full chain, most-local first, each rung showing where the value comes
  from (its scope) and the file it lives in.
- `wtm config unset KEY [-s SCOPE]` — remove it from one tier.

An **inheritable** key resolves up the containment chain rather than staying
entity-local. Today only `base_branch` inherits: `config get base_branch` folds
the entity's own cascade plus its ancestors' (project → workspace → global
default), so it reports the same effective value `create` would use. For an
inheritable key `--sources` labels each rung by its definition level
(`project` / `workspace` / `global`) — matching `config show` — rather than its
raw scope, and still names the file.

Reserved names (`hooks`, `projects`, `worktrees`, `path`, `default_workspace`)
have their own commands and are refused as arbitrary keys.

## Inline hooks (`hook set/unset`)

`wtm hook set EVENT "CMD" [-s SCOPE]` / `wtm hook unset EVENT [-s SCOPE]` write
an inline hook (`[...hooks.on] EVENT = "CMD"`) at the cwd-inferred entity's
table-path, reusing the same scope machinery. A `set` replaces the command in
that one file; commands still accumulate across scopes at read time.

## Showing the effective config

`wtm config show [--sources] [--hooks]` prints the effective merge for the
current context: it lists all workspaces, then descends only in the active one
as far as the cwd sits — the cwd-bound project's resolved annotation and, when
the cwd is inside a worktree, that worktree's tier. Arbitrary (non-reserved)
keys resolved along the descent are always shown. `--sources` tags each value
with its **winning** scope (the full chain lives in `config get KEY --sources`);
`--hooks` also renders accumulated inline hooks per scope.

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
- `WTM_SYSTEM_CONFIG` — system config file path (default `/etc/wtm/config.toml`; point it at an absent path to skip system scope)
- `WTM_NO_CONFIG` — ignore all config files for reads, and refuse all config writes (env + CLI + defaults only)
- `WTM_SYSTEM_HOOKS_DIR` / `WTM_USER_HOOKS_DIR` — builtin hooks dir for that scope
- `WTM_NO_HOOKS` — disable all hook firing
- `XDG_CONFIG_HOME` — user config + user hooks location
- `WTM_SHELL_FILE` — the shell-wrapper directive channel (see
  [shell-integration.md](shell-integration.md))
