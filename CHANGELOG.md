# Changelog

## 1.18.0 - 2026-06-27

- New global `unifusion` launcher on `~/.local/bin`. The SessionStart runtime-sync
  hook (`scripts/gate/runtime_sync.py`) and the `install/*.sh` tails now link a
  `unifusion` bootstrap (alongside `unifable`/`unifable-hook`/`unifable-spec`)
  that execs `~/.unifable/current/skills/unifusion/scripts/unifusion.sh`. The
  panel now runs as `unifusion <question_file>` from any cwd, whether or not the
  plugin is enabled in the current session, as long as the plugin has been
  installed once (so `~/.unifable/current` is seeded). Survives cache-version
  deletion like the other launchers.
- Retired the `unifable:setup` slash command and deleted `setup/setup.sh` +
  `setup/install-bin.sh`. The bin install is fully owned by the SessionStart hook
  (and seeded at install time by `install/claude.sh` / `install/codex.sh` via
  `runtime_sync.py --source <cache>`); the legacy `<!-- UNIFABLE -->` /
  `<!-- UNIFABLE-ORCH -->` / `<!-- FABLIZE -->` block-strip is now inlined into
  the installers. `progress.json` (write-only state) is dropped. `setup/uninstall.sh`
  remains and now also removes the `unifusion` link.
- Extracted the canonical Realtime transport out of `scripts/gate/codex_judge.py`
  into a new `unifable_runtime/transport/` package: `realtime_ws.py` (RFC 6455
  WebSocket client + Codex OAuth token lifecycle + connection lifecycle) and
  `realtime_session.py` (pure session helpers — response routing, reask
  classifiers, reasoning config, concurrent-batch frame router). `codex_judge.py`
  is now a gate adapter that composes both, owns the 256k message cap
  (`transcript_tail`) + env-driven constants + token-usage recording, keeps the
  public `ask_structured` API stable, re-exports the transport names so existing
  tests that monkeypatch `cj._ws_connect` / `cj._fresh_tokens` / `cj._encode_frame`
  keep working, and adds `ask_structured_batch`.
- Hermetic test suite. `UNIFABLE_JUDGE_OFFLINE=1` is now the default in
  `tests/conftest.py` and `scripts/run_tests.sh`, so the direct judge path fails
  open instead of making a live ~1.3s Realtime WebSocket call per hook dispatch —
  verdicts no longer silently depend on Codex credential presence.
  `scripts/gate/judge_transport.py` moved the offline check so the daemon
  spawn/connect (and its ~3s backoff) is skipped when offline. Judge-behavior
  tests inject a `judge_fn` or patch the transport seam and clear the knob.
- Pinned xdist to `-n 8` in `pytest.ini` (was `-n auto`): with 1367 fast tests,
  per-worker startup + IPC + CPU contention dominates beyond ~8 workers.
  Measured on a 16-core M4 Max: `-n 8` ~= 6.5s vs `-n auto` (16) ~= 12s.
- `scripts/bump_version.py`, `scripts/check_versions_consistent.py`,
  `scripts/commit.sh`, and the justfile no longer manage a `setup/setup.sh`
  version field. `scripts/gate/cli_install.py` UserPromptSubmit auto-heal now
  re-seeds via `runtime_sync.py` instead of `install-bin.sh`. `scripts/gate/grade_override.py`
  judge prompt updated ("installer release tail" replaces "setup.sh release tail").
- New `tests/test_realtime_ws_protocol.py` (RFC 6455 frame + JWT-expiry fixtures);
  `scripts/audit_waits.py` and `docs/testing-optimization.md` add it to the
  wait-audit covered set. Added `docs/posttool-async-dispatch-plan.md` (design
  doc; no code) and `docs/benchmarks/python-consolidation-*.txt` snapshots.

Verification:

