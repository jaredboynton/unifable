#!/usr/bin/env python3
"""Run Claude Code and Codex CLI benchmark sessions through a PTY driver."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TASK = REPO_ROOT / "benchmark/tasks/evidence_gate_regression.md"
WORKSPACE_ROOT = Path(os.environ.get("UNIFABLE_BENCH_WORKSPACE_ROOT", Path(tempfile.gettempdir()) / "unifable-benchmark-workspaces"))
TOKEN_RE = re.compile(r"(?P<key>input|output|cached|total)[ _-]?tokens[^0-9]*(?P<value>[0-9][0-9,]*)", re.I)


@dataclass(frozen=True)
class Condition:
    host: str
    model: str
    effort: str
    unifable: bool

    @property
    def slug(self) -> str:
        suffix = "unifable" if self.unifable else "baseline"
        return f"{self.host}-{suffix}"


CONDITIONS = (
    Condition("claude", "opus-4.8", "xhigh", True),
    Condition("claude", "opus-4.8", "xhigh", False),
    Condition("codex", "gpt-5.5", "xhigh", True),
    Condition("codex", "gpt-5.5", "xhigh", False),
)


def _run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout, text=True, capture_output=True, check=False)


def _find_driver() -> tuple[str, str]:
    candidate = shutil.which("tctl")
    if candidate:
        return "tctl", candidate
    plugin_root = os.environ.get("DROID_PLUGIN_ROOT")
    if plugin_root:
        path = Path(plugin_root) / "bin/tctl"
        if path.exists():
            return "tctl", str(path)
    candidate = shutil.which("tuistory")
    if candidate:
        return "tuistory", candidate
    raise SystemExit("neither tctl nor tuistory found; install tuistory or set DROID_PLUGIN_ROOT")


def _copy_auth(src_home: Path, dst_home: Path, filename: str) -> None:
    src = src_home / filename
    if src.exists():
        dst = dst_home / filename
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _prepare_codex_home(run_dir: Path, condition: Condition) -> Path:
    run_id = run_dir.parent.parent.name
    cell_id = run_dir.name
    codex_home = WORKSPACE_ROOT / run_id / cell_id / "codex-home"
    if codex_home.exists():
        shutil.rmtree(codex_home)
    codex_home.mkdir(parents=True)
    (run_dir / "codex-home-path.txt").write_text(str(codex_home) + "\n", encoding="utf-8")
    _copy_auth(Path.home() / ".codex", codex_home, "auth.json")
    if condition.unifable:
        plugin_source = WORKSPACE_ROOT / run_id / cell_id / "plugin-source"
        if plugin_source.exists():
            shutil.rmtree(plugin_source)
        shutil.copytree(REPO_ROOT, plugin_source, ignore=shutil.ignore_patterns(".git", ".mypy_cache", ".pytest_cache", "__pycache__", "results"))
        env = {**os.environ, "CODEX_HOME": str(codex_home)}
        added = _run(["codex", "plugin", "marketplace", "add", str(plugin_source)], cwd=REPO_ROOT, env=env, timeout=120)
        installed = _run(["codex", "plugin", "add", "unifable@unifable"], cwd=REPO_ROOT, env=env, timeout=120)
        setup_log = run_dir / "codex-plugin-setup.txt"
        setup_log.write_text(added.stdout + added.stderr + installed.stdout + installed.stderr, encoding="utf-8")
        if added.returncode != 0 or installed.returncode != 0:
            raise SystemExit(f"failed to prepare Codex plugin home; see {setup_log}")
        config = codex_home / "config.toml"
        text = config.read_text(encoding="utf-8") if config.exists() else ""
        if '[plugins."unifable@unifable"]' not in text:
            with config.open("a", encoding="utf-8") as fh:
                fh.write('\n[plugins."unifable@unifable"]\nenabled = true\n')
    return codex_home


def _env_for(condition: Condition, worktree: Path, run_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["UNIFABLE_BENCHMARK"] = "1"
    env["UNIFABLE_BENCHMARK_WORKTREE"] = str(worktree)
    if condition.host == "codex":
        env["CODEX_HOME"] = str(_prepare_codex_home(run_dir, condition))
    return env


def _prepare_worktree(run_dir: Path, condition: Condition) -> Path:
    run_id = run_dir.parent.parent.name
    worktree = WORKSPACE_ROOT / run_id / run_dir.name / "worktree"
    if worktree.exists():
        shutil.rmtree(worktree)
    ignore = shutil.ignore_patterns(".git", ".mypy_cache", ".pytest_cache", "__pycache__", "results")
    shutil.copytree(REPO_ROOT, worktree, ignore=ignore)
    (run_dir / "worktree-path.txt").write_text(str(worktree) + "\n", encoding="utf-8")
    marker = worktree / ".unifable-benchmark-condition.json"
    marker.write_text(json.dumps(condition.__dict__, indent=2) + "\n", encoding="utf-8")
    return worktree


def _command_for(condition: Condition, task_path: Path, worktree: Path) -> str:
    prompt_arg = f"\"$(cat {shlex.quote(str(task_path))})\""
    if condition.host == "claude":
        flags = [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            "opus",
            "--effort",
            condition.effort,
            "--permission-mode",
            "bypassPermissions",
            "--dangerously-skip-permissions",
        ]
        if condition.unifable:
            flags.extend(["--setting-sources", "project"])
            flags.extend(["--plugin-dir", str(REPO_ROOT)])
        else:
            flags.append("--safe-mode")
        return " ".join(shlex.quote(part) for part in flags) + f" {prompt_arg}"
    if condition.host == "codex":
        flags = [
            "codex",
            "exec",
            "--json",
            "--model",
            condition.model,
            "--config",
            f'model_reasoning_effort="{condition.effort}"',
            "--dangerously-bypass-approvals-and-sandbox",
            "--cd",
            str(worktree),
        ]
        if condition.unifable:
            flags.append("--dangerously-bypass-hook-trust")
        else:
            flags.append("--ignore-user-config")
        return " ".join(shlex.quote(part) for part in flags) + f" {prompt_arg}"
    raise ValueError(condition.host)


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _normalize_usage(usage: dict[str, object]) -> dict[str, int]:
    input_tokens = _int(usage.get("input_tokens"))
    output_tokens = _int(usage.get("output_tokens"))
    cache_creation = _int(usage.get("cache_creation_input_tokens"))
    cache_read = _int(usage.get("cache_read_input_tokens"))
    cached = _int(usage.get("cached_tokens")) or _int(usage.get("cached_input_tokens")) or cache_read
    reasoning = _int(usage.get("reasoning_output_tokens"))
    total = _int(usage.get("total_tokens"))
    if total == 0:
        total = input_tokens + output_tokens + cache_creation + cache_read + reasoning
    out = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total,
    }
    if cached:
        out["cached_tokens"] = cached
    if cache_creation:
        out["cache_creation_input_tokens"] = cache_creation
    if cache_read:
        out["cache_read_input_tokens"] = cache_read
    if reasoning:
        out["reasoning_output_tokens"] = reasoning
    return out


def _json_events(text: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for line in text.splitlines():
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def _files_changed_from_text(text: str, host: str) -> int:
    """Count distinct files the agent actually edited.

    A proxy for productive work: a cell that runs dozens of commands but changes
    one file (or none) was churning, not building.
    """
    paths: set[str] = set()
    for event in _json_events(text):
        etype = event.get("type")
        if host == "claude" and etype == "assistant":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            for block in content or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                if block.get("name") not in EDIT_TOOLS:
                    continue
                tool_input = block.get("input")
                if isinstance(tool_input, dict):
                    path = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")
                    if path:
                        paths.add(str(path))
        elif host == "codex" and etype == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            if (item.get("item_type") or item.get("type")) != "file_change":
                continue
            if item.get("path"):
                paths.add(str(item["path"]))
            for change in item.get("changes") or []:
                if isinstance(change, dict) and change.get("path"):
                    paths.add(str(change["path"]))
            for fname in item.get("files") or []:
                paths.add(str(fname))
    return len(paths)


def _usage_from_text(text: str) -> dict[str, int]:
    usage_records: list[dict[str, int]] = []
    for parsed in _json_events(text):
        if not isinstance(parsed.get("usage"), dict):
            continue
        if parsed.get("type") in {"result", "turn.completed", "response.done", "response.completed"}:
            usage_records.append(_normalize_usage(parsed["usage"]))
    if usage_records:
        usage: dict[str, int] = {}
        for record in usage_records:
            for key, value in record.items():
                usage[key] = usage.get(key, 0) + value
        return usage
    usage = {}
    for match in TOKEN_RE.finditer(text):
        key = f"{match.group('key').lower()}_tokens"
        usage[key] = usage.get(key, 0) + int(match.group("value").replace(",", ""))
    return usage


def _status_from_events(condition: Condition, stdout: str, wait_found: bool) -> str:
    events = _json_events(stdout)
    if condition.host == "claude":
        results = [event for event in events if event.get("type") == "result"]
        if results:
            return "failed" if results[-1].get("is_error") else "completed"
    if condition.host == "codex":
        event_types = {event.get("type") for event in events}
        if "turn.completed" in event_types:
            return "completed"
        if event_types & {"turn.failed", "turn.cancelled", "turn.aborted", "turn.interrupted"}:
            return "failed"
    return "timeout" if not wait_found else "completed"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _wait_for_pattern(driver: str, session: str, pattern: str, *, cwd: Path, env: dict[str, str], timeout: int) -> tuple[str, str, bool]:
    deadline = time.monotonic() + timeout
    out = ""
    err = ""
    while time.monotonic() < deadline:
        remaining = max(1, int(deadline - time.monotonic()))
        window = min(60, remaining)
        waited = _run([driver, "-s", session, "wait", pattern, "--timeout", str(window * 1000)], cwd=cwd, env=env, timeout=window + 15)
        if waited.returncode == 0:
            return out + waited.stdout, err + waited.stderr, True
        out += waited.stdout
        err += waited.stderr
        if "not found" in waited.stderr.lower():
            break
    return out, err, False


def _write_session_files(run_dir: Path, condition: Condition, status: str, elapsed: float, stdout: str, stderr: str) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    usage = _usage_from_text(stdout + "\n" + stderr)
    files_changed = _files_changed_from_text(stdout, condition.host)
    if status == "completed" and not usage:
        status = "failed-no-usage"
    (run_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
    (run_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "host": condition.host,
                "model": condition.model,
                "effort": condition.effort,
                "unifable": condition.unifable,
                "status": status,
                "files_changed": files_changed,
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "timing.json").write_text(json.dumps({"elapsed_seconds": round(elapsed, 3)}, indent=2) + "\n", encoding="utf-8")
    (run_dir / "usage.json").write_text(json.dumps(usage, indent=2) + "\n", encoding="utf-8")


def run_with_tuistory(condition: Condition, raw_dir: Path, task_path: Path, timeout: int, cell_id: str | None = None) -> Path:
    driver_name, driver = _find_driver()
    cell_id = cell_id or condition.slug
    run_dir = raw_dir / cell_id
    run_dir.mkdir(parents=True, exist_ok=True)
    worktree = _prepare_worktree(run_dir, condition)
    session = f"unifable-bench-{cell_id}-{int(time.time())}"
    record = run_dir / "session.cast"
    launch = [driver, "launch", "pending", "-s", session, "--cols", "140", "--rows", "42"]
    if driver_name == "tctl":
        launch.extend(
            [
                "--backend",
                "tuistory",
                "--cwd",
                str(worktree),
                "--record",
                str(record),
                "--env",
                "FORCE_COLOR=3",
                "--env",
                "COLORTERM=truecolor",
            ]
        )
    else:
        launch.extend(["--cwd", str(worktree), "--env", "FORCE_COLOR=3", "--env", "COLORTERM=truecolor"])
    env = _env_for(condition, worktree, run_dir)
    start = time.monotonic()
    out = ""
    err = ""
    status = "unknown"
    cli_stdout = run_dir / "cli.stdout.jsonl"
    cli_stderr = run_dir / "cli.stderr.txt"
    try:
        command = _command_for(condition, task_path, worktree)
        inner = (
            "printf 'UNIFABLE_BENCH_START\\n'; "
            f"{command} > {shlex.quote(str(cli_stdout))} 2> {shlex.quote(str(cli_stderr))}; "
            "rc=$?; "
            f"cat {shlex.quote(str(cli_stdout))}; "
            f"cat {shlex.quote(str(cli_stderr))} >&2; "
            "printf '\\nUNIFABLE_BENCH_EXIT:%s\\n' \"$rc\"; "
            "exit \"$rc\""
        )
        launch[2] = "bash -lc " + shlex.quote(inner)
        launched = _run(launch, cwd=worktree, env=env, timeout=30)
        out += launched.stdout
        err += launched.stderr
        if launched.returncode != 0:
            status = f"launch-failed-{launched.returncode}"
            return run_dir
        done_pattern = r'/"type":"result"|UNIFABLE_BENCH_EXIT:/' if condition.host == "claude" else r'/"type":"turn.completed"|UNIFABLE_BENCH_EXIT:/'
        wait_out, wait_err, wait_found = _wait_for_pattern(driver, session, done_pattern, cwd=worktree, env=env, timeout=timeout)
        out += wait_out
        err += wait_err
        if driver_name == "tuistory":
            snap = _run([driver, "-s", session, "read", "--all", "--trim"], cwd=worktree, env=env, timeout=30)
        else:
            snap = _run([driver, "-s", session, "snapshot", "--trim"], cwd=worktree, env=env, timeout=30)
        out += snap.stdout
        err += snap.stderr
        status = _status_from_events(condition, _read_text(cli_stdout), wait_found)
    finally:
        closed = _run([driver, "-s", session, "close"], cwd=REPO_ROOT, env=env, timeout=30)
        out += closed.stdout
        err += closed.stderr
        elapsed = time.monotonic() - start
        (run_dir / "terminal.txt").write_text(out + "\n" + err, encoding="utf-8")
        stdout = _read_text(cli_stdout) or out
        stderr = _read_text(cli_stderr) or err
        _write_session_files(run_dir, condition, status, elapsed, stdout, stderr)
    return run_dir


def dry_run(raw_dir: Path) -> None:
    for idx, condition in enumerate(CONDITIONS, start=1):
        run_dir = raw_dir / condition.slug
        _write_session_files(
            run_dir,
            condition,
            "dry-run",
            float(idx),
            f"{condition.host} {condition.model} total tokens: {idx * 100}\n",
            "",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    parser.add_argument("--task", type=Path, default=DEFAULT_TASK)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--repeats", type=int, default=1, help="runs per condition; means are aggregated over repeats")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--driver", choices=("tuistory",), default="tuistory")
    args = parser.parse_args()

    result_dir = REPO_ROOT / "benchmark/results" / ("dry-run" if args.dry_run else args.run_id)
    raw_dir = result_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    repeats = max(1, args.repeats)
    if args.dry_run:
        dry_run(raw_dir)
    else:
        for repeat in range(1, repeats + 1):
            for condition in CONDITIONS:
                cell_id = condition.slug if repeats == 1 else f"{condition.slug}-r{repeat}"
                run_with_tuistory(condition, raw_dir, args.task, args.timeout, cell_id=cell_id)

    summary_json = result_dir / "summary.json"
    summary_md = result_dir / "summary.md"
    completed = _run(
        [
            "python3",
            str(REPO_ROOT / "benchmark/summarize.py"),
            str(raw_dir),
            "--out",
            str(summary_json),
            "--markdown",
            str(summary_md),
        ],
        cwd=REPO_ROOT,
        timeout=120,
    )
    print(completed.stdout, end="")
    print(completed.stderr, end="")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
