// search-fast.mjs — sub-1.5s code search: host-side retrieve -> hydrate -> one
// model turn. Replaces the agentic explore loop on the fast path.
//
// The model floor is ~560ms/turn, so to land under 1.5s we must finish in ONE
// turn. Host-side retrieval (parallel async rg) assembles a ranked, hydrated
// candidate pool good enough that the model only re-ranks and calls finish.
// If the pool is empty or the model declines, search-rt falls back to the
// agentic loop (runSearch) for quality safety.
//
// Env:
//   EXPLORE_SEARCH_FAST=0              disable fast path (pure legacy loop)
//   EXPLORE_SEARCH_FAST_MAX_CANDIDATES max hydrated candidate files (default 16)
//   EXPLORE_SEARCH_FAST_HYDRATE_SPAN   max lines per hydrated window (default 40)
//   EXPLORE_SEARCH_FAST_GREP_CAP       max rg matches parsed per pattern (default 400)

import { execFile } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import {
  detectAstBinary,
  ensureAstTool,
  expandLineRange,
  langForPath,
  stripCommentsEnabled,
} from "./ast-context.mjs";
import { makeLineHider } from "./lib/code-line.mjs";
import { codeSymbolIdents } from "./search-seed.mjs";

const NON_SOURCE_RE = /\.(md|markdown|json|ndjson|jsonl|txt|log|csv|tsv|ya?ml|toml|lock|svg|png|jpe?g|gif|ico)$/i;
const GREP_EXCLUDES = [
  ".git", "node_modules", ".pnpm", ".yarn", "vendor", "Pods", ".bundle",
  "__pycache__", ".venv", "venv", "dist", "build", "out", "target", ".next", ".nuxt",
  ".cache", ".turbo", "generated", "*.min.js", "*.min.css", "*.map", "*.generated.*",
];
const DECL_KW = "fn|func|def|class|struct|trait|impl|enum|type|interface|const|function|pub|module|fun";
const STOPWORDS = new Set([
  "this", "that", "with", "from", "what", "when", "where", "which", "while", "does",
  "done", "into", "over", "your", "have", "here", "they", "them", "then", "than",
  "code", "work", "works", "working", "used", "uses", "using", "make", "made",
  "like", "also", "some", "such", "only", "just", "very", "more", "most", "much",
  "incoming", "request", "requests", "handle", "handles", "handling", "check",
  "checks", "checking", "happen", "happens", "about", "through", "across", "each",
  "every", "find", "show", "tell", "explain", "describe", "understand", "function",
  "files", "file", "implement", "implemented", "implementation", "logic", "module",
]);

function envInt(name, fallback) {
  const v = process.env[name];
  if (v == null || v === "") return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? Math.trunc(n) : fallback;
}

export function fastEnabled() {
  return process.env.EXPLORE_SEARCH_FAST !== "0";
}

// Content words: lowercased, >=4 chars, stopwords + pure-symbol tokens removed
// (symbols are handled by codeSymbolIdents). These drive the prose def-grep.
export function contentWords(query) {
  const out = [];
  const seen = new Set();
  for (const m of String(query || "").matchAll(/[A-Za-z][A-Za-z]{3,}/g)) {
    const w = m[0].toLowerCase();
    if (seen.has(w) || STOPWORDS.has(w)) continue;
    seen.add(w);
    out.push(w);
  }
  return out;
}

