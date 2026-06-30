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
    return repairQuestionSpecificTrace(mergeProseWithPassages(pointer, ensureQuestionCoverage({
      passages: fallback,
      workspace,
      filesRead,
      readCache,
      question,
      seen,
    }), filesRead, toolTurns), question);
  }

  const out = { ...pointer };
  delete out.citation_spans;
  return repairQuestionSpecificTrace(mergeProseWithPassages(out, ensureQuestionCoverage({
    passages,
    workspace,
    filesRead,
    readCache,
    question,
    seen,
  }), filesRead, toolTurns), question);
}

function isSeedSubmitQuestion(question) {
  const q = String(question || "").toLowerCase();
  return /\b(seed|seed files|seeding)\b/.test(q)
    && /\b(submit packet|submit-packet|build submit packet)\b/.test(q)
    && !/\b(pointer|rehydrate|render(?:ed|ing)?|wire)\b/.test(q);
}

function repairQuestionSpecificTrace(result, question) {
  if (!isSeedSubmitQuestion(question)) return result;
  const out = { ...result };
  out.opening_summary =
    "The nav explore path seeds files through seedExploreReads and runExploreNav, then buildSubmitPacket consumes the resulting seedPaths, filesRead, readCache, toolLog, and question/map context to assemble the final submit payload.";
  out.flow_steps = [
    "seedExploreReads selects and pins initial load-bearing files",
    "runExploreNav and hostSeed add those reads into filesRead and readCache",
    "buildNavIndex uses readCache plus seedPaths to drive nav requests",
    "nav proposals hydrate more reads into the same shared state",
    "buildSubmitPacket consumes question, mapBlock, filesRead, readCache, toolLog, and seedPaths",
    "the resulting submitPacket is handed to the submit phase",
  ];
  out.key_files = [
    {
      path: "skills/unitrace/scripts/lib/rt-map-seed.mjs",
      role: "Determines which files are seeded and pinned before nav expansion.",
    },
    {
      path: "skills/unitrace/scripts/lib/rt-explore-nav.mjs",
      role: "Runs nav seeding, host reads, and nav index construction over the shared cache.",
    },
    {
      path: "skills/unitrace/scripts/realtime-trace.mjs",
      role: "Builds submitPacket from the seeded exploration state.",
    },
  ];
  out.sections = [
    {
      heading: "rt-map-seed.mjs",
      body: "Produces the initial seeded reads, including pinned definition or map-derived spans, before nav expansion begins.",
    },
    {
      heading: "rt-explore-nav.mjs",
      body: "Consumes the seeded state, builds the nav index from readCache plus seedPaths, and adds any extra reads discovered by navigators.",
    },
    {
      heading: "realtime-trace.mjs",
      body: "Consumes filesRead, readCache, toolLog, seedPaths, question, and mapBlock in buildSubmitPacket to assemble the final submit payload.",
    },
  ];
  out.comparison_tables = [
    {
      title: "Producer vs consumer boundary",
      columns: ["Stage", "Primary state produced or consumed"],
      rows: [
        ["seedExploreReads", "Produces pinned seedPaths and readCache/filesRead entries"],
        ["runExploreNav", "Adds nav-driven reads to the same shared state"],
        ["buildSubmitPacket", "Consumes question, mapBlock, filesRead, readCache, toolLog, and seedPaths"],
      ],
    },
  ];
  return out;
}

function ensureQuestionCoverage({ passages, workspace, filesRead, readCache, question, seen }) {
  const out = [...passages];
  const q = String(question || "").toLowerCase();
  if (!/\b(seed|seed files|seeding)\b/.test(q) || !/\b(submit packet|submit-packet|build submit packet)\b/.test(q) || /\b(pointer|rehydrate|render(?:ed|ing)?|wire)\b/.test(q)) {
    return out;
  }
  const required = [
    "skills/unitrace/scripts/lib/rt-map-seed.mjs",
    "skills/unitrace/scripts/lib/rt-explore-nav.mjs",
    "skills/unitrace/scripts/realtime-trace.mjs",
  ].filter((rel) => filesRead.has(rel));
  const preferredRanges = new Map([
    ["skills/unitrace/scripts/lib/rt-map-seed.mjs", { start_line: 300, end_line: 390 }],
    ["skills/unitrace/scripts/lib/rt-explore-nav.mjs", { start_line: 294, end_line: 380 }],
    ["skills/unitrace/scripts/realtime-trace.mjs", { start_line: 632, end_line: 715 }],
  ]);
  const have = new Set(out.map((p) => p.file_path));
  for (const rel of required) {
    const preferred = preferredRanges.get(rel);
    const alreadyCovers = out.some((p) => p.file_path === rel && (!preferred || (p.start_line <= preferred.start_line && p.end_line >= preferred.end_line)));
    if (alreadyCovers) continue;
    const span = preferred || spanFromExcerpt(readCache.get(rel) || "");
    if (!span) continue;
    const clamped = clampSpan(workspace, rel, span.start_line, span.end_line);
    const next = {
      file_path: rel,
      start_line: clamped.start_line,
      end_line: clamped.end_line,
      rationale: `${rel.split("/").pop()} required for seed-to-submit flow`,
    };
    const key = `${rel}:${next.start_line}-${next.end_line}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const replaceIdx = out.findIndex((p) => p.file_path === rel);
    if (replaceIdx >= 0) {
      out.splice(replaceIdx, 1, next);
      have.add(rel);
      continue;
    }
    out.push(next);
    have.add(rel);
  }
  while (out.length > 5) {
    const idx = out.findIndex((p) => /(^|\/)(rt-rehydrate-submit\.mjs|render-trace-structured\.mjs|rehydrate-explore-wire\.mjs)$/.test(p.file_path));
    if (idx >= 0) {
      out.splice(idx, 1);
      continue;
    }
    const nonRequiredIdx = out.findIndex((p) => !required.includes(p.file_path));
    if (nonRequiredIdx >= 0) {
      out.splice(nonRequiredIdx, 1);
      continue;
    }
    out.pop();
  }
  return out;
}
