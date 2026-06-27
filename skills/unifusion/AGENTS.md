# unifusion — agent notes

Maintainer notes for editing the **unifusion** skill itself. The runtime contract the calling model
follows lives in `SKILL.md`; the panel/judge policy lives in `references/`. This file is for an agent
changing the scripts or the skill's structure. Do not duplicate `SKILL.md` here.

## What it is

Fanned-out multi-model panel + synthesis. The orchestrator (Opus 4.8, Claude Code) writes the user's
question to a temp file, then runs ONE script — `scripts/unifusion.sh` — which auto-detects every panelist
CLI and fans the SAME prompt to all of them **in parallel, blind, and clean-room** (Opus via the `cb`
Bedrock CLI; external models via `scripts/run_*.sh`). Opus then judges every answer and writes the final
deliverable. Opus is always the judge and is never one of the panelist processes, so the pipeline can't be
reversed. Claude Code is the only runtime (no Codex/Cursor variant here).

The single-entrypoint design is deliberate: detection, the session brief, prompt assembly, and the per-CLI
fan-out used to be separate manual steps with slugs/pins to choose; now `unifusion.sh` does all of it and
the caller only judges + saves. "Always use all available," automatically.

## Architecture

Plain bash + two interpreter helpers; no build step.

- `scripts/unifusion.sh <question_file> [run_dir]` — THE entrypoint. Detects panelist CLIs (cb/codex/agy/
  kimi/glm), builds the best-effort session brief, assembles the one canonical prompt
  (`panel_prompt.md`), fans every available panelist out as parallel background jobs into
  `<run_dir>/<label>_out.md` (always Opus via `cb`; a 2nd `cb` if no external CLI → the `opus4.8-4.8`
  fallback), waits, and prints a manifest (`RUN_DIR=`, `PANEL_PROMPT=`, `CONTEXT=`, `SLUG=`, one
  `PANELIST <label> <ok|dropped:reason> <out>` line each, `ESTIMATE=`). Never judges, never gates, always
  exits 0. Folds in the old `detect_panel.sh` + `preflight.sh` (both removed).
- `scripts/run_cb.sh` — Opus 4.8 panelist via `cb -p --model opus --output-format text` (stdin
  prompt). Isolated `CLAUDE_CONFIG_DIR` with live standard user hooks (from `~/.claude/settings.json`),
  no plugins, `fastMode`, plus `--mcp-config` Exa only (`--strict-mcp-config`). The `cb` wrapper
  auto-adds `--dangerously-skip-permissions` for web+bash. Override model via `UNIFUSION_OPUS_MODEL`.
- `scripts/resolve_session.sh [--path|--id|--json] [--fingerprint-file <f>]` — host-agnostic resolver:
  walks process ancestry to the host agent (claude/codex/droid/glm), reads its session id (env/argv/
  session store), maps id→transcript path, and uses the fingerprint (the verbatim question) to disambiguate
  among cwd candidates and verify the pick. Unresolved → non-zero (fail closed).
- `scripts/summarize_session.sh <out>` → resolver → `scripts/compact-full-transcript.mjs` — best-effort
  factual session brief. The shim resolves the transcript via `resolve_session.sh --path --fingerprint-file
  /tmp/unifusion_question.txt`, runs the summarizer with `--transcript <path> --provider gemini`, then strips
  forward-looking sections so the shared brief stays factual-only. Exit 3 (no transcript) / 4 (no key) /
  6 (failed) → orchestrator skips injection.
- `scripts/compact-full-transcript.mjs` — multi-provider (codex/gemini/xai/mantle) transcript summarizer
  using **schema-constrained structured output** (`responseMimeType` + JSON schema, no function calling).
  Transcript source precedence: `--input` > `--transcript`/`UNIFUSION_TRANSCRIPT` > `--session` (Claude-only);
  no source → exit 3. A content guard exits 3 if the transcript yields no citable text before any API call.
  Two foreign-format adapters feed the native Claude pipeline: `codexPayloadText` makes Codex `.payload`-shaped
  records citable/renderable, and `atifToClaudeJsonl` converts a Devin ATIF-v1.4 JSON document (`steps[]`)
  into Claude-shaped JSONL records (`sha256`/`bytes` still hash the original file). So Claude, Codex, Droid,
  Devin, and GLM transcripts all summarize end-to-end. Writes a bundle to `--out-dir`; the brief is
  `<out-dir>/summary.md`. Vendored from **patchpress**; keep the four provider dispatch paths in sync
  when edited. Dependency modules in the same directory: `tool-use-format.mjs`, `handoff-density.mjs`,
  `prompt-adaptation.mjs`, `renderer-prompt-guides.mjs`.
