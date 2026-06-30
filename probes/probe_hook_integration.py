#!/usr/bin/env python3
"""Live Claude/Codex hook integration probe driven through tuistory.

Creates a small-but-real Python fixture, wires project-local hooks to the current
checkout's hook scripts, asks Claude Code or Codex to fix the fixture, and records
which tool events the host actually delivered to hooks. This is intentionally a
probe (not pytest): it spends model calls and writes artifacts under
probes/bench/results/.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
RESULT_ROOT = REPO_ROOT / "probes" / "bench" / "results"
PY = sys.executable

GATED_EXACT = {
    "Bash",
    "REPL",
    "exec_command",
    "Task",
    "Agent",
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "apply_patch",
}
WEB_LIKE_TOOL_NAMES = {"WebSearch", "web_search", "webrun"}


@dataclass(frozen=True)
class Host:
    name: str
    model: str
    effort: str


HOSTS = {
    "claude": Host("claude", "haiku", "medium"),
    "codex": Host("codex", "gpt-5.5", "medium"),
}


def _run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout, text=True, capture_output=True, check=False)


def _find_tui_driver() -> tuple[str, str]:
    tctl = shutil.which("tctl")
    if tctl:
        return "tctl", tctl
    plugin_root = os.environ.get("DROID_PLUGIN_ROOT")
    if plugin_root:
        candidate = Path(plugin_root) / "bin" / "tctl"
        if candidate.exists():
            return "tctl", str(candidate)
    tuistory = shutil.which("tuistory")
    if tuistory:
        return "tuistory", tuistory
    raise SystemExit("tuistory/tctl not found; install tuistory or set DROID_PLUGIN_ROOT")


def _write(path: Path, text: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    if mode is not None:
        path.chmod(mode)


def _write_fixture(root: Path) -> None:
    _write(
        root / "pyproject.toml",
        """
        [project]
        name = "calcflow-fixture"
        version = "0.1.0"
        requires-python = ">=3.10"

        [tool.pytest.ini_options]
        testpaths = ["tests"]
        pythonpath = ["src"]
        """,
    )
    _write(
        root / "README.md",
        """
        # calcflow fixture

        `calcflow.metrics.weighted_percentile` should accept unsorted values with
        repeated samples, reject negative weights, and return deterministic nearest-rank
        weighted percentiles. The CLI should read a JSON array from stdin and print
        `{"p50": ..., "p90": ...}`.
        """,
    )
    _write(root / "src" / "calcflow" / "__init__.py", '"""Small stats fixture."""\n')
    _write(
        root / "src" / "calcflow" / "metrics.py",
        """
        from __future__ import annotations

        from collections.abc import Iterable


        def weighted_percentile(values: Iterable[float], weights: Iterable[float], percentile: float) -> float:
            pairs = list(zip(values, weights))
            if not pairs:
                raise ValueError("values must not be empty")
            if percentile < 0 or percentile > 100:
                raise ValueError("percentile must be in [0, 100]")
            total = sum(weight for _, weight in pairs)
            if total <= 0:
                raise ValueError("total weight must be positive")
            threshold = total * percentile / 100
            running = 0.0
            # BUG: order-sensitive and accepts negative weights.
            for value, weight in pairs:
                running += weight
                if running >= threshold:
                    return float(value)
            return float(pairs[-1][0])
        """,
    )
    _write(
        root / "src" / "calcflow" / "cli.py",
        """
        from __future__ import annotations

        import json
        import sys

        from .metrics import weighted_percentile


        def main() -> int:
            payload = json.load(sys.stdin)
            values = [row["value"] for row in payload]
            weights = [row.get("weight", 1) for row in payload]
            result = {
                "p50": weighted_percentile(values, weights, 50),
                # BUG: copy/paste error; should be p90.
                "p90": weighted_percentile(values, weights, 50),
            }
            print(json.dumps(result, sort_keys=True))
            return 0


        if __name__ == "__main__":
            raise SystemExit(main())
        """,
    )
    _write(
        root / "tests" / "test_metrics.py",
        """
        from __future__ import annotations

        import json
        import os
        import subprocess
        import sys

        import pytest

        from calcflow.metrics import weighted_percentile


        def test_weighted_percentile_is_order_independent_with_repeats():
            values = [10, 1, 10, 5]
            weights = [1, 3, 2, 4]
            assert weighted_percentile(values, weights, 50) == 5
            assert weighted_percentile(reversed(values), reversed(weights), 50) == 5
            assert weighted_percentile(values, weights, 90) == 10


        def test_weighted_percentile_rejects_negative_weights():
            with pytest.raises(ValueError, match="negative"):
                weighted_percentile([1, 2, 3], [1, -2, 3], 50)


        def test_cli_outputs_p50_and_p90():
            rows = [
                {"value": 10, "weight": 1},
                {"value": 1, "weight": 3},
                {"value": 10, "weight": 2},
                {"value": 5, "weight": 4},
            ]
            proc = subprocess.run(
                [sys.executable, "-m", "calcflow.cli"],
                input=json.dumps(rows),
                text=True,
                capture_output=True,
                env={**os.environ, "PYTHONPATH": "src"},
                check=True,
            )
            assert json.loads(proc.stdout) == {"p50": 5.0, "p90": 10.0}
        """,
    )


def _logger_script() -> str:
    return """#!/usr/bin/env python3
