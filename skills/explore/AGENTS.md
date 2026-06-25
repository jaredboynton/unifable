# Explore skill — agent notes

## Websearch transports

| Script | Model / API | Auth |
|---|---|---|
| `websearch.sh` | **Default:** gpt-realtime-2 via `websearch-rt.sh` (2 rounds: web_run/alpha + pointer submit; search `low`, submit `minimal`) | Codex OAuth + `curl` |
| `websearch-rt.sh` | **gpt-realtime-2** Realtime WebSocket + Codex `alpha/search` via `web_run` (default) | Codex OAuth + `curl` |

**Retired (archived under `scripts/archive/`, do not use):** the `EXPLORE_WS_BACKEND=exa` RT backend, the `ensemble` (F3 multi-backend) fetch mode, and `websearch-gemini.sh` (Gemini + Exa-MCP path) are retired — the native alpha `swarm` arm beats them on judged quality and breadth. Alpha is the only supported RT websearch backend.

**Alpha fetch mode:** `swarm` is the sole mode — it fires fanout source-class strategies + deepen aspect facets as one concurrent host-side search wave, then a single ranked open pass (deepest pool, ~45 URLs, at ~20s; `docs/benchmarks/websearch-swarm.md`). The older `fanout` / `deepen` / `search-open` / `search-only` / `combined` modes are retired. Swarm runs host-side over HTTP, so the pipeline reconnects the RT socket before submit (the socket idle-closes during host search; see `rt-agent-session.mjs`). Tune open breadth with `EXPLORE_WS_SWARM_OPEN_CAP` (default 18).

**Alpha defaults (speed):** one coalesced `alpha/search` batch per RT turn (`EXPLORE_WS_STOP_SEARCHES=1`, `EXPLORE_WS_COALESCE_WEB_RUN=1`), `128000` max output tokens (the model cap; small caps truncate multi-page fetches), query-matched per-URL excerpts for submit, mandatory citation coverage (cite every on-topic fetched source).

