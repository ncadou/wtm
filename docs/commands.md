# Commands

Every command runs against one **active workspace**, resolved in this order:

```
leading WORKSPACE positional  >  $WTM_WORKSPACE  >  cwd (longest path match)
                              >  default_workspace  >  the sole declared workspace  >  error
```

There is no built-in default and no `-w` flag: with no workspace declared or
selected, `wtm` errors and asks you to declare or select one. To reach another
workspace, name it as the leading positional (`wtm delete WORKSPACE PROJECT
NAME`); `$WTM_WORKSPACE` pins it for a session.

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
                          edit a workspace (empty value clears a field); -p rewrites the registry
                          entry, -d/-b write <ws>/.wtm/config.toml if the dir exists else the entry
rename  OLD NEW           rename the registry key (does not move the directory)
delete  NAME             unregister a workspace (user scope; never touches the projects root)
default [NAME] [--clear] get/set/clear default_workspace
enter   [NAME]           cd into the projects root, firing workspace-enter hooks
leave   [NAME] [-C]      fire workspace-leave hooks (cascading the worktree leave when inside one); cd out unless -C
```

Workspaces are *declared* in config. `wtm` reads config from every scope but
*writes* only the user file (its `[workspaces.*]` registry and the top-level
`default_workspace` / `base_branch` keys) and the entity-local `.wtm/config.toml`
files — via `add`/`set`/`rename`/`delete`/`default` (and `wtm project set`,
`wtm config base-branch`), using a comment-preserving `tomlkit` round-trip with
an atomic replace. System config stays hand-edited (those verbs refuse
system-only names for registry writes).

## Projects (`wtm project …`)

```
new     [WORKSPACE] NAME          create an empty project (.git/ + worktrees/)
clone   URL [[WORKSPACE] NAME]    clone into a new project (bare); alias: wtm clone
list    [WORKSPACE] [-t|--tree] [-w|--worktrees] [-j|--json] [-a|--all]
                                  list a workspace's projects (WORKSPACE, or the active one; --tree: with their worktrees;
                                  --worktrees: flat one-worktree-per-line, tab-separated
                                  project/name/path/branch/head; --json: those rows as JSON;
                                  --all: span every declared workspace, prepending a workspace column)
import  [SOURCE|PROJECT] [opts]   fold a plain clone into the bare-repo layout; alias: wtm import
resolve [[WORKSPACE] PROJECT] [-e|--env]   print a project's directory (--env: emit the project-level WTM_* contract)
enter   [[WORKSPACE] PROJECT]     cd into the project dir, firing project-enter hooks (PROJECT is a project-pinned locator)
leave   [[WORKSPACE] PROJECT] [-C]   fire project-leave hooks (cascading the worktree leave when inside one); cd out unless -C
set     [[WORKSPACE] PROJECT] [-b BASE] [-d DESC]   annotate a project (empty value clears);
                                  writes <project>/.wtm/config.toml if the dir exists else the user-file annotation
