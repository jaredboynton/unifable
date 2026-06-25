// Host-assembled code_passages from read cache and seed priority.
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { expandLineRange } from "../ast-context.mjs";
import { MAX_SPAN, safeRelPath } from "./trace-schema.mjs";

const MAX_PASSAGES = 5;
const DEFAULT_SPAN = 35;

function lineCount(workspace, rel) {
  const p = join(workspace, rel);
  if (!existsSync(p)) return 0;
  return readFileSync(p, "utf8").split("\n").length;
}

function clampSpan(workspace, rel, start, end) {
  const abs = join(workspace, rel);
  let s = Math.max(1, start);
  let e = Math.max(s, end);
  if (existsSync(abs)) {
    const exp = expandLineRange(abs, s, e);
    s = exp.startLine;
    e = exp.endLine;
  }
  const total = lineCount(workspace, rel);
  if (total > 0) e = Math.min(e, total);
  if (e - s + 1 > MAX_SPAN) e = s + MAX_SPAN - 1;
  if (e - s + 1 > 40) e = s + 39;
  return { start_line: s, end_line: e };
}

function spanFromExcerpt(excerpt) {
  const lines = String(excerpt || "").split("\n").filter(Boolean);
  let min = Infinity;
  let max = 0;
  for (const line of lines) {
    const m = line.match(/^(\d+)\|/);
    if (!m) continue;
    const n = Number(m[1]);
    min = Math.min(min, n);
    max = Math.max(max, n);
  }
  if (!Number.isFinite(min) || max < min) return null;
  return { start_line: min, end_line: max };
}

export function pickCodePassages({
  workspace,
  filesRead,
  readCache,
  seedPaths = [],
  question = "",
  maxPassages = MAX_PASSAGES,
}) {
  const priority = new Set(seedPaths);
  const ordered = [...filesRead]
    .map((p) => safeRelPath(workspace, p))
    .filter(Boolean)
    .sort((a, b) => {
      const pa = priority.has(a) ? 0 : 1;
      const pb = priority.has(b) ? 0 : 1;
      if (pa !== pb) return pa - pb;
      return a.localeCompare(b);
    });

  const passages = [];
  for (const rel of ordered) {
    if (passages.length >= maxPassages) break;
    const excerpt = readCache.get(rel);
    let span = spanFromExcerpt(excerpt);
    if (!span) {
      const total = lineCount(workspace, rel);
      if (!total) continue;
      span = { start_line: 1, end_line: Math.min(DEFAULT_SPAN, total) };
    }
    const clamped = clampSpan(workspace, rel, span.start_line, span.end_line);
    const base = rel.split("/").pop() || rel;
    passages.push({
      file_path: rel,
      start_line: clamped.start_line,
      end_line: clamped.end_line,
      rationale: `${base} load-bearing span for: ${String(question).slice(0, 80)}`,
    });
  }
  return passages;
}

export function mergeProseWithPassages(prose, passages, filesRead, toolTurns) {
  const out = { ...prose };
  out.code_passages = passages;
  out.grounding_manifest = {
    files_read: [...filesRead].sort(),
    tool_turns: toolTurns,
  };
  if (!Array.isArray(out.comparison_tables)) out.comparison_tables = [];
  return out;
}