- `python3 -m py_compile scripts/gate/runtime_sync.py scripts/gate/cli_install.py scripts/gate/codex_judge.py scripts/gate/judge_transport.py scripts/gate/grade_override.py scripts/bump_version.py scripts/check_versions_consistent.py unifable_runtime/transport/realtime_ws.py unifable_runtime/transport/realtime_session.py`
- `bash -n install/claude.sh install/codex.sh setup/uninstall.sh scripts/commit.sh tests/test_unifable_hook_dispatch.sh`
- `python3 -m pytest tests/test_runtime_sync.py tests/test_cli_install.py tests/test_janitor.py tests/test_bump_version.py tests/test_realtime_ws_protocol.py tests/test_codex_judge_reask.py tests/test_judge_message_cap.py -q`
- `just generated-docs` (regenerates `docs/generated/*.md`)
- `just test-all`

## 1.17.5 - 2026-06-27

- PostToolUse no longer disarms the groundedness breaker inline. The breaker LIFT
  (disarm) moved off the hot path into a detached worker
  (`scripts/gate/breaker_release_lane.py`): `gate_post_tool.py` dispatches it on a
  release tool while the breaker is armed, the worker runs the transcript release
  judge under the same `breaker_lock` as the foreground arm, persists the disarmed
  state, and enqueues the lift message (`db.breaker_release_*`) for the next
  PreToolUse (or Stop, on a text-only tail) to drain. A `breaker_release_bg` lease
  debounces one in-flight disarm per breaker per TTL so an edit burst cannot fork a
  process storm. Arming stays synchronous in PreToolUse (it must block a mutation),
  and PreToolUse re-runs the release judge on every armed call, so a slow or dead
  worker self-heals — the next gated tool disarms itself. This closes the prior
  race where the inline PostToolUse disarm wrote breaker state without the lock.
- The standalone test-after-edit PostToolUse hook is folded into `gate_post_tool.py`
  (`_test_after_edit_context`, self-gated on `UNIFABLE_TEST_AFTER_EDIT=1`). Both
  `hooks/hooks.json` and `.codex-plugin/hooks.json` now wire one PostToolUse entry
  instead of two, so the host spawns one PostToolUse process per tool call.

Verification:

- `python3 -m py_compile hooks/gate_post_tool.py hooks/pre_tool_use.py hooks/gate_stop.py scripts/gate/breaker_release_lane.py scripts/gate/db.py`
- `python3 -m pytest tests/test_breaker_release_lane.py tests/test_breaker_post_tool.py -q`
- `just test-all`

## 1.17.4 - 2026-06-28

- UserPromptSubmit consolidated from three hook processes to one. `router.sh`
  (pack routing) and `gate_prompt_effort.py` (effort-gated playbook) are now
  invoked in-process by `hooks/gate_prompt.py` via `_router_prefix()` and
  `_effort_suffix()`, which merge their output with the grade/enhance context
  into a single `additionalContext` emission (router -> grade/enhance -> effort
  order preserved). Both `hooks/hooks.json` and `.codex-plugin/hooks.json` now
  wire one UserPromptSubmit entry. The host spawns one process per prompt instead
  of three, cutting per-prompt shell-out and token overhead with identical
  output. `router.sh` and `gate_prompt_effort.py` remain as files (dispatch tests
  and runtime_sync still target them).

Verification:

- `python3 -m py_compile hooks/gate_prompt.py scripts/gate/pack_router.py`
- `python3 -m pytest tests/test_pack_router.py tests/test_effort_inject.py tests/test_grade_and_enhance.py -q`
- `python3 scripts/generate_docs.py --check`
- `just test-all`

## 1.17.3 - 2026-06-28

- SessionStart restate instruction now states the restate gate is a one-time
  step ("Do this ONLY ONCE, before any other tool call"), so the model stops
  re-running `unifable restate` off-script and clobbering a richer goal with a
  thinner restatement.
- Test guidance: `tests/AGENTS.md` now forbids asserting on exact prose/copy
  wording. Removed the brittle wording assertions from the SessionStart frame and
  redundant-restate tests, replacing them with structural and behavioral checks
  (the production redundant-detection regex, command tokens, line structure) so
  copy edits no longer break the suite.

Verification:

- `just version 1.17.3`; `python3 scripts/generate_docs.py --check`
- `just test-all`

## 1.17.2 - 2026-06-28

- Removed the STANDARD (`normal`) mode verification steering line from
  `context_for_mode`. It advertised "The Stop gate blocks completion until a
  verification has run for changed files," but the observation gate
  (`verify_state.should_block_stop`) only hard-blocks at HEAVY, so the line
  overstated enforcement on every STANDARD prompt and cost tokens each turn. The
  HEAVY (`deep`) verification guidance, which the gate actually backs, is
  unchanged.

