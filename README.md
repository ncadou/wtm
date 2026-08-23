# wtm — a git worktree manager

If you're not working with git worktrees yet, you probably should. They let you
have multiple branches checked out at once, making it easy to switch between them
without stashing or juggling a single working directory.

But worktree management is a chore in itself. The `git worktree` CLI interface is a
bit cumbersome to use. And even when you have your worktree, is your project
functional? Most likely, you need to run some setup commands to get your environment
ready, maybe copy some files in it. This things are entirely outside of git's scope,
so you have to remember to do them every time you create a new worktree.

`wtm` turns worktrees into a smooth daily workflow: a single command creates a
worktree, puts it on the right branch, cds your shell into it, and fires
whatever setup you've wired up.

Then it scales the idea outward with workspaces:

- Many worktrees per repo, plus whatever data directories, scripts and whatnot,
  and it's not just a repo anymore, it's a *project*.
- Many projects side by side, that's a *workspace*. You can set up multiple
  workspaces, if you want to keep, say, work and side projects apart.

## Main Concepts

`wtm` is built around four ideas, each wrapping the previous one:

1. **Working with code** happens in a *checkout*: a directory of files sitting
   on one branch. That's the thing you actually edit, build, and test.

2. A **worktree** is one such checkout. The point of `wtm` is that a single
   repository can have *many* worktrees at once — one per branch you care about
   — living side by side in their own directories. No stashing, no
   checkout-dance: to switch tasks you just `cd` to another worktree.

3. A **project** is a repository together with all of its worktrees, laid out
   tidily on disk: a bare `.git` (the shared git data, with no working files of
   its own) plus a `worktrees/` directory holding the checkouts.

  - You can also add anything else you want to the project: a `data/` directory,
   scripts, etc. Anything outsite of `.git` and `worktrees/` is just a normal file or
   directory, completely ignored by git, they won't even appear as untracked files in
   `git status`, and `wtm` won't care about them either.

   ```
   <project>/
       .git/                 bare repository (shared history, no checkout)
       worktrees/
           main/             a worktree on branch 'main'
           feature-x/        a worktree on branch 'feature-x'
       my-stuff/             any file or directory you want to add to the project
       ...
   ```

4. A **workspace** is a named home for a bunch of projects — a *projects root*.
   You might have one workspace for work and another for open source. Every
   `wtm` command runs against one **active workspace**, so your projects stay
   grouped and you can jump between collections deliberately.

   ```
   <workspace>/              e.g. ~/work
       api/                  a project
           .git/
           worktrees/...
           .envrc
       web/                  another project
           .git/
           worktrees/...
   ```

Workspaces are the only thing you *declare* (in config). Projects are simply
*discovered* on disk — any `<name>/.git` under a workspace is a project — and
config only annotates them. `wtm` itself knows only this structure; anything
project-specific (shared data dirs, `direnv`, tmux/`sesh`) lives in
[hooks](docs/hooks.md), not in `wtm`.

## Install

Requirements:

- **git ≥ 2.43** (needed to bootstrap a commit-less project, which uses
  `git worktree add --orphan`)
- Python **3.11+**

Install the CLI with whichever tool you prefer:

```sh
# uv (recommended)
uv tool install git+https://.../wtm      # or a local path: uv tool install ./wtm

# pipx
pipx install git+https://.../wtm

# pip
pip install git+https://.../wtm
```

This puts a `wtm` on your `PATH`. To let `wtm enter`, `wtm create -c`, and
`wtm leave` actually change your shell's directory and environment, source the
shell wrapper from your shell rc (`~/.zshrc`, `~/.bashrc`, ...):

```sh
eval "$(wtm shell-init)"                    # emit it from the installed wtm

. /path/to/wtm/src/wtm/shell/wtm.sh          # ... or source it by path (from a clone)
```

It defines a `wtm` shell function. Without it, `wtm` can't move the parent shell — it
will just print the target directory for you to `cd` yourself. See
[docs/shell-integration.md](docs/shell-integration.md) for how it works.

Optionally, install shell completions (Typer-generated):

```sh
wtm --show-completion            # prints the snippet for your shell; add it to your rc

eval "$(wtm --show-completion)"  # ... or source it directly from your shell rc
```

## Getting started

The whole daily loop, end to end.

**1. Declare a workspace** — a directory that will hold your projects:

```sh
wtm workspace add work ~/work        # register '~/work' as the 'work' workspace
wtm workspace default work           # make it the default active workspace
```

