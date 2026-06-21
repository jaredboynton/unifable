# Fable Harness Gap Analysis

unifable (a fork of fivetaku/fablize) is an observation and completion enforcement layer for Claude Code and Codex: it classifies tasks at UserPromptSubmit, records tool-result evidence via PostToolUse, and blocks Stop when a non-quick task changed files without observed verification. This document synthesizes a multi-repo comparison run on 2026-06-20 (workflow wh8baw0jp, Haiku discovery + Sonnet fan-out analysis) that diffed unifable against eight public Fable-replication harness repositories to surface techniques unifable does not yet implement.

---

## TL;DR — top gaps

1. Debounced real test-runner PostToolUse hook (auto-discover + run project tests after each edit) — HalalifyMusic/fable-mode — high applicability / medium effort
2. Pre-edit enforcement gate (block Edit/Write until a spec artifact exists and passes) — SihyeonJeon/why-was-fable-banned — high applicability / large effort
3. Structured spec artifact with eight decision axes (.wfb/spec.json schema) — SihyeonJeon/why-was-fable-banned — high applicability / large effort
4. Evidence-ledger /ground skill + cold grounding-verifier sub-agent — HalalifyMusic/fable-mode — high applicability / small-medium effort
5. Warning threshold accumulation (halt at 3 minor concerns, surface all at once) — HalalifyMusic, mrtooher, Nam-Cheol/codex-fable-mode (3 repos) — high applicability / small effort
6. Find-and-replace word-boundary safety rule (prompt-level, zero hook changes) — HalalifyMusic, mrtooher, Nam-Cheol (3 repos) — high applicability / small effort
7. Confirmed-before-flagging rule for self-critique (no finding without a tool call) — HalalifyMusic, mrtooher, Nam-Cheol (3 repos) — high applicability / small effort
8. Effort-gated playbook injection with per-session dedup — HalalifyMusic/fable-mode — high applicability / small effort
9. Explicit findings ledger with severity, lifecycle, and Stop-gate cross-link — parktaeyang/claude-fable-harness — high applicability / medium effort
10. Domain-specific verification patterns (software, research, data) — Nam-Cheol/codex-fable-mode — high applicability / medium effort

---

## Corpus

| Name | URL | What it is |
|---|---|---|
| SihyeonJeon/why-was-fable-banned | https://github.com/SihyeonJeon/why-was-fable-banned | Pre-edit gate that blocks all Edit/Write until a deterministic spec artifact passes; enforces decision records for every change |
| fivetaku/fablize | https://github.com/fivetaku/fablize | Claude Code plugin that makes Opus behave like Fable; unifable's upstream fork source |
| AAO-SH/fable-harness | https://github.com/AAO-SH/fable-harness | Project-local agent harness skill for traceable AI workflows; semantic memory, decision traces, subagent briefs |
| parktaeyang/claude-fable-harness | https://github.com/parktaeyang/claude-fable-harness | Korean harness implementation; independent findings ledger with severity/lifecycle, Stop-gate keyed to open findings |
| mcpe500/fable-harness | https://github.com/mcpe500/fable-harness | Empty repository (size 0, no commits); no techniques extractable |
| HalalifyMusic/fable-mode | https://github.com/HalalifyMusic/fable-mode | Leaked Fable 5 system prompt + measured execution playbook derived from 1,307 Fable-5 / 10,470 Opus-4.8 turns; debounced test-runner hook; /ground skill + cold grounding-verifier agent |
| mrtooher/fable-mode | https://github.com/mrtooher/fable-mode | Claude skill enforcing multi-stage planning, sub-agent delegation, and self-verification; three model-tier variants |
| Nam-Cheol/codex-fable-mode | https://github.com/Nam-Cheol/codex-fable-mode | Documentation-only Codex skill; typed Output Lock, four-level Depth Gate, Procedure/Tool/Delegation budgets, Visible Route Contract, 30-doc reference library, eval suite with scoring rubric |

---

## Gap Analysis

### Pre-Edit Enforcement

