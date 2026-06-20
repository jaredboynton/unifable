# unifable

A harness that makes Opus (or any Claude/Codex model) behave like **Fable** — completion,
evidence, and verification enforced as *procedure*, auto-routed per task. **One codebase,
two hosts:** Claude Code (as a plugin marketplace) and Codex (as a skill + hooks).

unifable is a fork of [`fivetaku/fablize`](https://github.com/fivetaku/fablize) (MIT). It unifies
the Claude-Code plugin and the Codex port into a single installable repo and fixes the
PostToolUse false-positive failure gate (see [Why this fork](#why-this-fork)).

## What it does

A harness cannot raise a model's ceiling; it makes the model reach its own ceiling by turning
verification, completion, and investigation into procedure. The same gate scripts run on both
hosts via Claude-Code-compatible hooks:

| Hook | Script | Role |
|---|---|---|
| UserPromptSubmit | `router.sh`, `gate_prompt.py`, `fable-inject.sh` (Codex) | Route task signal to a pack; classify task mode |
| PostToolUse | `gate_post_tool.py` | Observe evidence: changed files, verification results, **real** failures |
| Stop | `gate_stop.py`, `finish-the-work.sh` | Completion verification gate; promise-no-act guard |

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

Codex has no plugin marketplace, so a script copies the skill and merges the hooks
into `~/.codex/hooks.json` (non-unifable hooks are preserved; re-runnable):

```bash
git clone https://github.com/jaredboynton/unifable ~/__devlocal/unifable
bash ~/__devlocal/unifable/install/codex.sh
```

This installs `~/.codex/skills/unifable`, adds the UserPromptSubmit / PostToolUse / Stop entries,
removes any legacy `~/.codex/skills/fablize`, and injects the operating block into `~/.codex/AGENTS.md`.
Trust the new hooks via `/hooks` on next launch.

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
