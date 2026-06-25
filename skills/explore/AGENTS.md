# Explore skill — agent notes

## Websearch transports

| Script | Model / API | Auth |
|---|---|---|
| `websearch.sh` | **Default:** gpt-realtime-2 via `websearch-rt.sh` (2 rounds: web_run/alpha + pointer submit; search `low`, submit `minimal`) | Codex OAuth + `curl` |
| `websearch-rt.sh` | **gpt-realtime-2** Realtime WebSocket + Codex `alpha/search` via `web_run` (default) | Codex OAuth + `curl` |

**Retired (archived under `scripts/archive/`, do not use):** the `EXPLORE_WS_BACKEND=exa` RT backend, the `ensemble` (F3 multi-backend) fetch mode, and `websearch-gemini.sh` (Gemini + Exa-MCP path) are retired — the native alpha `swarm` arm beats them on judged quality and breadth. Alpha is the only supported RT websearch backend.

**Alpha fetch mode:** `swarm` is the sole mode — it fires fanout source-class strategies + deepen aspect facets as one concurrent host-side search wave, then a single ranked open pass (deepest pool, ~45 URLs, at ~20s; `docs/benchmarks/websearch-swarm.md`). The older `fanout` / `deepen` / `search-open` / `search-only` / `combined` modes are retired. Swarm runs host-side over HTTP, so the pipeline reconnects the RT socket before submit (the socket idle-closes during host search; see `rt-agent-session.mjs`). Tune open breadth with `EXPLORE_WS_SWARM_OPEN_CAP` (default 18).

**Alpha defaults (speed):** one coalesced `alpha/search` batch per RT turn (`EXPLORE_WS_STOP_SEARCHES=1`, `EXPLORE_WS_COALESCE_WEB_RUN=1`), `128000` max output tokens (the model cap; small caps truncate multi-page fetches), query-matched per-URL excerpts for submit, mandatory citation coverage (cite every on-topic fetched source).

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

## Forced tool use on gpt-realtime-2 (anti-narration)

All gpt-realtime-2 function-call loops force `tool_choice: "required"` every turn and carry the prompt line "Do not narrate steps or tool calls. Perform all searching/reading silently." `finish`/`submit` tools are always in the toolset so required is always satisfiable, and it eliminates plain-text non-answer turns. Applies to: search (`realtime-search.mjs`), trace explore phase (`realtime-trace.mjs`, gated by `EXPLORE_RT_EXPLORE_TOOL_REQUIRED`, default on), and submit phases (`askStructured`, already required).

## Trace transports

| Script | Model / API | Auth |
|---|---|---|
| `trace.sh` | **Default:** gpt-realtime-2 via `trace-rt.sh` (explore `low` / submit `minimal`) | Codex OAuth (`codex login` → `~/.codex/auth.json`) |
| `trace-rt.sh` | **gpt-realtime-2** via OpenAI **Realtime WebSocket** (direct entry; env overrides) | Codex OAuth |

Superseded trace variants (`trace-gemini.sh` Gemini CLI, `trace-gk.sh` Grok, `trace-cursor.sh` cursor-agent) are retired under `scripts/archive/`.

## gpt-realtime-2 is the target for trace

**gpt-realtime-2** (OpenAI Realtime WebSocket) is the target model/API for deep codebase trace in this skill. Default entry: `trace.sh` → `trace-rt.sh` / `realtime-trace.mjs`.

- Explore phase: Realtime tools (`explore_exec`); default reasoning effort `low`.
- Submit phase: Realtime structured output (`askStructured` / function call JSON), with host pointer rehydration by default; default reasoning effort `minimal`.
- **Do not** add Codex Responses HTTP (`chatgpt.com/backend-api/codex/responses`) or other Responses-API submit paths here — they are out of scope and were removed after bench showed no win over Realtime submit.

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
