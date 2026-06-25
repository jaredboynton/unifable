# Explore scripts - agent notes

## Scope

These rules apply to `scripts/` wrappers and top-level runtime modules. The
root `AGENTS.md` stays an index; keep script-specific details here.

## Supported Entrypoints

- `search.sh` -> `search-rt.mjs` -> `search-lib.mjs` with
  `realtime-search.mjs` for fast in-repo code location.
- `trace.sh` -> `trace-rt.sh` -> `realtime-trace.mjs` for deep codebase
  behavior questions with grounded code citations.
- `websearch.sh` -> `websearch-rt.sh` -> `realtime-websearch.mjs` for external
  research through the Codex alpha/search `web_run` bridge.
- `map.sh` and `map*.mjs` are repo prefetch helpers used by trace/search flows.

Retired cursor, Gemini, Grok, Exa, Cerebras, probe, and old benchmark paths stay
under `scripts/archive/`. Do not route new supported behavior through archived
files.

## Shell Wrapper Rules

- Wrappers that need local configuration SHOULD source `env.sh` and call
  `explore_load_skill_env "$SKILL_DIR"` before preflight checks.
- Shell environment values win over values loaded from the skill-local `.env`;
  never write or document actual secret values in memory files.
- `trace-rt.sh` owns run-state layout: isolated run directories under
  `EXPLORE_RUNS_DIR` or the default cache, optional `EXPLORE_OUT`, explicit
  `EXPLORE_RUN_ID`, and absolute-path-only state overrides.
- Control flags are not accepted after the quoted trace/websearch prompt; keep
  that guard when changing wrapper argument parsing.

## Realtime Defaults

- The supported live paths target `gpt-realtime-2`.
- Force tool use in Realtime agent loops when a finish or submit tool is present;
  this prevents narration-only turns.
- Keep Realtime sockets hot across tool rounds and prune conversation items with
  `conversation.item.delete` unless a debug path explicitly needs reconnects.
- Search and trace should prefer low/minimal reasoning settings that existing
  wrappers already set; raise them only for a measured reason.

## Warm Daemon Pool (search, websearch, trace)

- Search, websearch, and trace all reuse the shared warm-socket daemon pool via
  `lib/daemon-client.mjs` (process pool, default 4 slots; see `scripts/gate/
  realtime_daemon.py`). The daemon is one-shot per request and never on the
  correctness path: every helper fails open (returns null) so the caller falls
  back to a live-session turn and then the agentic loop.
- Pool size 4 and per-socket in-flight 128 are MEASURED, not folklore
  (`docs/benchmarks/realtime-concurrency.md`): a 4-socket pool beat 8 and 16 on a
  16-wide fan-out (both models), and there is no account concurrent-session cap
  (32/32 sockets connected). Re-run `scripts/gate/bench_realtime_concurrency.py`
  before changing either cap; do not re-confirm what that doc already records.
- Warm the pool concurrently with other startup work (`warmDaemonPool`); the
  first cold call pays a one-time daemon spawn (~7-8s) that is not
  representative of warm latency.
- `gpt-realtime-mini` is the latency tier (parallel scoring/navigation, ~2x TPS,
  no reasoning option); reserve full `gpt-realtime-2` for synthesis/submit where
  quality dominates. Models are namespaced into separate socket pools.

## Trace Fast Path (nav + daemon submit)

- Default explore mode is `nav` (`EXPLORE_RT_EXPLORE_MODE`): host seeds with the
  search-fast retriever, then fans out 8 parallel `gpt-realtime-mini` navigators
  (`lib/rt-explore-nav.mjs`) that propose grep terms / read paths; the host
  hydrates and coalesces. `agentic` (legacy `explore_exec` loop) is the override
  and the automatic fail-open; `hybrid` adds a one-turn agentic top-up.
- Submit synthesizes over the daemon pool (`runDaemonPointerSubmit`, full
  `gpt-realtime-2`, reasoning omitted) and reuses the pointer rehydrate +
  validate + reask path; a miss/invalid result falls back to the live-session
  `runSubmitPhase`. Submit generation is the dominant cost.
- These defaults were chosen by `scripts/bench/trace-ab.sh` on the kepler
  precision set; do not change them without a new benchmark run. See
  `docs/benchmarks/trace-fast.md`.

## Search and Hydration Rules

- Treat `grep_search` as find plus hydrate: raw matches should be paired with
  AST-aware context before asking the model to choose final ranges.
- When changing `search-fast.mjs`, do not choose a single early definition as
  the hydration anchor. Find the densest cluster across all query hit lines,
  pass the cluster range into `expandLineRange`, then clamp the hydrated window.
- Prefer a definition only when it falls inside the densest query-hit cluster.
  Otherwise use the cluster center/range so unrelated file headers and early
  definitions do not steal the window.
- Keep symbol seeding confidence-gated. Prose nouns should not seed by default;
  broad words produce unrelated hits in large polyglot repos.

## Websearch Rules

- Alpha `swarm` is the supported fetch mode. Do not revive `fanout`,
  `deepen`, `search-open`, `search-only`, `combined`, Exa, or Gemini paths.
- Realtime `web_search` is not supported on `gpt-realtime-2`; use the local
  `web_run` function bridge to Codex alpha/search.
- Keep alpha output token caps high enough for multi-page fetches, and keep
  citation coverage mandatory for every on-topic fetched source.

## Verification

- Wrapper/preflight changes: run `scripts/setup.sh` or the nearest wrapper smoke.
- Shell wrapper changes: run `bash -n` on the edited wrapper and any touched
  shell tests; use stubbed auth/keychain tests for archived Cursor/ACP paths.
- Search changes: run `scripts/test-search.sh` or targeted Node tests under
  `scripts/test/`.
- Trace changes: run `scripts/test-trace-rt.sh` or the targeted
  `scripts/test/*trace*.test.mjs`. To re-validate trace fast-path defaults,
  run the live A/B harness `scripts/bench/trace-ab.sh` (needs Codex auth +
  `~/__devlocal/kepler`).
- Websearch changes: run `scripts/test-websearch-rt.sh` or the targeted
  websearch tests.