**What it is.** A PreToolUse hook that intercepts every Edit / Write / MultiEdit / NotebookEdit / apply_patch call and exits with code 2 — blocking the write — until a structured spec artifact exists and passes a deterministic validator. The model cannot author implementation code until the spec gate clears.

**Who does it.** SihyeonJeon/why-was-fable-banned. Evidence: `adapters/hooks/pre_tool_use.py`: `if run_gate('validate', '--root', root, '--gate', 'spec')[0] != 0: return 2`.

**unifable status.** No. unifable's gate_post_tool.py is a passive observer activated after tools run; gate_stop.py enforces at completion. There is no mechanism that blocks a first edit.

**Applicability.** High. **Effort.** Large. **Recommendation.** Add a PreToolUse hook (hooks/pre_tool_use.py) that checks for a per-task spec file in .unifable/spec/ before allowing edits. Can start with a lighter variant — require only a restated_goal and acceptance_criteria field — and build toward wfb's full eight-axis schema. Gate is unconditional (always on, no env disable), graded: LIGHT waives, STANDARD+ enforces.

---

### Spec and Contract Artifacts

**What it is.** A mandatory JSON artifact the model must author before editing. SihyeonJeon/wfb defines eight decision axes: restated_goal (must differ from raw ask), non_goals, ambiguities, must_read, constraints, rejected_alternatives (>=2 with broken boundary), risks (severity by blast radius + runnable mitigation), acceptance_criteria (runnable commands only). Separately, a grade-specific "contract" (pass-conditions for the current grade tier) is injected as additionalContext at task start to eliminate reactive bounce. wfb measured a 4.53x to 2.99x gross token ratio improvement from this injection.

**Who does it.** SihyeonJeon/why-was-fable-banned. Evidence: `gates/wfb_gate.py` lines 48-64 (SPEC_TEMPLATE), `adapters/codex/spec.schema.json`, `adapters/hooks/user_prompt_submit.py` lines 113-118 (contract injection), `TOKEN_BUDGET.md` (token ratio measurement).

**unifable status.** No. The UserPromptSubmit router.sh injects pack context based on task signal, but produces no per-task structured spec artifact and no grade-specific contract injection.

**Applicability.** High. **Effort.** Large. **Recommendation.** Implement as a two-phase addition: (1) a `goals.py spec` subcommand that writes a .unifable/spec/<task-id>.json using a simplified three-field schema (restated_goal, acceptance_criteria, risks); (2) extend the UserPromptSubmit router to inject the acceptance_criteria as additionalContext when a spec file already exists for the current task.

---

### Grade Tiers

**What it is.** Three-tier complexity classification: LIGHT (typo/comment/rename — only restated_goal + 1 acceptance check), STANDARD (full spec), HEAVY (auth/payments/migration/security — adds architectural constraints, similar_implementations, observation loop at done). Grade is auto-detected from prompt keywords and propagates to spec requirements and contract injection content.

**Who does it.** SihyeonJeon/why-was-fable-banned. Evidence: `gates/wfb_gate.py` HEAVY_RE, STANDARD_RE, NONTRIVIAL_VERB_RE; `FABLE_PROCEDURE.md` SPEC exit gate section.

**unifable status.** Partial. unifable classifies quick / normal / deep (three tiers) via gate_prompt.py but these control gate activation, not a spec schema or contract content. There is no LIGHT tier that waives spec requirements for trivial changes.

**Applicability.** High. **Effort.** Medium. **Recommendation.** Map quick/normal/deep onto LIGHT/STANDARD/HEAVY semantics. For quick (LIGHT): waive spec requirement; for deep (HEAVY): add an architectural constraints field to the spec schema and require rejected_alternatives count >= 2.

---

### Fake-Evidence Detection

**What it is.** At completion, each acceptance_criteria item must carry an evidence field containing live command output. A FAKE_MARKERS tuple (`'not run'`, `'assumed'`, `'would pass'`, `'TBD'`, `'pending'`, etc.) is checked and any match blocks the done gate as fabricated.

**Who does it.** SihyeonJeon/why-was-fable-banned. Evidence: `gates/wfb_gate.py` FAKE_MARKERS tuple and `gate_done()`; `tests/test_wfb_gate.py` test_fake_evidence_blocks.

