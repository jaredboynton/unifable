# Evidence-Lockdown Gate — design

Goal: make citation a *precondition to acting*, not an after-the-fact check. Until the model has
documented verifiable evidence (code `path:line`, tool `command -> output`, research/prior-art URL),
only read/research tools and an explicit script allowlist are permitted; mutating/action tools are
blocked at the PreToolUse boundary. This closes the hole a Stop-side detector leaves open — where the
agent thrashes through edits first and only "cites" at the end.

## Why a PreToolUse lockdown (prior art + in-repo substrate)

- Prior art: `why-was-fable-banned` (https://github.com/SihyeonJeon/why-was-fable-banned) implements
  exactly this shape — a `PreToolUse` hook "intercepts every edit and exits 2 until the spec passes"
  (README "How it works"). Its spec demands restated goal, context chosen by authority, >=2 rejected
  alternatives with the boundary each breaks, risks, and *runnable acceptance*. Measured in-repo:
  "Adversarial / edge gate tests (downgrade, bypass, malformed, no-brick): 35/35 pass"; decision record
  per change goes from "none" to "enforced"; unspeced/forbidden-path edits go from "possible" to
  "blocked" (README "Benchmarks"). Rules were derived from 42 recorded real engineering sessions
  generalized to 8 decision axes (README "Where the rules came from"). This validates the pattern;
  note wfb gates *edits only*, not Bash — gating Bash (below) is beyond the proven prior art.
- In-repo substrate (we extend, not greenfield): `hooks/pre_tool_use.py:157-190` is a PreToolUse
  gate that blocks `WRITE_TOOLS` until a spec validates (unconditional — no env disable, fail-open
  on malformed input). `scripts/gate/spec.py:116-168` validates `restated_goal` +
  `acceptance_criteria[{check, evidence}]` and rejects placeholder evidence via `FAKE_MARKERS`
  (`spec.py:78-99`); HEAVY uses frontier-first workflow (>=2 frontier approach tasks + 1 primary;
  see `heavy_workflow.py`). Classification uses the operative user instruction, not pasted corpus.
  Proactive grade adjudication: always-on UserPromptSubmit hook when effective grade is
  HEAVY; gpt-realtime-2 (`grade_override.py`) downgrades mis-graded tasks to STANDARD
  without operator action. Pinned `grade_override_target` survives `higher_mode` re-escalation.
  `scripts/gate/classify_task.py:75` maps quick->LIGHT / normal->STANDARD / deep->HEAVY. The
  lockdown is three deltas on this.

## The two phases

- RESEARCH phase (locked) — the default once a work-shaped task at grade >= STANDARD starts.
  - ALLOWED: `Read`, `Grep`, `Glob`, `WebSearch`, `WebFetch`, read-only MCP
    (exa/tavily/ref/octocode search+fetch). `Bash` is allowed ONLY for `cd`, `ls`, `glob`, `rg`, or running
    any file named `trace.sh` or `websearch.sh` (see Bash gating).
  - BLOCKED: `Edit`/`Write`/`MultiEdit`/`NotebookEdit`/`apply_patch`, `Task`/`Agent`, and non-whitelisted `Bash`, each with a
    message naming the unlock step.
- ACTION phase (unlocked) — once the evidence artifact validates, all tools allowed (the existing spec
  gate's pass condition, now richer). Citations sync from ledger activity automatically
  (`sync_citations_from_activity` in `scripts/gate/citations.py`); on Stop, `auto_validate_spec`
  runs fresh checks for every open task (including failed; set ``replay_failed`` on
  a task to replay stored output instead), and adjudicates disputed impossibility
  claims (`scripts/gate/spec.py`). Resolved statuses are
  `validated`, `retracted`, and `superseded` (agent tasks replaced by a judge-added requirement
  via `supersedes: [ids]` — non-blocking). Agent-facing CLI: `unifable restate`,
  `unifable add-task`, and `unifable dispute`. A failed task is re-checked
  automatically on the next Stop (no manual retry); fix the cause and stop again.

## PreToolUse block stderr (change-only dedup)

`scripts/gate/pretool_block.py` scopes dedup to one assistant turn (`block_epoch`) and one block
reason (`block_signature` = kind + normalized detail). The first block for a signature emits full
instructions (or `compact_pretool_output` when the unlock footer already went out this turn — cite
lines kept, boilerplate not repeated). Identical retries emit nothing on stderr; exit code 2 alone
signals the block. A new signature in the same turn (e.g. bash whitelist then citation verify) still
emits compact output because the reason changed. When the gate lifts, `consume_gate_cleared_notify`
emits `Gate cleared.` (+ optional hygiene headlines) via PreToolUse `additionalContext` — the
positive transition notify; block counts are not reset on clear (re-block same signature stays silent).

## Delta 1 — broaden the locked surface (pre_tool_use.py)

Today guard 2 only fires for `tool_name in WRITE_TOOLS`. Add an evidence-gate guard that also
fires for `Bash` (classified, below) and treats the spec as the unlock. Reuse `_block()` (exit 2)
and the grade machinery. LIGHT (quick) waives entirely. The gate is unconditional — there is no
env disable.

## Delta 2 — citation fields in the unlock predicate (spec.py SPEC_SCHEMA)

Add, so the spec literally *is* the three evidence types (for **code-profile** tasks):
- `repo_context`: list of `path:line` strings the model read (CODE evidence). Validate each matches
  `\S+:\d+`; optionally existence-check the file. Required >=1 at STANDARD+ for code profile.
- `prior_art`: list of source URLs/repo refs (RESEARCH evidence). Required >=1 at STANDARD+ for code
  profile (external docs fetched via WebFetch/curl -- WebSearch alone does not count).
- `evidence_profile`: `code` (default) or `operational`, set by the grade classifier on
  UserPromptSubmit. **Operational** tasks (account research, draft replies, internal-tool synthesis)
  waive both `repo_context` and `prior_art` at STANDARD+; restated goal + requirement tasks unlock
  edits, and the Stop judge validates task check output.
- `acceptance_criteria[{check, evidence}]` — TOOL-OUTPUT evidence (already present; FAKE_MARKERS enforced).
- `rejected_alternatives` (legacy HEAVY field, removed) — HEAVY now uses frontier approach tasks.
- **HEAVY adoption invariant** — once a frontier is adopted (`comparison_winner` set), the primary
  approach task is always `superseded`. A primary left at `validated` after adoption is harness state
  to self-heal (`ensure_primary_superseded_on_adoption` in `heavy_workflow.py`), not agent work; Stop
  hints must not replay the primary's stale `judge_reason` as if more proof is needed.
Validation extends `validate_spec` (`spec.py:116`) with these; the contract strings
(`spec.py:216-237`) gain the new fields so the model is told exactly what to fill.

## Delta 3 — Bash gating (the novel, risky part)

Classify the `Bash` command string in the research phase:
- ALLOW only if every command segment is `cd`, `ls`, `glob`, `rg`, invokes a file whose basename is
  `trace.sh` or `websearch.sh` (directly or through `sh`/`bash`/`zsh`), or invokes a user-facing unifusion skill
  script (`unifusion.sh`, `save_run.sh`, `summarize_session.sh`, `resolve_session.sh`).
- User-facing allowlist copy (PreToolUse blocks, breaker steering, setup block) is install-detected via
  `scripts/gate/research_bash_guidance.py`: when `SKILL.md` + `scripts/trace.sh` exist under the explore
  skill, messages name installed script paths (`trace.sh`, and `websearch.sh` when present); otherwise
  explore scripts are omitted from guidance. Enforcement remains basename `trace.sh` / `websearch.sh` regardless.
- BLOCK otherwise, with a message that names the allowed commands and says broader Bash unlocks only
  after a valid task spec exists with repo_context citations, acceptance evidence, and prior_art.
- Safety: this is an ALLOWLIST (block-by-default in research phase) — higher false-positive risk than
  wfb's edit-only gate. Mitigations: (a) unconditional + fail-open on malformed input (matches
  `pre_tool_use.py:206-209`); (b) the model always retains a full research toolset, so it is never
  bricked — it can still inspect with `cd`/`ls`/`glob`/`rg`, use `trace.sh` or `websearch.sh`, or run unifusion panel scripts; (c) ship behind the holdout harness
  (`scripts/shadow/` + `UNIFABLE_HOLDOUT=1`) and measure block-rate / false-positive-rate.

## Groundedness breaker — provisional lift

Separate from the evidence spec gate: the overconfidence breaker (`scripts/gate/groundedness.py`)
can arm on an unproven load-bearing claim and block mutations. Three release paths:

- **Full disarm** — release judge finds the claim grounded, retracted, or no longer load-bearing.
- **Provisional lift** — the model is pursuing the verification the breaker requested (reads cited,
  docs fetched, minimal experiment-setup edit) but is not yet fully grounded. Mutations are allowed
  within `lift_scope` (scoped to USER GOAL, including minimal scripts/checks when execution is
  required); the block cap is paused. The hook notifies the model why the lift was granted.
- **FAIL_OPEN** — after `BREAKER_MAX_BLOCKS` consecutive blocks on one arm (default 3).

While provisionally lifted, a monitor judge runs on mutation PreToolUse and the release judge also
runs (so a read/fetch that fully grounds the claim can disarm before the next mutation). **Minor
scope drift** yields an advisory hint only (`Hint (advisory, not a gate): …`) — the lift stays
open. **Egregious** unrelated work re-arms the breaker. Full disarm while lifted still applies when
the claim is grounded (including empirical validation in tool output), retracted, or no longer
load-bearing.

## Judge message size cap (gpt-realtime-2)

The Realtime judge (`scripts/gate/codex_judge.py`) caps each message field at **256,000
characters** (API hard limit). Transcript tails are budgeted in `scripts/gate/transcript_tail.py`
(`JUDGE_TRANSCRIPT_CHAR_BUDGET`, `cap_judge_message`, `fit_judge_user_message`); `ask_structured`
applies the same cap at send time as a backstop. Regression: `tests/test_transcript_tail.py`,
`tests/test_judge_message_cap.py`.

## Advisory judge hints — guidance, never a gate

Verdict paths and proactive nudges are separate:

- **Verdict feedback** — `judge_task` / `judge_dispute` (`scripts/gate/spec.py`) return one `reason`
  string (why evidence failed plus a next step when `verdict=0`). The task board shows it once via
  the inline `judge:` row; `notify_spec_update` and Stop validation emit headline + board only (no
  separate `Judge:` / `Hint:` stderr preamble).
- **Proactive nudge** — threshold-bounded loops call `judge_hint`, which is verdict-free by
  construction (schema returns only `hint`). A hint **never** changes a verdict, changes a task
  status, or opens/lifts the completion breaker. Proactive hints use the distinct
  `UNIFABLE_MODEL_HINT` channel (`scripts/gate/model_notify.py`).

Proactive surfaces, all fail-open (a judge error yields no hint and leaves gate behavior
byte-identical):

- **Stop completion-breaker loop** — once the agent has re-blocked Stop `COMPLETION_HINT_THRESHOLD`
  times (`hooks/gate_stop.py`, counter `completion_stop_blocks` in the ledger), one `judge_hint`
  call appends a nudge to the still-blocking reason. The block is unchanged; only guidance is added.
- **Repeated-failure loop** — when PostToolUse sees the same failure class repeat
  (`hooks/gate_post_tool.py`), the deterministic detection triggers a `judge_hint` call; the
  guidance itself is reasoned by the judge, never a canned string. Silent if the judge has nothing.

The proactive loops are threshold-bounded (mirroring `BREAKER_MAX_BLOCKS`) so they never spend a
judge call per tool. Locked by `tests/test_judge_hint.py` (proactive hints never resolve tasks).

## Stop validation digest — proactive judge surfacing

`build_stop_validate_context` (`scripts/gate/model_notify.py`) packages Stop adjudication for the
model in priority order:

1. **Action required** — full judge reasoning (+ optional `hint:`) for every task ID referenced in
   this stop's headlines (tasks adjudicated this stop).
2. **This stop** — collapsed headline delta (batch loop-release retractions merge to one line).
3. **Board** — incomplete tasks only; stale failures stay one-line rows without replaying prior
   judge essays.

The completion block **`reason`** also appends short `Action:` lines (tasks changed this stop) so
actionable guidance survives host preview truncation. When the digest is truncated, `reason` points
at `last_stop_validation.txt` beside the session spec. The digest is persisted on every Stop
adjudication; it is injected via `additionalContext` only when Stop is **blocked**, not on clean
passthrough. Regression: `tests/test_spec_model_notify.py`,
`tests/test_auto_validate_stop.py`.

## Completion loop lift — judge-adjudicated Stop release (V1)

When the completion breaker traps a session in a suicide loop (Stop re-blocked with no net
progress — same failing tasks, judge-added runaway, repeated rejections), a **separate**
loop-release judge may lift the gate. This is distinct from advisory hints (which never lift)
and from the deterministic stall cap (`COMPLETION_MAX_STALLED_BLOCKS = 6` in
`scripts/gate/verify_state.py`), which remains the hard backstop.

**Trigger** (observable from ledger + spec, in `scripts/gate/loop_release.py`):

- `completion_stall_blocks >= 3`, OR
- `completion_stop_blocks` reaches `COMPLETION_LOOP_JUDGE_THRESHOLD` (default 4), OR
- the same incomplete task set repeats across consecutive blocks, OR
- **requirement fragmentation** — many failed agent tasks plus overlapping pending judge-added
  replacements (`detect_requirement_fragmentation` in `scripts/gate/spec.py`; title collisions or
  >=5 failed tasks with judge backlog).

One judge call per loop episode (debounced; not every Stop).

**Verdicts** (`judge_completion_loop_release`):

- **Provisional** — allow Stop through for 1–3 attempts (`loop_lift_stops_remaining` in the
  ledger) so the agent can change approach; `lift_scope` states allowed next actions.
- **Permanent (V1)** — retract specific **judge-added** spurious requirements (never
  agent-authored tasks); may open the breaker if enough tasks clear.
- **None** — gate unchanged; fail-open on judge error.

**Hook wiring** — `hooks/gate_stop.py` runs loop detection after `auto_validate_spec` and
before the completion block decision: consume provisional budget, invoke loop judge when
triggered, notify via `systemMessage` on allow-stop lifts (no `additionalContext`, which would
re-engage the session). Regression:
`tests/test_loop_release.py`.

## Requirement supersession — converging failed sprawl

When the judge rejects evidence but the requirement itself is wrong (not merely unproven),
it should **revise** the check via `adjust_requirements` rather than add a parallel requirement.
When a genuinely new replacement is needed, `new_requirements` may include optional
`supersedes: [task_ids]`:

- **Agent-authored** targets become `superseded` (`[SS]`) — non-blocking, linked via
  `superseded_by`.
- **Judge-added** duplicates in the bundle are **retracted**.

`_apply_check_result` applies the supersedes bundle before mutating the current task status so
batch Stop adjudication cannot re-fail siblings already superseded in the same pass.
Regression: `tests/test_supersession.py`.

## Rollout

Unconditional (always on, no env disable), fail-open on malformed input. Graded: LIGHT waives;
**code** STANDARD+ = repo_context + prior_art + acceptance/tasks; **operational** STANDARD+ =
restated goal + tasks only (no path:line or URL citations before edits); HEAVY adds frontier workflow.
Both hosts: Claude native hooks + Codex (same `pre_tool_use.py`, both list `apply_patch`). Tests:
extend `tests/test_spec_gate.py` + add a Bash-classifier test (allow/block matrix, malformed,
no-brick), mirroring wfb's 35/35 adversarial set.

## Open forks for the user

1. Bash gating scope: full lockdown now (writes + Bash allowlist, the stated intent, highest leverage,
   highest false-positive risk) vs. writes-first then add Bash gating after measuring.
2. Allowlist contents: currently `cd`, `ls`, `glob`, `rg`, any file named `trace.sh` or `websearch.sh`, and the four
   user-facing unifusion skill scripts by basename.
