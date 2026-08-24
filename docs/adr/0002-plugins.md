# 2. Plugins: activation cells plus per-invocation manifest resolution

Date: 2026-08-24

## Status

Accepted — implemented (core machinery, both transports, and the two first
converts, `wtm-git` and `wtm-server`).

## Context

The first two satellite tools integrated by hand:

- `wtm-git` shipped hook scripts under `share/wtm-git/` that users installed
  manually (`wtm hook install ... -e post-config -s user`);
- `wtm-server` asked users to hand-write inline hook entries
  (`post-create = "wtm-server register"`) into config, and its CLI wrote an
  `inheritable = ["wtm-server"]` entry into the user file imperatively at
  register time.

Both integrations duplicated plugin knowledge in places owned by the user:
hook dirs, inline hook tables, the inheritable allowlist. A third friction was
environmental: `wtm` installs as an isolated uv tool while plugins install
elsewhere, so same-environment entry-point discovery alone cannot serve
plugins that must stay standalone.

## Decision

1. **Activation is declarative; contribution is dynamic.** Registering a
   plugin writes exactly one thing: a `[plugins] NAME = true` cell at a scope
   file's top level. Cells deep-merge per-plugin most-local-wins across
   scopes (a workspace can disable one plugin without shadowing others).
   Everything else the plugin contributes lives in its **manifest**,
   resolved fresh on every invocation.

2. **Layered transport.** The canonical name resolves as a `wtm.plugins`
   entry point when one exists, else as a `<NAME> wtm-manifest --json`
   subprocess. An entry point commits the resolution (present-but-broken is
   reported rather than masked by a stale executable). The command form keeps
   standalone packages standalone and works across virtualenvs.

3. **Warn-and-skip degradation.** A registered-but-unresolvable name warns
   once per invocation and contributes nothing until it resolves again;
   `plugin list` flags it broken. Registration never aborts for it.

4. **Hooks form a sixth tier**, not a scope: commands fire first on setup
   events, last on teardown (machine-global generality), under the operation's
   usual enabled/strict answer, with the standard env contract. They render in
   `hook list` with provenance.

5. **Config integration stays inside existing rules.** `plugins` becomes a
   reserved key (its own table, refused as an arbitrary key). Active plugins'
   namespaces own their key heads silently; a known-but-inactive owner's head
   warns on write (attribution via registry cells + entry-point names,
   normalized `-`/`_`). Manifest `inheritable` elements are a *baseline*:
   explicit user declarations win per key, deactivation removes the baseline.
   `defaults` seed once via `plugin add --seed`, user-owned afterwards.

## Consequences

- No drift: a plugin upgrade changes behavior on the next invocation without
  touching any config file; removal is one cell unset.
- Every invocation pays fresh manifest resolution (one subprocess per active
  command-form plugin). Accepted for correctness-first simplicity; caching is
  deferred until measured to matter.
- Library users who never attach manifests get unchanged behavior
  (`Config._plugins` defaults empty).
- Python-callable hooks and typed cell validation are deliberately out of
  scope; the manifest API version gates future extensions.

## Implementation

`wtm/config/manifest.py` (shape contract), `wtm/plugins.py` (resolution),
`Config` plugin methods (registry fold, contributions, management),
`wtm/hooks.py` sixth tier, `wtm/cli/plugin.py` (+ completions). Guarded by
`tests/test_plugins.py`.
