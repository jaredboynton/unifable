#!/usr/bin/env python3
"""Canonical runtime inventory + classifier for the Python-consolidation migration.

Walks the shipped runtime trees (hooks, scripts, setup, bin, skills, packs) and
classifies every non-Python file into exactly one state:

  active       implementation on a supported runtime path; by final acceptance it
               MUST NOT require Node/Bun.
  compat-shim  thin launcher that exists only to exec Python (host launch semantics).
  legacy-flag  reachable only behind an env rollback flag; off by default, time-boxed.
  archived     retired variant kept for reference; must be excluded from runtime sync
               (or explicitly allowlisted).
  fixture      test/bench/dev tooling; not an active runtime path; must be excluded
               from runtime sync (or explicitly allowlisted).

Classification is rule-driven and deterministic. Every row carries a non-empty
owner and reason. A repo-owned allowlist (docs/benchmarks/python-consolidation-runtime-allowlist.json)
provides per-path overrides; each override must name an owner and reason too.

The audit is both filename-based (extension / executable launcher) and
content-based: it scans file bodies for `node`, `bun`, `.mjs`, `.js`, and
npm/yarn/pnpm so a Python file that shells out to Node is caught even though its
extension is `.py`.

Usage:
  audit_runtime_inventory.py [--write-artifact PATH]
  audit_runtime_inventory.py --fail-on-active node --fail-on-active bun [--write-artifact PATH]

Exit codes:
  0  inventory built; every row classified with owner+reason; no --fail-on-active
     token appears on an `active` row.
  1  an unclassified row, an empty owner/reason, a missing allowlisted path, or a
     forbidden token on an `active` row.
  2  usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ALLOWLIST = REPO / "docs" / "benchmarks" / "python-consolidation-runtime-allowlist.json"

# Top-level trees that ship as runtime. Mirrors runtime_sync._RUNTIME_TREE minus
# the pure-metadata plugin dirs (those carry no executable non-Python files).
RUNTIME_TREES = ("hooks", "scripts", "unifable_runtime", "setup", "bin", "skills", "packs")

CLASSES = ("active", "compat-shim", "legacy-flag", "archived", "fixture")

# Non-Python runtime file extensions that need a classification.
_NONPY_EXT = (".mjs", ".js", ".cjs", ".ts", ".sh", ".rb")

# Content tokens that mark a Node/Bun/npm dependency. Word-boundaried so `node`
# does not match `nodejs_path_variable` substrings spuriously; `.mjs`/`.js` are
# matched as file-suffix references.
_CONTENT_TOKENS = {
    "node": re.compile(r"\bnode\b"),
    "bun": re.compile(r"\bbun\b"),
    "npm": re.compile(r"\bnpm\b"),
    "yarn": re.compile(r"\byarn\b"),
    "pnpm": re.compile(r"\bpnpm\b"),
    ".mjs": re.compile(r"\.mjs\b"),
    ".js": re.compile(r"\.js\b"),
}

# bin/ stable launchers are extensionless executables.
_BIN_LAUNCHERS = {"unifable", "unifable-hook", "unifable-spec"}


@dataclass
class Row:
    path: str
    classification: str
    owner: str
    reason: str
    is_nonpy: bool
    content_tokens: list[str]


def _git_tracked(repo: Path) -> list[Path]:
    """All tracked files under the runtime trees, relative to repo root."""
    import subprocess

    args = ["git", "-C", str(repo), "ls-files", *[f"{t}/**" for t in RUNTIME_TREES]]
    try:
        out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        # Fallback: filesystem walk (keeps the audit usable outside a git checkout).
        files: list[Path] = []
        for tree in RUNTIME_TREES:
            base = repo / tree
            if base.is_dir():
                files.extend(p.relative_to(repo) for p in base.rglob("*") if p.is_file())
        return sorted(files)
    return sorted(Path(line) for line in out.splitlines() if line.strip())


def _is_nonpy_runtime(rel: Path) -> bool:
    """True if this file needs a classification on filename grounds."""
    if rel.suffix in _NONPY_EXT:
        return True
    if rel.parts[0] == "bin" and rel.name in _BIN_LAUNCHERS:
        return True
    return False


def _scan_content(abs_path: Path) -> list[str]:
    """Return the sorted content tokens present in a text file body."""
    try:
        body = abs_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    return sorted(tok for tok, pat in _CONTENT_TOKENS.items() if pat.search(body))


def _load_allowlist() -> dict[str, dict]:
    if not ALLOWLIST.is_file():
        return {}
    try:
        data = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {e["path"]: e for e in data.get("entries", []) if "path" in e}


def _default_classify(rel: Path) -> tuple[str, str, str] | None:
    """Rule-based (classification, owner, reason) for a non-Python file, or None.

    None means no rule matched and the file must appear in the allowlist.
    Rules are ordered most-specific first.
    """
    posix = rel.as_posix()
    name = rel.name

    if "/archive/" in posix:
        return ("archived", "explore-skill", "retired variant kept for reference; excluded from active runtime")
    if "/bench/" in posix or name.startswith(("bench-", "bench_")):
        return ("fixture", "explore-skill", "benchmark harness; dev-only, not an active runtime path")
    if "/test/" in posix or name.startswith(("test-", "test_")) or name.endswith((".test.mjs", "-test.mjs")):
        return ("fixture", "explore-skill", "test harness; dev-only, not an active runtime path")

    if rel.parts[0] == "bin" and name in _BIN_LAUNCHERS:
        return ("compat-shim", "runtime", "stable launcher that execs the synced Python runtime")
    if rel.parts[0] == "setup" and rel.suffix == ".sh":
        return ("compat-shim", "setup", "install/uninstall launcher; ports to Python entrypoint with a shell shim")

    if posix.startswith("skills/explore/scripts/"):
        return ("active", "explore-skill", "supported explore runtime path; targeted for Python port")
    if posix.startswith("skills/unifusion/scripts/"):
        return ("active", "unifusion-skill", "supported Unifusion runtime path; targeted for Python port")

    return None


def build_inventory(repo: Path = REPO) -> tuple[list[Row], list[str]]:
    """Return (rows, problems). problems is empty when every row is valid."""
    allowlist = _load_allowlist()
    seen_allow: set[str] = set()
    rows: list[Row] = []
    problems: list[str] = []

    for rel in _git_tracked(repo):
        abs_path = repo / rel
        if not abs_path.is_file():
            continue
        nonpy = _is_nonpy_runtime(rel)
        tokens = _scan_content(abs_path) if (nonpy or rel.suffix == ".py") else []
        # A pure-Python file with no Node tokens is not a classification subject.
        if not nonpy and not tokens:
            continue
        # Python files with content hits are reported but not state-classified
        # (their state is "python"); only non-Python files carry a CLASS.
        if not nonpy:
            rows.append(Row(rel.as_posix(), "python", "gate", "Python file flagged for Node/Bun content reference", False, tokens))
            continue

        posix = rel.as_posix()
        override = allowlist.get(posix)
        if override:
            seen_allow.add(posix)
            cls = override.get("classification", "")
            owner = override.get("owner", "")
            reason = override.get("reason", "")
        else:
            ruled = _default_classify(rel)
            if ruled is None:
                problems.append(f"unclassified runtime file (add to allowlist): {posix}")
                continue
            cls, owner, reason = ruled

        if cls not in CLASSES:
            problems.append(f"invalid classification '{cls}' for {posix}")
        if not owner.strip():
            problems.append(f"empty owner for {posix}")
        if not reason.strip():
            problems.append(f"empty reason for {posix}")
        rows.append(Row(posix, cls, owner, reason, True, tokens))

    for path in allowlist:
        if path not in seen_allow and not (repo / path).is_file():
            problems.append(f"allowlist entry points to missing file: {path}")

    rows.sort(key=lambda r: r.path)
    return rows, problems


def summarize(rows: list[Row]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.classification] = counts.get(r.classification, 0) + 1
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Runtime inventory classifier")
    ap.add_argument("--write-artifact", type=Path, help="write inventory JSON to this path")
    ap.add_argument(
        "--fail-on-active",
        action="append",
        default=[],
        metavar="TOKEN",
        help="fail if TOKEN (node/bun/.mjs/...) appears on an active row; repeatable",
    )
    args = ap.parse_args(argv)

    rows, problems = build_inventory()
    forbidden = set(args.fail_on_active)
    active_hits: list[str] = []
    if forbidden:
        for r in rows:
            if r.classification != "active":
                continue
            hit = forbidden.intersection(r.content_tokens)
            # A .mjs/.js extension is itself a forbidden-token hit.
            if r.path.endswith(".mjs") and (".mjs" in forbidden):
                hit = hit | {".mjs"}
            if r.path.endswith(".js") and (".js" in forbidden):
                hit = hit | {".js"}
            if hit:
                active_hits.append(f"{r.path}: active row carries forbidden {sorted(hit)}")

    artifact = {
        "trees": list(RUNTIME_TREES),
        "counts": summarize(rows),
        "rows": [asdict(r) for r in rows],
        "problems": problems,
        "fail_on_active": sorted(forbidden),
        "active_hits": active_hits,
    }
    if args.write_artifact:
        args.write_artifact.parent.mkdir(parents=True, exist_ok=True)
        args.write_artifact.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    for p in problems:
        print(f"PROBLEM: {p}", file=sys.stderr)
    for h in active_hits:
        print(f"FAIL: {h}", file=sys.stderr)

    if problems or active_hits:
        return 1
    counts = summarize(rows)
    print(json.dumps({"counts": counts, "rows": len(rows)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
