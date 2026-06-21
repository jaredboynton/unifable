#!/usr/bin/env python3
"""Per-session groundedness breaker state, separate from the activity ledger.

Operational state (armed/disarmed, debounce, block count) and an append-only event
log live here. Judges read only transcript material (host JSONL + rendered events);
this module is for hook persistence, not judge input beyond event rendering.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from atomicio import write_text_atomic
except ImportError:  # pragma: no cover
    from scripts.gate.atomicio import write_text_atomic

from ledger import data_root, ledger_key, ledger_path, utc_now

EVENT_KINDS = frozenset({"ARM", "DISARM", "NEEDED", "FAIL_OPEN", "STALE_ARM_DROPPED"})
MAX_EVENTS = 50

DEFAULT_BREAKER: dict[str, Any] = {
    "breaker_key": "",
    "breaker_judged_at": 0.0,
    "breaker_armed": False,
    "breaker_steering": "",
    "breaker_claim": "",
    "breaker_armed_at": 0.0,
    "breaker_block_count": 0,
    "events": [],
    "last_updated": "",
}


def default_breaker() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_BREAKER)


def breaker_path(input_data: dict[str, Any]) -> Path:
    return data_root() / "breaker" / f"{ledger_key(input_data)}.json"


def _event_ts() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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
        for key in ("claim", "steering", "needed", "block_count", "grounded"):
            value = event.get(key)
            if value not in (None, "", False):
                text = str(value).replace('"', "'").replace("\n", " ")
                parts.append(f'{key}="{text}"')
        padded = str(idx).zfill(6)
        lines.append(
            f'<record line="{padded}" type="unifable_breaker" role="gate">\n'
            + " ".join(parts)
            + "\n</record>"
        )
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


def claim_already_adjudicated(claim: str, events: list[dict[str, Any]]) -> bool:
    normalized = claim.strip().lower()
    if not normalized:
        return False
    for old in adjudicated_claims(events):
        old_norm = old.strip().lower()
        if normalized == old_norm or normalized in old_norm or old_norm in normalized:
            return True
    return False


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


def load_breaker(input_data: dict[str, Any]) -> dict[str, Any]:
    path = breaker_path(input_data)
    if not path.is_file():
        migrated = _migrate_from_ledger(input_data)
        if migrated is not None:
            save_breaker(input_data, migrated)
            return migrated
        return default_breaker()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_breaker()
    state = default_breaker()
    if isinstance(data, dict):
        state.update({key: data.get(key, value) for key, value in state.items()})
    if not isinstance(state.get("events"), list):
        state["events"] = []
    trim_breaker(state)
    return state


def save_breaker(input_data: dict[str, Any], state: dict[str, Any]) -> Path:
    path = breaker_path(input_data)
    trim_breaker(state)
    state["last_updated"] = utc_now()
    return write_text_atomic(path, json.dumps(state, indent=2, sort_keys=True))
