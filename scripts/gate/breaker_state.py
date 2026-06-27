#!/usr/bin/env python3
"""Per-session groundedness breaker state, separate from the activity ledger.

Operational state (armed/disarmed, debounce, block count) and an append-only event
log live here. Judges read only transcript material (host JSONL + rendered events);
this module is for hook persistence, not judge input beyond event rendering.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from ledger import data_root, ledger_key, ledger_path, utc_now

EVENT_KINDS = frozenset(
    {
        "ARM",
        "DISARM",
        "NEEDED",
        "FAIL_OPEN",
        "STALE_ARM_DROPPED",
        "LIFT",
        "REINSTATE",
        "SCOPE_HINT",
        "VERIFY_DISPATCH",
        "VERIFY_RESULT",
    }
)
MAX_EVENTS = 50

DEFAULT_BREAKER: dict[str, Any] = {
    "breaker_key": "",
    "breaker_judged_at": 0.0,
    # Wall-clock of the last judge API call (any kind), used only to coalesce a
    # parallel tool-call batch. Separate from breaker_judged_at so the 15s arm
    # debounce is untouched.
    "breaker_judge_call_at": 0.0,
    "breaker_armed": False,
    "breaker_steering": "",
    # Stepwise director: the minimal next-step instruction and the tool scope the
    # director judge persists on each debounced call. The PreToolUse hook enforces
    # breaker_tool_scope deterministically (no judge call) via tool_scope.in_scope;
    # breaker_directive is surfaced to the model as the current instruction.
    "breaker_directive": "",
    "breaker_tool_scope": {},
    # Last director directive actually surfaced to the model, so an unchanged
    # directive is not re-emitted every debounce window (token-aware/minimal).
    "breaker_last_directive_surfaced": "",
    "breaker_claim": "",
    # Judge-granted evidence-gate lift (scripts/gate/gate_lift.py): a scoped grant
    # ({signature, command, paths, scope, uses}) authorizing one blocked mutation,
    # plus a per-session synchronous lift-judge call counter that bounds runaway
    # judging. Must live in DEFAULT_BREAKER or load_breaker drops it on every load.
    "breaker_gate_lift": {},
    "breaker_gate_lift_calls": 0,
    # Durable adjudicated-claims guard: every claim resolved via DISARM or
    # FAIL_OPEN is appended here so the re-arm suppression survives the bounded
    # `events` trim (MAX_EVENTS) and a /compact. Bounded, deduped. Must live in
    # DEFAULT_BREAKER or load_breaker drops it on every load.
    "breaker_adjudicated_claims": [],
    "breaker_armed_at": 0.0,
    "breaker_block_count": 0,
    # Auto-grounding (async verification lane, scripts/gate/verify_lane.py): when the
    # breaker arms on a claim that grounds only by RUNNING repo-sanctioned checks, it
    # dispatches a detached background runner and tracks it here. breaker_verify_key
    # is the sidecar cache key (claim + repo state); breaker_verify_tasks mirrors the
    # atomic {subclaim, command, status, exit, tail} list the poll path advances;
    # breaker_verify_dispatched_at bounds the block-count-cap exemption while the
    # suite runs. Cleared on disarm. Distinct from provisional (the MODEL pursues
    # verification) -- here the BREAKER pursues it.
    "breaker_verify_key": "",
    "breaker_verify_tasks": [],
    "breaker_verify_dispatched_at": 0.0,
    "breaker_provisional": False,
    "breaker_lift_reason": "",
    "breaker_lift_scope": "",
    "breaker_pending_notify": "",
    "events": [],
    "last_updated": "",
}


def default_breaker() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_BREAKER)


def breaker_path(input_data: dict[str, Any]) -> Path:
    return data_root() / "breaker" / f"{ledger_key(input_data)}.json"


def _lock_timeout() -> float:
    """Bounded wait for the breaker judge lock.

    The lock is held across the judge API call (which can run up to its own
    UNIFABLE_JUDGE_TIMEOUT), so a parallel batch serializes on it: the first
    process judges, the rest wait then reuse the result. Capped well below the
    judge timeout so a genuinely hung judge never stalls the batch -- on expiry
    we proceed unlocked (fail-open, degrading to the pre-coalesce stampede)."""
    try:
        return max(0.0, float(os.environ.get("UNIFABLE_BREAKER_LOCK_TIMEOUT", "12.0") or "12.0"))
    except (TypeError, ValueError):
        return 12.0


_LOCK_POLL_SECONDS = 0.02


@contextlib.contextmanager
def breaker_lock(input_data: dict[str, Any], timeout: float | None = None):
    """Cross-process exclusive lock for the breaker read-modify-(judge)-write.

    Fail-open by construction: if fcntl is unavailable or the lock cannot be
    acquired within `timeout`, the body still runs (unlocked). A crashed holder
    releases the flock automatically, so a dead process can never wedge a batch."""
    if fcntl is None:  # pragma: no cover
        yield
        return
    wait = _lock_timeout() if timeout is None else max(0.0, float(timeout))
    path = breaker_path(input_data)
    lock_path = path.parent / f"{path.name}.judge.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:  # pragma: no cover - filesystem failure: do not block the tool
        yield
        return
    acquired = False
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break  # fail-open: proceed unlocked rather than stall
                time.sleep(_LOCK_POLL_SECONDS)
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def _event_ts() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def render_events(events: list[dict[str, Any]]) -> str:
    """Render breaker events as stripped transcript-style records for judge prompts."""
    if not events:
        return ""
    lines: list[str] = []
    for idx, event in enumerate(events, start=1):
        kind = str(event.get("kind") or "UNKNOWN")
        ts = str(event.get("ts") or "")
        parts = [f"event={kind}"]
        if ts:
            parts.append(f'timestamp="{ts}"')
        for key in (
            "claim",
            "steering",
            "needed",
            "block_count",
            "grounded",
            "reason",
            "scope",
            "corrective",
            "hint",
            "subclaim",
            "command",
            "exit",
        ):
            value = event.get(key)
            if value not in (None, "", False):
                text = str(value).replace('"', "'").replace("\n", " ")
                parts.append(f'{key}="{text}"')
        padded = str(idx).zfill(6)
        lines.append(f'<record line="{padded}" type="unifable_breaker" role="gate">\n' + " ".join(parts) + "\n</record>")
    return "\n".join(lines) + "\n"


def adjudicated_claims(events: list[dict[str, Any]]) -> list[str]:
    """Claims that must not re-arm (DISARM or FAIL_OPEN events)."""
    claims: list[str] = []
    for event in events:
        kind = str(event.get("kind") or "")
        if kind not in ("DISARM", "FAIL_OPEN"):
            continue
        claim = str(event.get("claim") or "").strip()
        if claim and claim not in claims:
            claims.append(claim)
    return claims


# Generic claim scaffolding dropped before the token-overlap test so two claims
# that share only filler ("the", "is", "a") are not treated as the same claim.
_CLAIM_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "to", "of", "for", "in", "on", "then", "with",
        "that", "this", "it", "is", "are", "be", "by", "as", "at", "from", "into",
        "not", "any", "all", "so", "its", "their", "them", "was", "were", "has", "have",
        "causing", "because", "due", "via", "field",
    }
)
_CLAIM_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Overlap-coefficient threshold above which a claim counts as a paraphrase of an
# already-adjudicated one. Mirrors DIRECTIVE_NEAR_DUP_THRESHOLD in breaker_runtime;
# kept high so only genuine restatements (not merely topical overlap) are caught.
_CLAIM_OVERLAP_THRESHOLD = 0.7
_CLAIM_OVERLAP_MIN_TOKENS = 4


def _claim_tokens(text: str) -> set[str]:
    return {t for t in _CLAIM_TOKEN_RE.findall(str(text or "").lower()) if t not in _CLAIM_STOPWORDS}


def _claims_paraphrase(a: str, b: str) -> bool:
    """True when two claims are paraphrases of each other (overlap coefficient).

    Overlap (|A and B| / min(|A|,|B|)), not Jaccard: a paraphrase that ADDS detail
    inflates the union and would sink Jaccard below threshold, yet the shared core
    still fills most of the shorter claim. Both claims must clear a minimum token
    count, so a terse claim never spuriously matches on one or two shared words."""
    ta, tb = _claim_tokens(a), _claim_tokens(b)
    smaller = min(len(ta), len(tb))
    if smaller < _CLAIM_OVERLAP_MIN_TOKENS:
        return False
    return (len(ta & tb) / smaller) >= _CLAIM_OVERLAP_THRESHOLD


def claim_already_adjudicated(
    claim: str,
    events: list[dict[str, Any]],
    extra_claims: list[str] | None = None,
) -> bool:
    """True when *claim* matches an already-adjudicated (DISARM/FAIL_OPEN) claim.

    Sources scanned: the bounded `events` log PLUS `extra_claims` -- the durable
    `breaker_adjudicated_claims` list, which survives the MAX_EVENTS trim and a
    /compact so a resolved claim cannot silently re-arm. Matching: exact, then
    bidirectional substring containment, then token-overlap paraphrase, so a judge
    re-wording a resolved claim after compact does not slip through."""
    normalized = claim.strip().lower()
    if not normalized:
        return False
    candidates = list(adjudicated_claims(events))
    for old in extra_claims or []:
        old_s = str(old or "").strip()
        if old_s and old_s not in candidates:
            candidates.append(old_s)
    for old in candidates:
        old_norm = old.strip().lower()
        if not old_norm:
            continue
        if normalized == old_norm or normalized in old_norm or old_norm in normalized:
            return True
        if _claims_paraphrase(normalized, old_norm):
            return True
    return False


def record_adjudicated_claim(state: dict[str, Any], claim: str) -> None:
    """Append *claim* to the durable adjudicated-claims list (bounded, deduped).

    Called on every DISARM and FAIL_OPEN so the suppression guard does not depend
    on the bounded event log. Bounded by MAX_EVENTS to stay small."""
    c = str(claim or "").strip()
    if not c:
        return
    lst = state.get("breaker_adjudicated_claims")
    if not isinstance(lst, list):
        lst = []
    if c not in lst:
        lst.append(c)
    state["breaker_adjudicated_claims"] = lst[-MAX_EVENTS:]


def append_event(state: dict[str, Any], kind: str, **fields: Any) -> None:
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown breaker event kind: {kind}")
    events = state.get("events")
    if not isinstance(events, list):
        events = []
    event = {"kind": kind, "ts": _event_ts(), **fields}
    events.append(event)
    state["events"] = events[-MAX_EVENTS:]


def trim_breaker(state: dict[str, Any]) -> None:
    events = state.get("events")
    if isinstance(events, list):
        state["events"] = events[-MAX_EVENTS:]


def clear_provisional_lift(state: dict[str, Any]) -> None:
    state["breaker_provisional"] = False
    state["breaker_lift_reason"] = ""
    state["breaker_lift_scope"] = ""
    state["breaker_pending_notify"] = ""


def lift_provisional(
    state: dict[str, Any],
    claim: str,
    reason: str,
    scope: str,
    pending_notify: str,
) -> None:
    state["breaker_armed"] = False
    state["breaker_provisional"] = True
    state["breaker_claim"] = claim
    state["breaker_lift_reason"] = reason
    state["breaker_lift_scope"] = scope
    state["breaker_pending_notify"] = pending_notify
    state["breaker_steering"] = ""
    state["breaker_armed_at"] = 0.0
    state["breaker_block_count"] = 0


def reinstate(state: dict[str, Any], claim: str, corrective: str) -> None:
    clear_provisional_lift(state)
    state["breaker_armed"] = True
    state["breaker_claim"] = claim
    state["breaker_steering"] = corrective
    state["breaker_block_count"] = 0


def _migrate_from_ledger(input_data: dict[str, Any]) -> dict[str, Any] | None:
    """One-time copy from legacy ledger breaker fields when breaker file is absent."""
    lp = ledger_path(input_data)
    if not lp.is_file():
        return None
    try:
        ledger = json.loads(lp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(ledger, dict) or not ledger.get("breaker_armed"):
        return None
    state = default_breaker()
    for key in DEFAULT_BREAKER:
        if key == "events":
            continue
        if key in ledger:
            state[key] = ledger[key]
    claim = str(state.get("breaker_claim") or "")
    steering = str(state.get("breaker_steering") or "")
    if claim or steering:
        append_event(state, "ARM", claim=claim, steering=steering)
    for old in ledger.get("breaker_adjudicated_claims") or []:
        if old and isinstance(old, str):
            append_event(state, "DISARM", claim=old, grounded=True)
    return state


def _import_legacy_breaker(input_data: dict[str, Any]) -> dict[str, Any] | None:
    """One-time import of legacy breaker state into the DB: prefer the standalone
    ``breaker/{key}.json`` file, else copy the even-older inline ledger breaker
    fields. Returns a normalized breaker dict, or None when nothing to import."""
    data: dict[str, Any] | None = None
    path = breaker_path(input_data)
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
        except (OSError, json.JSONDecodeError):
            data = None
    if data is None:
        data = _migrate_from_ledger(input_data)
    if data is None:
        return None
    state = default_breaker()
    state.update({key: data.get(key, value) for key, value in state.items()})
    if not isinstance(state.get("events"), list):
        state["events"] = []
    try:
        import db

        db.breaker_save(ledger_key(input_data), state)
    except Exception:
        pass
    return state


def load_breaker(input_data: dict[str, Any]) -> dict[str, Any]:
    # Storage is the consolidated SQLite DB (db.breaker + db.breaker_events). The
    # legacy breaker/{key}.json (or older inline ledger fields) is imported once on
    # first miss. Any DB error fails open to a fresh default breaker.
    try:
        import db

        data = db.breaker_load(ledger_key(input_data))
    except Exception:
        data = None
    if data is None:
        data = _import_legacy_breaker(input_data)
    if data is None:
        return default_breaker()
    state = default_breaker()
    if isinstance(data, dict):
        state.update({key: data.get(key, value) for key, value in state.items()})
    if not isinstance(state.get("events"), list):
        state["events"] = []
    if not isinstance(state.get("breaker_adjudicated_claims"), list):
        state["breaker_adjudicated_claims"] = []
    trim_breaker(state)
    return state


def save_breaker(input_data: dict[str, Any], state: dict[str, Any]) -> Path:
    # Storage is the consolidated SQLite DB. The breaker read-modify-(judge)-write
    # is still serialized by the POSIX flock in breaker_lock() -- that lock guards
    # the expensive judge API CALL (coalescing parallel hooks into one), a concern
    # orthogonal to storage that WAL does not and must not replace. Returns the
    # legacy path for signature compatibility.
    trim_breaker(state)
    state["last_updated"] = utc_now()
    try:
        import db

        db.breaker_save(ledger_key(input_data), state)
    except Exception:
        pass
    return breaker_path(input_data)
