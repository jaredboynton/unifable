#!/usr/bin/env node
// search-rt.mjs — gpt-realtime-2-driven agentic code search for the explore skill.
//
// Usage:
//   bun search-rt.mjs [--root DIR] [--json] "<natural-language query>"
//   node search-rt.mjs [--root DIR] [--json] "<natural-language query>"
//   search.sh "<natural-language query>"   (preferred; handles preflight)
//
// Spawns the model-agnostic runSearch loop (search-lib.mjs) with gpt-realtime-2
// as the brain over a Codex OAuth Realtime WebSocket, and local ripgrep (rg) as
// the tool executor. On finish, reads authoritative bytes from disk and prints
// verbatim code-reference blocks in startLine:endLine:path format.
//
// Zero npm dependencies. Requires Bun or Node 18+ and rg on PATH, plus Codex auth.
//
// Env:
//   UNITRACE_RT_MODEL                  default: gpt-realtime-2
//   UNITRACE_CODEX_AUTH_PATH           default: ~/.codex/auth.json
//   UNITRACE_SEARCH_REASONING_EFFORT   default: minimal
//   UNITRACE_SEARCH_TIMEOUT_MS         default: 60000 (per model call)
//   UNITRACE_SEARCH_DEBUG              set to 1 for stderr diagnostics
//   UNITRACE_MAP_MODE                  none | pagerank | sigmap | tandem (default: tandem)
//   UNITRACE_MAP_BUDGET                map token budget (default: 1024)
//   UNITRACE_AST_CONTEXT              set to 0 to disable AST-aware hit expansion (default: on)

import { homedir } from "node:os";
import { join } from "node:path";
import fs from "node:fs";

import {
  printResults,
  readFinishFiles,
  runSearch,
  SYSTEM_PROMPT,
  TOOL_SPECS,
} from "./search-lib.mjs";
import { createRealtimeSearchCaller } from "./realtime-search.mjs";
import { generateMapText } from "./map.mjs";
import { seedSearchHits } from "./search-seed.mjs";
import { fastEnabled, runFastPath } from "./search-fast.mjs";

const MODEL = process.env.UNITRACE_RT_MODEL || "gpt-realtime-2";
const AUTH_PATH = process.env.UNITRACE_CODEX_AUTH_PATH || join(homedir(), ".codex", "auth.json");
const REASONING_EFFORT = process.env.UNITRACE_SEARCH_REASONING_EFFORT || "minimal";
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
  process.stderr.write("usage: search-rt.mjs [--root DIR] [--json] \"<query>\"\n");
  process.exit(2);
}
if (!fs.existsSync(AUTH_PATH)) {
  process.stderr.write(`error: Codex auth not found at ${AUTH_PATH}\n  run: codex login\n`);
  process.exit(1);
}

const REPO_ROOT = rootArg || process.env.UNITRACE_WORKSPACE || process.cwd();

// FAST PATH: host-side retrieve -> hydrate -> single warm-daemon rank turn.
// Targets <1.5s by finishing in one model turn off a pre-ranked candidate pool,
// with no per-call socket connect (the daemon holds a warm socket) and no map.
// Returns null to signal fallback to the agentic loop below.
if (fastEnabled()) {
  try {
    const fastFiles = await runFastPath(REPO_ROOT, QUERY, { debug: DEBUG });
    if (fastFiles !== null) {
      if (!fastFiles.length) {
        if (!jsonMode) process.stdout.write("No relevant results found.\n");
        else process.stdout.write("[]\n");
        process.exit(0);
      }
      const refs = readFinishFiles(REPO_ROOT, fastFiles);
      printResults(refs, jsonMode);
      process.exit(0);
    }
    if (DEBUG) process.stderr.write("[search-rt] fast path declined; falling back to agentic loop\n");
  } catch (e) {
    if (DEBUG) process.stderr.write(`[search-rt] fast path error: ${e.message}; falling back\n`);
  }
}

const callModel = createRealtimeSearchCaller({
  model: MODEL,
  authPath: AUTH_PATH,
  systemPrompt: SYSTEM_PROMPT,
  toolSpecs: TOOL_SPECS,
  reasoningEffort: REASONING_EFFORT,
  timeoutMs: TIMEOUT_MS,
  debug: DEBUG,
});

// Overlap the ~330ms socket connect+prewarm with map generation so it leaves
// the turn-1 critical path. Swallow rejections here; callModel surfaces them.
callModel.warm().catch(() => {});

// Host-side definition seeding overlaps the warm too: grep query symbols and
// hydrate their definitions so the model can cite without a discovery turn.
let seedHits = [];
try {
  seedHits = seedSearchHits(REPO_ROOT, QUERY);
} catch (e) {
  if (DEBUG) process.stderr.write(`[search-rt] seed error: ${e.message}\n`);
}
if (DEBUG && seedHits.length) {
  process.stderr.write(`[search-rt] seeded ${seedHits.length} definition window(s): ${seedHits.map((s) => s.path).join(", ")}\n`);
}

let mapText = "";
if (mapMode !== "none") {
  const map = await generateMapText(REPO_ROOT, QUERY, {
    mode: mapMode,
    noCache: process.env.UNITRACE_MAP_NO_CACHE === "1",
  });
  mapText = map.text;
  if (DEBUG && mapText) {
    process.stderr.write(`[search-rt] map mode=${mapMode} bytes=${mapText.length} fromCache=${map.fromCache}\n`);
  }
}

let files;
try {
  files = await runSearch(QUERY, {
    repoRoot: REPO_ROOT,
    debug: DEBUG,
    mapText,
    seedHits,
    callModel: (messages, meta) => callModel(messages, meta),
  });
} finally {
  callModel.close();
}

if (!files || !files.length) {
  if (!jsonMode) process.stdout.write("No relevant results found.\n");
  else process.stdout.write("[]\n");
  process.exit(0);
}

const refs = readFinishFiles(REPO_ROOT, files);
printResults(refs, jsonMode);
