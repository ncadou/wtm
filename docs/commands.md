# Commands

Every command runs against one **active workspace**, resolved in this order:

```
-w/--workspace NAME  >  $WTM_WORKSPACE  >  cwd (longest path match)
                     >  default_workspace  >  the sole declared workspace  >  error
```

There is no built-in default: with no workspace declared or selected, `wtm`
errors and asks you to declare or select one. A global `-w/--workspace NAME`
(placed *before* the subcommand) selects the active workspace for that
invocation.

Commands are grouped under `workspace`, `project`, `worktree`, `hook`, and
`config`. Each worktree-level command also has a top-level alias
(`wtm create …` == `wtm worktree create …`), and `wtm clone …` / `wtm import …`
alias the matching `project` commands. The two forms are equivalent.

Run `wtm --help` (or `wtm COMMAND --help`) for the authoritative, always-current
usage.

## Workspaces (`wtm workspace …`)

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

## Projects (`wtm project …`)

```
new     NAME                      create an empty project (.git/ + worktrees/)
clone   URL [NAME]                clone into a new project (bare); alias: wtm clone
list    [-t|--tree] [-w|--worktrees] [-j|--json] [-a|--all]
                                  list projects in the active workspace (--tree: with their worktrees;
                                  --worktrees: flat one-worktree-per-line, tab-separated
                                  project/name/path/branch/head; --json: those rows as JSON;
                                  --all: span every declared workspace, prepending a workspace column)
import  [SOURCE|PROJECT] [opts]   fold a plain clone into the bare-repo layout; alias: wtm import
resolve [PROJECT] [-e|--env]      print a project's directory (--env: emit the project-level WTM_* contract)
```

Projects are *discovered* on disk (a `<name>/.git`), never declared; config
only *annotates* them (description, base branch, hooks). See
[import.md](import.md) for `wtm import`.

`wtm project resolve [PROJECT]` prints the project directory
(`<workspace>/PROJECT`), inferring PROJECT from the cwd when omitted — handy for
`cd "$(wtm project resolve alpha)"`. It is pure path math (no git, no existence
check, so it works for an absent project). With `--env` it emits only the
project-level slice of the contract (`WTM_PROJECT`, `WTM_PROJECT_DIR`,
`WTM_WORKTREES_DIR`, `WTM_GIT_COMMON_DIR`, `WTM_WORKSPACE`, `WTM_WORKSPACE_DIR`)
— there is no `WTM_WORKTREE_DIR` or `WTM_BRANCH`, since a project has no single
worktree.

## Worktrees (`wtm worktree …`, each also a top-level alias)

```
create  [PROJECT] NAME [-b BRANCH] [--base REF] [-d DIR] [-c] [-i]  add a worktree (+branch); -c to cd, -i to run install hooks
install ([PROJECT] NAME | DIR)                        run install hooks for a worktree (alias for 'wtm hook run install')
enter   ([PROJECT] NAME | DIR) [-c] [-b BRANCH] [--base REF] [-i]   cd into a worktree; -c creates it first if absent
leave   ([PROJECT] NAME | DIR) [-C]                   run leave hooks for a worktree; cds out unless -C (non-destructive)
delete  (PROJECT NAME | DIR) [-f] [-D]                remove a worktree (-f force, -D delete branch too)
rename  ([PROJECT] [OLDNAME] | DIR) NEWNAME [-b]      rename/move a worktree; -b renames its branch too
list    [PROJECT]                                     list a project's worktrees (project inferred from cwd if omitted)
resolve [[PROJECT] NAME]                              print a worktree's path (no arg: the cwd worktree; --env: emit WTM_* for eval)
```

## Config (`wtm config …`)

```
show [--sources]          the effective merged config (--sources: declaring scope + inert-annotation flags)
```

See [configuration.md](configuration.md).

## Targeting a worktree

For `enter` / `leave` / `delete` / `rename` / `hook run` / `hook install` /
`hook delete`, the positionals decode into a target within the active workspace:

- `PROJECT NAME` → `<active-workspace>/PROJECT/worktrees/NAME`
- `NAME` → `<cwd-project>/worktrees/NAME` (project inferred from the current
  directory; `NAME` may be nested, e.g. `feature/x`)
- `DIR` → an explicit path (a single arg is a path only if it starts with `/`,
  `./`, `../`, or is `.`/`..`)
- no argument → the current directory (`delete` requires an explicit target)

### `create`'s branch resolution

`create` resolves a branch as: existing local branch → use it; `origin/NAME`
exists → track it; otherwise create a new branch off a base ref (`--base/--off
REF` > the project's configured `base_branch` > `origin/HEAD` > `HEAD`). A
configured/requested base is resolved as `origin/REF` then local `REF`; in a
populated repo a base that resolves to nothing is a hard error. If the
repository has no commits yet (e.g. a freshly-created project), the worktree is
bootstrapped on a new orphan branch regardless of any configured base.

### `enter -c` (idempotent enter)

`enter -c` makes entering idempotent: if the target worktree is absent it is
created first (with `create`'s branch resolution, `-b BRANCH` to override the
branch), then entered; if it already exists `enter` just cds in. With `-c` the
target is restricted to `PROJECT NAME` or `NAME` (no `DIR`, no cwd default), and
`-b` outside `-c` (or when the worktree already exists) is rejected/ignored.

### `resolve` (read-only path query)

`resolve` is a read-only path query. Given `PROJECT NAME` or `NAME` (no `DIR`)
it prints the computed worktree path without touching git, so it works for
not-yet-created targets — handy for `cd "$(wtm resolve PROJECT NAME)"`. Given no
argument it resolves the worktree you are standing in (inspecting git for its
toplevel), so a bare `wtm resolve` prints the current worktree's root; run
outside any worktree it errors, asking for a NAME. With `--env` it instead emits
the `WTM_*` contract as `export KEY='value'` lines for
`eval "$(wtm resolve --env …)"`; that mode always inspects git, so the worktree
must already exist.

### `rename` (right-to-left positionals)

`rename` moves a worktree to a new name. Its positionals are parsed
right-to-left: the **last** positional is `NEWNAME`, and everything before it is
an ordinary source target (`PROJECT NAME | NAME | DIR | cwd`) — so
`wtm rename NEWNAME` renames the worktree you are standing in,
`wtm rename OLDNAME NEWNAME` names the source within the cwd project, and
`wtm rename PROJECT OLDNAME NEWNAME` is fully explicit. `NEWNAME` is validated
like `create`'s NAME (nesting allowed, `..` rejected, not a path) and always
lands at `<project>/worktrees/NEWNAME` in the **same** project. The directory is
moved with `git worktree move` (a *locked* worktree must be `git worktree
unlock`'d first). `-b/--branch` additionally renames the branch to `NEWNAME`
(hard error on a detached HEAD; a no-op when it already matches). A destination
that already exists, or a branch-name collision, is a hard error (never forced).
If you rename the worktree you are standing in, your shell follows the move to
the corresponding subpath under the new location.