Verification:

- `python3 tests/test_classify_ambiguity.py`; `python3 scripts/generate_docs.py`
- `just test-all`

## 1.17.1 - 2026-06-27

- Removed the model-facing `unifable contract` CLI subcommand. Contract strings
  remain internal hook helpers, while the CLI now exposes only spec-mutating
  commands (`restate`, `add-task`, `set-primary`, `add-frontier`).
- Added a redundant-restatement ack/steer: once the restate gate is already
  satisfied, a repeated `unifable restate` call now tells the model to stop
  restating and move to the next step instead of silently looking like fresh work.

Verification:

- `just version 1.17.1`; `just generated-docs` + `python3 scripts/generate_docs.py --check`
- `just test-all`

## 1.17.0 - 2026-06-27

- PostToolUse reconcile + frontier-discover judges are now fire-and-forget. These
  advisory board-maintenance judges gate nothing, so they no longer block the
  agent's next tool behind a gpt-realtime-2 round-trip on every evidence-changing
  tool. `gate_post_tool` now spawns `scripts/gate/posttool_background.py` detached
  (`start_new_session`, the janitor pattern); the child re-derives spec/ledger, runs
  the judges, applies deltas under `update_spec`'s lock, and enqueues the resulting
  "Spec update" context via `db.posttool_bg_push`. The next PreToolUse drains and
  injects it (`_drain_bg_context`), so reconcile lands one tool-step late instead of
  on the hot path. A `db.posttool_bg_lease` debounce gates one in-flight job per
  spec_key per TTL window so sequential tools cannot fork a process storm. Fail-open
  throughout: any error spawns/pushes/drains nothing.
- Anti-churn guards for the advisory judges: a `spec_judge` revise that lands on an
  already-applied (title, check) signature is now a no-op (cosmetic-reword loop fix)
  and per-task revises are capped; `posttool_notify` drops a "Spec update:" block
  whose structural signature (task ids + action verbs) already surfaced this epoch,
  even when the reason text is paraphrased (which full-body hash dedup missed).
- New AGENTS.md drift validator (`scripts/check_agents_md.py`, `just agents-audit`,
  wired as a pre-commit hook): checks that every AGENTS.md's relative markdown links
  resolve and that each `just <recipe>` referenced in a code span maps to a real
  justfile recipe. Prose `just` and `patch|minor|major` args are ignored.

Verification:

- `just version 1.17.0`; `just generated-docs` + `python3 scripts/generate_docs.py --check`
- `just test-all`
- `python3 scripts/check_agents_md.py` (passed across all AGENTS.md files)

## 1.16.0 - 2026-06-27

- Default output style switched from Fable to **mute** on Claude. `install/claude.sh`
  now sets `outputStyle = "mute"`, ships `output-styles/mute.md` (silent between tool
  calls, caveman-terse when speaking), and still ships `output-styles/fable.md` so the
  Fable orchestrator persona remains selectable. The Fable persona text is no longer
  loaded by default; unifable's grounded/verified/delegate procedure still ships via the
  SessionStart + UserPromptSubmit + Stop hooks, so orchestrator behavior is unchanged --
  only the default output verbosity changes.
- `install/claude.sh` now respects `CLAUDE_CONFIG_DIR` (default `$HOME/.claude`) so the
  installer targets the active Claude config dir instead of always `$HOME/.claude`.
- README updated to document mute-as-default with Fable available.

Verification:

- `just version 1.16.0`; `just generated-docs` + `python3 scripts/generate_docs.py --check`
- `just test-all`
- Manual: ran the patched installer with `CLAUDE_CONFIG_DIR` set; `settings.json`
  `outputStyle=mute`, both `mute.md` and `fable.md` copied to `output-styles/`.
- `~/.claude-/scripts/eval-mute-discipline.sh 3` → 3/3 PASS (Haiku, unifable enabled).

## 1.15.0 - 2026-06-27

- Removed the legacy `~/.claude/unifusion-runs/` directory (one real run record
  migrated to `~/.unifable/unifusion-runs/`); no repo code read from the old path.
