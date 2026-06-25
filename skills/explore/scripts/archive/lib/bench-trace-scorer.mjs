#!/usr/bin/env node
// bench-trace-scorer.mjs — robust quality metrics for trace bench outputs.
//
// Extracts citations from line-start fences, inline fences, path-first refs,
// structured JSON sidecars, and ref labels. Shared aggregates via bench-scorer-common.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  countChars,
  countMarkdownHeadings,
  isNonEmpty,
  median,
  normalizePath,
  percentile,
} from "./bench-scorer-common.mjs";
import {
  extractFileTokens,
  isExploreWireFormat,
  sectionScoreWire,
  wireComplianceScore,
} from "./explore-wire-format.mjs";
import { detectAstBinary, langForPath, listAstNodes } from "../ast-context.mjs";
import { isPreambleLine } from "./code-line.mjs";

const LINE_START_FENCE_RE = /^```(\d+):(\d+):([^\n`]+)/gm;
const INLINE_FENCE_RE = /```(\d+):(\d+):([^`\s]+)```/g;
const PATH_FIRST_RE = /([\w./-]+\.(?:sh|mjs|js|py|md|ts|tsx|json)):(\d+):(\d+)/g;
const REF_LABEL_RE = /^<ref(\d+)>/gm;

const SEMANTIC_SECTION_MARKERS = [
  { key: "flow", re: /\b(flow|end-to-end workflow|pipeline|data flow)\b/i },
  { key: "keyFiles", re: /\b(key files?|key components?|important files?)\b/i },
  { key: "codeRefs", re: /\b(code references?|code passages?|citations?)\b/i },
  { key: "purpose", re: /\b(purpose|overview|summary|trace)\b/i },
];

function citationKey(pathValue, start, end) {
  return `${normalizePath(pathValue)}:${start}:${end}`;
}

function addCitation(map, kind, pathValue, start, end) {
  const p = normalizePath(pathValue);
  const s = Number(start);
  const e = Number(end);
  if (!p || !Number.isFinite(s) || !Number.isFinite(e) || s < 1 || e < s) return;
  const key = citationKey(p, s, e);
  if (map.has(key)) return;
  map.set(key, { path: p, startLine: s, endLine: e, kind });
}

function extractFromStructuredJson(structuredJsonPath) {
  const byKind = { structured: 0 };
  const map = new Map();
  if (!structuredJsonPath || !fs.existsSync(structuredJsonPath)) {
    return { map, byKind };
  }
  let data;
  try {
    data = JSON.parse(fs.readFileSync(structuredJsonPath, "utf8"));
  } catch {
    return { map, byKind };
  }
  const passages = Array.isArray(data?.code_passages) ? data.code_passages : [];
  for (const passage of passages) {
    if (!passage || typeof passage !== "object") continue;
    const p = passage.file_path || passage.path || passage.file;
    const start = passage.start_line ?? passage.startLine ?? passage.start;
    const end = passage.end_line ?? passage.endLine ?? passage.end;
    const before = map.size;
    addCitation(map, "structured", p, start, end);
    if (map.size > before) byKind.structured += 1;
  }
  return { map, byKind };
}

export function extractTraceCitations(markdown, { structuredJsonPath } = {}) {
  const text = markdown || "";
  const map = new Map();
  const byKind = {
    lineStartFence: 0,
    inlineFence: 0,
    pathFirst: 0,
    structured: 0,
    refLabel: 0,
  };

  for (const m of text.matchAll(LINE_START_FENCE_RE)) {
    const before = map.size;
    addCitation(map, "lineStartFence", m[3], m[1], m[2]);
    if (map.size > before) byKind.lineStartFence += 1;
  }

  for (const m of text.matchAll(INLINE_FENCE_RE)) {
    const before = map.size;
    addCitation(map, "inlineFence", m[3], m[1], m[2]);
    if (map.size > before) byKind.inlineFence += 1;
  }

  for (const m of text.matchAll(PATH_FIRST_RE)) {
    const before = map.size;
    addCitation(map, "pathFirst", m[1], m[2], m[3]);
    if (map.size > before) byKind.pathFirst += 1;
  }

  byKind.refLabel = (text.match(REF_LABEL_RE) || []).length;

  const structured = extractFromStructuredJson(structuredJsonPath);
  for (const [key, cite] of structured.map) {
    if (!map.has(key)) {
      map.set(key, cite);
      byKind.structured += 1;
    }
  }

  return {
    all: [...map.values()],
    uniqueCitations: map.size,
    uniquePaths: new Set([...map.values()].map((c) => c.path)).size,
    byKind,
  };
}

export function scoreTraceSections(markdown) {
  const text = markdown || "";
  const headings = countMarkdownHeadings(text);
  const semanticMarkers = {};
  let semanticHits = 0;
  for (const { key, re } of SEMANTIC_SECTION_MARKERS) {
    const hit = re.test(text);
    semanticMarkers[key] = hit;
    if (hit) semanticHits += 1;
  }
  const sectionScore = Math.max(headings.total, semanticHits);
  return { headings, semanticMarkers, semanticHits, sectionScore };
}

export function scoreTraceStructure(markdown) {
  const text = markdown || "";
  const hasFlow = /\b(flow|end-to-end|pipeline|data flow)\b/i.test(text) || /^## Flow\b/m.test(text);
  const hasKeyFiles = /\b(key files?|key components?)\b/i.test(text) || /^## Key files\b/m.test(text);
  const hasCodeRefs =
    /\b(code references?|code passages?)\b/i.test(text) || /^## Code references\b/m.test(text);
  const hasComparisonTable = /\|.+\|\s*\n\|[-:\s|]+\|/m.test(text);

  let completenessScore = 0;
  if (hasFlow) completenessScore += 1;
  if (hasKeyFiles) completenessScore += 1;
  if (hasCodeRefs) completenessScore += 1;

  return {
    hasFlow,
    hasKeyFiles,
    hasCodeRefs,
    hasComparisonTable,
    completenessScore,
  };
}

// --- Citation grounding ----------------------------------------------------
// A citation is "grounded" when its line span points at real, load-bearing code
// rather than a file header (shebang / top comment / import block) — the 1:3
// failure mode. Three strategies, strongest-first, each falling back when its
// signal is unavailable:
//   ast     — span overlaps a real definition node (ast-grep / tree-sitter)
//   content — span contains >=1 substantive (non-preamble) source line
//   shape   — span is not a degenerate file-top span (needs no file access)
// Prior art: grounded-citation eval requires cited [file:start-end] ranges to
// overlap real code via interval arithmetic (arXiv:2512.12117); ast-grep yields
// definition node line ranges (https://ast-grep.github.io).
const HEADER_TOP_MAX = 5;

function resolveCitedFile(workspace, relPath) {
  if (!workspace || !relPath) return null;
  const root = path.resolve(workspace);
  const abs = path.resolve(root, relPath);
  if (abs !== root && !abs.startsWith(`${root}${path.sep}`)) return null;
  try {
    if (!fs.statSync(abs).isFile()) return null;
  } catch {
    return null;
  }
  return abs;
}

function groundedByShape(cite) {
  // Ungrounded only when the span sits entirely in the file-top region.
  return !(cite.startLine === 1 && cite.endLine <= HEADER_TOP_MAX);
}

function groundedByContent(absPath, cite) {
  let lines;
  try {
    lines = fs.readFileSync(absPath, "utf8").split(/\r?\n/);
  } catch {
    return null;
  }
  if (cite.startLine > lines.length) return false; // out-of-range / fabricated
  const end = Math.min(cite.endLine, lines.length);
  for (let i = cite.startLine; i <= end; i += 1) {
    if (!isPreambleLine(lines[i - 1] ?? "")) return true;
  }
  return false;
}

function groundedByAst(absPath, cite, bin) {
  const lang = langForPath(absPath);
  if (!lang) return null;
  const nodes = listAstNodes(absPath, { binary: bin, lang });
  if (!nodes.length) return null;
  for (const node of nodes) {
    if (node.endLine >= cite.startLine && node.startLine <= cite.endLine) return true;
  }
  return false;
}

export function classifyCitationGrounded(cite, { workspace = null, strategy = "auto", astBinary } = {}) {
  if (!cite || !Number.isFinite(cite.startLine) || !Number.isFinite(cite.endLine)) return false;
  if (strategy === "shape") return groundedByShape(cite);
  const abs = resolveCitedFile(workspace, cite.path);
  if (!abs) return groundedByShape(cite);

  if (strategy === "ast" || strategy === "auto") {
    const bin = astBinary ?? detectAstBinary();
    if (bin) {
      const viaAst = groundedByAst(abs, cite, bin);
      if (viaAst !== null) return viaAst;
    }
  }
  const viaContent = groundedByContent(abs, cite);
  if (viaContent !== null) return viaContent;
  return groundedByShape(cite);
}

export function annotateGrounding(citationList, opts = {}) {
  const list = Array.isArray(citationList) ? citationList : [];
  const bin = opts.workspace && opts.strategy !== "shape" ? (opts.astBinary ?? detectAstBinary()) : null;
  let grounded = 0;
  const citations = list.map((c) => {
    const g = classifyCitationGrounded(c, { ...opts, astBinary: bin });
    if (g) grounded += 1;
    return { ...c, grounded: g };
  });
  return {
    citations,
    groundedCitations: grounded,
    ungroundedCitations: citations.length - grounded,
    groundednessRatio: citations.length ? grounded / citations.length : 0,
  };
}

export function computeQualityIndex({
  uniqueCitations = 0,
  sectionScore = 0,
  completenessScore = 0,
  uniquePaths = 0,
  groundedCitations,
} = {}) {
  // Grounded citations earn full credit; header-only / ungrounded citations earn
  // a fraction. When groundedCitations is omitted, treat all as grounded so the
  // metric is unchanged for callers that do not classify grounding.
  const UNGROUNDED_WEIGHT = 0.25;
  const total = Math.min(uniqueCitations, 20);
  const grounded = Math.max(0, Math.min(groundedCitations ?? uniqueCitations, total));
  const ungrounded = Math.max(0, total - grounded);
  const citationPart = ((grounded + ungrounded * UNGROUNDED_WEIGHT) / 20) * 40;
  const sectionPart = (Math.min(sectionScore, 8) / 8) * 25;
  const structurePart = (Math.min(completenessScore, 3) / 3) * 20;
  const depthPart = (Math.min(uniquePaths, 8) / 8) * 15;
  return Math.round(citationPart + sectionPart + structurePart + depthPart);
}

export function extractWireFileTokens(text) {
  return extractFileTokens(text);
}

export function scoreTraceOutput({ markdown, structuredJsonPath, raw, workspace = null, grounding = "content" } = {}) {
  const body = markdown || raw || "";
  const wire = isExploreWireFormat(body);
  const empty = !isNonEmpty(body);
  const citations = wire
    ? {
        all: extractFileTokens(body).map((t) => ({
          path: normalizePath(t.path),
          startLine: t.startLine,
          endLine: t.endLine,
          kind: "wireFile",
        })),
        uniqueCitations: extractFileTokens(body).length,
        uniquePaths: new Set(extractFileTokens(body).map((t) => normalizePath(t.path))).size,
        byKind: {
          lineStartFence: 0,
          inlineFence: 0,
          pathFirst: 0,
          structured: 0,
          refLabel: 0,
          wireFile: extractFileTokens(body).length,
        },
      }
    : extractTraceCitations(body, { structuredJsonPath });
  const sections = wire
    ? { headings: countMarkdownHeadings(body), semanticMarkers: {}, semanticHits: 0, sectionScore: sectionScoreWire(body) }
    : scoreTraceSections(body);
  const structure = scoreTraceStructure(body);
  const compliance = wire ? wireComplianceScore(body, "trace") : null;
  const groundingInfo = annotateGrounding(citations.all, { workspace, strategy: grounding });
  const qualityIndex = computeQualityIndex({
    uniqueCitations: citations.uniqueCitations,
    sectionScore: sections.sectionScore,
    completenessScore: structure.completenessScore,
    uniquePaths: citations.uniquePaths,
    groundedCitations: groundingInfo.groundedCitations,
  });

  return {
    empty,
    chars: countChars(body),
    uniqueCitations: citations.uniqueCitations,
    uniquePaths: citations.uniquePaths,
    sectionScore: sections.sectionScore,
    headingTotal: sections.headings.total,
    semanticHits: sections.semanticHits,
    completenessScore: structure.completenessScore,
    qualityIndex,
    groundedCitations: groundingInfo.groundedCitations,
    ungroundedCitations: groundingInfo.ungroundedCitations,
    groundednessRatio: groundingInfo.groundednessRatio,
    groundingStrategy: grounding,
    citeLineStart: citations.byKind.lineStartFence,
    citeInline: citations.byKind.inlineFence,
    citePathFirst: citations.byKind.pathFirst,
    citeStructured: citations.byKind.structured,
    citeRefLabels: citations.byKind.refLabel,
    citeWireFile: citations.byKind.wireFile || 0,
    wireFormat: wire,
    wireCompliance: compliance,
    structure,
    sections,
    citations,
  };
}

export function aggregateTraceScores(rows) {
  const okRows = rows.filter((r) => !r.empty);
  const n = okRows.length || 1;
  const pick = (key) => okRows.map((r) => r[key] ?? 0);

  return {
    count: rows.length,
    okCount: okRows.length,
    medianWallS: median(rows.map((r) => r.wallS ?? 0)),
    medianBytes: median(pick("chars")),
    medianUniqueCitations: median(pick("uniqueCitations")),
    medianUniquePaths: median(pick("uniquePaths")),
    medianSectionScore: median(pick("sectionScore")),
    medianCompleteness: median(pick("completenessScore")),
    medianQualityIndex: median(pick("qualityIndex")),
    medianGroundedCitations: median(pick("groundedCitations")),
    medianGroundednessRatio: median(pick("groundednessRatio")),
    medianCiteLineStart: median(pick("citeLineStart")),
    medianCiteInline: median(pick("citeInline")),
    medianCitePathFirst: median(pick("citePathFirst")),
    p95WallS: percentile(rows.map((r) => r.wallS ?? 0), 0.95),
  };
}

function parseArgs(argv) {
  const args = { file: null, structured: null, json: false, workspace: null, grounding: "content" };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--file" && argv[i + 1]) args.file = argv[++i];
    else if (arg.startsWith("--file=")) args.file = arg.slice(7);
    else if (arg === "--structured" && argv[i + 1]) args.structured = argv[++i];
    else if (arg.startsWith("--structured=")) args.structured = arg.slice(13);
    else if (arg === "--workspace" && argv[i + 1]) args.workspace = argv[++i];
    else if (arg.startsWith("--workspace=")) args.workspace = arg.slice(12);
    else if (arg === "--grounding" && argv[i + 1]) args.grounding = argv[++i];
    else if (arg.startsWith("--grounding=")) args.grounding = arg.slice(12);
    else if (arg === "--json") args.json = true;
    else if (arg === "--help" || arg === "-h") args.help = true;
  }
  return args;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help || !args.file) {
    process.stderr.write(
      "usage: bench-trace-scorer.mjs --file <out.md> [--structured structured.json] [--json]\n",
    );
    process.exit(args.help ? 0 : 2);
  }

  const markdown = fs.readFileSync(args.file, "utf8");
  const score = scoreTraceOutput({
    markdown,
    structuredJsonPath: args.structured,
    workspace: args.workspace,
    grounding: args.grounding,
  });
  if (args.json) {
    process.stdout.write(`${JSON.stringify(score)}\n`);
  } else {
    process.stdout.write(
      [
        `uniqueCitations=${score.uniqueCitations}`,
        `groundedCitations=${score.groundedCitations}`,
        `groundednessRatio=${score.groundednessRatio.toFixed(2)}`,
        `sectionScore=${score.sectionScore}`,
        `completeness=${score.completenessScore}`,
        `qualityIndex=${score.qualityIndex}`,
        `citeLineStart=${score.citeLineStart}`,
        `citeInline=${score.citeInline}`,
        `citePathFirst=${score.citePathFirst}`,
      ].join("\n") + "\n",
    );
  }
}

const isMain = process.argv[1] === fileURLToPath(import.meta.url);
if (isMain) main();
