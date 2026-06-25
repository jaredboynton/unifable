---
name: explore
description: >-
  Deep behavioral codebase trace via trace.sh (gpt-realtime-2). Use for
  multi-file flow and how/why questions with grounded code citations.
metadata:
  author: Jared Boynton
  version: "0.9.0"
  argument-hint: <question>
---

# Explore

Run from the repo root:

```bash
~/.agents/skills/explore/scripts/trace.sh "<question>"
```

Requires `node`, `rg`, and Codex OAuth (`codex login`). Run `~/.agents/skills/explore/scripts/setup.sh` once to verify deps.

## External research (websearch)

```bash
~/.agents/skills/explore/scripts/websearch.sh "<research goal>"
```

Default path is gpt-realtime-2 via `websearch-rt.sh`: two rounds (web_run via Codex `alpha/search`, pointer submit) with search reasoning `low` and submit `minimal`. Requires Codex OAuth and `curl`.

| Entry | Backend |
|---|---|
| `websearch.sh` | Default RT delegate (2-round alpha + pointer submit) |
| `websearch-rt.sh` | Direct gpt-realtime-2 + Codex alpha/search |

**Tools:** `search.sh` (fast locate, gpt-realtime-2), `map.sh` (repo prefetch), `trace.sh` / `trace-rt.sh` (deep trace, gpt-realtime-2), `websearch.sh` / `websearch-rt.sh` (external research, gpt-realtime-2).

Superseded variants (cursor/gemini/grok trace, gemini websearch, Cerebras search), benchmarks, and probes are retired and live under `scripts/archive/` — not maintained, not part of the supported path.
