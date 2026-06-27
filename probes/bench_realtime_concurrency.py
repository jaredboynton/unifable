#!/usr/bin/env python3
"""Live concurrency probe for the Codex Realtime transport.

Measures the THREE distinct numbers the daemon stack conflated as folklore:

  Axis A - session concurrency: how many sockets connect cleanly at once (tests
           the unsupported "the account throttles concurrent sessions around 8"
           claim). Opens K sockets simultaneously and records per-socket
           handshake outcome + any 429/refusal/close.
  Axis B - pool latency: the production shape. Fan out N small structured asks
           across P warm sockets (round-robin), median of repeats. This is what
           daemon-client.mjs POOL_SIZE should be tuned on.
  Axis C - per-socket in-flight: re-validate BATCH_MAX_INFLIGHT. Fire M
           out-of-band response.create on ONE socket and count silent
           empties/drops (the model skipping the required tool call under load).

Stdlib only. Reuses codex_judge's exact transport (no reimplemented framing) so
it measures the path the daemon actually uses. Writes raw.json + summary.md to a
persisted results dir so the chosen caps cite evidence, not memory.

Usage:
  python3 probes/bench_realtime_concurrency.py \
    --models gpt-realtime-2,gpt-realtime-mini \
    --axes a,b,c \
    --out probes/bench/results/<ts>
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
# This probe lives in probes/ (excluded from `just test-all`) but reuses the gate's
# real transport, so put scripts/gate on the path rather than the probe's own dir.
sys.path.insert(0, str(HERE.parent / "scripts" / "gate"))

import codex_judge as cj  # noqa: E402

# A trivial, fixed structured ask: the unit of work is the same shape the daemon
# scores with (one required tool call returning a single integer).
SCORE_SCHEMA = {
    "type": "object",
    "properties": {"score": {"type": "integer"}},
    "required": ["score"],
    "additionalProperties": False,
}
SYSTEM = "You return a single integer score via the score tool. Always call the tool exactly once."
USER = "Return the integer 7 via the score tool now."


def _req() -> dict[str, Any]:
    return {"system": SYSTEM, "user": USER, "schema": SCORE_SCHEMA, "schema_name": "score"}


def _valid(chosen: str | None) -> bool:
    if not chosen or not chosen.strip():
        return False
    try:
        obj = json.loads(chosen)
    except json.JSONDecodeError:
        return False
    return isinstance(obj, dict) and isinstance(obj.get("score"), int)


# --- Axis A: session concurrency --------------------------------------------

def axis_session(model: str, ks: list[int], auth_path: str | None, timeout: float) -> list[dict[str, Any]]:
    rows = []
    for k in ks:
        results: list[dict[str, Any]] = [{} for _ in range(k)]
        tokens = cj._fresh_tokens(auth_path, force=False)
        barrier = threading.Barrier(k)

        def open_one(i: int) -> None:
            barrier.wait()  # release all connects as simultaneously as threads allow
            t0 = time.monotonic()
            try:
                sock = cj._ws_connect(tokens, model, timeout)
                results[i] = {"ok": True, "connect_ms": round((time.monotonic() - t0) * 1000)}
                try:
                    sock.sendall(cj._encode_frame(b"", opcode=0x8))
                    sock.close()
                except OSError:
                    pass
            except Exception as exc:  # noqa: BLE001 - record every failure verbatim
                results[i] = {"ok": False, "connect_ms": round((time.monotonic() - t0) * 1000), "error": str(exc)[:200]}

        threads = [threading.Thread(target=open_one, args=(i,)) for i in range(k)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ok = sum(1 for r in results if r.get("ok"))
        errs = [r.get("error", "") for r in results if not r.get("ok")]
        rows.append({"k": k, "ok": ok, "failed": k - ok, "errors": errs[:5],
                     "connect_ms_p50": _p50([r.get("connect_ms") for r in results if r.get("ok")])})
        print(f"[A session] {model} K={k}: {ok}/{k} connected"
              + (f"  ERR: {errs[0]}" if errs else ""), file=sys.stderr)
    return rows


# --- shared OOB driver (one socket, M responses) ----------------------------

def _drive_oob(model: str, reqs: list[dict[str, Any]], auth_path: str | None, timeout: float) -> dict[str, Any]:
    """Open one socket, fire len(reqs) OOB response.create, read until all finish
    or timeout. Mirrors cj._batch_once but returns valid/empty/error counts and
    wall time. Never raises (records the failure)."""
    n = len(reqs)
    t0 = time.monotonic()
    try:
        tokens = cj._fresh_tokens(auth_path, force=False)
        sock = cj._ws_connect(tokens, model, cj.HANDSHAKE_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        return {"n": n, "valid": 0, "empty": 0, "errored": n, "session_error": str(exc)[:200],
                "wall_ms": round((time.monotonic() - t0) * 1000)}
    state = cj._new_batch_state(n)
    try:
        cj._send_text(sock, {"type": "session.update", "session": {"type": "realtime", "output_modalities": ["text"]}})
        for cid, req in enumerate(reqs):
            cj._send_text(sock, cj._response_create(req, cid))
        deadline = time.monotonic() + timeout
        while len(state["finished"]) < n and not state["session_error"]:
            if time.monotonic() >= deadline:
                break
            opcode, payload = cj._read_message(sock)
            if opcode == 0x8:
                break
            if opcode == 0x9:
                sock.sendall(cj._encode_frame(payload, opcode=0xA))
                continue
            if opcode not in (0x0, 0x1, 0x2):
                continue
            try:
                env = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            cj._batch_route(state, env)
    finally:
        try:
            sock.sendall(cj._encode_frame(b"", opcode=0x8))
            sock.close()
        except OSError:
            pass
    valid = empty = errored = 0
    for i in range(n):
        if state["error"][i] or (state["session_error"] and i not in state["finished"]):
            errored += 1
            continue
        if _valid(cj._batch_chosen(state, i)):
            valid += 1
        else:
            empty += 1
    return {"n": n, "valid": valid, "empty": empty, "errored": errored,
            "finished": len(state["finished"]), "session_error": state["session_error"],
            "wall_ms": round((time.monotonic() - t0) * 1000)}


# --- Axis C: per-socket in-flight -------------------------------------------

def axis_inflight(model: str, ms: list[int], repeats: int, auth_path: str | None, timeout: float) -> list[dict[str, Any]]:
    rows = []
    for m in ms:
        samples = [_drive_oob(model, [_req() for _ in range(m)], auth_path, timeout) for _ in range(repeats)]
        valid = [s["valid"] for s in samples]
        empty = [s["empty"] for s in samples]
        rows.append({"m": m, "repeats": repeats, "valid_min": min(valid), "valid_med": _p50(valid),
                     "empty_med": _p50(empty), "drop_rate_med": round(_p50(empty) / m, 4) if m else 0,
                     "wall_ms_med": _p50([s["wall_ms"] for s in samples]), "samples": samples})
        print(f"[C inflight] {model} M={m}: valid_med={_p50(valid)}/{m} drop_med={_p50(empty)}", file=sys.stderr)
    return rows


# --- Axis B: pool latency ----------------------------------------------------

def axis_pool(model: str, ps: list[int], n: int, repeats: int, auth_path: str | None, timeout: float) -> list[dict[str, Any]]:
    rows = []
    for p in ps:
        wall_samples = []
        valid_samples = []
        for _ in range(repeats):
            # Spread N requests across P sockets, each socket driving its share as
            # OOB responses; sockets run concurrently in threads (socket I/O
            # releases the GIL, so wall reflects API-side parallelism).
            buckets: list[list[dict[str, Any]]] = [[] for _ in range(p)]
            for i in range(n):
                buckets[i % p].append(_req())
            buckets = [b for b in buckets if b]
            out: list[dict[str, Any]] = [{} for _ in buckets]
            t0 = time.monotonic()

            def run_bucket(idx: int, reqs: list[dict[str, Any]]) -> None:
                out[idx] = _drive_oob(model, reqs, auth_path, timeout)

            threads = [threading.Thread(target=run_bucket, args=(idx, b)) for idx, b in enumerate(buckets)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            wall = round((time.monotonic() - t0) * 1000)
            wall_samples.append(wall)
            valid_samples.append(sum(o.get("valid", 0) for o in out))
        rows.append({"p": p, "n": n, "repeats": repeats, "wall_ms_med": _p50(wall_samples),
                     "wall_ms_min": min(wall_samples), "valid_med": _p50(valid_samples),
                     "wall_samples": wall_samples, "valid_samples": valid_samples})
        print(f"[B pool] {model} P={p} N={n}: wall_med={_p50(wall_samples)}ms valid_med={_p50(valid_samples)}/{n}",
              file=sys.stderr)
    return rows


def _p50(xs: list[Any]) -> Any:
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    return round(statistics.median(xs), 2)


def _recommend(model: str, pool_rows: list[dict[str, Any]], inflight_rows: list[dict[str, Any]],
               session_rows: list[dict[str, Any]]) -> dict[str, Any]:
    rec: dict[str, Any] = {"model": model}
    # Pool: smallest P whose median wall is within 5% of the best observed wall.
    if pool_rows:
        best = min(r["wall_ms_med"] for r in pool_rows if r["wall_ms_med"] is not None)
        within = [r["p"] for r in pool_rows if r["wall_ms_med"] is not None and r["wall_ms_med"] <= best * 1.05]
        rec["pool_size"] = min(within) if within else None
        rec["pool_best_wall_ms"] = best
    # In-flight: largest M with zero median drops; cap at ~half that for margin.
    if inflight_rows:
        clean = [r["m"] for r in inflight_rows if (r["empty_med"] or 0) == 0]
        rec["inflight_clean_max"] = max(clean) if clean else None
        first_drop = [r["m"] for r in inflight_rows if (r["empty_med"] or 0) > 0]
        rec["inflight_first_drop"] = min(first_drop) if first_drop else None
    # Session: max K that connected fully; whether any ceiling was observed.
    if session_rows:
        full = [r["k"] for r in session_rows if r["failed"] == 0]
        rec["session_max_clean"] = max(full) if full else None
        rec["session_ceiling_observed"] = any(r["failed"] > 0 for r in session_rows)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="gpt-realtime-2,gpt-realtime-mini")
    ap.add_argument("--axes", default="a,b,c")
    ap.add_argument("--out", default="")
    ap.add_argument("--auth-path", default=os.environ.get("EXPLORE_CODEX_AUTH_PATH"))
    ap.add_argument("--timeout", type=float, default=30.0)
    # Light scope defaults (matches approved plan).
    ap.add_argument("--session-ks", default="8,16,24,32")
    ap.add_argument("--pool-ps", default="4,8,16")
    ap.add_argument("--pool-n", type=int, default=16)
    ap.add_argument("--pool-repeats", type=int, default=2)
    ap.add_argument("--inflight-ms", default="128,224")
    ap.add_argument("--inflight-repeats", type=int, default=2)
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    axes = {a.strip().lower() for a in args.axes.split(",") if a.strip()}
    ks = [int(x) for x in args.session_ks.split(",") if x.strip()]
    ps = [int(x) for x in args.pool_ps.split(",") if x.strip()]
    ms = [int(x) for x in args.inflight_ms.split(",") if x.strip()]

    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_dir = Path(args.out) if args.out else (HERE / "bench" / "results" / stamp)
    out_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {"stamp": stamp, "models": models, "axes": sorted(axes),
                              "params": {"session_ks": ks, "pool_ps": ps, "pool_n": args.pool_n,
                                         "pool_repeats": args.pool_repeats, "inflight_ms": ms,
                                         "inflight_repeats": args.inflight_repeats},
                              "results": {}, "recommendations": []}

    for model in models:
        # codex_judge._realtime_reasoning_config gates the `reasoning` field on the
        # MODULE-level MODEL ("mini" rejects reasoning). The real daemon sets that
        # via UNIFABLE_JUDGE_MODEL at spawn; here we drive multiple models in one
        # process, so align the module constant to the model under test before
        # building any response.create (otherwise mini gets a reasoning field it
        # rejects and every response errors out).
        cj.MODEL = model
        mres: dict[str, Any] = {}
        if "a" in axes:
            mres["session"] = axis_session(model, ks, args.auth_path, args.timeout)
        if "b" in axes:
            mres["pool"] = axis_pool(model, ps, args.pool_n, args.pool_repeats, args.auth_path, args.timeout)
        if "c" in axes:
            mres["inflight"] = axis_inflight(model, ms, args.inflight_repeats, args.auth_path, args.timeout)
        report["results"][model] = mres
        report["recommendations"].append(_recommend(model, mres.get("pool", []), mres.get("inflight", []), mres.get("session", [])))

    (out_dir / "raw.json").write_text(json.dumps(report, indent=2) + "\n")
    (out_dir / "summary.md").write_text(_summary_md(report))
    print(f"\nResults: {out_dir}\n", file=sys.stderr)
    print(_summary_md(report))
    return 0


def _summary_md(report: dict[str, Any]) -> str:
    L = [f"# Realtime concurrency probe - {report['stamp']}", "",
         f"Models: {', '.join(report['models'])} · axes: {', '.join(report['axes'])}",
         f"Params: {json.dumps(report['params'])}", ""]
    for model, mres in report["results"].items():
        L.append(f"## {model}")
        if "session" in mres:
            L += ["", "### Axis A - session concurrency", "",
                  "| K | connected | failed | connect p50 (ms) | first error |", "|---|---|---|---|---|"]
            for r in mres["session"]:
                L.append(f"| {r['k']} | {r['ok']} | {r['failed']} | {r['connect_ms_p50']} | {(r['errors'][0] if r['errors'] else '-')[:60]} |")
        if "pool" in mres:
            L += ["", "### Axis B - pool latency (N=%d)" % report["params"]["pool_n"], "",
                  "| P | wall med (ms) | wall min (ms) | valid med | N |", "|---|---|---|---|---|"]
            for r in mres["pool"]:
                L.append(f"| {r['p']} | {r['wall_ms_med']} | {r['wall_ms_min']} | {r['valid_med']} | {r['n']} |")
        if "inflight" in mres:
            L += ["", "### Axis C - per-socket in-flight", "",
                  "| M | valid med | valid min | drop med | drop rate | wall med (ms) |", "|---|---|---|---|---|---|"]
            for r in mres["inflight"]:
                L.append(f"| {r['m']} | {r['valid_med']} | {r['valid_min']} | {r['empty_med']} | {r['drop_rate_med']} | {r['wall_ms_med']} |")
        L.append("")
    L += ["## Recommendations", "", "| model | pool_size | pool best wall | inflight clean max | inflight first drop | session max clean | session ceiling? |",
          "|---|---|---|---|---|---|---|"]
    for rec in report["recommendations"]:
        L.append(f"| {rec.get('model')} | {rec.get('pool_size')} | {rec.get('pool_best_wall_ms')} | "
                 f"{rec.get('inflight_clean_max')} | {rec.get('inflight_first_drop')} | "
                 f"{rec.get('session_max_clean')} | {rec.get('session_ceiling_observed')} |")
    L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    raise SystemExit(main())
