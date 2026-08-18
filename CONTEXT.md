# wtm

Domain language for `wtm`, a git worktree manager over one or more named
**workspaces**, each a projects-root layout (`<workspace>/<project>/.git` bare
repo + `worktrees/<name>/`).

## Language

**Workspace**:
A named projects root, *declared* in config (path + name + description +
hooks). Every command operates within one **active** workspace. Also the name
of the projects-root-wide hook/config scope. The `wtm workspace` verbs
`add`/`set`/`rename`/`delete`/`default` *write* these declarations (user scope
only); `enter`/`leave` fire its lifecycle hooks (see **Workspace lifecycle**).
_Avoid_: root (say "workspace path" or "projects root of the workspace")

**Active workspace**:
The workspace a command targets, resolved `WORKSPACE positional > $WTM_WORKSPACE
> cwd (longest path match) > default_workspace > sole declared workspace >
error`. There is no built-in default and no `-w` flag: with nothing
declared/selected, `wtm` errors. The leading `WORKSPACE` in a command's
positionals overrides the rest per-target (see **Target grammar**).

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
`WORKSPACE PROJECT NAME | PROJECT NAME | NAME | DIR | cwd`, count-based. The
leading `WORKSPACE` is optional and reachable only with the full triple; with
fewer tokens the workspace falls back to the active one. `WORKSPACE`, `PROJECT`,
and `NAME` share one flat namespace, so a 2-token target is always
`PROJECT NAME`, never `WORKSPACE PROJECT`. Lives in `wtm.target`.

**Target mode**:
Which shape of the target grammar a command accepts. One of `WORKTREE`
(DIR and cwd-default allowed), `DELETE` (DIR allowed, no cwd-default), or
`NAME_ONLY` (`[[WORKSPACE] PROJECT] NAME` only, no DIR, no cwd-default).

**Token kind**:
What may appear at a given positional slot during completion: a `WORKSPACE`
name, a `PROJECT` name, or a `WORKTREE_NAME`.

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
moment. The eleven events are `pre-create`, `post-create`, `install`, `enter`,
`leave`, `pre-delete`, `post-delete`, `pre-rename`, `post-rename`,
`workspace-enter`, `workspace-leave`. The two `workspace-*` events are
workspace-scoped (see **Workspace lifecycle**); the other nine are worktree
events.

**Workspace lifecycle**:
`wtm workspace enter`/`leave` operate at workspace granularity. `enter` cds into
the projects root and fires `workspace-enter`; `leave` fires `workspace-leave`
and cds out. These two events fire only across the `system`/`user`/`workspace`
scopes with a workspace-only contract (`WTM_EVENT`, `WTM_WORKSPACE`,
`WTM_WORKSPACE_DIR`) -- there is no worktree/project/branch. `leave` cascades:
standing inside a worktree of the target, it fires that worktree's `leave`
first (worktree->workspace teardown order), then `workspace-leave`.

**Registry write seam**:
`wtm` reads config everywhere but writes only via `wtm.registry`, which mutates
the *user-scope* file's `[workspaces.*]` entries and top-level
`default_workspace` (comment-preserving `tomlkit` round-trip, atomic replace).
The `add`/`set`/`rename`/`delete`/`default` verbs refuse system-only names and
`WTM_NO_CONFIG`.

**Scope**:
Where a hook/config is discovered/declared, from most general to most specific:
`system`, `user`, `workspace`, `project`, `worktree`. Config scopes mirror hook
scopes; each scope has its own config file, scalars resolve most-specific-first,
and hooks accumulate.

**Source / Target** (import):
`wtm import` folds a legacy plain clone (the *source*) into the bare-repo
layout (the *target* project) of the active workspace.

**Shell session**:
The `WTM_*` context the shell wrapper carries between an `enter` and a `leave`.
Movers that cd the shell also emit shell directives (a `cd` plus `export`/`unset`
of the eight-var contract) out-of-band via the `WTM_SHELL_FILE` temp file, which
the wrapper *sources*. Set on enter/`create -c`/`import -c` (full contract) and
on `workspace enter` (workspace vars only); *updated* on `rename` when standing
in the moved worktree; *unset* on `leave`/`delete`-of-current/`workspace leave`
(even with `-C`, which suppresses only the cd). Because the exported
`WTM_WORKSPACE` also *selects* the active workspace, entering **pins** it (over
cwd inference) until leave -- the session is deliberately sticky.
