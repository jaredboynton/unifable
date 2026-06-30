We are updating the unifable hook gate implementation. Please review the current repo state and give evidence-backed architecture guidance.

Current objective:
- Add MCP mutation coverage to PreToolUse; hook matchers should include mcp__* while read-like MCP tools stay on the grounding floor.
- Validate whether hooks intercept WebSearch or non-shell non-MCP calls. Live Codex hook probes show catch-all PreToolUse receives webrun, and live Claude probes show catch-all PreToolUse receives WebSearch/WebFetch/Read/ToolSearch, while the production matcher excludes those names unless explicitly listed.
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
- Claude live probes show WebSearch, WebFetch, Read, and ToolSearch are delivered to catch-all PreToolUse/PostToolUse; production unifable does not gate those names because the production matcher excludes them.
- Latest Codex and Claude live runs after adjustments completed with pytest passing and hard_block_mentions=0.
- A regression in Claude structured deny handling was fixed: block helpers now record an internal blocked marker so Claude's required exit-0 deny output is not misinterpreted as an allow path and followed by a second `{}` output.
- The first post-edit Unifusion rerun used an Opus Bedrock root orchestrator and produced no usable result before we interrupted it; the active Unifusion root default was changed to GPT-5.5 with WebSearch enabled while keeping Opus as a panelist.
- The next Unifusion rerun returned 4/4 panelists and recommended retaining the current architecture, with extra hardening.
- Follow-up hardening from that guidance was implemented: broader MCP mutation classification tests (`get_or_create`, `list_and_purge`, GraphQL mutation, SQL mutation, body/payload/value/script payloads), exact single-JSON Claude deny full-path tests across spec/bash/delegation/protected blocks, Codex `apply_patch` rc=2/stderr coverage, protected-path redirects for allowed commands such as `find`, `pytest`, and `rg | sed`, and explicit matcher-sync assertions that Factory/Devin plugin manifests currently have no hook wiring.
- Full `just test-all` initially exposed that some Stop-output tests were accidentally using Codex shaping because the runner exports `CODEX_THREAD_ID`; those Claude-specific tests are now pinned with `UNIFABLE_HOST=claude`, and `just test-all` passes.

Please identify any remaining architectural gaps, risky assumptions, missing tests, and whether this design is the best current approach for the stated objective.
