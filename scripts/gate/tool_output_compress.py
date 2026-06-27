"""Age-based tool-output compression for judge transcript rendering.

Ports headtail, dspc, and mask strategies from patchpress compact-full-transcript.mjs.
"""

from __future__ import annotations

import math
import os
import re
from typing import Any

from tool_use_format import sha256_text

DEFAULT_KEEP_RECENT = 64
DEFAULT_MIN_CHARS = 2400
DEFAULT_HEAD_CHARS = 900
DEFAULT_TAIL_CHARS = 500
DEFAULT_STRATEGY = "mask"

DEFAULT_DSPC_STAGE1_RATIO = 0.7
DEFAULT_DSPC_BETA_ATTN = 0.6
DEFAULT_DSPC_BETA_LOSS = 0.3
DEFAULT_DSPC_BETA_POS = 0.1
DEFAULT_DSPC_POS_LAMBDA = 1.0
DEFAULT_DSPC_POS_SIGMA_FRAC = 0.25


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def judge_compression_config() -> dict[str, Any]:
    strategy = str(os.environ.get("UNIFABLE_JUDGE_TOOL_OUTPUT_STRATEGY") or DEFAULT_STRATEGY).lower()
    if strategy not in {"headtail", "dspc", "mask"}:
        strategy = DEFAULT_STRATEGY
    return {
        "strategy": strategy,
        "keep_recent": _int_env("UNIFABLE_JUDGE_TOOL_OUTPUT_KEEP_RECENT", DEFAULT_KEEP_RECENT),
        "min_chars": _int_env("UNIFABLE_JUDGE_TOOL_OUTPUT_MIN_CHARS", DEFAULT_MIN_CHARS),
        "head_chars": _int_env("UNIFABLE_JUDGE_TOOL_OUTPUT_HEAD_CHARS", DEFAULT_HEAD_CHARS),
        "tail_chars": _int_env("UNIFABLE_JUDGE_TOOL_OUTPUT_TAIL_CHARS", DEFAULT_TAIL_CHARS),
        "tool_use_min_chars": _int_env("UNIFABLE_JUDGE_TOOL_USE_COMPRESS_MIN_CHARS", 800),
        "tool_use_head_chars": _int_env("UNIFABLE_JUDGE_TOOL_USE_COMPRESS_HEAD_CHARS", 400),
        "tool_use_tail_chars": _int_env("UNIFABLE_JUDGE_TOOL_USE_COMPRESS_TAIL_CHARS", 200),
        "dspc_stage1_ratio": _float_env("UNIFABLE_JUDGE_DSPC_STAGE1_RATIO", DEFAULT_DSPC_STAGE1_RATIO),
        "dspc_beta_attn": _float_env("UNIFABLE_JUDGE_DSPC_BETA_ATTN", DEFAULT_DSPC_BETA_ATTN),
        "dspc_beta_loss": _float_env("UNIFABLE_JUDGE_DSPC_BETA_LOSS", DEFAULT_DSPC_BETA_LOSS),
        "dspc_beta_pos": _float_env("UNIFABLE_JUDGE_DSPC_BETA_POS", DEFAULT_DSPC_BETA_POS),
        "dspc_pos_lambda": _float_env("UNIFABLE_JUDGE_DSPC_POS_LAMBDA", DEFAULT_DSPC_POS_LAMBDA),
        "dspc_pos_sigma_frac": _float_env("UNIFABLE_JUDGE_DSPC_POS_SIGMA_FRAC", DEFAULT_DSPC_POS_SIGMA_FRAC),
    }


