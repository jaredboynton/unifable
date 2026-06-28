# WebSocket-mode adaptation spec — explore trace

Source material (read 2026-06-25):
- Engineering post: <https://openai.com/index/speeding-up-agentic-workflows-with-websockets/> (direct fetch 403'd; content recovered via web search of the same URL + secondary coverage).
- API guide: <https://developers.openai.com/api/docs/guides/websocket-mode>

> Line citations to `realtime-trace.mjs` and `rt-agent-session.mjs` are against the
> live working tree (the RtAgentSession transport refactor in flight, not yet on HEAD).
> Citations to `realtime_client.mjs`, `rt-session-utils.mjs`, `rt-tools.mjs`,
> `xai_client.mjs`, `trace-rt.sh`, `AGENTS.md` are committed and stable.
> Raw benchmark data: `benchmarks/2026-06-25-ws-mode-adaptation/` (local, gitignored).

## 1. What OpenAI WebSocket mode is

A persistent-connection transport **for the Responses API**, aimed at "long-running, tool-call-heavy workflows."

| Property | Value | Source |
|---|---|---|
| Endpoint | `wss://api.openai.com/v1/responses` | guide |
| Auth | `Authorization: Bearer {OPENAI_API_KEY}` | guide |
| Model | GPT-5.5 (ZDR / `store=false` compatible) | guide |
| Continue a turn | `response.create` with `previous_response_id` + only new `input` items | guide |
| State reuse | connection-local in-memory cache holds **one** previous-response state (most recent); follow-up `response.create` fetches it instead of rebuilding the conversation | post + guide |
| Warmup | `generate:false` primes state without producing output | guide |
| Limits | 60-min connection cap; no multiplexing (one in-flight response); sequential `response.create`; errors `previous_response_not_found`, `websocket_connection_limit_reached` | guide |
| Headline gain | "up to roughly 40% faster end-to-end" for rollouts with **20+ tool calls** (vs per-turn HTTP); Vercel ~40%, Cline 39%, Cursor up to 30% | post |

The gain is a **transport** gain: it removes (a) per-turn TCP/TLS/HTTP setup and (b) re-uploading the growing conversation each turn. It does **not** speed up model generation.

## 2. What explore's trace already does

`trace.sh` -> `trace-rt.sh` -> `realtime-trace.mjs` over the **Realtime API** WebSocket.

| Property | explore today | Citation |
|---|---|---|
| Endpoint | `api.openai.com/v1/realtime` (raw RFC6455 WS, zero-dep) | `scripts/lib/realtime_client.mjs:9-10` |
| Auth / model | Codex OAuth (`chatgpt-account-id` + Bearer) / `gpt-realtime-2` | `realtime_client.mjs:15,265-267` |
| One WS per trace | single `RealtimeConnection`, opened once | `rt-agent-session.mjs:20,30` |
| Reused across phases | explore **and** submit run on the same connection | `realtime-trace.mjs` (`runSubmitPhase(session.connection, …)`) |
| Incremental per-turn sends | each turn sends only the new `function_call_output` items + `response.create` — the transcript is never re-uploaded (server retains conversation state) | `rt-agent-session.mjs:211-222,154-160` |
| Turn collapsing | explore is one `explore_exec` call running many nested grep/read/shell in a single model turn | `realtime-trace.mjs` UNITRACE_SYSTEM, `scripts/lib/rt-explore-runtime.mjs` |
| Context pruning | explore items deleted **fire-and-forget** before submit (no serial round-trips) | `rt-session-utils.mjs:88-93` |
| Slim submit packet | submit sends a **pointer index** (path + 14-line preview), not re-sent full excerpts | `realtime-trace.mjs` (`UNITRACE_RT_SUBMIT_POINTER_INDEX`) |
| `previous_response_id` pattern | already used on the xAI Responses path | `scripts/lib/xai_client.mjs:131` |

## 3. Measured bottleneck (v9, real runs)

`benchmarks/2026-06-25-trace-rt-v9/*/err.log`:

| run | explore_ms | submit_ms | explore_turns | tool_calls |
|---|---|---|---|---|
| rt-1 | 5553 | 6612 | 1 | 1 |
| rt-2 | 3312 | 5619 | 1 | 1 |
| rt-3 | 3740 | 6287 | 1 | 1 |

Trace is **1 model turn / 1 tool call per phase**, and **submit (generation) is the larger half**. Wall-clock (~9-12s) is dominated by model generation, not transport.

## 4. Adaptable vs not

