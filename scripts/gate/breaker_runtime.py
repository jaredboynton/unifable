#!/usr/bin/env python3
"""Groundedness-breaker runtime: tool-class constants, transcript assembly,
breaker-state primitives (arm/disarm/record), and operator-facing messages.

Extracted from groundedness.py; re-exported by the groundedness facade. Imports
only from breaker_filters (downward in the DAG) plus host-agnostic state/tail
helpers.
"""
from __future__ import annotations

import os
import re
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # annotation-only: breaker_judges imports breaker_runtime, not vice versa
    from breaker_judges import ReleaseVerdict

try:
    from breaker_filters import (
        _SPEC_BOARD_BEGIN,
        _SPEC_BOARD_END,
        _SPEC_BOARD_MAX,
        _USER_GOAL_MAX,
    )
    from breaker_state import (
        append_event,
        clear_provisional_lift,
        lift_provisional,
        record_adjudicated_claim,
        render_events,
    )
    from transcript_locate import locate_transcript as _locate_transcript
    from transcript_tail import (
        TRANSCRIPT_TOKEN_BUDGET,
        latest_user_prompt_fingerprint,
        stripped_transcript_tail,
        tail_tokens,
    )
    from tool_restrictions import GROUNDEDNESS_BLOCKED_TOOLS, RELEASE_TOOLS as _RELEASE_TOOLS
except ImportError:  # pragma: no cover
    from scripts.gate.breaker_filters import (
        _SPEC_BOARD_BEGIN,
        _SPEC_BOARD_END,
        _SPEC_BOARD_MAX,
        _USER_GOAL_MAX,
    )
    from scripts.gate.breaker_state import (
        append_event,
        clear_provisional_lift,
        lift_provisional,
        record_adjudicated_claim,
        render_events,
    )
    from scripts.gate.transcript_locate import locate_transcript as _locate_transcript
    from scripts.gate.transcript_tail import (
        TRANSCRIPT_TOKEN_BUDGET,
        latest_user_prompt_fingerprint,
        stripped_transcript_tail,
        tail_tokens,
    )
    from scripts.gate.tool_restrictions import GROUNDEDNESS_BLOCKED_TOOLS, RELEASE_TOOLS as _RELEASE_TOOLS

# Coalesce window: once any judge has fired for a key, concurrent PreToolUse
# processes from the same parallel tool-call batch (which all judge the identical
# transcript and so get the identical verdict) skip their own judge call and
# reuse the persisted breaker state. Override: UNIFABLE_JUDGE_COALESCE_WINDOW.
try:
    JUDGE_COALESCE_WINDOW_SECONDS = float(os.environ.get("UNIFABLE_JUDGE_COALESCE_WINDOW", "2.0") or "2.0")
except (TypeError, ValueError):
    JUDGE_COALESCE_WINDOW_SECONDS = 2.0


MUTATION_TOOLS = frozenset(GROUNDEDNESS_BLOCKED_TOOLS)


RELEASE_TOOLS = frozenset(_RELEASE_TOOLS)


JUDGE_WINDOW_SECONDS = 3


DIRECTIVE_MAX_CHARS = 400


_DIRECTIVE_TOKEN_RE = re.compile(r"[a-z0-9]+")


# Generic instruction scaffolding; two directives that share only these words carry
# no evidence of being the same step, so they are dropped before the overlap test.
_DIRECTIVE_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "to", "of", "for", "in", "on", "then", "with",
        "that", "this", "it", "is", "are", "be", "by", "as", "at", "from", "into",
        "your", "you", "do", "not", "any", "all", "so", "its", "their", "them", "how",
        "what", "via", "use", "using", "should", "must",
    }
)


DIRECTIVE_NEAR_DUP_THRESHOLD = 0.7


_DIRECTIVE_DEDUP_MIN_TOKENS = 4


def _directive_tokens(text: str) -> set[str]:
    toks = _DIRECTIVE_TOKEN_RE.findall(str(text or "").lower())
    return {t for t in toks if t not in _DIRECTIVE_STOPWORDS}