- SessionStart janitor: fire-and-forget reaper (`scripts/gate/janitor.py`, spawned
  detached and throttled) cleans stale `~/.unifable/` state -- legacy
  ledger/breaker/spec JSON, 0-byte locks (race-free flock probe), dead daemon
  sockets, and stale DB rows -- older than 24h, with unifusion provenance on a
  separate 30-day retention. A session whose host process is still alive is NEVER
  reaped: `session_start.py` writes an alive-marker with the host PID
  (`scripts/gate/process_host.py` ancestry walk) and the reaper probes
  `os.kill(pid, 0)` + comm match before any skey. Fail-open and bounded
  (`UNIFABLE_JANITOR_MAX_REAP`); `UNIFABLE_JANITOR=0` disables.

Verification:

- `just test-all` (1293 passed + eval_gate_proof 40/40 + test_gate_robustness 14/14)
- `python3 scripts/audit_waits.py` (52 matched / 52 documented / 0 test sleeps)
- Manual: ran `janitor.py --run` against a copy of `~/.unifable`; legacy
  `ledgers`/`breaker`/`specs` JSON dropped (28M/24M/33M -> 0B/0B/444K),
  `bin`/`versions`/`current`/`progress.json` untouched, provenance kept (<30d).
- `uv run --no-project --with-requirements requirements-dev.txt python3 scripts/generate_docs.py --check`

## 1.14.1 - 2026-06-27

- Document `probes/` as the home for live bench/probe scripts; remove duplicate
  `scripts/bench_bedrock_ttft.py` (canonical: `probes/bench_bedrock_ttft.py`).
- README + gate AGENTS notes: Bedrock `nvidia.nemotron-nano-3-30b` as a possible
  non-Realtime judge path (not wired in).
- Slim root `AGENTS.md` into an index; hook wiring and gate conventions moved to
  `hooks/AGENTS.md` and `scripts/gate/AGENTS.md`.
- Unifusion provenance writes under `${UNIFABLE_DATA:-~/.unifable}/unifusion-runs/`
  instead of `~/.claude/unifusion-runs/`.
- `commands/setup.md`: clarify hook-only context delivery and current CLI entrypoints.

Verification:

- `just test-all` (1266 passed + eval_gate_proof 40/40 + test_gate_robustness 14/14)
- `uv run --no-project --with-requirements requirements-dev.txt python3 scripts/generate_docs.py --check`

## 1.14.0 - 2026-06-27

- Async auto-grounding lane for the groundedness breaker
  (`scripts/gate/verify_lane.py`): when the arm judge decomposes a load-bearing
  claim into repo-sanctioned verification commands (`verify_tasks` in
  `breaker_prompts.py`), the host dispatches them in a detached background runner
  (off the PreToolUse critical path), polls results on later tool calls, and
  auto-disarms as each subclaim grounds. Commands are policy-sanctioned only
  (justfile targets, documented test invocations, checks named in AGENTS.md /
  CHANGELOG); destructive or publishing commands are silently dropped. Fail-open
  throughout -- the lane can remove a false arm on a passing check, never add a
  block. Stop hook parity: `gate_stop.py` polls pending verification on allow-stop
  so text-only turns still get disarmed/confirmed.
- Moved Realtime concurrency probe to `probes/bench_realtime_concurrency.py`
  (excluded from the wait-audit scan; `probes/` holds ad-hoc benchmarks). Added
  optional Bedrock TTFT probe (`probes/bench_bedrock_ttft.py`).
- AGENTS.md: require pointer + host rehydrate for any lossless verbatim value
  that must reach the model without truncation.

Verification:

- `python3 -m pytest tests/test_verify_lane.py tests/test_groundedness_breaker.py -q`
- `just test-all` (1266 passed + eval_gate_proof 40/40 + test_gate_robustness 14/14)
- `python3 scripts/generate_docs.py --check`

## 1.13.0 - 2026-06-27

