"""Smoke test: every module under ``src/`` must import cleanly.

This is the guard that closes the gap a gate found once — a SyntaxError in the entrypoint
slipped through while the rest of the suite (which never imported it) stayed green. Importing
each module exercises compilation + module-level code, so a non-importable module can never
pass the suite again.
"""

import importlib
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "src"

# Every top-level module file under src/ (skip __init__/dunder helpers).
_MODULES = sorted(p.stem for p in SRC_DIR.glob("*.py") if not p.stem.startswith("_"))


def test_src_has_modules():
    """Sanity: we actually discovered something to import (guards an empty glob)."""
    assert _MODULES, f"No importable modules found under {SRC_DIR}"


@pytest.mark.parametrize("module_name", _MODULES)
def test_module_imports(module_name):
    """Importing the module must not raise (catches SyntaxError, bad imports, etc.)."""
    importlib.import_module(module_name)
