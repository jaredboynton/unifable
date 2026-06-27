#!/usr/bin/env python3
"""Async background verification lane for the groundedness breaker (auto-grounding).

When the breaker arms on a load-bearing claim that can only be grounded by RUNNING
repo-sanctioned verification (test suites, builds, release/publication checks) -- the
read-only self-resolve lanes (verify predicate / explore search / single read-only
command) cannot cover those -- the judge decomposes the claim into atomic
{subclaim, command} tasks. This module:

  1. sanction_command: the safety boundary. A command runs ONLY if it is a
     verification-shaped command the repo's OWN policy names (justfile target,
     documented test invocation, a check spelled in AGENTS.md/CHANGELOG) and carries
     no destructive/publishing token. Anything else is silently dropped (never run).

  2. dispatch_verification: spawn a DETACHED background runner (start_new_session,
     std* = DEVNULL -- mirrors judge_client._spawn) that runs each sanctioned command
     via spec_stop_validate.run_check and writes incremental results to a per-(session,
     repo-state) sidecar. The PreToolUse hook never blocks on the suite; it polls the
     sidecar on later tool calls and auto-disarms as each subclaim grounds.

Every path is fail-open: a spawn failure, a missing/garbled sidecar, an unsanctioned
command, or any error leaves the existing breaker behavior intact -- this lane can
only REMOVE a false arm (on a passing check), never add a block or fake grounding.

Stdlib only.

# cleanup-traps: not-applicable -- detached session runner (start_new_session)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


# Per-command wall-clock cap for a background verification check. Matches the Stop
# gate's _CHECK_TIMEOUT default; a slow suite simply times out (exit 124) and the
# arm stands rather than the lane hanging.
VERIFY_CMD_TIMEOUT = _env_int("UNIFABLE_VERIFY_CMD_TIMEOUT", 600)

# Max atomic verification tasks accepted from one arm. Bounds runaway decomposition.
VERIFY_MAX_TASKS = _env_int("UNIFABLE_VERIFY_MAX_TASKS", 6)

_OUTPUT_TAIL = 2000  # captured output kept per command result

# Verification-shaped leading executables. A command must start with one of these
# (after env assignments / wrappers) to even be considered; the policy-binding check
# below still has the final say.
_VERIFY_EXECUTABLES = frozenset(
    {
        "just",
        "make",
        "pytest",
        "tox",
        "nox",
        "npm",
        "pnpm",
        "yarn",
        "cargo",
        "go",
        "uv",
        "node",
        "deno",
        "bash",
        "sh",
        "zsh",
    }
)
_PY_INTERPRETERS = frozenset({"python", "python3", "python2"})
_SCRIPT_INTERPRETERS = frozenset({"bash", "sh", "zsh"})
_WRAPPERS = frozenset({"env", "command", "nice", "time", "stdbuf", "nohup"})
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Tokens that prove a command mutates the world, publishes, or escalates. Their
# presence ALWAYS rejects -- the lane verifies, it never ships.
_DESTRUCTIVE_SUBSTRINGS = (
    "rm ",
    "rm\t",
    " --force",
    "--force-",
    "push",
    "publish",
    "deploy",
    " reset --hard",
    "git tag",
    "git commit",
    "git rebase",
    "git reset",
    "git clean",
    "gh release",
    "gh pr",
    "twine",
    "docker push",
    "wrangler",
    "mkfs",
    "dd ",
    "sudo",
    "curl",
    "wget",
    "ssh ",
    "scp ",
    "> /",
    ":(){",
    "chmod",
    "chown",
)

# Repo files whose text sanctions a verification command (the command, or its
# leading tokens / target, must appear here). Bounded read; missing files skipped.
_POLICY_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    "justfile",
    "Justfile",
    "CHANGELOG.md",
    "README.md",
    "package.json",
    "pyproject.toml",
    "Makefile",
)
_POLICY_MAX_BYTES = 1_000_000


def _policy_text(cwd: str | Path) -> str:
    """Concatenated, lowercased repo policy text used to sanction commands. Fail-open
    to '' so an unreadable repo simply sanctions nothing (no command runs)."""
    base = Path(cwd or ".")
    parts: list[str] = []
    total = 0
    for name in _POLICY_FILES:
        try:
            p = base / name
            if not p.is_file():
                continue
            if p.stat().st_size > _POLICY_MAX_BYTES:
                continue
            parts.append(p.read_text(encoding="utf-8", errors="replace"))
            total += len(parts[-1])
            if total > _POLICY_MAX_BYTES:
                break
        except OSError:
            continue
    return "\n".join(parts).lower()


def _norm(text: str) -> str:
    return " ".join(str(text or "").split()).lower()


def _strip_wrappers(tokens: list[str]) -> list[str]:
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _ENV_ASSIGN_RE.match(tok) or os.path.basename(tok.rstrip("/")) in _WRAPPERS:
            i += 1
            continue
        break
    return tokens[i:]


def _sanction_candidates(tokens: list[str]) -> list[str]:
    """Strings that, if present verbatim in policy text, sanction the command:
    the leading two/three tokens, and the head + its first non-flag target."""
    cands: list[str] = []
    if len(tokens) >= 2:
        cands.append(" ".join(tokens[:2]))
    if len(tokens) >= 3:
        cands.append(" ".join(tokens[:3]))
    head = os.path.basename(tokens[0].rstrip("/"))
    target = next((t for t in tokens[1:] if not t.startswith("-")), "")
    if target:
        cands.append(f"{head} {os.path.basename(target)}")
        cands.append(os.path.basename(target))
    return [_norm(c) for c in cands if c.strip()]


def sanction_command(cmd: str, cwd: str | Path = ".") -> bool:
    """True when *cmd* is a verification command the repo's own policy sanctions.

    Default-deny. A command passes only when ALL hold: (1) no destructive/publishing
    token; (2) a verification-shaped leading executable (a test/build runner, or an
    interpreter running a test module / a repo script); (3) the command -- or its
    leading tokens / target -- appears verbatim in repo policy text. Any parse error
    or unreadable policy returns False, so an un-sanctionable command never runs."""
    s = str(cmd or "").strip()
    if not s:
        return False
    low = s.lower()
    if any(bad in low for bad in _DESTRUCTIVE_SUBSTRINGS):
        return False
    try:
        tokens = shlex.split(s)
    except ValueError:
        return False
    tokens = _strip_wrappers(tokens)
    if not tokens:
        return False
    head = os.path.basename(tokens[0].rstrip("/"))
    if head in _PY_INTERPRETERS:
        # Only a test-runner invocation: python -m pytest|unittest|tox|nox ...
        if "-m" not in tokens:
            return False
        try:
            mod = tokens[tokens.index("-m") + 1]
        except (ValueError, IndexError):
            return False
        if mod not in ("pytest", "unittest", "tox", "nox"):
            return False
    elif head in _SCRIPT_INTERPRETERS:
        # bash/sh/zsh running a repo script (the target carries the verification).
        if not any(not t.startswith("-") for t in tokens[1:]):
            return False
    elif head not in _VERIFY_EXECUTABLES:
        return False
    policy = _policy_text(cwd)
    if not policy:
        return False
    if _norm(s) in policy:
        return True
    return any(c in policy for c in _sanction_candidates(tokens))


def sanction_tasks(raw: Any, cwd: str | Path = ".") -> list[dict[str, str]]:
    """Normalize + sanction judge-supplied verify_tasks into a bounded list of
    {subclaim, command}. Drops any task whose command is not sanctioned. Fail-open
    to [] on any error so an arm never depends on this lane."""
    try:
        if not isinstance(raw, list):
            return []
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            command = str(item.get("command") or "").strip()
            subclaim = str(item.get("subclaim") or "").strip()
            if not command or not subclaim:
                continue
            if command in seen:
                continue
            if not sanction_command(command, cwd):
                continue
            seen.add(command)
            out.append({"subclaim": subclaim, "command": command})
            if len(out) >= VERIFY_MAX_TASKS:
                break
        return out
    except Exception:
        return []


def _git_state(cwd: str | Path) -> str:
    """Repo-state fingerprint (HEAD + dirty tree) so a verify result is cached per
    repo state and re-dispatched only when the repo changes. Fail-open to ''."""
    parts: list[str] = []
    for args in (["git", "rev-parse", "HEAD"], ["git", "status", "--porcelain"]):
        try:
            proc = subprocess.run(
                args, cwd=str(cwd or "."), capture_output=True, text=True, timeout=5, check=False
            )
            parts.append(proc.stdout or "")
        except Exception:
            parts.append("")
    return "\n".join(parts)


def verify_key(claim: str, cwd: str | Path = ".") -> str:
    """Cache key for a verification run: claim + repo state. Same claim against the
    same repo state reuses the prior sidecar; a repo change forces a fresh run."""
    raw = f"{str(claim or '').strip()}|{_git_state(cwd)}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:24]


def _verify_dir() -> Path:
    from ledger import data_root

    return data_root() / "verify"


def _verify_path(input_data: dict[str, Any], key: str) -> Path:
    from ledger import ledger_key

    return _verify_dir() / f"{ledger_key(input_data)}-{key}.json"


def _load_sidecar(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def read_verification_results(input_data: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    """Per-command results from the sidecar: {command: {exit, tail, finished_at}}.
    Fail-open to {} (no results yet / unreadable) so polling never raises."""
    if not key:
        return {}
    try:
        data = _load_sidecar(_verify_path(input_data, key))
        results = data.get("results")
        return results if isinstance(results, dict) else {}
    except Exception:
        return {}


def verification_done(input_data: dict[str, Any], key: str) -> bool:
    """True when the background runner marked the sidecar complete. Fail-open False."""
    if not key:
        return False
    try:
        return str(_load_sidecar(_verify_path(input_data, key)).get("status") or "") == "done"
    except Exception:
        return False


def run_verification_tasks(commands: list[str], cwd: str | Path, out_path: str | Path) -> None:
    """Run each command (parallel, bounded) and write incremental results to the
    sidecar. The ONLY writer of the results file; pollers read-only. Never raises."""
    from atomicio import write_text_atomic

    try:
        from spec_stop_validate import run_check
    except Exception:
        try:
            from scripts.gate.spec_stop_validate import run_check  # pragma: no cover
        except Exception:
            return

    cmds = [str(c or "").strip() for c in (commands or []) if str(c or "").strip()]
    state: dict[str, Any] = {
        "status": "running",
        "dispatched_at": time.time(),
        "commands": cmds,
        "results": {},
    }

    def _flush() -> None:
        try:
            write_text_atomic(out_path, json.dumps(state))
        except Exception:
            pass

    _flush()
    if not cmds:
        state["status"] = "done"
        _flush()
        return

    workers = min(len(cmds), max(1, _env_int("UNIFABLE_VERIFY_PARALLELISM", 4)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_check, c, cwd, VERIFY_CMD_TIMEOUT): c for c in cmds}
        for fut in as_completed(futures):
            c = futures[fut]
            try:
                exit_code, output = fut.result()
            except Exception as exc:  # noqa: BLE001
                exit_code, output = 127, f"(check failed to run: {exc})"
            state["results"][c] = {
                "exit": exit_code,
                "tail": str(output or "")[-_OUTPUT_TAIL:],
                "finished_at": time.time(),
            }
            _flush()
    state["status"] = "done"
    _flush()


def dispatch_verification(
    input_data: dict[str, Any],
    claim: str,
    tasks: list[dict[str, str]],
    cwd: str | Path,
) -> str:
    """Spawn the detached background runner for *tasks* and return the verify_key.

    Idempotent per repo state: if a sidecar for this (session, claim, repo-state)
    already exists, reuse it instead of re-running the suite. Returns '' on any
    failure (no commands, spawn error) so the caller treats it as 'no auto-verify'."""
    from atomicio import write_text_atomic

    commands = [str(t.get("command") or "").strip() for t in (tasks or []) if str(t.get("command") or "").strip()]
    if not commands:
        return ""
    try:
        key = verify_key(claim, cwd)
        out_path = _verify_path(input_data, key)
        if out_path.is_file():
            return key  # already dispatched for this exact repo state
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Seed the sidecar so a poll before the runner attaches sees 'running'.
        write_text_atomic(
            out_path,
            json.dumps({"status": "running", "dispatched_at": time.time(), "commands": commands, "results": {}}),
        )
    except Exception:
        return ""
    try:
        _spawn_runner(out_path, cwd)
    except Exception:
        return ""
    return key


def _spawn_runner(out_path: str | Path, cwd: str | Path) -> None:
    """Spawn the detached background runner (mirrors judge_client._spawn). Isolated
    so the dispatch decision (key/seed) stays separate from the OS side effect."""
    try:
        devnull = open(os.devnull, "wb")
    except OSError:
        devnull = None
    subprocess.Popen(
        [sys.executable, str(_HERE / "verify_lane.py"), "--run", str(out_path), str(cwd)],
        stdin=subprocess.DEVNULL,
        stdout=devnull or subprocess.DEVNULL,
        stderr=devnull or subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        cwd=str(_HERE),
    )


def _run_main(argv: list[str]) -> int:
    """Background entrypoint: read the seeded sidecar, run its commands, finalize."""
    if len(argv) < 3 or argv[0] != "--run":
        return 1
    out_path = argv[1]
    cwd = argv[2]
    try:
        commands = _load_sidecar(Path(out_path)).get("commands") or []
        run_verification_tasks([str(c) for c in commands], cwd, out_path)
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_main(sys.argv[1:]))
