#!/usr/bin/env python3
"""Summarize raw benchmark sessions into comparable timing/token results."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
from typing import Any


# A benchmark run is only comparable when every host is measured both with and
# without unifable. These are the four cells a summary must contain to be accepted.
REQUIRED_CONDITIONS = frozenset(
    {"claude:unifable", "claude:baseline", "codex:unifable", "codex:baseline"}
)

# Estimated first-party API list prices (USD per million tokens). These exist only
# to turn raw token counts into a cache-weighted cost: cache reads bill at 0.1x the
# base input price, so a raw `total_tokens` that is mostly cache reads wildly
# overstates real cost. `est_cost_usd` is a comparable cost proxy, NOT a bill -- the
# benchmark CLIs run under subscriptions/quota, not metered API billing.
# Prices fetched 2026-06-24:
#   Anthropic Opus 4.8: https://platform.claude.com/docs/en/about-claude/pricing
#     ($5 input / $0.50 cache read / $6.25 5m cache write / $25 output per MTok)
#   OpenAI GPT-5.5:     https://developers.openai.com/api/docs/pricing
#     ($5 input / $0.50 cached input / $30 output per MTok; cached = 0.1x input)
PRICING_AS_OF = "2026-06-24"
PRICING: dict[str, dict[str, float]] = {
    "opus-4.8": {"input": 5.0, "cache_read": 0.50, "cache_write": 6.25, "output": 25.0},
    "gpt-5.5": {"input": 5.0, "cache_read": 0.50, "cache_write": 5.0, "output": 30.0},
}


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _token_components(usage: dict[str, Any], host: str) -> dict[str, int]:
    """Split usage into vendor-normalized cost components.

    Anthropic reports ``input_tokens`` net of cache; OpenAI/Codex reports it
    inclusive of cached tokens, so the fresh (freshly-processed) prompt is
    recovered differently per host.
    """
    input_tokens = _as_int(usage.get("input_tokens"))
    cached_input = _as_int(usage.get("cache_read_input_tokens")) or _as_int(usage.get("cached_tokens"))
    cache_write = _as_int(usage.get("cache_creation_input_tokens"))
    output_tokens = _as_int(usage.get("output_tokens"))
    reasoning = _as_int(usage.get("reasoning_output_tokens"))
    fresh_input = max(0, input_tokens - cached_input) if host == "codex" else input_tokens
    return {
        "fresh_input_tokens": fresh_input,
        "cached_input_tokens": cached_input,
        "cache_write_tokens": cache_write,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning,
    }


def _est_cost_usd(components: dict[str, int], model: str) -> float | None:
    price = PRICING.get(model)
    if price is None:
        return None
    cost = (
        components["fresh_input_tokens"] * price["input"]
        + components["cached_input_tokens"] * price["cache_read"]
        + components["cache_write_tokens"] * price["cache_write"]
        + (components["output_tokens"] + components["reasoning_tokens"]) * price["output"]
    ) / 1_000_000
    return round(cost, 4)


def _avg(values: list[float], digits: int) -> float | None:
    nums = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return round(mean(nums), digits) if nums else None


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path}: invalid JSON: {exc}") from exc


def _sum_int(record: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    total = 0
    seen = False
    for key in keys:
        value = record.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            total += value
            seen = True
    return total if seen else None


def _session_summary(path: Path) -> dict[str, Any]:
    meta = _load_json(path / "meta.json")
    usage = _load_json(path / "usage.json")
    timing = _load_json(path / "timing.json")
    host = meta.get("host", "unknown")
    model = meta.get("model", "unknown")
    total_tokens = usage.get("total_tokens")
    if not isinstance(total_tokens, int):
        total_tokens = _sum_int(usage, ("input_tokens", "output_tokens"))

    components = _token_components(usage, host)
    files_changed = meta.get("files_changed")
    return {
        "session": path.name,
        "host": host,
        "model": model,
        "effort": meta.get("effort", "unknown"),
        "unifable": bool(meta.get("unifable")),
        "status": meta.get("status", "unknown"),
        "elapsed_seconds": timing.get("elapsed_seconds"),
        # Raw total (cache-inflated; kept for continuity, not the headline metric).
        "total_tokens": total_tokens,
        # Cost components: fresh input is the real prompt work, cached input bills
        # at 0.1x, output is generation. est_cost_usd cache-weights all of them.
        "fresh_input_tokens": components["fresh_input_tokens"],
        "cached_input_tokens": components["cached_input_tokens"],
        "cache_write_tokens": components["cache_write_tokens"],
        "output_tokens": components["output_tokens"],
        "reasoning_tokens": components["reasoning_tokens"],
        "est_cost_usd": _est_cost_usd(components, model),
        "files_changed": files_changed if isinstance(files_changed, int) else None,
        "result_dir": str(path),
    }


def summarize(raw_dir: Path) -> dict[str, Any]:
    sessions = [
        _session_summary(path)
        for path in sorted(raw_dir.iterdir())
        if path.is_dir() and (path / "meta.json").exists()
    ]
    groups: dict[str, list[dict[str, Any]]] = {}
    for session in sessions:
        key = f"{session['host']}:{'unifable' if session['unifable'] else 'baseline'}"
        groups.setdefault(key, []).append(session)

    aggregates = []
    for key, items in sorted(groups.items()):
        # Means are over successful cells only: a transient failure (e.g. an API
        # 529) produces a zero-token, short-elapsed cell that would otherwise drag
        # every average toward zero. Repeats exist precisely so one bad cell does
        # not sink the condition.
        ok = [i for i in items if i.get("status") == "completed"]
        aggregates.append(
            {
                "condition": key,
                "runs": len(items),
                "completed_runs": len(ok),
                "mean_elapsed_seconds": _avg([i["elapsed_seconds"] for i in ok], 3),
                "mean_est_cost_usd": _avg([i["est_cost_usd"] for i in ok], 4),
                "mean_output_tokens": _avg([i["output_tokens"] for i in ok], 1),
                "mean_fresh_input_tokens": _avg([i["fresh_input_tokens"] for i in ok], 1),
                "mean_cached_input_tokens": _avg([i["cached_input_tokens"] for i in ok], 1),
                "mean_files_changed": _avg([i["files_changed"] for i in ok], 2),
                "mean_total_tokens": _avg([i["total_tokens"] for i in ok], 1),
            }
        )

    return {
        "raw_dir": str(raw_dir),
        "pricing_as_of": PRICING_AS_OF,
        "sessions": sessions,
        "aggregates": aggregates,
    }


def missing_conditions(summary: dict[str, Any]) -> set[str]:
    """Return the required benchmark cells absent from *summary*'s aggregates."""
    present = {row.get("condition") for row in summary.get("aggregates", [])}
    return set(REQUIRED_CONDITIONS) - present


