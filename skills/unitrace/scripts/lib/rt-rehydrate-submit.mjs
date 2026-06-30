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

function splitExcerptSegments(excerpt) {
  return String(excerpt || "")
    .split("\n---\n")
    .map((seg) => seg.split("\n").filter(Boolean))
    .filter((seg) => seg.length);
}

export function buildReadIndexEntries(orderedEntries, { maxFiles = 12 } = {}) {
  const entries = [];
  const slice = orderedEntries.slice(0, maxFiles);
  for (const [path, excerpt] of slice) {
    const segments = splitExcerptSegments(excerpt);
    let pushed = false;
    for (const segLines of segments) {
      const segText = segLines.join("\n");
      const span = spanFromExcerpt(segText);
      entries.push({
        path,
        excerpt: segText,
        start_line: span?.start_line ?? null,
        end_line: span?.end_line ?? null,
      });
      pushed = true;
    }
    if (!pushed) {
      entries.push({ path, excerpt: String(excerpt || ""), start_line: null, end_line: null });
    }
  }
  return entries;
}

export function buildReadIndex(orderedEntries, { maxFiles = 12, previewLines = 3 } = {}) {
  const previewExcerpt = (excerpt) => (
    String(excerpt || "")
      .split("\n")
      .filter(Boolean)
      .slice(0, previewLines)
      .map((l) => `  ${l}`)
      .join("\n")
  );

  const lines = [
    "READ INDEX (cite excerpt_index in citation_spans; host rehydrates verbatim):",
    "",
  ];
  const entries = buildReadIndexEntries(orderedEntries, { maxFiles });
  for (let i = 0; i < entries.length; i++) {
    const { path, excerpt, start_line, end_line } = entries[i];
    const range = start_line && end_line ? `lines ${start_line}-${end_line}` : "line range unknown";
    const preview = previewExcerpt(excerpt);
    lines.push(`[${i}] ${path} (${range})`, preview, "");
  }
  if (orderedEntries.length > maxFiles) {
    lines.push(`... (${orderedEntries.length - maxFiles} more files read, omitted from index)`, "");
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
    const entry = orderedPaths[idx];
    const rel = safeRelPath(workspace, typeof entry === "string" ? entry : entry?.path);
    if (!rel || !filesRead.has(rel)) continue;
    const entryStart = typeof entry === "object" ? entry?.start_line : null;
    const entryEnd = typeof entry === "object" ? entry?.end_line : null;
    let boundedStart = Number.isInteger(entryStart) ? Math.max(cite.start_line, entryStart) : cite.start_line;
    let boundedEnd = Number.isInteger(entryEnd) ? Math.min(cite.end_line, entryEnd) : cite.end_line;
    const citedSpan = Math.max(1, boundedEnd - boundedStart + 1);
    const entrySpan = Number.isInteger(entryStart) && Number.isInteger(entryEnd)
      ? Math.max(1, entryEnd - entryStart + 1)
      : null;
    if (entrySpan && entrySpan <= 30) {
      boundedStart = entryStart;
      boundedEnd = entryEnd;
    } else if (entrySpan && citedSpan < 4 && entrySpan <= 40) {
      boundedStart = entryStart;
      boundedEnd = entryEnd;
    }
    const key = `${rel}:${boundedStart}-${boundedEnd}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const clamped = clampSpan(workspace, rel, boundedStart, Math.max(boundedStart, boundedEnd));
    let finalStart = clamped.start_line;
    let finalEnd = clamped.end_line;
    if (Number.isInteger(entryStart)) finalStart = Math.max(finalStart, entryStart);
    if (Number.isInteger(entryEnd)) finalEnd = Math.min(finalEnd, entryEnd);
    if (finalEnd < finalStart) {
      finalStart = boundedStart;
      finalEnd = Math.max(boundedStart, boundedEnd);
    }
    passages.push({
      file_path: rel,
      start_line: finalStart,
      end_line: finalEnd,
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