show    [[WORKSPACE] PROJECT]     detail (dir, description, base_branch + provenance, worktree count)
```

Projects are *discovered* on disk (a `<name>/.git`), never declared; config
only *annotates* them (description, base branch, hooks). See
[import.md](import.md) for `wtm import`.

`wtm project resolve [[WORKSPACE] PROJECT]` prints the project directory
(`<workspace>/PROJECT`), inferring PROJECT from the cwd when omitted — handy for
`cd "$(wtm project resolve alpha)"`. It is pure path math (no git, no existence
check, so it works for an absent project). With `--env` it emits only the
project-level slice of the contract (`WTM_PROJECT`, `WTM_PROJECT_DIR`,
`WTM_WORKTREES_DIR`, `WTM_GIT_COMMON_DIR`, `WTM_WORKSPACE`, `WTM_WORKSPACE_DIR`)
— there is no `WTM_WORKTREE_DIR` or `WTM_BRANCH`, since a project has no single
worktree.

`wtm project enter [[WORKSPACE] PROJECT]` cds into the project dir (beside its
`.git`/`worktrees`, PROJECT inferred from the cwd when omitted), fires
`project-enter` hooks, and exports that same project-level contract — the middle
tier between `workspace enter` (the projects root) and `worktree enter` (a
checkout). Exporting `WTM_WORKSPACE` pins the active workspace for the session
(the two worktree vars are unset). `wtm project leave [[WORKSPACE] PROJECT] [-C]`
fires `project-leave` and cds out (to `$OLDPWD` if outside the project tree, else
the projects root when it cascaded out of a worktree, else stays put); `-C`
suppresses the cd but still ends the session. Standing inside one of the
project's worktrees, `leave` cascades the worktree's `leave` first
(worktree→project teardown order). The project must already exist on disk
(`<project>/.git` must be present).

Both take their PROJECT as a **locator pinned to the project tier** (see
[the locator](#the-locator)): a bare name is matched across *every* declared
workspace, so `wtm project enter otherproj` reaches another workspace's project,
while the pin keeps a same-named *worktree* out of the running. `wtm project
resolve` / `set` / `show` keep the positional target grammar — their only
qualifier is a WORKSPACE, an exact registry key, so a locator would add nothing.

## Worktrees (`wtm worktree …`, each also a top-level alias)
```
create  [[WORKSPACE] PROJECT] NAME [-b BRANCH] [--base REF] [-d DIR] [-c] [-i]  add a worktree (+branch); -c to cd, -i to run install hooks
enter   (LOCATOR | DIR) [-i]                           cd into a worktree/project/workspace by name
leave   (LOCATOR | DIR) [-C]                          run leave hooks for what the locator names; cds out unless -C (non-destructive)
delete  (WORKSPACE PROJECT NAME | PROJECT NAME | DIR) [-f] [-D]   remove a worktree (-f force, -D delete branch too)
rename  ([[WORKSPACE] PROJECT] [OLDNAME] | DIR) NEWNAME [-b]      rename/move a worktree; -b renames its branch too
list    [[WORKSPACE] PROJECT]                         list a project's worktrees (project-pinned locator; inferred from cwd if omitted)
resolve [[[WORKSPACE] PROJECT] NAME]                  print a worktree's path (no arg: the cwd worktree; --env: emit WTM_* for eval)
set     [[[WORKSPACE] PROJECT] NAME] [-d DESC]        annotate a worktree (subcommand only, no top-level alias)
```

`create`, `resolve`, and `worktree set` name a NAME that need not
exist, so they take a [qualifier-only locator](#qualifier-only-locator): the
trailing NAME stays literal while its leading PROJECT is resolved globally.

## Config (`wtm config …`)

```
show [--origin] [--hooks]     the effective merged config for the current context: all workspaces, plus the
                              cwd-bound project (and its worktree, if cwd is in one) with their resolved
                              values (--origin: the tier each value resolves from + inert-annotation flags;
                              --hooks: accumulated inline hooks per scope)
show --all | --workspace |    dump a full subtree rooted at the chosen entity (TARGET per the level's grammar,
     --project | --worktree   cwd-inferred when omitted), descending every project and worktree below it
     [TARGET] [-j/--json]     (on-disk unioned with declared); effective-values-only (no --origin/--hooks);
                              -j/--json emits a nested JSON tree (also valid on the default view)
base-branch [VALUE] [--clear] get/set/clear the global default base_branch (user config); the getter
                              shows the effective value and the scope it comes from
