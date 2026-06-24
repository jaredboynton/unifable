# Benchmark methodology and metric design

How the unifable benchmark measures Claude Code and Codex CLI, why the original
`total_tokens` headline was misleading, and what the harness reports instead.
The harness lives in `benchmark/bench.py` (runner) and `benchmark/summarize.py`
(aggregation); the four-cell acceptance rule is enforced by
`tests/test_benchmark_harness.py`.

## The four cells

A run is only comparable when every host is measured both with and without
unifable, so the harness always produces four cells
(`benchmark/bench.py:39`, `benchmark/summarize.py:15`):

| Cell | Host | unifable | How baseline is disabled |
|---|---|---|---|
| `claude:unifable` | Claude Code | on | `--plugin-dir` + `--setting-sources project` |
| `claude:baseline` | Claude Code | off | `--safe-mode` |
| `codex:unifable` | Codex CLI | on | plugin installed in isolated `CODEX_HOME` |
| `codex:baseline` | Codex CLI | off | `--ignore-user-config` |

Each cell runs the same task prompt in an isolated worktree copy of the repo,
driven through a PTY (`tuistory`/`tctl`). Host wiring is in
`benchmark/bench.py:124` (`_command_for`). With `--repeats N`, each cell runs N
times and the means aggregate over the repeats.

## Why raw `total_tokens` is the wrong headline

The original summary reported one number per cell: `total_tokens`, the sum of
input + output + cache-creation + cache-read tokens. In practice that sum is
**78-90% cache reads**, and cache reads are nearly free.

Both vendors bill a cache hit at **0.1x the base input price**:

