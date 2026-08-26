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
outranks lesser locality** — the precedence rule behind every read. What config
describes is an **entity**: a workspace, a project, or a worktree, plus the
implicit **global** root above them all. Each entity's values fold across the
scope files that describe it by a locality-ordered, recursive merge; the
most-local *present* value of a key wins (`false`/`0`/`""` are real values —
clearing a key *removes* it rather than writing an empty string). Inline hooks
(`hooks.on`) are the one exception: they *accumulate* across scopes. Workspace
**paths** are `~`/`$VAR`-expanded and must be absolute after expansion.

An entity stores its *own* config at the **top level** of its own file; a
containing file nests a child under `<child-plural>.<name>`. So the same key —
say a project's `base-branch` — is writable (and correctly resolved) at every
scope above the project:

| where written | table in that file |
|---|---|
| project-local | top level |
| workspace-local | `[projects.NAME]` |
| user / system | `[workspaces.W.projects.NAME]` |

Worktrees add one more rung (`[worktrees.T]` project-local,
`[projects.P.worktrees.T]` workspace-local, and so on).

A taste — the [arbitrary-key facility](#arbitrary-keys-config-setgetunset)
writes any key at the entity your cwd infers (details below):

```sh
wtm config set ci.runner.image ubuntu   # lands in <project>/.wtm/config.toml
wtm config get ci.runner.image          # ubuntu
```

Most config is hand-edited; management commands write it for you — through one
authority, `wtm.config`, with a comment-preserving `tomlkit` round-trip and an
atomic replace (see [Concurrency](#concurrency)). It offers an arbitrary-key
facility (read/write any scalar at any named scope), a typed read view, and
named structural registry mutations (`add`/`set`/`rename`/`remove` workspace,
`default-workspace`, global `base-branch`) that write the **user file** and own
their cross-key fixups (a rename follows a dangling `default-workspace`).

## Write placement (`-s/--scope`)

Annotation writes name their target scope explicitly with `-s/--scope`
(mirroring `hook install`); the auto-default is the **deepest scope whose
directory exists**. The allowed scopes narrow by entity:

| command | writes | allowed `--scope` |
|---|---|---|
| `wtm workspace set -d/-b` | workspace `description` / `base-branch` | workspace, user, system |
| `wtm project set -d/-b` | project `description` / `base-branch` | project, workspace, user, system |
| `wtm worktree set -d` | worktree `description` | worktree, project, workspace, user, system |
| `wtm config base-branch [VALUE] [--clear]` | global `base-branch` (user file, top-level) | — |
| `wtm workspace default [NAME] [--clear]` | `default-workspace` (user file, top-level) | — |
| `wtm workspace add` / `set --path` | workspace `path` (user file registry entry) | — user-file only |

Workspace **declarations** (`add`/`set --path`/`rename`/`default`) stay
user-file-only. `WTM_NO_CONFIG` refuses all writes. Because a workspace-local
value *wins* on merge, clearing it there (`set -b ""`) lets a lesser-locality
value (e.g. the user-file registry annotation) resurface.

## Concurrency

Every config write — scalar, array element, table cell, structural mutation —
is serialised by a **per-directory lock** (`flock` on the config file's
containing dir, which is stable — unlike the file, replaced on every write) and
is a **read-modify-write**: it re-reads the file fresh under the lock and
applies just its delta, so a concurrent writer's *other* keys survive (no
clobber when two invocations touch the same file, and no lost element when they
mutate the same array). A write that changes nothing (e.g. removing an absent
key) is elided — it neither creates an empty file nor churns an existing one.

## Arbitrary keys (`config set/get/unset`)

Any arbitrary key resolves by the same locality merge.
`wtm config set/get/unset KEY [VALUE]` acts
on the **cwd-inferred entity** (the deepest one the cwd sits inside — worktree,
else project, else the active workspace); `-s/--scope` picks the file tier the
same way annotations do. An optional **locator** prefix names another entity
instead; `-g/--global` skips entities entirely and targets the global tier
(see [Global tier](#global-tier--g) below).

- `wtm config set KEY VALUE [-s SCOPE]` — write an arbitrary key. `KEY` may be a
  dotted path (`ci.runner.image`) naming a nested cell; see
  [Sections](#sections-dotted-paths-and-deep-merge) below.
- `wtm config get KEY [SUBKEY] [--origin] [--json]` — resolve it (most-local wins). A
  scalar prints bare, an array one element per line, a table `subkey = value` per
  line (sorted, nested sub-tables indented); pass `SUBKEY` to read a single table
  cell, or use a dotted `KEY` to read a nested cell. Non-string scalars print
  TOML-faithfully (booleans lower-case, dates ISO-8601). `--origin` lists where each
  value is defined: per-cell (`path = value  [source]  file`) for a table, or one
  line per carrying rung for a scalar/array — its scope, or its definition level
  (`project`/`workspace`/`global`) for an inherited key — and the file it lives in:

  ```text
  $ wtm config get port --origin
  3001  [worktree]  ~/work/api/worktrees/feat-42/.wtm/config.toml
  8080  [project]  ~/work/api/.wtm/config.toml
  ```

  `--json` instead emits the effective value as bare JSON (`8080`, `true`, a nested
  object, `null` when unset); mutually exclusive with `--origin`.
- `wtm config unset KEY [-s SCOPE]` — remove it from one tier.

### Global tier (`-g`)

`-g/--global` on all three verbs targets the **global tier**: top-level keys of
the user file (`-s system`: the system file), with no `[workspaces…]` nesting —
the same slots `config show -g` renders and `config base-branch` writes. Reads
probe both files with the user file winning. Mutually exclusive with a locator
(and with SUBKEY on `get`; a dotted `KEY` still reads a nested cell). A
global-tier key is still **entity-local by
default**: until the key is also named in an `inheritable` array, no descendant
entity resolves it.

### Sections (dotted paths and deep-merge)

A `KEY` may be a **dotted path** (`ci.runner.image`) naming a cell inside a
section — a TOML table. `config set ci.runner.image ubuntu` writes
`[ci.runner] image = "ubuntu"`; `config get ci.runner.image` reads it back. The
`-t`/`--table` flag is the one-level shorthand for the same thing:
`config set ci.runner image ubuntu -t` (KEY=`ci.runner`, SUBKEY=`image`) is
identical to `config set ci.runner.image ubuntu`. A bare `KEY` keeps its
top-level meaning. Only the **head** segment of a dotted path is checked against
the reserved names (`hooks` / `projects` / `worktrees` / `workspaces` / `path` /
`default-workspace` / `inheritable` / `plugins`) — deeper segments are harmless
cells, so
`ci.workspaces` is allowed (a cell named `workspaces` inside your `ci` section),
but `hooks.on.x` is refused. The one carve-out: the hook policy cells
`hooks.strict` and `hooks.enabled` (exactly two segments, boolean values) are
writable here — they have no dedicated verb (see
[hooks.md](hooks.md#fire-time-policy)); every other `hooks.*` path stays
reserved (`hooks.on.*` belongs to `hook set/unset`). Writing into a key head
owned by a known-but-inactive [plugin](plugins.md) succeeds with a warning;
while the plugin is active it is silent and normal.

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
  from that array (matching by native equality; see **Typed values** below).
- `wtm config set KEY SUBKEY VALUE -t [-s SCOPE]` — set `SUBKEY = VALUE` in the
  table at `KEY`; creates the table on first set. `KEY` may be dotted, so this is
  the one-level form of a dotted write.
- `wtm config unset KEY SUBKEY -t [-s SCOPE]` — remove `SUBKEY` from that table.

`-a` and `-t` are mutually exclusive. Values are stored as **strings** unless a
typed opt-in says otherwise (see below). Mutating a key that already holds a
different kind (a scalar, or an array where a
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

### Typed values (`--type` / `--toml`)

Bare writes store strings. Two opt-in flags make a write land as its native TOML
kind instead — useful when other tools read the file directly, or when an integer
should not spend its life quoted:

- `wtm config set KEY VALUE --type int|float|bool|str` — coerce `VALUE` to the
  named scalar. `bool` accepts click's truthiness tokens case-insensitively
  (`true/t/1/yes/y/on` vs `false/f/0/no/n/off`; anything else is refused).
- `wtm config set KEY VALUE -x/--toml` — parse `VALUE` exactly as TOML would:
  every scalar spelling, including the three date/time forms
  (`1979-05-27T07:32:00Z`). Arrays and tables parse but are **not writable yet**
  (scalars only) — element-wise `-a`/`-t` remain the way to grow them.

The two flags are mutually exclusive; both compose with `-a` (typed elements,
removed by their own kind of value via the same flags on `config unset`) and
`-t` (typed cells). They are refused on `inheritable -a`, whose elements are key
declarations, not values. A typed overwrite silently replaces whatever kind was
there; cross-tier reads still resolve most-local, regardless of kind.

Reads were always native-typed (a hand-written `port = 8080` resolves as an
integer); text output renders non-strings TOML-faithfully so what `config get`
prints round-trips back through `--toml`, and `config_slice_json`
(`config show --json`, `config get --json`) encodes dates as ISO-8601 strings.
The structural tier (`base-branch`, `path`, descriptions, hook commands) stays
string-only by nature.

```sh
wtm config set -g port 8080 --type int          # port = 8080
wtm config set -g strict on --type bool         # strict = true
wtm config set -g since 1979-05-27T07:32:00Z -x # a real TOML datetime
wtm config get -g port --json                   # 8080
```

### Inheritance (the `inheritable` axis)

An **inheritable** key resolves up the containment chain rather than staying
entity-local — it is a *non-entity-specific* key: a shared default describing a
whole subtree (like `base-branch`), not a property of one node (like
`description`). `config get` on such a key folds the entity's own cascade plus
its ancestors' (project → workspace → global default), so it reports the same
effective value a real command would use; for it `--origin` labels each rung by
its definition level (`project` / `workspace` / `global`) — matching
`config show` — rather than its raw scope, and still names the file.

Two builtin keys are classified in code: `base-branch` inherits, `description`
does not. Every arbitrary key is **entity-local by default**. To opt one into
inheritance, name it in a top-level `inheritable` array — maintained through the
same array flags (the one reserved key those flags may touch):

```toml
inheritable = ["deploy_env", { key = "region", floor = "project" }]
deploy_env = "staging"                 # a global default every entity may inherit
region = "eu-west"                     # participates at project tier and above only
```

```sh
wtm config set inheritable deploy_env -a                    # opt a key into inheritance
wtm config set -g inheritable deploy_env -a                 # ... declared at the global tier
wtm config set inheritable region -a --floor project        # ... with a tier floor
wtm config unset inheritable region -a                      # opt it back out (by name)
```

Each element is validated: a reserved key or a hardcoded-non-inheritable builtin
(`description`) is refused (opting it in would contradict the hardcoded rule),
and so is any other shape than a key name or a `{ key = ..., floor = ... }`
table — a malformed hand-edited array is a hard error at read time, like a kind
clash. The `inheritable` declaration **accumulates** (unions) across scopes like
`hooks.on`: a workspace may name a key to opt it in for all its projects, even
if the global file does not. When several scopes declare floors for the same
key, the most-local declaration wins. Membership is always a curated decision —
hardcoded (builtins), declared by you, or declared by an active
[plugin](plugins.md) (whose elements form a baseline that travels with the
plugin's activation: your explicit `inheritable` entries outrank a plugin's
floor for the same key, and deactivating the plugin removes its baseline) —
never inferred from a key's name,
type, or value.

### Floors

A **floor** is a per-key attribute naming the *deepest entity tier the key
participates in* (`worktree < project < workspace < global`). A floored key's
resolution chain drops every rung below its floor, and a write that would land
below it (`-s/--scope` deeper than the floor, including through an ancestor
registry table) is refused with `WriteProhibitedError`. Removing such a value
is never refused — unsetting a stray below-floor value is cleanup, not
placement.

`base-branch` is the builtin example: its floor is `project`, because a fork
point for new branches is a property of a project subtree, never of one
checkout — a worktree-local `base-branch` neither resolves nor writes, keeping
`config get` aligned with what `create` forks from. User-declared floors give
your own keys the same shape: `{ key = "region", floor = "workspace" }` makes
`region` a workspace-and-above default that no project or worktree can override.
A floor on `base-branch` itself is refused — it owns a typed surface whose
resolution is fixed.

Reserved names (`hooks`, `projects`, `worktrees`, `workspaces`, `path`,
`default-workspace`, `inheritable`, `plugins`) have their own surfaces and are
refused as arbitrary keys.

## Plugins (`[plugins]`)

Plugin activation is a reserved table at any scope file's top level — one
boolean per plugin, deep-merged per cell most-local-wins like any section (so
a workspace may disable one plugin without touching the others):

```toml
[plugins]
wtm-git = true
wtm-server = false      # a deeper scope can shadow an outer true, per plugin
```

Manage it with `wtm plugin add/remove/list/show` rather than by hand; what the
activation buys and how manifests are resolved is [plugins.md](plugins.md)'s
subject.

## Inline hooks (`hook set/unset`)

`wtm hook set EVENT "CMD" [-s SCOPE]` / `wtm hook unset EVENT [-s SCOPE]` write
an inline hook (`[...hooks.on] EVENT = "CMD"`) at the cwd-inferred entity's
table-path, reusing the same scope machinery. A `set` replaces the command in
that one file; commands still accumulate across scopes at read time.

## Showing the effective config

`wtm config show` renders the resolved config as one **global-rooted pruned
tree**. A single optional locator (one or more positional tokens), or the cwd,
resolves a target entity; the branch from global down to that target is always
shown, and `-r` extends downward into the target's subtree.

```text
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

A run against the [example config](#example) below (with a `feat-42` worktree
checked out), from inside that worktree:

```text
$ wtm config show
default base-branch: main
workspace work: /home/nia/work
  description: day job
  project api: /home/nia/work/api
    base-branch: develop
    worktree feat-42: /home/nia/work/api/worktrees/feat-42
```

The text view shows each value **once, at its defining tier**: an inherited
value is suppressed at descendants, whose defining ancestor is always rendered
above on the same branch — here the global `base-branch: main` prints once at
the root even though `api` and `feat-42` both resolve to it (`api`'s own
`develop` overrides it for the project subtree). Arbitrary keys follow the same
rule. `--json` is the counterpart view: full *effective* merged values,
inherited values repeated at every descendant.

On-disk entities are unioned with declared ones, so annotated-but-inert
projects and declared-but-absent worktrees still surface (an inert project is
marked `(no .git on disk)`).

`--origin` tags each value with its **winning** scope, arbitrary keys
included: a section is labelled per cell (each leaf its own carrier, e.g.
`ci:` / `  registry: ghcr  [project]`), mirroring `config get KEY --origin`'s
per-cell provenance in the `config show` tree style. (The full per-rung chain
lives in `config get KEY --origin`.) `--hooks` also renders accumulated inline
hooks per scope. Both are **ungated** — they apply in every mode, with or
without `-r` or `-g` (the former subtree-only restriction is gone).

`-j`/`--json` emits the result as a nested JSON object instead of text, always
rooted at global (`default-base-branch`, `extra`, then
`workspaces` → `projects` → `worktrees`, pruned exactly as the text form).
Effective values only — inherited values appear at every descendant — and no
provenance: `--json` combined with `--origin` or `--hooks` is an error (use
`config get KEY --origin` for structured provenance).

The **workspace registry** (name → path) lives at system/user scope; a
workspace's own metadata and its projects' annotations may also live in its
`<workspace>/.wtm/config.toml`. Projects are discovered on disk and only
annotated (by name) with a description, a `base-branch`, and hooks.

## Example

```toml
# ~/.config/wtm/config.toml
default-workspace = "work"          # active fallback when cwd matches none
base-branch = "main"                # global default fork point for new branches

[workspaces.work]
path = "~/work"
description = "day job"
[workspaces.work.hooks.on]          # workspace-scope inline hooks (event -> cmd(s))
post-create = "direnv allow"
enter = ["tmux new-window", "echo entered $WTM_PROJECT"]
[workspaces.work.projects.api]      # annotate a discovered project
base-branch = "develop"
[workspaces.work.projects.api.hooks.on]
install = "make deps"

[workspaces.oss]
path = "$HOME/oss"

[hooks]                             # this file's own (user) scope
builtins = false                    # disable user-scope builtin file discovery
strict = true                       # hook failures fatal everywhere (ladder-resolved)
enabled = false                     # …or fire nothing at all (wins over strict)
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
- `WTM_NO_HOOKS` — disable all hook firing (master off switch, above the `[hooks] enabled`/`strict` config ladder and `--strict`)
- `XDG_CONFIG_HOME` — user config + user hooks location
- `WTM_SHELL_FILE` — the shell-wrapper directive channel (see
  [shell-integration.md](shell-integration.md))
