#!/usr/bin/env python3
"""Verify the benchmark harness artifacts expected by the completion gate."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESULT_DIR = ROOT / "benchmark/results/dry-run"
REQUIRED_CONDITIONS = {"claude:unifable", "claude:baseline", "codex:unifable", "codex:baseline"}


def main() -> int:
    subprocess.run(
        ["python3", "benchmark/bench.py", "--dry-run", "--driver", "tuistory"],
        cwd=ROOT,
        check=True,
    )

    summary_path = RESULT_DIR / "summary.json"
    markdown_path = RESULT_DIR / "summary.md"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    assert summary_path.is_file()
    assert markdown_path.is_file()
    assert str(summary.get("raw_dir", "")).endswith("benchmark/results/dry-run/raw")
    assert summary.get("sessions")

    for session in summary["sessions"]:
        assert isinstance(session.get("elapsed_seconds"), (int, float))
        assert isinstance(session.get("total_tokens"), int)
        result_dir = Path(session["result_dir"])
        assert "benchmark/results" in result_dir.as_posix()
        assert (result_dir / "timing.json").is_file()
        assert (result_dir / "usage.json").is_file()

    conditions = {row.get("condition") for row in summary.get("aggregates", [])}
    assert REQUIRED_CONDITIONS <= conditions

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.search(r"(?im)^##\s+.*benchmark", readme)

    print(f"verified benchmark dry-run summary at {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
