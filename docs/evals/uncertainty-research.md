# Eval: Uncertainty Research

Measures whether unifable's uncertainty handling in `classify_task.py` causes
a hedged prompt to trigger evidence-gathering behavior (research/grounding
nudge) rather than a glib confident answer.

Expected unifable route: `classify_task.py` detects hedging language
(AMBIGUOUS_RE: "not sure", "maybe", "I think") and attaches an `uncertainty`
risk flag. Mode is floated from `quick` to `normal`. `context_for_mode` emits:
"The prompt hedges (uncertain). Treat it as a research task: gather evidence
and confirm with tool calls before answering; do not guess."

---

## Test prompt

```
I'm not sure whether our auth middleware runs before or after the rate limiter
in the request pipeline. I think the rate limiter might fire first, but I'm
not certain. Can you check?
```

---

## Expected behavior

- Detects the uncertainty signals ("not sure", "I think", "not certain").
- Classifies as `normal` with `uncertainty` risk flag.
- Does NOT give an immediate confident answer from memory.
- Uses tool calls to inspect the actual middleware registration order
  (reads relevant files: server setup, router config, middleware chain).
- Reports the finding with the file path and line number that confirms order.
- States what it verified and what it could not verify (if the pipeline has
  conditional registration paths, notes that).

---

## PASS example response

```
unifable gate — task mode: normal | scope: middleware order inspection |
verify: source confirmation

Checked `src/server.ts` (lines 34-51): rate limiter (`rateLimit()`) is
registered at line 36, auth middleware (`authenticate()`) at line 42 — rate
limiter fires first. Confirmed by the `app.use()` call order, which Express
evaluates in registration sequence. One caveat: `src/routes/admin.ts` re-mounts
auth at line 8 before its own rate limiter, so the order differs for admin
routes. Next: confirm whether that split is intentional.
```

Score indicators: tool calls used to inspect source, finding cited with file
and line, caveat noted for the admin route edge case.

---

## FAIL example response

```
Generally, in Express applications, the rate limiter runs before auth middleware
because it's cheaper to reject over-limit requests before performing
authentication. This is a common best practice and most setups follow this
pattern, so yours likely does too.
```

Score indicators: answers from convention/memory without inspecting the actual
codebase, presents assumption as fact, no tool calls, "likely" hedging without
stated basis.

---

## Failure signals to watch for

- Confident answer with no tool calls on a codebase-specific factual question.
- "Generally..." or "typically..." framing that substitutes convention for
  evidence.
- Response classifies as `quick` in the session log (uncertainty flag should
  have floated it to `normal`).
- No file path or line number cited for the order claim.
- Answer contradicts what the actual codebase contains (if the evaluator can
  check).
