"""Thin git subprocess wrappers used by wtm.

Read helpers return parsed strings/paths; mutating helpers (``worktree add``,
``worktree remove``, ``branch -D``, ``init``, ``clone``) stream git's own output
and raise :class:`GitError` on failure.
"""

from __future__ import annotations

__all__ = [
    "GitError",
    "ORIGIN_FETCH_REFSPEC",
    "WorktreeEntry",
    "add_remote",
    "add_worktree",
    "branch_exists",
    "clone",
    "commit_exists",
    "common_dir",
    "current_branch",
    "delete_branch",
    "fetch",
    "init",
    "is_repo",
    "list_remotes",
    "push_refs",
    "ref_exists",
    "remote_url",
    "remove_remote",
    "remove_worktree",
    "set_config_fetch",
    "set_head",
    "set_remote_url",
    "toplevel",
    "worktree_branches",
    "worktree_entries",
]

import subprocess
from dataclasses import dataclass
from pathlib import Path

# The dual-ref layout wtm relies on: upstream heads land in refs/remotes/origin/*
# (not refs/heads/*), keeping local refs/heads/* free for worktree branches.
ORIGIN_FETCH_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"


class GitError(Exception):
    """A git invocation exited non-zero."""


def _run(
    cwd: str | Path | None, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run ``git [-C cwd] <args>`` capturing output."""
    cmd = ["git"]
    if cwd is not None:
        cmd += ["-C", str(cwd)]
    cmd += list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise GitError(message or f"git {' '.join(args)} failed")

    return result


def _stream(cwd: str | Path | None, *args: str) -> None:
    """Run a mutating git command, letting its stdout/stderr pass through."""
    cmd = ["git"]
    if cwd is not None:
        cmd += ["-C", str(cwd)]
    cmd += list(args)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise GitError(f"git {args[0]} failed (exit {result.returncode})")


# --- read helpers ------------------------------------------------------------


def is_repo(directory: str | Path) -> bool:
    """Return whether *directory* is inside a git repository."""
    return _run(directory, "rev-parse", "--git-dir", check=False).returncode == 0


def toplevel(directory: str | Path) -> Path | None:
    """Return the worktree top level, or ``None`` if not resolvable."""
    result = _run(directory, "rev-parse", "--show-toplevel", check=False)
    if result.returncode != 0:
        return None
    out = result.stdout.strip()

    return Path(out) if out else None


def common_dir(directory: str | Path) -> Path:
    """Return the absolute git common dir."""
    result = _run(directory, "rev-parse", "--path-format=absolute", "--git-common-dir")

    return Path(result.stdout.strip())


def current_branch(directory: str | Path) -> str:
    """Return the current short branch name, or '' when detached/unborn."""
    result = _run(directory, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)

    return result.stdout.strip() if result.returncode == 0 else ""


def ref_exists(repo_dir: str | Path, ref: str) -> bool:
    """Return whether *ref* exists in *repo_dir*."""
    return _run(repo_dir, "show-ref", "--verify", "--quiet", ref, check=False).returncode == 0


def commit_exists(repo_dir: str | Path, ref: str) -> bool:
    """Return whether *ref* resolves to a commit in *repo_dir*.

    False for an unborn ``HEAD`` (a fresh, commit-less repository), which is the
    signal to bootstrap the first worktree on an orphan branch.
    """
    return (
        _run(
            repo_dir,
            "rev-parse",
            "--verify",
            "--quiet",
            f"{ref}^{{commit}}",
            check=False,
        ).returncode
        == 0
    )


def branch_exists(repo_dir: str | Path, branch: str) -> bool:
    """Return whether a local branch *branch* exists in *repo_dir*."""
    return ref_exists(repo_dir, f"refs/heads/{branch}")


def list_remotes(repo_dir: str | Path) -> dict[str, str]:
    """Return a mapping of remote name to fetch URL for *repo_dir*."""
    result = _run(repo_dir, "remote", "-v")
    remotes: dict[str, str] = {}
    for line in result.stdout.splitlines():
        # Lines look like "<name>\t<url> (fetch|push)"; keep the fetch URLs.
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "(fetch)":
            remotes[parts[0]] = parts[1]

    return remotes


def remote_url(repo_dir: str | Path, name: str) -> str | None:
    """Return the fetch URL of remote *name*, or ``None`` if it has none."""
    result = _run(repo_dir, "remote", "get-url", name, check=False)
    if result.returncode != 0:
        return None
    out = result.stdout.strip()

    return out or None


def worktree_branches(repo_dir: str | Path) -> set[str]:
    """Return the set of branch names currently checked out in any worktree.

    Parses ``git worktree list --porcelain`` ``branch refs/heads/<name>`` lines;
    such branches cannot be pushed to (git refuses to update a checked-out
    branch), so callers skip them when importing references.
    """
    result = _run(repo_dir, "worktree", "list", "--porcelain")
    prefix = "branch refs/heads/"

    return {line[len(prefix) :] for line in result.stdout.splitlines() if line.startswith(prefix)}


@dataclass(frozen=True)
class WorktreeEntry:
    """A single worktree as reported by ``git worktree list --porcelain``."""

    path: Path
    head: str  # the checked-out commit sha ('' for a bare entry)
    branch: str | None  # short branch name, or None when detached/bare
    bare: bool
    detached: bool
    locked: bool
    prunable: bool


def worktree_entries(repo_dir: str | Path) -> list[WorktreeEntry]:
    """Return the structured worktree set registered in *repo_dir*.

    Parses the per-worktree blocks of ``git worktree list --porcelain``: each
    block opens with a ``worktree <path>`` line and is terminated by a blank
    line, carrying ``HEAD``, ``branch``/``detached``, ``bare``, ``locked``, and
    ``prunable`` attributes. The bare repository itself is included by git;
    callers filter it by the ``bare`` flag as needed.
    """
    result = _run(repo_dir, "worktree", "list", "--porcelain")
    entries: list[WorktreeEntry] = []
    path: Path | None = None
    head = ""
    branch: str | None = None
    bare = detached = locked = prunable = False

    def _flush() -> None:
        if path is not None:
            entries.append(
                WorktreeEntry(
                    path=path,
                    head=head,
                    branch=branch,
                    bare=bare,
                    detached=detached,
                    locked=locked,
                    prunable=prunable,
                )
            )

    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            _flush()
            path = Path(line[len("worktree ") :])
            head = ""
            branch = None
            bare = detached = locked = prunable = False
        elif line.startswith("HEAD "):
            head = line[len("HEAD ") :]
        elif line.startswith("branch refs/heads/"):
            branch = line[len("branch refs/heads/") :]
        elif line == "bare":
            bare = True
        elif line == "detached":
            detached = True
        elif line.startswith("locked"):
            locked = True
        elif line.startswith("prunable"):
            prunable = True
    _flush()

    return entries


# --- mutating helpers --------------------------------------------------------


def init(directory: str | Path) -> None:
    """Initialise a new bare repository at *directory*."""
    _stream(None, "init", "-q", "--bare", "--", str(directory))


def clone(url: str, dest: str | Path) -> None:
    """Clone *url* into *dest* as a bare repository.

    Sets ``remote.origin.fetch`` to :data:`ORIGIN_FETCH_REFSPEC` so that future
    fetches populate ``refs/remotes/origin/*`` rather than ``refs/heads/*``,
    yielding the dual-ref layout wtm's worktree branch resolution relies on.
    """
    _stream(
        None,
        "clone",
        "--bare",
        "-c",
        f"remote.origin.fetch={ORIGIN_FETCH_REFSPEC}",
        "--",
        url,
        str(dest),
    )


def add_worktree(
    repo_dir: str | Path,
    dest: str | Path,
    *,
    commitish: str | None = None,
    new_branch: str | None = None,
    track: str | None = None,
    orphan: bool = False,
) -> None:
    """Add a worktree.

    - ``commitish`` only: check out an existing branch/commit.
    - ``new_branch`` + ``commitish``: create a new branch off *commitish*.
    - ``new_branch`` + ``track``: create a tracking branch off *track*.
    - ``new_branch`` + ``orphan``: create an empty worktree on a new orphan
      branch (bootstrap a commit-less repository; requires git >= 2.43).
    """
    args = ["worktree", "add"]
    if orphan:
        args += ["--orphan", "-b", new_branch, "--", str(dest)]  # type: ignore[list-item]
    elif track is not None:
        args += ["--track", "-b", new_branch, "--", str(dest), track]  # type: ignore[list-item]
    elif new_branch is not None:
        args += ["-b", new_branch, "--", str(dest), commitish or "HEAD"]
    else:
        args += ["--", str(dest), commitish or "HEAD"]
    _stream(repo_dir, *args)


def remove_worktree(repo_dir: str | Path, worktree: str | Path, *, force: bool) -> None:
    """Remove a worktree, optionally forcing past local changes."""
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args += ["--", str(worktree)]
    _stream(repo_dir, *args)


def delete_branch(repo_dir: str | Path, branch: str) -> None:
    """Force-delete a branch."""
    _stream(repo_dir, "branch", "-D", branch)


def add_remote(repo_dir: str | Path, name: str, url: str) -> None:
    """Add remote *name* pointing at *url*."""
    _stream(repo_dir, "remote", "add", name, url)


def set_remote_url(repo_dir: str | Path, name: str, url: str) -> None:
    """Set the URL of remote *name*."""
    _stream(repo_dir, "remote", "set-url", name, url)


def set_config_fetch(repo_dir: str | Path, name: str = "origin") -> None:
    """Set ``remote.<name>.fetch`` to :data:`ORIGIN_FETCH_REFSPEC`."""
    _stream(repo_dir, "config", f"remote.{name}.fetch", ORIGIN_FETCH_REFSPEC)


def remove_remote(repo_dir: str | Path, name: str) -> None:
    """Remove remote *name* (no error if it is absent)."""
    _run(repo_dir, "remote", "remove", name, check=False)


def push_refs(
    repo_dir: str | Path,
    remote: str,
    *,
    all_branches: bool = False,
    tags: bool = False,
    force: bool = False,
    refspec: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Push branches and/or tags from *repo_dir* to *remote*.

    Captures output (rather than streaming) so the caller can report which refs
    were rejected. Without *force* the push is fast-forward-only; rejected refs
    surface in the returned process's stderr. Returns the completed process; a
    non-zero exit with only fast-forward rejections is not raised here so the
    caller can summarise rejects instead of aborting.

    An explicit *refspec* (e.g. ``<sha>:refs/heads/<name>``) pushes that single
    refspec, transferring the object and creating the ref in one move; used to
    pin a branch at a detached worktree's commit, which no ``--all`` push would
    carry (the commit is on no branch).
    """
    args = ["push"]
    if force:
        args.append("--force")
    if all_branches:
        args.append("--all")
    if tags:
        args.append("--tags")
    args.append(remote)
    if refspec is not None:
        args.append(refspec)

    return _run(repo_dir, *args, check=False)


def fetch(repo_dir: str | Path, remote: str) -> None:
    """Fetch *remote* (populating refs/remotes/<remote>/* via its refspec)."""
    _stream(repo_dir, "fetch", remote)


def set_head(repo_dir: str | Path, remote: str) -> None:
    """Set refs/remotes/<remote>/HEAD by auto-detecting the remote's default."""
    _run(repo_dir, "remote", "set-head", remote, "--auto", check=False)
