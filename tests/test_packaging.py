"""Packaging-level guarantees: the config library imports without the CLI stack."""

from __future__ import annotations

import subprocess
import sys

import pytest


def _assert_import_clean(code: str) -> None:
    """Run *code* in a fresh interpreter and fail on error or a leaked typer import."""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"import check failed:\n{result.stderr}")


def test_import_wtm_config_does_not_pull_typer() -> None:
    """``wtm.config`` is usable as a library: no CLI (typer) on import."""
    _assert_import_clean(
        "import sys\n"
        "import wtm.config\n"
        "assert 'typer' not in sys.modules, 'typer leaked into wtm.config import'\n"
    )


def test_import_wtm_root_does_not_pull_typer() -> None:
    """The lazy package root defers ``wtm.cli`` until an entry point is touched."""
    _assert_import_clean(
        "import sys\n"
        "import wtm\n"
        "assert 'typer' not in sys.modules, 'typer leaked into bare wtm import'\n"
    )


def test_lazy_entry_points_resolve() -> None:
    """``wtm.app`` / ``wtm.main`` still resolve through the lazy ``__getattr__``."""
    _assert_import_clean("import wtm; assert callable(wtm.app); assert callable(wtm.main)\n")
