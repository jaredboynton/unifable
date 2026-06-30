// search-seed.mjs — host-side query-symbol definition seeder for explore search.
//
// Before turn 1, grep the query's strong code symbols, pick the best DEFINITION
// hit per symbol, hydrate the enclosing function/class (AST via ast-grep, or a
// clamped line window fallback), and return ready-to-cite code windows. Injected
// into the initial state so the model can finish without a discovery turn.
//
// Self-contained (no trace internals). Runs over ripgrep host-side in ~tens of
// ms and overlaps the socket warm, so it adds ~nothing to the critical path.
//
// Env:
//   UNITRACE_SEARCH_SEED          set to 0 to disable (default: on)
//   UNITRACE_SEARCH_SEED_MAX      max seed windows (default: 6)
//   UNITRACE_SEARCH_SEED_BEFORE   fallback lines before hit (default: 10)
//   UNITRACE_SEARCH_SEED_AFTER    fallback lines after hit (default: 30)

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { detectAstBinary, ensureAstTool, expandLineRange, langForPath, stripCommentsEnabled } from "./ast-context.mjs";
import { makeLineHider } from "./lib/code-line.mjs";

const SYMBOL_RE = /[A-Za-z_][A-Za-z0-9_]{3,}/g;
const NON_SOURCE_RE = /\.(md|markdown|json|ndjson|jsonl|txt|log|csv|tsv|ya?ml|toml|lock|svg|png|jpe?g|gif|ico)$/i;
const SEED_EXCLUDES = [
  ".git", "node_modules", ".pnpm", ".yarn", "vendor", "Pods", ".bundle",
  "__pycache__", ".venv", "venv", "dist", "build", "out", "target", ".next", ".nuxt",
  ".cache", ".turbo", "generated", "*.min.js", "*.min.css", "*.map", "*.generated.*",
];

function envInt(name, fallback) {
  const v = process.env[name];
  if (v == null || v === "") return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? Math.trunc(n) : fallback;
}

export function seedEnabled() {
  return process.env.UNITRACE_SEARCH_SEED !== "0";
}

// Strong code symbols only: snake_case, camelCase, or SCREAMING_CASE. Plain
// English words ("convert", "command") are skipped so we grep signal, not noise.
export function codeSymbolIdents(query) {
  const out = [];
  const seen = new Set();
  for (const m of String(query || "").matchAll(SYMBOL_RE)) {
    const id = m[0];
    if (seen.has(id)) continue;
    const symbolish = id.includes("_") || /[a-z][A-Z]/.test(id) || /^[A-Z][A-Z0-9]{2,}$/.test(id);
    if (!symbolish) continue;
    seen.add(id);
    out.push(id);
  }
  return out;
}

