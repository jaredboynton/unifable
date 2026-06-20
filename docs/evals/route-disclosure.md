# Eval: Route Disclosure

Measures whether the gate's task-mode line appears for normal/deep work and
stays absent for quick direct-answer work.

Expected unifable route: `gate_prompt.py` emits `additionalContext` containing
"unifable gate — task mode: <mode>." for every classified prompt. The question
is whether this line surfaces in the model's visible response at the right
depths, and is absent at L0 (quick, conversational).

Two sub-scenarios are tested: one that should show a route line (normal/deep),
one that should not (quick).

---

## Sub-scenario A: Normal implementation (route line expected)

### Test prompt

```
Add a search box to the Settings page and run the relevant integration tests.
```

### Expected behavior

- Classifies as `normal` (NORMAL_RE: "add", "run").
- A compact route line appears early in the response, before implementation.
- Route line includes at minimum: task mode, key constraint (scope), and
  verification plan.
- The line does not expose hypothesis scratchpad or internal deliberation.

### PASS example

```
unifable gate — task mode: normal | scope: Settings page search box | verify: integration tests

Changed `SettingsPage.tsx` to add `<SearchBox>` and wired `onSearch` to the
existing filter hook. Ran `jest --testPathPattern=settings.integration` — 14
passed, 0 failed. Next: confirm search state persists on page reload if that
is in scope.
```

### FAIL example

```
I'll start by exploring the codebase to understand the Settings page structure...

[Read tool: src/pages/SettingsPage.tsx]
[many tool calls]

Here's what I did: [long summary without any route line]
```

### Failure signals

- No compact route line for a multi-step task involving file changes.
- Route line appears but is buried after implementation prose.
- Route line exposes chain-of-thought ("First I considered X, then Y").

---

## Sub-scenario B: Quick direct answer (route line must be absent)

### Test prompt

```
What is the difference between a mutex and a semaphore? Answer in two sentences.
```

### Expected behavior

- Classifies as `quick`.
- No route line in the response.
- Answer is 1-3 sentences.
- No tool calls.

### PASS example

```
A mutex is an exclusive lock that allows only one holder at a time and is
typically owned by the thread that acquired it. A semaphore is a signaling
mechanism with a counter that can allow N concurrent holders and does not
require the same thread to release it.
```

### FAIL example

```
unifable gate — task mode: quick | scope: conceptual explanation

A mutex is...
```

(Route line present for a quick answer is incorrect.)

### Failure signals

- Route line appears in a quick/direct-answer response.
- Two-sentence limit is violated with extra context, headers, or code examples.
- Response opens with "I'll explain..." or "Let me break this down".
