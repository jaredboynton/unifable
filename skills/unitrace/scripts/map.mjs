#!/usr/bin/env node
// map.mjs — repo map prefetch CLI (pagerank, sigmap, tandem).

import { generatePagerankMap } from "./map-pagerank.mjs";
import { generateSigmapMap } from "./map-sigmap.mjs";
import {
  MAP_MODES,
  charBudgetFromTokens,
  listRepoFilesMeta,
  readMapCache,
  resolveRepoRoot,
  wrapRepoMapBlock,
  writeMapCache,
} from "./map-lib.mjs";
import path from "node:path";
import { fileURLToPath } from "node:url";

function parseArgs(argv) {
  let root = null;
  let mode = process.env.UNITRACE_MAP_MODE || "tandem";
  let budgetTokens = Number(process.env.UNITRACE_MAP_BUDGET || 1024);
  let json = false;
  let noCache = false;
  const positional = [];

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--root" && argv[i + 1]) {
      root = argv[++i];
    } else if (arg.startsWith("--root=")) {
      root = arg.slice(7);
    } else if (arg === "--mode" && argv[i + 1]) {
      mode = argv[++i];
    } else if (arg.startsWith("--mode=")) {
      mode = arg.slice(7);
    } else if (arg === "--budget" && argv[i + 1]) {
      budgetTokens = Number(argv[++i]);
    } else if (arg.startsWith("--budget=")) {
      budgetTokens = Number(arg.slice(9));
    } else if (arg === "--json") {
      json = true;
    } else if (arg === "--no-cache") {
      noCache = true;
    } else if (arg === "--help" || arg === "-h") {
      return { help: true };
    } else {
      positional.push(arg);
    }
  }

  return {
    root: root || process.env.UNITRACE_WORKSPACE || process.cwd(),
    mode,
    budgetTokens,
    budgetChars: charBudgetFromTokens(budgetTokens),
    query: positional.join(" ").trim(),
    json,
    noCache,
  };
}

function printHelp() {
  process.stdout.write(
    "usage: map.mjs [--root DIR] [--mode none|pagerank|sigmap|tandem] [--budget TOKENS] [--json] [--no-cache] \"<query>\"\n" +
      "env: UNITRACE_MAP_MODE, UNITRACE_MAP_BUDGET, UNITRACE_WORKSPACE\n",
  );
}

export async function generateMapText(repoRoot, query, options = {}) {
  const mode = options.mode || "none";
  const budgetChars = options.budgetChars ?? charBudgetFromTokens(options.budgetTokens ?? 1024);
  const noCache = Boolean(options.noCache);

  if (mode === "none" || !query) {
    return { mode, text: "", mapMs: 0, fromCache: false };
  }
  if (!MAP_MODES.has(mode)) {
    throw new Error(`invalid map mode: ${mode}`);
  }

  if (!noCache) {
    const cached = readMapCache(repoRoot, mode, query, budgetChars);
    if (cached?.text) {
      return { mode, text: cached.text, mapMs: 0, fromCache: true };
    }
  }

  const started = Date.now();

  // Enumerate once; bail before the multi-second tag-extraction pass on a huge
  // non-git tree (a home dir or cache that hit the file cap), which is never a
  // useful prefetch target. Fail open to no map so the trace is never blocked.
  const maxFilesEnv = Number(process.env.UNITRACE_MAP_MAX_FILES);
  const fileOpts = Number.isFinite(maxFilesEnv) && maxFilesEnv > 0 ? { maxFiles: maxFilesEnv } : {};
  const { files, viaGit, truncated } = listRepoFilesMeta(repoRoot, fileOpts);
  if (process.env.UNITRACE_MAP_ALLOW_HUGE !== "1" && !viaGit && truncated) {
    return { mode, text: "", mapMs: Date.now() - started, fromCache: false, skipped: "huge-non-git" };
  }

  let body = "";
  if (mode === "pagerank") {
    body = generatePagerankMap(repoRoot, query, { budgetChars, files });
  } else if (mode === "sigmap") {
    body = generateSigmapMap(repoRoot, query, { budgetChars, files });
  } else if (mode === "tandem") {
    const pr = generatePagerankMap(repoRoot, query, { budgetChars: Math.floor(budgetChars / 2), files });
    const sm = generateSigmapMap(repoRoot, query, { budgetChars: Math.floor(budgetChars / 2), files });
    body = `${wrapRepoMapBlock("pagerank", pr)}\n${wrapRepoMapBlock("sigmap", sm)}`;
  }
  const mapMs = Date.now() - started;
  if (body && !noCache) writeMapCache(repoRoot, mode, query, budgetChars, body);
  return { mode, text: body, mapMs, fromCache: false };
}

const isMain = process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1]);
if (isMain) {
  const args = parseArgs(process.argv.slice(2));
  if (args.help || !args.query) {
    printHelp();
    process.exit(args.help ? 0 : 2);
  }

  if (!MAP_MODES.has(args.mode)) {
    process.stderr.write(`error: invalid mode ${args.mode}\n`);
    process.exit(2);
  }

  const repoRoot = resolveRepoRoot(args.root);
  const result = await generateMapText(repoRoot, args.query, args);

  if (args.json) {
    process.stdout.write(`${JSON.stringify({ ...result, bytes: result.text.length }, null, 2)}\n`);
  } else if (result.text) {
    process.stdout.write(`${result.text}\n`);
  }
}
