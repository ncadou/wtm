# Importing an existing clone

`wtm import` folds a legacy plain clone (the *source*) into the bare-repo +
worktrees layout (the *target*), migrating an existing checkout into a
workspace. It carries over remotes, references, tags, and the live working
checkout (modified + untracked files) recreated as a worktree, automating the
manual clone/push recipe. The import is non-destructive: the source is left
intact (only a temporary push remote is added and removed).

## Quick start

Standing in the clone you want to migrate:

```console
$ cd ~/src/myapp
$ wtm import -c
```

The target project name defaults to the source's basename. This creates, under
the active workspace's projects root:

```
myapp/
├── .git/                  bare repo (bare-cloned from the source on disk)
└── worktrees/
    └── main/              your checkout: current branch, modified +
                          untracked files, git-ignored files included
```

`-c/--cd` drops your shell into the new worktree. Re-run any invocation with
`-n/--dry-run` first to preview the plan and a per-worktree rsync preview with
no side effects.

## Where to run it

Run it from inside the source repo (as above) or from under the projects root
(the source is then the lone positional or `--source`). `--source PATH` /
`--project NAME` override either inference.

## Creating the target

When the target project is absent, one of three create methods produces the
bare repo:

- default: bare-clone the source **on disk** (fast, offline, hard-linked), then
  repoint `origin` at the source's real origin URL. This clone already carries
  the source's local branches and tags, so references are not pushed.
- `--from-origin`: bare-clone the origin URL over the network (full upstream);
  references are then pushed over from the source
- `--init-bare`: an empty bare repo; references come entirely from the push

`--from-origin` and `--init-bare` are mutually exclusive.

The bare repo always gets
`remote.origin.fetch=+refs/heads/*:refs/remotes/origin/*` — the dual-ref layout
[create's branch resolution](commands.md#creates-branch-resolution) relies on.
`--fetch` runs a final `git fetch origin` afterwards to populate
`refs/remotes/origin/*` and `origin/HEAD`; it needs the source to have an
`origin` remote (without one it is skipped silently).

A target whose `.git` already exists is refused unless `--update` is passed
(importing into it mutates a project that may already be in active use). With
`--update`, references are imported by pushing from the source:
fast-forward-only by default (rejected branches are reported; branches checked
out in a target worktree are skipped), or `--force` to overlay.

## Recreating the worktree

The worktree is recreated on the source's current branch (named after it) and
its working tree is mirrored with `rsync --archive --delete` (uncommitted
deletions carry over; `.git` is always excluded). A detached HEAD has no branch
to name the worktree after: pass `--name NAME` or the import fails.

Git-ignored files are copied **by default**; with no `--filter-file` that
includes heavy artefacts (`.venv`, `node_modules`, …), so pass
`--filter-file PATH` (rsync filter), `--exclude PATTERN`, or `--no-ignored`.

### `--all-worktrees`

`--all-worktrees` reproduces the source's **whole worktree set** instead of
just the current checkout:

- every linked worktree the source has is recreated, named after its **source
  directory basename**, each placed on its own branch (the main worktree
  included, so its target name is the clone's dirname, not its branch)
- a detached worktree is reproduced by pinning a branch (named after the
  source dir) at its exact commit — no `--name` needed
- a worktree whose target name already exists, or whose branch is already
  checked out in the target, is **skipped with a warning**; the rest are still
  attempted
- a hard failure on one worktree is reported and the run exits non-zero; the
  other worktrees are still created
- prunable (missing-on-disk) source worktrees are skipped; locked ones are
  included

`-c/--cd` lands in the worktree matching the one you invoked from, else the
main one. `--all-worktrees` is mutually exclusive with `--name` and
`--no-worktree`.

## Hooks

Each recreated worktree fires the `pre-create` and `post-create` events; the
`install` event never fires during an import, and `-c/--cd` emits shell
directives without firing `enter`. `--strict` makes a hook failure fatal. See
[hooks.md](hooks.md).

## Options

| option | effect |
|---|---|
| `[SOURCE\|PROJECT]` | the side not inferred from the cwd: target PROJECT standing in the source, else SOURCE path |
| `-s`, `--source PATH` | source repo path (default: inferred) |
| `-p`, `--project NAME` | target project name (default: inferred) |
| `-o`, `--from-origin` | create the target by cloning the origin URL (network) |
| `-i`, `--init-bare` | create the target as an empty bare repo (offline) |
| `-f`, `--fetch` | fetch origin afterwards (populates `origin/*` + HEAD) |
| `-u`, `--update` | allow importing into an existing target |
| `--force` | force-push references into an existing target |
| `-a`, `--all-worktrees` | reproduce every linked source worktree |
| `--name NAME` | worktree name (required for a detached HEAD) |
| `--no-remotes` | skip remote import |
| `--no-refs` | skip pushing references |
| `--no-worktree` | skip recreating the working checkout |
| `--no-tags` | skip pushing tags |
| `--no-ignored` | drop git-ignored files from the worktree copy |
| `--exclude PATTERN` | rsync exclude pattern (repeatable) |
| `--filter-file PATH` | rsync filter file to merge (repeatable) |
| `-n`, `--dry-run` | print the plan and an rsync preview; no changes |
| `-c`, `--cd` | cd into the new worktree |
| `--strict` | hook failures are fatal |
