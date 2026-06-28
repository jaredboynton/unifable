# PostToolUse async-dispatch + consolidation plan

Status: **pending approval** (ralplan consensus; no code changed)

## Goal (elegant restatement)

Turn PostToolUse from a synchronous verifier into a **non-blocking dispatcher**.
It fires once, forks its load-bearing work into detached/threaded workers, and
returns `{}` immediately. Workers write their verdicts to the shared ledger /
breaker state **under the existing cross-process lock**. The gates that already
run on the hot path become the convergence points:

- **Arming stays synchronous in PreToolUse.** It is the only decision that must
  block a mutation before it runs. Untouched.
- **Disarm (the unblock) moves off the hot path.** A worker writes the lifted
  state when it finishes; the block clears the instant that state lands.
- **PreToolUse is the self-healing convergence point.** It already re-runs the
  disarm judge on every armed call, so a slow/dead worker never strands the
  breaker — the next gated tool disarms itself.
- **Stop is the sole async barrier.** It drains in-flight workers so nothing
  ships while a verdict is pending.

Slogan: **observe asynchronously, enforce at the gates, converge at Stop.**

## What the trace established (ground truth)

| Concern | Where it lives today | Sync/async | Lock |
|---|---|---|---|
| ARM + tool-scope persist | PreToolUse `evaluate_pre_tool_locked` (`pre_tool_use.py:726`) | sync, **blocks mutation** | `breaker_lock` |
| tool-scope enforce (`in_scope`) | PreToolUse `_enforce_tool_scope` | sync block | reads state |
| DISARM via transcript judge | PostToolUse `_breaker_release_context` -> `evaluate_post_tool_release` + `save_breaker` (`gate_post_tool.py:84-104`) | **sync, ~100s daemon-thread budget** | **NO lock** |
| DISARM via transcript judge (armed) | PreToolUse `evaluate_pre_tool` armed branch | sync | `breaker_lock` |
| DISARM via sanctioned-command verify | `verify_lane` detached runner -> sidecar; polled by PreToolUse-armed + Stop | **already async** | `breaker_lock` on poll |
| reconcile / frontier-discover | `posttool_background.spawn_reconcile_job` detached -> `db.posttool_bg` queue; drained by PreToolUse `_drain_bg_context` + Stop | **already async** | spec lock + lease |
| repeated-failure hint | PostToolUse `run_posttool_judges` thread | sync, advisory | n/a |
| test-after-edit | **separate PostToolUse process** `test_after_edit.py` (2nd manifest group) | sync subprocess (<=60s) | n/a |

Two findings drive the plan:

1. **The pattern already exists.** `verify_lane` (sanctioned-command disarm) and
   `posttool_background` (reconcile/discover) are exactly the user's
   "fork -> worker writes verdict -> gates converge" model: detached
   `start_new_session` spawn, atomic DB queue (`posttool_bg_lease/push/drain`),
   PreToolUse drain, Stop drain. The async disarm generalizes this to the
   transcript-release judge.
2. **There is a latent race today.** PostToolUse's inline disarm calls
   `save_breaker` **without `breaker_lock`**, while PreToolUse arm/disarm holds
   it. A concurrent arm + post-tool disarm can clobber. Routing disarm through a
   locked worker **fixes** this, it does not introduce it.

## Decision

### Criteria
1. **Latency** — remove synchronous judge round-trips from the PostToolUse hot path.
2. **Correctness** — no lost disarm; no clobbered arm; arming stays synchronous and blocking in PreToolUse.
3. **Reuse** — lean on the existing detached-spawn + DB-queue + drain + `breaker_lock` machinery; invent nothing new.
4. **Fail-open** — a dead/slow worker never wedges; worst case the verdict is absent and the next gate re-checks.
5. **Bounded** — a lease guards against per-edit process storms; Stop is the hard barrier.

### Options considered

**Option A — literal full fire-and-forget.** PostToolUse spawns every job
detached and emits `{}`. Workers write under lock; PreToolUse/Stop converge.
- Pros: lowest PostToolUse latency; uniform model; fixes the unlocked-disarm race.
- Cons: the inline disarm *message* ("you may proceed") no longer rides the same
  turn — it surfaces on the next PreToolUse drain (one-tool lag), or at Stop if
  the model goes text-only.

**Option B — minimal: only fold `test_after_edit`, keep disarm inline/sync.**
- Pros: smallest change; preserves the immediate disarm message.
- Cons: ignores the actual architectural ask; PostToolUse still spends the ~100s
  disarm budget on the hot path; the unlocked-disarm race remains.

**Option C — hybrid (CHOSEN).** Option A done with the existing primitives:
route the transcript-disarm judge through the **same locked queue+drain** the
reconcile path uses, make the disarm worker take `breaker_lock`, fold
`test_after_edit` into the single PostToolUse entrypoint as one dispatched
worker, and strengthen Stop to drain the disarm worker (not just `verify_key`).

### Chosen: Option C — deciding reason
It satisfies all five criteria with machinery that already ships: correctness
(locked disarm closes the race), reuse (lease/queue/drain/lock exist), latency
(PostToolUse drops to spawn-time). The one-tool-lag disarm-message cost of async
is **already absorbed** by the system — PreToolUse re-runs the disarm judge on
every armed call, so the next gated tool disarms itself regardless; the worker
just makes it usually-already-done. Stop covers the text-only tail.

