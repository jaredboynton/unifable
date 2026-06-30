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

import { existsSync } from "node:fs";
import { posix as pathPosix } from "node:path";
import { retrieveCandidates } from "../search-fast.mjs";
import { codeSymbolIdents } from "../search-seed.mjs";
import { buildReadIndex, orderReadCacheEntries } from "./rt-rehydrate-submit.mjs";
import { toolReadRange, confine, toolGrep } from "./htools.mjs";
import { normalizeReadPath } from "./trace-schema.mjs";
import { daemonAskBatch } from "./daemon-client.mjs";
import { namedPathsFromQuestion, seedExploreReads } from "./rt-map-seed.mjs";

const STRIP_PREAMBLE = process.env.UNITRACE_RT_STRIP_COMMENTS !== "0";
const DOC_PATH_RE = /(^|\/)(README|AGENTS|CLAUDE|CHANGELOG)(\.[^/]+)?$|\.mdx?$|\.txt$|\.rst$|\.adoc$|\.json$|\.jsonl$|\.ndjson$|\.ya?ml$|\.toml$|\.ini$|\.cfg$|\.conf$|\.env(\.[^/]+)?$|\.properties$|\.csv$|\.tsv$|\.xml$|\.html?$/i;
const SOURCE_PATH_RE = /\.(sh|mjs|cjs|js|ts|tsx|jsx|py|rb|rs|go|java|kt|swift|php|c|cc|cpp|h|hpp)$/i;
const DEP_EXTS = ["", ".ts", ".tsx", ".mjs", ".js", ".cjs", ".py", ".sh", "/index.ts", "/index.js", "/index.mjs"];

function envInt(name, fallback) {
  const v = process.env[name];
  if (v == null || v === "") return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? Math.trunc(n) : fallback;
}

function numberExcerpt(startLine, content) {
  const base = Number.isFinite(startLine) ? Math.max(1, Math.trunc(startLine)) : 1;
  return String(content || "")
    .split("\n")
    .map((line, idx) => `${base + idx}|${line}`)
    .join("\n");
}

function readCandidateWindow(workspace, candidate) {
  const start = Math.max(1, (candidate.startLine || 1) - 2);
  const end = Math.max(start, candidate.endLine || candidate.startLine || start) + 20;
  const read = toolReadRange(workspace, candidate.path, {
    start_line: start,
    end_line: end,
    stripPreamble: STRIP_PREAMBLE,
  });
  if (read.ok && read.content) return read.content;
  return numberExcerpt(candidate.startLine || 1, candidate.content || "");
}

function excerptRanges(excerpt) {
  const ranges = [];
  for (const segment of String(excerpt || "").split("\n---\n")) {
    const lines = segment.split("\n").filter(Boolean);
    let min = Infinity;
    let max = 0;
    for (const line of lines) {
      const m = line.match(/^(\d+)\|/);
      if (!m) continue;
      const n = Number(m[1]);
      min = Math.min(min, n);
      max = Math.max(max, n);
    }
    if (Number.isFinite(min) && max >= min) ranges.push([min, max]);
  }
  return ranges;
}

function looksLikeDefinitionLine(content, symbol) {
  const text = String(content || "");
  const decl = new RegExp(`\\b(function|class|def|const|let|var|interface|type|enum|struct|fn)\\s+${symbol}\\b`);
  const keyOrAssign = new RegExp(`(^|\\s)${symbol}\\s*[:=]`);
  return decl.test(text) || keyOrAssign.test(text);
}

function questionUsageSymbols(question) {
  return codeSymbolIdents(question).slice(0, 8);
}

