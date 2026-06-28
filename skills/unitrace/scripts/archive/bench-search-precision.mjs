#!/usr/bin/env node
// bench-search-precision.mjs — RepoQA-style local needle eval for search + map modes.

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { aggregateScores, parseSearchJsonOutput, recommendMode, scoreSearchResult } from "./lib/bench-scorer.mjs";
import { loadRepoQATasks, repoqaAdapterStatus } from "./lib/repoqa-tasks.mjs";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE_REPO = path.resolve(SCRIPT_DIR, "fixtures/search-mini-repo");
const MAP_AST_REPO = path.resolve(SCRIPT_DIR, "fixtures/map-ast-repo");
const UNITRACE_REPO = path.resolve(SCRIPT_DIR, "..");
const DEFAULT_TASKS = path.resolve(SCRIPT_DIR, "fixtures/bench-needles/tasks.jsonl");
const MAP_MODES = ["none", "pagerank", "sigmap", "tandem"];

function parseArgs(argv) {
  let tasksFile = process.env.UNITRACE_BENCH_TASKS || DEFAULT_TASKS;
  let outDir = null;
  let modes = MAP_MODES;
  let runs = Number(process.env.UNITRACE_BENCH_RUNS || 1);
  let offlineOnly = false;
  let mockSearch = false;

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--tasks" && argv[i + 1]) tasksFile = argv[++i];
    else if (arg.startsWith("--tasks=")) tasksFile = arg.slice(8);
    else if (arg === "--out" && argv[i + 1]) outDir = argv[++i];
    else if (arg.startsWith("--out=")) outDir = arg.slice(6);
    else if (arg === "--modes" && argv[i + 1]) modes = argv[++i].split(",").map((s) => s.trim());
    else if (arg === "--runs" && argv[i + 1]) runs = Number(argv[++i]);
    else if (arg === "--offline-only") offlineOnly = true;
    else if (arg === "--mock-search") mockSearch = true;
    else if (arg === "--help" || arg === "-h") return { help: true };
  }

  if (!outDir) {
    const stamp = new Date().toISOString().slice(0, 10);
    outDir = path.resolve(UNITRACE_REPO, "benchmarks", `${stamp}-map-precision`);
  }

  return { tasksFile, outDir, modes, runs, offlineOnly, mockSearch };
}

function resolveRepoToken(token) {
  if (token === "__FIXTURE__") return FIXTURE_REPO;
  if (token === "__MAP_AST__") return MAP_AST_REPO;
  if (token === "__UNITRACE__") return UNITRACE_REPO;
  if (token === "__UNIFABLE__") {
    return process.env.UNITRACE_BENCH_UNIFABLE || path.join(process.env.HOME || "", "__devlocal/unifable");
  }
  return token;
}

function loadTasks(tasksFile, offlineOnly) {
  const raw = fs.readFileSync(tasksFile, "utf8").trim().split("\n").filter(Boolean);
  const tasks = raw.map((line, idx) => {
    const task = JSON.parse(line);
    task.repo = resolveRepoToken(task.repo);
    return task;
  });
  const repoqa = loadRepoQATasks();
  const merged = [...tasks, ...repoqa];
  return offlineOnly
    ? merged.filter((t) => (t.tags || []).includes("offline"))
    : merged.filter((t) => fs.existsSync(t.repo));
}

function mockSearchResult(task) {
  return [{
    path: task.expect.path,
    startLine: task.expect.startLine,
    endLine: task.expect.endLine,
    content: "mock",
  }];
}

async function runTask(task, mode, runIndex, { mockSearch }) {
  let searchMs = 0;
  let refs;
  if (mockSearch) {
    refs = mockSearchResult(task);
  } else {
    const env = { ...process.env, UNITRACE_MAP_MODE: mode };
    if (process.env.UNITRACE_MAP_NO_CACHE === "1") env.UNITRACE_MAP_NO_CACHE = "1";
    const started = Date.now();
    const search = spawnSync(path.join(SCRIPT_DIR, "search.sh"), ["--root", task.repo, "--json", task.query], {
      cwd: task.repo,
      env,
      encoding: "utf8",
      maxBuffer: 8 * 1024 * 1024,
    });
    searchMs = Date.now() - started;
    if (search.status !== 0 && !search.stdout.trim()) {
      throw new Error(`search failed for ${task.id}/${mode}: ${search.stderr.slice(0, 200)}`);
    }
    refs = parseSearchJsonOutput(search.stdout);
  }

  const score = scoreSearchResult(refs, task.expect);
  return {
    id: task.id,
    mode,
    run: runIndex,
    repo: task.repo,
    ...score,
    mapMs: 0,
    mapBytes: 0,
    searchMs,
  };
}

