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
//   EXPLORE_SEARCH_FAST_MAX_FILES      max ranked files to hydrate (default 12)
//   EXPLORE_SEARCH_FAST_MAX_SPANS      max total hydrated spans/candidates (default 32)
//   EXPLORE_SEARCH_FAST_SPANS_PER_FILE max AST-node spans per file (default 6)
//   EXPLORE_SEARCH_FAST_HYDRATE_SPAN   max lines per hydrated span (default 40)
//   EXPLORE_SEARCH_FAST_GREP_CAP       max rg matches parsed (default 4000)

import { execFile } from "node:child_process";
import {
  detectAstBinary,
  ensureAstTool,
  hydrateHitsToBlocks,
  langForPath,
} from "./ast-context.mjs";
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

// Classify combined-rg hits into per-file stats for FILE-LEVEL ranking. "def"
// means the line declares a name containing one of the terms (DECL keyword +
// term); everything else is a "ref". Tracks distinct matched terms, per-hit
// matched terms (for rarity-weighted span selection), and document frequency
// per term (rare terms are the distinctive signal).
export function classifyHits(hits, terms) {
  const declRe = terms.length
    ? new RegExp(`\\b(${DECL_KW})\\s+\\w*(${terms.join("|")})\\w*`, "i")
    : null;
  const termRes = terms.map((t) => new RegExp(`\\b${t}`, "i"));
  const files = new Map();
  const docFreq = new Map(); // term -> # files containing it
  for (const h of hits) {
    let f = files.get(h.file);
    if (!f) { f = { file: h.file, def: 0, ref: 0, terms: new Set(), hits: [] }; files.set(h.file, f); }
    const isDef = declRe ? declRe.test(h.text) : false;
    if (isDef) f.def += 1; else f.ref += 1;
    const matched = [];
    for (let i = 0; i < terms.length; i++) {
      if (termRes[i].test(h.text)) { matched.push(terms[i]); if (!f.terms.has(terms[i])) docFreq.set(terms[i], (docFreq.get(terms[i]) || 0) + 1); f.terms.add(terms[i]); }
    }
    f.hits.push({ file: h.file, line: h.line, isDef, matched });
  }
  return { files, docFreq };
}

// Score per-file stats for ranking. def is capped (a real implementation rarely
// has >6 matching declarations; 12+ signals a generator/data file -> penalize),
// refs contribute marginally, multi-term coverage and central-dir placement
// dominate. Returns files ordered best-first, each carrying its hits (ordered by
// rarity-weighted term coverage then def) so the caller hydrates the most
// distinctive matches into AST-node spans first.
export function scoreCandidates({ files, docFreq }) {
  const nFiles = files.size || 1;
  // Rarity weight: rare terms (low document frequency) carry more signal than
  // terms that hit nearly every file. Adds a small base so a single common term
  // still counts.
  const rarity = (t) => 1 + Math.log(nFiles / (1 + (docFreq.get(t) || 0)));
  const scored = [];
  for (const f of files.values()) {
    const defCap = Math.min(f.def, 6);
    const overflow = f.def > 12 ? -4 : 0;
    let score = defCap * 4 + Math.min(f.ref, 3) + (f.terms.size - 1) * 6 + overflow;
    if (CENTRAL_RE.test(f.file)) score += 5;
    if (GENERATOR_RE.test(f.file)) score -= 4;
    const hitWeight = (h) => h.matched.reduce((s, t) => s + rarity(t), 0) + (h.isDef ? 2 : 0);
    const hits = [...f.hits].sort((a, b) => hitWeight(b) - hitWeight(a) || a.line - b.line);
    scored.push({ file: f.file, score, hits, defCount: f.def, refCount: f.ref, termCount: f.terms.size });
  }
  scored.sort((a, b) => b.score - a.score || a.file.localeCompare(b.file));
  return scored;
}

