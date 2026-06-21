#!/usr/bin/env python3
"""Robustness / safety checks for the unifable observation gate before wiring it global.

Covers the failure modes the gate-comparison doc warned about:
  - fail-open on bad/empty input (never crash, never block on our own bug)
  - cannot trap the agent forever (MAX_STOP_BLOCKS then allow)
  - respects the stop_hook_active loop guard
  - precision spot-checks: typecheck-success passes; config-edit-unverified reminds
"""

import json
import os
import subprocess
import sys
import tempfile

HOOKS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks")
PY = sys.executable


def run(script, payload, data_dir, raw=False):
    env = dict(os.environ)
    env["UNIFABLE_DATA"] = data_dir
    stdin = payload if raw else json.dumps(payload)
    p = subprocess.run([PY, os.path.join(HOOKS, script)], input=stdin,
                       capture_output=True, text=True, env=env)
    return p


# The evidence gate is unconditional (no env disable). Precision checks below that
# expect ALLOW need a valid spec present so the observation gate is what decides.
VALID_SPEC = {
    "restated_goal": "Robustness harness fixture.",
    "acceptance_criteria": [{"check": "pytest -q", "evidence": "5 passed in 0.4s"}],
    "must_read": [{"cite": "src/x.py:1", "why": "fixture passage"}],
    "prior_art": ["https://example.com/doc"],
    "constraints": ["fixture constraint"],
    "rejected_alternatives": ["alt a rejected: reason.", "alt b rejected: reason."],
}


def write_spec(cwd, sid):
    d = os.path.join(cwd, ".unifable", "spec")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{sid}.json"), "w") as f:
        json.dump(VALID_SPEC, f)


def as_json(p):
    try:
        return json.loads(p.stdout or "{}")
    except json.JSONDecodeError:
        return {"_raw": p.stdout}


def blocks(p):
    return as_json(p).get("decision") == "block"


checks = []


def check(name, cond):
    checks.append((name, bool(cond)))


# --- A. fail-open: empty + invalid stdin on every hook -> exit 0, never block ---
for script in ("gate_prompt.py", "gate_post_tool.py", "gate_stop.py"):
    dd = tempfile.mkdtemp(prefix="fz_")
    pe = run(script, "", dd, raw=True)
    pi = run(script, "}{not json", dd, raw=True)
    check(f"{script} empty-stdin exit0+noblock", pe.returncode == 0 and not blocks(pe))
    check(f"{script} bad-json exit0+noblock", pi.returncode == 0 and not blocks(pi))

# --- B. stop_hook_active guard: the SOFT (observation) gate must NOT block when set.
#     A valid spec is present so the infinite evidence gate passes and only the
#     observation gate (which honours the loop guard) is exercised. ---
dd = tempfile.mkdtemp(prefix="fz_"); cw = tempfile.mkdtemp(prefix="fzcwd_")
run("gate_prompt.py", {"prompt": "implement X production-ready", "session_id": "B", "cwd": cw}, dd)
run("gate_post_tool.py", {"tool_name": "Edit", "tool_input": {"file_path": "src/x.py", "old_string": "a", "new_string": "b"}, "session_id": "B", "cwd": cw}, dd)
write_spec(cw, "B")
p_guard = run("gate_stop.py", {"session_id": "B", "cwd": cw, "stop_hook_active": True}, dd)
check("stop_hook_active=true -> no block (observation gate)", not blocks(p_guard))

# --- C. cannot trap forever: same session blocks at most MAX_STOP_BLOCKS(2), then allows ---
dd = tempfile.mkdtemp(prefix="fz_"); cw = tempfile.mkdtemp(prefix="fzcwd_")
run("gate_prompt.py", {"prompt": "implement X production-ready", "session_id": "C", "cwd": cw}, dd)
run("gate_post_tool.py", {"tool_name": "Edit", "tool_input": {"file_path": "src/x.py", "old_string": "a", "new_string": "b"}, "session_id": "C", "cwd": cw}, dd)
write_spec(cw, "C")  # evidence passes; the observation gate (changed+unverified) drives the cap
d1 = blocks(run("gate_stop.py", {"session_id": "C", "cwd": cw, "stop_hook_active": False}, dd))
d2 = blocks(run("gate_stop.py", {"session_id": "C", "cwd": cw, "stop_hook_active": False}, dd))
d3 = blocks(run("gate_stop.py", {"session_id": "C", "cwd": cw, "stop_hook_active": False}, dd))
check("blocks first 2 then allows (no infinite trap)", d1 and d2 and not d3)

