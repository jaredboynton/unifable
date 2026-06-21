# Evidence Protocol adherence — root-cause diagnosis

Question: why do Codex (GPT) and Claude read but under-follow unifable's Evidence Protocol —
(1) cite CODE evidence (file:line) for decisions, (2) cite TOOL OUTPUTS for truth claims,
(3) cite CURRENT RESEARCH / PRIOR ART (URLs) for architecture decisions — Codex worse than Claude?

Method: 6-agent fan-out (A1-A3 mapped the repo, B1-B3 surveyed external SOTA), each claim cited
to file:line or URL; the load-bearing internal claims were re-verified by the orchestrator directly.

Evidence tier legend: [V] orchestrator-verified in this session · [A] agent-reported with file:line ·
[X] external research (cited URL, reported by agent, not independently re-run).

## The five root causes, ranked

### RC1 (primary) — The Evidence Protocol is advisory text with ZERO detection or enforcement
- [V] No gate detects, requires, scores, or blocks on any citation behavior. The only hard
  Stop-blocking gate is `verify_state.py:30-49` (`should_block_stop`), which checks three ledger
  booleans — `task_mode==deep` AND `changed_files_seen` AND one `verification_results` entry with
  `success==True`. Its docstring states the decision is made "purely from observed ledger state —
  never from the assistant's claim text" (`verify_state.py:5-7`). It is a *verification-ran* gate,
  not a *citation* gate.
- [V] `goals.py:99` validates `--evidence` as a non-empty string only (`a.evidence and a.evidence.strip()`);
  no content/format check. Final-story `--verify-cmd`/`--verify-evidence` also non-empty-only (`goals.py:102-103`).
