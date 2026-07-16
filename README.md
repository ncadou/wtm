# wtm — git worktree manager

`wtm` manages git worktrees laid out under one or more named **workspaces**
(each a projects root):

```
<workspace>/<project>/
    .git/                 bare repository
    worktrees/<name>/     worktrees
```

Workspaces are *declared* in config; projects are *discovered* on disk (a
`<name>/.git`) and only *annotated* by config. `wtm` knows only this structure;
anything project-specific (shared data dirs, direnv, tmux/sesh) belongs in
hooks, not in `wtm`.

Every command runs against one **active workspace**, resolved `-w/--workspace >
$WTM_WORKSPACE > cwd (longest path match) > default_workspace > the sole
declared workspace > error`. There is no built-in default: with no workspace
declared or selected, `wtm` errors and asks you to declare/select one.

## Commands

Commands are grouped under `workspace`, `project`, `worktree`, `hook`, and
`config`. Each worktree-level command also has a top-level alias (`wtm create …`
== `wtm worktree create …`), and `wtm clone …` / `wtm migrate …` alias the
matching `project` commands. The two forms are equivalent. A global
`-w/--workspace NAME` (before the subcommand) selects the active workspace.

**Workspaces** (`wtm workspace …`):

```
list                      declared workspaces (name, path, active marker '*', description)
resolve [NAME] [-e|--env] print a workspace's path (default: active); --env emits WTM_WORKSPACE(_DIR)
show    [NAME]            detail for one workspace (path, description, base_branch, scope, project count)
add     NAME [PATH] [-d DESC] [-b BASE] [-c]
                          register a workspace (user scope; PATH default: cwd; -c mkdir -p)
set     NAME [-p PATH] [-d DESC] [-b BASE]
                          edit an existing user-scope entry in place (empty value clears a field)
rename  OLD NEW           rename the registry key (does not move the directory)
delete  NAME             unregister a workspace (user scope; never touches the projects root)
default [NAME] [--clear] get/set/clear default_workspace
enter   [NAME]           cd into the projects root, firing workspace-enter hooks
leave   [NAME] [-C]      fire workspace-leave hooks (cascading the worktree leave when inside one); cd out unless -C
```

Workspaces are *declared* in config. `wtm` reads config from every scope but
*writes* only the user-scope file's `[workspaces.*]` registry and the top-level
`default_workspace` key — via `add`/`set`/`rename`/`delete`/`default`, using a
comment-preserving `tomlkit` round-trip with an atomic replace. System config
stays hand-edited (those verbs refuse system-only names).

**Config** (`wtm config …`):

```
show [--sources]          the effective merged config (--sources: declaring scope + inert-annotation flags)
```

**Projects** (`wtm project …`):

```
new     NAME                      create an empty project (.git/ + worktrees/)
clone   URL [NAME]                clone into a new project (bare); alias: wtm clone
list    [-t|--tree] [-w|--worktrees] [-j|--json] [-a|--all]
                                  list projects in the active workspace (--tree: with their worktrees;
                                  --worktrees: flat one-worktree-per-line, tab-separated
                                  project/name/path/branch/head; --json: those rows as JSON;
                                  --all: span every declared workspace, prepending a workspace column)
migrate [SOURCE|PROJECT] [opts]   fold a plain clone into the bare-repo layout; alias: wtm migrate
```

**Worktrees** (`wtm worktree …`, each also a top-level alias):

```
create  [PROJECT] NAME [-b BRANCH] [--base REF] [-d DIR] [-c] [-i]  add a worktree (+branch); -c to cd, -i to run install hooks
install ([PROJECT] NAME | DIR)                         run install hooks for a worktree (alias for 'wtm hook run install')
enter   ([PROJECT] NAME | DIR) [-c] [-b BRANCH] [--base REF] [-i]   cd into a worktree; -c creates it first if absent
leave   ([PROJECT] NAME | DIR) [-C]                    run leave hooks for a worktree; cds out unless -C (non-destructive)
delete  (PROJECT NAME | DIR) [-f] [-D]                 remove a worktree (-f force, -D delete branch too)
rename  ([PROJECT] [OLDNAME] | DIR) NEWNAME [-b]       rename/move a worktree; -b renames its branch too
list    [PROJECT]                                      list a project's worktrees (project inferred from cwd if omitted)
resolve [[PROJECT] NAME]                               print a worktree's path (no arg: the cwd worktree; --env: emit WTM_* for eval)
```

**Hooks** (`wtm hook …`):