```

`--worktree`'s TARGET takes a [qualifier-only locator](#qualifier-only-locator)
(the dumped NAME may be declared-but-absent, so only its PROJECT is resolved);
the other levels are named positionally.

See [configuration.md](configuration.md).

## Targeting a worktree

For `delete` / `rename` / `hook install` / `hook delete`, the positionals decode
into a target. A leading `WORKSPACE` reaches another workspace; without it the
active workspace is used. Because `WORKSPACE`, `PROJECT`, and `NAME` share one
flat namespace, the decode is count-based — a two-token target is always
`PROJECT NAME`, so a non-cwd workspace needs the full triple:

- `WORKSPACE PROJECT NAME` → `<WORKSPACE>/PROJECT/worktrees/NAME`
- `PROJECT NAME` → `<active-workspace>/PROJECT/worktrees/NAME`
- `NAME` → `<cwd-project>/worktrees/NAME` (project inferred from the current
  directory; `NAME` may be nested, e.g. `feature/x`)
- `DIR` → an explicit path (a single arg is a path only if it starts with `/`,
  `./`, `../`, or is `.`/`..`)
- no argument → the current directory (`delete` requires an explicit target)

`project new` / `clone` / `resolve` / `set` / `show` use the same grammar, scoped
to what they accept. The commands that name something which must already *exist*
take a **locator** instead, and those that invent only the trailing NAME take a
**qualifier-only locator** (see below).

## The locator

`enter`, `leave`, `list`, `project enter`, `project leave`, `hook list`, and
`hook run` resolve their positionals as a **locator**: a free, scope-inferring
name query matched across *every* declared workspace, so the same token works no
matter which workspace is active (a recalled `wtm enter mywt` finds its worktree
from anywhere). The last token is the entity you land on; leading tokens are
ancestor qualifiers in top-down order. Resolution is a leaf-tier cascade — try the
deepest tier first, fall back only on a miss:

- worktree named the last token (ancestors matched as a project *or* a
  workspace); else
- project named the last token (a leading token is its workspace); else
- workspace named the last token (single token only).

Exactly one match is used, with that tier's `WTM_*` contract; several print the
fully-qualified choices for the command you ran and exit non-zero; none is an
error. Examples: `enter mywt` (any workspace), `enter proj mywt` /
`enter ws mywt` (qualified), `enter ws proj mywt` (fully qualified),
`enter proj` (a project), `enter ws` (a workspace). Where a locator-taking
command also accepts a path, a single `DIR` (`/`, `./`, `../`, `.`/`..`) or no
argument (the cwd) keeps the classic behavior — those are not names to resolve.
Shell completion offers the name universe the command can reach, labelled by kind
with each candidate's location context.

### Leaf-tier pin

`enter` and `leave` are tier-polymorphic and run the full cascade. The others
accept one kind of target only, so they **pin** the leaf tier: `list`,
`project enter`, and `project leave` pin it to a project (a same-named worktree is
never resolved, and completion offers projects rather than every name in the
tree); `hook list` and `hook run` pin it to a worktree. A pin narrows *which tier
answers*, never how an ambiguity within it is reported.

### Qualifier-only locator

`create`, `resolve`, `worktree set`, and `config show --worktree` all
name a worktree that need not exist, so the leaf is invisible to the index. They
take a locator over the *leading* tokens only, keeping the trailing NAME literal —
so `wtm create otherproj feat` creates in another workspace's project, and
`wtm resolve otherproj newwt` prints its path, with no workspace spelled out. The
qualifiers go in as one project-pinned query, so `PROJECT` and `WORKSPACE PROJECT`
both resolve through the ordinary cascade and the 3-token form keeps its meaning.

It is strictly additive — no invocation that worked before changes meaning:

- fewer than two positionals carry no qualifier at all (a `DIR`, a lone NAME
  against the cwd project, or the cwd default) and pass straight through;
- a qualifier that names **no** project in any workspace is taken literally, so
  these commands still name a wholly absent target (`wtm resolve newproj newwt`
  stays pure path math).

Only an *ambiguous* qualifier errors; its printed choices carry the trailing NAME,
so each line stays runnable exactly as typed.

### What does *not* take a locator, and why

A locator reaches globally, which fences it off from two classes of command:

- **Destructive verbs.** `delete`, `rename`, and `hook delete` stay bounded by
  the cwd: reaching into another project or workspace must be spelled out
  positionally, so a bare `wtm delete main` can never resolve into a workspace
  you are not standing in. The dividing line is irreversibility, not mutation —
  `enter`, `hook run`, and `create` all mutate or fire hooks and still reach
  globally. `hook install` likewise keeps the grammar: it writes into a `--scope`
  rather than pointing at the worktree.
- **Nothing to gain.** `project new`, `project clone`, `project resolve`,
  `project set`, `project show`, and `config show --workspace`/`--project` have
  only a WORKSPACE as a qualifier, which is an exact registry key already.

### Current-context tiebreak

Before reporting an ambiguity, a locator narrows the tied candidates by the
current context — the active workspace and, from the cwd, its project. If exactly
one candidate matches (project first, relaxing to the workspace alone), it
resolves silently; otherwise the tie stands and prints as before. The tiebreak can
only turn a former error into a hit; it never touches an already-unique
resolution.

## Per-command notes

### `create`'s branch resolution

`create` resolves a branch as: existing local branch → use it; `origin/NAME`
exists → track it; otherwise create a new branch off a base ref (`--base/--off
REF` > the project's configured `base_branch` > `origin/HEAD` > `HEAD`). A
configured/requested base is resolved as `origin/REF` then local `REF`; in a
populated repo a base that resolves to nothing is a hard error. If the
repository has no commits yet (e.g. a freshly-created project), the worktree is
bootstrapped on a new orphan branch regardless of any configured base.

### `resolve` (read-only path query)

`resolve` is a read-only path query. Given `[[WORKSPACE] PROJECT] NAME` (no
`DIR`) it prints the computed worktree path without touching git, so it works
for not-yet-created targets — handy for `cd "$(wtm resolve PROJECT NAME)"`. The
NAME is literal and its leading PROJECT is a
[qualifier-only locator](#qualifier-only-locator), so `wtm resolve otherproj wt`
reaches another workspace while an unknown PROJECT still resolves by path math.
Given no
argument it resolves the worktree you are standing in (inspecting git for its
toplevel), so a bare `wtm resolve` prints the current worktree's root; run
outside any worktree it errors, asking for a NAME. With `--env` it instead emits
the `WTM_*` contract as `export KEY='value'` lines for
`eval "$(wtm resolve --env …)"`; that mode always inspects git, so the worktree
must already exist.

### `rename` (right-to-left positionals)

`rename` moves a worktree to a new name. Its positionals are parsed
right-to-left: the **last** positional is `NEWNAME`, and everything before it is
an ordinary source target (`WORKSPACE PROJECT NAME | PROJECT NAME | NAME | DIR |
cwd`) — so `wtm rename NEWNAME` renames the worktree you are standing in,
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
