# Audit: brittle string-match assertions

Verdict: the suite has a real brittle-assertion problem in **two distinct shapes**.
The worst shape is narrow; the milder shape is widespread. Evidence below is from
`rg` enumeration + Read confirmation over `tests/` on 2026-06-23.

## Shape 1 — prompt-wording assertions (worst; narrow)

Asserting that an LLM judge **system-prompt constant contains a specific English
word**, instead of asserting what the judge **does**. Green means "the word `harness`
is present," not "the breaker arms correctly." Breaks on any rephrase (the code uses
`.lower()` everywhere, so even case drift is tolerated only by luck).

Concentrated in 3 files — confirmed by enumerating every `= X._*SYSTEM` alias
assignment in `tests/` and reading the matched sites. These are the only files that
alias a `_*SYSTEM` prompt into a short var and assert word-membership on it:

| File | Prompt-wording asserts | Evidence |
|---|---|---|
| test_groundedness_breaker.py | ~49 | alias assignments :277,:278,:297,:304,:311,:401,:527,:534,:553,:717,:723,:731; word-membership asserts :269-273,:298-314,:528-549,:718-733; non-alias `called_user[0]` template checks :712-713 |
| test_grade_classify_judge.py | ~16 (8 functions) | `s = go._GRADE_SYSTEM.lower()` :22,:31,:37,:43,:49,:55,:61,:79; asserts :23-63,:80-81 |
| test_classify_ambiguity.py | 2 | :22 `s = go._GRADE_SYSTEM.lower()`; asserts `"hedging language signals research, not quick" in s` and `"uncertainty" in s` (Read-confirmed) |

Excluded (correctly): `test_judge_message_cap.py:116` and `test_judge_prefix_stability.py:38-39`
use `gb._JUDGE_SYSTEM` for **byte-identity** (`==`), a healthy cache-stability contract,
not word-membership.

## Shape 2 — message-field string-matching (milder; widespread)

Asserting `"word" in <stderr|reason|notify|ctx|msg|headline|out>`. This checks that a
block reason / notification mentions a concept ("spec", "breaker", "fetch"). Mostly
defensible — it guards user-visible message content — but it IS the brittleness in
question: reword the gate's stderr and these break.

251 asserts across ~33 files. Heaviest: test_spec_model_notify.py (78),
test_spec_gate.py (46), test_pack_router.py (13), test_hook_token_dedup.py (11),
test_auto_validate_stop.py (11), test_posttool_context_dedup.py (11),
test_spec_state_notifications.py (10).

## Healthy assertions (do not touch)

- Enum / state checks: `status == "validated"`, `decision == "block"`, `grade == "HEAVY"`, `compute_heavy_phase(spec) == "frontier"` (tests/test_evidence_policy.py:45-47, tests/test_heavy_workflow.py:50-247).
- Byte-identity / cache-stability: `called_system_prompt[0] == gb._JUDGE_SYSTEM` (tests/test_groundedness_breaker.py:706).
- Format-prefix: `msg.startswith("Gate cleared.")`, `ctx.startswith("synced 3 cite(s):")`, `ctx.startswith("[unifable:investigation]")` (tests/test_gate_cleared_notify.py:36, tests/test_spec_state_notifications.py:55, tests/test_pack_router.py:63).

## Recommendation

1. Shape 1 (prompt-wording): convert to behavior tests (feed input, assert verdict/decision). The `FakeJudge` / `RoutingJudge` outcome tests already in the suite subsume them. Highest-value cleanup.
2. Shape 2 (message-field): lower priority. Where a check guards a stable reason code, prefer matching an enum/keyword that is itself a contract. Tolerate the rest as intentional user-facing-message guards.
3. Leave enum/status/identity/format checks alone.

## Pilot

`test_grade_classify_judge.py` (smallest, self-contained, ~16 asserts across 8 functions)
is the recommended pilot for the before/after rewrite pattern before touching the larger
`test_groundedness_breaker.py`.
