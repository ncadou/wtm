# wtm

Domain language for `wtm`, a git worktree manager over one or more named
**workspaces**, each a projects-root layout (`<workspace>/<project>/.git` bare
repo + `worktrees/<name>/`).

## Language

**Workspace**:
A named projects root, *declared* in config (path + name + description +
hooks). Every command operates within one **active** workspace. Also the name
of the projects-root-wide hook/config scope.
_Avoid_: root (say "workspace path" or "projects root of the workspace")

**Active workspace**:
The workspace a command targets, resolved `-w flag > $WTM_WORKSPACE > cwd
(longest path match) > default_workspace > sole declared workspace > error`.
There is no built-in default: with nothing declared/selected, `wtm` errors.

**Project**:
A bare repository plus its worktrees, living at `<workspace>/<name>`. Projects
are *discovered* on disk (a `<name>/.git`), never declared; config only
*annotates* them (description, base branch, hooks).
_Avoid_: repo (ambiguous with the bare repo inside it)

**Worktree**:
A working checkout under a project's `worktrees/`, on its own branch.

**Target**:
The worktree (or project) a command points at, named by the target grammar.
_Avoid_: destination, path (too generic)

**Target grammar**:
The decoding of a command's positionals into a target:
`PROJECT NAME | NAME | DIR | cwd`, resolved within the active workspace (the
workspace is orthogonal to the positionals). Lives in `wtm.target`.

**Target mode**:
Which shape of the target grammar a command accepts. One of `WORKTREE`
(DIR and cwd-default allowed), `DELETE` (DIR allowed, no cwd-default), or
`NAME_ONLY` (`PROJECT NAME`/`NAME` only, no DIR, no cwd-default).

**Token kind**:
What may appear at a given positional slot during completion: a `PROJECT`
name or a `WORKTREE_NAME`.

**Base branch**:
A project's configured fork point for a *new* branch. Feeds `create`'s
new-branch base ref only (`--base/--off` > configured > `origin/HEAD` > `HEAD`
> orphan); irrelevant when checking out an existing local/origin branch.

**Hook**:
An executable (builtin file) or `sh -c` command (inline config) fired on a
lifecycle event, discovered/declared across scopes.

**Builtin hook / inline hook**:
A **builtin** hook is a discovered `wtm-<event>` file (or `.d/*` entry); an
**inline** hook is a command declared in config (`[...hooks.on] event = "..."`).
Within a scope, builtins fire first (unless `builtins = false`), then inline
commands. Both accumulate across scopes.

**Event**:
A lifecycle point that fires hooks. Names follow a `pre-`/`post-` convention: a
prefix marks the side of a state mutation, a bare name is a mutation-free
moment. The nine events are `pre-create`, `post-create`, `install`, `enter`,
`leave`, `pre-delete`, `post-delete`, `pre-rename`, `post-rename`.

**Scope**:
Where a hook/config is discovered/declared, from most general to most specific:
`system`, `user`, `workspace`, `project`, `worktree`. Config scopes mirror hook
scopes; each scope has its own config file, scalars resolve most-specific-first,
and hooks accumulate.

**Source / Target** (migration):
`wtm migrate` folds a legacy plain clone (the *source*) into the bare-repo
layout (the *target* project) of the active workspace.
