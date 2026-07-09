# wtm

Domain language for `wtm`, a git worktree manager over a single projects-root
layout (`$WTM_PROJECTS_ROOT/<project>/.git` bare repo + `worktrees/<name>/`).

## Language

**Project**:
A bare repository plus its worktrees, living at `$WTM_PROJECTS_ROOT/<name>`.
_Avoid_: repo (ambiguous with the bare repo inside it)

**Worktree**:
A working checkout under a project's `worktrees/`, on its own branch.

**Target**:
The worktree (or project) a command points at, named by the target grammar.
_Avoid_: destination, path (too generic)

**Target grammar**:
The decoding of a command's positionals into a target:
`PROJECT NAME | NAME | DIR | cwd`. Lives in `wtm.target`.

**Target mode**:
Which shape of the target grammar a command accepts. One of `WORKTREE`
(DIR and cwd-default allowed), `DELETE` (DIR allowed, no cwd-default), or
`NAME_ONLY` (`PROJECT NAME`/`NAME` only, no DIR, no cwd-default).

**Token kind**:
What may appear at a given positional slot during completion: a `PROJECT`
name or a `WORKTREE_NAME`.

**Hook**:
An executable fired on a lifecycle event, discovered across scopes.

**Event**:
A lifecycle point that fires hooks. Names follow a `pre-`/`post-` convention: a
prefix marks the side of a state mutation, a bare name is a mutation-free
moment. The nine events are `pre-create`, `post-create`, `install`, `enter`,
`leave`, `pre-delete`, `post-delete`, `pre-rename`, `post-rename`.

**Scope**:
Where a hook is discovered/installed, from most general to most specific:
`system`, `user`, `workspace`, `project`, `worktree`.

**Source / Target** (migration):
`wtm migrate` folds a legacy plain clone (the *source*) into the bare-repo
layout (the *target* project).
