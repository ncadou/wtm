# Targeting and Locators

Commands use two ways to identify a target:

- A **target grammar** decodes positionals into a path using the active
  workspace. Destructive commands use this form to avoid surprising global
  actions.
- A **locator** resolves an existing worktree, project, or workspace by name
  across every declared workspace. It is useful for commands such as
  `wtm enter feature-x` from any directory.

## Target grammar

The target grammar is count-based. `WORKSPACE`, `PROJECT`, and `NAME` share one
flat namespace, so a two-token target is always `PROJECT NAME`, never
`WORKSPACE PROJECT`.

| Positionals | Resolves to |
|---|---|
| `WORKSPACE PROJECT NAME` | `<WORKSPACE>/PROJECT/worktrees/NAME` |
| `PROJECT NAME` | `<active-workspace>/PROJECT/worktrees/NAME` |
| `NAME` | `<cwd-project>/worktrees/NAME` |
| `DIR` | An explicit path when the single argument starts with `/`, `./`, or `../`, or is `.` or `..` |
| No argument | The current directory where the command permits a cwd default |

`NAME` may be nested, for example `feature/x`. `delete` requires an explicit
target. `delete`, `rename`, `hook install`, and `hook delete` use this grammar;
they do not resolve a bare target globally.

Project commands whose only qualifier is a workspace, such as
`project new`, `project clone`, `project resolve`, `project set`, and
`project show`, also use positional grammar. A workspace is already an exact
registry key, so a locator provides no additional reach.

## Locators

A locator is a free name query matched across every declared workspace. The
last token is the entity name; leading tokens qualify its ancestors. Resolution
tries the deepest tier first and falls back only when that tier has no match:

1. A worktree named by the last token, qualified by a project or workspace.
2. A project named by the last token, optionally qualified by a workspace.
3. A workspace named by the last token, with no qualifier.

Exactly one match is used. Several matches produce qualified choices and a
non-zero exit. No match is an error. Examples:

```sh
wtm enter mywt             # worktree, from any workspace
wtm enter project mywt    # worktree qualified by project
wtm enter ws project mywt # fully qualified worktree
wtm enter project         # project
wtm enter ws              # workspace
```

The locator is used by `enter`, `leave`, `resolve`, `list`, `project enter`,
`project leave`, `hook list`, `hook run`, and `config show`. The generic config
commands also accept an optional locator before their key:

```sh
wtm config get project port
wtm config set ws project ci.runner.image ubuntu
```

`enter`, `leave`, and `resolve` also accept no argument: the cwd itself then
rides the locator as a path, so the nearest upward entity answers (worktree >
project > workspace). `hook list` and `hook run` pin their no-arg cwd to the
worktree tier.

## Paths

A locator also accepts a single **path** instead of a name query: anything that
starts with `/`, `~`, `./`, or `../`, or is `.` or `..`. A token containing `/`
but without such a prefix is still read as a name query first (nested worktree
names like `feature/x` are names); only when it matches nothing is it retried as
a cwd-relative path.

The path must exist and lie under a declared workspace; the deepest entity whose
directory contains it answers, so *any depth* truncates to the most local,
tier-appropriate entity:

```sh
wtm enter ~/ws/api/worktrees/main/src   # enters 'main'
wtm list ./api/worktrees/feat           # lists project 'api'
wtm enter .                             # standing in a workspace root: workspace
```

Truncation only climbs to the command's pin tier. A path that stops at a
shallower tier is an error (`hook list <project-dir>` reports that the path
names a project, not a worktree), and a path outside every declared workspace
is rejected -- unmanaged trees are out of scope by design.

Shell completion mirrors this: a partial-path token completes one directory
level at a time, offering only the dirs whose subtree still leads to an entity
admissible under the command's pin. Nothing is offered past the pin, so
completion never steers into a truncation.

## Leaf-tier pins

Some commands accept only one kind of locator result:

| Commands | Leaf tier |
|---|---|
| `enter`, `leave`, `resolve` | Worktree, project, or workspace |
| `list`, `project enter`, `project leave` | Project |
| `hook list`, `hook run` | Worktree |

A pin narrows which tier can answer. It does not change ambiguity handling.
For example, `project enter NAME` never resolves a same-named worktree.

## Qualifier-only locators

`create` and `worktree set` name a worktree that may not exist. They resolve
only the leading project qualifier and keep the trailing `NAME` literal:

```sh
wtm create ws project feature-x
wtm worktree set project feature-x -d "Login work"
```

With fewer than two positionals there is no qualifier. A qualifier that names
no project is treated literally, so creating a wholly new project remains
possible. Only an ambiguous qualifier produces an error.

## Ambiguity and current context

Before reporting an ambiguity, a locator narrows tied candidates using the
active workspace and the project inferred from the cwd. It first tries the
project context, then the workspace context. This can turn an ambiguity into a
unique match, but never changes an already unique result.
