We are updating the unifable hook gate implementation. Please review the current repo state and give evidence-backed architecture guidance.

Current objective:
- Add MCP mutation coverage to PreToolUse; hook matchers should include mcp__* while read-like MCP tools stay on the grounding floor.
- Validate whether hooks intercept WebSearch or non-shell non-MCP calls. Live Codex hook probes show catch-all PreToolUse receives webrun, while the production matcher excludes it.
- Keep Codex and Claude completion breaker caps default-infinite.
- Strengthen protected-path Bash tests for heredoc, tee, sed/perl in-place, redirects, and repo-local .unifable variants before widening Bash allowlist.
- Add Claude structured permissionDecision:"deny" output in the pretool block path while preserving Codex exit-2/stderr compatibility.
- Validate through real tuistory integration probes using Codex gpt-5.5 medium and Claude Code haiku.
- If models run into hard blocks or loops, adjust steering and allowlists so hard blocks are fallback only.

Recent implementation direction:
- PreToolUse matchers now include mcp__.*.
- MCP read-like tools are allowed; MCP mutations go through protected-path/spec gates.
- Tool scope treats read-like MCP as grounding-floor and apply_patch as an Edit/Write alias.
- Bash research allowlist was widened only after protected-path tests: read-only find, sed -n, pytest -q, unifable help/version, and pipeline sed sinks.
- Pretool block output now emits Claude structured permissionDecision:"deny" JSON for Claude and keeps Codex rc=2/stderr.
- A tuistory live harness in probes/probe_hook_integration.py creates a Python fixture, installs project-local hooks plus a catch-all logger hook, and runs Codex/Claude.
- Codex live probes show webrun is delivered to catch-all PreToolUse/PostToolUse, proving hosts can deliver non-shell non-MCP web-like tool events when matched; production unifable does not gate webrun because the production matcher excludes it.
- Latest Codex live run after adjustments completed with pytest passing and hard_block_mentions=0.

Please identify any remaining architectural gaps, risky assumptions, missing tests, and whether this design is the best current approach for the stated objective.
