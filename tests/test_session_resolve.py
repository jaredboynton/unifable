#!/usr/bin/env python3
"""resolve_session_id precedence (scripts/gate/spec.py).

Keys specs per conversation across hosts:
  stdin session_id > CLAUDE_CODE_SESSION_ID > CODEX_THREAD_ID > default.
Claude Code sends session_id on stdin (step 1, unchanged). Codex omits it from
the hook payload, so the env fallback keeps its specs from colliding on one
shared 'default' file. Callers that must fail open pass default=None.

Runs under pytest or standalone (python3 tests/test_session_resolve.py).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))
from spec import resolve_session_id  # noqa: E402

_ENV = ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")


def _clear():
    return {k: os.environ.pop(k, None) for k in _ENV}


def _restore(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_stdin_session_wins_over_env():
    saved = _clear()
    try:
        os.environ["CLAUDE_CODE_SESSION_ID"] = "cc"
        os.environ["CODEX_THREAD_ID"] = "cdx"
        assert resolve_session_id({"session_id": "stdin"}) == "stdin"
    finally:
        _restore(saved)


def test_claude_env_used_when_no_stdin():
    saved = _clear()
    try:
        os.environ["CLAUDE_CODE_SESSION_ID"] = "cc"
        os.environ["CODEX_THREAD_ID"] = "cdx"
        assert resolve_session_id({}) == "cc"
        assert resolve_session_id(None) == "cc"
    finally:
        _restore(saved)


def test_codex_env_used_when_no_claude():
    saved = _clear()
    try:
        os.environ["CODEX_THREAD_ID"] = "cdx"
        assert resolve_session_id({}) == "cdx"
        # empty/whitespace stdin session_id is falsy -> falls through to env
        assert resolve_session_id({"session_id": ""}) == "cdx"
    finally:
        _restore(saved)


def test_default_and_fail_open():
    saved = _clear()
    try:
        assert resolve_session_id({}) == "default"
        assert resolve_session_id(None) == "default"
        assert resolve_session_id({}, default=None) is None
        assert resolve_session_id(None, default=None) is None
    finally:
        _restore(saved)


if __name__ == "__main__":
    fails = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"  [OK] {_name}")
            except AssertionError as e:
                fails += 1
                print(f"  [FAIL] {_name}: {e}")
    print("RESULT:", "all pass" if not fails else f"{fails} failed")
    sys.exit(1 if fails else 0)
