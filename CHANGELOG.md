# Changelog

## 1.22.6 - 2026-06-30

- Add a held-out gating set and de-noised reporting to the unitrace trace-vs-cursor
  benchmark (`skills/unitrace/scripts/bench/trace-vs-cursor.mjs`). The harness now
  medians each task across its repeats, reports per-task quality and speed
  win/tie/loss with a win-rate, adds wall p25-p75 and composite-range spread, and
  flags low-sample or within-noise runs in the verdict notes. New
  `trace-repo-matrix-holdout.json` carries gating tasks on subsystems distinct from
  the dev/tuning matrix so pipeline changes are not scored on the questions they were
  tuned against.
- Refine trace explore and submit grounding with question-agnostic mechanisms. The nav
  explorer follows callers via generic symbol extraction and reads candidate windows for
  usage coverage, search seeding scores env-var references better, and the submit prompt
  now flags thin coverage for an unsupported cross-file header/return/forwarding/fail-open
  claim instead of asserting it. Retire the unused `rt-pipeline-seed` helper.
- Remove the question-specific assertions from `trace-schema.mjs` `validateTraceObject`
  (hardcoded filenames and line ranges keyed to specific questions) and replace them with
  question-agnostic schema tests.

Verification:

- `just test-all`
- `python3 scripts/generate_docs.py --check`

## 1.22.5 - 2026-06-30

- Harden MCP mutation detection for read-looking tool names and payloads. The
  classifier now treats `get_or_create`, `list_and_purge`, GraphQL mutations,
  SQL mutation payloads, and write-like `body` / `payload` / `value` / `script`
  fields as mutation signals while preserving read-only `SELECT` coverage.
- Strengthen hook contract tests around Claude and Codex output paths: Claude
  PreToolUse blocks now have full-path single-JSON `permissionDecision:"deny"`
  coverage across core block kinds, Codex `apply_patch` retains exit-2/stderr
  coverage, and Claude Stop-output tests force `UNIFABLE_HOST=claude` so a Codex
  runner env cannot mask additionalContext assertions.
- Extend protected-path Bash coverage for newly allowed command shapes that
  redirect output into `.unifable` or the global spec store (`find`, `pytest`,
  `python -m pytest`, and `rg | sed -n`).
- Switch the Droid-native Unifusion root orchestrator default to GPT-5.5 with
  `WebSearch` enabled, keeping Opus 4.8 as an architect panelist instead of the
  root orchestrator.
- Improve unitrace trace grounding for nav seed-to-submit questions: steer
  toward the seed producer and submit-packet consumer, suppress downstream
  rehydrate/render drift when irrelevant, backfill required passages, and add
  schema tests for that boundary.
- Add `docs/plans/codexd-substrate.md`, recording the current plan to keep
  `rtinferd` as the shared Codex-auth substrate rather than folding workflow
  policy into a monolithic daemon.

Verification:

- `just test-all` (1528 passed, 9 subtests; 40/40 gate scenarios; 14/14 robustness checks)
- `python3 scripts/generate_docs.py --check`
- `bash skills/unifusion/scripts/selfcheck.sh`
- Live tuistory probes: Claude haiku and Codex GPT-5.5 both passed fixture tests
  with `hard_block_mentions=0`
- Unifusion guidance reruns: 4/4 architect panelists returned

## 1.22.4 - 2026-06-30

- Fix `test_research_bash_guidance.py` test that asserted on stale
  `bash_allowed_summary()` copy. Updated to match current allowlist:
  added `find (read-only)`, `sed -n`, and `pytest -q`.
- Update `eval_gate_proof.py` scenario BL4: `pytest -q` is now allowed
  in pre-spec research phase, reflecting the targeted pytest verification
  policy change.

Verification:

- `just test-all` (1522 passed, 9 subtests; 40/40 gate scenarios; 14/14 robustness checks)

## 1.22.3 - 2026-06-29