def compact_old_tool_output_headtail(body: str, entry: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    text = str(body or "")
    min_chars = int(cfg["min_chars"])
    head_chars = int(cfg["head_chars"])
    tail_chars = int(cfg["tail_chars"])
    if len(text) <= min_chars:
        return {"body": text, "compressed": False}
    head = text[:head_chars].rstrip("\n")
    tail = text[max(len(text) - tail_chars, head_chars) :].lstrip("\n").rstrip("\n")
    omitted = max(len(text) - len(head) - len(tail), 0)
    marker = (
        f"[tool output compressed: original_chars={len(text)} omitted_chars={omitted} "
        f"line={entry.get('lineNumber')} body_sha256={sha256_text(text)} "
        f"record_sha256={entry.get('hash')}]"
    )
    return {
        "body": "\n".join([head, "", marker, "", tail]),
        "compressed": True,
        "originalChars": len(text),
        "omittedChars": omitted,
    }


def _dspc_split_sentences(text: str) -> list[str]:
    segments: list[str] = []
    for raw_line in str(text).split("\n"):
        if raw_line.strip() == "":
            continue
        parts = re.split(r"(?<=[.!?])\s+", raw_line) if re.search(r"[.!?]\s", raw_line) else [raw_line]
        for part in parts:
            if part.strip():
                segments.append(part)
    return segments


def _dspc_tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+", str(text).lower())


def _dspc_max(values: list[float]) -> float:
    return max(values) if values else 0.0


def compact_old_tool_output_dspc(body: str, entry: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    text = str(body or "")
    min_chars = int(cfg["min_chars"])
    head_chars = int(cfg["head_chars"])
    tail_chars = int(cfg["tail_chars"])
    if len(text) <= min_chars:
        return {"body": text, "compressed": False}
    budget = max(head_chars + tail_chars, 1)

    segments = _dspc_split_sentences(text)
    n = len(segments)
    if n <= 1:
        return compact_old_tool_output_headtail(body, entry, cfg)

    seg_tokens = [_dspc_tokenize(seg) for seg in segments]
    df: dict[str, int] = {}
    for toks in seg_tokens:
        for term in set(toks):
            df[term] = df.get(term, 0) + 1

    def idf(term: str) -> float:
        return math.log(n / df.get(term, 1))

    global_tf: dict[str, int] = {}
    for toks in seg_tokens:
        for term in toks:
            global_tf[term] = global_tf.get(term, 0) + 1
    global_score = {term: tf * idf(term) for term, tf in global_tf.items()}
    query_terms = {
        term
        for term, _ in sorted(global_score.items(), key=lambda kv: (-kv[1], kv[0]))[: min(12, len(global_score))]
    }

    stage1_score = []
    for toks in seg_tokens:
        if not toks:
            stage1_score.append(0.0)
            continue
        score = sum(idf(term) for term in toks if term in query_terms)
        stage1_score.append(score / math.sqrt(len(toks)))

    keep_stage1 = max(1, int(math.floor(float(cfg["dspc_stage1_ratio"]) * n)))
    stage1_idx = sorted(range(n), key=lambda i: (-stage1_score[i], i))[:keep_stage1]

    sigma = max(float(cfg["dspc_pos_sigma_frac"]) * n, 1.0)
    raw_attn: list[float] = []
    raw_loss: list[float] = []
    raw_pos: list[float] = []
    for i, toks in enumerate(seg_tokens):
        denom = len(toks) or 1
        salience = sum(global_score.get(term, 0.0) for term in toks)
        informativeness = sum(idf(term) for term in toks)
        raw_attn.append(salience / denom)
        raw_loss.append(informativeness / denom)
        raw_pos.append(1.0 + float(cfg["dspc_pos_lambda"]) * math.exp(-(((i - n / 2) ** 2) / (2 * sigma * sigma))))

    def normalize(arr: list[float]) -> list[float]:
        mx = _dspc_max(arr)
        return [x / mx for x in arr] if mx > 0 else [0.0] * len(arr)

    n_attn = normalize(raw_attn)
    n_loss = normalize(raw_loss)
    alpha = [
        float(cfg["dspc_beta_attn"]) * n_attn[i]
        + float(cfg["dspc_beta_loss"]) * n_loss[i]
        + float(cfg["dspc_beta_pos"]) * raw_pos[i]
        for i in range(n)
    ]

    ranked = sorted(stage1_idx, key=lambda i: (-alpha[i], i))
    selected: set[int] = set()
    used = 0
    for i in ranked:
        cost = len(segments[i]) + 1
        if selected and used + cost > budget:
            continue
        selected.add(i)
        used += cost
        if used >= budget:
            break

    kept_ordered = sorted(selected)
    pieces: list[str] = []
    prev = -1
    for i in kept_ordered:
        if prev != -1 and i > prev + 1:
            pieces.append("[...]")
        pieces.append(segments[i])
        prev = i
    if kept_ordered and kept_ordered[0] > 0:
        pieces.insert(0, "[...]")
    if kept_ordered and kept_ordered[-1] < n - 1:
        pieces.append("[...]")
    kept = "\n".join(pieces)
    omitted = max(len(text) - len(kept), 0)
    marker = (
        f"[tool output compressed: strategy=dspc original_chars={len(text)} omitted_chars={omitted} "
        f"line={entry.get('lineNumber')} body_sha256={sha256_text(text)} "
        f"record_sha256={entry.get('hash')} stage1_kept={keep_stage1}/{n} "
        f"stage2_kept={len(kept_ordered)}]"
    )
    return {
        "body": "\n".join([marker, "", kept]),
        "compressed": True,
        "originalChars": len(text),
        "omittedChars": omitted,
    }


def mask_old_tool_output_body(body: str, entry: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    text = str(body or "")
    min_chars = int(cfg["min_chars"])
    if len(text) <= min_chars:
        return {"body": text, "compressed": False}
    line_count = text.count("\n") + (1 if text else 0)
    marker = (
        f"[tool output masked: strategy=mask original_chars={len(text)} original_lines={line_count} "
        f"omitted_chars={len(text)} line={entry.get('lineNumber')} body_sha256={sha256_text(text)} "
        f"record_sha256={entry.get('hash')}]"
    )
    return {
        "body": marker,
        "compressed": True,
        "originalChars": len(text),
        "omittedChars": len(text),
    }


def compact_tool_output_body(body: str, entry: dict[str, Any], cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or judge_compression_config()
    strategy = cfg["strategy"]
    if strategy == "mask":
        return mask_old_tool_output_body(body, entry, cfg)
    if strategy == "dspc":
        return compact_old_tool_output_dspc(body, entry, cfg)
    return compact_old_tool_output_headtail(body, entry, cfg)
