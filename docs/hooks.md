# Hooks

Hooks are how anything project-specific — shared data dirs, `direnv`, a
`.venv`, `tmux`/`sesh` sessions — attaches to `wtm`'s lifecycle. `wtm` itself
knows only the workspace/project/worktree structure; everything else lives in
hooks.

A hook is either a **builtin** discovered file or an **inline** `sh -c` command
declared in config; explicit inline hooks *add to* the builtins. `wtm` fires
thirteen **events** across five **scopes**.

## Events

Event names follow a `pre-`/`post-` convention: a prefix marks the side of a
state mutation, a bare name is a mutation-free moment.

| event | fires on |
|---|---|
| `pre-create` / `post-create` | `wtm create` |
| `install` | `wtm create -i`, `wtm enter -i`, or `wtm hook run install` |
| `enter` | whenever `wtm` cds you into a worktree (`wtm enter`, `wtm create -c`) |
| `leave` | `wtm leave` |
| `pre-delete` / `post-delete` | `wtm delete` |
| `pre-rename` / `post-rename` | `wtm rename` |
| `project-enter` | `wtm project enter` |
| `project-leave` | `wtm project leave` |
| `workspace-enter` | `wtm workspace enter` |
| `workspace-leave` | `wtm workspace leave` |

Within a single `wtm create -c -i` the order is `pre-create` → `post-create` →
`install` → `enter`; within `wtm enter -i` it is `install` → `enter`.

Nine of the events are worktree events. The two `project-enter` /
`project-leave` events are project-scoped: fired by `wtm project enter`/`leave`,
across the system/user/workspace/project scopes only (never worktree), with the
project-level `WTM_*` contract (`WTM_EVENT` plus `WTM_PROJECT`,
`WTM_PROJECT_DIR`, `WTM_WORKTREES_DIR`, `WTM_GIT_COMMON_DIR`, `WTM_WORKSPACE`,
`WTM_WORKSPACE_DIR`; no `WTM_WORKTREE_DIR`/`WTM_BRANCH`; their `$1` is the project
dir). The two `workspace-enter` / `workspace-leave` events are workspace-scoped:
fired by `wtm workspace enter`/`leave`, across the system/user/workspace scopes
only, with a workspace-only `WTM_*` contract (`WTM_EVENT`, `WTM_WORKSPACE`,
`WTM_WORKSPACE_DIR`; their `$1` is the projects root).

The teardown side cascades inside-out. `wtm project leave`, run from inside one
of the project's worktrees, fires that worktree's `leave` first, then
`project-leave` (worktree → project). `wtm workspace leave`, run from inside a
worktree, fires the full chain: that worktree's `leave`, then the containing
project's `project-leave`, then `workspace-leave` (worktree → project →
workspace).

## Scopes and discovery

Builtin file discovery per scope (a bare `wtm-<event>` file plus a
`wtm-<event>.d/*` directory):

- **system**    — `/etc/wtm/hooks` (override: `$WTM_SYSTEM_HOOKS_DIR`, or config `[hooks] dir`)
- **user**      — `$XDG_CONFIG_HOME/wtm/hooks` (override: `$WTM_USER_HOOKS_DIR`, or config `[hooks] dir`)
- **workspace** — `<workspace>/.wtm/hooks` (override: workspace config `[hooks] dir`)
- **project**   — `<project>/.git/hooks` (override: project config `[project.hooks] dir`)
- **worktree**  — `<worktree>/.wtm/hooks` (override: worktree config `[hooks] dir`)

Every `[hooks] dir` override applies to firing *and* management alike —
`hook install` / `delete` / `list` resolve through the same directory authority,
so what you install is where firing looks.

Inline hooks are declared as an event→command(s) map in the matching config
scope (see [configuration.md](configuration.md)): a scope's own `[hooks.on]`, a
workspace entry's `[workspaces.NAME.hooks.on]`, a project's `[project.hooks.on]`
/ `[workspaces.NAME.projects.PNAME.hooks.on]`. A command may be a string or a
list of strings; each runs via `/bin/sh -c` with the same `$1` + `WTM_*`
contract and strict semantics as file hooks. Set `builtins = false` in a
scope's `[hooks]` table to disable *that scope's* builtin file discovery (inline
hooks still fire).

## Fire-time policy

Two tri-state booleans live in any scope's `[hooks]` table and resolve through
config like everything else — folded most-local-present-wins down the fired
scope ladder, so one effective answer per operation (a less-local declaration
fills a gap; the most-local one wins):

```toml
[hooks]
enabled = false   # fire nothing for operations under this entity
strict = true     # hook failures are fatal without --strict
```

The precedence tower, high to low:

1. `WTM_NO_HOOKS` (env) — master off switch, wins over everything;
2. the CLI `--strict` flag — force-on only (it can never un-strict);
3. the config ladder (`[hooks] enabled` / `[hooks] strict`);
4. defaults — enabled, non-strict.

