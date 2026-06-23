#!/usr/bin/env python3
"""Stop-hook timeout budget: the gate can never be killed mid-judge.

Regression for the codex-thread "hook timed out after 10s": the host Stop budget
must comfortably exceed a single judge round-trip, the judge's own deadlines must
fit under the host budget, and auto_validate_spec must honour a wall-clock budget
so it returns cleanly instead of running unbounded judge/check work."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))

import codex_judge  # noqa: E402
import spec as spec_mod  # noqa: E402
from spec import auto_validate_spec, load_spec, save_spec, spec_template  # noqa: E402

MANIFESTS = ["hooks/hooks.json", ".codex-plugin/hooks.json"]


def _stop_gate_timeout(path: Path) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    for group in data["hooks"]["Stop"]:
        for hook in group["hooks"]:
            if "gate_stop" in hook["command"]:
                return int(hook["timeout"])
    raise AssertionError(f"no gate_stop hook in {path}")


def test_manifests_stop_timeout_at_least_120():
    for rel in MANIFESTS:
        assert _stop_gate_timeout(REPO / rel) >= 120, rel


def test_installer_stop_timeout_at_least_120():
    txt = (REPO / "install" / "merge_hooks.py").read_text(encoding="utf-8")
    m = re.search(r"gate_stop\.py[\s\S]{0,160}?timeout\"?\s*:\s*(\d+)", txt)
    assert m and int(m.group(1)) >= 120


def test_single_judge_call_fits_under_host_budget():
    # handshake + read must finish before the host kills the hook, so even a slow
    # single judge call returns a clean JudgeError instead of a host timeout.
    host = min(_stop_gate_timeout(REPO / rel) for rel in MANIFESTS)
    assert codex_judge.HANDSHAKE_TIMEOUT + codex_judge.READ_TIMEOUT <= host


def _spec_with_pending(tmp_path, key):
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [{"id": "T1", "title": "t", "check": "true", "status": "pending"}]
    save_spec(str(tmp_path), key, s)
    return load_spec(str(tmp_path), key)


def test_zero_budget_does_no_work(tmp_path, monkeypatch):
    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".", timeout=None: (0, "ok"))
    monkeypatch.setattr(spec_mod, "judge_tasks", lambda sp, items: [(1, "ok", [], "") for _ in items])
    spec, _ = auto_validate_spec(_spec_with_pending(tmp_path, "Z"), str(tmp_path), time_budget=0.0)
    assert spec["tasks"][0]["status"] == "pending"  # deadline already passed -> untouched


def test_budget_bounds_check_timeout(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(spec_mod, "run_check",
                        lambda check, cwd=".", timeout=None: seen.__setitem__("t", timeout) or (0, "ok"))
    monkeypatch.setattr(spec_mod, "judge_tasks", lambda sp, items: [(1, "ok", [], "") for _ in items])
    spec, _ = auto_validate_spec(_spec_with_pending(tmp_path, "B"), str(tmp_path), time_budget=30.0)
    assert spec["tasks"][0]["status"] == "validated"
    assert seen["t"] is not None and seen["t"] <= 30  # check bounded by remaining budget


def test_gate_stop_passes_a_budget():
    import gate_stop
    assert gate_stop.STOP_JUDGE_BUDGET <= min(_stop_gate_timeout(REPO / rel) for rel in MANIFESTS)