def is_accepted(summary: dict[str, Any]) -> bool:
    """A benchmark result is accepted only when all four required cells are present."""
    return not missing_conditions(summary)


def _cell(value: Any) -> str:
    return "n/a" if value is None else str(value)


def write_markdown(summary: dict[str, Any], path: Path) -> None:
    lines = [
        "# Benchmark Results",
        "",
        "| Condition | Runs (ok/total) | Mean elapsed s | Est. cost USD | Output tok | Fresh input tok | Cached input tok | Files changed |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary["aggregates"]:
        runs = f"{row.get('completed_runs', row['runs'])}/{row['runs']}"
        lines.append(
            f"| {row['condition']} | {runs} | "
            f"{_cell(row.get('mean_elapsed_seconds'))} | {_cell(row.get('mean_est_cost_usd'))} | "
            f"{_cell(row.get('mean_output_tokens'))} | {_cell(row.get('mean_fresh_input_tokens'))} | "
            f"{_cell(row.get('mean_cached_input_tokens'))} | {_cell(row.get('mean_files_changed'))} |"
        )
    lines.append("")
    lines.append(
        f"Est. cost USD is a cache-weighted cost proxy from first-party API list prices "
        f"(as of {summary.get('pricing_as_of', PRICING_AS_OF)}); cache reads bill at 0.1x input, "
        f"so it is far more honest than raw total tokens. Raw `total_tokens` (cache-inflated) "
        f"is retained per-session in summary.json. Files changed counts distinct files edited -- "
        f"a proxy for productive work versus gate/exploration churn."
    )
    lines.append("")
    lines.append("Raw session artifacts are stored beside this summary.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw_dir", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.dry_run and not any(args.raw_dir.glob("*/meta.json")):
        args.out.parent.mkdir(parents=True, exist_ok=True)
        sample = args.raw_dir / "sample-claude-unifable"
        sample.mkdir(parents=True, exist_ok=True)
        (sample / "meta.json").write_text(
            json.dumps(
                {
                    "host": "claude",
                    "model": "opus-4.8",
                    "effort": "xhigh",
                    "unifable": True,
                    "status": "dry-run",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (sample / "timing.json").write_text('{"elapsed_seconds": 1.0}\n', encoding="utf-8")
        (sample / "usage.json").write_text('{"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}\n', encoding="utf-8")

    summary = summarize(args.raw_dir)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    if args.markdown:
        write_markdown(summary, args.markdown)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
