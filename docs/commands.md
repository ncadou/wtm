# Commands

This page is a quick reference for `wtm` commands. For the rules that decide
what a positional name means, see [Targeting and locators](targeting.md).

Every command runs against one **active workspace**, resolved in this order:

```
leading WORKSPACE positional  >  $WTM_WORKSPACE  >  cwd (longest path match)
                               >  default-workspace  >  the sole declared workspace  >  error
```

There is no built-in default and no `-w` flag. With no workspace declared or
selected, `wtm` reports an error. `$WTM_WORKSPACE` pins the workspace for a
shell session.

Commands that directly run lifecycle hooks accept `--strict`. With this flag,
hook failures are fatal instead of warnings. The `post-config` broadcast is an
exception: it is triggered after config writes, but its failures remain
warnings.

Run `wtm --help` or `wtm COMMAND --help` for the authoritative, always-current
usage.

## Shell integration

| Command or option | Purpose |
|---|---|
| `shell-init` | Print the shell wrapper. Use it with `eval "$(wtm shell-init)"` in a shell startup file. |
| `--install-completion` | Install shell completion for the current shell. |
| `--show-completion` | Print shell completion code for inspection or manual installation. |

## Worktrees (`wtm worktree ...`)

The following commands also have top-level aliases, except `set`.