**Daemon-pool source scoring + focused synthesis (mirrors `search-fast.mjs`).** After the host swarm fetch and authority gate, websearch scores EVERY fetched page 0-10 for goal relevance in PARALLEL across the warm daemon pool (`scoreAndRankSources` -> `daemonAskBatch`), keeps pages >= floor (best first), and synthesizes the report from only the top-ranked sources via a single daemon turn (`runDaemonPointerSubmit` -> `daemonAsk`). This replaces the old monolithic submit that ingested all ~45 pages in one slow turn. Measured A/B (MCP goal): scoring 43 pages = ~0.8s on the mini pool (kept top 24), synth 12.3s vs 15.4s monolithic; same 6-section structure and citation coverage.
- **Scorer = `gpt-realtime-mini`** (`EXPLORE_WS_SCORER_MODEL`, 2x TPS, reasoning omitted); **synthesis = `gpt-realtime-2`** (`EXPLORE_WS_SYNTH_MODEL`) with reasoning OMITTED (`reasoning_effort:"none"` -> daemon sends no `reasoning` field). The pool warms concurrently with the host fanout so scoring pays no connect+handshake.
- **Citations validate/rehydrate against the FULL fetchLog**, so pruning to the top-K never invalidates citation indices (each entry keeps its `fetchIndex`).
- **Fail-open (daemon never on the correctness path):** if the score batch is unavailable, all pruned pages pass through unscored; if the daemon synth misses or fails validation after one reask, it falls back to the legacy session submit (which reconnects the RT socket first). Force the legacy path with `EXPLORE_WS_DAEMON=0`.
- **Knobs:** `EXPLORE_WS_DAEMON` (default on), `EXPLORE_WS_SCORER_MODEL` (`gpt-realtime-mini`), `EXPLORE_WS_SYNTH_MODEL` (`gpt-realtime-2`), `EXPLORE_WS_SCORE_MIN` (floor 4 = the rubric's "useful supporting context" band and up), `EXPLORE_WS_SYNTH_MAX_SOURCES` (24), `EXPLORE_WS_SCORE_EXCERPT_MAX` (1200). The daemon pool, namespace, and process-pool design are shared with search and the judge (see "Warm daemon pool"); websearch uses the `"websearch"` namespace so its sockets never collide.

**Realtime `web_search`:** Not supported on gpt-realtime-2 Realtime WS. Server rejects `{ type: "web_search" }` with `Supported values are: 'function' and 'mcp'.` Use `web_run` function tool bridged to Codex `alpha/search` instead. (Probe retired to `scripts/archive/probe-rt-websearch.sh`.)

## Code search transport

`search.sh` -> `search-rt.mjs` drives gpt-realtime-2 (Codex OAuth Realtime WS) with an agentic ripgrep loop (`search-lib.mjs`). Speed defaults:

- **Socket warm overlaps map generation:** `search-rt.mjs` calls `callModel.warm()` before `generateMapText` so the ~330ms connect+prewarm leaves the turn-1 critical path.
- **`tool_choice: "required"` every turn:** `finish` is always in the toolset, so required is always satisfiable; it prevents plain-text non-answers that cost a nudge round-trip.
- **RT-only prompt addendum** (`RT_SEARCH_ADDENDUM` in `realtime-search.mjs`): suppress narration, batch grep/glob then reads, target 2-3 turns. Kept separate from the shared `SYSTEM_PROMPT` (which stays model-agnostic in `search-lib.mjs`).
- **grep_search is find + hydrate.** `enrichGrepLines` (`ast-context.mjs`) appends an `--- ast context ---` section to every grep result: the enclosing function/class per hit (AST via ast-grep) or a clamped line window fallback (`EXPLORE_GREP_HYDRATE_PAD`=8, `EXPLORE_GREP_HYDRATE_MAX_SPAN`=60), deduped per file. The model usually cites finish ranges from this without a separate `read`. Raw matches are capped to `MAX_OUTPUT_LINES` BEFORE hydration so the context survives truncation. Benefits both engines (shared `TOOL_SPECS`).
- **Definition seeding (`search-seed.mjs`, RT path only).** Before turn 1, host-side grep the query's code symbols, pick the best DEFINITION per file, hydrate the enclosing node, and inject `<seed_hits>` into the initial state so the model can finish without a discovery turn. Symbol queries land in 1-2 turns (~1.6-1.9s on kepler). **Symbols only** (snake_case/camelCase/SCREAMING_CASE): a prose-noun pass was tried and removed because generic words grep into unrelated files in large polyglot repos and bad seeds hurt more than none. **Confidence-gated**: a symbol seeds only when one file is the clear top-scoring definition; ties (e.g. a generator that embeds the same source as a string) abstain and leave it to the model's grep loop. Pure-prose queries are a clean no-op (no regression). Host work overlaps the socket warm (~250ms, off critical path). Disable with `EXPLORE_SEARCH_SEED=0`; tune with `EXPLORE_SEARCH_SEED_MAX` (6), `EXPLORE_SEARCH_SEED_BEFORE` (10), `EXPLORE_SEARCH_SEED_AFTER` (30). Injected only via `search-rt.mjs` (host-side seeding, model-agnostic).
- **Reasoning effort default `minimal`.** With forced tool use and grep hydration, the model no longer wanders; it batches and converges in 2-4 turns. `EXPLORE_SEARCH_REASONING_EFFORT` remains an override.
- Parallel tool calls are already on (`parallel_tool_calls` in `realtime-search.mjs`); the loop fires 3-6 calls/turn.

## Fast search path (host retrieve + parallel span scoring)

`search-rt.mjs` tries `runFastPath` (`search-fast.mjs`) BEFORE the agentic loop; on `null` (empty pool, daemon unavailable, or nothing cleared the score floor) it falls back to `runSearch` for quality safety. The fast path replaces the multi-turn explore loop with host pre-processing + ONE parallel scoring wave, landing symbol queries ~0.6s and prose ~1.3s (warm pool).

Pipeline:
1. **One combined ripgrep** over the alternation of all query terms (`runCombinedRg`); classify def-vs-ref + rank files in node (`classifyHits` / `scoreCandidates`). Scoring is IDF-style: rare query terms (low document frequency) outweigh common ones, central dirs score up, generators/vendor down.
2. **Multi-span AST hydration** via the shared `hydrateHitsToBlocks` (`ast-context.mjs`, the same proven path as grep `enrichGrepLines`): each hit expands to its enclosing AST node (cAST: never split mid-function), deduped per file, comment-stripped, clamped. Emits MULTIPLE spans per file (default 6) so a scattered answer (a helper + the core fn far apart) is each captured as its own candidate. Hits are hydrated in rarity-weighted order so the most distinctive matches win the per-file span budget.
3. **Parallel relevance scoring**: one tiny 0-10 call PER SPAN, fanned out across the warm daemon process-pool (`daemonAskBatch`). The score prompt is an explicit 0-10 rubric (`SCORE_INSTRUCTIONS`) written for a no-reasoning model: no ambiguity, judge the window on its own merit.
4. **Coalesce** (`EXPLORE_SEARCH_FINISH_MODE`): `score` (A1, default) keeps spans >= floor, groups by file, merges ranges (`path:a-b,c-d`), orders by best span score, no model turn. `coalesce` (A2) adds one finish turn over survivors. A/B showed A1 wins on quality AND latency; A2 is debug-only.

Tuning: `EXPLORE_SEARCH_FAST=0` (disable), `EXPLORE_SEARCH_FAST_MAX_FILES` (12), `EXPLORE_SEARCH_FAST_MAX_SPANS` (32), `EXPLORE_SEARCH_FAST_SPANS_PER_FILE` (6), `EXPLORE_SEARCH_FAST_HYDRATE_SPAN` (40), `EXPLORE_SEARCH_SCORE_MIN` (floor 4 = the rubric's "related supporting code" band and up; a single span rarely scores the rubric's "definitive answer" for distributed prose answers, so 6 starved prose queries), `EXPLORE_SEARCH_FINISH_MAX` (6 files).

## Warm daemon pool (shared with judge)

Scoring runs against a pool of always-warm Realtime sockets so no call pays connect/prewarm. `scripts/gate/realtime_daemon.py` (generalized from the judge daemon) holds persistent gpt-realtime sockets; `lib/daemon-client.mjs` is the node IPC client.

- **Process pool, not thread pool.** One Realtime session serializes responses and the Python GIL serializes a thread pool, so the client spawns N single-worker daemon processes (default `POOL_SIZE`=8, matching the ~8 concurrent OpenAI account ceiling) on sockets `searchd/<key>-<slot>.sock`. `daemonAskBatch` spreads requests `i % POOL` across slots; measured ~16 calls in ~1.1s flat. One socket can run ~128 concurrent OOB responses (throughput, used by judge) but its latency grows with N; the process pool keeps latency flat.
- **Event-driven, eager-warm, fail-open.** Workers wake on a socketpair self-pipe (no polling), warm all sockets at startup, idle-shutdown, and reconnect + refresh tokens on drop. Daemon unavailable -> client returns null -> fast path falls back to the in-process warm call, then the agentic loop. At pool=1 the daemon is byte-identical to the judge.
- **Scorer model knob.** `EXPLORE_SEARCH_SCORER_MODEL` (default `gpt-realtime-2`). `gpt-realtime-mini` is opt-in: 2x TPS, 32k context, function calling, but **rejects the `reasoning` option** ("Unsupported option for this model") so `_realtime_reasoning_config` omits `reasoning` for mini (and for effort `none`/`off`). Sockets are namespaced by model; the client spawns the daemon with `UNIFABLE_JUDGE_MODEL=<scorer>`. mini scores ~2.3x faster (N=8 0.63s vs 1.42s) at comparable quality.

## Realtime model throughput (WSS, measured)

Do **not** send the `priority` service tier on Realtime requests — it measurably SLOWS them (lower TPS, higher or equal TTFT in every row below). Leave the tier at default. The Realtime search/trace/judge/daemon paths already send no `priority`/`service_tier`; keep it that way. (The only `service_tier: "priority"` left is `lib/codex-responses-client.mjs`, a Codex Responses HTTP path that is out of scope for search/trace.)

Reasoning `omit` (no `reasoning` field) beats `minimal` on both TPS and TTFT. `gpt-realtime-mini` is roughly 2x the TPS of the full models. Measured WSS TPS (default / priority) and TTFT (default / priority):

| Model | Reasoning | WSS TPS default / priority | WSS TTFT default / priority |
|---|---|---|---|
| `gpt-realtime-1.5` | omit | 113.4 / 92.8 | 269 / 329 ms |
| `gpt-realtime` | omit | 115.9 / 107.5 | 288 / 217 ms |
| `gpt-realtime-2` | omit | 121.3 / 112.0 | 513 / 511 ms |
| `gpt-realtime-2` | minimal | 107.5 / 110.0 | 445 / 461 ms |
| `gpt-realtime-mini` | omit | 219.3 / 176.7 | 308 / 185 ms |



All gpt-realtime-2 function-call loops force `tool_choice: "required"` every turn and carry the prompt line "Do not narrate steps or tool calls. Perform all searching/reading silently." `finish`/`submit` tools are always in the toolset so required is always satisfiable, and it eliminates plain-text non-answer turns. Applies to: search (`realtime-search.mjs`), trace explore phase (`realtime-trace.mjs`, gated by `EXPLORE_RT_EXPLORE_TOOL_REQUIRED`, default on), and submit phases (`askStructured`, already required).

## Trace transports

| Script | Model / API | Auth |
|---|---|---|
| `trace.sh` | **Default:** gpt-realtime-2 via `trace-rt.sh` (explore `low` / submit `minimal`) | Codex OAuth (`codex login` → `~/.codex/auth.json`) |
| `trace-rt.sh` | **gpt-realtime-2** via OpenAI **Realtime WebSocket** (direct entry; env overrides) | Codex OAuth |

Superseded trace variants (`trace-gemini.sh` Gemini CLI, `trace-gk.sh` Grok, `trace-cursor.sh` cursor-agent) are retired under `scripts/archive/`.

## gpt-realtime-2 is the target for trace

**gpt-realtime-2** (OpenAI Realtime WebSocket) is the target model/API for deep codebase trace in this skill. Default entry: `trace.sh` → `trace-rt.sh` / `realtime-trace.mjs`.

- Explore phase: default mode `nav` (host-driven micro-agent), with `agentic` (legacy `explore_exec` Realtime loop, reasoning `low`) as the override and the automatic fail-open.
- Submit phase: Realtime structured output (`askStructured` / function call JSON) with host pointer rehydration, synthesized over the warm daemon pool by default; default reasoning effort `minimal` on the session fallback, omitted on the daemon path.
- **Do not** add Codex Responses HTTP (`chatgpt.com/backend-api/codex/responses`) or other Responses-API submit paths here — they are out of scope and were removed after bench showed no win over Realtime submit.

## Trace fast path (nav explore + daemon submit)

A/B-decided defaults on the kepler precision set (`scripts/bench/trace-ab.sh`; results + tradeoffs in `docs/benchmarks/trace-fast.md`). Do not change these without a new benchmark run.

- **Explore = `nav` (`EXPLORE_RT_EXPLORE_MODE`, default).** `lib/rt-explore-nav.mjs`: host seeds the read cache with the search-fast retriever (`retrieveCandidates` — one combined rg → classify/score → AST-hydrate, pinned), then fans out **8 parallel `gpt-realtime-mini` navigators** (`EXPLORE_RT_NAV_COUNT=8`, `EXPLORE_RT_NAV_ROUNDS=1`) over the warm pool. Each navigator (distinct facet framing) returns `grep_terms` + `read_paths`; the host greps (one combined rg) and reads (`toolReadRange`, confined + preamble-stripped), unions/dedups by path+range, and writes into the same `readCache` the submit phase consumes. mini never reads files itself. Breadth in one round beat depth (4×2/6×2/8×2) on the bench.
- **Submit = daemon pool, full `gpt-realtime-2`, reasoning omitted** (`runDaemonPointerSubmit` → `daemonAsk`, `EXPLORE_RT_DAEMON=1`, `EXPLORE_RT_SYNTH_MODEL=gpt-realtime-2`). Reuses the pointer rehydrate + `validateTraceObject` + one-reask path; a daemon miss or post-reask invalid result falls back to the live-session `runSubmitPhase`. A **mini synth collapsed quality** (bench score 3 vs 5-6), so the synth model stays full; submit generation is the dominant cost in every mode.
- **Three-tier fail-open:** nav → (daemon unavailable) agentic `explore_exec` loop; daemon submit → live-session submit → agentic submit. The daemon is never on the correctness path. `hybrid` mode adds a one-turn agentic top-up on thin coverage (override; added latency without a quality win on the bench).
- **Pool warming:** `realtime-trace.mjs` warms the synth pool (and the nav-model pool when it differs) concurrently with connect + explore, so neither batch pays a connect+handshake. Shared pool/namespace design is in "Warm daemon pool"; trace uses the `"trace"` namespace.
- **Knobs:** `EXPLORE_RT_EXPLORE_MODE` (`nav`|`agentic`|`hybrid`), `EXPLORE_RT_NAV_MODEL` (`gpt-realtime-mini`), `EXPLORE_RT_NAV_COUNT` (8), `EXPLORE_RT_NAV_ROUNDS` (1), `EXPLORE_RT_DAEMON` (1), `EXPLORE_RT_SYNTH_MODEL` (`gpt-realtime-2`), `EXPLORE_RT_NAV_SEED_SPANS` (12), `EXPLORE_RT_NAV_ROUND_SPANS` (8).

Optional submit overrides (debug only): `EXPLORE_RT_SUBMIT_TRANSPORT=rt|wire-rt` (default `rt`).

## Realtime WebSocket agent-loop conventions

- **Hot socket:** keep one Realtime connection across tool rounds; prune with `conversation.item.delete` by default (`EXPLORE_*_SUBMIT_FRESH_CONTEXT=delete` or unset).
- **Reconnect:** set `EXPLORE_*_SUBMIT_FRESH_CONTEXT=reconnect` to close and reopen between rounds (legacy websearch behavior).
- **Prewarm:** `session.update` sent immediately after connect (via `RtAgentSession.prewarm`).
- **Recovery:** one reconnect retry per tool turn on connection-closed errors (`RtAgentSession.ensureAlive`).
- Session helper: `scripts/lib/rt-agent-session.mjs`.

## Where to look

| Task | Location |
|---|---|
| Realtime trace pipeline | `scripts/realtime-trace.mjs` |
| Trace nav micro-agent (explore) | `scripts/lib/rt-explore-nav.mjs` |
| Daemon IPC client (shared) | `scripts/lib/daemon-client.mjs` |
| Trace A/B harness + benchmark | `scripts/bench/trace-ab.sh`, `docs/benchmarks/trace-fast.md` |
| Realtime client | `scripts/lib/realtime_client.mjs` |
| RT agent session (reuse/prewarm/reconnect) | `scripts/lib/rt-agent-session.mjs` |
| Pointer submit / rehydration | `scripts/lib/rt-rehydrate-submit.mjs`, `scripts/lib/rt-pick-passages.mjs` |
| Schema + validation | `scripts/lib/trace-schema.mjs` |
| Wrapper | `scripts/trace.sh` (default), `scripts/trace-rt.sh` (direct) |
| Tests | `scripts/test-trace-rt.sh`, `scripts/test-websearch-rt.sh` |
| Archived variants/benches/probes | `scripts/archive/` (retired, unmaintained) |

## Conventions

- NO EMOJIS in code, comments, logs, or docs.
- Winning optimizations become **code defaults**; env vars are overrides, not feature flags.
