// Host-driven micro-agent explore loop for trace. Replaces the full-model
// explore_exec loop with: host seed (search-fast retrieve+hydrate) -> K parallel
// gpt-realtime-mini "navigators" that propose what else to read -> host hydrates
// (one combined rg per round + targeted line-range reads) -> coalesce/dedup ->
// repeat for R rounds. The daemon is one-shot, so the agent loop lives on the
// host: mini is the brain (selects/proposes), htools are the hands (read-only,
// workspace-confined, preamble-stripped). gpt-realtime-2 is reserved for submit.
//
// Returns the exact { filesRead, readCache, seedPaths, toolTurnCount,
// exploreTurns, maxBatch, exploreItemIds } shape that buildSubmitPacket and
// runSubmitPhase already consume, so it is a drop-in for runExplorePhase.
//
// Fail-open: returns null when the daemon path is unavailable or every navigator
// failed, so the caller can fall back to the full-model explore loop.

import { retrieveCandidates } from "../search-fast.mjs";
import { buildReadIndex, orderReadCacheEntries } from "./rt-rehydrate-submit.mjs";
import { toolReadRange, confine } from "./htools.mjs";
import { normalizeReadPath } from "./trace-schema.mjs";
import { daemonAskBatch } from "./daemon-client.mjs";
import { withReasoningSteer } from "./realtime_client.mjs";

const STRIP_PREAMBLE = process.env.UNITRACE_RT_STRIP_COMMENTS !== "0";

function envInt(name, fallback) {
  const v = process.env[name];
  if (v == null || v === "") return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? Math.trunc(n) : fallback;
}

// Distinct framings so K navigators diversify WHAT they probe instead of all
// chasing the same obvious entrypoint. Cycled when K exceeds the list length.
const FACETS = [
  "the primary entry point and the top-level control flow that answers the question",
  "the data structures, types, and state that flow through this code path",
  "the helper functions, callees, and imported modules the entry point depends on",
  "configuration, environment variables, flags, and defaults that change behavior",
  "error handling, edge cases, fallbacks, and validation on this path",
  "where outputs are produced, persisted, rendered, or returned to the caller",
  "tests, fixtures, or call sites that demonstrate how this code is exercised",
  "alternative or comparison code paths the question contrasts against",
];

export const NAV_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["grep_terms", "read_paths", "done"],
  properties: {
    grep_terms: {
      type: "array",
      maxItems: 6,
      items: { type: "string" },
      description: "Code symbols / identifiers / short strings to grep for to find more load-bearing code. Empty if nothing new to find.",
    },
    read_paths: {
      type: "array",
      maxItems: 6,
      items: {
        type: "object",
        additionalProperties: false,
        required: ["path"],
        properties: {
          path: { type: "string", description: "Repo-relative path to read (must be a real file you saw in the READ INDEX or repo map)." },
          start_line: { type: "integer", minimum: 1 },
          end_line: { type: "integer", minimum: 1 },
        },
      },
      description: "Specific files (optionally line ranges) to read next that are load-bearing for the answer.",
    },
    done: { type: "boolean", description: "true when the files already read are sufficient to answer the question." },
  },
};

const NAV_INSTRUCTIONS = [
  "You are one of several parallel codebase navigators operating in read-only mode.",
  "You are given a QUESTION, a FACET to focus on, and a READ INDEX of code already retrieved.",
  "Your job: decide what ELSE must be read to answer the QUESTION from your facet, then return it via the navigate tool.",
  "Return grep_terms (symbols/identifiers to locate more code) and/or read_paths (specific files + line ranges to read).",
  "Only propose paths you actually see in the READ INDEX or that are clearly named in the question; never invent paths.",
  "Prefer definitions and load-bearing implementation over call sites and tests unless your facet is about them.",
  "If the READ INDEX already covers your facet, return done:true with empty arrays.",
  "Be precise and minimal — propose at most a few high-value targets, not a broad sweep.",
].join("\n");

function navPromptFor(question, indexText, facet) {
  return [
    "QUESTION:",
    question,
    "",
    `YOUR FACET: ${facet}`,
    "",
    "READ INDEX (code already retrieved this run):",
    indexText || "(nothing read yet)",
    "",
    "What else must be read to answer the QUESTION from your facet? Call navigate now.",
  ].join("\n");
}

// Render the current readCache as a navigator-facing index: pointers + previews,
// reusing the same builder the submit phase uses.
export function buildNavIndex(readCache, seedPaths, maxFiles) {
  const ordered = orderReadCacheEntries(readCache, seedPaths);
  return buildReadIndex(ordered, { maxFiles, previewLines: 4 });
}

// Union + dedup navigator proposals across the K parallel navigators: terms are
// deduped case-insensitively; explicit read paths are deduped by path+range.
// Returns { terms, paths, allDone } where allDone is true only when every valid
// navigator reported done.
export function dedupNavProposals(results) {
  const valid = results.filter((r) => r && typeof r === "object");
  const grepTerms = [];
  const readPaths = [];
  let allDone = valid.length > 0;
  for (const r of valid) {
    if (Array.isArray(r.grep_terms)) for (const t of r.grep_terms) if (typeof t === "string" && t.trim()) grepTerms.push(t.trim());
    if (Array.isArray(r.read_paths)) for (const p of r.read_paths) readPaths.push(p);
    if (!r.done) allDone = false;
  }
  const seenTerm = new Set();
  const terms = grepTerms.filter((t) => { const k = t.toLowerCase(); if (seenTerm.has(k)) return false; seenTerm.add(k); return true; });
  const seenPath = new Set();
  const paths = readPaths.filter((p) => {
    if (!p || typeof p.path !== "string") return false;
    const k = `${p.path}:${p.start_line || ""}-${p.end_line || ""}`;
    if (seenPath.has(k)) return false;
    seenPath.add(k);
    return true;
  });
  return { terms, paths, allDone, validCount: valid.length };
}

