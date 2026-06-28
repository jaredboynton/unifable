# scripts/archive — retired explore experiments

These scripts are **superseded and unmaintained**. Nothing on the live explore path
imports, execs, or documents them. They are kept only for reference/history.

The supported skill is gpt-realtime-2 end to end:

- `scripts/trace.sh` → `trace-rt.sh` → `realtime-trace.mjs` (deep behavioral trace)
- `scripts/websearch.sh` → `websearch-rt.sh` → `realtime-websearch.mjs` (external research, alpha `swarm`)
- `scripts/search.sh` → `search-rt.mjs` → `search-lib.mjs` + `realtime-search.mjs` (agentic ripgrep search)

## What's here and why retired

| Group | Files | Why |
|---|---|---|
| Alternate trace engines | `trace-cursor.sh`, `cursor-*.mjs`, `trace-gemini.sh`, `gemini-trace.mjs`, `trace-gk.sh`, `grok-trace.mjs`, `lib/gk-tools.mjs`, `lib/xai_client.mjs` | gpt-realtime-2 (`trace-rt.sh`) won on quality/latency |
| Cerebras search | `search-cerebras.sh`, `search.mjs`, `cerebras-search.mjs` | `search.sh` rewritten to gpt-realtime-2 (`search-rt.mjs`) |
| Exa RT backend / ensemble | `lib/rt-exa-tools.mjs` | retired; alpha `swarm` is the sole websearch backend |
| Cerebras trace submit | `lib/rt-submit-cerebras.mjs` | RT submit (`askStructured`) is the only path |
| Gemini websearch | `websearch-gemini.sh`, `websearch-gemini.mjs` | separate agy + Exa-MCP path, retired |
| Cursor index / proto | `cursor-index.mjs`, `lib/hcursor-*.mjs`, `lib/hproto.mjs`, `test/proto-oracle.*` | cursor-agent harness, retired |
| Research runtime | `lib/rt-research-*.mjs` | unused experimental tools |
| Benchmarks | `bench-*.sh`, `bench-*.mjs`, `lib/bench-scorer*.mjs`, `lib/bench-*-scorer.mjs`, `lib/repoqa-tasks.mjs` | one-off eval harnesses |
| Probes | `probe-*.sh`, `probe-*.mjs` | one-off API probes |
| Variant tests | `test-trace-gk.sh`, `test-trace-gm.sh`, `test-trace-inputs.sh`, `test-trace-ten-dirs.sh`, `test-trace-concurrency.sh`, `test-acp-*.sh`, `test-websearch-gemini.sh`, `test/*` for the above | test the retired variants |
