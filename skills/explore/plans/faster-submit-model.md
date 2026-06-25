# Faster submit-model path for trace-rt

## STATUS: thesis REFUTED by throughput measurement (2026-06-25)

Measured, both axes (`submit_ms ≈ TTFT + output_tokens / TPS`):

| Model | TTFT | TPS | per-token | submit_ms @100 tok | @250 tok |
|---|---|---|---|---|---|
| GPT Realtime 2 | 1046ms | 135.4 | 7.39ms | 1785ms | 2892ms |
| GPT 5.4 | 1220ms | 71.7 | 13.95ms | 2615ms | 4707ms |
| GPT 5.5 | 2281ms | 66.2 | 15.11ms | 3792ms | 6058ms |

gpt-realtime-2 has the **lower intercept (TTFT) AND the lower slope (per-token)**, so it is fastest for
all output sizes N ≥ 0 — the lines never cross in the positive domain. The break-even TTFT advantage the
GPT-5.x path would have needed does not exist; GPT-5.5's TTFT is actually 1235ms *worse*. **Do not route
submit to GPT-5.x. No A/B benchmark can rescue it — the measurement is dispositive.** gpt-realtime-2 is
the right submit model. The remaining generation lever is therefore *fewer output tokens / fewer reasks*
(shrink schema, keep pointer mode), not a faster model. The plan below is retained as the investigation
record; its routing recommendation is withdrawn.

## Thesis (withdrawn — see STATUS above)

Transport is provably tapped out (WS-mode already implemented; overlap savings = ~3ms noise,
`plans/websocket-mode-adaptation.md`). The remaining latency lever is the **generation axis**:
the structured `submit_trace` phase. Today both phases of a trace run on one realtime session
(`gpt-realtime-2`), so the submit JSON is emitted by a model tuned for low-latency *audio*
streaming, not one-shot structured text. Routing the submit phase to a faster text model
(GPT-5.5) is the next improvement.

The decisive enabler: **the submit phase emits only pointers, not code.** In pointer mode the
model returns `citation_spans` (`excerpt_index` + line ranges + rationale); the host rehydrates
verbatim code and re-validates groundedness deterministically
(`rt-rehydrate-submit.mjs:75 rehydratePointerSubmit`, gated by `validateTraceObject` at
`realtime-trace.mjs:580`). The submit model therefore never authors code, so swapping it
**cannot** regress the 1.00 groundedness ratio — that property is owned by the host, not the model.

## Current architecture (verified)

| Concern | Location | Note |
|---|---|---|
| Session model (explore + submit) | `realtime-trace.mjs:877` | `--model` / `EXPLORE_RT_MODEL` default `gpt-realtime-2` |
| Submit transport selector | `realtime-trace.mjs:157-161` | `EXPLORE_RT_SUBMIT_TRANSPORT` ∈ {`rt`,`cerebras`,`wire-rt`}, default `rt` |
| Submit branch dispatch | `realtime-trace.mjs:522,716` | `cerebras` → off-realtime; else `askStructured(conn,...)` on realtime |
| Submit reasoning effort | `realtime-trace.mjs:542,613` | `EXPLORE_RT_SUBMIT_REASONING_EFFORT` |
| Off-realtime submit precedent | `lib/rt-submit-cerebras.mjs:22` | OpenAI-compatible `/chat/completions`, `json_schema` strict, retries+timeout |
| Per-phase timing (already there) | `realtime-trace.mjs:737,757,830` | `phase submit_ms=...`; also `explore_ms`, `connect_ms` |
| Codex Responses body builder | `lib/codex-responses-client.mjs:126` | `model`, `reasoning.effort`, `service_tier: priority`, ChatGPT auth |
| GPT-5.5 availability signal | `lib/codex-alpha-search-client.mjs:77-80` | gpt-5.4/5.5 output caps already referenced in-tree |

Key consequence: **a generic off-realtime submit seam already exists** (`cerebras` branch). Adding
a GPT-5.5 path is extending that seam, not new architecture. And `submit_ms` is already logged, so
the A/B metric needs zero new instrumentation.

## The seam to extend

`submitTransport()` already returns a transport string and the submit loop already branches on it.
Add a `responses` value and a sibling module mirroring `rt-submit-cerebras.mjs`.

### Backend choice (recommendation: Responses/codex primary)

- **Primary — codex Responses API (GPT-5.5).** Reuses the ChatGPT auth already wired
  (`codex-responses-client.mjs loadChatgptAuth`), and GPT-5.5 is referenced in-tree. Structured
  output via a single forced function tool (`tool_choice` pinned to the submit schema), same
  contract `askStructured` uses on the realtime side. New module `lib/rt-submit-responses.mjs`.
- **Fallback — OpenAI-compatible `/chat/completions`.** If a plain GPT-5.5 key is preferred, the
  `rt-submit-cerebras.mjs` shape works as-is by changing base URL + model; lowest-effort but needs
  a separate key. Use only if the codex Responses path is unavailable in a given run (e.g. headless).

## Configuration knobs (all opt-in; default unchanged)

| Env / flag | Default | Effect |
|---|---|---|
| `EXPLORE_RT_SUBMIT_TRANSPORT=responses` | `rt` | route submit to GPT-5.5 path |
| `EXPLORE_RT_SUBMIT_MODEL` | `gpt-5.5` | submit model id (decoupled from explore `EXPLORE_RT_MODEL`) |
| `EXPLORE_RT_SUBMIT_MODEL_EFFORT` | `low` | reasoning effort for the submit call |
| `EXPLORE_RT_SUBMIT_MODEL_TIMEOUT_MS` | `15000` | mirror cerebras timeout |
| `EXPLORE_RT_SUBMIT_MODEL_RETRIES` | `3` | mirror cerebras retry policy |

