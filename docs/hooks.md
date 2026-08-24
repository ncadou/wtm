# Hooks

Hooks are how anything project-specific — shared data dirs, `direnv`, a
`.venv`, `tmux`/`sesh` sessions — attaches to `wtm`'s lifecycle. `wtm` itself
knows only the workspace/project/worktree structure; everything else lives in
hooks.

A hook is either a **builtin** discovered file or an **inline** `sh -c` command
declared in config; explicit inline hooks *add to* the builtins. `wtm` fires
thirteen lifecycle **events** across five **scopes**, plus the fourteenth,
`post-config`, a config-write broadcast described below.

## A first hook

Drop an executable file named after an event into a scope's hooks directory —
here, the project's:

```sh
mkdir -p <project>/.wtm/hooks
cat > <project>/.wtm/hooks/post-create <<'EOF'
#!/bin/sh
echo "new worktree: $1 (branch $WTM_BRANCH)"
EOF
chmod +x <project>/.wtm/hooks/post-create
wtm create alpha feature-x             # fires post-create for this project
```

Or declare the same thing inline in config (each command runs via `/bin/sh -c`
with the same `$1` + [`WTM_*` contract](#the-wtm_-contract)):

```toml
# <project>/.wtm/config.toml
[hooks.on]
post-create = "uv sync"
enter = ["direnv allow"]               # a list = several commands, in order
```

Confirm what will fire with `wtm hook list` (grouped by event, across every
scope). Everything below is detail.

## Events

Event names follow a `pre-`/`post-` convention: a prefix marks the side of a
state mutation, a bare name is a mutation-free moment.

| event | fires on |
|---|---|
| `pre-create` / `post-create` | `wtm create`, `wtm import` (per recreated worktree) |
| `install` | `wtm create -i`, `wtm enter -i`, or `wtm hook run install` |
| `enter` | whenever `wtm` cds you into a worktree (`wtm enter`, `wtm create -c`) |
| `leave` | `wtm leave` |
| `pre-delete` / `post-delete` | `wtm delete` |
| `pre-rename` / `post-rename` | `wtm rename` |
| `project-enter` | `wtm project enter` |
| `project-leave` | `wtm project leave` |
| `workspace-enter` | `wtm workspace enter` |
| `workspace-leave` | `wtm workspace leave` |
| `post-config` | after a `wtm` invocation wrote one or more config files |

Within a single `wtm create -c -i` the order is `pre-create` → `post-create` →
`install` → `enter`; within `wtm enter -i` it is `install` → `enter`.

### Event tiers

Nine of the thirteen lifecycle events are worktree events: they fire for one
worktree across all five scopes, with the full
[`WTM_*` contract](#the-wtm_-contract) and the worktree directory as `$1`
(two exceptions under [timing](#timing-relative-to-the-state-change)). The four
others are the project- and workspace-lifecycle pairs:

| pair | fired by | scopes fired | `$1` / working dir | env contract |
|---|---|---|---|---|
| `project-enter` / `project-leave` | `wtm project enter` / `leave` | system → user → workspace → project (never worktree) | the project dir | `WTM_EVENT` plus `WTM_PROJECT`, `WTM_PROJECT_DIR`, `WTM_WORKTREES_DIR`, `WTM_GIT_COMMON_DIR`, `WTM_WORKSPACE`, `WTM_WORKSPACE_DIR`; no `WTM_WORKTREE_DIR`/`WTM_BRANCH` |
| `workspace-enter` / `workspace-leave` | `wtm workspace enter` / `leave` | system → user → workspace only | the projects root | `WTM_EVENT`, `WTM_WORKSPACE`, `WTM_WORKSPACE_DIR` |

The teardown side cascades inside-out. `wtm project leave`, run from inside one
of the project's worktrees, fires that worktree's `leave` first, then
`project-leave` (worktree → project). `wtm workspace leave`, run from inside a
worktree, fires the full chain: that worktree's `leave`, then the containing
project's `project-leave`, then `workspace-leave` (worktree → project →
workspace).

### post-config

The one event outside the lifecycle scheme: a **broadcast** fired once per
invocation, after the command settles, whenever that invocation actually wrote
(flushed) any config file — via management commands, annotations,
`hook set/unset`, or structural registry mutations. Hand edits never fire
anything.

Its contract differs from the lifecycle events:

- `WTM_EVENT=post-config` and `WTM_CONFIG_FILES` (newline-separated absolute
  paths of every file written, sorted) replace the subject-based vars; the
  workspace pair (`WTM_WORKSPACE`/`WTM_WORKSPACE_DIR`) is present when the
  changed paths imply one. `$1` and the working directory are the deepest
  common ancestor of the changed files.
- Scope context is *implied by the changed paths*: a `<dir>/.wtm/config.toml`
  names its entity (a worktree when under a project's `worktrees/`, a project
  when it holds a bare `.git`, else a declared workspace). Scopes that cannot
  be resolved from it are skipped silently.
- Firing is suppressed inside hook subprocesses (`WTM_IN_HOOK=1`): a hook
  that writes config never recurses; the outer invocation's own firing covers
  those writes.
- The natural consumer is an auto-commit hook (e.g. from `wtm-git`), which
  reads `WTM_CONFIG_FILES` and syncs the repos tracking them.

## Scopes and discovery

Builtin file discovery per scope (a bare `<event>` file plus a
`<event>.d/*` directory):

- **system**    — `/etc/wtm/hooks` (override: `$WTM_SYSTEM_HOOKS_DIR`, or config `[hooks] dir`)
- **user**      — `$XDG_CONFIG_HOME/wtm/hooks` (override: `$WTM_USER_HOOKS_DIR`, or config `[hooks] dir`)
- **workspace** — `<workspace>/.wtm/hooks` (override: workspace config `[hooks] dir`)
- **project**   — `<project>/.wtm/hooks` (override: project config `[hooks] dir`)
- **worktree**  — `<worktree>/.wtm/hooks` (override: worktree config `[hooks] dir`)

Every `[hooks] dir` override applies to firing *and* management alike —
`hook install` / `delete` / `list` resolve through the same directory authority,
so what you install is where firing looks.

Inline hooks are declared as an event→command(s) map (`[hooks.on]`) in the
matching config scope (see [configuration.md](configuration.md)), following the
usual nesting rule: an entity's own file keeps the table top level, a
containing file nests it under `<child-plural>.<name>`:

| written in | inline-hook tables |
|---|---|
| system / user file | `[hooks.on]` (that file's own scope), `[workspaces.W.hooks.on]` |
| workspace file | `[hooks.on]` (the workspace itself), `[projects.P.hooks.on]`, `[projects.P.worktrees.T.hooks.on]` |
| project file | `[hooks.on]` (the project itself), `[worktrees.T.hooks.on]` |
| worktree file | `[hooks.on]` (the worktree itself) |

A command may be a string or a list of strings; each runs via `/bin/sh -c` with
the same `$1` + `WTM_*` contract and strict semantics as file hooks. Set
`builtins = false` in a scope's `[hooks]` table to disable *that scope's*
builtin file discovery (inline hooks still fire).

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
2. the CLI `--strict` flag (accepted by every firing command: `create`, `enter`,
   `leave`, `delete`, `rename`, `project enter`/`leave`,
   `workspace enter`/`leave`, `import`, `hook run`) — force-on only (it can
   never un-strict);
3. the config ladder (`[hooks] enabled` / `[hooks] strict`);
4. defaults — enabled, non-strict.

Both knobs are **fire-time only**: `hook install`, `hook delete`, and
`hook list` always operate. Write them with the arbitrary-key facility
(`wtm config set hooks.strict true [-s SCOPE]`, `wtm config unset …`) —
`config get hooks.strict` then reports exactly what firing would resolve.
Unknown sub-keys under `[hooks]` are tolerated; the known booleans must be
TOML booleans.

## Firing order

Within a scope, builtin files run first (the bare `<event>` file before its
`<event>.d/*` entries, sorted lexically), then inline commands in declared
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
`WTM_WORKTREES_DIR`, `WTM_GIT_COMMON_DIR`, `WTM_WORKSPACE`, `WTM_WORKSPACE_DIR`
(what each holds: [shell-integration.md](shell-integration.md#the-wtm_-variables)).

`WTM_WORKSPACE` is the active workspace name (empty when the project is not
under a declared workspace); `WTM_WORKSPACE_DIR` is its path (the projects
root). The `pre-rename` event adds `WTM_NEW_DIR`/`WTM_NEW_BRANCH` (the
destination); the `post-rename` event adds `WTM_OLD_DIR`/`WTM_OLD_BRANCH` (the
pre-rename path and branch; equal to the new values when unchanged), so a hook
can reconcile path-keyed state, e.g.
`sesh rename "$WTM_OLD_DIR" "$WTM_WORKTREE_DIR"`.

Failures are logged but non-fatal unless the operation is strict — the CLI
`--strict` flag, or a ladder-resolved `[hooks] strict = true` (see
[Fire-time policy](#fire-time-policy)) — under which *any* failing hook raises
and aborts the command (so a failing `pre-*` hook vetoes its operation before
the state change). A builtin file that exists but is not executable is skipped
with a warning — fatal under strict. `wtm` does not track or ignore
`$WTM_WORKTREE_DIR/.wtm/` — whether per-worktree hooks are committed to the
branch is left to you.

## Managing hooks

```
install FILE|DIR [TARGET] [-s SCOPE] [-e EVENT] [-l] [-f]  install a hook into a scope
run     [TARGET...] HOOK [-s SCOPE] [--strict]             run an event's hooks (all scopes by default)
delete  [TARGET...] HOOK [-s SCOPE] [-r]                   delete a hook (event, or event/entry)
list    [TARGET] [--types]                                 list installed hooks, grouped by event across scopes
```

`HOOK` (the event) is the **last** positional for `run` and `delete`; anything
before it targets the worktree.

- `wtm hook install FILE|DIR [TARGET] [--scope system|user|workspace|project|worktree] [--event E] [--link] [--force]`
  installs a hook (default scope `project`). The event is inferred from a
  `<event>` / `<event>.d` source name, or given with `--event`. A
  **file** joins an existing `<event>.d/` (keeping its basename) or otherwise
  becomes the bare `<event>` file; a **directory**'s contents are merged into
  `<event>.d/`. Copies are made executable (0755); `--link` symlinks the
  source instead. Existing destinations require `--force`. `--scope system`,
  `--scope user`, and `--scope workspace` need no target.
- `wtm hook run [TARGET] HOOK [--scope …] [--strict]` runs an event's hooks (all
  scopes unless `--scope` restricts it). `--strict` forces failures to be fatal
  on top of the config ladder. `HOOK` (the event) is the last
  positional; everything before it is a `TARGET`, which — like `hook list`'s — is
  a **locator** pinned to the worktree tier.
- `wtm hook delete [TARGET] HOOK [--scope …] [-r]` removes a hook. `HOOK` is the
  last positional: an event (removes the bare `<event>` file; its
  `<event>.d/` only with `-r/--recursive`) or `event/entry` (removes one
  `.d` entry).
- `wtm hook list [TARGET] [--types]` lists hooks grouped by event across scopes,
  builtin files and inline config hooks alike (inline ones marked `(config)`);
  `--types` prints a compact per-event summary. Being read-only over an existing
  worktree, its `TARGET` is a **locator** pinned to the worktree tier (see
  [targeting.md](targeting.md#locators)): a bare name reaches another
  workspace's worktree, and a name matching none is an error. `hook install`
  writes into a `--scope` and `hook delete` is destructive, so those two keep the
  positional target grammar.
- `wtm hook set EVENT "CMD" [--scope …]` / `wtm hook unset EVENT [--scope …]`
  write an inline hook (`[...hooks.on] EVENT = "CMD"`) at the cwd-inferred
  entity's table-path; `--scope` defaults to the deepest existing dir. A `set`
  replaces the command in that one file; commands still accumulate across scopes
  at read time. `hook install`/`delete` manage builtin files; `hook set`/`unset`
  manage inline commands.

## When a hook doesn't fire

Worked through in order, this finds most causes:

- **Disabled up the ladder** — `[hooks] enabled = false` at any scope on the
  fired ladder kills the whole operation;
  `wtm config get hooks.enabled` reports what firing would resolve.
- **Executable bit** — a builtin file without one is skipped with a warning
  (`chmod +x`). Inline commands don't need it.
- **Event name** — must be exactly one of the fourteen; `wtm hook list` shows
  what exists, and `wtm hook install` infers the event from the source name.
- **Wrong scope's directory** — firing resolves each scope's dir through the
  same authority as management ([above](#scopes-and-discovery)), so a
  `[hooks] dir` override moves both.
- **Master switch** — is `WTM_NO_HOOKS` set?
- **Ran but failed quietly** — non-strict failures print a warning and
  continue; rerun with `wtm hook run … --strict` to make them fatal and see the
  exit codes.
- **`post-config` specifically** — hand edits never fire it, and neither does a
  write made from inside another hook (the `WTM_IN_HOOK` sentinel).