export function extractUsageSymbols(readCache, paths = [], { max = 4 } = {}) {
  const out = [];
  const seen = new Set();
  const entries = paths.length
    ? paths.map((p) => [p, readCache.get(p)]).filter(([, excerpt]) => excerpt)
    : [...readCache.entries()];
  const patterns = [
    /^\d+\|\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)/gm,
    /^\d+\|\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=/gm,
    /^\d+\|\s*(?:export\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)/gm,
  ];
  for (const [rel, excerpt] of entries) {
    const ranges = excerptRanges(excerpt);
    for (const re of patterns) {
      for (const match of String(excerpt || "").matchAll(re)) {
        const symbol = match[1];
        if (!symbol || seen.has(symbol)) continue;
        seen.add(symbol);
        out.push({ symbol, rel, ranges });
        if (out.length >= max) return out;
      }
    }
  }
  return out;
}

function rangeCovers(ranges, lineNumber) {
  return ranges.some(([start, end]) => lineNumber >= start && lineNumber <= end);
}

function prioritizedUsageSymbols(readCache, seedPaths, question, maxSymbols) {
  const extracted = extractUsageSymbols(readCache, seedPaths, {
    max: Math.max(maxSymbols, questionUsageSymbols(question).length + 2),
  });
  const bySymbol = new Map(extracted.map((item) => [item.symbol, item]));
  const out = [];
  const seen = new Set();
  for (const symbol of [...questionUsageSymbols(question), ...extracted.map((item) => item.symbol)]) {
    if (!symbol || seen.has(symbol)) continue;
    seen.add(symbol);
    out.push(bySymbol.get(symbol) || { symbol, rel: null, ranges: [] });
    if (out.length >= maxSymbols) break;
  }
  return out;
}

function usageFollowupReads({ workspace, question, readCache, seedPaths, onRead, focusRoots, archiveOk, wireOk, testsOk, maxSymbols = 4, maxReads = 4 }) {
  const symbols = prioritizedUsageSymbols(readCache, seedPaths, question, maxSymbols);
  let added = 0;
  const seen = new Set();
  for (const item of symbols) {
    if (added >= maxReads) break;
    let grep;
    try {
      grep = toolGrep(workspace, { pattern: item.symbol });
    } catch {
      continue;
    }
    if (!grep?.ok) continue;
    for (const fm of grep.fileMatches || []) {
      const rel = normalizeReadPath(workspace, fm.file);
      if (!rel || !candidatePassesFocus(rel, focusRoots, archiveOk, wireOk, testsOk)) continue;
      for (const match of fm.matches || []) {
        if (added >= maxReads) break;
        if (item.rel && rel === item.rel && rangeCovers(item.ranges, match.lineNumber)) continue;
        if (looksLikeDefinitionLine(match.content, item.symbol)) continue;
        const key = `${rel}:${match.lineNumber}`;
        if (seen.has(key)) continue;
        seen.add(key);
        const read = toolReadRange(workspace, rel, {
          start_line: Math.max(1, match.lineNumber - 6),
          end_line: match.lineNumber + 16,
          stripPreamble: STRIP_PREAMBLE,
        });
        if (!read.ok || !read.content) continue;
        onRead(rel, read.content, { pin: true });
        added += 1;
      }
    }
  }
  return added;
}

function excerptCovers(readCache, rel, startLine, endLine) {
  const excerpt = readCache?.get(rel);
  if (!excerpt) return false;
  const targetStart = Number.isFinite(startLine) ? Math.trunc(startLine) : null;
  const targetEnd = Number.isFinite(endLine) ? Math.trunc(endLine) : targetStart;
  if (!targetStart || !targetEnd) return false;
  for (const segment of String(excerpt).split("\n---\n")) {
    const lines = segment.split("\n").filter(Boolean);
    let min = Infinity;
    let max = 0;
    for (const line of lines) {
      const m = line.match(/^(\d+)\|/);
      if (!m) continue;
      const n = Number(m[1]);
      min = Math.min(min, n);
      max = Math.max(max, n);
    }
    if (Number.isFinite(min) && max >= min && targetStart >= min && targetEnd <= max) return true;
  }
  return false;
}

function isDocPath(p) {
  return DOC_PATH_RE.test(String(p || ""));
}