| WS-mode lever | Status in explore | Verdict |
|---|---|---|
| Persistent WS connection | Already present (Realtime WS) | **Already captured** |
| Reuse connection across turns/phases | Already present | **Already captured** |
| Don't re-upload transcript per turn | Already present (incremental items) | **Already captured** |
| Don't re-send content already seen | Already present (pointer-index submit) | **Already captured** |
| Minimize tool-call turns (the 20+ regime the 40% targets) | Already collapsed to 1 via `explore_exec` | **Already captured** |
| `wss://api.openai.com/v1/responses` + API key + GPT-5.5 | explore uses Codex OAuth + `gpt-realtime-2` + `/v1/realtime` | **Not adaptable** as a drop-in: different endpoint, auth, billing, and model. Would be a model swap, not a transport swap. |
| `previous_response_id` continuation cache | Realtime uses server-side conversation items instead; pattern already known on xAI path | **N/A** to Realtime; no benefit to add |
| Headline ~40% (vs per-turn HTTP, 20+ calls) | trace is persistent-WS + ~1 turn/phase | **Structurally unavailable** — the baseline it improves on doesn't exist here |

**Bottom line:** explore's trace already implements WS-mode's entire transport playbook on the Realtime API. The literal WS-mode endpoint is not a drop-in (auth/model/billing), and its measured win targets a regime (per-turn HTTP, 20+ tool calls) that explore does not operate in. The remaining latency is model generation, which WS-mode does not address.

## 5. Residual lever tested — and the before/after result

The one transport-shaped latency at process start is the **WS handshake** (~220ms, measured in isolation). The only non-trivial host work before the first model turn inside the process is `seedExploreReads` (`buildExploreToolSchemas` is a static return — `scripts/lib/rt-tools.mjs:22-24`); the expensive repo-map build runs upstream in `scripts/trace-rt.sh:340` *before* `node realtime-trace.mjs` launches (`:390`). So the only overlap available in-process is hiding the seed reads behind the handshake.

Implemented as `UNITRACE_RT_OVERLAP_SETUP` (run `connect()` concurrent with the seed reads) plus permanent `connect_ms`/`seed_ms` instrumentation, then benchmarked: `scripts/bench-ws-overlap.sh`, query "How does trace.sh work end to end?", 3 interleaved reps/arm against this workspace.

| arm | n(ok) | med wall | med connect_ms | med seed_ms | med explore_ms | med submit_ms | med quality | med grounded |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| before (overlap off) | 3/3 | 11.07s | 247 | 0 | 4135 | 6409 | 61 | 1.00 |
| after (overlap on) | 3/3 | 10.92s | 239 | **3** | 4780 | 6224 | 58 | 1.00 |

Per-run (all 6, `overlap-results.tsv`): generation (explore+submit) is **90–96% of wall** in every run; transport (connect+seed) is **1.9–2.9%**. The seed reads cost **~3ms**, so the overlap's ceiling is ~3ms — below the per-run wall variance (10.55–12.05s, driven entirely by `submit_ms`/`explore_ms`). The −0.15s median "win" is noise.

**Decision: not shipped.** A 3ms saving is not a winning optimization (repo convention: defaults are reserved for wins, not feature-flagged micro-changes — `AGENTS.md`). The `UNITRACE_RT_OVERLAP_SETUP` flag and `precomputedSeed` plumbing were reverted. The `connect_ms` phase metric was kept, but it lives in the **working-tree** `realtime-trace.mjs` and is **not yet on HEAD** — see §7.

## 6. Conclusion

Benchmarks confirm the trace is **generation-bound** (~95% of wall): there is no transport headroom for WS-mode to capture, and explore already implements WS-mode's transport playbook on the Realtime API. The only path to a materially faster trace is the model/generation axis (a faster submit model, fewer/smaller output tokens) — a model decision, not a transport one.

## 7. Handoff — uncommitted `connect_ms` metric in `realtime-trace.mjs`

For whoever owns the in-flight **RtAgentSession** transport refactor (untracked `scripts/lib/rt-agent-session.mjs` + `scripts/test/rt-agent-session.test.mjs`; the working-tree `realtime-trace.mjs` already imports `RtAgentSession` / `rt-session-utils`, while HEAD still has the inline `waitForResponse`).

This session added one small instrumentation hunk on top of your refactor, in `runStructuredTrace` (working-tree `realtime-trace.mjs`, ~5 lines):

```js
const connectStart = Date.now();
await session.connect();
const connectMs = Date.now() - connectStart;
// Handshake cost is ~2% of wall (benchmarked); kept as a phase metric so future
// tuning stays measurement-driven. The trace is generation-bound, not transport-bound.
toolLog.push(`phase connect_ms=${connectMs}`);
```

- It is **not separately committable**: it only applies on top of the RtAgentSession (`session.connect()`) structure, which is not on HEAD. So it was left in the working tree rather than committed under a parallel owner's refactor.
- **Action when your refactor lands:** keep these lines (the `connect_ms` phase metric is what grounds §3/§5 of this spec and is cheap/permanent), or drop them if undesired — nothing else depends on `connect_ms`.
- The reverted overlap experiment (`UNITRACE_RT_OVERLAP_SETUP`, `precomputedSeed`) is **already removed** from the working tree; only the `connect_ms` line above remains.
