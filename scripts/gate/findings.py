#!/usr/bin/env python3
"""Per-project findings store for the unifable observation gate.

State lives in the consolidated SQLite DB (db.findings + db.projects), keyed by a
stable hash of the project root, so it is distinct from the per-session ledger
and survives concurrent writers without a userspace lock. A legacy
``<root>/.unifable/findings.json`` is imported once on first access.

Severity tiers: low | medium | high | critical
Status lifecycle: open -> resolved | rejected
                  open -> blocked  (external hold, still counts as open for gate)

blocking_findings(root) returns all severity=high|critical AND status=open|blocked.
The Stop gate imports this function and blocks completion while the list is non-empty.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

try:  # bare import when scripts/gate is on sys.path (hooks + tests); package import otherwise
    import db
    from ledger import resolve_path
except ImportError:  # pragma: no cover
    from scripts.gate import db
    from scripts.gate.ledger import resolve_path

SEVERITIES = ("low", "medium", "high", "critical")
STATUSES = ("open", "blocked", "resolved", "rejected")
BLOCKING_SEVERITIES = {"high", "critical"}
BLOCKING_STATUSES = {"open", "blocked"}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _resolved_root(root: str | Path) -> Path:
    return resolve_path(root)


def _findings_path(root: str | Path) -> Path:
    """Legacy on-disk location, retained for messages and one-time import."""
    return _resolved_root(root) / ".unifable" / "findings.json"


def _root_hash(root: str | Path) -> str:
    return hashlib.sha256(str(_resolved_root(root)).encode("utf-8", "replace")).hexdigest()[:16]


def _slug(title: str) -> str:
    return _SLUG_RE.sub("-", title.lower().strip())[:24].strip("-")


def _import_legacy_findings(root: str | Path, root_hash: str) -> None:
    """One-time import of a legacy findings.json into the DB on first miss."""
    path = _findings_path(root)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict) or not data.get("findings"):
        return
    db.findings_replace(root_hash, str(_resolved_root(root)), data)


def load_findings(root: str | Path) -> dict[str, Any]:
    root_hash = _root_hash(root)
    data = db.findings_load(root_hash)
    if not data.get("findings") and not data.get("counter"):
        _import_legacy_findings(root, root_hash)
        data = db.findings_load(root_hash)
    return data


def save_findings(root: str | Path, data: dict[str, Any]) -> Path:
    db.findings_replace(_root_hash(root), str(_resolved_root(root)), data)
    return _findings_path(root)


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
    # Ensure any legacy file is imported so the per-project counter continues.
    load_findings(root)
    fid = db.finding_add(
        _root_hash(root),
        str(_resolved_root(root)),
        _slug(title),
        title,
        severity,
        source=source,
        location=location,
        evidence=evidence,
    )
    if fid is None:  # DB unavailable: fail open rather than crash an explicit action
        raise RuntimeError("findings store unavailable")
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
    load_findings(root)  # import legacy rows so set_status can find them
    finding = db.finding_set_status(
        _root_hash(root),
        fid,
        status,
        resolution=resolution,
        verify_cmd=verify_cmd,
        verify_evidence=verify_evidence,
    )
    if finding is None:  # DB unavailable
        raise RuntimeError("findings store unavailable")
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
        help="Project root (default: cwd). Findings are stored in the consolidated unifable DB, keyed by project root.",
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