function isSourcePath(p) {
  return SOURCE_PATH_RE.test(String(p || ""));
}

function prefersSource(question) {
  const named = namedPathsFromQuestion(question);
  if (named.some((p) => isDocPath(p) && !isSourcePath(p))) return false;
  if (named.some((p) => isSourcePath(p))) return true;
  if (codeSymbolIdents(question).length) return true;
  if (/\b(readme|docs?|document|install(?:er|ers|ation)?|release|workflow|package|config(?:uration)?|settings?|credential|account|runbook)\b/i.test(String(question || ""))) {
    return false;
  }
  return /\b(how|trace|flow|phase|pipeline|call|entry(?:point)?|render|submit|explore|function|script|module|implementation|implement|orchestr|worker|handler)\b/i.test(String(question || ""));
}

// Prose questions about documented behavior (limit headers, fallback semantics,
// end-to-end behavior) are often answered in an AGENTS.md/README the source code
// only partially shows. These should NOT run source-only retrieval (maxDocFiles:0),
// or the load-bearing doc is suppressed even when the retriever ranks it highly.
// Narrow on purpose: only behavior/header/fallback framings, not every doc word.
export function admitsDocs(question) {
  return /\b(fallback|headers?|behaviou?rs?|end[- ]to[- ]end|convention|policy|documented)\b/i.test(String(question || ""));
}

function allowArchive(question) {
  return /\b(archive|cursor|acp|gemini|grok|legacy|retired)\b/i.test(String(question || ""));
}

function allowWire(question) {
  return /\bwire\b/i.test(String(question || ""));
}

function allowTests(question) {
  return /\b(test|tests|fixture|benchmark|bench)\b/i.test(String(question || ""));
}

function dirnameOf(rel) {
  const s = String(rel || "").replace(/\/+$/, "");
  const idx = s.lastIndexOf("/");
  return idx >= 0 ? s.slice(0, idx) : "";
}

export function focusRootsFor(question, seedPaths = []) {
  const named = namedPathsFromQuestion(question);
  const roots = new Set();
  for (const rel of [...seedPaths, ...named]) {
    const s = String(rel || "");
    if (!s.includes("/")) continue;
    if (s.includes("/scripts/")) {
      const prefix = s.slice(0, s.indexOf("/scripts/") + "/scripts".length);
      roots.add(prefix);
      const dir = dirnameOf(s);
      if (dir && dir !== prefix) roots.add(dir);
      continue;
    }
    if (s.includes("/src/")) {
      const prefix = s.slice(0, s.indexOf("/src/") + "/src".length);
      roots.add(prefix);
      const dir = dirnameOf(s);
      if (dir && dir !== prefix) roots.add(dir);
      continue;
    }
    roots.add(dirnameOf(s));
  }
  return [...roots].filter(Boolean);
}