// Plain-English content words removed: prose-noun seeding was too noisy in
// large polyglot repos (generic words grep into unrelated files), and bad seeds
// hurt more than no seeds. Only exact code symbols are seeded now.
function isSeedableSource(rel) {
  if (!rel || NON_SOURCE_RE.test(rel)) return false;
  if (/(^|\/)fixtures\//.test(rel)) return false;
  return Boolean(langForPath(rel));
}

// Score a match by how much it looks like the DEFINITION of `ident` (not a call
// site or import). Declarations win; bare calls score low. Case-sensitive.
export function pickDefHit(matches, ident) {
  const decl = new RegExp(`\\b(function|class|def|const|let|var|interface|type|enum|struct|fn|impl|trait)\\s+${ident}\\b`);
  const keyOrAssign = new RegExp(`(^|\\s)${ident}\\s*[:=]`);
  const call = new RegExp(`\\b${ident}\\s*\\(`);
  const envLike = /^[A-Z][A-Z0-9_]{2,}$/.test(ident);
  const envRef = envLike
    ? new RegExp(`\\b(?:process\\.env\\.|env\\[['"]?|\\$\\{?)${ident}\\b`)
    : null;
  let best = null;
  for (const m of matches) {
    const c = String(m.text || "");
    let score = 0;
    if (decl.test(c)) score += 3;
    if (keyOrAssign.test(c)) score += 3;
    if (envRef?.test(c)) score += 3;
    if (/\bexport\b/.test(c)) score += 1;
    if (call.test(c)) score += 1;
    if (!best || score > best.score) best = { line: m.line, score };
    if (best.score >= 4) break;
  }
  return best;
}

function rgMatches(repoRoot, ident) {
  const args = [
    "--no-config", "--no-heading", "--with-filename", "--line-number",
    "--color=never", "--trim", "--max-columns=400", "--case-sensitive",
    ...SEED_EXCLUDES.flatMap((e) => ["-g", `!${e}`]),
    `\\b${ident}\\b`,
    ".",
  ];
  const res = spawnSync("rg", args, { cwd: repoRoot, encoding: "utf8", maxBuffer: 8 * 1024 * 1024 });
  if (res.status !== 0 && res.status !== 1) return [];
  const byFile = new Map();
  for (const line of (res.stdout || "").split(/\r?\n/)) {
    if (!line.trim()) continue;
    const m = line.match(/^(.+?):(\d+):(.*)$/);
    if (!m) continue;
    const file = m[1].replace(/^\.\//, "");
    if (!isSeedableSource(file)) continue;
    if (!byFile.has(file)) byFile.set(file, []);
    byFile.get(file).push({ line: parseInt(m[2], 10), text: m[3] });
  }
  return [...byFile.entries()].map(([file, matches]) => ({ file, matches }));
}

function readWindow(absPath, startLine, endLine) {
  let raw;
  try { raw = fs.readFileSync(absPath, "utf8"); } catch { return ""; }
  const all = raw.split(/\r?\n/);
  const s = Math.max(1, startLine);
  const e = Math.min(endLine, all.length);
  if (!stripCommentsEnabled()) return all.slice(s - 1, e).join("\n");
  // Feed the hider from line 1 so a block comment opened above the window is
  // tracked; emit only survivors within [s, e].
  const hide = makeLineHider(absPath);
  const out = [];
  for (let i = 1; i <= e; i++) {
    const hidden = hide(all[i - 1] ?? "");
    if (i < s || hidden) continue;
    out.push(all[i - 1] ?? "");
  }
  return out.join("\n");
}

// Grep the query's symbols, pick the best definition per symbol, hydrate the
// enclosing node, and return deduped code windows: [{ path, startLine, endLine, content }].
export function seedSearchHits(repoRoot, query, {
  maxSeeds = envInt("UNITRACE_SEARCH_SEED_MAX", 6),
  before = envInt("UNITRACE_SEARCH_SEED_BEFORE", 10),
  after = envInt("UNITRACE_SEARCH_SEED_AFTER", 30),
} = {}) {
  if (!seedEnabled()) return [];
  const symbols = codeSymbolIdents(query);
  if (!symbols.length) return [];

  ensureAstTool({ install: process.env.UNITRACE_AST_SKIP_INSTALL !== "1" });
  const binary = detectAstBinary();

  const seeds = [];
  const seenFiles = new Set();

  const addSeed = (file, line) => {
    if (seenFiles.has(file)) return false;
    const abs = path.resolve(repoRoot, file);
    let s = line;
    let e = line;
    if (binary) {
      const exp = expandLineRange(abs, line, line, { binary });
      s = exp.startLine;
      e = exp.endLine;
    }
    if (s === line && e === line) {
      s = Math.max(1, line - before);
      e = line + after;
    }
    const content = readWindow(abs, s, e);
    if (!content.trim()) return false;
    seenFiles.add(file);
    seeds.push({ path: file, startLine: s, endLine: e, content });
    return true;
  };

  // Pass 1: exact code symbols named in the query (highest precision). Collect
  // the best def hit per file, then only seed when there is a unique top scorer.
  // If two files tie (e.g. a generator that embeds the same source as a string),
  // seed nothing and let the model's grep loop disambiguate.
  for (const ident of symbols) {
    if (seeds.length >= maxSeeds) break;
    const fileGroups = rgMatches(repoRoot, ident);
    if (!fileGroups.length) continue;

    const ranked = [];
    for (const fg of fileGroups) {
      if (seenFiles.has(fg.file)) continue;
      const hit = pickDefHit(fg.matches, ident);
      if (!hit || hit.score < 2) continue;
      ranked.push({ file: fg.file, hit });
    }
    if (!ranked.length) continue;
    ranked.sort((a, b) => b.hit.score - a.hit.score);
    const top = ranked[0];
    const runnerUp = ranked[1];
    const confident = !runnerUp || top.hit.score - runnerUp.hit.score >= 1;
    if (!confident) continue;
    addSeed(top.file, top.hit.line);
  }

  return seeds;
}
