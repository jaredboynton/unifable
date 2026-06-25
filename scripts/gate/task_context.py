#!/usr/bin/env python3
"""Task-shape context lines for UserPromptSubmit (host-agnostic)."""

from __future__ import annotations

import re

_SELF_REFERENTIAL_HARNESS_TASK_RE = re.compile(
    r"\b("
    r"evidence[\s-]gate[\s-]regression|"
    r"this harness|the harness(?:'s|\s+own)?|"
    r"(?:unifable|fablize)(?:'s)?\s+(?:own\s+)?(?:gate|hooks|harness|plugin)|"
    r"scripts/gate|hooks/(?:gate_|pre_tool)|"
    r"pre[\s-]edit\s+gate|completion\s+gate|groundedness\s+breaker|"
    r"regression\s+test.*(?:acceptance|benchmark\s+result|four\s+cell)|"
    r"benchmark\s+result.*(?:acceptance|four\s+cell)|"
    r"modify(?:ing)?\s+(?:the\s+)?(?:gate|hooks|harness)|"
    r"debug(?:ging)?\s+(?:the\s+)?(?:gate|hooks|breaker)"
    r")\b",
    re.I,
)


def is_self_referential_harness_task(text: str) -> bool:
    """True when the operative prompt is about changing or debugging the harness itself."""
    return bool(_SELF_REFERENTIAL_HARNESS_TASK_RE.search(str(text or "")))


def self_referential_harness_context_line(operative_prompt: str) -> str:
    """One-shot banner steering agents away from meta-recursion on harness work."""
    if not is_self_referential_harness_task(operative_prompt):
        return ""
    return (
        "\n\nSelf-referential harness task. Implement the requested deliverable "
        "only — do not modify hooks/, scripts/gate/, or debug gate machinery unless the "
        "user explicitly asked for harness changes."
    )
