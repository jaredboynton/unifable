"""Resolve session transcript JSONL paths from hook payloads."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _encode_cwd(cwd: str) -> str:
    return cwd.replace("/", "-").replace("_", "-")


def locate_transcript(input_data: dict[str, Any]) -> str | None:
    """Return transcript_path from payload or Claude projects layout."""
    tp = input_data.get("transcript_path")
    if tp and Path(str(tp)).is_file():
        return str(tp)
    sid = input_data.get("session_id")
    cwd = input_data.get("cwd") or os.getcwd()
    if sid:
        cand = Path.home() / ".claude" / "projects" / _encode_cwd(str(cwd)) / f"{sid}.jsonl"
        if cand.is_file():
            return str(cand)
    return None