- LEAN `SYNTH_SYSTEM` for the prompt enhancer
  (`skills/explore/scripts/enhance-prompt.mjs`): added one worked few-shot
  example, bench-isolated as the SOLE quality driver (3.50 -> 4.00 / 4). Kept the
  prefix lean (~2500 chars) -- the extra decomposition/anti-patterns/output-
  format text in the fat variant earned no quality, and a live gpt-realtime-2
  probe found Realtime caches the prefix at ANY size (no 1024-token floor for the
  WS API), so padding to cross 1024 would only slow cold calls. Exported
  `SYNTH_SYSTEM` and guarded the CLI `run()` under an `isMain` check so the
  prompt is importable; added a few-shot-presence + anti-bloat guard test.
- Documented the Realtime prompt-cache mechanics behind the prefix-size decision
  (`docs/evals/prompt-enhance.md`): the cache is machine/socket-local (live
  gpt-realtime-2: same-socket 99% cached_tokens hit, cross-socket 0%),
  `prompt_cache_key` is REJECTED by the Realtime WS API (`unknown_parameter` in
  both `session.update` and `response.create`), and the cross-call cache lever is
  the family-sticky worker routing shipped in 1.12.2
  (`UNIFABLE_STICKY_ROUTING`, `UNIFABLE_STICKY_OVERFLOW_INFLIGHT`). The grade
  judge (`_GRADE_SYSTEM` in `scripts/gate/grade_override.py`) is deliberately
  UNCHANGED: a few-shot-fattened variant REGRESSED classification (83% -> 75%;
  the extra examples biased a genuine deep/architectural task to
  normal/operational), and sticky routing already caches its ~680-token prefix.
  Opt-in `usage`/`cached_tokens` telemetry on the daemon client
  (`skills/explore/scripts/lib/daemon-client.mjs`, `withUsage: true`) shipped in
  1.12.2 and backed these measurements.

Verification:

- `node --test skills/explore/scripts/test/*.test.mjs` (132 passed) -- new
  `SYNTH_SYSTEM` few-shot-presence + anti-bloat guard + the explore suite.
- `python3 -m pytest tests/test_judge_daemon_routing.py tests/test_grade_override.py tests/test_submit_enhance.py tests/test_grade_and_enhance.py -q` (66 passed).
- `python3 -m py_compile scripts/gate/realtime_daemon.py scripts/gate/grade_override.py`.
- `just generated-docs` (no diff -- hook output + judge prompts unchanged).
- Live gpt-realtime-2: `/tmp/enhance-bench/bench-synth.mjs` (LEAN = FAT quality
  at half the tokens; few-shot is the sole driver), `bench-grade.py` (fattening
  regressed -> kept current), `cache-probe.mjs` + `diag-session-key.py`
  (same-socket 99% cache hit, cross-socket 0%, `prompt_cache_key` rejected).

## 1.12.2 - 2026-06-27

- Removed `unifable doctor` CLI subcommand and the session-env validation
  protocol (`docs/session-env-validation.md`, `scripts/measure_session_env.py`).

Verification:

- `python3 -m pytest tests/test_spec_canonical_root.py tests/test_spec_facade_api.py tests/test_session_resolve.py -q`
- `just test-all`

## 1.12.1 - 2026-06-27

- Made judge directive/steering file references lossless via pointer + host
  rehydration, fixing head-truncated filenames in hook feedback (e.g.
  `research_bash_guidance.py` surfaced as `ash_guidance.py`,
  `groundedness_facade_api.py` as `ness_facade_api.py`). The judge now receives a
  numbered FILE INDEX of the paths it already saw and references a file by its
  index in double brackets (`[[n]]`); `scripts/gate/file_refs.py` rehydrates the
  pointer to the exact path in `breaker_judges.arm_judge` before directive and
  steering are parsed. The model emits an integer, never a path string, so
  truncation is impossible by construction. Mirrors the explore skill's READ
  INDEX / `excerpt_index` pointer-submit pattern
  (`skills/explore/scripts/lib/rt-rehydrate-submit.mjs`).

Verification:

- `python3 -m pytest tests/test_file_refs.py tests/test_director.py -q` (26 passed)
- Live gpt-realtime-2 validation under recall pressure: pointer adoption 16/16,
  every directive rehydrated to full untruncated paths, 0 truncations.

## 1.12.0 - 2026-06-27