def directives_near_duplicate(a: str, b: str, threshold: float = DIRECTIVE_NEAR_DUP_THRESHOLD) -> bool:
    """True when two director directives are paraphrases of the SAME step.

    The breaker surfaces a directive only when it is genuinely new work. Byte-exact
    equality catches identical re-emissions, but the real re-request failure mode is
    the judge RE-WORDING an already-satisfied instruction every debounce window
    (e.g. "Capture and attach proof artifacts ..." -> "Capture and attach THE proof
    artifacts ... then summarize ..."), which slips past `!=` and tells the model to
    redo work it just did.

    Metric: the overlap coefficient over content tokens (stopwords dropped) --
    |A and B| / min(|A|, |B|). Overlap (not Jaccard) is deliberate: a paraphrase that
    ADDS detail inflates the union and would push Jaccard below threshold, yet the
    shared core instruction still fills most of the SHORTER directive. Fail-safe: a
    too-short directive (< _DIRECTIVE_DEDUP_MIN_TOKENS content tokens) falls back to
    exact match, so a terse new instruction is never spuriously suppressed.
    """
    sa = str(a or "").strip()
    sb = str(b or "").strip()
    if not sa or not sb:
        return False
    if sa == sb:
        return True
    ta = _directive_tokens(sa)
    tb = _directive_tokens(sb)
    smaller = min(len(ta), len(tb))
    if smaller < _DIRECTIVE_DEDUP_MIN_TOKENS:
        return sa == sb
    return (len(ta & tb) / smaller) >= threshold


BREAKER_MAX_BLOCKS_DEFAULT = 3


_TRANSCRIPT_TOKEN_BUDGET = TRANSCRIPT_TOKEN_BUDGET


def max_blocks() -> int:
    try:
        return max(1, int(os.environ.get("UNIFABLE_BREAKER_MAX_BLOCKS", BREAKER_MAX_BLOCKS_DEFAULT)))
    except (TypeError, ValueError):
        return BREAKER_MAX_BLOCKS_DEFAULT


def is_mutation_tool(tool_name: str) -> bool:
    return tool_name in MUTATION_TOOLS


def is_release_tool(tool_name: str, input_data: dict | None = None) -> bool:
    tool = str(tool_name or "")
    if tool in RELEASE_TOOLS:
        return True
    if not isinstance(input_data, dict):
        return False
    try:
        from bash_classify import is_allowed_research_bash
        from parse_tool_result import (
            _REPL_CAT_RE,
            _REPL_READ_PATH_RE,
            _REPL_VIEW_IMAGE_PATH_RE,
            _REPL_WEBFETCH_URL_RE,
            _mcp_tool_is_read_like,
            command_from_input,
            is_mcp_tool,
            is_repl_tool,
            is_shell_tool,
            read_targets,
            repl_code_from_input,
            repl_shell_cmds_from_code,
        )
    except ImportError:
        from scripts.gate.bash_classify import is_allowed_research_bash  # pragma: no cover
        from scripts.gate.parse_tool_result import (  # pragma: no cover
            _REPL_CAT_RE,
            _REPL_READ_PATH_RE,
            _REPL_VIEW_IMAGE_PATH_RE,
            _REPL_WEBFETCH_URL_RE,
            _mcp_tool_is_read_like,
            command_from_input,
            is_mcp_tool,
            is_repl_tool,
            is_shell_tool,
            read_targets,
            repl_code_from_input,
            repl_shell_cmds_from_code,
        )
    if is_mcp_tool(tool) and _mcp_tool_is_read_like(tool):
        return bool(read_targets(input_data))
    if is_repl_tool(tool):
        code = repl_code_from_input(input_data)
        shell_cmds = repl_shell_cmds_from_code(code)
        if shell_cmds:
            return all(is_allowed_research_bash(cmd)[0] for cmd in shell_cmds)
        return bool(
            _REPL_READ_PATH_RE.search(code)
            or _REPL_CAT_RE.search(code)
            or _REPL_VIEW_IMAGE_PATH_RE.search(code)
            or _REPL_WEBFETCH_URL_RE.search(code)
        )
    if is_shell_tool(tool):
        allowed, _ = is_allowed_research_bash(command_from_input(input_data))
        return allowed
    return False


def _encode_cwd(cwd: str) -> str:
    return cwd.replace("/", "-").replace("_", "-")


def locate_transcript(input_data: dict) -> str | None:
    try:
        from transcript_locate import locate_transcript as _locate
    except ImportError:
        from scripts.gate.transcript_locate import locate_transcript as _locate  # pragma: no cover
    return _locate(input_data)


def transcript_segment(input_data: dict, max_tokens: int = _TRANSCRIPT_TOKEN_BUDGET) -> str:
    path = locate_transcript(input_data)
    if not path:
        return ""
    return stripped_transcript_tail(path, max_tokens)