import json
import os
import sys
import time

event = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    payload = json.load(sys.stdin)
except Exception as exc:
    payload = {"_parse_error": str(exc)}

record = {
    "ts": time.time(),
    "event": event or payload.get("hook_event_name", ""),
    "tool_name": payload.get("tool_name", ""),
    "cwd": payload.get("cwd", ""),
    "permission_mode": payload.get("permission_mode", ""),
}
path = os.environ.get("HOOK_PROBE_LOG") or os.path.join(os.getcwd(), ".hook-probe", "events.jsonl")
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(record, sort_keys=True) + "\\n")
print("{}")
"""


def _hook_command(script: str) -> str:
    return shlex.quote(PY) + " " + shlex.quote(str(REPO_ROOT / "hooks" / script))


def _logger_command(event: str) -> str:
    return shlex.quote(PY) + " .hook-probe/log_hook.py " + shlex.quote(event)


def _hooks_config() -> dict[str, Any]:
    return {
        "hooks": {
            "SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": _hook_command("session_start.py"), "timeout": 30}]}],
            "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": _hook_command("gate_prompt.py"), "timeout": 95}]}],
            "PreToolUse": [
                {"matcher": ".*", "hooks": [{"type": "command", "command": _logger_command("PreToolUse"), "timeout": 10}]},
                {
                    "matcher": "^(Bash|REPL|exec_command|Task|Agent|Edit|Write|MultiEdit|NotebookEdit|apply_patch|mcp__.*)$",
                    "hooks": [{"type": "command", "command": _hook_command("pre_tool_use.py"), "timeout": 10}],
                },
            ],
            "PostToolUse": [
                {"matcher": ".*", "hooks": [{"type": "command", "command": _logger_command("PostToolUse"), "timeout": 10}]},
                {"matcher": ".*", "hooks": [{"type": "command", "command": _hook_command("gate_post_tool.py"), "timeout": 120}]},
            ],
            "Stop": [{"hooks": [{"type": "command", "command": _hook_command("gate_stop.py"), "timeout": 120}]}],
        }
    }


def _install_project_hooks(root: Path) -> Path:
    log = root / ".hook-probe" / "events.jsonl"
    _write(root / ".hook-probe" / "log_hook.py", _logger_script(), mode=0o755)
    config = _hooks_config()
    _write(root / ".codex" / "hooks.json", json.dumps(config, indent=2) + "\n")
    _write(root / ".claude" / "settings.json", json.dumps(config, indent=2) + "\n")
    return log


def _task_prompt() -> str:
    return textwrap.dedent(
        """
        Fix this repository end to end. It is intentionally small but non-trivial.
        This is a focused normal bug-fix task, not architectural exploration.

        Requirements:
        - Inspect the implementation and tests before editing.
        - Optional hook-observability probe: if native web search or a read-like MCP search/fetch tool is available, make one harmless query for weighted percentile guidance. This is not a completion requirement; do not add it to the unifable task board, and do not claim that a source was opened unless you actually fetched/opened one. Do not simulate web search with curl.
        - Fix calcflow.metrics.weighted_percentile so it is order-independent, rejects negative weights, validates input lengths, and handles repeated samples deterministically.
        - Fix calcflow.cli so p90 is actually the 90th percentile.
        - Add or preserve tests proving the behavior.
        - Run python -m pytest -q and make it pass.
        - Do not edit .unifable or .hook-probe files.
        - If you use the unifable task board, keep checks simple and pytest-based; do not put long shell pipelines inside task checks.
        """
    ).strip()


def _command_for(host: Host, fixture: Path, prompt_path: Path) -> str:
    prompt_arg = f"\"$(cat {shlex.quote(str(prompt_path))})\""
    if host.name == "claude":
        parts = [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            host.model,
            "--effort",
            host.effort,
            "--permission-mode",
            "bypassPermissions",
            "--dangerously-skip-permissions",
            "--setting-sources",
            "project",
        ]
        return " ".join(shlex.quote(part) for part in parts) + " " + prompt_arg
    if host.name == "codex":
        parts = [
            "codex",
            "exec",
            "--json",
            "--model",
            host.model,
            "--config",
            f'model_reasoning_effort="{host.effort}"',
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
            "--cd",
            str(fixture),
        ]
        return " ".join(shlex.quote(part) for part in parts) + " " + prompt_arg
    raise ValueError(host.name)


def _launch_args(driver_name: str, driver: str, session: str, command: str, cwd: Path) -> list[str]:
    if driver_name == "tctl":
        return [
            driver,
            "launch",
            command,
            "-s",
            session,
            "--backend",
            "tuistory",
            "--repo-root",
            str(cwd),
            "--cwd",
            str(cwd),
            "--cols",
            "140",
            "--rows",
            "42",
            "--env",
            "FORCE_COLOR=3",
            "--env",
            "COLORTERM=truecolor",
            "--env",
            "TERM=xterm-256color",
        ]
    return [
        driver,
        "launch",
        command,
        "-s",
        session,
        "--cwd",
        str(cwd),
        "--cols",
        "140",
        "--rows",
        "42",
        "--env",
        "FORCE_COLOR=3",
        "--env",
        "COLORTERM=truecolor",
        "--env",
        "TERM=xterm-256color",
    ]


def _wait_for_exit(driver: str, session: str, cwd: Path, env: dict[str, str], timeout: int) -> tuple[str, str, bool]:
    pattern = "/HOOK_PROBE_EXIT:[0-9]+/"
    proc = _run([driver, "-s", session, "wait", pattern, "--timeout", str(timeout * 1000)], cwd=cwd, env=env, timeout=timeout + 30)
    return proc.stdout, proc.stderr, proc.returncode == 0


def _read_all(driver: str, session: str, cwd: Path, env: dict[str, str]) -> tuple[str, str]:
    if Path(driver).name == "tctl":
        proc = _run([driver, "-s", session, "snapshot", "--trim"], cwd=cwd, env=env, timeout=30)
    else:
        proc = _run([driver, "-s", session, "read", "--all", "--trim"], cwd=cwd, env=env, timeout=30)
    return proc.stdout, proc.stderr


def _events(log: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not log.exists():
        return out
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def _run_fixture_tests(fixture: Path) -> dict[str, Any]:
    proc = _run([PY, "-m", "pytest", "-q"], cwd=fixture, timeout=120)
    return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def _git_diff(fixture: Path) -> str:
    proc = _run(["git", "diff", "--", "src", "tests"], cwd=fixture, timeout=30)
    return proc.stdout + proc.stderr


def _summarize(host: Host, run_dir: Path, fixture: Path, log: Path, terminal: str, exit_found: bool) -> dict[str, Any]:
    events = _events(log)
    pretools = [str(e.get("tool_name") or "") for e in events if e.get("event") == "PreToolUse"]
    posttools = [str(e.get("tool_name") or "") for e in events if e.get("event") == "PostToolUse"]
    non_shell_non_mcp_pretools = [
        tool
        for tool in pretools
        if tool
        and tool not in GATED_EXACT
        and not tool.startswith("mcp__")
    ]
    websearch_pretools = [tool for tool in pretools if tool in WEB_LIKE_TOOL_NAMES]
    test_result = _run_fixture_tests(fixture)
    combined_output = terminal
    for name in ("stdout.txt", "stderr.txt"):
        path = run_dir / name
        if path.exists():
            combined_output += "\n" + path.read_text(encoding="utf-8", errors="replace")
    summary = {
        "host": host.name,
        "model": host.model,
        "effort": host.effort,
        "run_dir": str(run_dir),
        "fixture": str(fixture),
        "exit_found": exit_found,
        "pretool_tools": sorted(set(pretools)),
        "posttool_tools": sorted(set(posttools)),
        "web_like_pretool_tools": sorted(set(websearch_pretools)),
        "web_like_pretool_count": len(websearch_pretools),
        "non_shell_non_mcp_pretool_tools": sorted(set(non_shell_non_mcp_pretools)),
        "mcp_pretool_count": sum(1 for tool in pretools if tool.startswith("mcp__")),
        "pytest": test_result,
        "hard_block_mentions": len(
            re.findall(
                r"Evidence spec required|Protected unifable state|permissionDecision|(?:Command|Tool call) blocked by PreToolUse hook",
                combined_output,
            )
        ),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (run_dir / "diff.patch").write_text(_git_diff(fixture), encoding="utf-8")
    (run_dir / "summary.md").write_text(
        "\n".join(
            [
                f"# Hook Integration Probe: {host.name}",
                "",
                f"- model: `{host.model}`",
                f"- exit sentinel found: `{exit_found}`",
                f"- PreToolUse tools: `{', '.join(summary['pretool_tools'])}`",
                f"- PostToolUse tools: `{', '.join(summary['posttool_tools'])}`",
                f"- Web-like PreToolUse tools: `{', '.join(summary['web_like_pretool_tools'])}`",
                f"- Non-shell non-MCP PreToolUse tools: `{', '.join(summary['non_shell_non_mcp_pretool_tools'])}`",
                f"- MCP PreToolUse count: `{summary['mcp_pretool_count']}`",
                f"- pytest rc: `{test_result['returncode']}`",
                f"- hard block mentions: `{summary['hard_block_mentions']}`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return summary


def run_host(host: Host, result_root: Path, timeout: int, dry_run: bool) -> dict[str, Any]:
    run_dir = result_root / host.name
    fixture = run_dir / "fixture"
    if fixture.exists():
        shutil.rmtree(fixture)
    fixture.mkdir(parents=True)
    _write_fixture(fixture)
    _run(["git", "init"], cwd=fixture, timeout=30)
    _run(["git", "add", "."], cwd=fixture, timeout=30)
    _run(["git", "commit", "-m", "fixture"], cwd=fixture, timeout=30)
    log = _install_project_hooks(fixture)
    prompt_path = run_dir / "task.md"
    _write(prompt_path, _task_prompt())
    command = _command_for(host, fixture, prompt_path)
    (run_dir / "command.txt").write_text(command + "\n", encoding="utf-8")
    if dry_run:
        summary = {
            "host": host.name,
            "model": host.model,
            "effort": host.effort,
            "run_dir": str(run_dir),
            "fixture": str(fixture),
            "dry_run": True,
        }
        (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        return summary

    driver_name, driver = _find_tui_driver()
    env = os.environ.copy()
    env["HOOK_PROBE_LOG"] = str(log)
    env["UNIFABLE_DATA"] = str(run_dir / "unifable-data")
    env["UNIFABLE_HOST"] = host.name
    env["UNIFABLE_GRADE"] = "STANDARD"
    env["UNIFABLE_VERIFY_CITATIONS"] = "0"
    session = f"unifable-hook-probe-{host.name}-{int(time.time())}"
    stdout_path = run_dir / "agent.stdout"
    stderr_path = run_dir / "agent.stderr"
    inner = (
        "printf 'HOOK_PROBE_START\\n'; "
        f"{command} > {shlex.quote(str(stdout_path))} 2> {shlex.quote(str(stderr_path))}; "
        "rc=$?; "
        f"cat {shlex.quote(str(stdout_path))}; "
        f"cat {shlex.quote(str(stderr_path))} >&2; "
        "printf '\\nHOOK_PROBE_EXIT:%s\\n' \"$rc\"; "
        "exit \"$rc\""
    )
    launch = _launch_args(driver_name, driver, session, "bash -lc " + shlex.quote(inner), fixture)
    started = _run(launch, cwd=fixture, env=env, timeout=30)
    terminal = started.stdout + started.stderr
    exit_found = False
    try:
        waited_out, waited_err, exit_found = _wait_for_exit(driver, session, fixture, env, timeout)
        terminal += waited_out + waited_err
        read_out, read_err = _read_all(driver, session, fixture, env)
        terminal += read_out + read_err
    finally:
        closed = _run([driver, "-s", session, "close"], cwd=fixture, env=env, timeout=30)
        terminal += closed.stdout + closed.stderr
    (run_dir / "terminal.txt").write_text(terminal, encoding="utf-8")
    if stdout_path.exists():
        (run_dir / "stdout.txt").write_text(stdout_path.read_text(encoding="utf-8"), encoding="utf-8")
    if stderr_path.exists():
        (run_dir / "stderr.txt").write_text(stderr_path.read_text(encoding="utf-8"), encoding="utf-8")
    return _summarize(host, run_dir, fixture, log, terminal, exit_found)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=("claude", "codex", "both"), default="both")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    result_root = args.out or RESULT_ROOT / f"hook-integration-{stamp}"
    result_root.mkdir(parents=True, exist_ok=True)

    selected = [HOSTS["claude"], HOSTS["codex"]] if args.host == "both" else [HOSTS[args.host]]
    summaries = [run_host(host, result_root, args.timeout, args.dry_run) for host in selected]
    aggregate = {"result_root": str(result_root), "summaries": summaries}
    (result_root / "summary.json").write_text(json.dumps(aggregate, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(aggregate, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