// Combined rg -> file-level rank -> per-file AST-node multi-span hydration.
// Returns one candidate PER SPAN so a scattered answer (helper + core fn far
// apart in the same file) yields a candidate for each, and the parallel scorer
// rates every span on its own merit. Spans are produced by the shared
// `hydrateHitsToBlocks` (the proven grep-hydration path: enclosing AST node,
// per-file dedup, comment-strip, clamp). Returns
// { candidates:[{path,startLine,endLine,content,score}], terms }.
export async function retrieveCandidates(repoRoot, query, {
  maxFiles = envInt("EXPLORE_SEARCH_FAST_MAX_FILES", 12),
  maxSpans = envInt("EXPLORE_SEARCH_FAST_MAX_SPANS", 32),
  spansPerFile = envInt("EXPLORE_SEARCH_FAST_SPANS_PER_FILE", 6),
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

  // ONE combined ripgrep; classify + rank files in node, then hydrate the top
  // files' hits into AST-node spans.
  const hits = await runCombinedRg(repoRoot, terms, grepCap);
  const ranked = scoreCandidates(classifyHits(hits, terms)).slice(0, maxFiles);

  const candidates = [];
  for (const r of ranked) {
    if (candidates.length >= maxSpans) break;
    const blocks = hydrateHitsToBlocks(repoRoot, r.hits, {
      maxBlocks: spansPerFile,
      binary,
      maxSpan: hydrateSpan,
    });
    for (const b of blocks) {
      if (candidates.length >= maxSpans) break;
      candidates.push({ path: b.path, startLine: b.startLine, endLine: b.endLine, content: b.content, score: r.score });
    }
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

export const SCORE_INSTRUCTIONS = [
  "You are a code-search relevance scorer. You will be given a QUERY and exactly one CODE window.",
  "Decide how directly the CODE answers the QUERY, then return a single integer 0-10 via the score tool.",
  "Use this exact scale:",
  "0-1: unrelated code (the window is about something else).",
  "2-3: same general area but does not answer the query (an import, a type, a constant, an incidental mention).",
  "4-5: related supporting code (a caller, a test, a helper) that touches the answer but is not the answer.",
  "6-7: part of the answer (one of the functions/blocks that implements what the query asks about).",
  "8-10: the definitive answer (this window contains the core definition/implementation the query is asking for).",
  "Judge ONLY this window's own code. Reward definitions and real implementations; do not reward files just for",
  "mentioning the query words. Score conservatively and consistently. Return only the integer score.",
].join("\n");

const SCORE_SCHEMA = {
  type: "object",
  properties: { score: { type: "integer", description: "0-10 relevance per the scale: 0-1 unrelated, 4-5 supporting, 6-7 part of the answer, 8-10 the definitive answer" } },
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

export function scorePromptFor(query, c) {
  return [
    "QUERY:",
    query,
    "",
    `CODE (${c.path} lines ${c.startLine}-${c.endLine}):`,
    c.content,
    "",
    "How directly does this CODE answer the QUERY? Return the integer score 0-10 now.",
  ].join("\n");
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
  const scoreFloor = envInt("EXPLORE_SEARCH_SCORE_MIN", 4);
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
  const survivors = candidates
    .map((c, i) => ({ c, score: scored[i] && typeof scored[i].score === "number" ? scored[i].score : -1 }))
    .filter((x) => x.score >= scoreFloor)
    .sort((a, b) => b.score - a.score || b.c.score - a.c.score);
  if (debug) {
    process.stderr.write(`[search-fast] score_ms=${Date.now() - tScore} mode=${mode} survivors=${survivors.length}/${candidates.length}\n`);
  }
  if (!survivors.length) {
    // Pool returned scores but nothing cleared the floor: genuine no-match only
    // if scoring actually ran; if every score is -1 (all failed) fall back.
    const anyScored = scored.some((s) => s && typeof s.score === "number");
    return anyScored ? [] : null;
  }

  if (mode === "score") {
    // A1: host coalesce, no final model turn. Group surviving spans BY FILE and
    // merge their ranges (path:a-b,c-d), so a file whose answer spans multiple
    // AST nodes is returned with every relevant range. Files are ordered by best
    // span score and capped to FINISH_MAX.
    const byFile = new Map(); // path -> { best, ranges:[[s,e]] }
    for (const { c, score } of survivors) {
      let f = byFile.get(c.path);
      if (!f) { f = { best: score, ranges: [] }; byFile.set(c.path, f); }
      f.best = Math.max(f.best, score);
      f.ranges.push([c.startLine, c.endLine]);
    }
    const ordered = [...byFile.entries()]
      .sort((a, b) => b[1].best - a[1].best || a[0].localeCompare(b[0]))
      .slice(0, finishMax);
    const lines = ordered.map(([p, f]) => {
      const ranges = f.ranges.sort((a, b) => a[0] - b[0]).map(([s, e]) => `${s}-${e}`).join(",");
      return `${p}:${ranges}`;
    });
    const validation = validateFinishFiles(repoRoot, lines.join("\n"));
    if (validation.kind === "ok") return validation.files;
    return validation.kind === "empty" ? [] : null;
  }

  // A2: one final coalesce turn over the survivors so the model can merge them.
  const ranked = survivors.slice(0, finishMax);
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