**unifable status.** Partial. gate_stop.py has REAL-failure signal detection and repeating-failure disclosure, but no semantic check for placeholder strings in evidence fields.

**Applicability.** High. **Effort.** Small. **Recommendation.** Add a FAKE_MARKERS tuple check in gate_stop.py or a companion verify_state script. When the model's final stop contains any marker from the list in an evidence-adjacent context, surface a block reason with the matching string.

---

### Pre-Edit Protected Gate State

**What it is.** The PreToolUse hook blocks any edit to gate-state files (.wfb/GRADE, .wfb/ACTIVE, .wfb/edits.txt, .wfb/STATE, .wfb/sessions/). The model may write only .wfb/spec.json. A patch touching both spec and state files is rejected as a whole.

**Who does it.** SihyeonJeon/why-was-fable-banned. Evidence: `adapters/hooks/pre_tool_use.py` PROTECTED_PATHS list; `tests/test_wfb_gate.py` test_model_cannot_edit_state.

**unifable status.** No. unifable has no protection on its .unifable/ state directory.

**Applicability.** High. **Effort.** Small. **Recommendation.** Add a PROTECTED_PATHS list in a PreToolUse hook covering .unifable/ledger.jsonl, .unifable/goals.json, and .unifable/state/. Block and emit a human-readable reason if the model attempts to write those paths directly.

---

### Debounced Real Test-Runner PostToolUse Hook

**What it is.** After each Edit / Write / MultiEdit, test-after-edit.py auto-discovers the project's test runner (Node / Python / Rust / Go / Make) from the directory tree, skips doc/asset extensions, debounces per project root to avoid thrash, enforces a per-run timeout, and injects pass/fail output as additionalContext into the session. The measured data shows test-after-edit is not reliably fixable by intention alone: Fable-5 ran tests after edits at 91% vs Opus-4.8 at 83%.

**Who does it.** HalalifyMusic/fable-mode. Evidence: `hooks/test-after-edit.py` full file; `FABLE_PLAYBOOK.md` lines 10-28 (metrics table).

**unifable status.** No. gate_post_tool.py passively records whether the model ran a verification command; it does not trigger any test suite proactively.

**Applicability.** High. **Effort.** Medium. **Recommendation.** Port test-after-edit.py into unifable as `hooks/test_after_edit.py`. Wire it as a PostToolUse hook in hooks/hooks.json with matcher `Edit|Write|MultiEdit`. gate_post_tool.py's verification_record can then ingest the injected pass/fail as an observed result. The two hooks are complementary: test_after_edit triggers, gate_post_tool observes.

---

### Evidence-Ledger /ground Skill and Cold Grounding-Verifier Sub-Agent