def judge_transcript(
    input_data: dict,
    events: list[dict[str, Any]],
    *,
    fresh_tool: str | None = None,
    max_tokens: int = _TRANSCRIPT_TOKEN_BUDGET,
) -> str:
    """Merged judge input: transcript tail + breaker events + spec board + fresh tool.

    Ordered for prompt caching: the big append-only host transcript comes FIRST as
    the stable, cacheable prefix; the small volatile records (breaker events, spec
    board, fresh tool output) are reserved at the END so they cannot shift the
    cached prefix and so tail truncation never drops authoritative task status.
    The host transcript is bounded with a sticky retention window (not a sliding
    `[-n:]`) so its prefix stays byte-identical across same-session judge calls.
    """
    from transcript_tail import MAX_CHARS_PER_TOKEN, retention_window

    tail_parts: list[str] = []
    rendered = render_events(events)
    if rendered:
        tail_parts.append(rendered.rstrip())
    board = _spec_board_block(input_data)
    if board:
        tail_parts.append(board.rstrip())
    if fresh_tool and fresh_tool.strip():
        tail_parts.append('<record line="000000" type="fresh_tool" role="tool">\n' + fresh_tool.strip() + "\n</record>")

    reserve_chars = sum(len(p) + 2 for p in tail_parts)
    host_budget_chars = max(
        2000,
        (max_tokens * MAX_CHARS_PER_TOKEN) - reserve_chars,
    )
    parts: list[str] = []
    host = transcript_segment(input_data, max_tokens=max_tokens)
    if host:
        host = retention_window(host, host_budget_chars)
        parts.append(host.rstrip())
    parts.extend(tail_parts)

    if not parts:
        return ""
    combined = "\n\n".join(parts)
    return tail_tokens(combined, max_tokens)


def _spec_board_block(input_data: dict) -> str:
    """Current evidence-spec task board for breaker judges (authoritative status)."""
    try:
        from model_notify import format_spec_status
        from spec_io import canonical_project_root, load_spec, resolve_session_id

        cwd = canonical_project_root(input_data.get("cwd") or os.getcwd())
        session_key = resolve_session_id(input_data, default=None)
        if not session_key:
            return ""
        spec = load_spec(cwd, session_key)
        if not spec:
            return ""
        board = format_spec_status(spec, collapse_resolved=True)
        if not board.strip():
            return ""
        body = f"{_SPEC_BOARD_BEGIN}\n{board}\n{_SPEC_BOARD_END}"
        if len(body) > _SPEC_BOARD_MAX:
            body = body[: _SPEC_BOARD_MAX - 24] + "\n(spec board truncated)\n" + _SPEC_BOARD_END
        return body
    except Exception:
        return ""


def _user_goal_block(input_data: dict, active_task: str) -> str:
    """Best-effort restated goal from the session spec for judge context."""
    try:
        from spec_io import canonical_project_root, load_spec, resolve_session_id

        cwd = canonical_project_root(input_data.get("cwd") or os.getcwd())
        session_key = resolve_session_id(input_data, default=None)
        if not session_key:
            return ""
        spec = load_spec(cwd, session_key)
        if not spec:
            return ""
        goal = str(spec.get("restated_goal") or "").strip()
        if not goal:
            return ""
        if len(goal) > _USER_GOAL_MAX:
            return goal[: _USER_GOAL_MAX - 3] + "..."
        return goal
    except Exception:
        return ""


def _provisional_lift_message(reason: str, scope: str) -> str:
    return (
        f"Temporary lift: {reason} "
        f"Allowed scope: {scope}. Mutation tools stay available inside that scope."
    )


def _disarm_message() -> str:
    return "Claim grounded. Mutation tools and Bash are available again."


def _needed_message(needed: str) -> str:
    return f"Claim still ungrounded. Next: {needed}"


def _fail_open_message(count: int, claim: str) -> str:
    detail = f" Claim: {claim}" if claim else ""
    return (
        f"Claim gate auto-released after {count} consecutive blocks (fail-open). "
        "The claim was never grounded; mutation tools and Bash are available again -- "
        f"verify it yourself before relying on it.{detail}"
    )


def _stale_arm_message(claim: str) -> str:
    detail = f" (claim: {claim})" if claim else ""
    return (
        "Cleared stale ungrounded-claim state from a previous "
        f"prompt/session{detail}; mutation tools and Bash are available."
    )


