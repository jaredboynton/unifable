#!/usr/bin/env bash
# unifable UserPromptSubmit router — when a task signal is detected, inject the relevant pack discipline as context.
# Routing: smallest matching pack only / overlap only when genuinely multi-category / mimic observable behavior only.
# Only verified packs are auto-routed.
# stdin: JSON {"prompt": "..."}. stdout: extra context (only when a signal matches). Always exits 0.
set -uo pipefail

# Plugin root: prefer the runtime-injected var, else fall back to this script's location.
ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
PACKS="$ROOT/packs"

prompt="$(python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("prompt",""))
except Exception: pass' 2>/dev/null || true)"
[ -z "${prompt:-}" ] && exit 0
low="$(printf '%s' "$prompt" | tr '[:upper:]' '[:lower:]')"

emit=""
add() { emit="${emit:+$emit
}$1"; }

# Debugging / root-cause → investigation-protocol
case "$low" in
  *debug*|*bug*|*error*|*traceback*|*"stack trace"*|*crash*|*failing*|*"not working"*)
    add "[unifable:investigation] Debugging/root-cause signal — follow $PACKS/investigation-protocol.txt: reproduce first, form 3+ competing hypotheses, gather evidence per hypothesis, trace the full causal chain, verify before/after, and report the hypotheses you rejected." ;;
esac
# Render/executable artifacts → verification-grounding
case "$low" in
  *html*|*svg*|*game*|*canvas*|*chart*|*render*|*website*|*webpage*)
    add "[unifable:grounding] Render/executable artifact signal — follow $PACKS/verification-grounding-pack.txt grounding loop: run it in the real renderer, observe the actual output, fix what the observation reveals, then re-run. A static check is not observation." ;;
esac
# Decision / design / trade-off → decision-trace
case "$low" in
  *decide*|*design*|*architecture*|*"trade-off"*|*tradeoff*|*approach*|*"which option"*|*choose*|*options*)
    add "[unifable:decision-trace] Decision/design signal — follow $PACKS/decision-trace.txt: Orient, Inspect, Decide, Act, Verify, Report; record what you checked and why at each step." ;;
esac
# Implementation / research / data → domain-verification recipes
case "$low" in
  *implement*|*build*|*feature*|*refactor*|*research*|*summarize*|*claims*|*sources*|*data*|*sql*|*dataset*|*analysis*|*pipeline*)
    add "[unifable:domain-verify] Use the failable-check recipes in $PACKS/domain-verification.txt for this domain (software / research / data): every acceptance criterion needs a check that can fail." ;;
esac
# Subagent delegation → subagent-brief template
case "$low" in
  *subagent*|*delegate*|*spawn*|*dispatch*|*parallelize*|*"in parallel"*|*orchestrate*)
    add "[unifable:subagent-brief] Delegating — use the brief template in $PACKS/subagent-brief.md: objective, context, constraints (incl. 'Do not touch: protected tests'), strict output contract, and the verification the worker must run." ;;
esac

# Emit as a JSON object — NOT raw text. Codex's UserPromptSubmit parser treats any
# stdout starting with '[' or '{' as "looks like JSON" and FAILS the hook if it does
# not parse ("hook returned invalid user prompt submit JSON output"); our pack lines
# start with "[unifable:...]". Wrapping in hookSpecificOutput.additionalContext parses
# cleanly on Codex AND is honored by Claude. Empty match -> no stdout (safe on both).
if [ -n "$emit" ]; then
  CTX="$emit" python3 -c 'import os, json
print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": os.environ.get("CTX", "")}}))'
fi
exit 0
