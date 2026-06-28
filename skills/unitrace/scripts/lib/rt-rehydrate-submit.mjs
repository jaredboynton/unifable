// Host rehydration: pointer citation_spans -> code_passages from read index.
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { expandLineRange } from "../ast-context.mjs";
import { MAX_SPAN, safeRelPath } from "./trace-schema.mjs";
import { mergeProseWithPassages, pickCodePassages } from "./rt-pick-passages.mjs";

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

function clampSpan(workspace, rel, start, end) {
  const abs = join(workspace, rel);
  let s = Math.max(1, start);
  let e = Math.max(s, end);
  if (existsSync(abs)) {
    const exp = expandLineRange(abs, s, e);
    s = exp.startLine;
    e = exp.endLine;
  }
  const total = existsSync(abs) ? readFileSync(abs, "utf8").split("\n").length : 0;
  if (total > 0) e = Math.min(e, total);
  if (e - s + 1 > MAX_SPAN) e = s + MAX_SPAN - 1;
  if (e - s + 1 > 40) e = s + 39;
  return { start_line: s, end_line: e };
}

export function orderReadCacheEntries(readCache, seedPaths = []) {
  // Rank by seed insertion order (grep-hit definition seeds come first), not
  // alphabetically — otherwise an alphabetically-early but less-relevant seed can
  // crowd the actual definition file out of the capped READ INDEX.
  const rank = new Map();
  seedPaths.forEach((p, i) => { if (!rank.has(p)) rank.set(p, i); });
  const rankOf = (p) => (rank.has(p) ? rank.get(p) : Number.MAX_SAFE_INTEGER);
  return [...readCache.entries()].sort(([a], [b]) => {
    const ra = rankOf(a);
    const rb = rankOf(b);
    if (ra !== rb) return ra - rb;
    return a.localeCompare(b);
  });
}

export function buildReadIndex(orderedEntries, { maxFiles = 12, previewLines = 3 } = {}) {
  const lines = [
    "READ INDEX (cite excerpt_index in citation_spans; host rehydrates verbatim):",
    "",
  ];
  const slice = orderedEntries.slice(0, maxFiles);
  for (let i = 0; i < slice.length; i++) {
    const [path, excerpt] = slice[i];
    const span = spanFromExcerpt(excerpt);
    const range = span ? `lines ${span.start_line}-${span.end_line}` : "line range unknown";
    const preview = String(excerpt || "")
      .split("\n")
      .slice(0, previewLines)
      .map((l) => `  ${l}`)
      .join("\n");
    lines.push(`[${i}] ${path} (${range})`, preview, "");
  }
  if (orderedEntries.length > slice.length) {
    lines.push(`... (${orderedEntries.length - slice.length} more files read, omitted from index)`, "");
  }
  return lines.join("\n");
}

export function rehydratePointerSubmit({
  pointer,
  orderedPaths,
  workspace,
  filesRead,
  readCache,
  toolTurns,
  seedPaths = [],
  question = "",
}) {
  const passages = [];
  const seen = new Set();
  for (const cite of pointer.citation_spans || []) {
    if (!cite || typeof cite !== "object") continue;
    const idx = cite.excerpt_index;
    if (!Number.isInteger(idx) || idx < 0 || idx >= orderedPaths.length) continue;
    const rel = safeRelPath(workspace, orderedPaths[idx]);
    if (!rel || !filesRead.has(rel)) continue;
    const key = `${rel}:${cite.start_line}-${cite.end_line}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const clamped = clampSpan(workspace, rel, cite.start_line, cite.end_line);
    passages.push({
      file_path: rel,
      start_line: clamped.start_line,
      end_line: clamped.end_line,
      rationale: String(cite.rationale || `${rel.split("/").pop()} cited span`),
    });
    if (passages.length >= 5) break;
  }

  if (!passages.length) {
    const fallback = pickCodePassages({
      workspace,
      filesRead,
      readCache,
      seedPaths,
      question,
    });
    return mergeProseWithPassages(pointer, fallback, filesRead, toolTurns);
  }

  const out = { ...pointer };
  delete out.citation_spans;
  return mergeProseWithPassages(out, passages, filesRead, toolTurns);
}
