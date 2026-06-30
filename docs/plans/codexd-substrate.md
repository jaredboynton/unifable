# rtinfer -> shared Codex-auth substrate (not a "codexd" monolith)

Unifusion panel (GPT-5.5, Opus 4.8, GLM-5.2, Kimi K2.7), 2026-06-30. Provenance:
`~/.unifable/unifusion-runs/2026-06-30_040259_droidexec-gpt5.5-opus4.8-glm5.2-kimi2.7.md`.

## Verdict

Make `rtinferd` the shared Codex-OAuth **substrate** (auth refresh, warm pools,
discovery, low-level authenticated transport). Do **not** fold websearch/trace/
scoring/synth/nav *policy* into it. Keep the daemon off the correctness path; it
stays fail-open. A `codexd` that owns `web_run` swarm, authority gating, citation
rehydration, trace FS reads, and final-output validation = one shared blast radius
coupling fast-moving skill logic to a global self-updating daemon. Rejected by panel.

## Grounding (this tree)

- Daemon lives in a separate repo `~/__devlocal/rtinfer` (Rust, multi-platform npm
  `@jaredboynton/rtinfer`). unifable ships only thin adapters
  (`skills/unitrace/scripts/lib/rtinfer-client.mjs`, `scripts/gate/rtinfer_client.py`).
  => any rename is upstream-led, not a unifable edit.
- Daemon ALREADY exposes a capability surface, not just `daemonAsk`:
  `/v1/infer`, `/v1/infer/health`, `/v1/models`, `/v1/chat/completions`,
  `/v1/responses`. The "substrate" the panel wants largely exists.
- Contract `rtinfer/1`; client tier string `realtime_tool_round`
  (`rtinfer-client.mjs:181`) — confirm the daemon implements it or drop the surface.
- Discovery: `CSE_RTINFER_URL` override + well-known `~/.cse-rtinfer/endpoint.json`
  (`contract` major-gated). Env family `CSE_RTINFER_*`, `UNITRACE_DAEMON_RTINFER`
  (legacy `UNITRACE_SEARCH_RTINFER`).
- Auth duplication is real: many callers read `~/.codex/auth.json` directly
  (`search-rt.mjs`, `trace-rt.sh`, `websearch-rt.sh`, `setup.sh`, ...). This is the
  best centralization target.
- No real `codexd` name collision in-tree (only `codexDeltaText`, unrelated).

## Plan

1. Keep `rtinfer/1` as the compatibility floor. Preserve `/v1/infer`, health, models,
   fail-open client behavior. Fix contract drift: decide `realtime_tool_round` —
   implement server-side or remove the client tier.
2. Add `codexd/1` as an ADDITIVE capability superset, not a flag-day rename. Health
   advertises both `rtinfer/1` and `codexd/1`. New clients may prefer `CODEXD_URL` +
   a codexd endpoint file while still honoring `CSE_RTINFER_URL` and the legacy path.
3. Centralize auth in the daemon: add a narrow token-broker/auth-proxy capability so
   callers stop reading `~/.codex/auth.json`. Fail-open: proxy down -> callers fall
   back to direct auth. Never expose bearer tokens in logs/health/traces. Probe
   alpha/search transport first (curl/TLS behavior) so the proxy doesn't reintroduce
   Cloudflare failures.
4. Daemon API = capability primitives, NOT product workflows.
   - Keep/extend: `health`, `capabilities`, `infer`, `infer/batch`, `responses`,
     `chat/completions`, optional `tool-round`, auth-health/proxy, maybe stateless
     score/synth over caller-provided inputs.
   - Stay caller-side: "run full websearch", "own trace", "read repo", "produce final
     citations". Swarm orchestration, authority gating, citation rehydrate, nav policy,
     final validation, per-skill fallbacks stay in `realtime-websearch.mjs` et al.
5. Lifecycle centrally managed, not per-skill. Daemon package owns install/status/
   self-update + user LaunchAgent (macOS). Skill scripts only probe health + do
   bounded idempotent bootstrap/hints — never mutate LaunchAgents or run installs on
   every invocation. Mutable daemon state stays OUT of `~/.unifable/current` (that path
   is versioned skill bodies). Need a `systemd --user`/foreground story before
   promising auto-maintenance on Linux.
6. Rename cautiously. Do NOT hard-rename now. `codexd` first as alias/conceptual
   superset once non-inference Codex-auth transport is real. Keep `rtinferd`,
   `rtinfer/1`, old env vars, old endpoint paths as compat aliases through a measured
   migration window.

## Risks

- Transparent auth proxy must preserve curl/TLS behavior on alpha/search or it
  reintroduces Cloudflare/transport failures.
- `realtime_tool_round` ownership undecided.
- Final endpoint/data-root naming must be settled before migration.
- Cross-platform lifecycle unproven outside macOS LaunchAgent.