- Stop narrating internal breaker state to the model. The provisional-lift
  message and the standing PostToolUse breaker line were verbose, redundant,
  and arrived too late to steer the tool decision they described. The lift is
  now an internal mechanism enforced by `tool_scope` + block directives; the
  model never sees lift prose. Six related fixes:
  - `lift_reason` is no longer model-facing. The release judge prompt now marks
    it an internal audit note (NOT shown to the agent); only a terse one-line
    `lift_scope` label is retained for the event log. (`scripts/gate/breaker_prompts.py`)
  - The provisional lift produces no model-facing notify. `_apply_release`
    records the lift in state (for `tool_scope` enforcement + the LIFT event)
    and returns `""`, so neither the in-band `breaker_notify` nor the background
    release drain carries lift prose. (`scripts/gate/breaker_runtime.py`,
    `scripts/gate/breaker_orchestration.py`)
  - PostToolUse no longer emits the standing `breaker: ARMED` /
    `breaker: PROVISIONAL lift` line; `_breaker_status_context` is a `""` stub.
    The PreToolUse one-shot notify is the single source of breaker guidance.
    (`hooks/gate_post_tool.py`)
  - PreToolUse `_block` no longer double-prints `breaker_notify` to stderr; the
    block `message` is the single channel. The `is_redundant_with_notify`
    suppression (which would emit empty blocks once the notify stopped printing)
    is removed. (`hooks/pre_tool_use.py`)
  - Stop-hook cleanup: the allow-stop tail emits `{}` unless
    `warning_after_max_blocks` is non-empty (it still calls the
    state-persisting `_advance_release`/`_advance_auto_verify` for side
    effects); `provisional_allow_message`/`format_loop_lift_context` drop the
    internal `loop_lift_reason`; `completion_runaway_warning` is trimmed to a
    one-line human-review nudge; fixed a duplicated clause in
    `_completion_stop_hint`'s signal. (`hooks/gate_stop.py`,
    `scripts/gate/loop_release.py`, `scripts/gate/verify_state.py`)
  - De-duplicate the `unifable restate` first-action instruction: SessionStart
    now sets `session_frame_notified`, and the first-prompt scaffold onboarding
    drops the restate line (starts at `add-task`) when that frame already fired.
    (`hooks/session_start.py`, `hooks/gate_prompt.py`, `scripts/gate/ledger.py`)

- unitrace bench harness: the rtinfer borrow-proof gate runs the rtinfer gold
  proof (plus a small dead-endpoint fail-open smoke) on both corpora instead of
  a uds-vs-rtinfer A/B; `search-multiformat-ab` gains `--warmup`/`--query-limit`
  and concurrency defaults. New `rtinfer-tool-caller.mjs` helper for tool-loop
  serialization. (`skills/unitrace/scripts/bench/`, `skills/unitrace/scripts/lib/`)

- unitrace daemon cutover cleanup: agentic search/trace stay on the rtinferd
  daemon bridge by default, trace/websearch no longer eagerly open a direct
  Realtime session before they know they need the fallback path, and the bench
  docs/scripts drop the remaining live `searchd`/`uds` cleanup language in
  favor of `direct` fallback terminology. Added daemon tool-round coverage in
  the unitrace rtinfer client tests. (`skills/unitrace/scripts/realtime-*`,
  `skills/unitrace/scripts/lib/`, `skills/unitrace/scripts/bench/`)

Verification:

- `just test-all` (1481 passed, 9 subtests)
- `just generated-docs` (regenerated `docs/generated/judgeprompts.md`)
- `ruff check`: zero new errors vs baseline (5 pre-existing C901, 1 pre-existing F841)
- `bash skills/unitrace/scripts/test-search.sh`
- `bash skills/unitrace/scripts/test-trace-rt.sh`
- `node --test skills/unitrace/scripts/test/daemon-attribution.test.mjs`
- `node --test skills/unitrace/scripts/test/rtinfer-client.test.mjs`

## 1.22.2 - 2026-06-29

- Finish the explore-to-unitrace rename in model-facing gate copy. The groundedness
  judge `resolve_query` schema and breaker self-resolution transcript still said
  "explore search"; they now say "unitrace search" (`scripts/gate/breaker_prompts.py`,
  `scripts/gate/breaker_judges.py`). SessionStart guidance already read "use
  unitrace for code tracing"; added a regression guard (`tests/test_context_block_thin.py`,
  `tests/test_director.py`). Regenerated `docs/generated/judgeprompts.md`.