**What it is.** The /ground skill defines a structured evidence ledger (VERIFIED / UNVERIFIED columns), a six-step loop with an explicit termination criterion (empty UNVERIFIED column), a fork-classification decision policy (code-determinable: decide it; preference: surface it), and spawns a cold grounding-verifier sub-agent. The grounding-verifier (`agents/grounding-verifier.md`) is read-only, adversarial, receives only the ledger and diff (never the author's reasoning), and performs row-check + gap-scan + summary-fidelity check before GO / NO-GO. It breaks the self-grading loop that packs alone leave intact.

**Who does it.** HalalifyMusic/fable-mode. Evidence: `skills/ground/SKILL.md` (101 lines); `agents/grounding-verifier.md` (53 lines); `FABLE_PLAYBOOK.md` lines 237-268 (always-on light grounding section).

**unifable status.** No. unifable has verification-grounding-pack.txt (run in real renderer -> observe -> fix -> re-run) and investigation-protocol.txt, but no /ground skill artifact, no evidence ledger table format, no termination test, and no cold second observer agent.

**Applicability.** High. **Effort.** Small (grounding-verifier: pure agent definition file) to Medium (/ground skill). **Recommendation.** Create `agents/grounding-verifier.md` by porting fable-mode's 53-line file (read-only tools declaration: Read, Glob, Grep, Bash). Create `skills/ground/SKILL.md` with the ledger format, five moves, termination test, and fork policy. Reference both from the unifable SKILL.md and operating block for any hard-to-reverse change.

---

### Warning Threshold Accumulation

**What it is.** A running counter of minor concerns across a multi-stage run that halts and surfaces all accumulated warnings when the count reaches 3. Prevents minor concerns from being individually dismissed and lost.

**Who does it.** HalalifyMusic/fable-mode, mrtooher/fable-mode, Nam-Cheol/codex-fable-mode (3 repos, high frequency signal).

**unifable status.** No. gate_stop.py blocks on missing verification in deep mode; there is no warning accumulator for low-severity signals.

**Applicability.** High. **Effort.** Small. **Recommendation.** Add a `warning_count` field to ledger.py DEFAULT_LEDGER. Increment it in gate_post_tool.py when the model emits a concern pattern but does not block. In gate_stop.py, surface all accumulated warnings as a non-blocking summary when count >= 3. Alternatively, encode the rule in unifable-block.md so the model self-tracks inline.

---

### Find-and-Replace Word-Boundary Safety Rule

**What it is.** An explicit operational rule in SKILL.md: use `\bword\b` anchors with sed, then grep for malformed compound words after any replacement pass.

**Who does it.** HalalifyMusic/fable-mode, mrtooher/fable-mode, Nam-Cheol/codex-fable-mode (3 repos, all SKILL.md variants including Opus / Sonnet / Haiku tiers).

**unifable status.** No. No equivalent rule exists in unifable's packs, SKILL.md, or operating block.

**Applicability.** High. **Effort.** Small. **Recommendation.** Add a 'find-and-replace safety' paragraph to unifable's SKILL.md and setup/unifable-block.md. No hook changes required; purely prompt-level addition.

---

### Confirmed-Before-Flagging Rule for Self-Critique

**What it is.** Before flagging any problem in self-review, confirm it exists via grep / diff / run / source check. Absence of evidence is not a finding. Prevents the model from manufacturing warnings to appear thorough.

**Who does it.** HalalifyMusic/fable-mode, mrtooher/fable-mode, Nam-Cheol/codex-fable-mode (3 repos).

**unifable status.** No. investigation-protocol.txt covers reproduce-first for debugging but there is no equivalent rule governing the pre-delivery self-review step.

**Applicability.** High. **Effort.** Small. **Recommendation.** Add a 'before delivery' working-style rule to SKILL.md and unifable-block.md: "Before flagging a problem in self-review, confirm it with a tool call. Absence of evidence is not a finding."

---

### Effort-Gated Playbook Injection with Per-Session Dedup

**What it is.** UserPromptSubmit reads the hook's `effort.level` field (or CLAUDE_EFFORT env), injects the full playbook as additionalContext only when effort is in {xhigh, max, ultracode}, and suppresses re-injection within the same session via a /tmp marker keyed to session_id. Avoids paying 12 KB per prompt while ensuring the playbook lands on high-effort work.

**Who does it.** HalalifyMusic/fable-mode. Evidence: `hooks/fable-trigger.py` lines 26-75; HEAVY_EFFORT = {'xhigh', 'max', 'ultracode'} at line 23; marker pattern at lines 51-59.

**unifable status.** No. fable-inject.sh (Codex) injects a fixed 7-line directive on every prompt unconditionally. gate_prompt.py classifies mode but does not gate on the effort field.

**Applicability.** High. **Effort.** Small. **Recommendation.** Add a second UserPromptSubmit hook (hooks/gate_prompt_effort.py, ~30 lines) that reads the effort field and, when effort in {xhigh, max, ultracode}, injects the unifable SKILL.md body as additionalContext with a per-session marker at /tmp/unifable-loaded-{session_id}.

---

### Findings Ledger with Severity, Lifecycle, and Stop-Gate Cross-Link

**What it is.** A standalone per-project JSON findings store (.claude-fable-harness/findings.json). Each finding carries id, title, severity (low/medium/high/critical), source, location, evidence, status (open/blocked/resolved/rejected), resolution, verify_cmd, verify_evidence. Findings move open->resolved (with evidence) or open->rejected (with reason). The Stop hook reads this file and emits `{"decision":"block"}` while any finding is open/blocked; if the file does not exist the hook is a no-op (opt-in activation). The final goal in the goals engine is cross-blocked by open findings.

**Who does it.** parktaeyang/claude-fable-harness. Evidence: `/tmp/fz-parktaeyang_claude_fable_harness/scripts/fable_findings.py` lines 1-218; `hooks/fable_stop_gate.py` lines 75-77 (no-op if file absent), lines 90-96 (override env), lines 100-126 (round limit + progress reset); `scripts/fable_goals.py` lines 74-84 (blocking_findings()), lines 183-187 (die if blockers on final goal complete).

**unifable status.** Done. Implemented at `scripts/gate/findings.py` (a severity-rated open/blocked/resolved/rejected findings store) and wired into `gate_stop.py` (`blocking_findings()` blocks Stop while any high/critical finding is open). It is opt-in exactly as recommended: a no-op when `.unifable/findings.json` is absent, so ordinary sessions are unaffected.

**Applicability.** High. **Effort.** Medium. **Recommendation (shipped).** Added `scripts/gate/findings.py` mirroring parktaeyang's fable_findings.py; gate_stop.py checks for .unifable/findings.json and calls blocking_findings() before allowing Stop. Opt-in: the hook is a no-op if the file does not exist, preserving ordinary-session behavior.

---

### Domain-Specific Verification Patterns

**What it is.** Concrete failable-check recipes for three domains missing from unifable's existing packs: software engineering (tests alongside implementation, error paths, not just happy path), research/knowledge work (every load-bearing claim traces to a source actually read), data analysis (null/duplicate checks before computing). These instantiate the general "verify before done" instruction into domain-actionable steps.

**Who does it.** Nam-Cheol/codex-fable-mode. Evidence: domain-pattern references throughout SKILL.md.

**unifable status.** Partial. unifable has investigation-protocol.txt (debugging) and verification-grounding-pack.txt (render artifacts). Both are narrow; neither covers research claims, data analysis null checks, or error-path coverage for software.

**Applicability.** High. **Effort.** Medium. **Recommendation.** Add `packs/domain-patterns.txt` covering the three domains as concrete failable-check recipes. Wire into router.sh with keyword signals: research/summarize/claims -> domain-patterns research section; sql/data/analysis -> data section; implement/build/fix -> software section (extend existing investigation-protocol routing). Alternatively, embed as a 'Domain patterns' section in SKILL.md.

---

### alwaysThinkingEnabled Settings Enforcement on Install

**What it is.** install.sh explicitly merges `alwaysThinkingEnabled: true` into ~/.claude/settings.json as part of setup, ensuring extended thinking is on without relying on user configuration.

**Who does it.** HalalifyMusic/fable-mode. Evidence: `shell/install.sh` lines 32-52.

**unifable status.** No. install/claude.sh and setup/setup.sh do not touch this setting. The operating block says "Reasoning effort scales with difficulty automatically" but provides no mechanical enforcement.

**Applicability.** High. **Effort.** Small. **Recommendation.** In setup/setup.sh and install/claude.sh, add a Python snippet that merges `alwaysThinkingEnabled: true` into ~/.claude/settings.json with a backup. Three lines of Python; idempotent write.

---

### Depth Gate and Output Lock System

**What it is.** A four-level Depth Gate (L0 conversational through L4 hard-to-reverse) assigns matching Procedure / Tool / Delegation budgets (P0-P4, T0-T4, A0-A3). A nine-category typed Output Lock pins the response form (prose / code / analysis / plan / comparison / structured-data / visualization / summary / dialog). A Visible Route Contract line is published for L2+ work so the intent interpretation is auditable before implementation starts.

**Who does it.** Nam-Cheol/codex-fable-mode. Evidence: SKILL.md full file; depth-gate, output-lock, and route-contract references throughout.

**unifable status.** Partial. unifable classifies quick/normal/deep and routes to packs, but has no Output Lock categories, no Procedure/Tool/Delegation budget tiers, and no Visible Route Contract disclosure.

**Applicability.** High. **Effort.** Medium. **Recommendation.** Extend context_for_mode in the operating block to emit a Visible Route Contract for deep/normal tasks: "Route: [task type] | Depth: L[n] | Output: [form]". Add Output Lock guidance to SKILL.md (nine categories as a reference list). Budget tiers (P/T/A) can be added as prompt-level rules without script changes.

---

### Final Response Shape by Depth Level

**What it is.** Per-level structure prescriptions for the final response: L0 (no process summary), L1 (cause + fix + verification), L2 (changed + checks + assumptions), L3 (intent interpretation + selected direction + trade-off + changed + verification + remaining assumptions), L4 (confirmed + not changed + risks + required user decisions + next step).

**Who does it.** Nam-Cheol/codex-fable-mode. Evidence: SKILL.md level-specific response templates.

**unifable status.** Partial. The always-on block says "Lead with the outcome" and "Ground completion claims in tool results" but prescribes no per-level structure.

**Applicability.** Medium. **Effort.** Small. **Recommendation.** Add per-level final-response shape guidance to context_for_mode. Map unifable's quick/normal/deep to L0-L1 / L2 / L3-L4 and emit the appropriate template as part of the mode injection.

---

### Local Semantic Memory Layer (Vector Search, Knowledge Graph, Shards)

**What it is.** memory_core.py builds 64-dim hash sparse vectors, shards them into .codex/memory/shards/*.jsonl, maintains a knowledge graph (nodes.jsonl + edges.jsonl with 'mentions' and 'sourced_by' edges), and a manifest.json. memory-search.py runs before broad file exploration to recall relevant prior notes by cosine + term-overlap scoring.

**Who does it.** AAO-SH/fable-harness. Evidence: `installer.py` memory_core.py installation; `scripts/memory-search.py`; `scripts/rebuild-memory.py`.

**unifable status.** No. ledger.py tracks per-session tool observations; there is no persistent cross-session note store or retrieval index.

**Applicability.** High. **Effort.** Large. **Recommendation.** Add a memory/ layer under .unifable/. Port or adapt AAO-SH's memory_core.py (vector(), build_memory(), search_memory()). Wire memory-search.py into the UserPromptSubmit router before pack injection. Use rebuild-memory.py after goals.py story checkpoints.

---

### Compact Semantic Notes with Category/Area/Topic Layout

**What it is.** Per-project note storage at .codex/notes/<category>/<area>/<topic>.md with YAML frontmatter (status, layer, sources, last_verified). _index.md auto-rebuilds on every write via update_notes_index().

**Who does it.** AAO-SH/fable-harness. Evidence: `installer.py` MEMORY_CLOSURE_TEMPLATE; update_notes_index() in memory_core.py.

**unifable status.** No. unifable has only global ~/.unifable/ledgers/ for per-session gate state; no project-local note storage.

**Applicability.** High. **Effort.** Medium. **Recommendation.** Introduce a notes/ directory under .unifable/. Add new-note.py as a thin wrapper around memory_core.create_note. Wire note creation into goals.py checkpoint: on complete checkpoint with evidence, prompt the agent to distill evidence into a semantic note.

---

### Decision Trace Workflow (Orient/Inspect/Decide/Act/Verify/Report)

**What it is.** new-trace.py creates a timestamped .md file at .codex/decision-traces/<stamp>-<slug>.md using the OIDAVR template and appends to _index.md. promote-trace.py extracts Decide/Verify sections and writes a compact semantic note, logging to memory/promotion_log.jsonl. check-closure.py fails when any trace with a Decide section has not been promoted, preventing agents from re-reading verbose raw traces in future sessions.

**Who does it.** AAO-SH/fable-harness. Evidence: `installer.py` new-trace.py, promote-trace.py, check-closure.py installation targets.

**unifable status.** No (new-trace, promote-trace) / Partial (check-closure). goals.py writes .unifable/goals.json + ledger.jsonl for multi-story state but produces no human-readable per-task audit trace. gate_stop.py enforces a different invariant (files changed -> verification ran).

**Applicability.** High. **Effort.** Medium. **Recommendation.** Add new-trace.py to scripts/ for single-task audit capture. goals.py could auto-create a trace on 'create' and update it on each checkpoint. Add promote-trace.py as a goals.py companion; after the final story verification gate passes, gate_stop.py could check promotion_log.jsonl for unpromoted Decide traces and emit an advisory block.

---

### Subagent Orchestration with Dispatchable Brief Files

**What it is.** subagent-plan.py generates per-domain brief .md files with explicit scope (Read / Edit / Do-not-touch), protected tests list, expected status codes, and verification command. subagent-result.py writes a structured result .md and back-links it into the dispatch table and active trace.

**Who does it.** AAO-SH/fable-harness. Evidence: `installer.py` SUBAGENT_BRIEF_TEMPLATE; subagent-plan.py and subagent-result.py installation targets.

**unifable status.** No. SKILL.md mentions reactive effort delegation but provides no script, file format, or dispatch manifest for it.

**Applicability.** Medium. **Effort.** Large. **Recommendation.** Add subagent-plan.py and subagent-result.py to unifable's scripts/ directory. Wire subagent-plan.py invocation into goals.py 'next' when a story objective contains subagent keywords. The dispatch manifest + result files give the orchestrator a durable record across session restarts, complementing the existing goals.json resume.

---

### Multi-Tier Model Variant Skills

**What it is.** Three SKILL.md files (opus/default, sonnet, haiku) that pin the same loop to different model tiers and spawn the appropriate subagent. Each variant adapts Procedure/Tool/Delegation budgets for the model tier.

**Who does it.** mrtooher/fable-mode, Nam-Cheol/codex-fable-mode. Evidence: multiple SKILL.md files in skills/ directories; model-tier references in README.

**unifable status.** No. unifable has a single SKILL.md targeting any model. There are no Sonnet or Haiku variant skills.

**Applicability.** Medium. **Effort.** Medium. **Recommendation.** Create skills/unifable-sonnet/SKILL.md and skills/unifable-haiku/SKILL.md mirroring fable-mode's variant structure. The escalation section (§4) already describes reactive effort delegation; the variant skills formalize the inverse (delegation to cheaper models for high-volume work).

---

### TDD Test Protection Rule

**What it is.** Operating rules explicitly forbid weakening, skipping, renaming, deleting, or rewriting pre-implementation TDD tests. The only valid replacement is an equally strict corrected test. Wired into AGENTS.md block and subagent brief template.

**Who does it.** AAO-SH/fable-harness. Evidence: `installer.py` harness_block() TDD line; SUBAGENT_BRIEF_TEMPLATE 'Do not touch: Protected tests'; `SKILL.md` §'Common Mistakes'.

**unifable status.** No. unifable's packs do not include a TDD protection rule.

**Applicability.** Medium. **Effort.** Small. **Recommendation.** Add a TDD protection rule to unifable's SKILL.md and unifable-block.md. One paragraph; no hook changes.

---

### Memory Closure Checklist Template

**What it is.** memory-closure.md is a 7-item checklist covering durable decisions, verified commands, TDD tests, stale notes, index links, back-links, and residual risks. Referenced as a pre-completion artifact.

**Who does it.** AAO-SH/fable-harness. Evidence: `installer.py` MEMORY_CLOSURE_TEMPLATE.

**unifable status.** Partial. gate_stop.py enforces verification behaviorally but there is no human/agent-readable checklist artifact that can be opened and filled before reporting done.

**Applicability.** Medium. **Effort.** Small. **Recommendation.** Add a memory_closure_template to unifable's packs/ directory (or as a template written by setup.sh). Reference it in gate_stop.py's warning message when verification is missing.

---

### Idempotent Marker-Comment Block Upsert in AGENTS.md/CLAUDE.md

**What it is.** HTML comment markers `<!-- fable-harness:start -->` / `<!-- fable-harness:end -->` enable idempotent block replacement on re-install without duplicate injection.

**Who does it.** AAO-SH/fable-harness. Evidence: `installer.py` block injection logic.

**unifable status.** Partial. setup.sh checks for block presence by string ('unifable') but has no marker-delimited replace-between-markers pattern for re-install idempotency.

**Applicability.** Low. **Effort.** Small. **Recommendation.** Add markers `<!-- unifable:start -->` / `<!-- unifable:end -->` around the injected block in setup/unifable-block.md and update setup.sh to use a sed/python replace-between-markers pattern on re-install.

---

### Behavioral Evaluation Suite with Scoring Rubric

**What it is.** Prompt-specific eval files with annotated expected route lines, pass/fail examples, failure-signal descriptions, and a multi-dimension scoring rubric for measuring whether prompt discipline changed model behavior in the intended direction.

**Who does it.** Nam-Cheol/codex-fable-mode. Evidence: evals/ directory (~5 files); TEST_PROMPTS.md (scoring rubric, 9 dimensions).

**unifable status.** Partial. unifable has automated Python gate tests (tests/test_gate.py, test_gate_robustness.py, test_gate_false_positive.py, test_recovery.py, test_shadow*.py) covering the gate machinery. There is no human-facing behavioral eval suite with route-disclosure checks or a scoring rubric for prompt-discipline behavior.

**Applicability.** Medium. **Effort.** Medium. **Recommendation.** Create docs/evals/ with eval files modeled on fable-mode's pattern: over-scope, output-drift, tool-bloat, grounding-stop-gate, route-disclosure, delegation, renderable-verification. Add a scoring rubric covering the relevant dimensions as a manual regression harness for the prompt-discipline side.

---

### Dual-Registry npm + PyPI Packaging

**What it is.** A single CI workflow with a mandatory verify job (Python tests, Node entry point check, npm pack dry-run, twine check) before publish; then separate publish-npmjs (with --provenance) and publish-pypi (via twine) jobs.

**Who does it.** AAO-SH/fable-harness. Evidence: `.github/workflows/publish-package.yml`; `pyproject.toml`; `bin/fable-harness.mjs`.

**unifable status.** No. unifable distributes as a Claude plugin + Codex skill. There is no npm or PyPI package, no versioned release artifact, and no CI publish pipeline.

**Applicability.** Low. **Effort.** Large. **Recommendation.** If distribution beyond the local machine is a goal, add pyproject.toml + package.json on the fable-harness pattern. The .claude-plugin/marketplace.json already covers Claude Code distribution; the main gap is Codex/npm users who cannot use the plugin marketplace.

---

## What unifable Already Does Well

- **PostToolUse observation ledger**: gate_post_tool.py records every changed file, verification command, and result; detects REAL-failure signals and repeating-failure patterns that expose silent failures the model would otherwise suppress.
- **Stop gate with file-change / verification coupling**: gate_stop.py enforces the invariant that a non-quick task that changed files must have an observed successful verification before completion is allowed. Blocks completion on missing evidence without requiring a spec artifact upfront.
- **Multi-story goals loop with session resume**: goals.py persists brief / goal / evidence / verify_cmd across sessions in .unifable/goals.json; the final story requires --verify-cmd and --verify-evidence, providing a durable multi-session completion gate.
- **Shadow measurement layer**: scripts/shadow provides A/B measurement and holdout comparison for evaluating gate impact on real sessions — a capability absent from all repos in the corpus.
- **Dual-host packaging**: the same harness ships as both a Claude Code plugin (marketplace.json) and a Codex skill + hooks, covering both runtimes from a single codebase.
- **Task-signal routing**: router.sh dispatches to investigation-protocol.txt or verification-grounding-pack.txt based on prompt signals, injecting the narrowest relevant constraint pack rather than loading all packs on every task.

---

## Provenance

- **Workflow ID**: wh8baw0jp
- **Models used**: Haiku (corpus discovery + repo fetching), Sonnet (per-repo fan-out analysis)
- **Date run**: 2026-06-20
- **Repos analyzed**: 8
- **Unreachable repos**: 0 (all 8 returned valid HTTP responses; mcpe500/fable-harness was reachable but empty — size 0, no commits, no files)
- **Effective repos with extractable techniques**: 7