- Tightened the stepwise director's `directive` and arm-path `steering` schema
  descriptions (`scripts/gate/breaker_prompts.py`) so hook feedback is
  self-contained and immediately executable: name the actual path, read-only
  command, or check inline, and NEVER reference a spec task by ID (`T1`, "the
  spec board's listed checks") that the model would have to look up. Fixes
  pointer-style directives like "use the spec board's T1 check" that gave the
  model a label instead of a runnable next action.
- Added repo-grounded prompt enhancement at UserPromptSubmit. For
  under-specified code asks (no path/file token, >= 20 words, not obviously
  operational), a Standard-tier enhancer — `retrieveCandidates` seed + 4
  parallel `gpt-realtime-mini` navigators + one full `gpt-realtime-2` synth
  (reasoning omitted, the proven trace-submit config) — runs concurrently with
  the grade judge and its output is prepended as the first `additionalContext`
  line, ahead of the static mode block. Hook wall-clock is max(grade, enhance),
  not their sum.
- Rewrote the normal/deep mode lines in `classify_task.context_for_mode` to
  trigger-anchored directives that name the verification category and the
  Stop-gate consequence (was a leading "If files change" conditional). This
  block is the fallback used whenever the enhancer does not fire or fails open.
- Hard gates on the enhanced text: zero repo-specific commands (reject ->
  static fallback), zero hallucinated paths (cited ranges filtered by the
  windows actually retrieved), char cap 1200, 6000 ms subprocess timeout,
  fail-open to the static baseline on any error. `UNIFABLE_PROMPT_ENHANCE=0`
  disables (default on).
- New `scripts/gate/submit_enhance.py` (host-agnostic policy, stdlib only) +
  `skills/explore/scripts/enhance-prompt.mjs` (Node entrypoint reusing the
  explore skill's in-repo machinery) + `tests/test_submit_enhance.py` (policy
  unit tests) + `tests/test_grade_and_enhance.py` (concurrent wiring tests) +
  `docs/evals/prompt-enhance.md`.

Verification:

- Bench: four-arm (/tmp/enhance-bench, 2026-06-27) across a fixture repo and
  the unifable repo with the synth at reasoning omitted. Standard scored
  quality 9.0 across all four prompts, 4/4 ok, 0 hallucinated paths, 0
  repo-specific commands; the lite (no-nav) tier collapsed to quality 3 on the
  large repo at omitted reasoning. See `docs/evals/prompt-enhance.md`.
- `python3 -m pytest tests/test_submit_enhance.py tests/test_grade_and_enhance.py -q` (34 passed)
- `just test-all` (pytest 1226 passed + eval_gate_proof + test_gate_robustness 14/14 + audit_waits 46/46)
- `python3 -m py_compile hooks/gate_prompt.py scripts/gate/submit_enhance.py scripts/gate/classify_task.py`
- `python3 scripts/generate_docs.py --check` (generated docs current)
- Director fix: `python3 -m pytest tests/test_director.py -q` (15 passed).
  Live judge benchmark (gpt-realtime-2, 2 scenarios x 3 wording variants): the
  shipped wording inlines the concrete command in every directive/steering and
  drops the baseline's pointer phrasing, with no latency change (~2.3-2.6 s per
  call across all variants).

## 1.11.1 - 2026-06-27

- Fixed explore repo-map prefetch hangs on large trees by replacing the final
  PageRank definition ranking pass with an O(E + D) file-rank accumulation path
  and adding over-cap graph guards.
- Added a non-git huge-tree bail-out for `generateMapText` so home/cache-like
  directories return an empty prefetch map quickly unless explicitly overridden.
- Added PageRank equivalence, over-cap skip, dense-graph linearity, and
  huge-tree bail tests plus benchmark notes for the selected ranking approach.

Verification:

- `node --test skills/explore/scripts/test/*.test.mjs` (128 passed)
- `just test-all`

## 1.11.0 - 2026-06-27

- Parallelized PostToolUse judge calls (reconcile, discover, disarm, hint) under a
  single wall-clock budget via `posttool_judges.py` daemon-thread fan-out, with
  the host PostToolUse hook timeout raised to 120s.
- Delta-merge spec updates: reconcile and discover return pure deltas applied under
  `update_spec()`'s lock (hygiene first, then reconcile, then frontier) so parallel
  siblings cannot lose citation or task-board mutations.