Verification:

- `python3 -m py_compile scripts/gate/breaker_prompts.py scripts/gate/breaker_judges.py scripts/gate/verify_lane.py`
- `just generated-docs`
- `uv run --no-project --with-requirements requirements-dev.txt python -m pytest tests/test_context_block_thin.py tests/test_director.py tests/test_groundedness_self_resolve.py -q`

## 1.22.1 - 2026-06-30

- Fix a completion-gate deadlock that trapped sessions doing trivial git workflow
  (e.g. "create work branches off main so we can commit without affecting main
  until validated"). Four compounding defects, all fixed:
  - The research-phase shell allowlist allowed `git branch <name>` (create ref)
    but blocked every standard command to move onto it (`git checkout`, `git
    switch`), while pre-allowing the genuinely history-mutating `git commit`/push.
    Non-destructive branch switching is now research-phase-safe: `git checkout
    [-b|-B] <branch> [start-point]`, `git switch [-c|-C] <branch> [start-point]`,
    and a bare `git checkout|switch <branch>` to an existing local branch are
    allowed; pathspec checkout (`--`, `.`-prefixed, `dir/file.ext`), `--detach`,
    `--force`, `--ours`/`--theirs`, `--merge`, `--patch`, and `--discard-changes`
    stay blocked. (`scripts/gate/bash_classify.py`)
  - The evidence spec demanded a fetched `prior_art` URL to run local git
    plumbing. Pure git-workflow goals (branch creation, "off main", work/feature
    branch) now waive `prior_art`; pure-workflow tasks (no substantive code edit,
    no external research) waive BOTH `repo_context` and `prior_art` -- there is
    no code passage to cite and no approach to research. HEAVY is never waived.
    (`scripts/gate/spec_validation.py`)
  - A completion-gate task check that requires an action the research-phase
    allowlist blocks is a gate self-contradiction that can loop forever waiting
    for an async loop-release judge that may never fire. A deterministic detector
    (`scripts/gate/check_satisfiability.py`) now flags it at Stop with a
    judge-independent notice and the allowed alternative (e.g. `git show-ref
    --verify refs/heads/<name>`), so the agent escapes instead of looping.
  - Opt-in stricter commit/push gate: `UNIFABLE_STRICT_COMMIT_GATE=1` (env,
    default off) gates `git commit`/`git push` behind a validated spec, for
    holdout measurement before any default flip.

- Fix pre-existing `tests/test_rtinfer_client.py` flakiness on dev hosts with the
  shared rtinferd running: the "fails open without daemon" test now forces
  no-daemon conditions hermetically instead of depending on host daemon state.

- Audit ledger sync: `scripts/audit_waits.py` and `docs/testing-optimization.md`
  updated for the daemon-refactor file removals and the new satisfiability module.

Verification:

- `just test-all` (1463 passed, 9 subtests)
- `python3 -m pytest tests/test_bash_classify.py tests/test_spec_gate.py tests/test_check_satisfiability.py tests/test_rtinfer_client.py -q`

## 1.21.8 - 2026-06-30

- Fix intermittent pytest-xdist worker crashes on macOS under concurrent load.
  `pathlib.Path.resolve()` is not thread-safe when hammered from many OS threads;
  `load_ledger` / `save_ledger` / `add_finding` each re-resolved paths on every
  call. New thread-safe cached `resolve_path()` in `ledger.py` backs `data_root()`,
  `findings._resolved_root()`, and `spec_io.canonical_project_root()` (which also
  fixed a cache-before-resolve bug that still called `resolve()` on every cache
  hit). Regression: `tests/test_resolve_path.py` (32 threads x 4000 resolves).

- Stepwise director memory: the groundedness director judge is stateless, so it
  re-paraphrased the same block forever. `breaker_directive_history` (bounded,
  survives `/compact`) plus a DIRECTOR STATE block in `arm_judge` gives the judge
  its own recent turns and the imminent tool attempt, so it can RELEASE once the
  transcript shows the step was done. `clear_director()` on disarm prevents stale
  deny-scopes after release. Verification: `tests/test_director.py`.

- Realtime judge daemon self-update: when `runtime_sync` flips
  `~/.unifable/current` to a new plugin version, a running daemon drains and exits
  so the next connect respawns on fresh code (`UNIFABLE_DAEMON_SELF_UPDATE`, default
  on). Verification: `tests/test_daemon_self_update.py`.

- Unitrace search multiformat fast path: hydrate and score docs/config/data files
  alongside code (line-window hydration + doc-aware rubric), exclude true binaries
  and lockfiles, and return `[]` instead of `null` when nothing clears the floor
  (`UNITRACE_SEARCH_FAST_NULL_FALLBACK`). New JS mirror of the judge rtinfer client
  (`lib/rtinfer-client.mjs`) for optional shared-daemon scoring borrow
  (`UNITRACE_DAEMON_RTINFER`, OFF by default; presence-hint gated so bare hosts
  never probe). Node tests wired into `just test-all` via `test-search.sh`.

- Unifusion skill docs refreshed for panel workflow. Unitrace bench harnesses for
  search-multiformat A/B and rtinfer borrow callers.

Verification:

- `just test-all`
- `node --test skills/unitrace/scripts/test/rtinfer-client.test.mjs`
- `node --test skills/unitrace/scripts/test/search-multiformat.test.mjs`

## 1.21.7 - 2026-06-30

- Let the judge borrow the shared cse-tools rtinfer daemon when present. New
  stdlib-only `scripts/gate/rtinfer_client.py` discovers an always-on
  `rtinfer/1` inference endpoint (`$CSE_RTINFER_URL` -> `http://127.0.0.1:8787`
  -> `~/.cse-rtinfer/endpoint.json`, each gated on `GET /v1/infer/health`
  returning `{contract:"rtinfer/1", ready:true}`) and runs one structured ask
  over its `realtime_structured` tier. `judge_transport.ask_structured` now
  tries this borrow path first, then the existing per-session UDS daemon, then a
  direct `codex_judge.ask_structured` -- so one warm pool can serve both repos
  with no second auth path. Opt-in and OFF by default behind
  `UNIFABLE_JUDGE_RTINFER=1`: the per-session judge stays byte-identical and
  every protected fallback test is deterministic regardless of whether a
  cse-toold happens to be running on the host. Fails open exactly like the rest
  of the gate (any unreachability/timeout/non-OK envelope returns `(None, None)`
  to signal fallback). Verification: `tests/test_rtinfer_client.py` (discovery,
  ready-gating, ok/non-ok envelope parsing) and the unchanged
  `tests/test_judge_transport_fallback.py`.

## 1.21.6 - 2026-06-29

- Centralize Realtime reasoning steer across all skill-path prompts. The
  effort-aware `withReasoningSteer(user, effort)` and new `shouldSteerForEffort`
  helper in `skills/unitrace/scripts/lib/realtime_client.mjs` now decide from the
  call's reasoning effort: steer (prepend `Respond quickly, do not reason.`) only
  when effort is omitted or in `{none, off, omit, minimal, low}`, passthrough at
  `medium`/`high`/`xhigh` where the steer would contradict the API level. The
  steer is applied at two chokepoints so every skill daemon call and every direct
  structured submit is covered once: `askOnSlot` in
  `skills/unitrace/scripts/lib/daemon-client.mjs` (covers enhance nav + synth,
  trace daemon submit, unisearch daemon synth + scorer, search fast-path scoring)
  and `askStructured` in `realtime_client.mjs` (covers trace + unisearch session
  submit, search finish turn). Direct agentic user-turn sends that bypass both
  chokepoints are wrapped explicitly: `realtime-search.mjs` `userItem`,
  `lib/rt-agent-session.mjs` `userPrompt` + nudge, and `realtime-trace.mjs`
  explore seed note + nudge. Redundant manual wraps added in 1.21.4
  (`enhance-prompt.mjs`, `lib/rt-explore-nav.mjs`, `realtime-websearch.mjs`
  scorer + synth) are removed since the chokepoints now own them. Gate judges
  (breaker/spec/grade/completion/suicide-loop) are excluded by design: they flow
  through Python `judge_transport` -> `codex_judge`, never these JS chokepoints.
  Backward compatible: a boolean second arg to `withReasoningSteer` still works.
  Idempotency guarantees no double-prefix during the transition.

Verification:

- `node --test skills/unitrace/scripts/test/reasoning-steer.test.mjs`
- `python3 -m pytest tests/test_judge_daemon_routing.py -q`
- `just test-all`

## 1.21.5 - 2026-06-29

- Whitelist the cse-sweep skill's read-only evidence entrypoint `sweep.sh` in the
  research-phase Bash gate. Running a cse-sweep is read-only evidence gathering
  (it writes only to a `/tmp` run dir) and grounds the spec, so it belongs in the
  pre-spec research phase alongside the unitrace scripts. `bash_classify.py` now
  trusts `sweep.sh` by basename both standalone (`scripts/sweep.sh ...`) and via an
  interpreter (`bash/sh/zsh scripts/sweep.sh ...`), mirroring the existing
  `UNITRACE_SCRIPT_BASENAMES` precedent via a new `CSE_TOOLS_SCRIPT_BASENAMES`
  constant in `research_bash_guidance.py`. The block-message allowlist copy
  (`bash_allowed_summary`, `allowed_research_bash_detail`) names it too. All other
  guards (protected paths, command substitution, redirection, dangerous env) are
  unchanged: `python sweep.sh`, `bash other.sh`, `$(...)`, and `UNIFABLE_DEV=` stay
  blocked.

Verification:

- `python3 -m pytest tests/test_bash_classify.py tests/test_research_bash_guidance.py -q`
- `just test-all`

## 1.21.4 - 2026-06-29

- Retune Realtime reasoning effort and prompt steering across unitrace/unisearch
  skill paths. Explore (agentic + nav mini batch) now omits API reasoning and
  prepends a steer line (`Respond quickly, do not reason.`) on user turns via
  shared `withReasoningSteer()` in `skills/unitrace/scripts/lib/realtime_client.mjs`.
  Submit paths (trace + unisearch session fallback + trace daemon submit) default
  to `reasoning.effort: low`. Unisearch daemon synth and parallel scorer use omit
  + steer; prompt enhancer synth uses `low` + steer (replacing the prior omit-only
  config). Defaults: `DEFAULT_UNITRACE_REASONING_EFFORT=none`,
  `DEFAULT_SUBMIT_REASONING_EFFORT=low`; shell wrappers updated accordingly.
- Fix warm-socket daemon IPC: `realtime_daemon._handle()` now forwards
  `reasoning_effort` from client requests into `codex_judge._response_create()`,
  so per-call reasoning settings from `daemonAsk`/`daemonAskBatch` actually apply
  (previously every daemon call silently inherited `UNIFABLE_JUDGE_REASONING_EFFORT`).

Verification:

- `node --test skills/unitrace/scripts/test/reasoning-steer.test.mjs`
- `python3 -m pytest tests/test_judge_daemon_routing.py::test_handle_forwards_reasoning_effort -q`
- `just test-all`

## 1.21.3 - 2026-06-29

- Harden the provisional-lift monitor against phantom board-task citations and
  pin the REPL tool-output visibility fix. The monitor judge
  (`monitor_provisional_judge`) could emit a `drift_level=2` re-arm whose
  `feedback` cited spec-board task IDs that do not exist in the rendered board
  (e.g. "T17/T18 still unresolved" when the board says "Spec complete: all tasks
  validated" and those IDs appear nowhere in the transcript). Two-layer guard:
  (2a) the `_MONITOR_SYSTEM` prompt and the `feedback` schema field now instruct
  the monitor to treat the rendered SPEC BOARD block as the only authoritative
  task status and never cite a task ID absent from it (`scripts/gate/breaker_prompts.py`);
  (2b) a deterministic host-side scrub `_scrub_phantom_task_ids` strips any `T<n>`
  token from monitor `feedback` that is not present verbatim in the segment, so
  an invented ID can never reach the model regardless of judge output. The drift
  verdict itself is never overridden -- only the message text is cleaned -- so
  real drift is never masked (`scripts/gate/breaker_judges.py`). Fail-safe: any
  error returns the original feedback. Also adds a regression test proving the
  renderer surfaces record-level `toolUseResult` proof to the monitor (pins the
  1.21.2 visibility fix against future renderer regressions).

Verification:

- `python3 -m py_compile scripts/gate/breaker_judges.py scripts/gate/breaker_prompts.py`
- `python3 -m pytest tests/test_groundedness_breaker.py -q`
- `just test-all`

## 1.21.2 - 2026-06-28

- Surface REPL tool output to the groundedness-breaker judge. In REPL-only
  sessions (`CLAUDE_CODE_REPL=1`) Claude Code leaves the inline
  `tool_result.content` empty and puts the real output in the record-level
  `toolUseResult` (Bash `{stdout,stderr}`, Read `{file:{content}}`, WebFetch
  `{result}`, ...). The judge transcript renderer rendered the inline form first,
  which returns the non-empty `"[tool_result]"` placeholder, so its existing
  `toolUseResult` fallback was never reached and the judge saw every tool result
  as an empty `[tool_result]`. With no visible output, the arm judge armed on
  already-proven claims (e.g. "the change was committed", seen right after
  `SECRET SCAN OK`) and the disarm judge could never ground them, deadlocking
  mutation/Bash. `_record_text` now falls back to the record-level
  `toolUseResult` when the inline render is empty or placeholder-only, preserving
  the `[tool_result]` framing (`scripts/gate/transcript_tail.py`). Non-REPL
  inline-content sessions are unaffected.

Verification:

- `python3 -m py_compile scripts/gate/transcript_tail.py`
- `python3 -m pytest tests/test_transcript_tail.py tests/test_groundedness_breaker.py tests/test_judge_transcript.py tests/test_tool_use_format.py tests/test_transcript_lineage.py -q`
- `just test-all`

## 1.21.1 - 2026-06-28

- Fix a research-gate deadlock in REPL-only sessions (`CLAUDE_CODE_REPL=1`).
  When every tool must run inside the `REPL` tool, shell is invoked as a
  positional string -- `await sh("unifable add-task ...")` -- but the gate's
  shell-command extractor only recognized the object form
  (`Bash({command: ...})` / `sh({command: ...})` / `exec_command({cmd: ...})`).
  Positional `sh("...")` extracted to nothing, so `pre_tool_use._enforce_bash`
  fell through to a blanket "REPL code is not a whitelisted research read"
  block -- even for whitelisted commands like the spec CLI. The Stop gate then
  kept demanding `unifable add-task` while the only way to run it was wrongly
  blocked. `repl_shell_cmds_from_code` now also matches positional-string and
  tagged-template forms (`sh("...")`, `sh(\`...\`)`, `sh\`...\``), with
  order-preserving dedupe so a command is never double-counted
  (`scripts/gate/parse_tool_result.py`). The single chokepoint fixes the
  pre-tool gate, the breaker research bypass, and citation extraction together.

Verification:

- `python3 -m py_compile scripts/gate/parse_tool_result.py`
- `python3 -m pytest tests/test_citation_verify.py tests/test_bash_pretooluse_gate.py tests/test_groundedness_breaker.py -q`
- `just test-all`

## 1.21.0 - 2026-06-28

- Bash research whitelist now allows `cat` and `nl` for explicit file reads
  before the evidence spec validates. They must name an actual file (no stdin,
  no shell redirections, no `/etc/`, `/dev/`, or pure-variable paths), and they
  work standalone, in `&&`/`||` chains, and as read-only pipeline sinks. This
  closes the gap where reading a single file to ground work required the
  heavier `unitrace` / `unisearch` path or manual `head`/`tail`. Updated the
  allowlist guidance (`scripts/gate/bash_classify.py`,
  `scripts/gate/research_bash_guidance.py`) and gate proof scenarios
  (`tests/eval_gate_proof.py`).
- Standing groundedness-breaker status in PostToolUse is now actionable.
  Instead of a truncated "ARMED on '<claim>'" line, the context line states
  why mutation tools are blocked and the exact next step to clear it, reusing
  the stored breaker steering/directive when available. Provisional lifts
  similarly state their scope, reason, and the drift warning. Routing prefixes
  and dedup remain unchanged (`hooks/gate_post_tool.py`).
- Runtime sync retries the `~/.unifable/current` flip once if a transient
  `OSError` (e.g. a `.current.tmp` collision) would otherwise leave `current`
  stranded on the old version after the new one was already copied
  (`scripts/gate/runtime_sync.py`).
- Thinner SessionStart frame for the global research launchers: drops the
  inline examples so the line reads "use unitrace for code tracing; use
  unisearch for web research" (`scripts/gate/context_block.py`).

Verification:

- `python3 -m py_compile hooks/gate_post_tool.py scripts/gate/bash_classify.py scripts/gate/research_bash_guidance.py scripts/gate/runtime_sync.py scripts/gate/context_block.py`
- `python3 -m pytest tests/test_bash_classify.py tests/test_bash_pretooluse_gate.py tests/test_gate_lift.py tests/test_research_bash_guidance.py tests/test_runtime_sync.py tests/test_breaker_status_context.py tests/test_spec_gate.py tests/test_spec_state_notifications.py -q`
- `just generated-docs`
- `just test-all`

## 1.20.1 - 2026-06-28

- Stop now always allows completion when the host is in Plan Mode, so the
  session can surface its plan for approval instead of looping. The completion
  gate's step 1 (evidence gate) is INFINITE -- it ignores `stop_hook_active` and
  the stop-block cap until the evidence spec validates -- and its task checks
  routinely require repo mutation that Plan Mode forbids. With no plan-mode
  exit, Stop blocked forever and the session (notably Codex) could never leave
  plan mode. `plan_mode` was consulted in `ledger.py`, `spec_judge.py`, and
  `pre_tool_use.py` but never in `gate_stop.py`. Added `_plan_mode_allows_stop`
  (`hooks/gate_stop.py:435`) and an allow-stop short-circuit
  (`hooks/gate_stop.py:475`) that runs after transcript resolution and before
  the evidence gate and completion-handoff judge. Detection: explicit
  `session_context.plan_mode_enabled` on the Stop payload, else the shared
  resolver (`plan_mode.resolve_plan_mode_for_hooks`: transcript `turn_context` /
  ledger cache). Fail-open. Regression: `tests/test_stop_plan_mode_allows.py`.

## 1.20.0 - 2026-06-28

- SessionStart `additionalContext` now surfaces the two global research
  launchers it never mentioned: `use unitrace to trace code` and `use unisearch
  for web research`, each with one runnable example. The frame matched the old
  interaction surface (only `unifable restate`) and omitted the `unitrace` /
  `unisearch` commands shipped on `~/.local/bin` in 1.19.0, so the model never
  learned they were available. Tightened the surrounding preflight prose
  (restate sentence, write-tools bullet, dropped the redundant narration line)
  to keep the frame thin (`<950` chars, `<12` non-empty lines) with the added
  guidance. `scripts/gate/context_block.py`; regenerated `docs/generated/`.

## 1.19.0 - 2026-06-28

- Renamed the `explore` skill to `unitrace` and `explore-websearch` to `unisearch`
  (directories, `name:` frontmatter, SKILL.md headings/descriptions, and the
  entry scripts `trace.sh` -> `unitrace.sh` and `websearch.sh` -> `unisearch.sh`).
  The deep-trace entry is now `skills/unitrace/scripts/unitrace.sh`; the external-
  research entry is `skills/unisearch/scripts/unisearch.sh`, a thin delegator that
  self-resolves its sibling `skills/unitrace` implementation (one impl, no drift).
  Internal JS module filenames and function names containing "explore" (e.g.
  `explore-skill-context.mjs`, `explore_exec`, `runExplorePhase`) are intentionally
  left as-is; the trace pipeline phase names ("explore phase", "nav explore") are
  unchanged.
- New global `unitrace` and `unisearch` launchers on `~/.local/bin`, mirroring the
  `unifusion` launcher added in 1.18.0. `scripts/gate/runtime_sync.py` now writes
  `_UNITRACE_BOOTSTRAP` + `_UNISEARCH_BOOTSTRAP` (registered in `_BOOTSTRAPS`) that
  exec `~/.unifable/current/skills/unitrace/scripts/unitrace.sh` and
  `.../skills/unisearch/scripts/unisearch.sh`. So `unitrace "<question>"` and
  `unisearch "<research goal>"` run from any cwd whether or not the plugin is
  enabled, as long as `~/.unifable/current` has been seeded once. `setup/uninstall.sh`
  now also removes the `unitrace` + `unisearch` links.
- **Breaking: `EXPLORE_*` env-var prefix migration.** All `EXPLORE_*` knobs are
  renamed: websearch-specific vars (`EXPLORE_WS_*`, `EXPLORE_ALPHA_*`,
  `EXPLORE_WEBSEARCH_*`) -> `UNISEARCH_*`; everything else (RT, SEARCH, MAP, GREP,
  AST, PAGERANK, BENCH, HOME, IMPL_DIR, SKILL_*, RUNS_DIR, etc.) -> `UNITRACE_*`.
  Anyone overriding `EXPLORE_*` knobs today must switch to the new prefixes. The
  `UNIFABLE_EXPLORE_SKILL_ROOT` env override is now `UNIFABLE_UNITRACE_SKILL_ROOT`.
- Gate allowlist (`scripts/gate/research_bash_guidance.py` + `bash_classify.py`):
  `UNITRACE_SCRIPT_BASENAMES = ("unitrace.sh", "unisearch.sh", "websearch.sh",
  "search.sh")`; the skill-name regex now matches `name: unitrace`; default roots
  point at `skills/unitrace`; `submit_enhance._entrypoint_path` resolves
  `skills/unitrace/scripts/enhance-prompt.mjs`. The trace seed matcher
  (`rt-map-seed.mjs`) now triggers on `\b(?:uni)?trace\b` so "unitrace" questions
  still get curated trace seeds, and the curated seed path is `scripts/unitrace.sh`.
- Runtime inventory (`scripts/audit_runtime_inventory.py`) + its allowlist
  (`docs/benchmarks/python-consolidation-runtime-allowlist.json`) re-pointed at
  `skills/unitrace` / `skills/unisearch` with `unitrace-skill` / `unisearch-skill`
  owners. Regenerated `docs/generated/` (judgeprompts now reference
  `unitrace.sh`/`search.sh`). Updated README, root + skills `AGENTS.md`, and
  `docs/evidence-gate-design.md` prose/paths.
- Tests: added `test_sync_installs_unitrace_launcher` +
  `test_sync_installs_unisearch_launcher` to `tests/test_runtime_sync.py`; updated
  `test_research_bash_guidance.py`, `test_bash_classify.py`,
  `test_runtime_inventory.py`, `test_submit_enhance.py`, `test_command_output_evidence.py`,
  `test_mcp_evidence.py`, `test_research_evidence_compress.py`,
  `test_codex_judge_reask.py`, and the in-skill node tests (`rt-map-seed`,
  `rt-pick-passages`, `rt-trace-utils`, `explore-wire-format`) to the new skill
  names, script basenames, and `UNITRACE_*`/`UNISEARCH_*` env vars.
- SessionStart frame (`scripts/gate/context_block.py`) now includes mute discipline:
  "Do not narrate exploration. Tool calls only until blocked or done." Regenerated
  hook-output docs; thin-frame test cap raised to 950 chars.
- Removed `output-styles/fable.md`. `install/claude.sh` ships only `mute.md` as the
  default output style (`outputStyle=mute`).

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
  (`research_bash_guidance._DEFAULT_UNITRACE_ROOTS`), so `trace.sh` / `search.sh`
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

## 1.22.0 - 2026-06-30

- Update rtinfer discovery for standalone rtinferd daemon. Drop the
  `http://127.0.0.1:8787` (cse-toold cockpit) candidate from both
  `scripts/gate/rtinfer_client.py` and
  `skills/unitrace/scripts/lib/rtinfer-client.mjs`. The `/v1/infer` endpoint
  has moved to the standalone rtinferd daemon (repo: rtinfer), which
  advertises via `~/.cse-rtinfer/endpoint.json`. New discovery order:
  `$CSE_RTINFER_URL` -> `~/.cse-rtinfer/endpoint.json`.
