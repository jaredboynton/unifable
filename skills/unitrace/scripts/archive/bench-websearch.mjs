#!/usr/bin/env node
// bench-websearch.mjs — quality + latency eval for websearch-gemini.sh (agy + Exa MCP).

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { aggregateWebsearchScores, scoreWebsearchOutput } from "./lib/bench-websearch-scorer.mjs";
import { rehydrateWebsearchWire } from "./lib/rehydrate-explore-wire.mjs";
import { isWireFormatEnabled } from "./lib/explore-output-prompt.mjs";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const UNITRACE_REPO = path.resolve(SCRIPT_DIR, "..");
const DEFAULT_TASKS = path.resolve(SCRIPT_DIR, "fixtures/bench-websearch/tasks.jsonl");

function parseArgs(argv) {
  let tasksFile = process.env.UNITRACE_BENCH_WEBSEARCH_TASKS || DEFAULT_TASKS;
  let outDir = null;
  let runs = Number(process.env.UNITRACE_BENCH_WEBSEARCH_RUNS || 1);
  let mock = process.env.UNITRACE_BENCH_WEBSEARCH_MOCK === "1";

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--tasks" && argv[i + 1]) tasksFile = argv[++i];
    else if (arg.startsWith("--tasks=")) tasksFile = arg.slice(8);
    else if (arg === "--out" && argv[i + 1]) outDir = argv[++i];
    else if (arg.startsWith("--out=")) outDir = arg.slice(6);
    else if (arg === "--runs" && argv[i + 1]) runs = Number(argv[++i]);
    else if (arg === "--mock") mock = true;
    else if (arg === "--help" || arg === "-h") return { help: true };
  }

  if (!outDir) {
    const stamp = new Date().toISOString().slice(0, 10);
    outDir = path.resolve(UNITRACE_REPO, "benchmarks", `${stamp}-websearch`);
  }

  return { tasksFile, outDir, runs, mock };
}

function loadTasks(tasksFile) {
  const raw = fs.readFileSync(tasksFile, "utf8").trim().split("\n").filter(Boolean);
  return raw.map((line) => JSON.parse(line));
}

function mockOutput(task) {
  if (process.env.UNITRACE_WIRE_FORMAT === "1") {
    return [
      "SECTION ExecutiveSummary",
      "Mock findings for bench wire path.",
      "SECTION InScopeFindings",
      "- Example <url:https://modelcontextprotocol.io>",
      "SECTION AdjacentOutOfScope",
      "- SWE-ReX patch agents",
      "SECTION PriorArt",
      "- <url:https://github.com/example/repo>",
      "SECTION GapsRisks",
      "- none in mock",
      "SECTION RecommendedNextSteps",
      "- Add optional LSP bridge",
    ].join("\n");
  }
  return [
    "### 1. Executive Summary",
    "Mock findings for bench unit path.",
    "### 2. In-scope findings",
    "- Example https://modelcontextprotocol.io",
    "### 3. Adjacent / out-of-scope",
    "- SWE-ReX patch agents",
    "### 4. Prior art / GitHub repos",
    "- https://github.com/example/repo",
    "### 5. Gaps, risks, or conflicting claims",
    "- none in mock",
    "### 6. Recommended next steps",
    "- Add optional LSP bridge",
  ].join("\n");
}

function taskEnv(task) {
  const env = { ...process.env };
  const tags = Array.isArray(task.tags) ? task.tags : [];
  if (tags.includes("explore")) {
    env.UNISEARCH_WEBSEARCH_SKILL_CONTEXT = "1";
  }
  return env;
}

function runTask(task, runIndex, { mock }) {
  let websearchMs = 0;
  let output;
  if (mock) {
    output = mockOutput(task);
  } else {
    const started = Date.now();
    const res = spawnSync(path.join(SCRIPT_DIR, "websearch-gemini.sh"), [task.query], {
      cwd: UNITRACE_REPO,
      env: taskEnv(task),
      encoding: "utf8",
      maxBuffer: 16 * 1024 * 1024,
    });
    websearchMs = Date.now() - started;
    output = res.stdout || "";
    if (res.status !== 0 && !output.trim()) {
      throw new Error(`websearch failed for ${task.id}: ${(res.stderr || "").slice(0, 300)}`);
    }
  }

  const rawOutput = output;
  const score = scoreWebsearchOutput(rawOutput, task.expect);
  if (isWireFormatEnabled()) {
    output = rehydrateWebsearchWire(rawOutput);
  }
  return {
    id: task.id,
    run: runIndex,
    query: task.query,
    ...score,
    websearchMs,
    outputChars: output.length,
  };
}

