# 1. Uniform locality-cascaded config across the full scope ladder

Date: 2026-07-24

## Status

Accepted — fully implemented. All six phases below and the four addenda have
shipped (see the phase-completion note under **Implementation phases**). The
locality/inheritance invariants are guarded by `tests/test_config.py` and
`tests/test_wtm.py`.

## Context

`wtm` config lives in up to five scopes, most-general to most-specific:
`system, user, workspace, project, worktree`. The read side and the write side
grew unevenly, leaving three problems.

1. **Asymmetric annotation placement.** A *project* annotation
   (`description`/`base_branch`) can be written to the project-local
   `<project>/.wtm/config.toml` or fall back to the user-file registry
   annotation, but there is no way to write it into the *workspace-local* file
   even though the read side already merges a workspace-local `[projects.NAME]`
   table (`_apply_workspace_local`). Placement is automatic ("most-local home
   that exists"), so for a project whose directory exists — the common case —
   the workspace tier is unreachable.

2. **A latent provenance bug.** `_base_branch_cascade` does not probe the
   workspace-local `[projects.NAME].base_branch`, so a value set only there is
   *seen* by `project_annotation()` (via the merged registry) but *missed* by
   `base_branch_provenance()` / `config show`, which report the wrong tier.

3. **Hand-written, per-field cascades.** Each resolvable field carries its own
   bespoke merge/probe logic. Arbitrary keys are not supported, the worktree
   tier only ever reads its own local file (no cascade), and worktrees have no
   scalar annotation at all.

The desired invariant is simple and uniform: **increased locality always
outranks lesser locality**, and any config supported at a child-local file is
writable — and correctly resolved — at every ancestor scope.

## Decision

Replace the per-field cascades with a single **locality-ordered, recursive
merge** over the whole scope ladder, plus explicit write-placement and an
arbitrary-key surface.

### Uniform storage layout

Self config is stored at the **top-level** of an entity's own file (the
`[project]` wrapper is dropped — no backward compatibility is retained).
Children nest under `<child-plural>.<name>`. An entity's config table is
therefore: start at the file root, then descend `<child-plural>.<name>` once per
ancestor that sits above that file.

Probe tables, most-local to least-local:

- **Worktree T** (in project P, workspace W): worktree-local top-level →
  project-local `[worktrees.T]` → workspace-local `[projects.P.worktrees.T]` →
  user/system `[workspaces.W.projects.P.worktrees.T]`.
- **Project P** (in W): project-local top-level → workspace-local
  `[projects.P]` → user/system `[workspaces.W.projects.P]`.
- **Workspace W:** workspace-local top-level → user/system `[workspaces.W]`.

The `path` and `default_workspace` keys remain user-file **declarations**,
outside the payload cascade.

### Two-layer resolution

- **Layer 1 — locality.** Fold probes least-to-most-local. Per key: absent takes
  the present value; two tables recurse; a kind mismatch (scalar / array /
  table) across scopes is a hard error naming the key path and both files;
  otherwise the most-local value wins wholesale. Resolution is **presence-based**
  (`false` / `0` / `""` are legitimate values; clearing removes the key). Arrays
  replace; `hooks.on` is the single accumulating key. `projects` / `worktrees`
  are child-descent containers, excluded from an entity's own payload. Layer 1
  records per-leaf provenance `(scope, table-path)`.
- **Layer 2 — inheritance.** A declarative registry names the keys that inherit
  down the containment chain. Today it holds only `base_branch` (project → its
  workspace → global top-level default); the mechanism is built so future keys
  can opt in while most keys stay strictly per-entity. Inheritable keys carry
  two-dimensional provenance `(tier, scope)`.

### Write placement and CLI

- `-s/--scope` (mirroring `hook install`) selects the target file on `project
  set`, `workspace set`, and a new `worktree set`; the auto-default is the
  deepest scope whose directory exists. Allowed scopes: project →
  {project, workspace, user, system}; workspace → {workspace, user, system};
  worktree → {worktree, project, workspace, user, system}. `WTM_NO_CONFIG` still
  refuses all writes; workspace declarations stay user-file-only.