function writeSummary(outDir, rows, modes) {
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(path.join(outDir, "results.jsonl"), `${rows.map((r) => JSON.stringify(r)).join("\n")}\n`);

  const summaryByMode = {};
  for (const mode of modes) {
    summaryByMode[mode] = aggregateScores(rows.filter((r) => r.mode === mode));
  }

  const recommendation = recommendMode(summaryByMode);
  const summary = { summaryByMode, recommendation, repoqa: repoqaAdapterStatus() };
  fs.writeFileSync(path.join(outDir, "summary.json"), `${JSON.stringify(summary, null, 2)}\n`);

  const tsvLines = ["mode\thit1\thit5\tavgLineIou\temptyRate\tmedianMapMs\tmedianSearchMs\tmedianTotalMs"];
  for (const mode of modes) {
    const s = summaryByMode[mode];
    tsvLines.push([
      mode,
      s.hit1.toFixed(3),
      s.hit5.toFixed(3),
      s.avgLineIou.toFixed(3),
      s.emptyRate.toFixed(3),
      s.medianMapMs.toFixed(0),
      s.medianSearchMs.toFixed(0),
      s.medianTotalMs.toFixed(0),
    ].join("\t"));
  }
  fs.writeFileSync(path.join(outDir, "summary.tsv"), `${tsvLines.join("\n")}\n`);

  const readme = [
    "# Map precision benchmark",
    "",
    `Generated: ${new Date().toISOString()}`,
    "",
    "## Summary",
    "",
    "| mode | hit@1 | hit@5 | line IoU | empty | map ms | search ms | total ms |",
    "|---|---:|---:|---:|---:|---:|---:|---:|",
    ...modes.map((mode) => {
      const s = summaryByMode[mode];
      return `| ${mode} | ${(s.hit1 * 100).toFixed(1)}% | ${(s.hit5 * 100).toFixed(1)}% | ${s.avgLineIou.toFixed(2)} | ${(s.emptyRate * 100).toFixed(1)}% | ${s.medianMapMs.toFixed(0)} | ${s.medianSearchMs.toFixed(0)} | ${s.medianTotalMs.toFixed(0)} |`;
    }),
    "",
    "## Recommendation",
    "",
    `- **Pick:** \`${recommendation.pick}\``,
    `- **Reason:** ${recommendation.reason}`,
    "",
    "## Scope",
    "",
    "Offline curated tasks from `scripts/fixtures/bench-needles/tasks.jsonl`. Use full live bench (includes unifable integration tasks) with `UNITRACE_BENCH_LIVE=1` and no `--offline-only`.",
    "",
    "Wall-clock includes map prefetch (when mode != none) plus Cerebras search.",
    "",
    "## Decision criteria",
    "",
    "| Pick | Condition |",
    "|---|---|",
    "| pagerank | Best hit@1 on integration tasks; map p95 under budget |",
    "| sigmap | Best hit@1 with lowest map bytes/latency |",
    "| tandem | hit@1 +3pp over best single and total latency under 2x |",
    "| none | No mode beats baseline by >= 2pp hit@1 |",
    "",
  ].join("\n");
  fs.writeFileSync(path.join(outDir, "README.md"), `${readme}\n`);

  return { summaryByMode, recommendation };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    process.stdout.write(
      "usage: bench-search-precision.mjs [--tasks FILE] [--out DIR] [--modes a,b,c] [--runs N] [--offline-only] [--mock-search]\n",
    );
    process.exit(0);
  }

  const tasks = loadTasks(args.tasksFile, args.offlineOnly);
  if (!tasks.length) {
    process.stderr.write("error: no tasks to run\n");
    process.exit(1);
  }

  const rows = [];
  for (const task of tasks) {
    for (const mode of args.modes) {
      for (let run = 1; run <= args.runs; run += 1) {
        process.stderr.write(`bench ${task.id} mode=${mode} run=${run}\n`);
        const row = await runTask(task, mode, run, { mockSearch: args.mockSearch });
        rows.push(row);
      }
    }
  }

  const { recommendation } = writeSummary(args.outDir, rows, args.modes);
  process.stdout.write(`bench complete: ${args.outDir}\n`);
  process.stdout.write(`recommendation: ${recommendation.pick} (${recommendation.reason})\n`);
}

main().catch((err) => {
  process.stderr.write(`error: ${err.message}\n`);
  process.exit(1);
});