function isSourceFile(rel) {
  if (!rel || NON_SOURCE_RE.test(rel)) return false;
  if (/(^|\/)fixtures\//.test(rel)) return false;
  return Boolean(langForPath(rel));
}

// ONE combined ripgrep over the alternation of all terms (case-insensitive).
// Classifying def-vs-ref and scoring happens in node (~tens of ms), far cheaper
// than firing a def+ref grep per term (which also let generators win on raw
// keyword counts). Returns parsed hit lines [{ file, line, text }].
function runCombinedRg(repoRoot, terms, cap) {
  const args = [
    "--no-config", "--no-heading", "--with-filename", "--line-number",
    "--color=never", "--trim", "--max-columns=400", "--ignore-case",
    ...GREP_EXCLUDES.flatMap((e) => ["-g", `!${e}`]),
    ...terms.flatMap((t) => ["-e", t]),
    ".",
  ];
  return new Promise((resolve) => {
    execFile("rg", args, { cwd: repoRoot, encoding: "utf8", maxBuffer: 32 * 1024 * 1024 }, (err, stdout) => {
      const hits = [];
      let n = 0;
      for (const line of (stdout || "").split(/\r?\n/)) {
        if (n >= cap) break;
        if (!line.trim()) continue;
        const m = line.match(/^(.+?):(\d+):(.*)$/);
        if (!m) continue;
        const file = m[1].replace(/^\.\//, "");
        if (!isSourceFile(file)) continue;
        hits.push({ file, line: parseInt(m[2], 10), text: m[3] });
        n += 1;
      }
      resolve(hits);
    });
  });
}

const CENTRAL_RE = /(^|\/)(src|lib|pkg|internal|hooks|app|core|routes|middleware|handlers?|controllers?|services?)\//;
const GENERATOR_RE = /(^|\/)(generate|generated|portal|vendor|examples?)\/|generate\.|\.min\./;

// Classify combined-rg hits into per-file stats. For each line, "def" means the
// line declares a name containing one of the terms (DECL keyword + term);
// everything else is a "ref". Tracks distinct matched terms and the best anchor
// line (the def line covering the most terms).
export function classifyHits(hits, terms) {
  const declRe = terms.length
    ? new RegExp(`\\b(${DECL_KW})\\s+\\w*(${terms.join("|")})\\w*`, "i")
    : null;
  const termRes = terms.map((t) => new RegExp(`\\b${t}`, "i"));
  const files = new Map();
  for (const h of hits) {
    let f = files.get(h.file);
    if (!f) { f = { file: h.file, def: 0, ref: 0, terms: new Set(), defLines: new Map(), refLines: [] }; files.set(h.file, f); }
    const isDef = declRe ? declRe.test(h.text) : false;
    if (isDef) f.def += 1; else { f.ref += 1; f.refLines.push(h.line); }
    const lineTerms = new Set();
    for (let i = 0; i < terms.length; i++) if (termRes[i].test(h.text)) { f.terms.add(terms[i]); lineTerms.add(terms[i]); }
    if (isDef && lineTerms.size) {
      const prev = f.defLines.get(h.line) || new Set();
      for (const t of lineTerms) prev.add(t);
      f.defLines.set(h.line, prev);
    }
  }
  return files;
}

// The line at the center of the densest cluster of hit lines (smallest window
// covering the most hits). Used as the anchor when no definition matched, so the
// hydrated window lands on real code instead of the file header.
function densestCluster(lines, span) {
  if (!lines.length) return 0;
  const sorted = [...lines].sort((a, b) => a - b);
  let bestStart = 0;
  let bestCount = 0;
  let j = 0;
  for (let i = 0; i < sorted.length; i++) {
    while (j < sorted.length && sorted[j] - sorted[i] <= span) j += 1;
    const count = j - i;
    if (count > bestCount) { bestCount = count; bestStart = i; }
  }
  const lo = sorted[bestStart];
  const hi = sorted[Math.min(bestStart + bestCount - 1, sorted.length - 1)];
  return Math.floor((lo + hi) / 2);
}

// Score per-file stats. def is capped (a real implementation rarely has >6
// matching declarations; 12+ signals a generator/data file -> penalize), refs
// contribute marginally, multi-term coverage and central-dir placement dominate.
export function scoreCandidates(fileStats, { hydrateSpan = 40 } = {}) {
  const scored = [];
  for (const f of fileStats.values()) {
    const defCap = Math.min(f.def, 6);
    const overflow = f.def > 12 ? -4 : 0;
    let score = defCap * 4 + Math.min(f.ref, 3) + (f.terms.size - 1) * 6 + overflow;
    if (CENTRAL_RE.test(f.file)) score += 5;
    if (GENERATOR_RE.test(f.file)) score -= 4;
    // Anchor: the def line covering the most terms; else earliest def; else the
    // densest ref-line cluster (so no-def files still hydrate real code, not the
    // file header at lines 1-N).
    let anchor = 0;
    let bestTerms = 0;
    for (const [line, lineTerms] of f.defLines) {
      if (lineTerms.size > bestTerms || (lineTerms.size === bestTerms && (anchor === 0 || line < anchor))) {
        bestTerms = lineTerms.size;
        anchor = line;
      }
    }
    if (anchor === 0) anchor = densestCluster(f.refLines, hydrateSpan);
    scored.push({ file: f.file, score, anchorLine: anchor, defCount: f.def, refCount: f.ref, termCount: f.terms.size });
  }
  scored.sort((a, b) => b.score - a.score || a.file.localeCompare(b.file));
  return scored;
}

function hydrateWindow(absPath, anchorLine, binary, maxSpan) {
  let s = anchorLine || 1;
  let e = anchorLine || 1;
  if (binary && anchorLine) {
    const exp = expandLineRange(absPath, anchorLine, anchorLine, { binary });
    s = exp.startLine;
    e = exp.endLine;
  }
  if (s === e) {
    // No AST node (or no anchor): take a leading window of the file.
    s = Math.max(1, (anchorLine || 1) - 4);
    e = s + maxSpan - 1;
  }
  let raw;
  try { raw = fs.readFileSync(absPath, "utf8"); } catch { return null; }
  const all = raw.split(/\r?\n/);
  e = Math.min(e, all.length);
  if (e - s + 1 > maxSpan) e = s + maxSpan - 1;
  const strip = stripCommentsEnabled();
  const hide = strip ? makeLineHider(absPath) : null;
  const lines = [];
  for (let i = 1; i <= e; i++) {
    const hidden = hide ? hide(all[i - 1] ?? "") : false;
    if (i < s || hidden) continue;
    lines.push(all[i - 1] ?? "");
  }
  const content = lines.join("\n");
  if (!content.trim()) return null;
  return { startLine: s, endLine: e, content };
}

// Fire all retrieval greps concurrently and assemble a ranked, hydrated
// candidate pool. Returns { candidates:[{path,startLine,endLine,content,score}], terms }.
export async function retrieveCandidates(repoRoot, query, {
  maxCandidates = envInt("EXPLORE_SEARCH_FAST_MAX_CANDIDATES", 16),
  hydrateSpan = envInt("EXPLORE_SEARCH_FAST_HYDRATE_SPAN", 40),
  grepCap = envInt("EXPLORE_SEARCH_FAST_GREP_CAP", 4000),
} = {}) {
  const symbols = codeSymbolIdents(query);
  const allWords = contentWords(query);
  // Drop content words that are sub-tokens of a code symbol: splitting
  // `parse_scope_segments` into parse/scope/segments greps the symbol's parts
  // into unrelated files (generators, helpers) and buries the real definition.
  // When a strong symbol is present its own match is the signal; the fragments
  // are noise.
  const symLower = symbols.map((s) => s.toLowerCase());
  const words = allWords.filter((w) => !symLower.some((s) => s.includes(w)));
  const terms = [...symbols, ...words];
  if (!terms.length) return { candidates: [], terms: [] };

  ensureAstTool({ install: process.env.EXPLORE_AST_SKIP_INSTALL !== "1" });
  const binary = detectAstBinary();

  // ONE combined ripgrep; classify + score in node.
  const hits = await runCombinedRg(repoRoot, terms, grepCap);
  const ranked = scoreCandidates(classifyHits(hits, terms), { hydrateSpan }).slice(0, maxCandidates);

  const candidates = [];
  for (const r of ranked) {
    const abs = path.resolve(repoRoot, r.file);
    const win = hydrateWindow(abs, r.anchorLine, binary, hydrateSpan);
    if (!win) continue;
    candidates.push({ path: r.file, startLine: win.startLine, endLine: win.endLine, content: win.content, score: r.score });
  }
  return { candidates, terms };
}

const MAX_POOL_CHARS = 16000;

// Render the candidate pool as a single user message for the one-shot rank turn.
export function buildRankPrompt(query, candidates) {
  const parts = [
    "<candidates>",
    "Pre-retrieved, ranked code windows (highest score first; already confirmed on disk).",
    "Select the file(s) and line ranges that answer the query and call finish NOW.",
    "Cite path:start-end straight from these windows. Do NOT call grep_search, read, glob, or list_directory.",
    "If none are relevant, call finish with an empty files string.",
  ];
  let used = parts.join("\n").length;
  for (const c of candidates) {
    const block = `---\n${c.startLine}:${c.endLine}:${c.path}\n${c.content}`;
    if (used + block.length > MAX_POOL_CHARS) break;
    parts.push(block);
    used += block.length;
  }
  parts.push("</candidates>", "", `<search_string>\n${query}\n</search_string>`);
  return parts.join("\n");
}

const RANK_INSTRUCTIONS = [
  "You are a code-search ranker. You are given pre-retrieved, ranked code windows",
  "that were already confirmed to exist on disk by host-side ripgrep.",
  "Select ONLY the windows that actually answer the query and call finish exactly once.",
  "Cite repo-relative path:start-end taken directly from the window headers.",
  "Prefer the definition/implementation over call sites. If several windows are part of",
  "the same answer, list each on its own line. If NONE are relevant, call finish with an",
  "empty files string. Do not invent paths or ranges.",
].join(" ");

const SCORE_INSTRUCTIONS = [
  "You are a code-search relevance scorer. Given a query and ONE code window,",
  "rate 0-10 how directly this window answers the query: 0 = unrelated, 10 = this",
  "is the definitive implementation that answers it. Judge the code on its own",
  "merit (definitions/implementations score higher than incidental mentions or",
  "call sites). Output only the integer score.",
].join(" ");

const SCORE_SCHEMA = {
  type: "object",
  properties: { score: { type: "integer", description: "0-10 relevance, 10 = definitive answer" } },
  required: ["score"],
  additionalProperties: false,
};

const FINISH_SCHEMA = {
  type: "object",
  properties: {
    files: {
      type: "string",
      description: "One file per line as path:lines (e.g. 'src/auth.rs:1-15,25-50\\nsrc/user.rs'). Empty string if no relevant code exists.",
    },
  },
  required: ["files"],
  additionalProperties: false,
};

function scorePromptFor(query, c) {
  return `<query>\n${query}\n</query>\n<code ${c.startLine}:${c.endLine}:${c.path}>\n${c.content}\n</code>\nScore 0-10.`;
}

// Fast path: host retrieve -> hydrate -> PARALLEL per-candidate relevance scoring
// across the warm daemon pool -> coalesce. Returns parsed finish files
// [{path,lines}] on success, an empty array for the clean "no relevant code"
// negative, or null to signal the caller to fall back to the agentic loop
// (empty pool, daemon unavailable, or no candidate cleared the score floor).
//
// Two finish strategies (EXPLORE_SEARCH_FINISH_MODE):
//   score  (A1, default): keep windows scoring >= floor, ordered by (model
//          score, retrieval score), capped to FINISH_MAX. No final model turn.
//   coalesce (A2): same scoring, then ONE finish turn over the survivors so the
//          model can merge/compare them.
export async function runFastPath(repoRoot, query, { debug = false } = {}) {
  if (!fastEnabled()) return null;
  const { daemonAsk, daemonAskBatch, warmDaemonPool } = await import("./lib/daemon-client.mjs");
  const { validateFinishFiles } = await import("./search-lib.mjs");

  const reasoningEffort = process.env.EXPLORE_SEARCH_REASONING_EFFORT || "minimal";
  const scoreFloor = envInt("EXPLORE_SEARCH_SCORE_MIN", 6);
  const finishMax = envInt("EXPLORE_SEARCH_FINISH_MAX", 6);
  const mode = (process.env.EXPLORE_SEARCH_FINISH_MODE || "score").trim();

  // Warm the pool and retrieve concurrently: the pool spawn/connect overlaps the
  // host ripgrep + AST hydration so neither is on the other's critical path.
  const t0 = Date.now();
  const [{ candidates }] = await Promise.all([
    retrieveCandidates(repoRoot, query),
    warmDaemonPool(repoRoot).catch(() => {}),
  ]);
  if (debug) process.stderr.write(`[search-fast] retrieve+warm_ms=${Date.now() - t0} candidates=${candidates.length}\n`);
  if (!candidates.length) return null;

  // PARALLEL scoring: one tiny call per candidate, spread across the pool.
  const tScore = Date.now();
  const scored = await daemonAskBatch(
    repoRoot,
    candidates.map((c) => ({
      system: SCORE_INSTRUCTIONS,
      user: scorePromptFor(query, c),
      schema: SCORE_SCHEMA,
      schemaName: "score",
      reasoningEffort,
    })),
  );
  if (scored == null) return null; // daemon disabled -> fall back
  const ranked = candidates
    .map((c, i) => ({ c, score: scored[i] && typeof scored[i].score === "number" ? scored[i].score : -1 }))
    .filter((x) => x.score >= scoreFloor)
    .sort((a, b) => b.score - a.score || b.c.score - a.c.score)
    .slice(0, finishMax);
  if (debug) {
    process.stderr.write(`[search-fast] score_ms=${Date.now() - tScore} mode=${mode} survivors=${ranked.length}/${candidates.length}\n`);
  }
  if (!ranked.length) {
    // Pool returned scores but nothing cleared the floor: genuine no-match only
    // if scoring actually ran; if every score is -1 (all failed) fall back.
    const anyScored = scored.some((s) => s && typeof s.score === "number");
    return anyScored ? [] : null;
  }

  if (mode === "score") {
    // A1: host coalesce, no final model turn. Emit survivors as path:lines.
    const files = ranked.map(({ c }) => ({ path: c.path, lines: `${c.startLine}-${c.endLine}` }));
    const validation = validateFinishFiles(repoRoot, files.map((f) => `${f.path}:${f.lines}`).join("\n"));
    if (validation.kind === "ok") return validation.files;
    return validation.kind === "empty" ? [] : null;
  }

  // A2: one final coalesce turn over the survivors so the model can merge them.
  const tRank = Date.now();
  const obj = await daemonAsk(repoRoot, {
    system: RANK_INSTRUCTIONS,
    user: buildRankPrompt(query, ranked.map(({ c }) => c)),
    schema: FINISH_SCHEMA,
    schemaName: "finish",
    reasoningEffort,
  });
  if (debug) process.stderr.write(`[search-fast] coalesce_ms=${Date.now() - tRank} ok=${obj != null}\n`);
  if (!obj || typeof obj.files !== "string") return null;
  const validation = validateFinishFiles(repoRoot, obj.files);
  if (validation.kind === "ok") return validation.files;
  if (validation.kind === "empty") return [];
  return null;
}
