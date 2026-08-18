# Importing an existing clone

`wtm import` folds a legacy plain clone (the *source*) into the bare-repo +
worktrees layout (the *target*), carrying over remotes, references, tags, and
the live working checkout (modified + untracked files) recreated as a worktree.
It automates the manual clone/push recipe. The import is non-destructive: the
source is left intact (only a temporary push remote is added and removed).

## Where to run it

Run it from inside the source repo (the target project name defaults to the
source's basename) or from under the projects root (the source is the lone
positional or `--source`). `--source PATH` / `--project NAME` override.

## Creating the target

When the target project is absent, one of three create methods produces the bare
repo:

- default: bare-clone the source **on disk** (fast, offline, hard-linked), then
  repoint `origin` at the source's real origin URL
- `--from-origin`: bare-clone the origin URL over the network (full upstream)
- `--init-bare`: an empty bare repo (references then come from the push)

The bare repo always gets
`remote.origin.fetch=+refs/heads/*:refs/remotes/origin/*`, the dual-ref layout
`create`'s branch resolution relies on. `--fetch` runs a final `git fetch
origin` afterwards to populate `refs/remotes/origin/*` and `origin/HEAD`.

A target whose `.git` already exists is refused unless `--update` is passed
(importing into it mutates a project that may already be in active use). With
`--update`, references are imported by pushing from the source:
fast-forward-only by default (rejected branches are reported; branches checked
out in a target worktree are skipped), or `--force` to overlay.

## Recreating the worktree

The worktree is recreated on the source's current branch (named after it; pass
`--name` for a detached HEAD) and its working tree is mirrored with `rsync
--archive --delete` (uncommitted deletions carry over; `.git` is always
excluded). Git-ignored files are copied **by default**; with no `--filter-file`
that includes heavy artefacts (`.venv`, `node_modules`, …), so pass
`--filter-file PATH` (rsync filter), `--exclude PATTERN`, or `--no-ignored`.

### `--all-worktrees`

`--all-worktrees` reproduces the source's **whole worktree set** instead of just
the current checkout: every linked worktree the source has is recreated, each
named after its **source directory basename** (the main worktree included, so
its target name is the clone's dirname, not its branch) and placed on its own
branch. A detached worktree is reproduced by pinning a branch (named after the
source dir) at its exact commit. Worktrees whose target name already exists, or
whose branch is already checked out in the target, are **skipped with a
warning** and the rest still attempted; a hard failure on one worktree is
reported and the run exits non-zero, but the others are still created. Prunable
(missing-on-disk) source worktrees are skipped; locked ones are included.
`-c/--cd` lands in the worktree matching the one you invoked from, else the main
one. `--all-worktrees` is mutually exclusive with `--name` and `--no-worktree`.

## Toggling phases

Phases are toggleable: `--no-remotes`, `--no-refs`, `--no-worktree`,
`--no-tags`, `--no-ignored`; `--fetch`, `--force`, and `--update` are opt-in.
`-n/--dry-run` prints the plan and a per-worktree `rsync -n` preview with no side
effects; `-c/--cd` drops you into the new worktree.
