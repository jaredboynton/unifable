# unifable

A harness that makes Opus (or any Claude/Codex model) behave like **Fable** — completion,
evidence, and verification enforced as *procedure*, auto-routed per task. **One codebase,
two hosts:** Claude Code and Codex, each as a **native plugin** (own manifest + own hooks).

unifable is a fork of [`fivetaku/fablize`](https://github.com/fivetaku/fablize). It unifies
the Claude-Code plugin and the Codex port into a single installable repo and fixes the
PostToolUse false-positive failure gate (see [Why this fork](#why-this-fork)).

## What it does

A harness cannot raise a model's ceiling; it makes the model reach its own ceiling by turning
verification, completion, and investigation into procedure. The same gate scripts run on both
hosts via Claude-Code-compatible hooks:

| Hook | Script | Role |
|---|---|---|
| UserPromptSubmit | `router.sh`, `gate_prompt.py`, `gate_prompt_effort.py` | Route task signal to a pack; classify task mode; effort-gated playbook |
| PostToolUse | `gate_post_tool.py` | Observe evidence: changed files, verification results, **real** failures |
| Stop | `gate_stop.py`, `finish-the-work.sh` | Completion verification gate; promise-no-act guard |

The **Fable orchestrator posture** (delegate-first) is delivered as always-on context loaded once
per session, not re-injected per prompt: on Claude via the **Fable output style**
(`output-styles/fable.md`, set by `install/claude.sh`), on Codex via an `AGENTS.md` block
(`setup/orchestrator-block.md`, injected by `install/codex.sh`).

Shared core lives in `scripts/gate/` (ledger, task classifier, tool-result parser, verify-state)
and `packs/` (investigation protocol, verification-grounding). Multi-story work is tracked by
`scripts/goals.py` with state under `./.unifable/`.

## Why this fork

Upstream's observation gate decided "tool failure" by grepping tool output for the bare words
`failed` / `failure` / `error:` whenever no structured exit code was present. On **Codex** the shell
`tool_response` is a plain output string with *no* `exit_code`
([`context.rs` `post_tool_use_response`](https://github.com/openai/codex)), so the gate fell back to
that grep and fired on every successful command whose output merely *contained* those words —
`cat`, `grep`, a passing `12 passed, 0 failed` summary — spamming:

> fablize gate observed a tool failure. Do not report completion until it is fixed...

unifable's `scripts/gate/parse_tool_result.py` is signal-first: a failure is asserted only from a
structured `exit_code`/`success`/`status` signal, or — when the host gives none — from a
high-precision anchored marker (`Traceback`, `command not found`, `panicked at`, a non-zero
`exit code N`, `N failed`/`N errors`, never `0 failed`). Locked in by
`tests/test_gate_false_positive.py`.

## Install — Claude Code

```
/plugin marketplace add jaredboynton/unifable
/plugin install unifable@unifable
```

Then (optional always-on operating block):

```bash
bash "${CLAUDE_PLUGIN_ROOT}/setup/setup.sh" global   # or: local
```

Hooks register automatically from `hooks/hooks.json` on install.

## Install — Codex

Codex loads unifable as a **native plugin** (`.codex-plugin/plugin.json` → `.codex-plugin/hooks.json`,
`${PLUGIN_ROOT}` paths). The supported path mirrors Claude's `/plugin`:

```bash
codex plugin marketplace add jaredboynton/unifable
codex plugin add unifable@unifable
```

`install/codex.sh` reproduces this non-interactively and **migrates off** any legacy install: it
registers the marketplace, installs + force-enables `unifable@unifable`, then retires the old
`~/.codex/skills/unifable` copy and strips the old unifable entries from `~/.codex/hooks.json`
(both backed up). Optional always-on operating block: prefix with `UNIFABLE_BLOCK=1`.

```bash
git clone https://github.com/jaredboynton/unifable ~/__devlocal/unifable
bash ~/__devlocal/unifable/install/codex.sh
```

Restart Codex; the plugin loads its own hooks. Verify with `codex plugin list`.

## More capabilities

Beyond the gate, unifable ships: a `/ground` skill + cold `grounding-verifier` agent for
hard-to-reverse changes; an opt-in pre-edit spec/contract gate (`UNIFABLE_SPEC_GATE=1`) and
debounced test-runner (`UNIFABLE_TEST_AFTER_EDIT=1`); a findings ledger and warning-threshold
accumulation; per-task **grade tiers** and a depth-shaped final response; per-model posture files
under `skills/unifable/tiers/`; a local semantic memory CLI (`scripts/memory/store.py`); routing
packs for domain verification, decision traces, subagent briefs, and memory closure; and a
behavioral eval suite (`docs/evals/`, `tests/eval_rubric.md`).

## Tests

```bash
python3 tests/test_gate.py                 # completion-gate scenarios
python3 tests/test_gate_robustness.py      # loop-guard / no-false-nag
python3 tests/test_gate_false_positive.py  # the fork's regression
python3 tests/test_recovery.py
```

## Credits & license

Based on [`fivetaku/fablize`](https://github.com/fivetaku/fablize) by fivetaku
(`gptaku.ai@gmail.com`); the methodology and gate design are theirs. unifable adds dual-host
packaging and the failure-detection fix.

The upstream repository ships **no license** (all rights reserved), so this is a **private
personal fork — not for redistribution**. Make it public only after obtaining explicit licensing
terms from the upstream author.
