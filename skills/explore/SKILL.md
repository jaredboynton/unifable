---
name: explore
description: >-
  Deep behavioral codebase trace via trace.sh, plus fast code-locate via
  search.sh (both gpt-realtime-2). Use for multi-file flow and how/why questions
  with grounded code citations, or to locate where something lives.
metadata:
  author: Jared Boynton
  version: "0.9.0"
  argument-hint: <question>
---

# Explore

Run from the repo root. The skill installs to the stable runtime at
`~/.unifable/current/skills/explore` (refreshed to the newest version on each
session); invoke it there regardless of which CLI or plugin cache is active:

```bash
~/.unifable/current/skills/explore/scripts/trace.sh "<question>"
```

Requires `node`, `rg`, and Codex OAuth (`codex login`). Run
`~/.unifable/current/skills/explore/scripts/setup.sh` once to verify deps.

## Fast code-locate (search)

```bash
~/.unifable/current/skills/explore/scripts/search.sh "<natural-language query>"
```

`search.sh` is an agentic ripgrep loop with gpt-realtime-2 as the brain — fast
locate, the complement to `trace.sh`'s deep behavioral understanding.

**Tools:** `search.sh` (fast locate, gpt-realtime-2), `map.sh` (repo prefetch),
`trace.sh` / `trace-rt.sh` (deep trace, gpt-realtime-2).

External research lives in the sibling **explore-websearch** skill
(`websearch.sh`).

Superseded variants (cursor/gemini/grok trace, gemini websearch, Cerebras
search), benchmarks, and probes are retired and live under `scripts/archive/` —
not maintained, not part of the supported path.
