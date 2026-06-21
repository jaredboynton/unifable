#!/usr/bin/env python3
"""unifable goal engine — a self-contained, stdlib-only multi-story loop with a verification gate.

Design (behavior only):
  - Decompose a task into sequential stories, persisted to a ledger (.unifable/) — survives session death.
  - A story can be checkpointed only after `next` activates it.
  - A `complete` checkpoint requires non-empty evidence.
  - The final story cannot complete without a verify command + result (the verification gate).

Usage:
  goals.py create --brief "..." --goal "title::objective" [--goal ...]
  goals.py next                       # activate the next story + print a handoff
  goals.py checkpoint --id G001 --status complete|failed|blocked --evidence "..."
                      [--verify-cmd "<command run>" --verify-evidence "<result>"]   # required on the final story
  goals.py status
State directory: ./.unifable/ (run from the repo root)
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Reuse the gate's hardened atomic writer (unique temp + os.replace) so two
# goals.py invocations writing goals.json at once can never leave a torn or
# half-written file -- the same guarantee the gate's JSON state already has.
# scripts/gate is resolved relative to THIS file, so the import works no matter
# what the caller's cwd is.
sys.path.insert(0, str(Path(__file__).resolve().parent / "gate"))
from atomicio import write_text_atomic
from spec import resolve_session_id, session_dir


# State is keyed per (directory, session): <data_root>/specs/<dir_hash(cwd)>/<session>/.
# The plan (goals.json) sits beside the evidence spec, so a new session never
# inherits a prior session's plan. resolve_session_id reads CLAUDE_CODE_SESSION_ID /
# CODEX_THREAD_ID from the env (this CLI gets no stdin), matching the hook's key.
def _state_dir() -> Path:
    return session_dir(os.getcwd(), resolve_session_id(None, default="default"))


def _goals_file() -> Path:
    return _state_dir() / "goals.json"


def _ledger_file() -> Path:
    return _state_dir() / "goals-ledger.jsonl"


def now():
    return datetime.now(timezone.utc).isoformat()


def log(event, **kw):
    d = _state_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(_ledger_file(), "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now(), "event": event, **kw}, ensure_ascii=False) + "\n")


def load():
    goals = _goals_file()
    if not goals.exists():
        sys.exit("unifable: no plan — run `create` from the repo root first.")
    return json.loads(goals.read_text(encoding="utf-8"))


def save(plan):
    # Atomic: a concurrent reader sees either the old goals.json or the complete
    # new one, never a truncated file; concurrent writers are last-writer-wins
    # with no torn state. write_text_atomic creates the session dir if missing.
    write_text_atomic(_goals_file(), json.dumps(plan, ensure_ascii=False, indent=1))


def cmd_create(a):
    if _goals_file().exists() and not a.force:
        sys.exit("unifable: a plan already exists. Check it with `status`, or replace it with --force.")
    goals = []
    for i, g in enumerate(a.goal, 1):
        if "::" not in g:
            sys.exit(f"unifable: --goal format is 'title::objective' — invalid: {g}")
        title, obj = g.split("::", 1)
        goals.append({"id": f"G{i:03d}", "title": title.strip(), "objective": obj.strip(),
                      "status": "pending", "evidence": None})
    if not goals:
        sys.exit("unifable: at least one --goal is required.")
    save({"brief": a.brief, "created": now(), "goals": goals})
    log("plan_created", brief=a.brief, count=len(goals))
    print(f"unifable: plan created — {len(goals)} stories")
    for g in goals:
        print(f"  {g['id']} {g['title']}: {g['objective']}")


def cmd_next(a):
    plan = load()
    active = [g for g in plan["goals"] if g["status"] == "in_progress"]
    if active:
        g = active[0]
    else:
        pending = [g for g in plan["goals"] if g["status"] == "pending"]
        if not pending:
            print("unifable: all stories complete ✓"); return
        g = pending[0]
        g["status"] = "in_progress"
        save(plan); log("story_started", id=g["id"], title=g["title"])
    is_final = g["id"] == plan["goals"][-1]["id"]
    print(f"=== unifable handoff — {g['id']} {g['title']}")
    print(f"Objective: {g['objective']}")
    print("Rule: work this story only. Produce evidence as you go.")
    if is_final:
        print("★ Final story — the complete checkpoint requires --verify-cmd and --verify-evidence (verification gate).")
    print(f"On completion: goals.py checkpoint --id {g['id']} --status complete --evidence \"<evidence>\""
          + (" --verify-cmd \"<command>\" --verify-evidence \"<result>\"" if is_final else ""))


def cmd_checkpoint(a):
    plan = load()
    g = next((x for x in plan["goals"] if x["id"] == a.id), None)
    if not g:
        sys.exit(f"unifable: {a.id} not found.")
    if g["status"] != "in_progress":
        sys.exit(f"unifable: {a.id} is not active ({g['status']}) — activate it with `next` first.")
    if a.status == "complete":
        if not (a.evidence and a.evidence.strip()):
            sys.exit("unifable: a complete checkpoint requires non-empty --evidence.")
        if g["id"] == plan["goals"][-1]["id"]:
            if not (a.verify_cmd and a.verify_cmd.strip() and a.verify_evidence and a.verify_evidence.strip()):
                sys.exit("unifable: the final story cannot complete without --verify-cmd and --verify-evidence (verification gate).")
    g["status"] = a.status
    g["evidence"] = a.evidence
    save(plan)
    log("checkpoint", id=g["id"], status=a.status, evidence=a.evidence,
        verify_cmd=a.verify_cmd, verify_evidence=a.verify_evidence)
    print(f"unifable: {g['id']} → {a.status}")
    remaining = [x for x in plan["goals"] if x["status"] in ("pending", "in_progress")]
    print("unifable: all stories complete ✓" if not remaining else f"unifable: {len(remaining)} stories left — continue with `next`.")


def cmd_status(a):
    plan = load()
    done = sum(1 for g in plan["goals"] if g["status"] == "complete")
    print(f"unifable: {done}/{len(plan['goals'])} complete — {plan['brief']}")
    mark = {"complete": "✓", "in_progress": "▶", "pending": "·", "failed": "✗", "blocked": "■"}
    for g in plan["goals"]:
        print(f"  {mark.get(g['status'],'?')} {g['id']} [{g['status']}] {g['title']}")


def main():
    p = argparse.ArgumentParser(prog="goals.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create"); c.add_argument("--brief", required=True)
    c.add_argument("--goal", action="append", default=[]); c.add_argument("--force", action="store_true")
    sub.add_parser("next")
    k = sub.add_parser("checkpoint"); k.add_argument("--id", required=True)
    k.add_argument("--status", required=True, choices=["complete", "failed", "blocked"])
    k.add_argument("--evidence", default=""); k.add_argument("--verify-cmd", dest="verify_cmd", default="")
    k.add_argument("--verify-evidence", dest="verify_evidence", default="")
    sub.add_parser("status")
    a = p.parse_args()
    {"create": cmd_create, "next": cmd_next, "checkpoint": cmd_checkpoint, "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    main()
