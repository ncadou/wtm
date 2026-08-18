# Shell integration

`wtm` writes diagnostics to stderr and emits shell directives out-of-band so the
calling shell can follow a move. The
[`src/wtm/shell/wtm.sh`](../src/wtm/shell/wtm.sh) wrapper
provides this: it passes a `WTM_SHELL_FILE` temp file to the real binary and
*sources* whatever the command writes there. The directives are a `cd` plus the
`export`/`unset` lines that sync the **`WTM_*` session contract** (below). This
is what makes `wtm enter` and `wtm create -c` change the current shell's
directory *and* environment.

Source it from your shell rc:

```sh
eval "$(wtm shell-init)"                    # emit it from the installed wtm

. /path/to/wtm/src/wtm/shell/wtm.sh          # ... or by path (from a clone)
```

`wtm shell-init` prints the same wrapper straight from the installed package, so
it works without a checkout on disk (handy for `git+`/`pipx`/`pip` installs).

Without the wrapper the commands stay silent about the environment (they cannot
alter the parent shell) and print a manual-`cd` notice for the target directory
only.

## The session model

The contract follows a **session** model: entering sets it, leaving clears it.

- `enter` / `create -c` / `import -c` — `cd` into the worktree and export the
  full contract: `WTM_WORKTREE_DIR`, `WTM_BRANCH`, `WTM_PROJECT`,
  `WTM_PROJECT_DIR`, `WTM_WORKTREES_DIR`, `WTM_GIT_COMMON_DIR`, `WTM_WORKSPACE`,
  `WTM_WORKSPACE_DIR`.
- `rename` (when you are standing in the moved worktree) — follow it and update
  `WTM_WORKTREE_DIR` / `WTM_BRANCH`.
- `leave` — `cd` out (`$OLDPWD` or the project dir) and **unset** the whole
  contract. `-C`/`--no-cd` keeps you put but still unsets (leaving ends the
  session regardless of whether you also move).
- `delete` (when you are standing in the doomed worktree) — rescue-`cd` out and
  unset the contract.
- `project enter` — `cd` into the project dir and export the project-level
  contract (`WTM_PROJECT`, `WTM_PROJECT_DIR`, `WTM_WORKTREES_DIR`,
  `WTM_GIT_COMMON_DIR`, `WTM_WORKSPACE`, `WTM_WORKSPACE_DIR`; the two worktree
  vars `WTM_WORKTREE_DIR`/`WTM_BRANCH` are unset). `project leave` `cd`s out
  (`$OLDPWD`, the projects root when it cascaded out of a worktree, or stays put)
  and unsets everything (even with `-C`).
- `workspace enter` — `cd` into the projects root and export only
  `WTM_WORKSPACE` / `WTM_WORKSPACE_DIR` (the six worktree/project vars are
  unset). `workspace leave` unsets everything (even with `-C`).

Because `WTM_WORKSPACE` is exported on `enter`, it **pins the active workspace**
(it beats cwd inference) until you `wtm leave` — the session is deliberately
sticky. Entering a worktree in another workspace by its leading `WORKSPACE`
positional (`wtm enter OTHER proj name`) re-pins the session to that workspace.