- Worktrees gain a `description` field.
- An arbitrary-key `config set/get/unset KEY [VALUE]` surface writes/reads arbitrary
  keys at a chosen scope; `config get KEY --sources` reports the full probe chain
  (including inheritance steps), while `config show --sources` labels each value
  with its winning scope only. (The 2026-07-30 addendum renames `--sources` to
  `-o/--origin` on both commands and adds the element-wise `-a/--array` /
  `-t/--table` modes.)
- Reserved names (`hooks`, `projects`, `worktrees`, `path`,
  `default_workspace`) are rejected as arbitrary keys. (The 2026-07-28 addendum
  adds `inheritable` to this set; the effective list today is the six names in
  `RESERVED_KEYS`.)
- An inline-hook writer (`hook set/unset EVENT "cmd" -s SCOPE`) writes
  `[…hooks.on]` at the entity's table-path, reusing the same prefix machinery.

## Consequences

- **Positive.** One merge engine replaces the bespoke cascades; the provenance
  bug disappears; the locality invariant holds uniformly; arbitrary keys and the
  worktree tier become first-class; the inheritance registry is an explicit
  extensibility seam.
- **Negative / costs.** Deeply nested keys
  (`[workspaces.W.projects.P.worktrees.T.hooks.on]`) are verbose. Dropping
  backward compatibility means existing `[project]`-wrapped local files and test
  fixtures must be rewritten. `config show` provenance grows a scope dimension,
  and the arbitrary-key merge needs firm kind-clash handling (chosen: error).

## Implementation phases

All six phases are shipped (each was independently shippable, and each landed in
order); the five addenda below record the follow-on corrections and extensions
made after phase 6.

1. Resolver rewrite: probe tables, arbitrary-key Layer-1 merge + provenance, Layer-2
   inheritance registry, kind-clash errors, top-level self layout; retire
   `_parse_project_local` / `_base_branch_cascade`. Data model gains
   `extra: dict` on `ProjectAnnotation` / `Workspace` / `ResolvedWorktree`, plus
   `description` on `ResolvedWorktree`.
2. `-s/--scope` write placement on `project set` / `workspace set`.
3. Worktree tier: `worktree set` + deep worktree read-cascade.
4. Arbitrary-key `config set/get/unset` + arbitrary-key cascade + reserved-name
   validation.
5. Inline-hook writer `hook set/unset`.
6. `config show` rewrite + docs (this ADR, `CONTEXT.md`, `docs/configuration.md`).

## Addendum (2026-07-24): completing the single-engine migration

Phase 1 retired the per-field *read* cascades, but the workspace **registry
build** kept a second, falsy-coalescing merge (`_merge_workspace` /
`_apply_workspace_local`) that fed the `Workspace` dataclass in parallel with the
probe engine. Two symptoms survived: an empty-string workspace scalar could not
clear an ancestor (violating the presence-based rule), and `Workspace.extra` was
never populated, so `config show` silently dropped workspace-level arbitrary keys
that `config get` could read. The registry is now a thin declaration index
(name + `path` + winning scope, from the user/system files only) and every
workspace payload folds through the same probe engine; `config show` enumerates a
workspace's projects as the union of on-disk dirs and declared names, each
resolved via the probe engine (so project-local-only annotations and inherited
`base_branch` surface). Two further read-surface asymmetries closed with it: the
worktree hook scope now accumulates across ancestor registries like the project
scope, and `config get` enforces the kind-clash error like `config show`.

## Addendum (2026-07-24): inheritance-aware `config get`, and cleanups