- Anthropic Opus 4.8: $5 input / **$0.50 cache read** / $6.25 5-minute cache
  write / $25 output per MTok
  (<https://platform.claude.com/docs/en/about-claude/pricing>).
- OpenAI GPT-5.5: $5 input / **$0.50 cached input** / $30 output per MTok; the
  cached rate is explicitly 10% of input
  (<https://developers.openai.com/api/docs/pricing>).

unifable injects a standing operating-mode context block at session start
(`hooks/session_start.py`). That block is cached once and re-read on every turn,
so it inflates `cache_read` — and therefore `total_tokens` — without a
proportional increase in real cost or real work. A metric dominated by cache
reads makes the harness *punish* the very mechanism (a stable cached preamble)
that it is supposed to reward.

There is also a vendor normalization trap: Anthropic reports `input_tokens` net
of cache, while OpenAI/Codex reports `input_tokens` inclusive of cached tokens.
Comparing the raw fields across hosts is apples-to-oranges until they are
normalized.

## What the harness reports now

`summarize.py` now splits usage into vendor-normalized components and computes a
cache-weighted cost (`benchmark/summarize.py`, `_token_components`,
`_est_cost_usd`):

- `fresh_input_tokens` — freshly processed prompt (for Codex, `input - cached`).
- `cached_input_tokens` — cache reads (billed at 0.1x).
- `cache_write_tokens` — cache creation (Anthropic only; 1.25x for 5m).
- `output_tokens` / `reasoning_tokens` — generation (and Codex reasoning).
- `est_cost_usd` — cache-weighted cost using the list prices above, keyed by
  model. This is a **comparable cost proxy, not a bill** — the CLIs run under
  subscriptions/quota, not metered API billing. Unknown models yield `null`
  rather than a guess.
- `files_changed` — distinct files the agent actually edited, a proxy for
  productive work versus gate/exploration churn
  (`benchmark/bench.py`, `_files_changed_from_text`).

Raw `total_tokens` is retained per session in `summary.json` for continuity but
is no longer the headline.

## Diagnosis of run 20260624T073500Z (the motivating example)

The original README read as though unifable made things worse. Re-scored with
the cache-weighted metric, the story inverts on one host and confirms a real
problem on the other.

| Cell | Latency | Raw total tokens | Est. cost (USD) | Output tok | Fresh input | Cached input | Distinct files changed |
|---|---:|---:|---:|---:|---:|---:|---:|
| claude:baseline | 405.6s | 174,971 | $0.55 | 12,344 | 143 | 135,368 | 1 |
| claude:unifable | 160.1s | 456,671 | $0.78 | 9,811 | 5,079 | 391,872 | 1 |
| codex:baseline | 97.5s | 334,748 | $0.59 | 4,601 | 52,353 | 275,968 | 1 |
| codex:unifable | 821.2s | 3,176,778 | $4.87 | 32,731 | 393,085 | 2,732,416 | 1 |

(Cost column re-computed from the saved `raw/*/usage.json` with the new
summarizer; the raw-token column is the old headline.)

### Claude: the single run looked like a win — but it did not replicate

In this one run `claude:unifable` finished in **160s versus the baseline's 406s**
and made **34 assistant turns versus 72** (counted from
`raw/claude-unifable/cli.stdout.jsonl` and `raw/claude-baseline/cli.stdout.jsonl`),
with **fewer output tokens** (9,811 vs 12,344). Read alone, that says grounding
made Claude converge faster and leaner, and that raw `total_tokens` (2.6x) inverted
a real ~1.4x cost win into an apparent regression.

That conclusion was an **n=1 artifact**. The 3-repeat replication below shows
`claude:unifable` is normally ~2.7x *slower* and ~6x costlier than baseline; the
160s cell was a lucky low-variance path where Claude edited directly instead of
delegating. The cache-inflation point stands; the "unifable made Claude faster"
point does not.

### Codex: a real ~8x overhead, caused by gate-wrestling

`codex:unifable` is genuinely ~8x in both latency and cost even after
cache-weighting, because fresh input (~7.5x) and output+reasoning (~8x) truly
exploded. The transcript (`raw/codex-unifable/cli.stdout.jsonl`) shows why: of
**60 command executions** (vs the baseline's 17), the majority are unifable
spec-CLI meta-work rather than the task —

- `unifable restate`, `unifable add-task`, `set-primary --help`,
  `add-frontier --help`, `dispute --help`, `set-primary`, two `add-frontier`,
  and a `dispute --task T1` that quotes `scripts/gate/spec.py` line numbers;
- repeated `rg` of `scripts/gate/spec.py` to reverse-engineer the CLI;
- the *same* targeted `pytest` invocation run roughly six times.

Yet it produced only **2 `file_change` events touching 1 distinct file** (vs the
baseline's clean 17 commands → 1 file → done). The agent rabbit-holed into the
gate's own mechanics. Two factors compound: Codex engages the spec CLI far more
aggressively than Claude, and the benchmark task is **self-referential** (it asks
the agent to add a regression test about the harness's own four-cell rule), which
pulls a gate-aware agent straight into the gate machinery.

## Replication: run 20260624T133303Z (3 repeats/cell)

Re-running with `--repeats 3` and completed-only means (a transient API 529 took
out one `claude:unifable` cell, which is excluded) gives the authoritative picture:

| Condition | ok/total | Mean elapsed | Est. cost (USD) | Output tok | Fresh input | Cached input | Files changed |
|---|---:|---:|---:|---:|---:|---:|---:|
| claude:baseline | 3/3 | 382s | $0.47 | 8,104 | 99 | 133,873 | 1 |
| claude:unifable | 2/3 | 1017s | $2.86 | 57,145 | 6,654 | 1,242,809 | 1 |
| codex:baseline | 3/3 | 90s | $0.68 | 4,059 | 81,854 | 190,421 | 1 |
| codex:unifable | 3/3 | 653s | $5.43 | 25,785 | 456,749 | 3,836,885 | 1 |

Every cell produced the one-file deliverable (`files_changed == 1`), so this is
overhead to reach the *same* output, not failed work. unifable costs ~2.7x latency
/ ~6x dollars on Claude and ~7.3x / ~8x on Codex.

Two findings from the transcripts explain it:

- **Claude delegates under the orchestrator posture.** Both completed
  `claude:unifable` cells made **zero main-thread edits** yet wrote the test file:
  they spawned subagents/workflows (one cell: 9 `Agent` + 3 `Workflow` calls) and
  the deliverable landed via a delegated worker after ~17 min. This is why the
  stream-based file counter (now replaced by a worktree snapshot — see below) read
  0; the worktree confirms the file and its tests pass.
- **Both hosts pay for gate machinery.** The cost is dominated by output and fresh
  input (Claude output 57k vs 8k baseline; Codex fresh input 457k vs 82k), i.e.
  real generation and re-reading, not cache.

**Measurement note (file changes).** The original counter parsed the agent's event
stream, which misses edits made by delegated subagents — it under-counted the
Claude cells as 0. `files_changed` is now measured by diffing a before/after
snapshot of the worktree filesystem (`bench.py` `_snapshot_worktree` /
`_files_changed_between`), which is delegation-proof. The published table above was
reconciled against the worktrees (deliverable present, tests pass).

## Known limitations

- **Self-referential task.** The default task
  (`benchmark/tasks/evidence_gate_regression.md`) is about the harness's own
  acceptance rule, which pulls gate-aware agents straight into the gate machinery —
  Codex into the spec CLI, Claude into orchestration/delegation. A neutral,
  self-contained coding task would measure grounding overhead on more
  representative work; that is the next candidate improvement, and it would likely
  shrink the overhead multiples reported here.
- **Cost ≠ quality.** This benchmark measures latency, tokens, and dollars to reach
  the deliverable. It does not score correctness, grounding, or how often the
  baseline would have shipped a wrong answer — which is the thing unifable trades
  cost for. A complete evaluation needs a quality axis this harness does not have.
- **`est_cost_usd` is a list-price proxy**, not the actual cost of the
  subscription/quota the CLIs run under. Prices are pinned with a date
  (`PRICING_AS_OF` in `summarize.py`) and will drift.
- **Small N and real variance.** Even with `--repeats 3`, these are local runs on
  one machine; one `claude:unifable` repeat failed on a transient API 529, and the
  earlier single-run headline was an outlier. Treat the multiples as order-of-
  magnitude, not precise.