function writeSummary(outDir, rows) {
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(path.join(outDir, "results.jsonl"), `${rows.map((r) => JSON.stringify(r)).join("\n")}\n`);

  const summary = aggregateWebsearchScores(rows);
  fs.writeFileSync(path.join(outDir, "summary.json"), `${JSON.stringify({ summary }, null, 2)}\n`);

  const tsv = [
    "metric\tvalue",
    `passRate\t${summary.passRate.toFixed(3)}`,
    `emptyRate\t${summary.emptyRate.toFixed(3)}`,
    `scopePassRate\t${summary.scopePassRate.toFixed(3)}`,
    `avgUrlCount\t${summary.avgUrlCount.toFixed(2)}`,
    `avgSections\t${summary.avgSections.toFixed(2)}`,
    `avgSectionHeadings\t${summary.avgSectionHeadings.toFixed(2)}`,
    `medianWebsearchMs\t${summary.medianWebsearchMs.toFixed(0)}`,
    `p95WebsearchMs\t${summary.p95WebsearchMs.toFixed(0)}`,
  ].join("\n");
  fs.writeFileSync(path.join(outDir, "summary.tsv"), `${tsv}\n`);

  const readme = [
    "# Websearch benchmark",
    "",
    `Generated: ${new Date().toISOString()}`,
    "",
    "## Summary",
    "",
    "| metric | value |",
    "|---|---:|",
    `| pass rate | ${(summary.passRate * 100).toFixed(1)}% |`,
    `| empty rate | ${(summary.emptyRate * 100).toFixed(1)}% |`,
    `| scope pass (no forbidden next steps) | ${(summary.scopePassRate * 100).toFixed(1)}% |`,
    `| avg URL count | ${summary.avgUrlCount.toFixed(1)} |`,
    `| avg section markers | ${summary.avgSections.toFixed(1)} / 6 |`,
    `| avg markdown headings (diagnostic) | ${summary.avgSectionHeadings.toFixed(1)} |`,
    `| median latency | ${summary.medianWebsearchMs.toFixed(0)} ms |`,
    `| p95 latency | ${summary.p95WebsearchMs.toFixed(0)} ms |`,
    "",
    "## Pass criteria (per task)",
    "",
    "- Non-empty output",
    `- >= minUrls citations`,
    "- Required urlPatterns (if any)",
    `- >= minSections of 6 prompt sections`,
    "- No forbiddenNextStepPatterns in Recommended next steps",
    "",
    "## Tasks",
    "",
    "`scripts/fixtures/bench-websearch/tasks.jsonl`",
    "",
  ].join("\n");
  fs.writeFileSync(path.join(outDir, "README.md"), `${readme}\n`);

  return summary;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    process.stdout.write(
      "usage: bench-websearch.mjs [--tasks FILE] [--out DIR] [--runs N] [--mock]\n",
    );
    process.exit(0);
  }

  const tasks = loadTasks(args.tasksFile);
  if (!tasks.length) {
    process.stderr.write("error: no tasks\n");
    process.exit(1);
  }

  const rows = [];
  for (const task of tasks) {
    for (let run = 1; run <= args.runs; run += 1) {
      process.stderr.write(`bench-websearch ${task.id} run=${run}\n`);
      rows.push(runTask(task, run, { mock: args.mock }));
    }
  }

  const summary = writeSummary(args.outDir, rows);
  process.stdout.write(`bench complete: ${args.outDir}\n`);
  process.stdout.write(
    `pass=${(summary.passRate * 100).toFixed(1)}% median=${summary.medianWebsearchMs.toFixed(0)}ms scope=${(summary.scopePassRate * 100).toFixed(1)}%\n`,
  );
}

main().catch((err) => {
  process.stderr.write(`error: ${err.message}\n`);
  process.exit(1);
});
