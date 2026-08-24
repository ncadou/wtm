# Plugins

A plugin is an installed tool that declares its integration points to `wtm`
instead of being wired in by hand: no hook scripts copied into hooks
directories, no inline hook lines maintained in config, no imperative
`config set` calls at install time.

`wtm-git` (per-entity git tracking) and `wtm-server` (worktree service
reservations) are both plugins; they double as the reference for each of the
two transports described below.

## Model

Registration is one config cell:

```toml
# any scope file's top level, e.g. ~/.config/wtm/config.toml
[plugins]
wtm-git = true
```

The cell is *activation*, nothing more. Everything a plugin contributes lives
in the plugin itself, described by a **manifest** that `wtm` resolves fresh on
every invocation:

- **entry point** first: the plugin package publishes an entry point under the
  `wtm.plugins` group whose name matches the registered cell;
- **command** otherwise: an executable named after the plugin answers
  `NAME wtm-manifest --json` on stdout.

An entry point commits the resolution -- if one exists for the name it wins
even when it fails to load, so a broken install is reported rather than
silently masked by a stale executable on PATH.

Because activation is just a cell, everything follows from the ordinary config
rules: cells deep-merge per-plugin most-local-wins across scopes, so a
workspace can enable or disable one plugin without touching the others, and
unregistering is `wtm plugin remove NAME`.

## The manifest

```jsonc
{
  "name": "wtm-server",          // must match the registry cell
  "api_version": 1,
  "hooks": {
    "post-create": "wtm-server register",
    "post-delete": ["wtm-server unregister"]
  },
  "namespaces": "wtm-server",    // arbitrary-key heads this plugin owns
  "inheritable": [
    { "key": "wtm-server", "floor": null }
  ],
  "defaults": [                  // written once by `plugin add --seed`
    { "key": "wtm-server.tld", "value": "test" }
  ]
}
```

- **hooks**: event -> one command or an array of commands. Fired exactly like
  inline hooks: `/bin/sh -c`, `$1`, the [`WTM_*` contract](hooks.md), and the
  operation's enabled/strict policy.
- **namespaces**: heads of dotted key paths (`wtm-server.web.port`) the plugin
  owns. Writing into an *active* plugin's namespace is silent and normal;
  writing into a known-but-inactive owner's namespace succeeds with a warning.
  A namespace may not be a reserved key.
- **inheritable**: keys the plugin opts into [inheritance](configuration.md)
  while active, optionally with a floor. Explicit user declarations in the
  `inheritable` array outrank a plugin's floor for the same key; deactivating
  the plugin removes its baseline entirely.
- **defaults**: seeding material for `wtm plugin add NAME --seed`. Written
  once, into the chosen scope's file top level; afterwards ordinary
  user-owned config that `plugin remove` leaves alone.
- **api_version**: this `wtm` speaks version 1; higher versions are refused
  with an upgrade hint.

Unknown fields are refused: new fields arrive with a new API version.

## Transport forms

Entry point (the richer form; requires the plugin importable in *wtm*'s own
environment):

```toml
# pyproject.toml of the plugin package
[project.entry-points."wtm.plugins"]
wtm-git = "wtm_git.plugin:manifest"   # a mapping or a zero-arg callable returning one
```

Command form (works across virtualenvs; keeps the plugin standalone):

```python
@app.command("wtm-manifest", hidden=True)
def wtm_manifest() -> None:
    """Emit the wtm plugin manifest as JSON."""
    print(json.dumps({...}))
```

## Firing order

Plugin commands form a sixth tier alongside the five scopes: **first** on
setup events, **last** on teardown (a plugin is machine-global like the system
scope). `wtm hook list` renders them per event as `(plugin:<name>)` rows, and
`wtm hook run <event>` fires them too.

## Management

```sh
wtm plugin add NAME [--seed] [-s SCOPE]   # register; warns (but registers) if unresolvable
wtm plugin remove NAME                    # clear the most-local activation cell
wtm plugin list [--json]                  # status, source, events; (*) = active here
wtm plugin show NAME                      # manifest + registration sites
```

An unresolvable registration stays dormant: every invocation warns and skips
it until the plugin appears (or you remove the cell). `post-config` fires
after the registration write, so a freshly added plugin's own `post-config`
hook sees its registration immediately.

## Authoring checklist

1. Ship a manifest over either transport, named exactly like your tool.
2. Keep hook commands env-driven (read `WTM_EVENT`, `$1`) and fast; failures
   are warnings unless the operation runs strict.
3. Declare your namespace(s) so users get attribution hints, and declare
   inheritable keys instead of writing `inheritable` entries imperatively.
4. Prefer `defaults` + `--seed` over writing config from your installer.
