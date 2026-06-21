# unifable v2 — implementation plan (26 features from the gap analysis)

Source: `research/fable-harness-gap-analysis.md`. Goal: implement all 26 gaps, dual-host
**plugin-native** (Claude Code plugin + Codex plugin, not skill). Durable/resumable: this file
is the source of truth for status. Update the Status column as work lands.

## Principles
- **Blocking hooks ship always-on** (the evidence/spec gate is unconditional — no env disable,
  fail-open on malformed input), so installing v2 enforces citation-before-action by default.
  Non-gate running hooks (e.g. test-after-edit, memory) ship opt-in (default OFF). Prompt/pack
  rules are always-on (low risk).
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
| 1 | Pre-Edit Enforcement | `hooks/pre_tool_use.py` | hooks.json PreToolUse; merge_hooks | on (unconditional) | done |
| 2 | Spec & Contract Artifacts | `scripts/gate/spec.py` | router/UserPromptSubmit contract inject | on (CLI+gate; prompt-inject opt-in) | done |
| 3 | Grade Tiers | — | classify_task.py grade_of | on | done |
| 4 | Fake-Evidence Detection | (in spec.py) | gate_stop.py uses check_fake_evidence | on | done |
| 5 | Pre-Edit Protected Gate State | (in pre_tool_use.py) | — | with #1 | done |
| 6 | Debounced Test-Runner | `hooks/test_after_edit.py` | hooks.json PostToolUse | OFF (`UNIFABLE_TEST_AFTER_EDIT`) | done |
| 7 | Optional grounding command + verifier agent | — | removed; replaced by enforced evidence and groundedness gates | off | removed |
| 8 | Warning Threshold Accumulation | — | ledger.py + gate_post_tool + gate_stop | on | done |
| 9 | Find-and-Replace Word-Boundary | — | SKILL.md + block | on | done |
| 10 | Confirmed-Before-Flagging | — | SKILL.md + block | on | done |
| 11 | Effort-Gated Playbook Inject | `hooks/gate_prompt_effort.py` | hooks.json UserPromptSubmit | on | done |
| 12 | Findings Ledger | `scripts/gate/findings.py` | gate_stop.py cross-link | on | done |
| 13 | Domain-Specific Verification | `packs/domain-verification.txt` | router.sh signal | on | done |
| 14 | alwaysThinkingEnabled on install | — | install/claude.sh, setup.sh | on | done |
| 15 | Depth Gate & Output Lock | `packs/output-contract.txt` | block + gate_prompt | on | done |
| 16 | Final Response Shape by Depth | — | SKILL.md + block | on | done |
| 19 | Decision Trace Workflow | `packs/decision-trace.txt` | router.sh | on | done |
| 20 | Subagent Brief Files | `packs/subagent-brief.md` | SKILL refs | on | done |
| 21 | Multi-Tier Model Variant Skills | `skills/unifable/tiers/{opus,sonnet,haiku,README}.md` | SKILL.md pointer | on | done |
| 22 | TDD Test Protection Rule | — | SKILL.md + block | on | done |
| 23 | Completion Checklist | `packs/completion-checklist.md` | gate_stop warning ref | on | done |
| 24 | Idempotent Marker Block Upsert | — | setup.sh markers + block | on | done |
| 25 | Behavioral Eval Suite + rubric | `docs/evals/*`, `tests/{eval_rubric.md,run_evals.py}` | — | on | done |
| 26 | Distribution: Claude + Codex plugin | `.codex-plugin/{plugin,hooks}.json` | install/codex.sh (native CLI), README | on | done |

## Phasing (all complete)
- **P2 (parallel agents, new files):** #1+#2+#5 (spec gate), #6, #7, #11, #12, #13+#19+#20+#23 (packs),
  #25 (evals). — done
- **P3 (orchestrator, shared files):** #3, #4, #8, #9, #10, #14, #15, #16, #21, #22, #24 + wire all new
  hooks into hooks.json + merge_hooks.py. — done
- **P4 (packaging):** #26 — Codex is now a NATIVE plugin (`.codex-plugin/plugin.json` ->
  `.codex-plugin/hooks.json` with `${PLUGIN_ROOT}` paths), installed via the supported
  `codex plugin marketplace add jaredboynton/unifable` + `codex plugin add unifable@unifable`
  (`install/codex.sh` reproduces this and migrates off the legacy skill+hooks.json). Claude plugin
  unchanged. — done
- **P5:** full suite + new tests + behavioral evals green; commit; README updated. — done

## Verification (this build)
- Test suites green: `test_classify_ambiguity` 12/12, `test_effort_inject` 14, `test_findings` 25,
  `test_gate_false_positive` 18/18, `test_gate_robustness` 12, `test_gate` 6/6,
  `test_recovery`, `test_shadow*` 3, `test_spec_gate` 31, `test_test_after_edit` 48.
- Codex native plugin: `codex plugin list` shows `unifable@unifable` enabled; cache under
  `~/.codex/plugins/cache/unifable/unifable/`; legacy `~/.codex/skills/unifable` + hooks.json
  entries retired (backed up).