Phase 4 shipped the arbitrary-key `config get/set/unset` surface, but `config get`
resolved only Layer 1 (the entity's own locality cascade), contradicting this
ADR's promise that `config get KEY --sources` reports the chain *including
inheritance steps*. The gap was user-visible: from inside a project,
`config get base_branch` printed `(unset)` while `create` forked off the
inherited workspace/global default — the two read paths disagreed.

`config get` is now inheritance-aware. An **inheritable-key registry**
(`INHERITABLE_KEYS`, today just `base_branch`) drives it: for such a key the
arbitrary-key resolver folds an *extended*, tier-tagged probe list — the entity's own
cascade followed by its ancestors' (project → workspace → global) — so the
most-local winner matches `base_branch_provenance` exactly. Because the extended
list stays most-local-first, no new precedence logic is needed. `--sources` now
labels each rung by its **definition level** (`project` / `workspace` /
`global`, matching `config show`) for an inheritable key, or its scope otherwise,
and always names the file the value lives in — so the several rungs that share a
scope (e.g. three `user`-file tiers) read distinctly. (The flag is `-o/--origin`
since the 2026-07-30 addendum; the behaviour described here is unchanged.)

Two cleanups rode along. Layer 1's per-leaf provenance return from
`_merge_locality` was dead (every caller discarded it, and it records only the
single winner — it cannot feed the `--sources` full chain), so it was removed;
the read view still reports provenance via `base_branch_provenance`. And the
arbitrary-key write path now marks its container tables (`workspaces` / `projects` /
`worktrees`) as super-tables like the structural writers, so a fresh deep write
renders dotted `[workspaces.W.projects.P]` headers with no empty parent lines.

## Addendum (2026-07-28): user-extensible inheritability

Layer 2's inheritable-key registry was a hardcoded frozenset (`base_branch`
only), so the *entity-specific vs non-entity-specific* distinction it encodes
was closed to users: an arbitrary key could cascade *within* an entity (Layer 1)
but never inherit *between* entities (Layer 2), no matter how default-like it
was. The two are one axis — an **inheritable** key is exactly a
*non-entity-specific* one: a shared default describing a whole subtree (it flows
down the containment chain), as opposed to a property of a single node
(`description`, `path`), which must never inherit.

Users now opt an arbitrary key onto that axis via a top-level `inheritable`
array of key names. The effective set is the builtin `INHERITABLE_KEYS` unioned
with every `inheritable` array found along the entity's chain; the declaration
**accumulates across scopes** (like `hooks.on`), so a workspace can opt a key in
for all its projects. It is additive opt-in only — arbitrary keys stay
entity-local by default, and the builtin classifications are fixed
(`base_branch` in, `description` out). `inheritable` is a reserved key (refused
as an arbitrary key, never leaked into `extra`).

Inheritance for a declared key reaches every read surface uniformly, not just
`config get`: the generalized inherit-chain builder feeds `GenericTarget.inherit`
for the whole effective set, and a shared tier-fold (`_fold_tiers`) injects
inherited ancestor values into the resolved view's / registry's `extra` for keys
a descendant leaves unset — so `config show` and `config get` agree (the same
divergence class the previous addendum closed for `base_branch`).

`base_branch` keeps one carve-out: it is a project-tier concept with no worktree
tier of its own, so a worktree-local `base_branch` is ignored (keeping
`config get` aligned with `base_branch_provenance` / `create`), whereas a
user-declared inheritable key set worktree-local *is* honoured. That is the only
place a builtin's chain differs from the arbitrary-key one.

## Addendum (2026-07-28): CLI/env surface and rationale left implicit

The Decision recorded the resolution engine but glossed three parts of the shipped
surface. Documenting them here (no behaviour change) so the ADR matches the code.

- **The `config base-branch` command.** The "Write placement and CLI" bullets
  cover `project set` / `workspace set` / `worktree set`, the arbitrary-key
  `config set/get/unset`, and `hook set/unset`, but omit the dedicated
  `wtm config base-branch [VALUE] [--clear]`. It is the read/set/clear surface for
  the **global top-level** `base_branch` default (the inheritance floor below the
  workspace tier): with no VALUE it prints the effective global default annotated
  with its winning scope, and set/clear write the user-scope top-level key. It is
  a thin wrapper over the `set_base_branch` / `clear_base_branch` structural
  mutations named in the Decision — a convenience peer of the arbitrary-key surface, not
  a new mechanism.

