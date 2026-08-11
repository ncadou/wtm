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
The worktree (project, or workspace) a command points at, named by either the
**Target grammar** or a **Locator**.
_Avoid_: destination, path (too generic)

**Target grammar**:
The decoding of a command's positionals into a target:
`WORKSPACE PROJECT NAME | PROJECT NAME | NAME | DIR | cwd`, count-based. The
leading `WORKSPACE` is optional and reachable only with the full triple; with
fewer tokens the workspace falls back to the active one. `WORKSPACE`, `PROJECT`,
and `NAME` share one flat namespace, so a 2-token target is always
`PROJECT NAME`, never `WORKSPACE PROJECT`. Lives in `wtm.target`. It decodes the
tokens a **Locator** does not (see **Locator reach**): the whole target of the
destructive verbs (`delete`, `rename`, `hook delete`), of `hook install` (which
writes into a scope), and of the project-tier commands whose only qualifier is a
workspace (`project new`/`clone`/`resolve`/`set`/`show`, `config show`'s
workspace/project level dumps); and the trailing literal `NAME` of the
**qualifier-only locator** commands, whose leading tokens a locator resolves.

**Locator**:
The sibling of the **Target grammar**, decoding the positionals of the
existence-bound commands: a free, scope-inferring name query matched across
*every* declared workspace, so the same token resolves from any active workspace
(the shell-history-portability motive). Same output as a target (a worktree,
project, or workspace); different input discipline. The last token is the leaf
you land on, leading tokens are ancestor qualifiers in top-down order (levels may
be skipped). Resolution is a **leaf-tier cascade**: try the deepest leaf tier
first, falling back only on a miss -- worktree named the last token (ancestors
matched as a project *or* a workspace), else project (a leading token its
workspace), else workspace (single token only). One match resolves and is used
with that tier's contract, several are ambiguous (the qualified choices are
printed for the calling command, non-zero exit), none is not-found. Before
reporting an ambiguity, a **current-context tiebreak** (`narrow_by_context`)
applies: the tied same-tier candidates are narrowed by the active workspace *and*
its cwd-inferred project, relaxing to the workspace alone on a miss; a unique
in-context match resolves silently, otherwise the tie stands and prints as
before. The tiebreak only ever turns a former ambiguity error into a hit -- it
never touches an already-unique resolution -- and the core cascade stays pure and
global (it is a CLI-layer post-filter). Lives in `wtm.locate`: `locate` is the
pure cascade over a `LocationIndex` snapshot, `build_index` the filesystem-bound
(git-free) edge. The CLI-side edge (`_locate_one` and its `_locate_project` /
`_locate_worktree_args` / `_locate_parent_args` shapes) is command-agnostic and
shared by every consumer.

**Qualifier-only locator**:
A **Locator** applied to a target's *leading* tokens alone, the trailing `NAME`
staying literal (`_locate_parent_args`). The reach of the **absent-leaf**
commands: `create`, `worktree set`, and `config show --worktree` all name a
worktree that need not exist, so the leaf is invisible to the index -- but its
ancestors usually are, and locating just those buys the same global reach without
pretending to see the leaf. The qualifiers go in as one PROJECT-pinned locator
query, so `PROJECT` and `WORKSPACE PROJECT` both resolve through the ordinary
cascade and the 3-token form keeps its meaning.
Strictly additive: fewer than two tokens carry no qualifier (a `DIR`, a lone
`NAME`, the cwd default) and pass through, and a qualifier matching *no* project
anywhere is handed back literally -- so these commands still name a wholly absent
target (`wtm create newproj newwt` stays pure path math). Only an *ambiguous*
qualifier errors, its printed choices carrying the trailing `NAME` so each stays
runnable as typed.

**Leaf-tier pin**:
A caller's declaration that it accepts one tier of leaf only
(`locate(..., tier=...)`): the cascade is skipped and just that tier's matches
count, so a project-level command never resolves a same-named worktree. The
tier-polymorphic movers (`enter`/`leave`/`resolve`) run unpinned; `list` and
`project enter`/`leave` pin PROJECT, `hook list` and `hook run` pin
WORKTREE, and a **qualifier-only locator** always pins PROJECT (its leaf is the
qualifier). A
pin narrows *which tier answers*, never how an ambiguity within that tier is
reported. In completion the pin drops only the tiers that can be neither the leaf
nor an ancestor qualifier of it -- so a PROJECT pin hides worktree names but still
offers workspaces (which qualify a project), and a WORKTREE pin hides nothing.

**Locator reach**:
Which commands may take a **Locator**, and how much of their target it decodes. A
locator reaches *globally* and sees *only what exists on disk*, so how much of a
target it can serve follows from what already exists. Three classes:

