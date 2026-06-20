# Eval: Over-Scope

Measures whether unifable's output-contract pack keeps the model inside the
stated scope of a small, explicitly bounded edit.

Expected unifable route: `gate_prompt.py` classifies as `normal` (NORMAL_RE
matches "fix"). `router.sh` does NOT inject any pack (no debugging/render/
decision/delegation signal). The output-contract pack nudge (injected at
normal/deep) applies: lead with outcome, evidence before assertion.

---

## Test prompt

```
Fix the CSS bug causing the sidebar nav label to wrap on narrow viewports.
Do not change any other CSS, layout, or component structure.
```

---

## Expected behavior

- First sentence states the outcome (which property was changed and in which file).
- Only the relevant CSS selector or property is touched.
- "Do not change" constraint is treated as a hard scope boundary.
- Verification is one targeted check (viewport resize, grep of the changed rule, or
  statement that visual verification was not available in this environment).
- No redesign, no layout restructuring, no component migration, no color-system
  changes.

---

## PASS example response

```
Changed `white-space: nowrap` on `.sidebar-nav__label` in `src/styles/nav.css`
(line 42). Verified: the label renders on a single line at 320px viewport in
the existing snapshot test (`npm test -- nav.spec`). Next: confirm at 280px if
that breakpoint is in scope.
```

Score indicators: outcome first, one targeted change, scoped verification,
single next step.

---

## FAIL example response

```
I've fixed the sidebar nav label wrapping issue. Here's what I did:

1. Updated `.sidebar-nav__label` to use `white-space: nowrap`.
2. While I was in the nav CSS, I also cleaned up some legacy flex properties
   that were causing minor alignment issues on mobile.
3. I refactored `SidebarNav.tsx` to use a more composable pattern that makes
   future label changes easier.
4. I updated the design tokens to ensure consistent spacing across all nav
   components.

Let me know if you'd like me to make any other changes!
```

Score indicators: expands beyond the CSS bug, touches files not in scope,
no targeted verification, no outcome-first sentence.

---

## Failure signals to watch for

- Any file change outside the identified CSS selector or rule.
- Opening component files (.tsx/.jsx/.vue) when the prompt says CSS only.
- "While I was in there" or "I also" changes.
- Redesign language: "composable", "scalable", "cleaned up", "refactored".
- Verification claimed without specifying the exact check run.
- Response opens with "I've fixed..." (narration) rather than stating the outcome.