- `scripts/run_codex.sh` (GPT-5.5), `run_gemini.sh` (Gemini 3.5 Flash via the standalone `gemini` CLI),
  `run_kimi.sh` (Kimi K2.7), `run_glm.sh` (GLM-5.2 via `glm-acp-agent` ACP) — one external panelist each.
  `run_agy.sh` is the preserved Antigravity (`agy`) baseline of the Gemini panelist (the pty bug-#76 path
  + transcript fallback), kept for side-by-side comparison; the orchestrator still launches `run_gemini.sh`.
- `scripts/_acp_client.mjs` — minimal ACP (Agent Client Protocol) stdio client that drives `glm-acp-agent`
  through the JSON-RPC protocol (initialize → authenticate → session/new → session/set_mode →
  session/prompt → session/close). Collects streamed `agent_message_chunk` text and writes it to stdout.
  Called by `run_glm.sh`.
- `scripts/_unifusion_lib.sh` — sourced by the runners; `have()`, `_has_content`, panel config builders
  (`_unifusion_write_cb_panel_settings`, `_unifusion_write_codex_panel_config`,
  `_unifusion_write_gemini_panel_settings`), and `_run_with_timeout`
  (perl fork+alarm, since stock macOS has no `timeout`/`gtimeout`). The child is exec'd as its own
  **process-group leader** and the deadline/signal handler kills the whole group, so panelist helper children
  (codex MCP servers, kimi's `kimi-code` worker) are reaped instead of orphaned. `UNIFUSION_TIMEOUT` default
  600s.
- `scripts/_pty_run.py` — runs `agy` under a fresh pty (`pty.fork`) to dodge agy bug #76 (empty stdout
  with no TTY) while surviving a socket stdin (headless/cmux).
- `scripts/save_run.sh` — writes the provenance `.md` under `~/.claude/unifusion-runs/` only. Accepts a
  single `<run_dir>` 5th arg and auto-discovers `*_out.md` (mapping cb_out→opus-A, cb_out_b→opus-B,
  codex_out→gpt5.5, gemini_out→gemini3.5flash, kimi_out→kimi2.7, glm_out→glm5.1), or an explicit
  `LABEL=path` list as fallback.
- `references/panel.md`, `references/judge_rubric.md` — panel composition + the two judge tracks.

## Panel isolation (plugins off, standard hooks on, Exa only)

Plugin harness hooks (unifable, hookd, etc.) stall or correlate panelists — most visibly the groundedness
breaker, which blocks mutation tools in a loop until timeout, and codex MCP startup, which hangs / "nests"
into a shared app-server across concurrent runs. So every runner strips **plugins and non-Exa MCP**; Exa is
the one server panelists keep for web research. For **cb** and **codex**, standard user hooks from the live
home config are preserved (hook scripts stay at `~/.claude/hooks/` and `~/.codex/hooks/`):

- **cb** → isolated `CLAUDE_CONFIG_DIR`: live hooks from `~/.claude/settings.json`, hardcoded panel
  `skillOverrides`/`permissions`, `enabledPlugins: {}`, `fastMode: true`, plus `--mcp-config` Exa only
  (`--strict-mcp-config`).
- **codex** → isolated `CODEX_HOME` (throwaway dir: `config.toml` with `service_tier = "fast"`,
  `include_apps_instructions = false`, `[features] hooks/code_mode`, copied `auth.json`, live
  `~/.codex/hooks.json`, Exa `[mcp_servers.exa]` only; no notify). `codex exec --dangerously-bypass-hook-trust`
  auto-approves hooks headlessly. Per-run, so concurrent runs never share Codex state.
- **glm** → `glm-acp-agent` is an ACP stdio agent (JSON-RPC), not a traditional CLI. The `_acp_client.mjs`
  shim drives it: MCP servers (Exa only) passed via `session/new` params, permission bypass via
  `session/set_mode bypass_permissions`, model pinned via `ACP_GLM_MODEL` env (default `glm-5.2`).
  No config file needed; no hooks/plugins/rules/skills to strip (the agent has none).