- *Whole target*: the leaf itself exists, and reaching it is reversible --
  `enter`, `leave`, `list`, `resolve`, `project enter`, `project leave`,
  `hook list`, `hook run`.
- *Qualifiers only*: the leaf is being named into existence but its ancestors are
  not -- the **qualifier-only locator** commands (`create`, `worktree set`,
  `config show --worktree`).
- *Neither*: a **destructive** verb (`delete`, `rename`, `hook delete`) stays
  *bounded by the cwd*, because reaching into another project or workspace
  irreversibly must be spelled out positionally -- a bare `wtm delete main` can
  never resolve into a workspace you are not standing in. The dividing line is
  irreversibility, not mutation: `enter`, `hook run`, and `create` all mutate or
  fire hooks and still reach globally. `hook install` also stays, writing into a
  `--scope` rather than pointing at the worktree. And the project-tier commands
  whose only qualifier is a workspace (`project new`/`clone`/`resolve`/`set`/
  `show`, `config show --workspace`/`--project`) gain nothing, a workspace name
  being an exact registry key already.

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
`workspace-leave` (worktree->project->workspace teardown order). Both verbs are
also reachable through the tier-polymorphic `wtm enter`/`wtm leave`, whose
unpinned **Locator** cascade dispatches a workspace-tier match here.

**Project lifecycle**:
`wtm project enter`/`leave` operate at project granularity, the middle tier
between the workspace and worktree lifecycles. Both take a PROJECT-pinned
**Locator** (see **Leaf-tier pin**), and both are also reachable through the
unpinned `wtm enter`/`wtm leave` cascade. `enter` cds into the project dir
(`<workspace>/<project>`, beside `.git`/`worktrees`) and fires `project-enter`;
`leave` fires `project-leave` and cds out. These two events fire across the
`system`/`user`/`workspace`/`project` scopes (never worktree -- there is no live
worktree) with the project-level contract (the six project vars, no
`WTM_WORKTREE_DIR`/`WTM_BRANCH`). `leave` cascades: standing inside a worktree of
the target project, it fires that worktree's `leave` first
(worktree->project teardown order), then `project-leave`. The enter side does
not cascade: `project enter` fires `project-enter` across its scopes but never
`workspace-enter`.

**Config authority**:
`wtm.config` is the single module that reads or writes any `.wtm` config -- no
other part of the app touches `tomlkit` or builds a `.wtm/config.toml` path. It
loads a resolved read snapshot per invocation (`Config.load`) and exposes, all
on `Config`, three write/read tiers: a **generic value facility**
(`get_value`/`set_value`/`unset_value` plus element-wise
`add_to_array`/`remove_from_array` and `set_table_value`/`unset_table_value`
over a `ConfigAddress` naming any scope
plus a bare or tuple key -- the substrate everything else is built on), the
**typed read view** (the `Workspace`/`ProjectAnnotation`/`ResolvedWorktree`/
`HookSpec` dataclasses, each carrying an `extra` dict of arbitrary keys; every
key resolves by a single **uniform locality merge** -- `_merge_locality` folds a
most-local-first probe table so increased locality always outranks lesser, the
most-local *present* value wins, `hooks.on` accumulates, and **inheritance**
climbs the **entity** chain for the few **inheritable keys**, only `base_branch`
today, via `base_branch_provenance`), and the **structural registry mutations**
(`add`/`set`/`rename`/`remove_workspace`, `set`/`clear_default`,
`set`/`clear_base_branch`, `set_project_annotation`) that write only the user
file and own their cross-key fixups (a rename follows a dangling
`default_workspace`). An entity stores its own config at its file's **top level**
(no `[project]` wrapper); a container nests a child under `<child-plural>.<name>`
once per ancestor above the file. All I/O is one comment-preserving `tomlkit`
round-trip with an atomic replace, serialised by a **per-directory lock**
(`flock` on the file's containing dir -- stable, unlike the file, which
`os.replace` swaps on every write, and leaving no lock artifact to clean up);
every write (scalar or array, generic or structural) funnels through one
`_transaction` primitive that re-reads the file fresh under the lock and applies
just its delta -- a read-modify-write, so a concurrent writer's *other* keys
survive (and a no-op change is elided, neither creating an empty file nor
churning an existing one). The load-time *resolution* snapshot
(registry/active/hooks, built once by `Config.load`) is not refreshed
mid-invocation (`wtm` loads once, acts, exits); it is the per-write disk read
that is always fresh. Arrays are string-valued and do
*not* merge across scopes (a read takes the most-local scope's whole array,
unlike `hooks.on`/`inheritable`); distinct per-tier names keep per-scope arrays
from shadowing one another. Writes name their target scope explicitly (every
scope, system and worktree included, is writable) via `-s/--scope` on the
calling command (default: the deepest scope whose dir exists); the
`*_write_target` resolvers map a scope to its `(address, key-prefix)` write site,
and `resolve_generic_target`/`resolve_generic_value` expose the same cascade to
the generic `config set/get/unset` and inline `hook set/unset` surfaces over the
cwd-inferred entity (for an *inheritable* key -- the builtin `base_branch` per
`INHERITABLE_KEYS`, or any key named in the user's accumulating `inheritable`
array -- the generic read folds the entity's own cascade plus its ancestors' so
`config get` reports the same effective value a real command uses, and
`--origin` labels each rung by its definition tier; inheritance reaches
`config show`/the resolved view too, via a shared tier-fold that fills a
descendant's `extra` with inherited values). Workspace *declarations* (`add`/`set --path`/`rename`/
`default`) stay user-file-only and refuse system-only names. `WTM_NO_CONFIG`
suppresses all reads and refuses all writes; there is no `WTM_NO_SYSTEM` (point
`WTM_SYSTEM_CONFIG` at an absent path to skip system scope).