- **System-scope selection is env-only; there is no `WTM_NO_SYSTEM`.** The system
  file is `$WTM_SYSTEM_CONFIG` (default `/etc/wtm/config.toml`). `WTM_NO_CONFIG`
  is the all-or-nothing isolation knob — it suppresses *every* scope's reads and
  refuses *every* write. There is deliberately no per-scope suppression flag; to
  drop just the system scope, point `WTM_SYSTEM_CONFIG` at a path that does not
  exist (an absent file resolves to an empty document). This keeps the env
  surface to two knobs and the "skip a scope" idiom uniform (an absent file, not a
  flag).

- **Inheritability is a curated allowlist, never inferred.** A key joins Layer 2
  only by explicit decision — the hardcoded `INHERITABLE_KEYS` (`base_branch`) or
  the user's accumulating `inheritable` array — and *never* by heuristic on the
  key's name, type, or value. Type/shape-inferred inheritance (e.g. "scalars
  inherit, tables don't") was rejected: it would make a key's cross-entity
  behaviour an implicit property of its data rather than a stated intent, and
  would silently reclassify a key when its value changed shape. The
  entity-specific-vs-shared-default distinction is a modelling choice, so it stays
  a curated decision.

Reserved-key reconciliation: the Decision's reserved list predates the
`inheritable` key added in the earlier 2026-07-28 addendum. The effective set is
now the six names in `RESERVED_KEYS`: `hooks`, `projects`, `worktrees`, `path`,
`default_workspace`, `inheritable`.

## Addendum (2026-07-30): kind-symmetric element-wise writes, and `-o/--origin`

Phase 4's arbitrary-key surface grew array mutation as a pair of `config set` flags
(`--array-add` / `--array-remove`), which put a *removal* on the `set` verb and
left the third TOML kind — the **table** — with no CLI at all, writable only by
hand or through the reserved `hooks` surface. The asymmetry was in the verbs, not
the substrate: `_Store` already keyed every read and write by a bare-or-tuple
`Key`, so nested writes worked underneath.

The element-wise modes now hang off the **verb that matches the mutation**, one
flag pair per kind:

- `config set -a/--array KEY VALUE` adds (set semantics, idempotent);
  `config unset -a/--array KEY VALUE` strips every occurrence.
- `config set -t/--table KEY SUBKEY VALUE` sets a cell;
  `config unset -t/--table KEY SUBKEY` removes one.

`-a` and `-t` are mutually exclusive, and each mode validates its own positional
arity. The table pair is a straight peer of the array pair in the substrate
(`_Store.table_set` / `table_unset`, `Config.set_table_value` /
`unset_table_value`), sharing the one `_transaction` primitive — per-directory
lock, fresh re-read, no-op elision, `WTM_NO_CONFIG` refusal — so a concurrent
writer's other keys survive a cell write exactly as they survive an array
append. A new `_writable_table` mirrors `_writable_array`, extending the
kind-clash rule to the third kind: `-t` onto a scalar or array (and `-a` onto a
table) is a hard error, matching the read-side `_check_kind`. The
`inheritable` allowlist keeps its carve-out as the one reserved array the flag
may touch; element validation runs on the `set -a` add path only, since removing
an element can never introduce an illegal one.

`config get` gained the matching **readback**, since a kind the CLI can write
should be a kind the CLI can print: a scalar prints bare (unchanged), an array
one element per line, a table `subkey = value` per line (sorted), and a trailing
`config get KEY SUBKEY` reads a single cell. Kind is auto-detected from the
resolved value rather than selected by a flag — the resolver already returns the
kind, so a read flag would only be able to contradict it.

Separately, `--sources` is renamed **`-o/--origin`** on both read commands
(`config get`, `config show`). The short form was the reason: `-s` meant
`--scope` on `config set` / `unset` but `--sources` on `config get` / `show`, so
one letter carried two meanings across sibling subcommands of the same noun.
`-s` is now unambiguously `--scope` across the whole `config` surface. Both read
commands were renamed together — leaving one as `--sources` would have traded a
cross-verb collision for a cross-verb divergence. The flag's *behaviour* is
unchanged: for an inheritable key it still labels each rung by definition tier
(`project` / `workspace` / `global`) and otherwise by scope, and always names the
file. Provenance output remains whole-value oriented; it does not render
per-element or per-cell origins for arrays and tables.