- **kimi** → `--skills-dir <empty>`; Exa from `[mcp_servers.exa]` in `~/.kimi-code/config.toml`; plus a
  best-effort by-name reap of the `kimi-code` worker it spawns (snapshot PIDs before, TERM/KILL the new
  ones after) since that worker daemonizes out of the process group.
- **gemini** → `run_gemini.sh` drives the standalone `gemini` CLI under an isolated `$HOME/.gemini`
  (`_unifusion_write_gemini_panel_settings`): Exa-only MCP (no user skills/extensions/tavily), banner/
  telemetry/auto-update off, `experimental.contextManagement=false` (kills the context-calibrator 404),
  `context.discoveryMaxDirs=0` (kills the /tmp tmp-mount EACCES scans), and a custom alias extending
  `gemini-3.5-flash-base` to set `thinkingConfig.thinkingLevel`. Invoked with `TERM=xterm-256color`
  (silences the dumb-terminal warning); clean stdout, has its own anti-empty guard.
- **agy** (run_agy.sh, comparison baseline) → Exa from `~/.gemini/config/mcp_config.json`; separate
  Antigravity binary; verified clean, has its own anti-empty guard.

`SKILL.md` is the entry; the skill is reachable identically at `~/.agents/skills/unifusion` and
`~/.claude/skills/unifusion` (same inode).

## Runner contract (every `run_*.sh`)

- Signature `run_<cli>.sh <prompt_file> <output_file> [extra]`; writes ONLY the model's clean final
  answer to `<output_file>`.
- cb/codex/kimi/glm run the model against a **throwaway copy** of the repo/workdir (deleted on exit), so a
  panelist's file writes never touch the live checkout.
- Run **panel isolation**: strip plugins/skills and non-Exa MCP (see Panel isolation above). cb/codex keep
  live standard user hooks; glm/kimi strip hooks/skills. Exa MCP is injected (cb/codex/glm throwaways)
  or read from user config (kimi/agy).
- Strip the CLI's wrapper to clean Markdown (ANSI + control bytes; kimi also has a leading bullet +
  2-space hanging indent).