- Atomic session-level coalesce for reconcile+discover on a dedicated
  `posttool_claims` table (SQLite schema v3), keyed by the same spec key as the
  evidence board; unreliable turn epochs fail open to running judges rather than
  suppressing work.
- Atomic HEAVY frontier counters (`posttool_frontier_counters`) replace
  last-writer-wins ledger bumps for `frontier_research_tools` /
  `frontier_discovery_count`.
- Bounded fail-open when the judge orchestrator fails: no sequential four-judge
  fallback that could exceed the host timeout.
- Frontier dedup in `apply_frontier_additions()` uses repo `_normalize_title()` /
  `_norm_title_conflicts()` instead of raw casefold.
- Realtime daemon pool dispatch counts queued outbox work plus in-flight holders
  when picking workers and checking overload, so a first-burst of concurrent judge
  requests spreads across the warm socket pool instead of piling onto worker 0.

Verification:

- `python3 -m pytest tests/test_posttool_parallel.py tests/test_posttool_timeout_budget.py tests/test_judge_daemon_routing.py tests/test_db.py -q`
- `just test-all` (1186+ passed, 9 subtests passed; 40/40 gate scenarios; 14/14 robustness checks)

## 1.10.2 - 2026-06-27

- Fixed the judge transcript renderer dropping Codex record bodies: `_record_text`
  in `transcript_tail.py` now falls back to top-level `payload` text
  (`response_item` / `event_msg` / `turn_context`, including `result.Ok.content`),
  mirroring unifusion's `codexPayloadText`. Codex proof is no longer invisible to
  the groundedness disarm judge, which was causing an unsatisfiable
  "claim still ungrounded" loop until fail-open.
- Hardened the breaker disarm prompt: `needed` must point only at artifacts
  already visible in the transcript segment and must not invent file paths or
  internal record types (turn_context, world_state, event_msg), which the judge
  was hallucinating and steering toward records the renderer cannot expose.
- Allowed `jq` (read-only JSON processor) in the pre-spec Bash research whitelist,
  both standalone and as a pipeline sink.
- Allowed read-only shell loops/conditionals (`for`/`while`/`until`/`if`/`case`)
  in the research whitelist: control keywords are stripped and the loop is allowed
  only when every command in its body is itself whitelisted. Command-substitution
  and dangerous-assignment guards are unchanged.
- Stopped truncating load-bearing Spec-update headlines: judge task titles,
  retraction reasons, and frontier rationales now reach the model whole, and a
  multi-task reconcile no longer silently drops headlines past the fourth
  (new `build_spec_update_context` helper).

Verification:

- `python3 -m pytest tests/test_transcript_tail.py tests/test_bash_classify.py tests/test_research_bash_guidance.py tests/test_spec_headlines_not_truncated.py tests/test_judge_transcript.py -q`
- `just test-all` (1153 passed, 9 subtests passed; 40/40 gate scenarios; 14/14 robustness checks)

## 1.10.1 - 2026-06-27

- Stopped emitting cite-only PostToolUse `additionalContext` after deterministic
  citation sync. Reads and fetches still update `repo_context` / `prior_art`, but
  the model no longer spends context on "synced N cite(s)" bookkeeping.
- Suppressed empty quick-task prompt context, so a waived task that needs no
  guidance emits `{}` instead of a generic "keep it concise" reminder.
- Kept PostToolUse state updates for real action signals: failures, breaker
  status, spec CLI feedback, and judge frontier/reconcile updates still surface
  when they require model action.
- Simplified reconcile revision headlines to `Tn revised: ...` and preserved the
  full judge reason instead of truncating it.

Verification:

- `python3 -m pytest tests/test_posttool_context_dedup.py tests/test_spec_state_notifications.py tests/test_citation_sync.py tests/test_hook_token_dedup.py tests/test_spec_reconcile.py -q` (36 passed)
- `just test-all` (1130 passed, 9 subtests passed)

## 1.10.0 - 2026-06-27

- Centralized the explore skill into the stable runtime: added `skills` to
  `runtime_sync._RUNTIME_TREE`, so `~/.unifable/current/skills/{explore,
  explore-websearch}` is seeded from the newest plugin version on every
  SessionStart and resolves the same way regardless of which CLI or plugin
  cache invoked the plugin.