**2. Get a project into it** — clone an existing repo (bare, into the layout),
or start an empty one:

```sh
wtm clone git@github.com:me/api.git  # -> ~/work/api/.git + worktrees/
wtm project new scratch              # an empty project
```

Already have a plain clone you want to fold in? See
[docs/import.md](docs/import.md).

**3. Create a worktree and jump into it** — this is the command you'll run most:

```sh
wtm create -c api feature-login      # new branch 'feature-login', cd into it
```

`create` figures out the branch for you: an existing local branch is reused, an
`origin/<name>` is tracked, otherwise a new branch is forked off the project's
base. `-c` cds you in; add `-i` to run install hooks (dependency setup, etc.).

**4. Work, then move on.** Switch tasks by entering another worktree:

```sh
wtm enter api hotfix               # jump to the 'hotfix' worktree
wtm list api                        # what worktrees does 'api' have?
```

**5. Tear down** when you're done:

```sh
wtm leave                            # run leave hooks, cd back out
wtm delete api feature-login -D      # remove the worktree and its branch
```

## Scenarios

**A feature and an urgent hotfix, in parallel.** You're deep in a feature when a
production bug lands. No stashing:

```sh
wtm create -c api feature-billing    # working here...
# ...bug report arrives...
wtm create -c api hotfix --off main  # fresh checkout off main, in its own dir
# fix, commit, push
wtm enter api feature-billing        # back to the feature, exactly as you left it
```

**Per-worktree setup with hooks.** Give every new worktree a ready-to-go
environment by wiring commands to lifecycle events in config:

```toml
[workspaces.work.projects.api.hooks.on]
post-create = "python -m venv .venv"
install     = "uv sync"
enter       = "direnv allow"
```

Now `wtm create -i -c api feature-x` creates the worktree, builds its `.venv`,
installs deps, and cds you in with `direnv` active. See
[docs/hooks.md](docs/hooks.md) for the full event list and firing order.

**Keeping worlds apart.** Declare more than one workspace and switch between
them:

```sh
wtm workspace add oss ~/oss
wtm clone git@github.com:someone/tool.git oss tool   # into workspace 'oss' as project 'tool'
wtm workspace list                   # '*' marks the active one
```

## Command overview

Commands are grouped; each worktree verb also has a top-level alias
(`wtm create ...` == `wtm worktree create ...`).

- **worktree** — `create`, `enter`, `leave`, `delete`, `rename`, `list`,
  `resolve` (day-to-day work)
- **project** — `new`, `clone`, `import`, `list`, `resolve`
- **workspace** — `add`, `set`, `rename`, `delete`, `default`, `list`, `show`,
  `resolve`, `enter`, `leave`
- **hook** — `install`, `run`, `delete`, `list`
- **config** — `show`

Run `wtm --help` or `wtm COMMAND --help` for authoritative, always-current
usage.

## Documentation

- [docs/commands.md](docs/commands.md) — every command and flag, plus the
  worktree **targeting grammar** and `create`/`resolve`/`rename` rules
- [docs/hooks.md](docs/hooks.md) — the eleven lifecycle events, five scopes,
  firing order, and the `WTM_*` contract
- [docs/configuration.md](docs/configuration.md) — config scopes, resolution
  precedence, the TOML schema, and environment variables
- [docs/import.md](docs/import.md) — folding an existing clone into the layout
- [docs/shell-integration.md](docs/shell-integration.md) — the shell wrapper and
  the session-contract model

## Development

`wtm` uses [`uv`](https://docs.astral.sh/uv/) for dependency management and
[Ruff](https://docs.astral.sh/ruff/) for formatting and linting. The `Makefile`
wraps the common tasks:

- `make reformat` — apply safe autofixes and format (`ruff check --fix` +
  `ruff format`)
- `make lint` — lint (`ruff check`)
- `make check` — verify formatting + lint without writing (CI-style gate)
- `make tests` — run the test suite (`pytest`)

Ruff config lives in `pyproject.toml` (line length 99, target Python 3.11).

## License

`wtm` is licensed under the [GNU Lesser General Public License v2.1 or
later](COPYING.LESSER) — you may use, modify, and redistribute it under
the terms of LGPL-2.1 or, at your option, any later version published by
the Free Software Foundation. The incorporated GPLv2 text ships as
[COPYING](COPYING). There is no warranty; see the license texts for
details.