- Exit codes are the degradation signal: `127` CLI missing, `124` timed out (`UNIFUSION_TIMEOUT`), `1`/other
  non-zero / empty → orchestrator drops that panelist. **Never exit 0 with an empty answer** (see
  run_gemini.sh's anti-empty guard).
- Match GPT-5.5's output style: enable web search, give full local tool access, request high reasoning.

## Env knobs

| Var | Default | Effect |
|-----|---------|--------|
| `UNIFUSION_TIMEOUT` | `600` | per-panelist deadline (seconds) |
| `UNIFUSION_EXA_MCP_URL` | (see `_unifusion_lib.sh`) | Exa MCP endpoint for cb/codex/glm throwaway configs |
| `UNIFUSION_OPUS_MODEL` | `opus` | cb model alias for the Opus panelist(s) |
| `UNIFUSION_CODEX_MODEL` | `gpt-5.5` | model in the isolated codex config |
| `KIMI_MODEL` | (unset → kimi `default_model`) | optional Kimi model override |
| `UNIFUSION_KIMI_BIN` | `~/.kimi-code/bin/kimi` | real kimi binary (bypasses shell alias) |
| `GLM_MODEL` | `glm-5.2` | model passed to glm-acp-agent via `ACP_GLM_MODEL` env |
| `GLM_MAX_TOKENS` | `131072` | per-call max output tokens (`glm-5.2` API maximum) |
| `GEMINI_MODEL` | `gemini-3.5-flash` | gemini CLI model id (run_gemini.sh) |
| `GEMINI_THINKING_LEVEL` | `HIGH` | gemini 3.x Flash reasoning effort: `MINIMAL`/`LOW`/`HIGH` |
| `AGY_MODEL` | `Gemini 3.5 Flash (Medium)` | agy model name (run_agy.sh baseline) |
| `UNIFUSION_AGY_NO_MODEL` | (unset) | omit `--model`, use agy default (run_agy.sh baseline) |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | (unset) | required by run_gemini.sh (isolated api-key auth); also enables the (gemini) session-context brief |
| `UNIFUSION_CONTEXT_PROVIDER` | `gemini` | summarizer provider (`codex`/`xai`/`mantle` also valid) |
| `UNIFUSION_TRANSCRIPT` | (unset) | explicit transcript path; overrides resolver/`--session` auto-detection |
| `UNIFUSION_CONTEXT_FILE`, `UNIFUSION_PANEL_NOTE`, `UNIFUSION_ESTIMATE` | — | passed into `save_run.sh` |

## Adding a panelist CLI

1. Write `scripts/run_<cli>.sh` to the runner contract above (copy the closest existing runner) — including
   the panel-isolation stripping for that CLI.
2. In `scripts/unifusion.sh`: add a `have <cli>` probe, and a `launch <label> <slug_token> <out>.md bash
   "$SCRIPT_DIR/run_<cli>.sh"` line in the fan-out block (bump `ext` if it's an external CLI).
3. Add the panelist to `references/panel.md` composition.
4. In `scripts/save_run.sh`, add a `<out_stem>` → label mapping in the auto-discover `label_for` case.
5. The estimate panelist count is derived from how many were launched; no edit needed.

## Testing

No harness besides `selfcheck.sh`. Smoke-test each script directly:

- `printf 'what is the latest node LTS?' > /tmp/q.md && bash scripts/run_<cli>.sh /tmp/q.md /tmp/o.md;
  echo "exit=$?"; cat /tmp/o.md` → clean Markdown, no wrapper artifacts. (For each of cb/codex/gemini/kimi/
  glm; confirm it returns in seconds, not blocked by plugin hooks/MCP — that's panel isolation working.)
- `bash scripts/unifusion.sh /tmp/q.md /tmp/ufrun` → a manifest with one `PANELIST ... ok ...` per installed
  CLI; every `*_out.md` in `/tmp/ufrun` is non-empty and distinct. After, `ps aux | grep kimi-code | grep -v
  grep | wc -l` should not grow run-over-run (orphan reap).
- `bash -n scripts/*.sh` to syntax-check after edits.
- `bash scripts/summarize_session.sh /tmp/ctx.md; echo "exit=$?"; head /tmp/ctx.md` → exit 0, a factual
  brief. Failable checks: the engine's `result.json` `transcript_sha256` equals `shasum -a256` of the live
  session file, and `grep -E '^## (Plans And Task State|Promises Made)'` on the brief returns nothing
  (the factual-only filter held).
- `bash scripts/save_run.sh <slug> /tmp/q.md /tmp/an.md /tmp/fn.md /tmp/ufrun` → a record under
  `~/.claude/unifusion-runs/` with a `### <label>` section per panelist.
- `bash scripts/selfcheck.sh` → PASS.

## Safe-change rules

- Keep Opus as the sole judge; the orchestrator session must stay separate from the panel. Opus panelists
  run as separate `cb` processes and can't call back out to spawn the judge (see `references/panel.md`,
  `judge_rubric.md`).
- Keep every panelist isolated (strip plugins/skills and non-Exa MCP; cb/codex keep standard user hooks).
  Plugin harness hooks — especially the groundedness breaker — must not load or they block a panelist's
  tools in a loop until timeout, or correlate the panel.
- Never paste one panelist's output into another's prompt — independence is the mechanism.
- Keep the factual-only post-filter in `summarize_session.sh` (strips Plans / Promises / Next-Step). The
  session brief is the panel's one shared prior; leaking proposed next steps would correlate the panel.
- Keep `compact-full-transcript.mjs` on schema-constrained structured output (never function calling);
  preserve all four provider dispatch paths when editing it. When syncing from patchpress, copy
  `tool-use-format.mjs` and sibling modules together; re-apply Unifusion-only adapters
  (`codexPayloadText`, `atifToClaudeJsonl`, `UNIFUSION_TRANSCRIPT`, session discovery).
- Optional long-session compression via `UNIFUSION_COMPACT_TRANSCRIPT_RENDERER=sentinel`,
  `UNIFUSION_COMPACT_TOOL_OUTPUT_STRATEGY=mask`, and `UNIFUSION_COMPACT_TOOL_OUTPUT_KEEP_RECENT=64`
  (wired in `summarize_session.sh`).
- Transcript resolution must be a deterministic id or a unique fingerprint match, else exit non-zero;
  never select a transcript by mtime/birth-time/cwd — a wrong transcript would corrupt every panelist.
- Keep the `_run_with_timeout` / pty helpers; they work around real macOS / headless limitations.
- A failing CLI drops only its own token; never abort the whole run.
- Provenance writes stay under `~/.claude/unifusion-runs/` (internal disk); never widen that path.
- This is a skill dir — keep runtime guidance in `SKILL.md`/`references/`, maintainer notes here only.