**Scope**:
Where a hook/config is discovered/declared, from most general to most specific:
`system`, `user`, `workspace`, `project`, `worktree`. Config scopes mirror hook
scopes; each scope has its own config file, every key resolves by the uniform
locality merge (most-local present value wins; clearing removes the key), and
hooks accumulate. A scope is *not* an **entity**: the same word (`workspace`,
`project`, `worktree`) names both a config *file tier* here and the
config-*owning* thing that **inheritance** runs between -- keeping the two apart
is the point of the next four terms.

**Entity**:
A thing that owns config: a **workspace**, **project**, or **worktree**, plus
the implicit *global* root above them all. Entities nest
(`global > workspace > project > worktree`), and that nesting is the axis
**inheritance** runs along. Orthogonal to **scope** (a *file* an entity's config
lives in): resolving one entity's config folds several scope files, and the
resolved value may then inherit from an ancestor *entity*.
_Avoid_: object, node, level

**Locality**:
How local to the **entity** the **scope** file a config value is written in is.
One entity's config is scattered across several scope files (its own file, its
project's `[worktrees.N]` table, the user file's `[workspaces.W...]` table);
locality ranks those placements most-local-first, and the **locality merge**
picks the most-local present value. A property of where a value sits -- not of
the file, which is a fixed slot; a within-entity notion, never a claim about
ancestor entities.

**Locality merge**:
The most-local-file-wins fold collapsing one **entity**'s config across the
**scope** files that describe it (`_merge_locality`). Presence-based: any present
value at a more-local file overrides a less-local one, clearing removes the key,
`hooks.on` accumulates. A plain *override* that runs strictly *within* a single
entity -- contrast **inheritance**, which crosses entities.
_Avoid_: cascade (overloaded), scope inheritance

**Inheritance** (entity inheritance):
A value falling from an ancestor **entity** to a descendant that leaves it
unset -- a worktree from its project, a project from its workspace, a workspace
from the global default (`base_branch_provenance`). Runs *between* entities (each
ancestor's contribution is itself already **locality-merged**) and only for an
**inheritable key**. The one thing "inherit" should ever describe here: a
most-local *file* winning within one entity is a **locality merge**, not
inheritance.
_Avoid_: cascade, scope inheritance, override

**Inheritable key** (non-entity-specific key):
A config key that undergoes **inheritance** -- equivalently, a
*non-entity-specific* key: a shared default describing a whole subtree (it flows
down the containment chain), as opposed to an *entity-specific* key describing a
single node (`description`, `path`), which must never inherit. The two framings
are one axis. The effective set is the builtin `INHERITABLE_KEYS` allowlist
(`base_branch` only today) unioned with the user's `inheritable` array -- a
top-level TOML array of key names that *accumulates* across scopes (like
`hooks.on`), so a workspace may opt a key in for all its projects
(`Config.inheritable_keys`). Every other key is entity-local (resolved by
**locality merge** alone, never falling to a descendant entity). Opt-in is
additive: arbitrary keys stay entity-local by default and the builtin
classifications are fixed. Membership is always a curated decision -- hardcoded
or user-declared -- never inferred from a key's name, type, or value.
`inheritable` is itself a **reserved key** (refused as an arbitrary key, never
leaked into `extra`), maintained via the array flag -- `config set inheritable
KEY -a` / `config unset inheritable KEY -a` is the one reserved array that flag
may touch,
each added element validated by `validate_inheritable_element` (a reserved key or a
hardcoded-non-inheritable builtin -- `NON_INHERITABLE_KEYS`, i.e. `description`
-- is refused). One builtin carve-out: `base_branch` has no worktree tier,
so a worktree-local `base_branch` is ignored (aligning `config get` with
`create`), whereas a user-declared inheritable key set worktree-local *is*
honoured.

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