// Hydrate a query's worth of grep terms into the readCache via the proven
// search-fast retriever (one combined rg -> classify -> score -> AST hydrate).
async function hydrateFromTerms(workspace, terms, onRead, { maxSpans }) {
  const query = terms.join(" ").trim();
  if (!query) return 0;
  let result;
  try {
    result = await retrieveCandidates(workspace, query, { maxSpans });
  } catch {
    return 0;
  }
  let added = 0;
  for (const c of result.candidates || []) {
    const rel = normalizeReadPath(workspace, c.path);
    if (!rel) continue;
    onRead(rel, c.content || "");
    added += 1;
  }
  return added;
}

// Read explicit path[+range] requests directly via htools (read-only, confined).
export function hydrateFromPaths(workspace, readPaths, onRead) {
  let added = 0;
  for (const entry of readPaths || []) {
    if (!entry || typeof entry.path !== "string") continue;
    const abs = confine(workspace, entry.path);
    if (!abs) continue;
    const args = { stripPreamble: STRIP_PREAMBLE };
    if (Number.isInteger(entry.start_line) && Number.isInteger(entry.end_line)) {
      args.start_line = entry.start_line;
      args.end_line = entry.end_line;
    }
    const r = toolReadRange(workspace, entry.path, args);
    if (!r.ok) continue;
    const rel = normalizeReadPath(workspace, entry.path);
    if (!rel) continue;
    onRead(rel, r.content || "");
    added += 1;
  }
  return added;
}

// Seed the readCache with the host retriever (search-fast) so navigators start
// from real, ranked, hydrated code instead of a blank slate.
async function hostSeed(workspace, question, onRead, { maxSpans }) {
  const seeded = [];
  let result;
  try {
    result = await retrieveCandidates(workspace, question, { maxSpans });
  } catch {
    return seeded;
  }
  for (const c of result.candidates || []) {
    const rel = normalizeReadPath(workspace, c.path);
    if (!rel) continue;
    // Pin seed windows so later, less-relevant reads cannot truncate them.
    onRead(rel, c.content || "", { pin: true });
    if (!seeded.includes(rel)) seeded.push(rel);
  }
  return seeded;
}

// Run the host-driven navigator explore loop. `onRead` is the shared read
// tracker (makeReadTracker) so seeds + nav reads land in the same readCache the
// submit phase consumes. Returns null to signal fail-open.
export async function runExploreNav({
  workspace,
  question,
  mapBlock,
  filesRead,
  readCache,
  onRead,
  namespace,
  navModel,
  navCount = envInt("UNITRACE_RT_NAV_COUNT", 8),
  rounds = envInt("UNITRACE_RT_NAV_ROUNDS", 1),
  maxReads = envInt("UNITRACE_RT_UNITRACE_MAX_READS", 20),
  seedSpans = envInt("UNITRACE_RT_NAV_SEED_SPANS", 12),
  roundSpans = envInt("UNITRACE_RT_NAV_ROUND_SPANS", 8),
  indexFiles = envInt("UNITRACE_RT_NAV_INDEX_FILES", 14),
  debug = false,
} = {}) {
  const t0 = Date.now();
  const seedPaths = await hostSeed(workspace, question, onRead, { maxSpans: seedSpans });
  if (debug) process.stderr.write(`[nav] seed_ms=${Date.now() - t0} seeded=${seedPaths.length}\n`);

  let toolTurnCount = 0;
  let navTurns = 0;
  let maxBatch = 0;
  let anyNavOk = false;

  for (let round = 0; round < rounds; round += 1) {
    if (filesRead.size >= maxReads) break;
    const indexText = buildNavIndex(readCache, seedPaths, indexFiles);
    const requests = Array.from({ length: navCount }, (_, i) => ({
      system: NAV_INSTRUCTIONS,
      user: withReasoningSteer(navPromptFor(question, indexText, FACETS[i % FACETS.length])),
      schema: NAV_SCHEMA,
      schemaName: "navigate",
    }));

    const results = await daemonAskBatch(namespace, requests, { model: navModel });
    if (results == null) {
      // Daemon path disabled: only fail-open if we have not seeded anything.
      if (round === 0 && !seedPaths.length) return null;
      break;
    }
    const { terms: dedupTerms, paths: dedupPaths, allDone, validCount } = dedupNavProposals(results);
    if (validCount) anyNavOk = true;
    navTurns += 1;
    maxBatch = Math.max(maxBatch, validCount);

    const before = filesRead.size;
    const fromPaths = hydrateFromPaths(workspace, dedupPaths, onRead);
    const fromTerms = await hydrateFromTerms(workspace, dedupTerms, onRead, { maxSpans: roundSpans });
    toolTurnCount += dedupTerms.length ? 1 : 0;
    toolTurnCount += fromPaths;
    if (debug) {
      process.stderr.write(`[nav] round=${round} navs=${validCount}/${navCount} terms=${dedupTerms.length} paths=${dedupPaths.length} added=${filesRead.size - before} total=${filesRead.size}\n`);
    }

    // Stop when navigators are satisfied or nothing new was discovered.
    const discovered = fromPaths + fromTerms;
    if (allDone || (discovered === 0 && round > 0)) break;
  }

  if (!anyNavOk && !seedPaths.length) return null;

  return {
    toolTurnCount: Math.max(toolTurnCount, 1),
    exploreTurns: navTurns,
    maxBatch,
    seedPaths,
    exploreItemIds: new Set(),
  };
}
