"""wtm command-line interface (typer).

A thin typer adapter: each command parses arguments and forwards to the
CLI-agnostic operations in :mod:`wtm.project` / :mod:`wtm.worktree`. All
diagnostics go to stderr; stdout carries only path output (``resolve`` and the
``cd`` fallback) so it stays clean for command substitution.
"""

from __future__ import annotations

__all__ = ["app", "main"]

from wtm.cli.app import app, main