| Command | Purpose |
|---|---|
| `create [[WORKSPACE] PROJECT] NAME` | Create a worktree and branch. Use `-b/--branch`, `--base/--off`, `-d/--dir`, `-c/--cd`, `-i/--install`, `-f/--fetch`, or `--strict`. |
| `enter [TARGET]` | Enter a worktree, project, or workspace. Use `-i/--install` or `--strict`. Names use a [locator](targeting.md#locators); a directory is also accepted. |
| `leave [TARGET]` | Run leave hooks and leave the target. Use `-C/--no-cd` to stay in place or `--strict`. |
| `delete WORKSPACE PROJECT NAME \| PROJECT NAME \| DIR` | Delete a worktree. The target is required. Use `-f/--force`, `-D/--delete-branch`, or `--strict`. |
| `rename [[WORKSPACE] PROJECT] [OLDNAME] NEWNAME` | Move a worktree. Use `-b/--branch` to rename its branch too, or `--strict`. Positionals are parsed right-to-left. |
| `list [[WORKSPACE] PROJECT]` | List a project's worktrees. The project is inferred from the cwd or resolved with a project-pinned locator. |
| `resolve [LOCATOR]` | Print a worktree, project, or workspace path. Use `-e/--env` for the corresponding `WTM_*` contract. No argument resolves the cwd worktree. |
| `set [[[WORKSPACE] PROJECT] NAME]` | Set a worktree description. Use `-d/--description` and `-s/--scope`. This command has no top-level alias. |

`create` and `worktree set` can name a worktree that does not exist yet. Their
leading project qualifier uses a [qualifier-only locator](targeting.md#qualifier-only-locators).

## Projects (`wtm project ...`)

| Command | Purpose |
|---|---|
| `new [WORKSPACE] NAME` | Create an empty project with `.git/` and `worktrees/`. |
| `clone URL [[WORKSPACE] NAME]` | Clone a repository into a new bare project. Alias: `wtm clone`. |
| `import [SOURCE\|PROJECT]` | Fold a plain clone into the bare-repository layout. Alias: `wtm import`. See [Importing a repository](import.md). |
| `list [WORKSPACE]` | List projects in a workspace. Use `-t/--tree`, `-w/--worktrees`, `-j/--json` with `--worktrees`, or `-a/--all`. |
| `resolve [[WORKSPACE] PROJECT]` | Print a project directory. Use `-e/--env` for the project-level contract. The path can be computed before the project exists. |
| `enter [[WORKSPACE] PROJECT]` | Enter a project directory and fire `project-enter` hooks. The target is a project-pinned locator. Use `--strict`. |
| `leave [[WORKSPACE] PROJECT]` | Fire `project-leave` hooks and leave the project. Use `-C/--no-cd` or `--strict`. The target is a project-pinned locator. |
| `set [[WORKSPACE] PROJECT]` | Set a project description or base branch. Use `-d/--description`, `-b/--base-branch`, and `-s/--scope`. |
| `show [[WORKSPACE] PROJECT]` | Show project path, description, effective base branch and provenance, and worktree count. |

Projects are discovered on disk (`<workspace>/<project>/.git`). Config only
annotates them. See [configuration](configuration.md) for write placement.

There is deliberately no removal command for projects: worktrees are removed
with `wtm delete`, but an empty project root is removed by hand
(`git worktree remove ...` first, then delete `<workspace>/<project>/`).

## Workspaces (`wtm workspace ...`)

| Command | Purpose |
|---|---|
| `list` | List declared workspaces, paths, active marker, and descriptions. |
| `resolve [NAME]` | Print a workspace path. Use `-e/--env` for `WTM_WORKSPACE` and `WTM_WORKSPACE_DIR`. |
| `show [NAME]` | Show workspace path, description, base branch, scope, and project count. |
| `add NAME [PATH]` | Add a workspace declaration to the user config. Use `-d/--description`, `-b/--base-branch`, or `-c/--create`. |
| `set NAME` | Edit a workspace. Use `-p/--path`, `-d/--description`, `-b/--base-branch`, and `-s/--scope`. |
| `rename OLD NEW` | Rename a workspace registry key without moving its directory. |
| `remove NAME` | Remove a workspace's user-config declaration without touching its projects root. |
| `default [NAME]` | Get or set the default workspace. Use `--clear` to remove it. |
| `enter [NAME]` | Enter a workspace's projects root and fire `workspace-enter` hooks. Use `--strict`. |
| `leave [NAME]` | Fire `workspace-leave` hooks and leave the workspace. Use `-C/--no-cd` or `--strict`. |

Workspace declarations are user-file-only. Scalar annotations can be written
to another allowed scope with `-s/--scope`; see [configuration](configuration.md).

## Hooks (`wtm hook ...`)

| Command | Purpose |
|---|---|
| `install FILE\|DIR [TARGET]` | Install or link a builtin hook. Use `-s/--scope`, `-e/--event`, `-l/--link`, or `-f/--force`. |
| `run [TARGET] HOOK` | Run an event's hooks. Use `-s/--scope` to restrict the scope or `--strict`. |
| `list [TARGET]` | List installed hooks by event. Use `-t/--types` for a compact summary. |
| `set EVENT CMD` | Set an inline hook at the cwd-inferred entity. Use `-s/--scope`. |
| `unset EVENT` | Unset an inline hook at the cwd-inferred entity. Use `-s/--scope`. |
| `delete [TARGET] HOOK` | Delete an event hook or an event entry. Use `-s/--scope` or `-r/--recursive`. |

`run` and `list` use a worktree-pinned locator. `install` and `delete` write
builtin files; `set` and `unset` write inline config hooks. See
[Hooks](hooks.md#managing-hooks) for events, scopes, and firing order.

## Config (`wtm config ...`)

| Command | Purpose |
|---|---|
| `show [LOCATOR]` | Show effective config as a global-rooted tree. Use `-g/--global`, `-r/--recurse`, `-d/--depth`, `-o/--origin`, `--hooks`, or `-j/--json`. |
| `get [LOCATOR] KEY [SUBKEY]` | Read an arbitrary key. Use `-o/--origin`, `-j/--json`, or `-g/--global`. |
| `set [LOCATOR] KEY [SUBKEY] VALUE` | Write an arbitrary key. Use `-a/--array`, `-t/--table`, `-f/--floor`, `-x/--toml`, `--type`, `-s/--scope`, or `-g/--global`. |
| `unset [LOCATOR] KEY [SUBKEY] [VALUE]` | Unset a key, array element, or table cell. Use `-a/--array`, `-t/--table`, `-x/--toml`, `--type`, `-s/--scope`, or `-g/--global`. |
| `base-branch [VALUE]` | Get, set, or clear the global default base branch. Use `--clear`. |

Without a locator, `get`, `set`, and `unset` use the entity inferred from the
cwd. `-g/--global` targets the global tier instead. See
[Configuration](configuration.md) for sections, arrays, typed values, floors,
and examples.

## Plugins (`wtm plugin ...`)

| Command | Purpose |
|---|---|
| `add NAME` | Add (register) a plugin. Use `--seed` to write its declared defaults or `-s/--scope` to choose the activation scope. |
| `remove NAME` | Remove (unregister) a plugin by clearing its most-local activation cell. |
| `list` | List registered plugins and resolution status. Use `-j/--json`. |
| `show NAME` | Show a plugin manifest and its registration sites. |

See [Plugins](plugins.md) for manifest resolution, namespaces, and hook
ordering.

## Per-command notes

### `create`'s branch resolution

`create` resolves a branch as: existing local branch -> use it;
`origin/NAME` exists -> track it; otherwise create a new branch off a base ref
(`--base/--off REF` > the project's configured `base-branch` > `origin/HEAD` >
`HEAD`). A configured/requested base is resolved as `origin/REF` then local
`REF`; in a populated repo a base that resolves to nothing is a hard error. If
the repository has no commits yet, the worktree is bootstrapped on a new orphan
branch regardless of any configured base.

### `resolve` (read-only path query)

`resolve` is the read-only twin of `enter`: the same unpinned
[locator](targeting.md#locators) cascade resolves the target, and the resolved
tier's path is printed. With no argument it resolves the worktree containing
the cwd. With `--env` it emits `export KEY='value'` lines for
`eval "$(wtm resolve --env ...)"`.

### `rename` (right-to-left positionals)

`rename` moves a worktree to a new name. The last positional is `NEWNAME`; all
preceding positionals form the source target. `NEWNAME` allows nested names but
rejects `..`. The worktree is moved with `git worktree move`; a locked worktree
must be unlocked first. `-b/--branch` also renames the branch, with a hard
error on detached HEAD or a branch-name collision. If the current worktree is
renamed, the shell follows it to the new location.