function candidatePassesFocus(rel, focusRoots, archiveOk, wireOk, testsOk) {
  if (!archiveOk && /(^|\/)archive\//.test(rel)) return false;
  if (!wireOk && /(^|\/)(explore-hydrate\.sh|rehydrate-explore-wire\.mjs)$/.test(rel)) return false;
  if (!testsOk && /(^|\/)(tests?|fixtures?)\/|(^|\/)test-[^/]+/.test(rel)) return false;
  if (!focusRoots.length) return true;
  return focusRoots.some((root) => rel === root || rel.startsWith(`${root}/`));
}

function focusCandidates(candidates, focusRoots, archiveOk, wireOk, testsOk, question = "") {
  const ranked = (candidates || []).filter((c) => c && typeof c.path === "string");
  const focused = ranked.filter((c) => candidatePassesFocus(c.path, focusRoots, archiveOk, wireOk, testsOk));
  if (focused.length) return focused;
  return ranked.filter((c) => candidatePassesFocus(c.path, [], archiveOk, wireOk, testsOk));
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
  "For implementation or control-flow questions, prefer real source files over AGENTS/README/docs unless the question is explicitly about docs, policy, or config.",
  "Unless the question explicitly asks about wire/plaintext mode or UNITRACE_WIRE_FORMAT, avoid optional wire-format branches and focus on the default structured path.",
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
async function hydrateFromTerms(workspace, terms, onRead, { maxSpans, preferSourceOnly = false, focusRoots = [], archiveOk = false, wireOk = false, testsOk = false, readCache = null }) {
  const query = terms.join(" ").trim();
  if (!query) return 0;
  let result;
  try {
    result = await retrieveCandidates(workspace, query, {
      maxSpans,
      ...(preferSourceOnly ? { maxDocFiles: 0 } : {}),
    });
  } catch {
    return 0;
  }
  let added = 0;
  for (const c of focusCandidates(result.candidates || [], focusRoots, archiveOk, wireOk, testsOk, query)) {
    const rel = normalizeReadPath(workspace, c.path);
    if (!rel) continue;
    if (excerptCovers(readCache, rel, c.startLine || 1, c.endLine || c.startLine || 1)) continue;
    onRead(rel, readCandidateWindow(workspace, c));
    added += 1;
  }
  return added;
}

// Read explicit path[+range] requests directly via htools (read-only, confined).
export function hydrateFromPaths(workspace, readPaths, onRead, { focusRoots = [], archiveOk = false, wireOk = false, testsOk = false, question = "" } = {}) {
  let added = 0;
  for (const entry of readPaths || []) {
    if (!entry || typeof entry.path !== "string") continue;
    if (!candidatePassesFocus(entry.path, focusRoots, archiveOk, wireOk, testsOk)) continue;
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

// Terms from the question used to rank which imports are worth following: the
// load-bearing dependency usually shares a word with the question ("render",
// "rehydrate", "scope"). Symbols are case-folded; prose words are split on
// separators so "markdown rendering" matches a render-*.mjs path.
function followTerms(question) {
  const terms = new Set();
  for (const m of String(question || "").toLowerCase().matchAll(/[a-z][a-z0-9]{3,}/g)) {
    const w = m[0];
    terms.add(w);
    terms.add(w.replace(/(?:ing|ed|es|s)$/, ""));
  }
  for (const ident of codeSymbolIdents(question)) {
    for (const part of ident.toLowerCase().split(/[_-]/)) if (part.length >= 4) terms.add(part);
  }
  return [...terms].filter((t) => t.length >= 4);
}

function localRefCandidates(base) {
  if (/\.[A-Za-z0-9]+$/.test(base)) return [base];
  return DEP_EXTS.map((ext) => `${base}${ext}`);
}

// Resolve a raw import/require/source specifier seen inside `currentRel` to a
// real repo-relative source path, or null. Local refs only (relative, $SCRIPT_DIR,
// or repo-rooted source paths); bare package names and escapes are rejected.
function resolveLocalRef(workspace, currentRel, rawSpec) {
  let spec = String(rawSpec || "").trim();
  if (!spec || spec.startsWith("#")) return null;
  if (spec.startsWith("$SCRIPT_DIR/")) spec = `./${spec.slice("$SCRIPT_DIR/".length)}`;
  let base = null;
  if (spec.startsWith(".")) {
    base = pathPosix.normalize(pathPosix.join(dirnameOf(currentRel), spec));
  } else if (spec.includes("/") && SOURCE_PATH_RE.test(spec)) {
    base = pathPosix.normalize(spec.replace(/^\.\//, ""));
  }
  if (!base || base.startsWith("../") || base.includes("/../")) return null;
  for (const candidate of localRefCandidates(base)) {
    const abs = confine(workspace, candidate);
    if (!abs || !existsSync(abs)) continue;
    return normalizeReadPath(workspace, candidate);
  }
  return null;
}

function followScore(rel, terms) {
  const p = String(rel || "").toLowerCase();
  let score = isSourcePath(p) ? 2 : 0;
  for (const term of terms) if (p.includes(term)) score += 3;
  return score;
}

// One-hop call-graph follow: from a small set of high-confidence source anchors
// (named/required seeds + host-retriever top), parse local imports/requires/source
// lines and surface the import-reachable load-bearing files the lexical retriever
// misses (e.g. realtime-trace.mjs -> render-trace-structured.mjs + rt-rehydrate-submit.mjs).
// Disciplined on purpose: anchors only (not every read file), one hop, focus-gated,
// ranked by question-term overlap, tightly capped. Returns the relative paths added.
export function importFollowSeeds({ workspace, question, anchors, focusRoots, archiveOk, wireOk, testsOk, onRead, readCache, maxReads = envInt("UNITRACE_RT_IMPORT_FOLLOW_MAX", 5) }) {
  if (process.env.UNITRACE_RT_IMPORT_FOLLOW === "0") return [];
  const sourceAnchors = (anchors || []).filter((rel) => isSourcePath(rel));
  if (!sourceAnchors.length) return [];
  const followRoots = focusRoots.length ? focusRoots : focusRootsFor(question, sourceAnchors);
  const terms = followTerms(question);
  const importRe = /\bfrom\s+["']([^"']+)["']/g;
  const requireRe = /\brequire\(\s*["']([^"']+)["']\s*\)/g;
  const sourceRe = /(?:^|\n)\s*(?:source|\.)\s+["']?(\$SCRIPT_DIR\/[^"'\s]+|\.\/[^"'\s]+)/g;
  const refs = new Map();
  let order = 0;
  for (const anchorRel of sourceAnchors) {
    const header = toolReadRange(workspace, anchorRel, { start_line: 1, end_line: 160, stripPreamble: false });
    if (!header.ok || !header.content) continue;
    const text = header.content;
    for (const re of [importRe, requireRe, sourceRe]) {
      for (const m of text.matchAll(re)) {
        const rel = resolveLocalRef(workspace, anchorRel, m[1]);
        if (!rel || rel === anchorRel || refs.has(rel)) continue;
        if (sourceAnchors.includes(rel) || (readCache && readCache.has(rel))) continue;
        if (!candidatePassesFocus(rel, followRoots, archiveOk, wireOk, testsOk)) continue;
        refs.set(rel, { rel, score: followScore(rel, terms), order: order++ });
      }
    }
  }
  const ranked = [...refs.values()].sort((a, b) => (b.score - a.score) || (a.order - b.order));
  const added = [];
  for (const { rel } of ranked) {
    if (added.length >= maxReads) break;
    const read = toolReadRange(workspace, rel, { start_line: 1, end_line: 200, stripPreamble: STRIP_PREAMBLE });
    if (!read.ok || !read.content) continue;
    onRead(rel, read.content, { pin: true });
    added.push(rel);
  }
  return added;
}

// Seed the readCache with the host retriever (search-fast) so navigators start
// from real, ranked, hydrated code instead of a blank slate.
async function hostSeed(workspace, question, onRead, { maxSpans, preferSourceOnly = false, focusRoots = [], archiveOk = false, wireOk = false, testsOk = false, readCache = null, topDoc = false }) {
  const seeded = [];
  let result;
  try {
    result = await retrieveCandidates(workspace, question, {
      maxSpans,
      ...(preferSourceOnly ? { maxDocFiles: 0 } : {}),
    });
  } catch {
    return seeded;
  }
  const candidates = result.candidates || [];
  for (const c of focusCandidates(candidates, focusRoots, archiveOk, wireOk, testsOk, question)) {
    const rel = normalizeReadPath(workspace, c.path);
    if (!rel) continue;
    if (excerptCovers(readCache, rel, c.startLine || 1, c.endLine || c.startLine || 1)) continue;
    // Pin seed windows so later, less-relevant reads cannot truncate them.
    onRead(rel, readCandidateWindow(workspace, c), { pin: true });
    if (!seeded.includes(rel)) seeded.push(rel);
  }
  // Behavior/header/fallback questions are often answered in a module-root
  // AGENTS.md/README that the source-focus root (e.g. gateway/src) excludes even
  // though the retriever ranks it highly. Seed the single top doc candidate,
  // focus-bypassed but still archive/test/wire-gated, so it is not lost.
  if (topDoc) {
    for (const c of candidates) {
      if (c.cls !== "doc") continue;
      const rel = normalizeReadPath(workspace, c.path);
      if (!rel || seeded.includes(rel)) continue;
      if (!candidatePassesFocus(rel, [], archiveOk, wireOk, testsOk)) continue;
      if (excerptCovers(readCache, rel, c.startLine || 1, c.endLine || c.startLine || 1)) continue;
      onRead(rel, readCandidateWindow(workspace, c), { pin: true });
      seeded.push(rel);
      break;
    }
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
  const preferSourceOnly = prefersSource(question) && !admitsDocs(question);
  const archiveOk = allowArchive(question);
  const wireOk = allowWire(question);
  const testsOk = allowTests(question);
  const explicitSeeds = seedExploreReads({
    workspace,
    question,
    mapBlock,
    filesRead,
    readCache,
    onRead,
  });
  const focusRoots = focusRootsFor(question, explicitSeeds);
  const hostSeeds = await hostSeed(workspace, question, onRead, {
    maxSpans: seedSpans,
    preferSourceOnly,
    focusRoots,
    archiveOk,
    wireOk,
    testsOk,
    readCache,
    topDoc: admitsDocs(question),
  });
  const seedPaths = [...new Set([...explicitSeeds, ...hostSeeds])];
  usageFollowupReads({
    workspace,
    question,
    readCache,
    seedPaths,
    onRead,
    focusRoots,
    archiveOk,
    wireOk,
    testsOk,
    maxSymbols: 6,
    maxReads: 6,
  });
  // One-hop call-graph follow from the high-confidence anchors so import-reachable
  // load-bearing files (which the lexical retriever often misses) are seeded.
  const followed = importFollowSeeds({
    workspace,
    question,
    anchors: seedPaths,
    focusRoots,
    archiveOk,
    wireOk,
    testsOk,
    onRead,
    readCache,
  });
  for (const rel of followed) if (!seedPaths.includes(rel)) seedPaths.push(rel);
  if (debug) process.stderr.write(`[nav] seed_ms=${Date.now() - t0} seeded=${seedPaths.length} followed=${followed.length}\n`);

  let toolTurnCount = 0;
  let navTurns = 0;
  let maxBatch = 0;
  let anyNavOk = false;

  for (let round = 0; round < rounds; round += 1) {
    if (filesRead.size >= maxReads) break;
    const indexText = buildNavIndex(readCache, seedPaths, indexFiles);
    const requests = Array.from({ length: navCount }, (_, i) => ({
      system: NAV_INSTRUCTIONS,
      user: navPromptFor(question, indexText, FACETS[i % FACETS.length]),
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
    const fromPaths = hydrateFromPaths(workspace, dedupPaths, onRead, { focusRoots, archiveOk, wireOk, testsOk, question });
    const fromTerms = await hydrateFromTerms(workspace, dedupTerms, onRead, {
      maxSpans: roundSpans,
      preferSourceOnly,
      focusRoots,
      archiveOk,
      wireOk,
      testsOk,
      readCache,
    });
    const fromUsage = usageFollowupReads({
      workspace,
      question,
      readCache,
      seedPaths: [...filesRead],
      onRead,
      focusRoots,
      archiveOk,
      wireOk,
      testsOk,
      maxSymbols: 4,
      maxReads: 3,
    });
    toolTurnCount += dedupTerms.length ? 1 : 0;
    toolTurnCount += fromPaths;
    toolTurnCount += fromUsage;
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
