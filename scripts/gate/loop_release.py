#!/usr/bin/env python3
"""Judge-adjudicated completion breaker loop release (V1: Stop/completion only).

Detects completion suicide-loop signatures from ledger + spec state, invokes a
structured loop-release judge, and applies provisional Stop lifts or permanent
retraction of judge-added spurious requirements. Fail-open on judge errors.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

try:
    COMPLETION_LOOP_JUDGE_THRESHOLD = int(os.environ.get("UNIFABLE_LOOP_JUDGE_THRESHOLD", "4"))
except ValueError:
    COMPLETION_LOOP_JUDGE_THRESHOLD = 4

try:
    LOOP_JUDGE_DEBOUNCE_SEC = float(os.environ.get("UNIFABLE_LOOP_JUDGE_DEBOUNCE_SEC", "60"))
except ValueError:
    LOOP_JUDGE_DEBOUNCE_SEC = 60.0

try:
    LOOP_PROVISIONAL_STOPS_MAX = int(os.environ.get("UNIFABLE_LOOP_PROVISIONAL_STOPS_MAX", "3"))
except ValueError:
    LOOP_PROVISIONAL_STOPS_MAX = 3

try:
    LOOP_STALL_SIGNATURE_BLOCKS = int(os.environ.get("UNIFABLE_LOOP_STALL_SIGNATURE_BLOCKS", "3"))
except ValueError:
    LOOP_STALL_SIGNATURE_BLOCKS = 3

# Re-fire the loop judge every N additional raw stop-blocks past the first
# judgment, even if the episode was already judged. Without this, a single
# decline suppresses the judge forever for that episode -- but the raw counter
# keeps climbing, and the judge deserves a second look with stronger evidence
# (the set-based streak is unreliable under fluctuation).
try:
    LOOP_JUDGE_REFIRE_STEP = int(os.environ.get("UNIFABLE_LOOP_JUDGE_REFIRE_STEP", "4"))
except ValueError:
    LOOP_JUDGE_REFIRE_STEP = 4

LOOP_EVENTS_MAX = 20

_LOOP_RELEASE_SCHEMA = {
    "type": "object",
    "properties": {
        "suicide_loop": {"type": "boolean"},
        "lift": {"type": "string", "enum": ["none", "provisional", "permanent"]},
        "reason": {"type": "string"},
        "lift_scope": {"type": "string"},
        "retract_task_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
        "provisional_stops": {"type": "integer"},
    },
    "required": ["suicide_loop", "lift", "reason"],
    "additionalProperties": False,
}

_LOOP_RETRACT_REASON = "loop release (spurious judge-added requirement)"

_LOOP_JUDGE_SYSTEM = (
    "You adjudicate whether an autonomous coding agent is trapped in a COMPLETION "
    "suicide loop: the Stop hook keeps blocking because requirements fail validation, "
    "with no net progress (same failing tasks, judge-added runaway, redundant or "
    "unsatisfiable checks). Two failure modes qualify: (1) STALL -- the same failing "
    "tasks repeat with no net progress; (2) REDUNDANCY -- a judge-added requirement "
    "duplicates the purpose of a VALIDATED requirement. Both set suicide_loop=true. "
    "You may authorize a lift ONLY when suicide_loop=true "
    "with evidence from the task board and activity. "
    "SIGNALS: completion_stop_blocks is the RAW blocked-stop count (never resets "
    "except on genuine full-open); it is the strongest loop indicator. The "
    "hard_cap field is the absolute limit -- once completion_stop_blocks reaches "
    "hard_cap the harness will force-release Stop regardless of your verdict, so "
    "your job is to make a SURGICAL decision (retract spurious judge tasks or "
    "grant a provisional lift) BEFORE the blunt cap fires. loop_same_set_streak "
    "and completion_stall_blocks can reset on fluctuation (task rotation), so a "
    "low value there does NOT contradict a high completion_stop_blocks. "
    "lift=provisional: allow Stop through temporarily (1-3 times) so the agent can "
    "change approach; lift_scope MUST state allowed next actions. "
    "lift=permanent: list judge-added spurious requirement ids in retract_task_ids "
    "(never agent-authored tasks). The harness RETRACTS those tasks automatically "
    "— do NOT write imperative instructions to the agent (no 'retract T16', "
    "'leave other items', etc.). The reason field is an internal audit note only "
    "(past tense, one sentence); it is NOT shown to the agent. When fragmentation "
    "is present (many failed tasks plus pending judge-added replacements with "
    "overlapping purpose), put failed judge-added duplicate ids in retract_task_ids. "
    "If a judge-added requirement's intent is already covered by a VALIDATED "
    "requirement, treat it as a redundancy loop: set suicide_loop=true, "
    "lift=permanent, and list those judge-added ids in retract_task_ids. "
    "lift=none: when work is legitimately remaining, the incomplete set is shrinking, "
    "or evidence is insufficient. On uncertainty, lift=none."
)


@dataclass(frozen=True)
class LoopReleaseVerdict:
    suicide_loop: bool
    lift: str
    reason: str
    lift_scope: str
    retract_task_ids: list[str]
    provisional_stops: int


def _incomplete_set_key(incomplete_ids: list[str]) -> str:
    return ",".join(sorted(str(i) for i in incomplete_ids if str(i).strip()))


def _append_loop_event(ledger: dict[str, Any], kind: str, **fields: Any) -> None:
    events = ledger.get("loop_events")
    if not isinstance(events, list):
        events = []
    entry: dict[str, Any] = {"kind": kind, "ts": time.time(), **fields}
    events.append(entry)
    ledger["loop_events"] = events[-LOOP_EVENTS_MAX:]


def update_loop_signature(ledger: dict[str, Any], incomplete_ids: list[str]) -> None:
    """Track consecutive blocks with the same incomplete task set."""
    key = _incomplete_set_key(incomplete_ids)
    prev = str(ledger.get("completion_prev_incomplete_set") or "")
    if key == prev:
        ledger["loop_same_set_streak"] = int(ledger.get("loop_same_set_streak") or 0) + 1
    else:
        ledger["loop_same_set_streak"] = 1
        ledger["loop_episode_id"] = key
    ledger["completion_prev_incomplete_set"] = key


def stall_signature(
    ledger: dict[str, Any],
    incomplete_ids: list[str],
    *,
    pending_block: bool = False,
    spec: dict[str, Any] | None = None,
) -> bool:
    """True when observable signals indicate a completion suicide loop."""
    if spec is not None:
        try:
            from spec_tasks import detect_requirement_fragmentation

            frag = detect_requirement_fragmentation(spec)
        except Exception:
            frag = None
        if frag is not None and (frag.get("title_collisions") or int(frag.get("failed_count") or 0) >= 5):
            return True
    if int(ledger.get("completion_stall_blocks") or 0) >= LOOP_STALL_SIGNATURE_BLOCKS:
        return True
    stop_blocks = int(ledger.get("completion_stop_blocks") or 0)
    if pending_block:
        stop_blocks += 1
    if stop_blocks >= COMPLETION_LOOP_JUDGE_THRESHOLD:
        return True
    if int(ledger.get("loop_same_set_streak") or 0) >= 2 and _incomplete_set_key(incomplete_ids):
        return True
    return False


def loop_lift_active(ledger: dict[str, Any]) -> bool:
    return str(ledger.get("loop_lift_kind") or "") == "provisional" and int(ledger.get("loop_lift_stops_remaining") or 0) > 0


def consume_provisional_stop_lift(ledger: dict[str, Any]) -> bool:
    """Decrement provisional budget; return True if Stop may pass this attempt."""
    if not loop_lift_active(ledger):
        return False
    remaining = int(ledger.get("loop_lift_stops_remaining") or 0)
    if remaining <= 0:
        return False
    ledger["loop_lift_stops_remaining"] = remaining - 1
    if ledger["loop_lift_stops_remaining"] <= 0:
        ledger["loop_lift_kind"] = ""
        ledger["loop_lift_scope"] = ""
    return True


def should_invoke_loop_judge(
    ledger: dict[str, Any],
    incomplete_ids: list[str],
    *,
    pending_block: bool = False,
    spec: dict[str, Any] | None = None,
) -> bool:
    if loop_lift_active(ledger):
        return False
    if not stall_signature(ledger, incomplete_ids, pending_block=pending_block, spec=spec):
        return False
    episode = str(ledger.get("loop_episode_id") or "")
    last_at = float(ledger.get("loop_judge_last_at") or 0.0)
    # Re-fire on the raw stop-block counter: if completion_stop_blocks has
    # climbed LOOP_JUDGE_REFIRE_STEP past the count when the judge last ran,
    # give it another look (the set-based episode guard is unreliable under
    # fluctuation, but the raw counter reliably climbs). This overrides the
    # same-episode suppression.
    last_judged_count = int(ledger.get("loop_judge_at_stop_blocks") or 0)
    current_stops = int(ledger.get("completion_stop_blocks") or 0)
    if last_judged_count and current_stops - last_judged_count >= LOOP_JUDGE_REFIRE_STEP:
        if not (last_at and (time.monotonic() - last_at) < LOOP_JUDGE_DEBOUNCE_SEC):
            return True
    if episode and episode == str(ledger.get("loop_judge_episode_id") or ""):
        return False
    if last_at and (time.monotonic() - last_at) < LOOP_JUDGE_DEBOUNCE_SEC:
        return False
    return True


def _parse_verdict(res: Any) -> LoopReleaseVerdict:
    if not isinstance(res, dict):
        return LoopReleaseVerdict(False, "none", "", "", [], 0)
    lift = str(res.get("lift") or "none").strip().lower()
    if lift not in ("none", "provisional", "permanent"):
        lift = "none"
    suicide = bool(res.get("suicide_loop"))
    if not suicide:
        lift = "none"
    ids_raw = res.get("retract_task_ids")
    ids = [str(x).strip() for x in ids_raw if str(x).strip()] if isinstance(ids_raw, list) else []
    try:
        prov = int(res.get("provisional_stops") or 0)
    except (TypeError, ValueError):
        prov = 0
    return LoopReleaseVerdict(
        suicide_loop=suicide,
        lift=lift,
        reason=str(res.get("reason") or "").strip(),
        lift_scope=str(res.get("lift_scope") or "").strip(),
        retract_task_ids=ids,
        provisional_stops=prov,
    )


def judge_completion_loop_release(
    spec: dict[str, Any],
    ledger: dict[str, Any],
    *,
    signal: str,
    recent: str = "",
) -> LoopReleaseVerdict:
    """Invoke the loop-release judge. Fail-open to lift=none on error."""
    try:
        from codex_judge import JudgeError
        from judge_transport import ask_structured
    except ImportError as exc:  # pragma: no cover
        return LoopReleaseVerdict(False, "none", f"judge unavailable: {exc}", "", [], 0)

    tasks = spec.get("tasks") or []
    stop_blocks = int(ledger.get("completion_stop_blocks") or 0)
    from verify_state import COMPLETION_MAX_STOP_BLOCKS

    fragmentation = None
    try:
        from spec_tasks import detect_requirement_fragmentation

        fragmentation = detect_requirement_fragmentation(spec)
    except Exception:
        pass

    user = json.dumps(
        {
            "goal": spec.get("restated_goal", ""),
            "signal": signal,
            "completion_stop_blocks": stop_blocks,
            "hard_cap": COMPLETION_MAX_STOP_BLOCKS or None,
            "completion_stall_blocks": ledger.get("completion_stall_blocks"),
            "loop_same_set_streak": ledger.get("loop_same_set_streak"),
            "incomplete_episode": ledger.get("loop_episode_id"),
            "fragmentation": fragmentation,
            "tasks": [
                {
                    "id": t.get("id"),
                    "title": t.get("title"),
                    "status": t.get("status"),
                    "added_by": t.get("added_by"),
                    "judge_reason": t.get("judge_reason"),
                }
                for t in tasks
                if isinstance(t, dict)
            ],
            "recent_activity": (recent or "")[:2000],
        },
        ensure_ascii=False,
    )
    try:
        res = ask_structured(_LOOP_JUDGE_SYSTEM, user, _LOOP_RELEASE_SCHEMA, schema_name="loop_release")
    except JudgeError as exc:
        return LoopReleaseVerdict(False, "none", f"judge error: {exc}", "", [], 0)
    return _parse_verdict(res)


def _filter_retract_ids(spec: dict[str, Any], ids: list[str]) -> list[str]:
    """V1: only judge-added, non-retracted tasks."""
    by_id = {str(t.get("id")): t for t in (spec.get("tasks") or []) if isinstance(t, dict)}
    out: list[str] = []
    for tid in ids:
        t = by_id.get(tid)
        if t is None:
            continue
        if t.get("added_by") != "judge":
            continue
        if t.get("status") in ("retracted", "validated"):
            continue
        out.append(tid)
    return out


def apply_loop_release_verdict(
    spec: dict[str, Any],
    ledger: dict[str, Any],
    verdict: LoopReleaseVerdict,
) -> tuple[list[str], str]:
    """Apply lift verdict. Mutates spec and ledger. Returns (headlines, notify_msg)."""
    episode = str(ledger.get("loop_episode_id") or "")
    ledger["loop_judge_episode_id"] = episode
    ledger["loop_judge_last_at"] = time.monotonic()
    ledger["loop_judge_at_stop_blocks"] = int(ledger.get("completion_stop_blocks") or 0)

    if verdict.lift == "none" or not verdict.suicide_loop:
        _append_loop_event(ledger, "LOOP_JUDGE_DECLINED", reason=verdict.reason[:200])
        return [], ""

    if verdict.lift == "provisional":
        if not verdict.lift_scope.strip():
            _append_loop_event(ledger, "LOOP_JUDGE_DECLINED", reason="provisional missing scope")
            return [], ""
        stops = verdict.provisional_stops
        if stops < 1:
            stops = 1
        stops = min(stops, LOOP_PROVISIONAL_STOPS_MAX)
        ledger["loop_lift_kind"] = "provisional"
        ledger["loop_lift_reason"] = verdict.reason
        ledger["loop_lift_scope"] = verdict.lift_scope
        ledger["loop_lift_stops_remaining"] = stops
        _append_loop_event(
            ledger,
            "LOOP_LIFT_PROVISIONAL",
            reason=verdict.reason[:200],
            scope=verdict.lift_scope[:200],
            stops=stops,
        )
        msg = format_loop_lift_context(ledger)
        headline = f"Completion loop lift (provisional): {verdict.reason[:120]}"
        return [headline], msg

    if verdict.lift == "permanent":
        allowed = _filter_retract_ids(spec, verdict.retract_task_ids)
        if not allowed:
            _append_loop_event(ledger, "LOOP_JUDGE_DECLINED", reason="no retractable judge tasks")
            return [], ""
        from spec_judge import _apply_adjustments

        adjustments = [{"id": tid, "action": "retract", "reason": _LOOP_RETRACT_REASON} for tid in allowed]
        headlines = _apply_adjustments(spec, {"adjust_requirements": adjustments})
        ledger["loop_lift_kind"] = "permanent"
        ledger["loop_lift_reason"] = verdict.reason
        retracted = list(ledger.get("loop_lift_retracted") or [])
        for tid in allowed:
            if tid not in retracted:
                retracted.append(tid)
        ledger["loop_lift_retracted"] = retracted
        _append_loop_event(
            ledger,
            "LOOP_LIFT_PERMANENT",
            reason=verdict.reason[:200],
            retracted=allowed,
        )
        try:
            from verify_state import reset_completion_stall

            reset_completion_stall(ledger)
        except Exception:
            pass
        return headlines, ""

    return [], ""


def format_loop_lift_context(ledger: dict[str, Any]) -> str:
    """Model-facing loop-lift notice. Permanent retractions use normal spec headlines only."""
    kind = str(ledger.get("loop_lift_kind") or "")
    if not kind:
        return ""
    if kind == "permanent":
        return ""
    reason = str(ledger.get("loop_lift_reason") or "").strip()
    if kind == "provisional":
        remaining = int(ledger.get("loop_lift_stops_remaining") or 0)
        scope = str(ledger.get("loop_lift_scope") or "").strip()
        return (
            "Completion loop lift (provisional):\n"
            f"{reason}\n"
            f"Stop lifts remaining: {remaining}. Stay within scope: {scope}"
        )
    return ""


def provisional_allow_message(ledger: dict[str, Any]) -> str:
    """Message when provisional lift consumes one Stop pass."""
    remaining = int(ledger.get("loop_lift_stops_remaining") or 0)
    scope = str(ledger.get("loop_lift_scope") or "").strip()
    reason = str(ledger.get("loop_lift_reason") or "").strip()
    return (
        "Completion breaker: provisional Stop lift active. "
        f"{reason} Stay within scope: {scope}. "
        f"Lifts remaining after this stop: {remaining}."
    )
