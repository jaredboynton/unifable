#!/usr/bin/env python3
"""unifable_runtime — shared, host-agnostic Python implementation modules.

This package holds the feature implementations (unitrace enhance-prompt, search,
map, trace, websearch; Unifusion; the Realtime transport; the daemon) that used
to live as Node/Bun scripts under `skills/*/scripts/`. Host adapters under
`hooks/` and `scripts/gate/` import from here; nothing in here imports back into
a host layer (enforced by tests/test_import_boundaries.py).

It resolves identically in two layouts:
  - the repo checkout (`<repo>/unifable_runtime/`)
  - the synced stable runtime (`~/.unifable/current/unifable_runtime/`)
because runtime_sync copies the package into each version dir alongside the
other runtime trees.

The Python interpreter contract (3.12+) is asserted by `require_supported_python`
and surfaced through `PYTHON_VERSION_ERROR` so every entrypoint — setup, runtime
sync, hook dispatch, compat shims — rejects an unsupported interpreter with one
identical message.
"""

from __future__ import annotations

import sys

# Single supported implementation runtime. pyproject pins py312; the launchers,
# setup preflight, and hook dispatch all gate on this same tuple.
MIN_PYTHON: tuple[int, int] = (3, 12)

# One exact message string shared across every entrypoint so the version error
# never differs by launcher (pinned by tests/test_python_version_contract.py).
PYTHON_VERSION_ERROR = (
    "unifable requires Python {min_major}.{min_minor}+ "
    "(found {cur_major}.{cur_minor}). Install a supported interpreter and retry."
)


def python_version_error(version: tuple[int, int] | None = None) -> str:
    """Render the canonical unsupported-interpreter message for `version`."""
    cur = version or sys.version_info[:2]
    return PYTHON_VERSION_ERROR.format(
        min_major=MIN_PYTHON[0],
        min_minor=MIN_PYTHON[1],
        cur_major=cur[0],
        cur_minor=cur[1],
    )


def is_supported_python(version: tuple[int, int] | None = None) -> bool:
    """True when `version` (default: this interpreter) meets the 3.12+ contract."""
    cur = version or sys.version_info[:2]
    return cur >= MIN_PYTHON


def require_supported_python(version: tuple[int, int] | None = None) -> None:
    """Exit 1 with the canonical message when the interpreter is unsupported.

    Entrypoints call this before doing real work so an old interpreter fails
    immediately and identically everywhere instead of crashing deep in a port.
    """
    if not is_supported_python(version):
        sys.stderr.write(python_version_error(version) + "\n")
        raise SystemExit(1)


__all__ = [
    "MIN_PYTHON",
    "PYTHON_VERSION_ERROR",
    "python_version_error",
    "is_supported_python",
    "require_supported_python",
]