def _apply_release(state: dict, claim: str, verdict: ReleaseVerdict) -> tuple[bool, str]:
    """Record release outcome on `state`. Returns (fully_disarmed, lift_notify_message)."""
    if verdict.grounded:
        append_event(state, "DISARM", claim=claim, grounded=True)
        record_adjudicated_claim(state, claim)
        disarm(state)
        return True, ""
    if verdict.provisional and verdict.lift_reason and verdict.lift_scope:
        notify = _provisional_lift_message(verdict.lift_reason, verdict.lift_scope)
        append_event(
            state,
            "LIFT",
            claim=claim,
            reason=verdict.lift_reason,
            scope=verdict.lift_scope,
        )
        lift_provisional(state, claim, verdict.lift_reason, verdict.lift_scope, notify)
        return False, notify
    if verdict.needed:
        append_event(state, "NEEDED", claim=claim, needed=verdict.needed)
        state["breaker_steering"] = verdict.needed
    return False, ""


def breaker_key(session_id: str, active_task: str) -> str:
    return f"{session_id or 'no-session'}|{active_task or ''}"


def resolve_task_lineage(input_data: dict, active_task: str) -> str:
    """Task-lineage component for breaker_key, robust to an empty active_task.

    The ledger's per-prompt ``active_task`` hash is the preferred signal, but in
    production it is empty ~90% of the time when the breaker runs (gate_prompt has
    not re-pinned it for this turn, notably after a /compact). An empty component
    collapses ``breaker_key`` to ``session|`` for EVERY prompt in the session, so
    the stale-arm drop (which fires only on key change) never triggers and a prior
    task's arm leaks into the next task. When active_task is empty we fall back to
    a fingerprint of the latest human user prompt -- stable within a task, distinct
    across tasks -- so task boundaries are tracked reliably. Fail-safe: any error
    or a missing transcript returns the original (possibly empty) active_task, so
    behavior never regresses below today's."""
    at = str(active_task or "").strip()
    if at:
        return at
    try:
        fp = latest_user_prompt_fingerprint(_locate_transcript(input_data))
    except Exception:
        return at
    return fp or at


def should_judge(state: dict, key: str, now: float, window: float = JUDGE_WINDOW_SECONDS) -> bool:
    if state.get("breaker_key") != key:
        return True
    last = state.get("breaker_judged_at") or 0.0
    try:
        return (now - float(last)) >= window
    except (TypeError, ValueError):
        return True


def should_coalesce(state: dict, key: str, now: float, window: float = JUDGE_COALESCE_WINDOW_SECONDS) -> bool:
    """True when a judge already fired for this key within the coalesce window.

    Used by the locked wrapper to mark later calls of the same parallel batch so
    they skip their (redundant) judge call. Requires a key match so a stale arm
    from a different prompt never suppresses a fresh judge."""
    if state.get("breaker_key") != key:
        return False
    last = state.get("breaker_judge_call_at") or 0.0
    if not last:
        return False
    try:
        # abs(): sibling processes of one batch capture time.time() independently,
        # and the first to take the lock may not hold the earliest stamp -- a few-ms
        # negative delta is still the same batch, so coalesce on proximity either way.
        return abs(now - float(last)) < window
    except (TypeError, ValueError):
        return False


def arm(state: dict, key: str, now: float, steering: str, claim: str) -> None:
    state["breaker_key"] = key
    state["breaker_judged_at"] = now
    state["breaker_armed"] = True
    state["breaker_steering"] = steering
    state["breaker_claim"] = claim
    state["breaker_armed_at"] = now
    state["breaker_block_count"] = 0
    append_event(state, "ARM", claim=claim, steering=steering)


def disarm(state: dict) -> None:
    state["breaker_armed"] = False
    state["breaker_steering"] = ""
    state["breaker_claim"] = ""
    state["breaker_armed_at"] = 0.0
    state["breaker_block_count"] = 0
    clear_provisional_lift(state)


def record_verdict(state: dict, key: str, now: float, verdict: int, steering: str, claim: str = "") -> None:
    if verdict == 1:
        arm(state, key, now, steering, claim)
        return
    disarm(state)
    state["breaker_key"] = key
    state["breaker_judged_at"] = now


def _release_log(count: int) -> None:
    try:
        sys.stderr.write(f"[breaker] auto-released after {count} consecutive blocks (fail-open)\n")
    except Exception:
        pass
