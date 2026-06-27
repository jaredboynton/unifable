---
name: explore-websearch
description: >-
  External web research via websearch.sh (gpt-realtime-2 + Codex alpha/search
  web_run). Use for up-to-date facts, library/API docs, prior art, version
  checks, and any question that needs the open web with grounded source URLs.
metadata:
  author: Jared Boynton
  version: "0.9.0"
  argument-hint: <research goal>
---

# Explore — Web Research

Run from the repo root. The skill installs to the stable runtime at
`~/.unifable/current/skills/explore-websearch` (refreshed to the newest version
on each session); invoke it there regardless of which CLI or plugin cache is
active:

```bash
~/.unifable/current/skills/explore-websearch/scripts/websearch.sh "<research goal>"
```

Default path is gpt-realtime-2 via `websearch-rt.sh`: two rounds (web_run via
Codex `alpha/search`, then pointer submit) with search reasoning `low` and
submit `minimal`. Requires Codex OAuth (`codex login`) and `curl`.

The implementation is shared with the sibling **explore** skill (deep trace +
fast locate); this skill is the external-research entrypoint. The complementary
codebase tools (`trace.sh`, `search.sh`) live in `explore`.
