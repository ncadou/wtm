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
`.wtm/config.toml` file. It offers an arbitrary-key facility (read/write any
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

Any arbitrary key cascades too. `wtm config set/get/unset KEY [VALUE]` acts
on the **cwd-inferred entity** (the deepest one the cwd sits inside — worktree,
else project, else the active workspace); `-s/--scope` picks the file tier the
same way annotations do.

- `wtm config set KEY VALUE [-s SCOPE]` — write an arbitrary key. `KEY` may be a
  dotted path (`ci.runner.image`) naming a nested cell; see [Sections](#sections)
  below.
- `wtm config get KEY [SUBKEY] [--origin]` — resolve it (most-local wins). A
  scalar prints bare, an array one element per line, a table `subkey = value` per
  line (sorted, nested sub-tables indented); pass `SUBKEY` to read a single table
  cell, or use a dotted `KEY` to read a nested cell. `--origin` lists where each
  value is defined: per-cell (`path = value  [source]  file`) for a table, or one
  line per carrying rung for a scalar/array — its scope, or its definition level
  (`project`/`workspace`/`global`) for an inherited key — and the file it lives in.
- `wtm config unset KEY [-s SCOPE]` — remove it from one tier.

### Sections (dotted paths and deep-merge)

A `KEY` may be a **dotted path** (`ci.runner.image`) naming a cell inside a
section — a TOML table. `config set ci.runner.image ubuntu` writes
`[ci.runner] image = "ubuntu"`; `config get ci.runner.image` reads it back. The
`-t`/`--table` flag is the one-level shorthand for the same thing:
`config set ci.runner image ubuntu -t` (KEY=`ci.runner`, SUBKEY=`image`) is
identical to `config set ci.runner.image ubuntu`. A bare `KEY` keeps its
top-level meaning. Only the **head** segment of a dotted path is checked against
the reserved names (`hooks` / `projects` / `worktrees` / `workspaces` / `path` /
`default_workspace` / `inheritable`) — deeper segments are harmless cells, so
`ci.workspaces` is allowed (a cell named `workspaces` inside your `ci` section),
but `hooks.on.x` is refused.

A section **deep-merges** across tiers — per cell, most-local wins. If a project
sets `[ci] registry` and the workspace sets `[ci] runner`, `config get ci` yields
both; `config get ci.runner` resolves the workspace's. Nested tables merge
recursively (`ci.runner.image` from one tier + `ci.runner.tag` from another →
both). A scalar-vs-table clash at any sub-path (`ci.runner` a scalar in one
tier, `ci.runner.image` a table in another) is a hard error. This holds on both
axes: locality (one entity's scope files) and, for an inheritable section,
inheritance down the containment chain.

### Array keys (`-a` / `--array`) and table keys (`-t` / `--table`)

`config set` / `config unset` also mutate a string **array** or a **table**
element-wise, instead of writing a scalar:

- `wtm config set KEY VALUE -a [-s SCOPE]` — add `VALUE` to the array at `KEY`
  (**set semantics**: idempotent, a value already present is a no-op); creates
  the array on first add. `KEY` may be dotted (`ci.ports`).
- `wtm config unset KEY VALUE -a [-s SCOPE]` — strip every occurrence of `VALUE`
  from that array.
- `wtm config set KEY SUBKEY VALUE -t [-s SCOPE]` — set `SUBKEY = VALUE` in the
  table at `KEY`; creates the table on first set. `KEY` may be dotted, so this is
  the one-level form of a dotted write.
- `wtm config unset KEY SUBKEY -t [-s SCOPE]` — remove `SUBKEY` from that table.

`-a` and `-t` are mutually exclusive. Values are always stored as **strings**.
Mutating a key that already holds a different kind (a scalar, or an array where a
table is expected, or vice versa) is a hard error (the same kind-clash rule reads
enforce). Arrays do **not** merge across scopes (unlike `hooks.on` /
`inheritable`, and unlike *tables* which deep-merge per cell): a read takes the
most-local scope's whole array. Give each tier a
distinct name when you want per-tier arrays that don't shadow one another — e.g.
a per-worktree port registry beside a per-project one:

```toml
# workspace scope
project_ports = ["8080"]
# worktree scope
worktree_ports = ["3001", "3002"]
```

Every config write is serialised by a **per-directory lock** (`flock` on the
config file's containing dir, which is stable — unlike the file, replaced on
every write) and is a **read-modify-write**: it re-reads the file fresh under
the lock and applies just its delta, so a concurrent writer's *other* keys
survive (no clobber when two invocations touch the same file, and no lost
element when they mutate the same array). A write that changes nothing (e.g.
removing an absent key) is elided — it neither creates an empty file nor churns
an existing one.

An **inheritable** key resolves up the containment chain rather than staying
entity-local — it is a *non-entity-specific* key: a shared default describing a
whole subtree (like `base_branch`), not a property of one node (like
`description`). `config get` on such a key folds the entity's own cascade plus
its ancestors' (project → workspace → global default), so it reports the same
effective value a real command would use; for it `--origin` labels each rung by
its definition level (`project` / `workspace` / `global`) — matching
`config show` — rather than its raw scope, and still names the file.

Two builtin keys are classified in code: `base_branch` inherits, `description`
does not. Every arbitrary key is **entity-local by default**. To opt one into
inheritance, name it in a top-level `inheritable` array — maintained through the
same array flags (the one reserved key those flags may touch):

```toml
inheritable = ["deploy_env", "team"]   # these arbitrary keys inherit like base_branch
deploy_env = "staging"                 # a global default every entity may inherit
```

```sh
wtm config set inheritable deploy_env -a     # opt a key into inheritance
wtm config unset inheritable deploy_env -a   # opt it back out
```

Each element is validated: a reserved key or a hardcoded-non-inheritable builtin
(`description`) is refused (opting it in would contradict the hardcoded rule).
The `inheritable` declaration **accumulates** (unions) across scopes like
`hooks.on`: a workspace may name a key to opt it in for all its projects, even
if the global file does not. Membership is always a curated decision — hardcoded
(builtins) or declared by you — never inferred from a key's name, type, or
value. `base_branch` stays a project-tier concept with no worktree tier of its
own, so a worktree-local `base_branch` is ignored (keeping `config get` aligned
with `create`); a user-declared inheritable key set worktree-local *is* honoured.

Reserved names (`hooks`, `projects`, `worktrees`, `path`, `default_workspace`,
`inheritable`) have their own surfaces and are refused as arbitrary keys.

## Inline hooks (`hook set/unset`)

`wtm hook set EVENT "CMD" [-s SCOPE]` / `wtm hook unset EVENT [-s SCOPE]` write
an inline hook (`[...hooks.on] EVENT = "CMD"`) at the cwd-inferred entity's
table-path, reusing the same scope machinery. A `set` replaces the command in
that one file; commands still accumulate across scopes at read time.

## Showing the effective config

`wtm config show` renders the effective merge as one **global-rooted pruned
tree**. A single optional locator (or the cwd) resolves a target entity; the
branch from global down to that target is always shown, and `-r` extends
downward into the target's subtree.

```
wtm config show [LOCATOR...] [-g] [-r] [-d TIER] [-o/--origin] [--hooks] [-j/--json]
```

**Target resolution** (first match wins):

1. `-g` with any locator token → error (mutually exclusive);
2. `-g` → target is the global tier;
3. one or more locator tokens → an unpinned locator resolves the target across
   every declared workspace, auto-detecting its tier (worktree > project >
   workspace). The locator is config-aware, so a project declared in config but
   absent on disk (inert), or a worktree declared but not yet created, still
   resolves;
4. no tokens → inferred from the cwd (worktree > project > workspace), falling
   back to global when the cwd is outside every declared workspace.

**Depth cap (`-d TIER`):** `global`=0, `workspace`=1, `project`=2,
`worktree`=3. Without `-d` the deepest tier rendered defaults to the target's
own tier (the branch down to it, nothing below); with `-r` it defaults to
`worktree` (the fullest descent). An explicit `-d TIER` caps the deepest tier
rendered *anywhere* in the output — it prunes the target itself when shallower
(e.g. from a worktree, `-d project` drops the worktree tier, leaving
project + workspace + global), and is a silent no-op when deeper than the target
without `-r`. `global` is a valid value.

**Recurse (`-r`):** additive. The branch from global to the target is always
shown; `-r` additionally expands the target's subtree (its siblings under each
ancestor and its own descendants) up to the depth cap.

| Command | Output |
|---------|--------|
| `config show` (in a worktree) | global → workspace → project → worktree |
| `config show -d project` (in a worktree) | global → workspace → project (worktree pruned) |
| `config show myproj` | global → workspace → project |
| `config show myproj -r` | global → workspace → project + every worktree |
| `config show myproj -r -d project` | global → workspace → project (no worktrees) |
| `config show -g` | global tier only |
| `config show -g -r` | everything (every workspace, project, worktree) |
| `config show -g -r -d project` | every workspace and project, no worktrees |
| `config show myws -r -d project` | global → workspace + all its projects, no worktrees |

On-disk entities are unioned with declared ones, so annotated-but-inert
projects and declared-but-absent worktrees still surface (an inert project is
marked `(no .git on disk)`). Arbitrary keys resolved along the
shown branch are always printed.

`--origin` tags each value with its **winning** scope, arbitrary keys
included: a section is labelled per cell (each leaf its own carrier, e.g.
`ci:` / `  registry: ghcr  [project]`), mirroring `config get KEY --origin`'s
per-cell provenance in the `config show` tree style. (The full per-rung chain
lives in `config get KEY --origin`.) `--hooks` also renders accumulated inline
hooks per scope. Both are **ungated** — they apply in every mode, with or
without `-r` or `-g` (the former subtree-only restriction is gone).

`-j`/`--json` emits the result as a nested JSON object instead of text, always
rooted at global (`default_base_branch`, `base_branch_source`, `extra`, then
`workspaces` → `projects` → `worktrees`, pruned exactly as the text form).
Effective values only — `--json` combined with `--origin` or `--hooks` is an
error (use `config get KEY --origin` for structured provenance).

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
