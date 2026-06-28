// trace-citations.mjs — extract code citations from a trace's markdown/wire output.
//
// Recognizes line-start fences (```start:end:path), inline fences, path-first refs
// (path:start:end), <refN> labels, and structured-JSON code_passages sidecars.
// Used by render-trace-structured.mjs to count and dedupe cited spans.

import fs from "node:fs";

const LINE_START_FENCE_RE = /^```(\d+):(\d+):([^\n`]+)/gm;
const INLINE_FENCE_RE = /```(\d+):(\d+):([^`\s]+)```/g;
const PATH_FIRST_RE = /([\w./-]+\.(?:sh|mjs|js|py|md|ts|tsx|json)):(\d+):(\d+)/g;
const REF_LABEL_RE = /^<ref(\d+)>/gm;

function normalizePath(p) {
  return (p || "").replace(/\\/g, "/").replace(/^\.\/+/, "");
}

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
