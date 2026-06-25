// Question + repo-map driven seed reads for trace-rt explore.
import { existsSync } from "node:fs";
import { basename } from "node:path";
import { mentionedIdentsFromQuery } from "../map-lib.mjs";
import { confine, toolReadRange, toolGrep } from "./htools.mjs";
import { normalizeReadPath } from "./trace-schema.mjs";
import { pipelineSeedReads } from "./rt-pipeline-seed.mjs";

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const STRIP_PREAMBLE = process.env.EXPLORE_RT_STRIP_COMMENTS !== "0";
const SCRIPT_NAME_RE = /[\w.-]+\.(?:sh|mjs|js|ts|tsx|py|md)\b/gi;
const MAP_LINE_RE = /^([^\s:#]+):(\d+)(?:-(\d+))?/;

function envInt(name, fallback) {
  const v = process.env[name];
  if (v == null || v === "") return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? Math.trunc(n) : fallback;
}

function envBool(name, fallback) {
  const v = process.env[name];
  if (v == null || v === "") return fallback;
  return v === "1" || v.toLowerCase() === "true" || v === "yes";
}

export function extractMapPaths(mapBlock) {
  if (!mapBlock || typeof mapBlock !== "string") return [];
  const paths = new Set();
  for (const line of mapBlock.split("\n")) {
    const t = line.trim();
    if (!t || t.startsWith("#") || t.startsWith("<") || t.startsWith("##")) continue;
    const m = t.match(/^([^\s:]+(?::\d+(?:-\d+)?)?)/);
    if (!m) continue;
    const raw = m[1].split(":")[0];
    if (raw.includes("/") || raw.endsWith(".sh") || raw.endsWith(".mjs") || raw.endsWith(".js")) {
      paths.add(raw.replace(/^\.\//, ""));
    }
  }
  return [...paths];
}

export function parseMapLineRanges(mapBlock) {
  if (!mapBlock) return [];
  const out = [];
  for (const line of mapBlock.split("\n")) {
    const t = line.trim();
    if (!t || t.startsWith("#")) continue;
    const m = t.match(MAP_LINE_RE);
    if (!m) continue;
    const path = m[1].replace(/^\.\//, "");
    const start = Number(m[2]);
    const end = Number(m[3] || m[2]);
    if (!path.includes("/")) continue;
    out.push({ path, start_line: start, end_line: end });
  }
  return out;
}

export function namedPathsFromQuestion(question) {
  if (!question) return [];
  const names = new Set();
  for (const m of String(question).matchAll(SCRIPT_NAME_RE)) {
    names.add(m[0]);
  }
  return [...names];
}

function resolveCandidate(workspace, candidate) {
  const tries = candidate.includes("/")
    ? [candidate]
    : [`scripts/${candidate}`, candidate];
  for (const p of tries) {
    const abs = confine(workspace, p);
    if (abs && existsSync(abs)) {
      const rel = normalizeReadPath(workspace, p);
      if (rel) return rel;
    }
  }
  return null;
}

const CURATED_TRACE_SEEDS = [
  { path: "scripts/trace.sh", start_line: 1, end_line: 20 },
  { path: "scripts/trace-rt.sh", start_line: 1, end_line: 120 },
  { path: "scripts/realtime-trace.mjs", start_line: 1, end_line: 80 },
];

export function requiredSeedPaths(question, workspace) {
  const out = [];
  for (const name of namedPathsFromQuestion(question)) {
    const rel = resolveCandidate(workspace, name);
    if (rel) out.push(rel);
  }
  const q = String(question || "").toLowerCase();
  if (/\btrace\b/.test(q)) {
    for (const p of ["scripts/trace.sh", "scripts/trace-rt.sh", "scripts/realtime-trace.mjs"]) {
      const rel = resolveCandidate(workspace, p);
      if (rel && !out.includes(rel)) out.push(rel);
    }
  }
  return out;
}

function curatedTraceSeeds(question, workspace) {
  const q = String(question || "").toLowerCase();
  if (!/\btrace\b/.test(q)) return [];
  const out = [];
  for (const spec of CURATED_TRACE_SEEDS) {
    const rel = resolveCandidate(workspace, spec.path);
    if (rel) out.push({ rel, ...spec });
  }
  return out;
}

function readSeedSpec(workspace, spec, onRead, filesRead, readCache, lines) {
  const rel = typeof spec === "string" ? resolveCandidate(workspace, spec) : spec.rel || resolveCandidate(workspace, spec.path);
  if (!rel) return null;
  const start = spec.start_line || 1;
  const end = spec.end_line || lines;
  const r = toolReadRange(workspace, rel, { start_line: start, end_line: end, stripPreamble: STRIP_PREAMBLE });
  if (!r.ok) return null;
  if (onRead) onRead(rel, r.content || "");
  else {
    filesRead.add(rel);
    readCache.set(rel, r.content || "");
  }
  return rel;
}

export function deriveSeedPaths(question, mapBlock, workspace, { max = 4 } = {}) {
  const ranked = [];
  const seen = new Set();

  const push = (p) => {
    const rel = resolveCandidate(workspace, p);
    if (!rel || seen.has(rel)) return;
    seen.add(rel);
    ranked.push(rel);
  };

  for (const p of requiredSeedPaths(question, workspace)) push(p);

  const idents = mentionedIdentsFromQuery(question);
  for (const p of extractMapPaths(mapBlock)) {
    if (ranked.length >= max) break;
    const base = basename(p).replace(/\.[a-z0-9]+$/i, "").toLowerCase();
    const name = basename(p).toLowerCase();
    if ([...idents].some((id) => base.includes(id.toLowerCase()) || id.toLowerCase().includes(base) || name.includes(id.toLowerCase()))) {
      push(p);
    }
  }

  const q = String(question || "").toLowerCase();
  for (const p of extractMapPaths(mapBlock)) {
    if (ranked.length >= max) break;
    if (p.startsWith("scripts/")) {
      if (p.includes("cursor-acp") && !/\b(cursor|acp)\b/.test(q)) continue;
      push(p);
    }
  }

  return ranked.slice(0, max);
}

const SYMBOL_RE = /[A-Za-z_][A-Za-z0-9_]{3,}/g;
// Docs, data, and fixtures match identifiers textually but are not where code is
// defined — seeding them buries the real source under noise.
const NON_SOURCE_RE = /\.(md|markdown|json|ndjson|jsonl|txt|log|csv|tsv|ya?ml|toml|lock|svg|png|jpe?g|gif|ico)$/i;
function isSeedableSource(rel) {
  if (!rel || NON_SOURCE_RE.test(rel)) return false;
  if (/(^|\/)fixtures\//.test(rel)) return false;
  return true;
}

// Code-symbol identifiers worth grepping for, extracted CASE-PRESERVED from the
// raw question (mentionedIdentsFromQuery lowercases, which breaks camelCase
// matching). Only strong signals — snake_case, SCREAMING_CASE, camelCase — so
// plain English words ("convert", "command") are not grepped.
function codeSymbolIdents(question) {
  const out = [];
  const seen = new Set();
  for (const m of String(question || "").matchAll(SYMBOL_RE)) {
    const id = m[0];
    if (seen.has(id)) continue;
    const symbolish = id.includes("_") || /[a-z][A-Z]/.test(id) || /^[A-Z][A-Z0-9]{2,}$/.test(id);
    if (!symbolish) continue;
    seen.add(id);
    out.push(id);
  }
  return out;
}

// Score a grep hit by how much it looks like the DEFINITION of `ident` (not a
// call site or import). Declarations win; bare calls score low. Case-sensitive
// against the original-case ident so camelCase matches exactly.
function pickDefHit(matches, ident) {
  const decl = new RegExp(`\\b(function|class|def|const|let|var|interface|type|enum|struct|fn)\\s+${ident}\\b`);
  const keyOrAssign = new RegExp(`(^|\\s)${ident}\\s*[:=]`);
  const call = new RegExp(`\\b${ident}\\s*\\(`);
  let best = null;
  for (const m of matches) {
    const c = String(m.content || "");
    let score = 0;
    if (decl.test(c)) score += 3;
    if (keyOrAssign.test(c)) score += 3;
    if (/\bexport\b/.test(c)) score += 1;
    if (call.test(c)) score += 1;
    if (!best || score > best.score) best = { lineNumber: m.lineNumber, score };
    if (best.score >= 4) break;
  }
  return best;
}

// Seed a read window centered on the *definition* of each mentioned identifier,
// so the read cache holds bodies (not just file headers) that the submit model
// can cite. Returns the relative paths seeded.
export function grepHitSeeds({
  workspace,
  question,
  onRead,
  max = envInt("EXPLORE_RT_GREP_SEED_MAX", 8),
  before = envInt("EXPLORE_RT_GREP_SEED_BEFORE", 12),
  after = envInt("EXPLORE_RT_GREP_SEED_AFTER", 28),
}) {
  const added = [];
  const local = new Set();
  for (const ident of codeSymbolIdents(question)) {
    if (added.length >= max) break;
    let g;
    try { g = toolGrep(workspace, { pattern: ident }); } catch { continue; }
    if (!g?.ok || !g.fileMatches?.length) continue;

    let choice = null;
    for (const fm of g.fileMatches) {
      const file = String(fm.file || "").replace(/^\.\//, "");
      const rel = normalizeReadPath(workspace, file) || resolveCandidate(workspace, file);
      if (!rel || !isSeedableSource(rel)) continue;
      const hit = pickDefHit(fm.matches || [], ident);
      if (!hit || hit.score < 2) continue;
      // Strictly-greater keeps the first (best-scoring) source file; ties do not
      // flip to a later alphabetical path.
      if (!choice || hit.score > choice.hit.score) choice = { rel, hit };
      if (choice.hit.score >= 4) break;
    }
    if (!choice) continue;
    // Read the definition window even if the file was already seeded as a header
    // elsewhere — different region, merged into the cache. Only skip if this exact
    // file was already grep-seeded in this call.
    if (local.has(choice.rel)) continue;

    const start = Math.max(1, choice.hit.lineNumber - before);
    const end = choice.hit.lineNumber + after;
    const r = toolReadRange(workspace, choice.rel, { start_line: start, end_line: end, stripPreamble: STRIP_PREAMBLE });
    if (!r.ok) continue;
    // Pin: this is the answer location; it must survive later, less-relevant reads.
    if (onRead) onRead(choice.rel, r.content || "", { pin: true });
    local.add(choice.rel);
    added.push(choice.rel);
  }
  return added;
}

export function seedExploreReads({
  workspace,
  question,
  mapBlock,
  filesRead,
  readCache,
  onRead,
  max = envInt("EXPLORE_RT_SEED_MAX", 4),
  lines = envInt("EXPLORE_RT_SEED_LINES", 120),
}) {
  const seedFromMap = envBool("EXPLORE_RT_SEED_FROM_MAP", true);
  const paths = [];
  const seen = new Set();

  const record = (rel) => {
    if (!rel || seen.has(rel)) return;
    seen.add(rel);
    paths.push(rel);
  };

  // Definition-centered seeds FIRST: identifiers named in the question are the
  // answer locations, so they lead the priority order (and can never be cut from
  // the READ INDEX). Own budget — does not consume the curated/map/derive budget.
  const grepAdded = grepHitSeeds({ workspace, question, onRead });
  for (const rel of grepAdded) record(rel);
  max += grepAdded.length;

  if (seedFromMap) {
    for (const spec of curatedTraceSeeds(question, workspace)) {
      if (paths.length >= max) break;
      const rel = readSeedSpec(workspace, spec, onRead, filesRead, readCache, lines);
      if (rel) record(rel);
    }
    const mapRanges = parseMapLineRanges(mapBlock);
    const idents = mentionedIdentsFromQuery(question);
    for (const range of mapRanges) {
      if (paths.length >= max) break;
      const rel = resolveCandidate(workspace, range.path);
      if (!rel || seen.has(rel)) continue;
      const base = basename(rel).replace(/\.[a-z0-9]+$/i, "").toLowerCase();
      const identHit = [...idents].some((id) => base.includes(id.toLowerCase()) || id.toLowerCase().includes(base));
      if (!identHit && !requiredSeedPaths(question, workspace).includes(rel)) continue;
      const r = readSeedSpec(workspace, { rel, start_line: range.start_line, end_line: range.end_line }, onRead, filesRead, readCache, lines);
      if (r) record(r);
    }
  }

  for (const rel of deriveSeedPaths(question, mapBlock, workspace, { max })) {
    if (paths.length >= max) break;
    if (seen.has(rel)) continue;
    const defaultLines = seedFromMap && parseMapLineRanges(mapBlock).some((r) => r.path === rel.split("/").pop() || r.path === rel)
      ? Math.min(lines, 80)
      : lines;
    const r = readSeedSpec(workspace, { rel, start_line: 1, end_line: defaultLines }, onRead, filesRead, readCache, lines);
    if (r) record(r);
  }

  const pipelineAdded = pipelineSeedReads(question, workspace, filesRead, onRead);
  for (const rel of pipelineAdded) record(rel);

  return paths;
}

export function shouldStopExplore({
  filesRead,
  question,
  workspace,
  toolTurnCount,
  minReads = envInt("EXPLORE_RT_EXPLORE_MIN_READS", 4),
  stopReads = envInt("EXPLORE_RT_STOP_READS", 6),
  stopToolCalls = envInt("EXPLORE_RT_STOP_TOOL_CALLS", 2),
}) {
  if (filesRead.size >= stopReads) return true;
  if (toolTurnCount >= stopToolCalls && filesRead.size >= minReads) return true;
  const required = requiredSeedPaths(question, workspace);
  if (filesRead.size >= minReads && required.every((p) => filesRead.has(p))) return true;
  return false;
}

export function preflightExploreExecCode(code) {
  if (!code || !String(code).trim()) return { ok: false, error: "explore_exec: empty code" };
  try {
    // eslint-disable-next-line no-new-func
    new AsyncFunction("tools", `"use strict";\n${code}`);
    return { ok: true };
  } catch (e) {
    return {
      ok: false,
      error: `explore_exec syntax error: ${e.message}`,
      hint: "Fix JavaScript syntax before retrying; use valid destructuring and await.",
    };
  }
}