- Split external web research into its own discoverable `explore-websearch`
  skill. Its `websearch.sh` is a thin delegate to the shared `explore`
  implementation (one engine, no duplicated lib): it resolves the explore copy
  from `~/.unifable/current/skills/explore`, then a sibling fallback.
- Pointed the research-Bash gate resolver at the central runtime first
  (`research_bash_guidance._DEFAULT_EXPLORE_ROOTS`), so `trace.sh` / `search.sh`
  / `websearch.sh` resolve from `~/.unifable/current` and survive removal of the
  legacy hand-maintained `~/.agents/skills/explore` copy.
- Rewrote `skills/explore/SKILL.md` to document the central path and the
  trace+search scope; external research now points at `explore-websearch`.

Verification:

- `python3 -m pytest tests/test_research_bash_guidance.py tests/test_bash_classify.py tests/test_runtime_sync.py -q` (157 passed)
- Force-seeded a throwaway runtime and confirmed `~/.unifable/current/skills/explore/scripts/trace.sh` and `explore-websearch/scripts/websearch.sh` resolve with `~/.agents` absent.
- Live e2e from the central path: `trace.sh`, `search.sh`, and the `explore-websearch` `websearch.sh` shim all returned grounded output (exit 0).

## 1.9.123 - 2026-06-27

- Synced judge transcript rendering with patchpress tool-use formatting: compact
  Edit/Write diffs, EditResult diffLines parsing, and age-based tool-output
  compression (default observation masking) via `transcript_tail.py`,
  `tool_use_format.py`, and `tool_output_compress.py`.
- Vendored patchpress 0.6.x compaction into the Unifusion skill
  (`tool-use-format.mjs`, supporting modules, updated `compact-full-transcript.mjs`)
  while preserving ATIF/Codex adapters and `UNIFUSION_TRANSCRIPT` resolution.
- Extended Skill-tool parsing in `breaker_filters.py` for `@@tool Skill` blocks;
  added optional Unifusion summarizer compression env knobs.

Verification:

- `python3 -m pytest tests/test_tool_use_format.py tests/test_judge_transcript.py tests/test_groundedness_breaker.py tests/test_transcript_retention.py -q`
- `bash skills/unifusion/scripts/selfcheck.sh`
- `python3 -m py_compile scripts/gate/tool_use_format.py scripts/gate/tool_output_compress.py scripts/gate/transcript_tail.py`

## 1.9.122 - 2026-06-27

- Moved groundedness breaker restriction copy out of judge-authored steering and
  into deterministic hook-owned output.
- Added canonical hook-visible tool restriction constants covering inspection,
  write, delegation, and shell/REPL surfaces.
- Tightened groundedness judge prompts so they ask for exact grounding actions
  while the hook appends the exact `Actions restricted to:` list.
- Extended regression coverage for stale restriction stripping, manifest matcher
  sync, generated judge prompts, and REPL/exec_command breaker blocking.

Verification:

- `python3 -m pytest -q`
- `python3 scripts/generate_docs.py --check`

## 1.9.121 - 2026-06-27

- Trimmed startup, PreToolUse, Stop, and completion-handoff hook wording so the
  model sees concrete next actions without stale breaker or blocked-tool claims.
- Updated judge and director prompts for impossibility evidence, provisional loop
  release, tool-scope guidance, heavy-workflow adoption, and generated reference
  examples.
- Narrowed pack router triggers and regenerated Claude/Codex hook output plus
  judge prompt references.
- Added scoped `AGENTS.md` and `CLAUDE.md` guidance across the repo, including
  release mechanics and changelog requirements.

Verification:

- `python3 scripts/generate_docs.py --check`
- `git diff --check`
- `python3 -m py_compile hooks/gate_prompt.py hooks/gate_prompt_effort.py hooks/gate_stop.py scripts/gate/context_block.py scripts/gate/pretool_block.py scripts/gate/heavy_workflow.py scripts/gate/spec_judge.py scripts/gate/breaker_prompts.py scripts/gate/loop_release.py scripts/gate/completion_handoff.py scripts/generate_docs.py`
- `just test-all`
