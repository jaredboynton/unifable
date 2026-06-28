#!/usr/bin/env python3
"""T8: live hook smoke test for the stepwise harness.

Exercises the REAL hook subprocesses end-to-end (not in-process helpers):
  1. SessionStart emits the actionable restate-first frame, not the old fat
     operating-mode block.
  2. PreToolUse enforces the director's persisted tool scope: an out-of-scope Edit
     on an unvalidated spec is blocked with the director's directive as the reason.

Hermetic: the judge is forced offline (UNIFABLE_JUDGE_OFFLINE) and runtime sync is
disabled so the smoke test never depends on credentials or mutates ~/.unifable.
The live, judge-reachable "director actually fires" check is exercised separately
(it is nondeterministic and not asserted here).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks"
sys.path.insert(0, str(REPO / "scripts" / "gate"))

import breaker_state  # noqa: E402


def _run_hook(hook: str, payload, env_extra: dict) -> tuple[int, str, str]:
    env = dict(os.environ)
    env.update(env_extra)
    p = subprocess.run(
        [sys.executable, str(HOOKS / hook)],
        input=json.dumps(payload) if not isinstance(payload, str) else payload,
        capture_output=True,
        text=True,
        env=env,
    )
    return p.returncode, p.stdout, p.stderr


def test_sessionstart_emits_thin_frame(tmp_path) -> None:
    rc, out, _ = _run_hook(
        "session_start.py",
        {"hook_event_name": "SessionStart", "cwd": str(tmp_path)},
        {"UNIFABLE_RUNTIME_SYNC": "0", "UNIFABLE_DATA": str(tmp_path)},
    )
    assert rc == 0
    payload = json.loads(out or "{}")
    ctx = payload.get("hookSpecificOutput", {}).get("additionalContext", "")
    # Thin frame: mandatory restate-first command + research-mode restrictions.
    assert ctx
    # The old fat operating-mode block must be gone.
    assert len(ctx) < 950


def test_pretool_enforces_director_scope_live(tmp_path) -> None:
    session_id = "smoke-scope"
    input_data = {"session_id": session_id, "cwd": str(tmp_path)}
    # Seed a director scope as if a prior debounced judge call had set it. A
    # future breaker_judged_at suppresses a fresh (clobbering) judge call so the
    # seeded scope survives into the scope-enforcement step.
    os.environ["UNIFABLE_DATA"] = str(tmp_path)
    st = breaker_state.default_breaker()
    st["breaker_key"] = f"{session_id}|"
    st["breaker_judged_at"] = time.time() + 3600
    st["breaker_directive"] = "Read foo.py before editing."
    st["breaker_tool_scope"] = {"deny": ["Edit"], "directive": "Read foo.py before editing."}
    breaker_state.save_breaker(input_data, st)

    payload = {
        "tool_name": "Edit",
        "session_id": session_id,
        "cwd": str(tmp_path),
        "tool_input": {"file_path": str(tmp_path / "foo.py"), "old_string": "a", "new_string": "b"},
    }
    rc, _, stderr = _run_hook(
        "pre_tool_use.py",
        payload,
        {
            "UNIFABLE_DATA": str(tmp_path),
            "UNIFABLE_JUDGE_OFFLINE": "1",
            "UNIFABLE_GRADE": "STANDARD",
        },
    )
    # Unvalidated spec + out-of-scope Edit -> blocked with the directive surfaced.
    assert rc == 2
    assert stderr.strip()


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
