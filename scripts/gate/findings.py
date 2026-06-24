#!/usr/bin/env python3
"""Per-project findings store for the unifable observation gate.

State lives at <root>/.unifable/findings.json (repo-local, one file per project).
This is distinct from the per-session ledger under ~/.unifable/ledgers/.

Severity tiers: low | medium | high | critical
Status lifecycle: open -> resolved | rejected
                  open -> blocked  (external hold, still counts as open for gate)

blocking_findings(root) returns all severity=high|critical AND status=open|blocked.
The Stop gate imports this function and blocks completion while the list is non-empty.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:  # bare import when scripts/gate is on sys.path (hooks + tests); package import otherwise
    from atomicio import write_text_atomic
except ImportError:  # pragma: no cover
    from scripts.gate.atomicio import write_text_atomic

try:  # POSIX advisory locking; absent on some platforms (e.g. Windows)
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

SEVERITIES = ("low", "medium", "high", "critical")


@contextlib.contextmanager
def _findings_lock(root: str | Path):
    """Serialize load-modify-save on the findings store.

    findings.json gates Stop completion (blocking_findings); a counter-keyed id
    plus last-writer-wins means two concurrent add_finding calls can mint the same
    id and clobber a finding that should block. This exclusive lock on a sibling
    ``.lock`` file makes the read-modify-write a critical section. Writes here are
    infrequent (CLI/agent driven, not the per-tool-call hot path), so the lock is
    free in practice. No-op where fcntl is unavailable.
    """
    if fcntl is None:  # pragma: no cover
        yield
        return
    lock_path = Path(str(_findings_path(root)) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


STATUSES = ("open", "blocked", "resolved", "rejected")
BLOCKING_SEVERITIES = {"high", "critical"}
BLOCKING_STATUSES = {"open", "blocked"}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _findings_path(root: str | Path) -> Path:
    return Path(root).resolve() / ".unifable" / "findings.json"


def _slug(title: str) -> str:
    return _SLUG_RE.sub("-", title.lower().strip())[:24].strip("-")


def load_findings(root: str | Path) -> dict[str, Any]:
    path = _findings_path(root)
    if not path.exists():
        return {"findings": {}, "counter": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"findings": {}, "counter": 0}
    if not isinstance(data, dict):
        return {"findings": {}, "counter": 0}
    data.setdefault("findings", {})
    data.setdefault("counter", 0)
    return data


def save_findings(root: str | Path, data: dict[str, Any]) -> Path:
    path = _findings_path(root)
    return write_text_atomic(path, json.dumps(data, indent=2, sort_keys=True))


def add_finding(
    root: str | Path,
    title: str,
    severity: str,
    *,
    source: str = "",
    location: str = "",
    evidence: str = "",
) -> str:
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {SEVERITIES}")
    with _findings_lock(root):
        data = load_findings(root)
        data["counter"] = data.get("counter", 0) + 1
        fid = f"{_slug(title)}-{data['counter']}"
        data["findings"][fid] = {
            "id": fid,
            "title": title,
            "severity": severity,
            "source": source,
            "location": location,
            "evidence": evidence,
            "status": "open",
            "resolution": "",
            "verify_cmd": "",
            "verify_evidence": "",
            "created": _utc_now(),
        }
        save_findings(root, data)
    return fid


def set_status(
    root: str | Path,
    fid: str,
    status: str,
    *,
    resolution: str | None = None,
    verify_cmd: str | None = None,
    verify_evidence: str | None = None,
) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    with _findings_lock(root):
        data = load_findings(root)
        finding = data["findings"].get(fid)
        if finding is None:
            raise KeyError(f"finding {fid!r} not found")
        finding["status"] = status
        if resolution is not None:
            finding["resolution"] = resolution
        if verify_cmd is not None:
            finding["verify_cmd"] = verify_cmd
        if verify_evidence is not None:
            finding["verify_evidence"] = verify_evidence
        save_findings(root, data)
    return finding


def get_finding(root: str | Path, fid: str) -> dict[str, Any] | None:
    data = load_findings(root)
    return data["findings"].get(fid)


def open_findings(root: str | Path) -> list[dict[str, Any]]:
    data = load_findings(root)
    return [f for f in data["findings"].values() if f.get("status") == "open"]


def blocking_findings(root: str | Path) -> list[dict[str, Any]]:
    """Return findings that block Stop: severity high|critical AND status open|blocked."""
    data = load_findings(root)
    return [
        f for f in data["findings"].values() if f.get("severity") in BLOCKING_SEVERITIES and f.get("status") in BLOCKING_STATUSES
    ]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_add(args: argparse.Namespace) -> int:
    fid = add_finding(
        args.root,
        args.title,
        args.severity,
        source=args.source or "",
        location=args.location or "",
        evidence=args.evidence or "",
    )
    print(fid)
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    data = load_findings(args.root)
    findings = list(data["findings"].values())
    if args.status:
        findings = [f for f in findings if f.get("status") == args.status]
    if not findings:
        print("(no findings)")
        return 0
    for f in findings:
        print(f"[{f['id']}] {f['severity'].upper()} {f['status']} — {f['title']}")
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    set_status(
        args.root,
        args.id,
        "resolved",
        resolution=args.evidence,
        verify_cmd=args.verify_cmd or "",
        verify_evidence=args.verify_evidence or "",
    )
    print(f"resolved {args.id}")
    return 0


def _cmd_reject(args: argparse.Namespace) -> int:
    set_status(args.root, args.id, "rejected", resolution=args.reason)
    print(f"rejected {args.id}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    f = get_finding(args.root, args.id)
    if f is None:
        print(f"not found: {args.id}", file=sys.stderr)
        return 1
    print(json.dumps(f, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="findings",
        description="Per-project findings store for the unifable gate.",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Project root (default: cwd). findings.json lives at <root>/.unifable/findings.json",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="Record a new finding")
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--severity", required=True, choices=SEVERITIES)
    p_add.add_argument("--source", default="")
    p_add.add_argument("--location", default="")
    p_add.add_argument("--evidence", default="")
    p_add.set_defaults(func=_cmd_add)

    p_list = sub.add_parser("list", help="List findings")
    p_list.add_argument("--status", choices=STATUSES, help="Filter by status")
    p_list.set_defaults(func=_cmd_list)

    p_resolve = sub.add_parser("resolve", help="Mark a finding resolved")
    p_resolve.add_argument("id")
    p_resolve.add_argument("--evidence", required=True, help="Resolution evidence")
    p_resolve.add_argument("--verify-cmd", dest="verify_cmd", default="")
    p_resolve.add_argument("--verify-evidence", dest="verify_evidence", default="")
    p_resolve.set_defaults(func=_cmd_resolve)

    p_reject = sub.add_parser("reject", help="Mark a finding rejected")
    p_reject.add_argument("id")
    p_reject.add_argument("--reason", required=True)
    p_reject.set_defaults(func=_cmd_reject)

    p_show = sub.add_parser("show", help="Show a single finding as JSON")
    p_show.add_argument("id")
    p_show.set_defaults(func=_cmd_show)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
