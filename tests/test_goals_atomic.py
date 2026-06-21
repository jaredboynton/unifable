#!/usr/bin/env python3
"""goals.py writes its plan atomically, at the global per-(directory, session) path.

Regression for the race where two goals.py invocations writing goals.json at
once could leave a torn/half-written file (scripts/goals.py save() used a plain
Path.write_text). save() now routes through the gate's write_text_atomic
(scripts/gate/atomicio.py), so a concurrent reader always sees a complete file.

The plan now lives at <UNIFABLE_DATA>/specs/<dir_hash(cwd)>/<session>/goals.json,
keyed by directory + session, so a new session never inherits a prior plan.

Runs under pytest or standalone (python3 tests/test_goals_atomic.py).
"""
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GOALS_PY = str(REPO / "scripts" / "goals.py")
sys.path.insert(0, str(REPO / "scripts" / "gate"))

SESSION = "goals-atomic-test"


def _env(data_dir):
    env = dict(os.environ)
    env["UNIFABLE_DATA"] = data_dir
    env["CLAUDE_CODE_SESSION_ID"] = SESSION
    return env


def _goals_path(cwd, data_dir):
    # Resolve the same keyed path the CLI writes, with UNIFABLE_DATA pointed at data_dir.
    old = os.environ.get("UNIFABLE_DATA")
    os.environ["UNIFABLE_DATA"] = data_dir
    try:
        from spec import session_dir
        return session_dir(cwd, SESSION) / "goals.json"
    finally:
        if old is None:
            os.environ.pop("UNIFABLE_DATA", None)
        else:
            os.environ["UNIFABLE_DATA"] = old


def _run(args, cwd, data_dir):
    return subprocess.run([sys.executable, GOALS_PY, *args], cwd=cwd,
                          capture_output=True, text=True, env=_env(data_dir))


def test_save_uses_atomic_writer():
    """The plain write is gone; save() goes through write_text_atomic."""
    src = (REPO / "scripts" / "goals.py").read_text(encoding="utf-8")
    assert "from atomicio import write_text_atomic" in src
    assert "write_text_atomic(_goals_file()" in src
    assert ".write_text(" not in src


def test_concurrent_writers_keep_valid_json():
    """Many concurrent `create --force` writers + readers: every read parses."""
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as data:
        assert _run(["create", "--brief", "b", "--goal", "t::o"], d, data).returncode == 0
        gpath = _goals_path(d, data)

        def writer(i):
            return _run(["create", "--force", "--brief", f"b{i}", "--goal", f"t{i}::o{i}"], d, data).returncode

        def reader(_):
            try:
                json.loads(gpath.read_text(encoding="utf-8"))
                return True
            except (json.JSONDecodeError, FileNotFoundError, ValueError):
                return False

        with cf.ThreadPoolExecutor(max_workers=24) as ex:
            wfut = [ex.submit(writer, i) for i in range(30)]
            rfut = [ex.submit(reader, i) for i in range(200)]
            wcodes = [f.result() for f in wfut]
            reads = [f.result() for f in rfut]

        assert all(c == 0 for c in wcodes), wcodes
        assert all(reads), "observed a torn/partial goals.json under concurrent writers"
        json.loads(gpath.read_text(encoding="utf-8"))  # final state still valid


def test_create_next_checkpoint_flow():
    """The multi-story loop still works end to end after the atomic change."""
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as data:
        assert _run(["create", "--brief", "b", "--goal", "only::do it"], d, data).returncode == 0
        assert _run(["next"], d, data).returncode == 0
        r = _run(["checkpoint", "--id", "G001", "--status", "complete",
                  "--evidence", "done", "--verify-cmd", "true", "--verify-evidence", "ok"], d, data)
        assert r.returncode == 0, r.stderr
        data_json = json.loads(_goals_path(d, data).read_text(encoding="utf-8"))
        assert data_json["goals"][0]["status"] == "complete"


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