```
install FILE|DIR [TARGET] [-s SCOPE] [-e EVENT] [-l] [-f]  install a hook into a scope
run     HOOK [TARGET] [-s SCOPE] [--strict]                run an event's hooks (all scopes by default)
delete  HOOK [TARGET] [-s SCOPE] [-r]                      delete a hook (event, or event/entry)
list    [TARGET] [--types]                                 list installed hooks, grouped by event across scopes
```

Run `wtm --help` (or `wtm COMMAND --help`) for the authoritative usage.

## Migrating an existing clone

`wtm migrate` folds a legacy plain clone (the *source*) into the bare-repo +
worktrees layout (the *target*), carrying over remotes, references, tags, and
the live working checkout (modified + untracked files) recreated as a worktree.
It automates the manual clone/push recipe.

Run it from inside the source repo (the target project name defaults to the
source's basename) or from under the projects root (the source is the lone
positional or `--source`). `--source PATH` / `--project NAME` override.

Target creation when the project is absent:

- default: bare-clone the source **on disk** (fast, offline, hard-linked),
  then repoint `origin` at the source's real origin URL
- `--from-origin`: bare-clone the origin URL over the network (full upstream)
- `--init-bare`: an empty bare repo (references then come from the push)

The bare repo always gets `remote.origin.fetch=+refs/heads/*:refs/remotes/origin/*`,
the dual-ref layout `create`'s branch resolution relies on. `--fetch` runs a
final `git fetch origin` afterwards to populate `refs/remotes/origin/*` and
`origin/HEAD`.

A target whose `.git` already exists is refused unless `--update` is passed
(migrating into it mutates a project that may already be in active use). With
`--update`, references are imported by pushing from the source: fast-forward-only
by default (rejected branches are reported; branches checked out in a target
worktree are skipped), or `--force` to overlay.

The worktree is recreated on the source's current branch (named after it; pass
`--name` for a detached HEAD) and its working tree is mirrored with `rsync
--archive --delete` (uncommitted deletions carry over; `.git` is always
excluded). Git-ignored files are copied **by default**; with no `--filter-file`
that includes heavy artefacts (`.venv`, `node_modules`, …), so pass
`--filter-file PATH` (rsync filter), `--exclude PATTERN`, or `--no-ignored`.