- [A] The three behaviors live only as advisory injected prose: `output-contract.txt:4` (evidence-before-assertion),
  `decision-trace.txt:20` (paste command + output), `orchestrator-block.md:4` (cite docs/research URL),
  `unifable-block.md:6` (ground claims in this session's tool results).
- [X] Advisory instructions yield probabilistic, not deterministic, compliance; multi-instruction
  adherence drops up to 61.8% under task load (IFEval++, Dec 2025, https://arxiv.org/pdf/2601.03269).
  Consequence: the ONE behavior backed by a hard gate (verification-ran) survives; the citation
  behaviors, advisory-only, are dropped first under load. This is the central mechanism behind
  "models read it but don't do it."

### RC2 — Even as text, several citation rules are un-checkable / aspirational
- [A] Most citation instructions are imperative and measurable in principle (`A2` extract), BUT key
  ones are vague: "ground completion claims in this session's tool results" has no threshold
  (`unifable-block.md:6`); "directly confirms" is undefined (`ground/SKILL.md:14`); "I saw it work"
  has no detectable signal (`verification-grounding-pack.txt:17`).
- Implication: you cannot gate what you cannot detect. Moving citation from advisory to enforced
  requires first re-phrasing each rule as a failable predicate (a script must be able to find a violation).

### RC3 — Identical text shipped to both hosts, tuned to neither; GPT's mechanics penalize it more
- [V] Zero GPT-specific phrasing. `router.sh:23-46` injects identical pack paths regardless of host;
  `.codex-plugin/hooks.json` and `hooks/hooks.json` run the same Python gates.
- [X] GPT and Claude follow instructions differently. GPT enforces a 4-level hierarchy
  root>system>developer>user, where developer-role rules outrank user/project context
  (https://model-spec.openai.com/2025-12-18.html); GPT responds best to terse, outcome-oriented
  developer-role rules and to constrained decoding (https://developers.openai.com/api/docs/guides/structured-outputs).
  Claude responds to XML-tagged "named contract" sections and role framing, with documented 20-40%
  consistency gains from XML structure
  (https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices).
- On Codex the protocol arrives as `~/.codex/AGENTS.md` project context [A `install/codex.sh:130-161`],
  i.e. lower-authority than a developer message, and GPT's "keep working until done" throughput bias
  sheds procedural overhead like citation. Identical untuned prose leaves compliance on the table for
  BOTH and for GPT more so.
- Correction to fan-out: A3's "Codex lacks edit/write observation" is imprecise — [V] Codex's
  PostToolUse matcher `^(Bash|apply_patch)$` (`.codex-plugin/hooks.json:30`) DOES cover apply_patch,
  Codex's native edit tool, so file-change observation is preserved. A3's "Codex Stop is advisory-only /
  cannot block" cites no repo evidence and is UNVERIFIED — do not build on it.

### RC4 — A latent contradiction in the prompt actively penalizes citation
- [V] The Output Contract is heavily conciseness-weighted: "Lead with the outcome" + "No narration of
  internal reasoning" + "Reserve markdown" + QUICK = "1 to 3 lines, no evidence table"
  (`output-contract.txt:7-9,17-21`). Its rule 4 ("evidence before assertion", `output-contract.txt:10`)
  is in tension with rules 1-3, and nothing states that citation is exempt from the conciseness rule.
- [X] Contradiction removal is documented as the single highest-impact lever for GPT-5 adherence
  (https://developers.openai.com/cookbook/examples/gpt-5/gpt-5_prompting_guide). When "be concise"
  and "cite everything" both fire with no reconciliation, the model optimizes the louder/more-repeated
  conciseness signal and drops citations. Fixable by an explicit carve-out: "citations are evidence,
  not narration; always required; exempt from the conciseness rule."

### RC5 — No model-appropriate delivery or recency placement
- [X] "Lost in the middle" is confirmed across GPT/Claude/Gemini: mid-context instructions suffer
  30%+ accuracy drop (Liu et al. 2024; Chroma 2025, https://arxiv.org/pdf/2510.10276). unifable's
  citation rules sit mid-block, not at the top/bottom, and are not re-injected near generation.
- [X] Identity framing ("you are an agent that always cites evidence") measurably shifts behavior but
  decays without re-injection (https://arxiv.org/pdf/2512.12775). Penalty/threat framing BACKFIRES via
  reward hacking — models game the appearance of compliance (https://lilianweng.github.io/posts/2024-11-28-reward-hacking/).
  Self-judging inflates 10-25% via self-preference (https://galileo.ai/blog/llm-as-a-judge-vs-human-evaluation),
  so any LLM-judge gate must be cross-model.

## Fix space (SOTA-grounded), constrained by what unifable actually controls

unifable is a hooks + injected-text harness on top of Claude Code and Codex CLI. It CAN: inject text at
UserPromptSubmit; block at Stop (Claude confirmed honors `decision:block`; Codex unverified); block at
PreToolUse; ship a Claude output-style and a Codex AGENTS.md block. It CANNOT directly set the model
API's `response_format`/json_schema or the system-vs-developer role split for an already-running CLI —
so "constrained decoding for a citations array" (B1/B2/B3's strongest generic lever) is only partially
reachable (via a hook-detectable evidence block, not true token-masking).

Highest-leverage, in-control moves:
- F-A [text, low risk, both hosts] Resolve the RC4 contradiction + re-phrase citation rules as failable
  predicates (RC2). Example contract clause: "Every claim about what works / passed / is true is
  immediately followed by `path:line` or a `cmd -> output` or a URL; an uncited claim is labeled
  `(assumption)`. Citations are evidence, not narration — never omit them for brevity."
- F-B [gate, high leverage, invasive] Add a Stop-side citation gate: for deep/decision tasks, detect
  whether the response (or ledger) carries the required evidence signal (file:line / `cmd->output` / URL);
  block-with-reminder if missing. NOTE: gate_stop deliberately does NOT read response text today, and the
  repo was previously burned by a false-positive nag removed for firing on ~1/3 of deep turns
  (`verify_state.py:42-45`). So F-B needs the shadow/holdout measurement harness (docs/MEASUREMENT_PROTOCOL.md)
  before it ships on by default.
- F-C [text, low risk] Per-host tuning: a `<citation_rules>` XML contract at the top of the Claude
  output-style; a terse developer-style citation rule placed first in the Codex AGENTS.md block; both at
  top/bottom, never mid-block (RC3, RC5).
- F-D [hook, low risk] Identity framing + recency re-injection: have the prompt router add a one-line
  citation reminder for deep/decision tasks, exploiting recency. No penalty/threat language (RC5).
- F-E [optional, cost] Cross-model LLM-judge citation check in the eval harness only (not inline), to
  measure compliance; never self-judge.

## Bottom line
The gap is architectural, not a wording slip: the Evidence Protocol is the one major discipline unifable
asks for but never gates, while everything it DOES gate (verification-ran, promise-no-act, findings) it
enforces. Citation behavior therefore degrades exactly as advisory instructions do under load — worse on
GPT because the identical prose is delivered as lower-authority context, untuned to GPT's hierarchy, and
sits in unresolved tension with the conciseness rules. The fix is to (1) make citation a failable,
gated predicate, (2) resolve the conciseness-vs-citation contradiction, and (3) deliver the rule in each
model's preferred form at a high-salience position — measured via the existing holdout harness before
defaulting on.
