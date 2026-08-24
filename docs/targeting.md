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

`enter` and `leave` also accept a single directory or no argument for the cwd
worktree. `hook list` and `hook run` accept a directory or cwd worktree too.
`resolve` accepts names only; no argument means the cwd worktree.

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
