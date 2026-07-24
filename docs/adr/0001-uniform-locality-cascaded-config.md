# 1. Uniform locality-cascaded config across the full scope ladder

Date: 2026-07-24

## Status

Accepted (implemented)

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
merge** over the whole scope ladder, plus explicit write-placement and a generic
key/value surface.

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
- A generic `config set/get/unset KEY [VALUE]` surface writes/reads arbitrary
  keys at a chosen scope; `config get KEY --sources` reports the full probe chain
  (including inheritance steps), while `config show --sources` labels each value
  with its winning scope only.
- Reserved names (`hooks`, `projects`, `worktrees`, `path`,
  `default_workspace`) are rejected as arbitrary keys.
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

Each phase is independently shippable:

1. Resolver rewrite: probe tables, generic Layer-1 merge + provenance, Layer-2
   inheritance registry, kind-clash errors, top-level self layout; retire
   `_parse_project_local` / `_base_branch_cascade`. Data model gains
   `extra: dict` on `ProjectAnnotation` / `Workspace` / `ResolvedWorktree`, plus
   `description` on `ResolvedWorktree`.
2. `-s/--scope` write placement on `project set` / `workspace set`.
3. Worktree tier: `worktree set` + deep worktree read-cascade.
4. Generic `config set/get/unset` + arbitrary-key cascade + reserved-name
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

Phase 4 shipped the generic `config get/set/unset` surface, but `config get`
resolved only Layer 1 (the entity's own locality cascade), contradicting this
ADR's promise that `config get KEY --sources` reports the chain *including
inheritance steps*. The gap was user-visible: from inside a project,
`config get base_branch` printed `(unset)` while `create` forked off the
inherited workspace/global default — the two read paths disagreed.

`config get` is now inheritance-aware. An **inheritable-key registry**
(`INHERITABLE_KEYS`, today just `base_branch`) drives it: for such a key the
generic resolver folds an *extended*, tier-tagged probe list — the entity's own
cascade followed by its ancestors' (project → workspace → global) — so the
most-local winner matches `base_branch_provenance` exactly. Because the extended
list stays most-local-first, no new precedence logic is needed. `--sources` now
labels each rung by its **definition level** (`project` / `workspace` /
`global`, matching `config show`) for an inheritable key, or its scope otherwise,
and always names the file the value lives in — so the several rungs that share a
scope (e.g. three `user`-file tiers) read distinctly.

Two cleanups rode along. Layer 1's per-leaf provenance return from
`_merge_locality` was dead (every caller discarded it, and it records only the
single winner — it cannot feed the `--sources` full chain), so it was removed;
the read view still reports provenance via `base_branch_provenance`. And the
generic write path now marks its container tables (`workspaces` / `projects` /
`worktrees`) as super-tables like the structural writers, so a fresh deep write
renders dotted `[workspaces.W.projects.P]` headers with no empty parent lines.
