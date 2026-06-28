#!/usr/bin/env node
// websearch-gemini.mjs — external research via Gemini 3.5 Flash (agy CLI + Exa MCP).
//
// Usage:
//   node websearch-gemini.mjs "<research goal / task>"
//   websearch-gemini.sh "<research goal / task>"   (preferred; handles preflight)
//
// Zero npm dependencies. Requires Node 18+, agy on PATH, script(1) for PTY capture.
//
// Env:
//   UNITRACE_AGY_MODEL          default: Gemini 3.5 Flash (Low)
//   UNITRACE_AGY_NO_MODEL       set to 1 to omit --model
//   UNITRACE_AGY_TIMEOUT        per-run budget in seconds (default: 600)
//   UNITRACE_AGY_BIN            agy binary override
//   UNITRACE_WORKSPACE          caller repo for AGENTS.md/README context (default: cwd)
//   UNISEARCH_WEBSEARCH_SKILL_CONTEXT  set to 1 to inject explore skill inventory

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { buildWebsearchPrompt, runAgyWebsearch } from "./websearch-lib.mjs";
import { rehydrateWebsearchWire } from "./lib/rehydrate-explore-wire.mjs";
import { isWireFormatEnabled } from "./lib/explore-output-prompt.mjs";

const argv = process.argv.slice(2);

if (argv.length === 0 || argv[0] === "--help" || argv[0] === "-h") {
  process.stderr.write(
    "usage: websearch-gemini.mjs \"<research goal / task>\"\n" +
      "env: UNITRACE_AGY_MODEL, UNITRACE_AGY_NO_MODEL, UNITRACE_AGY_TIMEOUT, UNITRACE_AGY_BIN, UNITRACE_WORKSPACE, UNISEARCH_WEBSEARCH_SKILL_CONTEXT\n",
  );
  process.exit(argv.length === 0 ? 2 : 0);
}

if (argv.some((arg) => arg.startsWith("--"))) {
  process.stderr.write("error: flags are not supported; pass one quoted research goal\n");
  process.exit(2);
}

const goal = argv.join(" ").trim();
if (!goal) {
  process.stderr.write("error: empty research goal\n");
  process.exit(2);
}

const geminiMcp = join(homedir(), ".gemini/config/mcp_config.json");
try {
  const cfg = readFileSync(geminiMcp, "utf8");
  if (!cfg.includes("mcp.exa.ai")) {
    process.stderr.write(
      "warning: exa MCP missing from ~/.gemini/config/mcp_config.json (web search may be limited)\n",
    );
  }
} catch {
  process.stderr.write(
    "warning: ~/.gemini/config/mcp_config.json not found (web search may be limited)\n",
  );
}

const workspace = process.env.UNITRACE_WORKSPACE || process.cwd();
const skillContext = process.env.UNISEARCH_WEBSEARCH_SKILL_CONTEXT === "1";
const prompt = buildWebsearchPrompt(goal, { workspace, skillContext, backend: "agy" });

try {
  const answer = await runAgyWebsearch(prompt);
  const body = isWireFormatEnabled()
    ? rehydrateWebsearchWire(answer)
    : answer;
  process.stdout.write(body.endsWith("\n") ? body : `${body}\n`);
} catch (err) {
  process.stderr.write(`error: ${err.message}\n`);
  if (err.stderrTail) {
    process.stderr.write("agy stderr tail:\n");
    process.stderr.write(`${err.stderrTail}\n`);
  }
  process.exit(err.message.includes("not found") ? 127 : 1);
}