`--all-worktrees` reproduces the source's **whole worktree set** instead of just
the current checkout: every linked worktree the source has is recreated, each
named after its **source directory basename** (the main worktree included, so
its target name is the clone's dirname, not its branch) and placed on its own
branch. A detached worktree is reproduced by pinning a branch (named after the
source dir) at its exact commit. Worktrees whose target name already exists, or
whose branch is already checked out in the target, are **skipped with a warning**
and the rest still attempted; a hard failure on one worktree is reported and the
run exits non-zero, but the others are still created. Prunable (missing-on-disk)
source worktrees are skipped; locked ones are included. `-c/--cd` lands in the
worktree matching the one you invoked from, else the main one. `--all-worktrees`
is mutually exclusive with `--name` and `--no-worktree`.

Phases are toggleable: `--no-remotes`, `--no-refs`, `--no-worktree`,
`--no-tags`, `--no-ignored`; `--fetch`, `--force`, and `--update` are opt-in.
`-n/--dry-run` prints the plan and a per-worktree `rsync -n` preview with no
side effects; `-c/--cd` drops you into the new worktree. Migration is
non-destructive: the source is left intact (only a temporary push remote is
added and removed).


## Targeting a worktree

For `install` / `enter` / `leave` / `delete` / `hook run` / `hook install` / `hook delete`:

- `PROJECT NAME` → `<active-workspace>/PROJECT/worktrees/NAME`
- `NAME` → `<cwd-project>/worktrees/NAME` (project inferred from the current
  directory; `NAME` may be nested, e.g. `feature/x`)
- `DIR` → an explicit path (a single arg is a path only if it starts with `/`,
  `./`, `../`, or is `.`/`..`)
- no argument → the current directory (`delete` requires an explicit target)

`create` resolves a branch as: existing local branch → use it; `origin/NAME`
exists → track it; otherwise create a new branch off a base ref (`--base/--off
REF` > the project's configured `base_branch` > `origin/HEAD` > `HEAD`). A
configured/requested base is resolved as `origin/REF` then local `REF`; in a
populated repo a base that resolves to nothing is a hard error. If the
repository has no commits yet (e.g. a freshly-created project), the worktree is
bootstrapped on a new orphan branch regardless of any configured base.

`enter -c` makes entering idempotent: if the target worktree is absent it is
created first (with `create`'s branch resolution, `-b BRANCH` to override the
branch), then entered; if it already exists `enter` just cds in. With `-c` the
target is restricted to `PROJECT NAME` or `NAME` (no `DIR`, no cwd default), and
`-b` outside `-c` (or when the worktree already exists) is rejected/ignored.

`resolve` is a read-only path query. Given `PROJECT NAME` or `NAME` (no `DIR`)
it prints the computed worktree path without touching git, so it works for
not-yet-created targets — handy for `cd "$(wtm resolve PROJECT NAME)"`. Given no
argument it resolves the worktree you are standing in (inspecting git for its
toplevel), so a bare `wtm resolve` prints the current worktree's root; run
outside any worktree it errors, asking for a NAME. With `--env` it instead emits
the `WTM_*` contract as `export KEY='value'` lines for
`eval "$(wtm resolve --env …)"`; that mode always inspects git, so the worktree
must already exist.

`wtm project resolve [PROJECT]` is the project-level counterpart: it prints the
project directory (`<workspace>/PROJECT`), inferring PROJECT from the cwd
when omitted — `cd "$(wtm project resolve alpha)"`. It is pure path math (no git,
no existence check, so it works for an absent project). With `--env` it emits
only the project-level slice of the contract (`WTM_PROJECT`, `WTM_PROJECT_DIR`,
`WTM_WORKTREES_DIR`, `WTM_GIT_COMMON_DIR`, `WTM_WORKSPACE`, `WTM_WORKSPACE_DIR`)
— there is no `WTM_WORKTREE_DIR` or `WTM_BRANCH`, since a project has no single
worktree.

`rename` moves a worktree to a new name. Its positionals are parsed right-to-left:
the **last** positional is `NEWNAME`, and everything before it is an ordinary
source target (`PROJECT NAME | NAME | DIR | cwd`) — so `wtm rename NEWNAME`
renames the worktree you are standing in, `wtm rename OLDNAME NEWNAME` names the
source within the cwd project, and `wtm rename PROJECT OLDNAME NEWNAME` is fully
explicit. `NEWNAME` is validated like `create`'s NAME (nesting allowed, `..`
rejected, not a path) and always lands at `<project>/worktrees/NEWNAME` in the
**same** project. The directory is moved with `git worktree move` (a *locked*
worktree must be `git worktree unlock`'d first). `-b/--branch` additionally
renames the branch to `NEWNAME` (hard error on a detached HEAD; a no-op when it
already matches). A destination that already exists, or a branch-name collision,
is a hard error (never forced). If you rename the worktree you are standing in,
your shell follows the move to the corresponding subpath under the new location.

## Hooks

`wtm` fires eleven events across five scopes. A hook is either a **builtin**
discovered file or an **inline** `sh -c` command declared in config; explicit
inline hooks *add to* the builtins. Event names follow a `pre-`/`post-`
convention: a prefix marks the side of a state mutation
(`pre-create`/`post-create`, `pre-delete`/`post-delete`, `pre-rename`/`post-rename`),
a bare name is a mutation-free moment (`install`, `enter`, `leave`). Nine of the
events are worktree events; the two `workspace-enter`/`workspace-leave` events
are workspace-scoped (fired by `wtm workspace enter`/`leave`, across the
system/user/workspace scopes only, with a workspace-only `WTM_*` contract).

Builtin file discovery per scope (bare file + `wtm-<event>.d/*`):

- **system**    — `/etc/wtm/hooks` (override: `$WTM_SYSTEM_HOOKS_DIR`, or config `[hooks] dir`)
- **user**      — `$XDG_CONFIG_HOME/wtm/hooks` (override: `$WTM_USER_HOOKS_DIR`, or config `[hooks] dir`)
- **workspace** — `<workspace>/.wtm/hooks` (override: workspace config `[hooks] dir`)
- **project**   — `<project>/.git/hooks` (override: project config `[project.hooks] dir`)
- **worktree**  — `<worktree>/.wtm/hooks` (override: worktree config `[hooks] dir`)

Inline hooks are declared as an event→command(s) map in the matching config
scope (see *Configuration*): a scope's own `[hooks.on]`, a workspace entry's
`[workspaces.NAME.hooks.on]`, a project's `[project.hooks.on]` /
`[workspaces.NAME.projects.PNAME.hooks.on]`. A command may be a string or a
list of strings; each runs via `/bin/sh -c` with the same `$1` + `WTM_*`
contract and `--strict` semantics as file hooks. Set `builtins = false` in a
scope's `[hooks]` table to disable *that scope's* builtin file discovery (inline
hooks still fire).

Within a scope, builtin files run first (the bare `wtm-<event>` file before its
`wtm-<event>.d/*` entries, sorted lexically), then inline commands in declared
order. Setup events (`pre-create`, `post-create`, `install`, `enter`,
`pre-rename`, `post-rename`) run most-general first: **system → user →
workspace → project → worktree**; teardown events (`leave`, `pre-delete`,
`post-delete`) reverse to most-specific first: **worktree → project → workspace
→ user → system**.

Timing relative to the state change:

- `pre-create` fires *before* the worktree is created, so its
  `$WTM_WORKTREE_DIR` does not exist yet and its working directory is the
  project dir. A `--strict` veto (non-zero exit) aborts creation before any
  filesystem change.
- `post-create` fires *after* the worktree exists, from that worktree.
- `pre-delete` fires *before* the worktree is removed, so per-worktree teardown
  hooks under `$WTM_WORKTREE_DIR/.wtm/hooks` still exist when they run; its
  working directory is `$WTM_WORKTREE_DIR`.
- `post-delete` fires *after* removal is fully settled (worktree gone, empty
  parents pruned, branch handled). Its `$WTM_WORKTREE_DIR` no longer exists, so
  its working directory is the project dir.
- `pre-rename` fires *before* the move, from the old worktree; `post-rename`
  fires *after* it, from the **new** worktree (its per-worktree hooks moved with
  it). `leave` fires while the worktree still exists.

Worktree scope is inapplicable to `pre-create` and `post-delete` — there is no
live worktree to hold `.wtm/hooks`.

The lifecycle events fire from: `pre-create`/`post-create` on `wtm create`;
`install` on `wtm install`, `wtm create -i`, or `wtm enter -i`; `enter` whenever
`wtm` cds you into a worktree (`wtm enter`, and `wtm create -c`); `leave` on
`wtm leave`; `pre-delete`/`post-delete` on `wtm delete`; `pre-rename`/`post-rename`
on `wtm rename`. Within a single `wtm create -c -i` the order is `pre-create` →
`post-create` → `install` → `enter`; within `wtm enter -i` it is
`install` → `enter`. The workspace events fire from: `workspace-enter` on
`wtm workspace enter`; `workspace-leave` on `wtm workspace leave` (which, when
run from inside a worktree, fires that worktree's `leave` first, then
`workspace-leave`). The `workspace-*` events carry only `WTM_EVENT`,
`WTM_WORKSPACE`, and `WTM_WORKSPACE_DIR` (their `$1` is the projects root), and
fire across the system/user/workspace scopes only.

Hooks receive the worktree directory as `$1` plus a `WTM_*` environment contract
(`WTM_EVENT`, `WTM_WORKTREE_DIR`, `WTM_BRANCH`, `WTM_PROJECT`, `WTM_PROJECT_DIR`,
`WTM_WORKTREES_DIR`, `WTM_GIT_COMMON_DIR`, `WTM_WORKSPACE`, `WTM_WORKSPACE_DIR`).
`WTM_WORKSPACE` is the active workspace name (empty when the project is not under
a declared workspace); `WTM_WORKSPACE_DIR` is its path (the projects root). The
`pre-rename`
event adds `WTM_NEW_DIR`/`WTM_NEW_BRANCH` (the destination); the `post-rename`
event adds `WTM_OLD_DIR`/`WTM_OLD_BRANCH` (the pre-rename path and branch; equal
to the new values when unchanged), so a hook can reconcile path-keyed state, e.g.
`sesh rename "$WTM_OLD_DIR" "$WTM_WORKTREE_DIR"`. Failures are logged but
non-fatal unless `--strict` is passed (under which a failing `pre-*` hook aborts
its operation). `wtm` does not track or ignore `$WTM_WORKTREE_DIR/.wtm/` —
whether per-worktree hooks are committed to the branch is left to you.

### Managing hooks

- `wtm hook install FILE|DIR [TARGET] [--scope system|user|workspace|project|worktree] [--event E] [--link] [--force]`
  installs a hook (default scope `project`). The event is inferred from a
  `wtm-<event>` / `wtm-<event>.d` source name, or given with `--event`. A **file**
  joins an existing `wtm-<event>.d/` (keeping its basename) or otherwise becomes
  the bare `wtm-<event>` file; a **directory**'s contents are merged into
  `wtm-<event>.d/`. Copies are made executable (0755); `--link` symlinks the
  source instead. Existing destinations require `--force`. `--scope system`,
  `--scope user`, and `--scope workspace` need no target.
- `wtm hook run [TARGET] HOOK [--scope …] [--strict]` runs an event's hooks
  (all scopes unless `--scope` restricts it). `HOOK` (the event) is the last
  positional. `wtm install` is a shorthand for `wtm hook run install`.
- `wtm hook delete [TARGET] HOOK [--scope …] [-r]` removes a hook. `HOOK` is the
  last positional: an event (removes the bare `wtm-<event>` file; its
  `wtm-<event>.d/` only with `-r/--recursive`) or `event/entry` (removes one
  `.d` entry).
- `wtm hook list [TARGET] [--types]` lists hooks grouped by event across scopes,
  builtin files and inline config hooks alike (inline ones marked `(config)`);
  `--types` prints a compact per-event summary. `hook install`/`delete` manage
  builtin files only — inline hooks are edited in the config file.

## Shell integration

`wtm` writes diagnostics to stderr and emits shell directives out-of-band so the
calling shell can follow the move. The `wtm` function in `../env` provides the
wrapper: it passes a `WTM_SHELL_FILE` temp file to the real binary and *sources*
whatever the command writes there. The directives are a `cd` plus the
`export`/`unset` lines that sync the **`WTM_*` session contract** (see below).
This is what makes `wtm enter` and `wtm create -c` change the current shell's
directory *and* environment.

The contract follows a **session** model: entering sets it, leaving clears it.

- `enter` / `create -c` / `migrate -c` — `cd` into the worktree and export the
  full contract: `WTM_WORKTREE_DIR`, `WTM_BRANCH`, `WTM_PROJECT`,
  `WTM_PROJECT_DIR`, `WTM_WORKTREES_DIR`, `WTM_GIT_COMMON_DIR`, `WTM_WORKSPACE`,
  `WTM_WORKSPACE_DIR`.
- `rename` (when you are standing in the moved worktree) — follow it and update
  `WTM_WORKTREE_DIR` / `WTM_BRANCH`.
- `leave` — `cd` out (`$OLDPWD` or the project dir) and **unset** the whole
  contract. `-C`/`--no-cd` keeps you put but still unsets (leaving ends the
  session regardless of whether you also move).
- `delete` (when you are standing in the doomed worktree) — rescue-`cd` out and
  unset the contract.
- `workspace enter` — `cd` into the projects root and export only
  `WTM_WORKSPACE` / `WTM_WORKSPACE_DIR` (the six worktree/project vars are
  unset). `workspace leave` unsets everything (even with `-C`).

Because `WTM_WORKSPACE` is exported on `enter`, it **pins the active workspace**
(it beats cwd inference) until you `wtm leave` — the session is deliberately
sticky. Without the wrapper the commands stay silent about the environment (they
cannot alter the parent shell) and print a manual-`cd` notice for the target
directory only.


## Configuration

`wtm` reads TOML config from up to five scopes, mirroring the hook scopes,
most-general to most-specific:

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
expansion. Config is hand-edited (except the user-scope registry, which
`wtm workspace add/set/rename/delete/default` also writes); `wtm config show
[--sources]` prints the effective merge.

The **workspace registry** (name → path) lives at system/user scope; a
workspace's own metadata and its projects' annotations may also live in its
`<workspace>/.wtm/config.toml`. Projects are discovered on disk and only
annotated (by name) with a description, a `base_branch`, and hooks.

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

### Environment variables

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
- `WTM_SHELL_FILE` — the shell-wrapper directive channel (see *Shell integration*)

## Development

`wtm` uses [`uv`](https://docs.astral.sh/uv/) for dependency management and
[Ruff](https://docs.astral.sh/ruff/) for both formatting and linting (import
sorting included). The `Makefile` wraps the common tasks:

- `make reformat` — apply safe autofixes (import sorting, pyupgrade rewrites,
  dead-import removal) and format the code (`ruff check --fix` + `ruff format`)
- `make lint` — lint (`ruff check`: pyflakes, pycodestyle, isort, pyupgrade,
  bugbear, comprehensions)
- `make check` — verify formatting + lint without writing (CI-style gate)
- `make tests` — run the test suite (`pytest`)

The underlying commands are `uv run ruff check --fix src tests`, `uv run ruff
format src tests`, and `uv run pytest`. Ruff config lives in `pyproject.toml`
(`[tool.ruff]`): line length 99, target Python 3.10.
