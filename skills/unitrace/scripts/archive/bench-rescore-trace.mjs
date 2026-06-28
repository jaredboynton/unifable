#!/usr/bin/env node
// bench-rescore-trace.mjs — rescore existing trace bench artifacts with bench-trace-scorer.
//
// Usage:
//   node scripts/bench-rescore-trace.mjs --dir benchmarks/2026-06-24-trace-gm

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { aggregateTraceScores, scoreTraceOutput } from "./lib/bench-trace-scorer.mjs";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(SCRIPT_DIR, "..");

function parseArgs(argv) {
  const args = { dir: null, writeReadme: true };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--dir" && argv[i + 1]) args.dir = argv[++i];
    else if (arg.startsWith("--dir=")) args.dir = arg.slice(6);
    else if (arg === "--no-readme") args.writeReadme = false;
    else if (arg === "--help" || arg === "-h") args.help = true;
  }
  return args;
}

function discoverRuns(benchDir) {
  const runs = [];
  if (!fs.existsSync(benchDir)) return runs;

  for (const entry of fs.readdirSync(benchDir, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const label = entry.name;
    const transport = label.startsWith("cursor")
      ? "cursor"
      : label.startsWith("gemini")
        ? "gemini"
        : null;
    if (!transport) continue;

    const outMd = path.join(benchDir, label, "runs", label, "out.md");
    if (!fs.existsSync(outMd)) continue;

    const structured = path.join(benchDir, label, "runs", label, "structured.json");
    runs.push({
      label,
      transport,
      outMd,
      structured: fs.existsSync(structured) ? structured : null,
    });
  }
  return runs;
}

function loadPriorResults(benchDir) {
  const tsv = path.join(benchDir, "results.tsv");
  if (!fs.existsSync(tsv)) return new Map();

  const lines = fs.readFileSync(tsv, "utf8").trim().split("\n");
  const header = lines.shift()?.split("\t") ?? [];
  const wallIdx = header.indexOf("wall_s");
  const okIdx = header.indexOf("ok");
  const byTransport = new Map();
  let cursorN = 0;
  let geminiN = 0;

  for (const line of lines) {
    const cols = line.split("\t");
    const transport = cols[0];
    const n = transport === "cursor" ? ++cursorN : ++geminiN;
    const label = `${transport}-${n}`;
    byTransport.set(label, {
      wallS: wallIdx >= 0 ? Number(cols[wallIdx]) : 0,
      ok: okIdx >= 0 ? Number(cols[okIdx]) : 1,
    });
  }
  return byTransport;
}

function median(nums) {
  if (!nums.length) return 0;
  const sorted = [...nums].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

function buildFindings({ cursor, gemini, pairedDelta }) {
  const lines = [
    "## Findings",
    "",
    `- **Latency:** Gemini CLI (\`gemini-3.1-flash-lite\`, plan mode) is ~${(cursor.medianWallS / gemini.medianWallS).toFixed(1)}x faster at the median (${gemini.medianWallS.toFixed(2)}s vs ${cursor.medianWallS.toFixed(2)}s), with a paired delta of **${pairedDelta >= 0 ? "+" : ""}${pairedDelta.toFixed(2)}s**.`,
    `- **Reliability:** ${cursor.okCount}/${cursor.count} cursor and ${gemini.okCount}/${gemini.count} gemini successful runs.`,
    `- **Quality (rescored):** Cursor median unique citations **${Math.round(cursor.medianUniqueCitations)}** vs Gemini **${Math.round(gemini.medianUniqueCitations)}** (was 0 with old \`^##\` / line-start fence grep). Gemini section score median **${Math.round(gemini.medianSectionScore)}** (semantic markers + headings; old \`##\` count was 0).`,
    `- **Citation formats:** Cursor uses line-start fences (median ${Math.round(cursor.medianCiteLineStart)}). Gemini uses inline fences (median ${Math.round(gemini.medianCiteInline)}) and path-first refs (median ${Math.round(gemini.medianCitePathFirst)}).`,
    `- **Depth:** Cursor output is ~${Math.round(cursor.medianBytes / Math.max(gemini.medianBytes, 1))}x larger (median ${Math.round(cursor.medianBytes)} vs ${Math.round(gemini.medianBytes)} bytes). Quality index median: cursor **${Math.round(cursor.medianQualityIndex)}** vs gemini **${Math.round(gemini.medianQualityIndex)}**.`,
    `- **Verdict:** \`trace.sh\` (Gemini CLI) remains a viable **fast, Cursor-free** trace path. Rescored metrics show Gemini does cite code (inline/path-first), but cursor traces are deeper on citations, structure, and bytes.`,
    "",
  ];
  return lines.join("\n");
}

function replaceSection(text, heading, body) {
  const re = new RegExp(`## ${heading}[\\s\\S]*?(?=\\n## |$)`);
  const block = `## ${heading}\n\n${body.trim()}\n`;
  if (re.test(text)) return text.replace(re, block);
  return `${text.trim()}\n\n${block}`;
}

function updateReadme(benchDir, summary, findings) {
  const readmePath = path.join(benchDir, "README.md");
  let text = fs.existsSync(readmePath) ? fs.readFileSync(readmePath, "utf8") : "";

  const resultsTable = [
    "| transport | n (ok) | Median wall | Median bytes | Med cites | Med sections | Med quality | Med cite inline | Med cite path-first |",
    "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
  ];

  for (const [transport, agg] of Object.entries(summary.byTransport)) {
    resultsTable.push(
      `| ${transport} | ${agg.okCount}/${agg.count} | ${agg.medianWallS.toFixed(2)}s | ${Math.round(agg.medianBytes)} | ${Math.round(agg.medianUniqueCitations)} | ${Math.round(agg.medianSectionScore)} | ${Math.round(agg.medianQualityIndex)} | ${Math.round(agg.medianCiteInline)} | ${Math.round(agg.medianCitePathFirst)} |`,
    );
  }
  if (summary.pairedDeltaWallS != null) {
    resultsTable.push("");
    resultsTable.push(
      `Paired median delta (gemini - cursor): **${summary.pairedDeltaWallS >= 0 ? "+" : ""}${summary.pairedDeltaWallS.toFixed(2)}s**`,
    );
  }

  const scorerNote =
    "- quality scorer: `scripts/lib/bench-trace-scorer.mjs` (multi-format citations + semantic sections)";
  if (!text.includes("bench-trace-scorer")) {
    text = text.replace(/(- gemini: trace-gm\.sh[^\n]*\n)/, `$1${scorerNote}\n`);
  }

  text = replaceSection(text, "Results", resultsTable.join("\n"));
  text = replaceSection(text, "Findings", findings.replace(/^## Findings\n\n?/, ""));

  if (!text.includes("summary-rescored.json")) {
    text = text.replace(
      /(## Artifacts\n\n)/,
      `$1- \`${path.join(benchDir, "results-rescored.tsv")}\`\n- \`${path.join(benchDir, "summary-rescored.json")}\`\n`,
    );
  }

  fs.writeFileSync(readmePath, text.endsWith("\n") ? text : `${text}\n`);
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help || !args.dir) {
    process.stderr.write("usage: bench-rescore-trace.mjs --dir <benchmark-dir> [--no-readme]\n");
    process.exit(args.help ? 0 : 2);
  }

  const benchDir = path.resolve(REPO, args.dir);
  const prior = loadPriorResults(benchDir);
  const discovered = discoverRuns(benchDir);

  if (!discovered.length) {
    process.stderr.write(`error: no run out.md files under ${benchDir}\n`);
    process.exit(1);
  }

  const scored = discovered.map((run) => {
    const markdown = fs.readFileSync(run.outMd, "utf8");
    const score = scoreTraceOutput({
      markdown,
      structuredJsonPath: run.structured,
    });
    const meta = prior.get(run.label) ?? {};
    return {
      label: run.label,
      transport: run.transport,
      outMd: run.outMd,
      wallS: meta.wallS ?? 0,
      ok: meta.ok ?? 1,
      ...score,
    };
  });

  const byTransport = {};
  for (const row of scored) {
    byTransport[row.transport] ??= [];
    byTransport[row.transport].push(row);
  }

  const summary = {
    benchDir,
    rescoredAt: new Date().toISOString(),
    runs: scored.map(({ citations, sections, structure, ...rest }) => rest),
    byTransport: Object.fromEntries(
      Object.entries(byTransport).map(([t, rows]) => [t, aggregateTraceScores(rows)]),
    ),
  };

  const cursorWalls = (byTransport.cursor ?? []).filter((r) => r.ok).map((r) => r.wallS);
  const geminiWalls = (byTransport.gemini ?? []).filter((r) => r.ok).map((r) => r.wallS);
  if (cursorWalls.length && geminiWalls.length && cursorWalls.length === geminiWalls.length) {
    const pairs = geminiWalls.map((g, i) => g - cursorWalls[i]);
    summary.pairedDeltaWallS = median(pairs);
  }

  const outPath = path.join(benchDir, "summary-rescored.json");
  fs.writeFileSync(outPath, `${JSON.stringify(summary, null, 2)}\n`);

  const rescoredTsv = [
    [
      "label",
      "transport",
      "wall_s",
      "ok",
      "unique_citations",
      "section_score",
      "completeness",
      "quality_index",
      "cite_lineStart",
      "cite_inline",
      "cite_pathFirst",
    ].join("\t"),
    ...scored.map((r) =>
      [
        r.label,
        r.transport,
        r.wallS,
        r.ok,
        r.uniqueCitations,
        r.sectionScore,
        r.completenessScore,
        r.qualityIndex,
        r.citeLineStart,
        r.citeInline,
        r.citePathFirst,
      ].join("\t"),
    ),
  ].join("\n");
  fs.writeFileSync(path.join(benchDir, "results-rescored.tsv"), `${rescoredTsv}\n`);

  if (args.writeReadme && summary.byTransport.cursor && summary.byTransport.gemini) {
    const cursorAgg = summary.byTransport.cursor;
    const geminiAgg = summary.byTransport.gemini;
    const findings = buildFindings({
      cursor: { ...cursorAgg, medianWallS: cursorAgg.medianWallS || median(cursorWalls) },
      gemini: { ...geminiAgg, medianWallS: geminiAgg.medianWallS || median(geminiWalls) },
      pairedDelta: summary.pairedDeltaWallS ?? 0,
    });
    updateReadme(benchDir, summary, findings);
  }

  process.stdout.write(`rescored ${scored.length} runs -> ${outPath}\n`);
  for (const [t, agg] of Object.entries(summary.byTransport)) {
    process.stdout.write(
      `  ${t}: medCites=${Math.round(agg.medianUniqueCitations)} medSec=${Math.round(agg.medianSectionScore)} medQI=${Math.round(agg.medianQualityIndex)}\n`,
    );
  }
}

main();
