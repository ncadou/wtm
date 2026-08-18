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
`PROJECT NAME`, never `WORKSPACE PROJECT`. Lives in `wtm.target`. Every
target-taking command uses it *except* `enter`, which takes a **Locator**.

**Locator** (experiment):
The sibling of the **Target grammar**, decoding `enter`'s positionals: a free,
scope-inferring name query matched across *every* declared workspace, so the
same token resolves from any active workspace (the shell-history-portability
motive). Same output as a target (a worktree, project, or workspace); different
input discipline. The last token is the leaf you land on, leading tokens are
ancestor qualifiers in top-down order (levels may be skipped). Resolution is a
**leaf-tier cascade**: try the deepest leaf tier first, falling back only on a
miss -- worktree named the last token (ancestors matched as a project *or* a
workspace), else project (a leading token its workspace), else workspace (single
token only). One match resolves and is entered with that tier's contract,
several are ambiguous (the qualified choices are printed, non-zero exit), none
is not-found. Before reporting an ambiguity, `enter` applies a **current-context
tiebreak** (`narrow_by_context`): the tied same-tier candidates are narrowed by
the active workspace *and* its cwd-inferred project, relaxing to the workspace
alone on a miss; a unique in-context match resolves silently, otherwise the tie
stands and prints as before. The tiebreak only ever turns a former ambiguity
error into a hit -- it never touches an already-unique resolution -- and the
core cascade stays pure and global (it is a CLI-layer post-filter). Lives in
`wtm.locate`: `locate` is the pure cascade over a `LocationIndex` snapshot,
`build_index` the filesystem-bound (git-free) edge. Only `enter` consumes it
today; the plumbing is command-agnostic.

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
moment. The thirteen events are `pre-create`, `post-create`, `install`, `enter`,
`leave`, `pre-delete`, `post-delete`, `pre-rename`, `post-rename`,
`project-enter`, `project-leave`, `workspace-enter`, `workspace-leave`. The two
`workspace-*` events are workspace-scoped and the two `project-*` events are
project-scoped (see **Workspace lifecycle** / **Project lifecycle**); the other
nine are worktree events.

**Workspace lifecycle**:
`wtm workspace enter`/`leave` operate at workspace granularity. `enter` cds into
the projects root and fires `workspace-enter`; `leave` fires `workspace-leave`
and cds out. These two events fire only across the `system`/`user`/`workspace`
scopes with a workspace-only contract (`WTM_EVENT`, `WTM_WORKSPACE`,
`WTM_WORKSPACE_DIR`) -- there is no worktree/project/branch. `leave` cascades the
full teardown chain: standing inside a worktree of the target, it fires that
worktree's `leave` first, then the containing project's `project-leave`, then
`workspace-leave` (worktree->project->workspace teardown order).

**Project lifecycle**:
`wtm project enter`/`leave` operate at project granularity, the middle tier
between the workspace and worktree lifecycles. `enter` cds into the project dir
(`<workspace>/<project>`, beside `.git`/`worktrees`) and fires `project-enter`;
`leave` fires `project-leave` and cds out. These two events fire across the
`system`/`user`/`workspace`/`project` scopes (never worktree -- there is no live
worktree) with the project-level contract (the six project vars, no
`WTM_WORKTREE_DIR`/`WTM_BRANCH`). `leave` cascades: standing inside a worktree of
the target project, it fires that worktree's `leave` first
(worktree->project teardown order), then `project-leave`. The enter side does
not cascade: `project enter` fires `project-enter` across its scopes but never
`workspace-enter`.

**Registry write seam**:
`wtm` reads config everywhere but writes through only two seams. `wtm.registry`
mutates the *user-scope* file -- its `[workspaces.*]` entries (paths + per-project
annotations) and top-level `default_workspace` / `base_branch` -- while
`wtm.config_writer` mutates the *entity-local* `.wtm/config.toml` files
(workspace and project scope). Both use a comment-preserving `tomlkit` round-trip
with an atomic replace; neither writes system or worktree config. Scalar writes
(`workspace set`, `project set`) target the most-local home that already exists:
the entity's own `.wtm/config.toml` when its dir is present, else the user-file
entry/annotation. The `add`/`set`/`rename`/`delete`/`default` verbs refuse
system-only names (for the registry-entry writes) and `WTM_NO_CONFIG`.

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
the wrapper *sources*. Set on enter/`create -c`/`import -c` (full contract), on
`project enter` (the six project vars, no `WTM_WORKTREE_DIR`/`WTM_BRANCH`), and
on `workspace enter` (workspace vars only); *updated* on `rename` when standing
in the moved worktree; *unset* on `leave`/`delete`-of-current/`project
leave`/`workspace leave` (even with `-C`, which suppresses only the cd). Because
the exported `WTM_WORKSPACE` also *selects* the active workspace, entering
**pins** it (over cwd inference) until leave -- the session is deliberately
sticky.
