#!/usr/bin/env python3
"""Bedrock Nemotron TTFT ablation benchmark.

Usage:
  AWS_PROFILE=kepler-admin python3 scripts/bench_bedrock_ttft.py --region us-east-1
  AWS_BEARER_TOKEN_BEDROCK=... python3 scripts/bench_bedrock_ttft.py --region us-west-2
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
MODEL = "nvidia.nemotron-nano-3-30b"
PROMPT = """Explain how a hash table handles collisions using chaining and open addressing.
Cover time complexity for insert, lookup, and delete. Give one concrete example.
Be thorough but concise — aim for about 300 words."""


@dataclass
class RunResult:
    label: str
    run: int
    ttft_ms: Optional[float]
    wall_s: float
    output_tokens: Optional[int]
    error: Optional[str] = None

    @property
    def e2e_tps(self) -> Optional[float]:
        if self.output_tokens and self.wall_s > 0:
            return self.output_tokens / self.wall_s
        return None

    @property
    def gen_tps(self) -> Optional[float]:
        if self.output_tokens and self.ttft_ms and self.wall_s > (self.ttft_ms / 1000):
            gen = self.wall_s - (self.ttft_ms / 1000)
            return self.output_tokens / gen if gen > 0 else None
        return None


def _boto3_client(cold: bool = False):
    import boto3
    from botocore.config import Config

    if cold:
        return boto3.client(
            "bedrock-runtime",
            region_name=REGION,
            config=Config(tcp_keepalive=False, retries={"max_attempts": 1}),
        )
    return boto3.client(
        "bedrock-runtime",
        region_name=REGION,
        config=Config(
            tcp_keepalive=True,
            retries={"max_attempts": 2, "mode": "standard"},
            read_timeout=300,
        ),
    )


_WARM_CLIENT = None


def _warm_client():
    global _WARM_CLIENT
    if _WARM_CLIENT is None:
        _WARM_CLIENT = _boto3_client(cold=False)
    return _WARM_CLIENT


def bench_converse_stream(client, reasoning_off: bool, run: int, label: str) -> RunResult:
    t0 = time.perf_counter()
    ttft = None
    usage = None
    err = None
    extra: dict[str, Any] = {}
    if reasoning_off:
        extra["additionalModelRequestFields"] = {
            "chat_template_kwargs": {"enable_thinking": False},
        }
    try:
        resp = client.converse_stream(
            modelId=MODEL,
            messages=[{"role": "user", "content": [{"text": PROMPT}]}],
            inferenceConfig={"maxTokens": 512, "temperature": 0.7},
            **extra,
        )
        for event in resp["stream"]:
            if "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta") or {}
                if delta.get("text") and ttft is None:
                    ttft = (time.perf_counter() - t0) * 1000
            if "metadata" in event:
                usage = (event["metadata"] or {}).get("usage")
    except Exception as exc:
        err = str(exc)
    wall = time.perf_counter() - t0
    ot = (usage or {}).get("outputTokens")
    return RunResult(label, run, ttft, wall, ot, err)


def _mantle_stream(reasoning_off: bool, run: int, label: str) -> RunResult:
    token = os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "")
    if not token:
        return RunResult(label, run, None, 0, None, "AWS_BEARER_TOKEN_BEDROCK not set")

    url = f"https://bedrock-mantle.{REGION}.api.aws/v1/chat/completions"
    body: dict[str, Any] = {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": 512,
        "temperature": 0.7,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if reasoning_off:
        body["chat_template_kwargs"] = {"enable_thinking": False}

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )
    t0 = time.perf_counter()
    ttft = None
    usage = None
    err = None
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            buf = b""
            for chunk in resp:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == b"[DONE]":
                        continue
                    evt = json.loads(payload)
                    if evt.get("error"):
                        err = str(evt["error"])
                        break
                    if evt.get("usage"):
                        usage = evt["usage"]
                    choices = evt.get("choices") or []
                    if choices:
                        delta = (choices[0].get("delta") or {}).get("content") or ""
                        if delta and ttft is None:
                            ttft = (time.perf_counter() - t0) * 1000
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")[:400]
    except Exception as exc:
        err = str(exc)
    wall = time.perf_counter() - t0
    ot = (usage or {}).get("completion_tokens")
    return RunResult(label, run, ttft, wall, ot, err)


def summarize(results: list[RunResult]) -> None:
    ok = [r for r in results if not r.error and r.ttft_ms is not None]
    print(f"\n=== {results[0].label if results else '?'} ===")
    for r in results:
        if r.error:
            print(f"  run {r.run}: ERROR {r.error[:200]}")
        elif r.e2e_tps and r.gen_tps:
            print(
                f"  run {r.run}: ttft={r.ttft_ms:.0f}ms wall={r.wall_s:.2f}s "
                f"tok={r.output_tokens} e2e={r.e2e_tps:.0f} gen={r.gen_tps:.0f}"
            )
        else:
            print(f"  run {r.run}: ttft={r.ttft_ms} wall={r.wall_s:.2f}s tok={r.output_tokens}")
    if ok:
        print(
            f"  AVG: ttft={statistics.mean([r.ttft_ms for r in ok]):.0f}ms "
            f"wall={statistics.mean([r.wall_s for r in ok]):.2f}s "
            f"e2e={statistics.mean([r.e2e_tps for r in ok if r.e2e_tps]):.0f} "
            f"gen={statistics.mean([r.gen_tps for r in ok if r.gen_tps]):.0f}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Bedrock Nemotron TTFT ablation")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--region", default=os.environ.get("BEDROCK_REGION", "us-east-1"))
    args = parser.parse_args()

    global REGION
    REGION = args.region

    print(f"Region: {REGION}  Model: {MODEL}  Runs/config: {args.runs}")

    try:
        import boto3  # noqa: F401
    except ImportError:
        print("boto3 required for converse-stream variants", file=sys.stderr)
        return 1

    suites = [
        (
            "converse-stream | warm client + tcp_keepalive | reasoning default",
            lambda i: bench_converse_stream(_warm_client(), False, i, "warm-default"),
        ),
        (
            "converse-stream | warm client + tcp_keepalive | reasoning off",
            lambda i: bench_converse_stream(_warm_client(), True, i, "warm-no-reason"),
        ),
        (
            "converse-stream | cold client per run | reasoning off",
            lambda i: bench_converse_stream(_boto3_client(cold=True), True, i, "cold-no-reason"),
        ),
        (
            "mantle chat/completions stream | bearer | reasoning default",
            lambda i: _mantle_stream(False, i, "mantle-default"),
        ),
        (
            "mantle chat/completions stream | bearer | reasoning off",
            lambda i: _mantle_stream(True, i, "mantle-no-reason"),
        ),
    ]

    all_avgs: dict[str, float] = {}
    for title, fn in suites:
        print(f"\n--- {title} ---")
        results = [fn(i) for i in range(1, args.runs + 1)]
        summarize(results)
        ok = [r for r in results if not r.error and r.ttft_ms is not None]
        if ok:
            all_avgs[title] = statistics.mean([r.ttft_ms for r in ok])

    if all_avgs:
        print("\n=== TTFT ranking (lower is better) ===")
        for title, avg in sorted(all_avgs.items(), key=lambda x: x[1]):
            print(f"  {avg:.0f}ms  {title}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
