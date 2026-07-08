# wtm — git worktree manager

`wtm` manages git worktrees laid out under a single projects root:

```
$WTM_PROJECTS_ROOT/<project>/
    .git/                 bare repository
    worktrees/<name>/     worktrees
```

`wtm` only knows this structure; anything project-specific (shared data dirs,
direnv, tmux/sesh) belongs in hooks, not in `wtm`.

## Commands

Commands are grouped under `project`, `worktree`, and `hook`. Each
worktree-level command also has a top-level alias (`wtm create …` ==
`wtm worktree create …`), and `wtm clone …` / `wtm migrate …` alias the matching
`project` commands. The two forms are equivalent.

**Projects** (`wtm project …`):

```
new     NAME                      create an empty project (.git/ + worktrees/)
clone   URL [NAME]                clone into a new project (bare); alias: wtm clone
list    [-t|--tree]               list all projects (--tree: with their worktrees)
resolve [PROJECT] [-e]            print a project's dir (--env: emit project WTM_* for eval)
migrate [SOURCE|PROJECT] [opts]   fold a plain clone into the bare-repo layout; alias: wtm migrate
```

**Worktrees** (`wtm worktree …`, each also a top-level alias):

```
create  [PROJECT] NAME [-b BRANCH] [-d DIR] [-c] [-i]  add a worktree (+branch); -c to cd, -i to run install hooks
install ([PROJECT] NAME | DIR)                         run install hooks for a worktree (alias for 'wtm hook run install')
enter   ([PROJECT] NAME | DIR) [-c] [-b BRANCH] [-i]   cd into a worktree; -c creates it first if absent
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

- `PROJECT NAME` → `$WTM_PROJECTS_ROOT/PROJECT/worktrees/NAME`
- `NAME` → `<cwd-project>/worktrees/NAME` (project inferred from the current
  directory; `NAME` may be nested, e.g. `feature/x`)
- `DIR` → an explicit path (a single arg is a path only if it starts with `/`,
  `./`, `../`, or is `.`/`..`)
- no argument → the current directory (`delete` requires an explicit target)

`create` resolves a branch as: existing local branch → use it; `origin/NAME`
exists → track it; otherwise create a new branch off `origin/HEAD` (or `HEAD`
when there is no `origin/HEAD`). If the repository has no commits yet (e.g. a
freshly-created project), the worktree is bootstrapped on a new orphan branch.

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
project directory (`$WTM_PROJECTS_ROOT/PROJECT`), inferring PROJECT from the cwd
when omitted — `cd "$(wtm project resolve alpha)"`. It is pure path math (no git,
no existence check, so it works for an absent project). With `--env` it emits
only the project-level slice of the contract (`WTM_PROJECT`, `WTM_PROJECT_DIR`,
`WTM_WORKTREES_DIR`, `WTM_GIT_COMMON_DIR`, `WTM_PROJECTS_ROOT`) — there is no
`WTM_WORKTREE_DIR` or `WTM_BRANCH`, since a project has no single worktree.

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

`wtm` fires `create`, `install`, `enter`, `leave`, `delete`, and `rename`
events, discovered across five scopes:

- **system**    — `$WTM_SYSTEM_HOOKS_DIR/wtm-<event>` and `…/wtm-<event>.d/*`
- **user**      — `$WTM_USER_HOOKS_DIR/wtm-<event>` and `…/wtm-<event>.d/*`
- **workspace** — `$WTM_WORKSPACE_HOOKS_DIR/wtm-<event>` and `…/wtm-<event>.d/*`
- **project**   — `$WTM_PROJECT_DIR/.git/hooks/wtm-<event>` and `…/wtm-<event>.d/*`
- **worktree**  — `$WTM_WORKTREE_DIR/.wtm/hooks/wtm-<event>` and `…/wtm-<event>.d/*`

The system scope covers every user on the machine; its hooks dir defaults to
`/etc/wtm/hooks` (override with `$WTM_SYSTEM_HOOKS_DIR`). The workspace scope
covers every project under the projects root; its hooks dir defaults to
`$WTM_PROJECTS_ROOT/.wtm/hooks` (override with `$WTM_WORKSPACE_HOOKS_DIR`).

Within a scope the bare `wtm-<event>` file runs before its `wtm-<event>.d/*`
entries (sorted lexically). Setup events (`create`, `install`, `enter`,
`rename`) run most-general first: **system → user → workspace → project →
worktree**; teardown events (`leave`, `delete`) reverse to most-specific first:
**worktree → project → workspace → user → system**. The `delete` event fires *before* the worktree
is removed, so per-worktree teardown hooks under `$WTM_WORKTREE_DIR/.wtm/hooks` still
exist when they run; its working directory is therefore `$WTM_WORKTREE_DIR`. The `leave`
event likewise fires while the worktree still exists. The `rename` event fires
*after* the move, from the **new** worktree (its per-worktree hooks moved with
it), so its working directory is the new `$WTM_WORKTREE_DIR`.

The lifecycle events fire from: `create` on `wtm create`; `install` on
`wtm install`, `wtm create -i`, or `wtm enter -i`; `enter` whenever `wtm` cds you
into a worktree (`wtm enter`, and `wtm create -c`); `leave` on `wtm leave`;
`delete` on `wtm delete`; `rename` on `wtm rename`. Within a single
`wtm create -c -i` the order is `create` → `install` → `enter`; within
`wtm enter -i` it is `install` → `enter`.

Hooks receive the worktree directory as `$1` plus a `WTM_*` environment contract
(`WTM_EVENT`, `WTM_WORKTREE_DIR`, `WTM_BRANCH`, `WTM_PROJECT`, `WTM_PROJECT_DIR`,
`WTM_WORKTREES_DIR`, `WTM_GIT_COMMON_DIR`, `WTM_PROJECTS_ROOT`). The `rename`
event additionally sets `WTM_OLD_DIR` and `WTM_OLD_BRANCH` (the pre-rename path
and branch; equal to the new values when unchanged), so a hook can reconcile
path-keyed state, e.g. `sesh rename "$WTM_OLD_DIR" "$WTM_WORKTREE_DIR"`. Failures are
logged but non-fatal unless `--strict` is passed. `wtm` does not track or ignore
`$WTM_WORKTREE_DIR/.wtm/` — whether per-worktree hooks are committed to the branch is left
to you.

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
- `wtm hook list [TARGET] [--types]` lists installed hooks grouped by event
  across scopes; `--types` prints a compact per-event summary.

## Shell integration

`wtm` writes diagnostics to stderr and emits the target directory out-of-band so
the calling shell can `cd` into it. The `wtm` function in `../env` provides this
wrapper: it passes a `WTM_CD_FILE` temp file to the real binary and `cd`s into
whatever path the command writes there. This is what makes `wtm enter` and
`wtm create -c` change the current shell's directory.

`wtm delete` uses the same channel to rescue a stranded shell: when you delete
the worktree you are standing in (or a subdirectory of it), it `cd`s to
`$OLDPWD` if that still exists, otherwise to the project dir. Without the
wrapper it stays silent (it cannot move the parent shell anyway).

## Configuration

`wtm` reads these environment variables:

- `WTM_PROJECTS_ROOT` — projects root (default `~/workspace/projects`)
- `WTM_SYSTEM_HOOKS_DIR` — system hooks directory (default `/etc/wtm/hooks`)
- `WTM_USER_HOOKS_DIR` — user hooks directory (default `$XDG_CONFIG_HOME/wtm/hooks`)
- `WTM_WORKSPACE_HOOKS_DIR` — workspace hooks directory (default
  `$WTM_PROJECTS_ROOT/.wtm/hooks`)

## Development

`wtm` uses [`uv`](https://docs.astral.sh/uv/) for dependency management and
[Ruff](https://docs.astral.sh/ruff/) for both formatting and linting (import
sorting included). The `Makefile` wraps the common tasks:

- `make reformat` — apply safe autofixes (import sorting, pyupgrade rewrites,
  dead-import removal) and format the code (`ruff check --fix` + `ruff format`)
- `make lint` — lint (`ruff check`: pyflakes, pycodestyle, isort, pyupgrade,
  bugbear, comprehensions)
- `make check` — verify formatting + lint without writing (CI-style gate)
- `make test` — run the test suite (`pytest`)

The underlying commands are `uv run ruff check --fix src tests`, `uv run ruff
format src tests`, and `uv run pytest`. Ruff config lives in `pyproject.toml`
(`[tool.ruff]`): line length 99, target Python 3.10.
