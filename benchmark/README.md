# unifable benchmark harness

This directory contains the local harness for comparing Claude Code and Codex CLI
on the same complex task with unifable enabled and disabled.

The runner writes each session under `benchmark/results/<run-id>/`:

- `raw/` contains per-session stdout, stderr, timing, and optional terminal recordings.
- `summary.json` contains normalized timing plus cost components: `fresh_input_tokens`,
  `cached_input_tokens`, `output_tokens`, a cache-weighted `est_cost_usd`, and
  `files_changed`. Raw `total_tokens` is kept per session but is no longer the headline,
  because it is dominated by near-free cache reads.
- `summary.md` is the human-readable report used by the top-level README.

Why cost-weighting matters and how the metric is defined is documented in
[docs/benchmark-methodology.md](../docs/benchmark-methodology.md).

The default task is intentionally small enough to run repeatedly but complex
enough to require a real edit plus verification: add a new evidence-gate
regression test and make it pass in an isolated worktree copy. (It is also
self-referential, which is a known confound for gate-aware agents — see the
methodology doc.)

Run the full four-cell matrix, optionally with repeats for variance:

```bash
python3 benchmark/bench.py --run-id "$(date -u +%Y%m%dT%H%M%SZ)" --repeats 3
```

Run a dry check without launching agent CLIs:

```bash
python3 benchmark/bench.py --dry-run
python3 benchmark/summarize.py benchmark/results/dry-run/raw --out benchmark/results/dry-run/summary.json --dry-run
```