Default `rt` means zero behavior change until the benchmark gate (below) clears.

## Routing rules

1. `submitTransport()` gains `responses` (extend the allowlist at `realtime-trace.mjs:159`).
2. In the submit loop (`realtime-trace.mjs:521`), add `else if (transport === "responses")` →
   `submitTraceViaResponses({ system: submitSystem, user: userText, schema, schemaName,
   hostPassages: useHostPassages || usePointerIndex, question, filesRead:[...filesRead], slim })`
   — identical call surface to `submitTraceViaCerebras` so pointer/prose/full modes flow unchanged.
3. **Pointer/prose modes only** for the swapped model. The host-rehydration branches
   (`rehydratePointerSubmit`, `mergeProseWithPassages`) are what make the swap groundedness-safe;
   full `submit_trace` (model-authored passages) should stay on `rt`. Enforce: if transport=responses
   and neither pointer nor host-passages mode is active, log a warning and fall back to `rt`.
4. The reask loop (attempt 0/1 with `VALIDATION FAILED` feedback, `realtime-trace.mjs:545,581`) wraps
   the new branch unchanged — same retry semantics on validation failure.
5. Explore phase is untouched; it still runs on the realtime session. Only the submit call reroutes.
   Handoff into submit is already serialized state (`filesRead`, `readCache`, `orderedPaths`,
   `userText`/READ INDEX) — no realtime-session coupling for submit.

## Safety / compatibility constraints

- **Groundedness invariant preserved by construction.** Submit model emits pointers; host fills
  verbatim code and `validateTraceObject`/`validateTraceWire` gate the result. Swap cannot lower
  groundedness — if the model emits a bad pointer, validation rejects and reasks, same as today.
- **Schema strictness.** Responses path must force exactly one call to the submit function
  (`tool_choice` pinned), matching the realtime `askStructured` contract. Reject/reask on multiple
  or zero calls.
- **Deadline honored.** New branch must respect `deadlineMs` (the loop already checks it at
  `realtime-trace.mjs:518`); module-level timeout mirrors cerebras `15s`.
- **Auth availability.** Responses path depends on ChatGPT auth; in headless/cron contexts where
  that's absent, fall back to `rt`. Detect once and log.
- **No new caps.** If the swap is active but unavailable, log the fallback explicitly (silent
  fallback would read as "responses path benchmarked" when it didn't run).
- **Reasoning content.** Keep `reasoning_format` hidden / `reasoning.encrypted_content` excluded
  from the trace output — submit returns only the schema object.

## Benchmark methodology

Goal: isolate the submit model as the only variable and prove a real `submit_ms` win with no
quality/groundedness regression.

1. **Hold the explore phase constant.** Capture one explore transcript per question
   (`filesRead`/`readCache`/`orderedPaths` + the built READ INDEX `userText`), then replay the
   *same* submit input into both transports. This removes explore-phase variance so the delta is
   pure submit-model latency. (If replay harness isn't ready, run matched pairs back-to-back and
   report variance.)
2. **Matched question set.** Reuse the existing bench harness and question bank under
   `benchmarks/YYYY-MM-DD-trace-rt-vN/` with `structured.json` sidecars; N ≥ the v9 set for
   comparability.
3. **Metrics (per question, both arms):**
   - `submit_ms` — primary, already logged (`realtime-trace.mjs:737,757,830`).
   - end-to-end wall-clock ms.
   - `quality_index` (bench-trace-scorer) — must not regress.
   - groundedness ratio — must stay 1.00.
   - validation reask rate — must not increase.
4. **Arms:** A = `EXPLORE_RT_SUBMIT_TRANSPORT=rt` (gpt-realtime-2, control);
   B = `responses` (gpt-5.5, effort=low). Optionally C = `cerebras` as a third reference point.
5. **Report:** median + p90 `submit_ms` per arm, paired delta, plus the quality/groundedness/reask
   columns. Store under `benchmarks/2026-06-25-trace-rt-submit-model/` with a README table.

## Phased rollout

| Phase | Work | Gate |
|---|---|---|
| 0 | PoC: one question, `responses` arm, confirm GPT-5.5 emits valid pointer schema + host rehydrates + validates 1.00 | pointer schema round-trips; groundedness 1.00 |
| 1 | Implement `lib/rt-submit-responses.mjs` (mirror cerebras), extend `submitTransport()` + dispatch branch, add unit test (mock the HTTP/Responses call → fixed pointer payload → assert rehydrate+validate) | unit test green; full suite still 155+ pass |
| 2 | A/B benchmark per methodology above | median `submit_ms` drops materially **and** quality_index flat-or-up **and** groundedness 1.00 **and** reask rate flat |
| 3 | Decision: flip default to `responses` only if Phase-2 gate clears; otherwise keep `rt` default and document the knob | — |

## Decision gate (ship criteria)

Ship the default flip **iff** all hold on the matched set:
- median `submit_ms(responses)` < `submit_ms(rt)` by a margin above run-to-run noise (define from
  control variance, not a guessed threshold).
- `quality_index` not lower than control.
- groundedness ratio = 1.00.
- validation reask rate not higher than control.

If any fails: keep `rt` default, leave `responses` as an opt-in knob, record the numbers.

## Open questions to close before Phase 1

1. Is GPT-5.5 reachable via the codex Responses backend with current ChatGPT auth, or does it need a
   separate key? (Probe `codex-responses-client.mjs` against `gpt-5.5` with a trivial forced-tool call.)
2. Does the Responses API enforce `strict` json-schema for forced function tools the same way
   chat/completions `json_schema` does, or is post-parse validation needed? (PoC determines.)
3. Replay harness: does a captured explore transcript already deserialize cleanly into the submit
   input, or is a small adapter needed to rebuild `readCache`/`orderedPaths`?