### Rejected: Option B — why
Too narrow. It delivers the hook consolidation but leaves the user's real ask
(non-blocking PostToolUse) unaddressed and preserves the latent unlocked-disarm
race and the synchronous judge budget.

## The convergence invariants (what makes async correct)

1. **Arm is synchronous in PreToolUse and never moves.** Only the lift goes async.
2. **One writer discipline via `breaker_lock`.** Every breaker read-judge-write —
   foreground arm/disarm AND the async disarm worker — runs inside the same
   `breaker_lock` flock. No clobbered arm.
3. **Disarm is idempotent across callers.** PostToolUse worker and PreToolUse-
   armed both run the same release judge on the same `breaker_claim`. A lost or
   slow worker is harmless: the next armed PreToolUse disarms inline.
4. **The DB queue is the rendezvous.** Workers never talk to each other; they
   write breaker state + enqueue context; gates drain. Atomic `BEGIN IMMEDIATE`.
5. **Stop drains everything.** It already polls `verify_key`; extend it to also
   wait briefly for an in-flight disarm worker so a text-only tail still converges.
6. **Fail-open per worker, bounded by a lease.** Spawn failure, dead worker, or
   storm guard all degrade to "verdict absent, gate re-checks" — never a wedge.

## Implementation outline (for the execution phase — not done here)

1. **Consolidate the manifest 2 -> 1.** Drop the second PostToolUse group
   (`test_after_edit.py`) from `hooks/hooks.json` and `.codex-plugin/hooks.json`.
   Keep `test_after_edit.py` as an importable module.
2. **Fold test-after-edit into `gate_post_tool.main()`** behind its existing env
   gate (`UNIFABLE_TEST_AFTER_EDIT`) + edit-tool matcher (`tool_name in EDIT_TOOLS`),
   dispatched as a worker (it already runs a detached <=60s subprocess + debounce).
   Merge its context into the single emission (same way `gate_prompt.py` merges
   router+effort+base).
3. **Move the transcript-disarm judge to a detached worker.** New
   `breaker_release` lane modeled on `posttool_background` /  `verify_lane`:
   detached `start_new_session` spawn, lease-guarded per breaker/claim key, the
   worker does `breaker_lock -> load_breaker -> evaluate_post_tool_release ->
   save_breaker`, then `posttool_bg_push`-style enqueue of the disarm message.
   PostToolUse stops calling `_breaker_release_context` inline.
4. **PreToolUse drains the disarm message** alongside the existing
   `_drain_bg_context` (one read-and-clear). Its armed-branch disarm judge is the
   self-heal backstop — unchanged.
5. **Stop drains the disarm worker.** Extend `_advance_auto_verify` (or a sibling)
   to also drain any pending breaker-release context / await an in-flight lease
   within a small bounded budget, under `breaker_lock`.
6. **PostToolUse emits `{}` on the hot path** once dispatch is done; only
   already-converged context (drained deltas) rides out.

## Test plan

- **unit**: disarm worker takes `breaker_lock`; interleaved arm+disarm shows no
  lost-arm; `test_after_edit` invoked in-process behind env+matcher gate and its
  context merges; lease prevents a second disarm worker per claim.
- **integration**: PostToolUse returns `{}` with no inline judge round-trip;
  disarm verdict lands in breaker state; next PreToolUse sees disarmed or
  self-heals; Stop drains a pending disarm.
- **e2e**: arm on a confident claim -> release tool -> PostToolUse dispatches ->
  next tool unblocked; model goes text-only -> Stop disarms.
- **observability**: `VERIFY_DISPATCH`/`DISARM` breaker events present; queue
  drain + lease counters; PostToolUse wall-clock drops to spawn-time in a bench.

## Pre-mortem (high-risk: touches the enforcement layer)

1. **Disarm never lands -> breaker stuck armed -> session bricked.** Mitigation:
   PreToolUse re-runs disarm every armed call (self-heal); Stop drains; worker is
   belt-and-suspenders, not the sole path; fail-open spawn.
2. **Async disarm clobbers a concurrent PreToolUse arm.** Mitigation: the worker
   takes `breaker_lock` — this is the fix; today's inline path does not lock.
3. **Process storm: every edit spawns a disarm worker.** Mitigation: lease TTL
   keyed on breaker/claim (mirror `posttool_bg_lease`); only one in flight.

## ADR

- **Decision**: PostToolUse becomes a non-blocking dispatcher; transcript-disarm
  moves to a locked detached worker; `test_after_edit` folds into the single
  entrypoint (PostToolUse 2 -> 1).
- **Drivers**: hot-path latency, the latent unlocked-disarm race, reuse of
  existing async primitives, fail-open safety.
- **Alternatives**: A (literal fire-and-forget, no lock discipline spelled out),
  B (consolidation only, disarm stays sync).
- **Why chosen**: C reuses shipped machinery, fixes the race via the existing
  lock, and the only cost (one-tool disarm-message lag) is already absorbed by
  PreToolUse's self-healing disarm.
- **Consequences**: PostToolUse no longer surfaces an immediate disarm message on
  the same turn; it arrives on the next gated tool or at Stop. Breaker state gains
  a third writer, all funneled through `breaker_lock`.
- **Follow-ups**: bench PostToolUse wall-clock before/after; confirm the lease
  key granularity (per-claim vs per-session) under parallel tool batches.