Both knobs are **fire-time only**: `hook install`, `hook delete`, and
`hook list` always operate. Write them with the arbitrary-key facility
(`wtm config set hooks.strict true [-s SCOPE]`, `wtm config unset …`) —
`config get hooks.strict` then reports exactly what firing would resolve.
Unknown sub-keys under `[hooks]` are tolerated; the known booleans must be
TOML booleans.

## Firing order

Within a scope, builtin files run first (the bare `wtm-<event>` file before its
`wtm-<event>.d/*` entries, sorted lexically), then inline commands in declared
order.

Across scopes, order depends on the phase:

- **Setup events** (`pre-create`, `post-create`, `install`, `enter`,
  `pre-rename`, `post-rename`, `project-enter`, `workspace-enter`) run
  most-general first:
  **system → user → workspace → project → worktree**.
- **Teardown events** (`leave`, `pre-delete`, `post-delete`, `project-leave`,
  `workspace-leave`) reverse to
  most-specific first: **worktree → project → workspace → user → system**.

## Timing relative to the state change

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

## The `WTM_*` contract

Hooks receive the worktree directory as `$1` plus a `WTM_*` environment
contract:

`WTM_EVENT`, `WTM_WORKTREE_DIR`, `WTM_BRANCH`, `WTM_PROJECT`, `WTM_PROJECT_DIR`,
`WTM_WORKTREES_DIR`, `WTM_GIT_COMMON_DIR`, `WTM_WORKSPACE`, `WTM_WORKSPACE_DIR`.

`WTM_WORKSPACE` is the active workspace name (empty when the project is not
under a declared workspace); `WTM_WORKSPACE_DIR` is its path (the projects
root). The `pre-rename` event adds `WTM_NEW_DIR`/`WTM_NEW_BRANCH` (the
destination); the `post-rename` event adds `WTM_OLD_DIR`/`WTM_OLD_BRANCH` (the
pre-rename path and branch; equal to the new values when unchanged), so a hook
can reconcile path-keyed state, e.g.
`sesh rename "$WTM_OLD_DIR" "$WTM_WORKTREE_DIR"`.

Failures are logged but non-fatal unless the operation is strict — the CLI
`--strict` flag, or a ladder-resolved `[hooks] strict = true` (see
[Fire-time policy](#fire-time-policy)) — under which a failing `pre-*` hook
aborts its operation. `wtm` does not track or ignore
`$WTM_WORKTREE_DIR/.wtm/` — whether per-worktree hooks are committed to the
branch is left to you.

## Managing hooks

```
install FILE|DIR [TARGET] [-s SCOPE] [-e EVENT] [-l] [-f]  install a hook into a scope
run     HOOK [TARGET] [-s SCOPE] [--strict]                run an event's hooks (all scopes by default)
delete  HOOK [TARGET] [-s SCOPE] [-r]                      delete a hook (event, or event/entry)
list    [TARGET] [--types]                                 list installed hooks, grouped by event across scopes
```

- `wtm hook install FILE|DIR [TARGET] [--scope system|user|workspace|project|worktree] [--event E] [--link] [--force]`
  installs a hook (default scope `project`). The event is inferred from a
  `wtm-<event>` / `wtm-<event>.d` source name, or given with `--event`. A
  **file** joins an existing `wtm-<event>.d/` (keeping its basename) or otherwise
  becomes the bare `wtm-<event>` file; a **directory**'s contents are merged into
  `wtm-<event>.d/`. Copies are made executable (0755); `--link` symlinks the
  source instead. Existing destinations require `--force`. `--scope system`,
  `--scope user`, and `--scope workspace` need no target.
- `wtm hook run [TARGET] HOOK [--scope …] [--strict]` runs an event's hooks (all
  scopes unless `--scope` restricts it). `--strict` forces failures to be fatal
  on top of the config ladder. `HOOK` (the event) is the last
  positional; everything before it is a `TARGET`, which — like `hook list`'s — is
  a **locator** pinned to the worktree tier.
- `wtm hook delete [TARGET] HOOK [--scope …] [-r]` removes a hook. `HOOK` is the
  last positional: an event (removes the bare `wtm-<event>` file; its
  `wtm-<event>.d/` only with `-r/--recursive`) or `event/entry` (removes one
  `.d` entry).
- `wtm hook list [TARGET] [--types]` lists hooks grouped by event across scopes,
  builtin files and inline config hooks alike (inline ones marked `(config)`);
  `--types` prints a compact per-event summary. Being read-only over an existing
  worktree, its `TARGET` is a **locator** pinned to the worktree tier (see
  [commands.md](commands.md#the-locator)): a bare name reaches another
  workspace's worktree, and a name matching none is an error. `hook install`
  writes into a `--scope` and `hook delete` is destructive, so those two keep the
  positional target grammar.
- `wtm hook set EVENT "CMD" [--scope …]` / `wtm hook unset EVENT [--scope …]`
  write an inline hook (`[...hooks.on] EVENT = "CMD"`) at the cwd-inferred
  entity's table-path; `--scope` defaults to the deepest existing dir. A `set`
  replaces the command in that one file; commands still accumulate across scopes
  at read time. `hook install`/`delete` manage builtin files; `hook set`/`unset`
  manage inline commands.