# --- D. precision: deep task + code edit + typecheck SUCCESS -> allow ---
dd = tempfile.mkdtemp(prefix="fz_"); cw = tempfile.mkdtemp(prefix="fzcwd_")
run("gate_prompt.py", {"prompt": "refactor the parser thoroughly", "session_id": "D", "cwd": cw}, dd)
run("gate_post_tool.py", {"tool_name": "Edit", "tool_input": {"file_path": "src/p.ts", "old_string": "a", "new_string": "b"}, "session_id": "D", "cwd": cw}, dd)
run("gate_post_tool.py", {"tool_name": "Bash", "tool_input": {"command": "tsc --noEmit"}, "tool_response": {"exit_code": 0, "stdout": "done"}, "session_id": "D", "cwd": cw}, dd)
write_spec(cw, "D")
p_ts = run("gate_stop.py", {"session_id": "D", "cwd": cw, "stop_hook_active": False}, dd)
check("deep + code edit + tsc success -> allow", not blocks(p_ts))

# --- E. lecture-style Korean doc prompt defaults to quick -> never blocks ---
dd = tempfile.mkdtemp(prefix="fz_")
run("gate_prompt.py", {"prompt": "윤자동 2회차 강의 준비해줘", "session_id": "E", "cwd": "/w"}, dd)
run("gate_post_tool.py", {"tool_name": "Edit", "tool_input": {"file_path": "course.md", "old_string": "a", "new_string": "b"}, "session_id": "E", "cwd": "/w"}, dd)
p_kr = run("gate_stop.py", {"session_id": "E", "cwd": "/w", "stop_hook_active": False}, dd)
check("KO lecture prompt (quick default) -> allow", not blocks(p_kr))

# --- F. deep-only: a NORMAL task + code edit + no verification -> allow (no hard block) ---
dd = tempfile.mkdtemp(prefix="fz_"); cw = tempfile.mkdtemp(prefix="fzcwd_")
run("gate_prompt.py", {"prompt": "fix the login bug in the parser", "session_id": "F", "cwd": cw}, dd)
run("gate_post_tool.py", {"tool_name": "Edit", "tool_input": {"file_path": "src/login.py", "old_string": "a", "new_string": "b"}, "session_id": "F", "cwd": cw}, dd)
write_spec(cw, "F")
p_normal = run("gate_stop.py", {"session_id": "F", "cwd": cw, "stop_hook_active": False}, dd)
check("deep-only: normal task changed+unverified -> allow (no block)", not blocks(p_normal))

# --- G. deep turn that changed NOTHING (analysis/audit) -> allow (no "add observable" nag) ---
dd = tempfile.mkdtemp(prefix="fz_"); cw = tempfile.mkdtemp(prefix="fzcwd_")
run("gate_prompt.py", {"prompt": "thoroughly audit the security of this module", "session_id": "G", "cwd": cw}, dd)
write_spec(cw, "G")
p_analysis = run("gate_stop.py", {"session_id": "G", "cwd": cw, "stop_hook_active": False}, dd)
check("deep analysis, no change -> allow (no false-positive nag)", not blocks(p_analysis))

# --- H. evidence gate is INFINITE: no spec (STANDARD) blocks even with the loop
#     guard set, and keeps blocking past MAX_STOP_BLOCKS (no cap, no release). ---
dd = tempfile.mkdtemp(prefix="fz_"); cw = tempfile.mkdtemp(prefix="fzcwd_")
h_guard = run("gate_stop.py", {"session_id": "H", "cwd": cw, "stop_hook_active": True}, dd, )
check("evidence gate ignores stop_hook_active (no spec -> block)", blocks(h_guard))
h_runs = [blocks(run("gate_stop.py", {"session_id": "H", "cwd": cw, "stop_hook_active": False}, dd)) for _ in range(4)]
check("evidence gate ignores the cap (4/4 blocks, infinite)", all(h_runs))

print("=" * 80)
print("unifable observation gate — robustness / safety checks")
print("=" * 80)
fails = 0
for name, ok in checks:
    fails += 0 if ok else 1
    print(f"  [{'OK' if ok else 'FAIL'}] {name}")
print("-" * 80)
print(f"RESULT: {'all pass' if not fails else str(fails) + ' FAILED'} ({len(checks)} checks)")
sys.exit(1 if fails else 0)
