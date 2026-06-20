#!/usr/bin/env python3
"""unifable PostToolUse hook — run project tests after each code edit.

Fires after Edit / Write / MultiEdit / NotebookEdit / apply_patch. Disabled by
default; set UNIFABLE_TEST_AFTER_EDIT=1 to enable. Fails open: any exception
emits {} and exits 0 so the host session is never interrupted.

Env knobs:
  UNIFABLE_TEST_AFTER_EDIT=1   enable the hook (default: off)
  UNIFABLE_TEST_DEBOUNCE=45    min seconds between runs per project root
  UNIFABLE_TEST_TIMEOUT=60     per-run subprocess timeout in seconds
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEBOUNCE_SECS = int(os.environ.get("UNIFABLE_TEST_DEBOUNCE", "45"))
TIMEOUT_SECS = int(os.environ.get("UNIFABLE_TEST_TIMEOUT", "60"))
TAIL_LINES = 30

# Extensions that should never trigger a test run.
# .json is intentionally NOT in this set so projects with JSON-driven fixtures
# remain testable; only clear docs/media/lockfiles are skipped.
SKIP_EXTS = {
    ".md", ".markdown", ".mdx", ".txt", ".rst", ".adoc",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".pdf",
    ".mp3", ".mp4", ".mov", ".wav",
    ".lock", ".lockb",
}

# Tool names that indicate a code edit occurred.
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"}


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")


def _emit_skip() -> None:
    _emit({})


def _emit_context(message: str) -> None:
    _emit({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": message,
        }
    })


# ---------------------------------------------------------------------------
# Extension filter
# ---------------------------------------------------------------------------

def should_skip_path(file_path: str) -> bool:
    """Return True when the edited file has a doc/asset/lock extension to skip."""
    ext = Path(file_path).suffix.lower()
    return ext in SKIP_EXTS


# ---------------------------------------------------------------------------
# Runner discovery
# ---------------------------------------------------------------------------

def discover_runner(start_dir: str) -> tuple[str | None, list[str] | None, str | None]:
    """Walk up from start_dir to find the nearest test runner.

    Returns (project_root, command_list, label) or (None, None, None).
    Prefers the NARROWEST (innermost) runner found.
    """
    d = os.path.abspath(start_dir)
    while True:
        # Node / JS-TS: package.json with a real "test" script
        pkg = os.path.join(d, "package.json")
        if os.path.isfile(pkg):
            try:
                with open(pkg, encoding="utf-8") as f:
                    scripts = json.load(f).get("scripts", {})
            except Exception:
                scripts = {}
            test_script = scripts.get("test", "")
            if test_script and "no test specified" not in test_script.lower():
                if os.path.isfile(os.path.join(d, "pnpm-lock.yaml")):
                    pm = "pnpm"
                elif os.path.isfile(os.path.join(d, "yarn.lock")):
                    pm = "yarn"
                elif os.path.isfile(os.path.join(d, "bun.lockb")):
                    pm = "bun"
                else:
                    pm = "npm"
                return d, [pm, "test"], f"{pm} test"

        # Python: pyproject.toml / setup.cfg / pytest.ini / tox.ini or a tests/ dir
        py_markers = ("pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini", "setup.py")
        if any(os.path.isfile(os.path.join(d, m)) for m in py_markers) or \
                os.path.isdir(os.path.join(d, "tests")):
            if os.path.isfile(os.path.join(d, "uv.lock")):
                return d, ["uv", "run", "pytest", "-q"], "uv run pytest -q"
            return d, [sys.executable, "-m", "pytest", "-q"], "pytest -q"

        # Rust
        if os.path.isfile(os.path.join(d, "Cargo.toml")):
            return d, ["cargo", "test", "-q"], "cargo test -q"

        # Go
        if os.path.isfile(os.path.join(d, "go.mod")):
            return d, ["go", "test", "./..."], "go test ./..."

        # Make with a test target
        makefile = os.path.join(d, "Makefile")
        if os.path.isfile(makefile):
            try:
                with open(makefile, encoding="utf-8", errors="replace") as f:
                    if any(line.startswith("test:") for line in f):
                        return d, ["make", "test"], "make test"
            except Exception:
                pass

        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent

    return None, None, None


# ---------------------------------------------------------------------------
# Debounce
# ---------------------------------------------------------------------------

def _marker_path(root: str) -> str:
    h = hashlib.sha256(root.encode("utf-8", "replace")).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), f"unifable-tae-{h}")


def is_debounced(root: str) -> bool:
    """Return True if a run for this root happened within DEBOUNCE_SECS."""
    marker = _marker_path(root)
    try:
        if os.path.exists(marker):
            if time.time() - os.path.getmtime(marker) < DEBOUNCE_SECS:
                return True
    except OSError:
        pass
    return False


def stamp_debounce(root: str) -> None:
    """Touch the debounce marker for this root."""
    marker = _marker_path(root)
    try:
        with open(marker, "w") as f:
            f.write("")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def run_tests(root: str, cmd: list[str], label: str) -> str:
    """Run cmd in root with a timeout; return a human-readable summary string."""
    try:
        result = subprocess.run(
            cmd,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        return (
            f"unifable test-after-edit: TIMEOUT ({label}): "
            f"suite exceeded {TIMEOUT_SECS}s in {root}; result inconclusive."
        )
    except FileNotFoundError:
        # Runner binary not installed — stay silent (emit {} from caller)
        return ""
    except Exception as exc:  # noqa: BLE001
        return f"unifable test-after-edit: ERROR ({label}): {exc}"

    combined = (result.stdout or "") + (result.stderr or "")
    tail = "\n".join(combined.strip().splitlines()[-TAIL_LINES:])

    if result.returncode == 0:
        return f"unifable test-after-edit: PASS ({label}): {tail}"
    return (
        f"unifable test-after-edit: FAIL ({label}) exit={result.returncode}:\n{tail}"
    )


def main() -> int:
    # Opt-in gate — off by default
    if not os.environ.get("UNIFABLE_TEST_AFTER_EDIT"):
        _emit_skip()
        return 0

    try:
        raw = sys.stdin.read()
        data: dict = json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001
        _emit_skip()
        return 0

    tool_name = data.get("tool_name", "")
    if tool_name not in EDIT_TOOLS:
        _emit_skip()
        return 0

    tool_input = data.get("tool_input") or {}
    file_path = tool_input.get("file_path") or tool_input.get("path") or ""

    if file_path and should_skip_path(file_path):
        _emit_skip()
        return 0

    # Determine start directory for runner discovery
    if file_path:
        start_dir = str(Path(file_path).parent)
    else:
        start_dir = data.get("cwd") or os.getcwd()

    root, cmd, label = discover_runner(start_dir)
    if not cmd:
        _emit_skip()
        return 0

    if is_debounced(root):
        _emit_skip()
        return 0

    stamp_debounce(root)
    summary = run_tests(root, cmd, label)

    if not summary:
        # Runner binary missing — silent skip
        _emit_skip()
        return 0

    _emit_context(summary)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 — fail open
        _emit({})
        raise SystemExit(0)
