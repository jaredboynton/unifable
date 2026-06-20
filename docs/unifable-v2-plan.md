# unifable v2 — implementation plan (26 features from the gap analysis)

Source: `research/fable-harness-gap-analysis.md`. Goal: implement all 26 gaps, dual-host
**plugin-native** (Claude Code plugin + Codex plugin, not skill). Durable/resumable: this file
is the source of truth for status. Update the Status column as work lands.

## Principles
- **New blocking/running hooks ship opt-in (default OFF)** via env/config, so installing v2 never
  suddenly blocks edits or auto-runs tests. Prompt/pack rules are always-on (low risk).
- **File ownership to avoid conflicts:** subagents create only NEW files; the shared core files
  (`hooks/hooks.json`, `skills/unifable/SKILL.md`, top-level `SKILL.md`, `setup/unifable-block.md`,
  `scripts/gate/classify_task.py`, `hooks/gate_stop.py`, `hooks/gate_post_tool.py`,
  `scripts/gate/ledger.py`, `setup/setup.sh`, `install/*`) are integrated by the orchestrator.
- **Host-agnostic hook scripts:** every hook reads stdin JSON and emits JSON/exit-code; host path
  wiring lives in `hooks/hooks.json` (Claude) and `install/merge_hooks.py` (Codex).
- Every code feature ships with a test; the full suite must stay green.

## Module interfaces (so parallel work composes)
- `scripts/gate/ledger.py` DEFAULT_LEDGER gains: `grade:""`, `warning_count:0`, `warnings:[]`.
- `scripts/gate/spec.py`: `SPEC_SCHEMA`, `FAKE_MARKERS`, `validate_spec(spec, grade)->(ok,reasons)`,
  `check_fake_evidence(text)->[markers]`, `spec_path(root,task_id)`, CLI `validate/contract/init`.
- `scripts/gate/findings.py`: `.unifable/findings.json` CRUD; `open_findings(root)->[...]`,
  `blocking_findings(root)->[...]` (high/critical & status==open). CLI add/resolve/reject/list.
- `scripts/gate/classify_task.py`: add `grade_of(mode)->LIGHT|STANDARD|HEAVY` mapping
  quick->LIGHT, normal->STANDARD, deep->HEAVY.
- Hooks read the standard payload `{tool_name,tool_input,tool_response,cwd,session_id,...}`.
  PreToolUse block = exit code 2 + stderr reason (both hosts honor exit 2).

## Feature map

| # | Feature | New files (agent-built) | Shared edits (orchestrator) | Default | Status |
|---|---|---|---|---|---|
| 1 | Pre-Edit Enforcement | `hooks/pre_tool_use.py` | hooks.json PreToolUse; merge_hooks | OFF (`UNIFABLE_SPEC_GATE`) | todo |
| 2 | Spec & Contract Artifacts | `scripts/gate/spec.py` | router/UserPromptSubmit contract inject | OFF | todo |
| 3 | Grade Tiers | — | classify_task.py grade_of | on | todo |
| 4 | Fake-Evidence Detection | (in spec.py) | gate_stop.py uses check_fake_evidence | on | todo |
| 5 | Pre-Edit Protected Gate State | (in pre_tool_use.py) | — | with #1 | todo |
| 6 | Debounced Test-Runner | `hooks/test_after_edit.py` | hooks.json PostToolUse | OFF (`UNIFABLE_TEST_AFTER_EDIT`) | todo |
| 7 | /ground skill + verifier agent | `skills/ground/SKILL.md`, `agents/grounding-verifier.md` | SKILL refs | on | todo |
| 8 | Warning Threshold Accumulation | — | ledger.py + gate_post_tool + gate_stop | on | todo |
| 9 | Find-and-Replace Word-Boundary | — | SKILL.md + block | on | todo |
| 10 | Confirmed-Before-Flagging | — | SKILL.md + block | on | todo |
| 11 | Effort-Gated Playbook Inject | `hooks/gate_prompt_effort.py` | hooks.json UserPromptSubmit | on | todo |
| 12 | Findings Ledger | `scripts/gate/findings.py` | gate_stop.py cross-link | on | todo |
| 13 | Domain-Specific Verification | `packs/domain-verification.txt` | router.sh signal | on | todo |
| 14 | alwaysThinkingEnabled on install | — | install/claude.sh, setup.sh | on | todo |
| 15 | Depth Gate & Output Lock | `packs/output-contract.txt` | block + gate_prompt | on | todo |
| 16 | Final Response Shape by Depth | — | SKILL.md + block | on | todo |
| 17 | Local Semantic Memory Layer | `scripts/memory/*` | — | OFF (opt-in CLI) | todo |
| 18 | Compact Semantic Notes layout | (in scripts/memory) | — | with #17 | todo |
| 19 | Decision Trace Workflow | `packs/decision-trace.txt` | router.sh | on | todo |
| 20 | Subagent Brief Files | `packs/subagent-brief.md` | SKILL refs | on | todo |
| 21 | Multi-Tier Model Variant Skills | `skills/unifable/SKILL.{opus,sonnet,haiku}.md`? | SKILL note | on | todo |
| 22 | TDD Test Protection Rule | — | SKILL.md + block | on | todo |
| 23 | Memory Closure Checklist | `packs/memory-closure.md` | gate_stop warning ref | on | todo |
| 24 | Idempotent Marker Block Upsert | — | setup.sh markers + block | on | todo |
| 25 | Behavioral Eval Suite + rubric | `docs/evals/*`, `tests/eval_rubric.md` | — | on | todo |
| 26 | Distribution: Claude + Codex plugin | `.codex-plugin/` or per research | install/*, README | on | todo (needs codex-plugin research) |

## Phasing
- **P2 (parallel agents, new files):** #1+#2+#5 (spec gate), #6, #7, #11, #12, #13+#19+#20+#23 (packs),
  #17+#18 (memory), #25 (evals).
- **P3 (orchestrator, shared files, sequential + verify each):** #3, #4, #8, #9, #10, #14, #15, #16,
  #21, #22, #24 + wire all new hooks into hooks.json + merge_hooks.py.
- **P4 (packaging):** #26 once codex-plugin research returns; convert Codex skill->plugin, keep Claude plugin.
- **P5:** full suite + new tests + e2e gate probes + behavioral evals green; commit; update README/CHANGELOG.
