#!/usr/bin/env node
// search.mjs — Cerebras-driven agentic code search for the explore skill.
//
// Usage:
//   node search.mjs [--root DIR] [--json] "<natural-language query>"
//   search.sh "<natural-language query>"   (preferred; handles preflight)
//
// Spawns an agent loop: Cerebras gpt-oss-120b acts as the brain,
// local ripgrep (rg) executes tools (grep/read/glob/list_directory). On
// finish, reads authoritative bytes from disk and prints verbatim code-
// reference blocks in startLine:endLine:path format.
//
// Zero npm dependencies. Requires Node 18+ (global fetch) and rg on PATH.
//
// Env:
//   CEREBRAS_API_KEY           required
//   CEREBRAS_BASE_URL          default: https://api.cerebras.ai/v1
//   UNITRACE_SEARCH_MODEL   default: gpt-oss-120b
//   UNITRACE_SEARCH_TIMEOUT_MS  default: 60000 (per model call)
//   UNITRACE_SEARCH_DEBUG       set to 1 for stderr diagnostics
//   UNITRACE_MAP_MODE           none | pagerank | sigmap | tandem (default: tandem)
//   UNITRACE_MAP_BUDGET         map token budget (default: 1024)
//   UNITRACE_AST_CONTEXT        set to 0 to disable AST-aware hit expansion (default: on)
//   UNITRACE_CEREBRAS_RETRIES     API retries on 5xx (default: 3)

import {
  printResults,
  readFinishFiles,
  runSearch,
  SYSTEM_PROMPT,
  TOOL_SPECS,
} from "./search-lib.mjs";
import { callCerebrasSearch } from "./cerebras-search.mjs";
import { generateMapText } from "./map.mjs";

const BASE_URL = (process.env.CEREBRAS_BASE_URL || "https://api.cerebras.ai/v1").replace(/\/$/, "");
const MODEL = process.env.UNITRACE_SEARCH_MODEL || "gpt-oss-120b";
const API_KEY = process.env.CEREBRAS_API_KEY || "";
const TIMEOUT_MS = parseInt(process.env.UNITRACE_SEARCH_TIMEOUT_MS || "60000", 10);
const DEBUG = process.env.UNITRACE_SEARCH_DEBUG === "1";

const argv = process.argv.slice(2);
let rootArg = null;
let jsonMode = false;
let mapMode = process.env.UNITRACE_MAP_MODE || "tandem";
const positional = [];

for (let i = 0; i < argv.length; i++) {
  if (argv[i] === "--root" && argv[i + 1]) { rootArg = argv[++i]; }
  else if (argv[i] === "--json") { jsonMode = true; }
  else if (argv[i] === "--map-mode" && argv[i + 1]) { mapMode = argv[++i]; }
  else if (argv[i].startsWith("--root=")) { rootArg = argv[i].slice(7); }
  else if (argv[i].startsWith("--map-mode=")) { mapMode = argv[i].slice(11); }
  else { positional.push(argv[i]); }
}

const QUERY = positional.join(" ").trim();
if (!QUERY) {
  process.stderr.write("usage: search.mjs [--root DIR] [--json] \"<query>\"\n");
  process.exit(2);
}
if (!API_KEY) {
  process.stderr.write("error: CEREBRAS_API_KEY is not set\n");
  process.exit(1);
}

const REPO_ROOT = rootArg || process.env.UNITRACE_WORKSPACE || process.cwd();

let mapText = "";
if (mapMode !== "none") {
  const map = await generateMapText(REPO_ROOT, QUERY, {
    mode: mapMode,
    noCache: process.env.UNITRACE_MAP_NO_CACHE === "1",
  });
  mapText = map.text;
  if (DEBUG && mapText) {
    process.stderr.write(`[search] map mode=${mapMode} bytes=${mapText.length} fromCache=${map.fromCache}\n`);
  }
}

async function callCerebras(messages, { finishOnly = false } = {}) {
  return callCerebrasSearch({
    apiKey: API_KEY,
    baseUrl: BASE_URL,
    model: MODEL,
    systemPrompt: SYSTEM_PROMPT,
    messages,
    tools: TOOL_SPECS,
    finishOnly,
    timeoutMs: TIMEOUT_MS,
    debug: DEBUG,
  });
}

const files = await runSearch(QUERY, {
  repoRoot: REPO_ROOT,
  debug: DEBUG,
  mapText,
  callModel: (messages, meta) => callCerebras(messages, meta),
});

if (!files || !files.length) {
  if (!jsonMode) process.stdout.write("No relevant code found.\n");
  else process.stdout.write("[]\n");
  process.exit(0);
}

const refs = readFinishFiles(REPO_ROOT, files);
printResults(refs, jsonMode);
