#!/usr/bin/env python3
"""Wave 2 contract: unifable_runtime stays a host-agnostic shared layer.

Dependency direction is one-way: host adapters (`hooks/`, and the Claude/Codex
IO living in `scripts/gate/`) import FROM `unifable_runtime`; nothing inside
`unifable_runtime` imports back into a host layer. This keeps the synced runtime
self-contained and prevents a circular adapter<->core coupling that would brick
`~/.unifable/current` when a host-only module is absent.

The test AST-scans every module under unifable_runtime/ for imports of the
forbidden host packages. A synthetic violation fixture proves the scanner trips.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "unifable_runtime"
sys.path.insert(0, str(REPO))

# Top-level module names a shared runtime module must never import. `hooks` is
# the host dispatch layer; `gate` is the Claude/Codex adapter package under
# scripts/gate. Shared code depends on neither.
FORBIDDEN_TOP_LEVEL = {"hooks", "gate"}


def _imported_top_levels(source: str) -> set[str]:
    """Top-level module names referenced by import / from-import in `source`."""
    tops: set[str] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                tops.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # level>0 is a relative import; ban-relative-imports already forbids
            # those repo-wide, but a relative import can't reach a host layer.
            if node.level == 0 and node.module:
                tops.add(node.module.split(".")[0])
    return tops


def _pkg_modules() -> list[Path]:
    return sorted(PKG.rglob("*.py"))


def test_package_exists_and_has_modules():
    assert PKG.is_dir(), "unifable_runtime package directory missing"
    assert _pkg_modules(), "expected at least one module under unifable_runtime/"


def test_no_module_imports_a_host_layer():
    offenders: list[str] = []
    for mod in _pkg_modules():
        tops = _imported_top_levels(mod.read_text(encoding="utf-8"))
        bad = tops & FORBIDDEN_TOP_LEVEL
        if bad:
            offenders.append(f"{mod.relative_to(REPO)}: imports {sorted(bad)}")
    assert not offenders, "host-layer imports inside unifable_runtime:\n" + "\n".join(offenders)


def test_scanner_trips_on_a_synthetic_violation(tmp_path):
    # Guard against a no-op scanner: a module that imports a host layer must be caught.
    bad = "from hooks import pre_tool_use\nimport gate.ledger\n"
    assert _imported_top_levels(bad) & FORBIDDEN_TOP_LEVEL == {"hooks", "gate"}

    clean = "import sys\nfrom pathlib import Path\nimport unifable_runtime\n"
    assert not (_imported_top_levels(clean) & FORBIDDEN_TOP_LEVEL)
