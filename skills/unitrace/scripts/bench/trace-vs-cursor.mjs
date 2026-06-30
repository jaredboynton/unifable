#!/usr/bin/env node
// trace-vs-cursor.mjs — benchmark unitrace.sh against a FROZEN cursor baseline
// across quick/medium/deep trace tasks on medium/large local repos.
//
// The cursor agent is never invoked: cursor's outputs are captured once into
// cursor-baseline/ (build-cursor-baseline.mjs) and loaded from there. Each frozen
// sample carries its measured wall time and the judge score it earned; the bench
// reuses that score while the judge is unchanged (judgeSignature match) and
// re-judges the stored markdown only if the judge changes, so both arms always
// face the same judge. Only unitrace runs live, repeated per --repeats.

import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { extractTraceCitations } from "../lib/trace-citations.mjs";
import { daemonAsk, warmDaemonPool } from "../lib/daemon-client.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SCRIPTS_DIR = path.resolve(HERE, "..");
const UNITRACE_SH = path.join(SCRIPTS_DIR, "unitrace.sh");
// The cursor agent is never invoked. Its outputs are frozen under cursor-baseline/
// (see build-cursor-baseline.mjs); the bench loads those instead of running cursor.
const CURSOR_BASELINE_DIR = path.join(HERE, "cursor-baseline");
const DEFAULT_TASKS = path.join(HERE, "trace-repo-matrix.json");
const RESULTS_ROOT = path.join(HERE, "results");
const JUDGE_NAMESPACE = "trace-vs-cursor-judge";
const JUDGE_MODEL = (process.env.UNITRACE_BENCH_JUDGE_MODEL || "gpt-realtime-2").trim();

const JUDGE_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["score", "reason"],
  properties: {
    score: { type: "integer" },
    reason: { type: "string" },
  },
};

const JUDGE_SYSTEM = [
  "You are a strict evaluator of codebase trace answers.",
  "You will receive a QUESTION, depth label, load-bearing EXPECTED PATHS, and a TRACE answer produced by an automated tracer.",
  "Score the TRACE 0-10 for correctness, completeness, and grounding.",
  "Treat EXPECTED PATHS as strong hints about the load-bearing files; they are not exhaustive, but missing most of them without an equivalent explanation is a quality failure.",
  "Reward answers that explain the actual flow, cite real paths/spans, and stay implementation-grounded.",
  "Penalize shallow summaries, doc-only answers to implementation questions, generic prose, or citations that do not support the answer.",
  "Return only the integer score and one short reason.",
].join("\n");

// A stable fingerprint of the judge (model + system prompt + schema). Frozen
// cursor judge scores are reused only while this matches; if the judge changes,
// the bench re-judges the stored cursor text so both arms face the same judge.
export function judgeSignature() {
  const material = JSON.stringify({ model: JUDGE_MODEL, system: JUDGE_SYSTEM, schema: JUDGE_SCHEMA });
  return createHash("sha256").update(material).digest("hex").slice(0, 16);
}

// Load the frozen cursor baseline (markdown samples + measured wall time + the
// judge score each earned at run time). Returns null when no baseline exists.
function loadCursorBaseline() {
  const manifestPath = path.join(CURSOR_BASELINE_DIR, "manifest.json");
  if (!existsSync(manifestPath)) return null;
  let doc;
  try { doc = JSON.parse(readFileSync(manifestPath, "utf8")); } catch { return null; }
  const judgeMatches = doc.judgeSignature === judgeSignature();
  return { doc, judgeMatches };
}

function expandHome(p) {
  return p && p.startsWith("~") ? path.join(os.homedir(), p.slice(1)) : p;
}

function argValue(name, fallback) {
  const i = process.argv.indexOf(name);
  return i === -1 ? fallback : process.argv[i + 1];
}

function median(nums) {
  const xs = nums.filter((n) => Number.isFinite(n)).sort((a, b) => a - b);
  if (!xs.length) return NaN;
  const mid = Math.floor(xs.length / 2);
  return xs.length % 2 ? xs[mid] : (xs[mid - 1] + xs[mid]) / 2;
}

function quantile(nums, q) {
  const xs = nums.filter((n) => Number.isFinite(n)).sort((a, b) => a - b);
  if (!xs.length) return NaN;
  const pos = (xs.length - 1) * q;
  const base = Math.floor(pos);
  const rest = pos - base;
  return xs[base + 1] !== undefined ? xs[base] + rest * (xs[base + 1] - xs[base]) : xs[base];
}

function fmt(n, dp = 0) {
  if (n == null || Number.isNaN(n)) return "-";
  return typeof n === "number" ? n.toFixed(dp) : String(n);
}

function sanitizeId(s) {
  return String(s || "").replace(/[^A-Za-z0-9._-]+/g, "-");
}

function normalizePath(p) {
  return String(p || "").replace(/\\/g, "/").replace(/^\.\/+/, "");
}

function parseArgs() {
  const repeats = Number(argValue("--repeats", "1"));
  const depths = (argValue("--depths", "") || "").split(",").map((s) => s.trim()).filter(Boolean);
  const repos = (argValue("--repos", "") || "").split(",").map((s) => s.trim()).filter(Boolean);
  const ids = (argValue("--ids", "") || "").split(",").map((s) => s.trim()).filter(Boolean);
  const tasksFile = expandHome(argValue("--tasks", DEFAULT_TASKS));
  const outDir = expandHome(argValue("--out", path.join(RESULTS_ROOT, new Date().toISOString().replace(/[:.]/g, "-"))));
  return { repeats, depths, repos, ids, tasksFile, outDir };
}

function loadTasks(file, { depths, repos, ids }) {
  const doc = JSON.parse(readFileSync(file, "utf8"));
  const tasks = (doc.tasks || []).map((task) => ({
    ...task,
    repo: expandHome(task.repo),
    expected_paths: (task.expected_paths || []).map(normalizePath),
  }));
  return tasks.filter((task) => {
    if (depths.length && !depths.includes(task.depth)) return false;
    if (repos.length && !repos.some((repo) => task.repo.includes(repo))) return false;
    if (ids.length && !ids.includes(task.id)) return false;
    return true;
  });
}

function traceSectionMetrics(markdown) {
  const text = String(markdown || "");
  const hasFlow = /^## Flow\b/m.test(text);
  const hasKeyFiles = /^## Key files\b/m.test(text);
  const hasCodeRefs = /^## Code references\b/m.test(text);
  const hasTables = /^\| .+ \|\s*$/m.test(text);
  const headingCount = (text.match(/^## /gm) || []).length;
  return {
    hasFlow,
    hasKeyFiles,
    hasCodeRefs,
    hasTables,
    headingCount,
    completeness: [hasFlow, hasKeyFiles, hasCodeRefs].filter(Boolean).length,
  };
}

function pathMentions(markdown, expectedPaths, citedPaths) {
  const body = normalizePath(markdown);
  let hits = 0;
  const matched = [];
  for (const p of expectedPaths) {
    const exact = citedPaths.has(p) || body.includes(p);
    const base = path.basename(p);
    const basenameHit = body.includes(base);
    if (exact || basenameHit) {
      hits += 1;
      matched.push(p);
    }
  }
  return { hits, matched, ratio: expectedPaths.length ? hits / expectedPaths.length : 1 };
}

function qualityMetrics(markdown, structuredJsonPath, expectedPaths) {
  const citations = extractTraceCitations(markdown, { structuredJsonPath });
  const sections = traceSectionMetrics(markdown);
  const citedPaths = new Set(citations.all.map((c) => normalizePath(c.path)));
  const coverage = pathMentions(markdown, expectedPaths, citedPaths);
  const citationPart = Math.min(citations.uniqueCitations, 12) / 12;
  const pathPart = Math.min(citations.uniquePaths, 6) / 6;
  const sectionPart = sections.completeness / 3;
  const qualityIndex = Math.round((citationPart * 0.4 + pathPart * 0.25 + sectionPart * 0.2 + coverage.ratio * 0.15) * 100);
  return {
    citations,
    sections,
    coverage,
    qualityIndex,
  };
}

function sliceForJudge(markdown, maxChars) {
  const text = String(markdown || "");
  if (text.length <= maxChars) return text;
  const half = Math.max(2000, Math.floor((maxChars - 32) / 2));
  return `${text.slice(0, half)}\n\n[... truncated for judging ...]\n\n${text.slice(text.length - half)}`;
}

async function judgeTrace(task, markdown) {
  if (!markdown || !markdown.trim()) return { score: 0, reason: "empty trace" };
  for (let attempt = 0; attempt < 3; attempt += 1) {
    for (const maxChars of [20000, 12000, 8000, 5000]) {
      const user = [
        "QUESTION:",
        task.question,
        "",
        `DEPTH: ${task.depth}`,
        "",
        "EXPECTED PATHS:",
        task.expected_paths.join("\n"),
        "",
        "TRACE:",
        sliceForJudge(markdown, maxChars),
      ].join("\n");
      try {
        const out = await daemonAsk(
          JUDGE_NAMESPACE,
          { system: JUDGE_SYSTEM, user, schema: JUDGE_SCHEMA, schemaName: "judge_trace" },
          { model: JUDGE_MODEL },
        );
        if (out && typeof out.score === "number") {
          return { score: out.score, reason: String(out.reason || "") };
        }
      } catch {
        // Retry with smaller slices, then retry the whole ladder.
      }
    }
  }
  return { score: NaN, reason: "judge failed" };
}

function compositeScore(metrics, judgeScore) {
  const judgePart = Number.isFinite(judgeScore) ? (judgeScore / 10) * 100 : 0;
  return Math.round(judgePart * 0.55 + metrics.qualityIndex * 0.30 + metrics.coverage.ratio * 100 * 0.15);
}

// Build cursor records for one task from the frozen baseline — no agent run.
// Each stored sample becomes a record: quality metrics are recomputed from the
// stored markdown (deterministic), the judge score is the frozen one when the
// judge is unchanged and otherwise re-judged, and wall time is the measured
// value captured when the cursor run was real. Returns [] when no baseline
// covers this task.
async function cursorRecordsFromBaseline(task, baseline) {
  const entry = baseline?.doc?.tasks?.[task.id];
  if (!entry || !Array.isArray(entry.samples) || !entry.samples.length) return [];
  const records = [];
  for (const [i, sample] of entry.samples.entries()) {
    const mdPath = path.join(CURSOR_BASELINE_DIR, sample.file);
    if (!existsSync(mdPath)) continue;
    const outMd = readFileSync(mdPath, "utf8");
    const metrics = qualityMetrics(outMd, null, task.expected_paths);
    let judged;
    if (baseline.judgeMatches && Number.isFinite(sample.frozenJudge)) {
      judged = { score: sample.frozenJudge, reason: "frozen baseline judge (judge unchanged)" };
    } else {
      judged = await judgeTrace(task, outMd);
    }
    const composite = compositeScore(metrics, judged.score);
    records.push({
      arm: "cursor",
      taskId: task.id,
      repo: task.repo,
      depth: task.depth,
      question: task.question,
      expectedPaths: task.expected_paths,
      repeat: i,
      code: 0,
      wallMs: Number.isFinite(sample.wallMs) ? sample.wallMs : NaN,
      runDir: mdPath,
      judgeScore: judged.score,
      judgeReason: judged.reason,
      coverageRatio: metrics.coverage.ratio,
      coverageHits: metrics.coverage.hits,
      qualityIndex: metrics.qualityIndex,
      uniqueCitations: metrics.citations.uniqueCitations,
      uniquePaths: metrics.citations.uniquePaths,
      completeness: metrics.sections.completeness,
      composite,
      fromBaseline: true,
    });
  }
  return records;
}

function parseRunId(text) {
  const match = String(text || "").match(/UNITRACE_RUN_ID=([A-Za-z0-9._-]+)/);
  return match ? match[1] : null;
}

async function runTrace(script, task, { arm, repeat, runsDir }) {
  const runId = sanitizeId(`${arm}__${task.id}__${repeat}`);
  const runRoot = path.join(runsDir, arm);
  mkdirSync(runRoot, { recursive: true });
  return new Promise((resolve) => {
    const env = {
      ...process.env,
      UNITRACE_WORKSPACE: task.repo,
      UNITRACE_RUNS_DIR: runRoot,
      UNITRACE_RUN_ID: runId,
    };
    const t0 = Date.now();
    const child = spawn("bash", [script, task.question], {
      env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    // cleanup-traps: ok - process signal handlers for child cleanup
    const cleanup = () => { try { child.kill("SIGTERM"); } catch {} };
    process.once("SIGINT", cleanup);
    process.once("SIGTERM", cleanup);
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => { stdout += d; });
    child.stderr.on("data", (d) => { stderr += d; });
    child.on("close", (code) => {
      process.off("SIGINT", cleanup);
      process.off("SIGTERM", cleanup);
      const wallMs = Date.now() - t0;
      const runDir = path.join(runRoot, runId);
      const outPath = path.join(runDir, "out.md");
      const errPath = path.join(runDir, "err.log");
      const structuredJsonPath = path.join(runDir, "structured.json");
      let outMd = stdout;
      let errLog = stderr;
      if (existsSync(outPath)) outMd = readFileSync(outPath, "utf8");
      if (existsSync(errPath)) errLog = readFileSync(errPath, "utf8");
      resolve({
        arm,
        code,
        wallMs,
        stdout,
        stderr,
        outMd,
        errLog,
        runDir,
        structuredJsonPath: existsSync(structuredJsonPath) ? structuredJsonPath : null,
        parsedRunId: parseRunId(stdout) || runId,
      });
    });
  });
}

function summarizeArm(rows, arm) {
  const subset = rows.filter((r) => r.arm === arm);
  return {
    arm,
    runs: subset.length,
    ok: subset.filter((r) => r.code === 0).length,
    medianWallMs: median(subset.map((r) => r.wallMs)),
    medianJudge: median(subset.map((r) => r.judgeScore)),
    medianCoverage: median(subset.map((r) => r.coverageRatio * 100)),
    medianQualityIndex: median(subset.map((r) => r.qualityIndex)),
    medianComposite: median(subset.map((r) => r.composite)),
    p25WallMs: quantile(subset.map((r) => r.wallMs), 0.25),
    p75WallMs: quantile(subset.map((r) => r.wallMs), 0.75),
    minComposite: quantile(subset.map((r) => r.composite), 0),
    maxComposite: quantile(subset.map((r) => r.composite), 1),
  };
}

// Aggregate each task across its repeats (median per arm) so the per-task
// quality/speed winner is a stable signal, not a single noisy sample.
function summarizeTasks(records, tasks) {
  return tasks.map((t) => {
    const u = records.filter((r) => r.taskId === t.id && r.arm === "unitrace");
    const c = records.filter((r) => r.taskId === t.id && r.arm === "cursor");
    const uComposite = median(u.map((r) => r.composite));
    const cComposite = median(c.map((r) => r.composite));
    const uWallMs = median(u.map((r) => r.wallMs));
    const cWallMs = median(c.map((r) => r.wallMs));
    return {
      id: t.id,
      depth: t.depth,
      reps: u.length,
      uComposite,
      cComposite,
      uJudge: median(u.map((r) => r.judgeScore)),
      cJudge: median(c.map((r) => r.judgeScore)),
      uWallMs,
      cWallMs,
      qualityWin: uComposite > cComposite,
      qualityTie: uComposite === cComposite,
      speedWin: uWallMs < cWallMs,
    };
  });
}

function summarizeDepth(rows, arm, depth) {
  const subset = rows.filter((r) => r.arm === arm && r.depth === depth);
  return {
    arm,
    depth,
    runs: subset.length,
    medianWallMs: median(subset.map((r) => r.wallMs)),
    medianJudge: median(subset.map((r) => r.judgeScore)),
    medianComposite: median(subset.map((r) => r.composite)),
  };
}

async function main() {
  const args = parseArgs();
  const tasks = loadTasks(args.tasksFile, args);
  if (!tasks.length) {
    process.stderr.write("no tasks selected\n");
    process.exit(2);
  }
  mkdirSync(args.outDir, { recursive: true });
  const runsDir = path.join(args.outDir, "runs");
  mkdirSync(runsDir, { recursive: true });

  warmDaemonPool(JUDGE_NAMESPACE, undefined, { model: JUDGE_MODEL }).catch(() => {});

  const baseline = loadCursorBaseline();
  const missingCursor = [];

  const records = [];
  for (const task of tasks) {
    // unitrace arm: live, repeated per --repeats.
    for (let repeat = 0; repeat < args.repeats; repeat += 1) {
      process.stderr.write(`run unitrace ${task.id} rep=${repeat}\n`);
      const run = await runTrace(UNITRACE_SH, task, { arm: "unitrace", repeat, runsDir });
      const metrics = qualityMetrics(run.outMd, run.structuredJsonPath, task.expected_paths);
      const judged = await judgeTrace(task, run.outMd);
      const composite = compositeScore(metrics, judged.score);
      records.push({
        arm: "unitrace",
        taskId: task.id,
        repo: task.repo,
        depth: task.depth,
        question: task.question,
        expectedPaths: task.expected_paths,
        repeat,
        code: run.code,
        wallMs: run.wallMs,
        runDir: run.runDir,
        judgeScore: judged.score,
        judgeReason: judged.reason,
        coverageRatio: metrics.coverage.ratio,
        coverageHits: metrics.coverage.hits,
        qualityIndex: metrics.qualityIndex,
        uniqueCitations: metrics.citations.uniqueCitations,
        uniquePaths: metrics.citations.uniquePaths,
        completeness: metrics.sections.completeness,
        composite,
      });
    }
    // cursor arm: frozen baseline, never an agent run, not multiplied by repeats.
    const cursorRecords = await cursorRecordsFromBaseline(task, baseline);
    if (cursorRecords.length) {
      process.stderr.write(`cursor ${task.id} from baseline (${cursorRecords.length} samples${baseline.judgeMatches ? ", frozen judge" : ", re-judged"})\n`);
      records.push(...cursorRecords);
    } else {
      missingCursor.push(task.id);
      process.stderr.write(`cursor ${task.id} SKIPPED (no baseline)\n`);
    }
  }

  const overall = [
    summarizeArm(records, "unitrace"),
    summarizeArm(records, "cursor"),
  ];
  const depths = [...new Set(tasks.map((t) => t.depth))];
  const depthSummary = [];
  for (const depth of depths) {
    depthSummary.push(summarizeDepth(records, "unitrace", depth));
    depthSummary.push(summarizeDepth(records, "cursor", depth));
  }

  const perTask = summarizeTasks(records, tasks);
  const qWins = perTask.filter((t) => t.qualityWin).length;
  const qTies = perTask.filter((t) => t.qualityTie).length;
  const qLosses = perTask.length - qWins - qTies;
  const sWins = perTask.filter((t) => t.speedWin).length;
  const qualityWinRate = perTask.length ? qWins / perTask.length : 0;

  const unitraceOverall = overall.find((s) => s.arm === "unitrace");
  const cursorOverall = overall.find((s) => s.arm === "cursor");
  const verdict = {
    pass:
      Number.isFinite(unitraceOverall.medianJudge)
      && Number.isFinite(cursorOverall.medianJudge)
      && unitraceOverall.medianWallMs < cursorOverall.medianWallMs
      && unitraceOverall.medianComposite > cursorOverall.medianComposite,
    reasons: [],
    notes: [],
    perTaskQuality: { wins: qWins, ties: qTies, losses: qLosses, winRate: qualityWinRate },
    perTaskSpeed: { wins: sWins, total: perTask.length },
  };
  if (!Number.isFinite(unitraceOverall.medianJudge) || !Number.isFinite(cursorOverall.medianJudge)) {
    verdict.reasons.push("one or more judged quality scores were unavailable; verdict is invalid until both arms judge cleanly");
  }
  if (!(unitraceOverall.medianWallMs < cursorOverall.medianWallMs)) {
    verdict.reasons.push(`speed median ${fmt(unitraceOverall.medianWallMs)}ms >= cursor ${fmt(cursorOverall.medianWallMs)}ms`);
  }
  if (!(unitraceOverall.medianComposite > cursorOverall.medianComposite)) {
    verdict.reasons.push(`quality median ${fmt(unitraceOverall.medianComposite)} <= cursor ${fmt(cursorOverall.medianComposite)}`);
  }

  // Stability notes: the aggregate median is noisy at low repeats. Surface the
  // per-task majority and flag when it disagrees with the aggregate verdict.
  verdict.notes.push(`per-task quality: ${qWins}W/${qTies}T/${qLosses}L (win-rate ${(qualityWinRate * 100).toFixed(0)}%)`);
  verdict.notes.push(`per-task speed wins: ${sWins}/${perTask.length}`);
  const medianSaysQualityWin = unitraceOverall.medianComposite > cursorOverall.medianComposite;
  const majoritySaysQualityWin = qWins > qLosses;
  if (medianSaysQualityWin !== majoritySaysQualityWin) {
    verdict.notes.push("quality signal within noise: aggregate median and per-task majority disagree; raise --repeats before trusting the verdict");
  }
  if (args.repeats < 3) {
    verdict.notes.push(`low-sample run (repeats=${args.repeats}); use --repeats 3 for a gating verdict`);
  }
  // Cursor provenance: cursor is never run live — it is the frozen baseline.
  if (baseline) {
    verdict.notes.push(
      `cursor = frozen baseline (generated ${baseline.doc.generatedAt}); ${baseline.judgeMatches ? "judge unchanged, frozen scores reused" : "judge changed, stored cursor text re-judged"}`,
    );
  } else {
    verdict.notes.push("no cursor baseline found (cursor-baseline/manifest.json missing); cursor arm empty — run build-cursor-baseline.mjs");
  }
  if (missingCursor.length) {
    verdict.notes.push(`no cursor baseline for ${missingCursor.length} task(s): ${missingCursor.join(", ")} — unitrace-only on those`);
  }

  const raw = { meta: args, tasks, records, overall, depthSummary, perTask, verdict, cursorBaseline: baseline ? { generatedAt: baseline.doc.generatedAt, judgeMatches: baseline.judgeMatches, judgeSignature: baseline.doc.judgeSignature } : null };
  writeFileSync(path.join(args.outDir, "raw.json"), JSON.stringify(raw, null, 2));

  const md = [];
  md.push(`# Trace vs Cursor benchmark`, "");
  md.push(`Tasks: ${tasks.length} · repeats: ${args.repeats}`, "");
  md.push(`## VERDICT: ${verdict.pass ? "PASS" : "FAIL"}`, "");
  for (const reason of verdict.reasons) md.push(`- ${reason}`);
  if (verdict.reasons.length) md.push("");
  for (const note of verdict.notes) md.push(`- note: ${note}`);
  if (verdict.notes.length) md.push("");

  md.push("## Aggregate signal", "");
  md.push(`- Quality: ${qWins}W / ${qTies}T / ${qLosses}L across ${perTask.length} tasks (win-rate ${(qualityWinRate * 100).toFixed(0)}%)`);
  md.push(`- Speed: ${sWins}/${perTask.length} tasks faster`);
  md.push("");

  md.push("## Overall", "");
  md.push("| arm | runs | ok | med wall (ms) | wall p25-p75 | med judge | med anchor % | med quality idx | med composite | comp range |");
  md.push("|---|---|---|---|---|---|---|---|---|---|");
  for (const s of overall) {
    md.push(`| ${s.arm} | ${s.runs} | ${s.ok} | ${fmt(s.medianWallMs)} | ${fmt(s.p25WallMs)}-${fmt(s.p75WallMs)} | ${fmt(s.medianJudge, 1)} | ${fmt(s.medianCoverage, 0)} | ${fmt(s.medianQualityIndex, 0)} | ${fmt(s.medianComposite, 0)} | ${fmt(s.minComposite)}-${fmt(s.maxComposite)} |`);
  }
  md.push("");

  md.push("## Per task (median across repeats)", "");
  md.push("| task | depth | reps | u wall | c wall | u judge | c judge | u comp | c comp | quality | speed |");
  md.push("|---|---|---|---|---|---|---|---|---|---|---|");
  for (const t of perTask) {
    const qmark = t.qualityTie ? "tie" : (t.qualityWin ? "WIN" : "loss");
    const smark = t.speedWin ? "WIN" : "loss";
    md.push(`| ${t.id} | ${t.depth} | ${t.reps} | ${fmt(t.uWallMs)} | ${fmt(t.cWallMs)} | ${fmt(t.uJudge, 1)} | ${fmt(t.cJudge, 1)} | ${fmt(t.uComposite)} | ${fmt(t.cComposite)} | ${qmark} | ${smark} |`);
  }
  md.push("");

  md.push("## By depth", "");
  md.push("| depth | arm | runs | med wall (ms) | med judge | med composite |");
  md.push("|---|---|---|---|---|---|");
  for (const s of depthSummary) {
    md.push(`| ${s.depth} | ${s.arm} | ${s.runs} | ${fmt(s.medianWallMs)} | ${fmt(s.medianJudge, 1)} | ${fmt(s.medianComposite, 0)} |`);
  }
  md.push("");

  md.push("## Per run", "");
  md.push("| task | depth | arm | rep | code | wall (ms) | judge | anchors | quality idx | composite |");
  md.push("|---|---|---|---|---|---|---|---|---|---|");
  for (const r of records) {
    md.push(`| ${r.taskId} | ${r.depth} | ${r.arm} | ${r.repeat} | ${r.code} | ${fmt(r.wallMs)} | ${fmt(r.judgeScore, 1)} | ${r.coverageHits}/${r.expectedPaths.length} | ${fmt(r.qualityIndex, 0)} | ${fmt(r.composite, 0)} |`);
  }
  md.push("");

  writeFileSync(path.join(args.outDir, "summary.md"), `${md.join("\n")}\n`);
  process.stdout.write(`Results: ${args.outDir}\n`);
  if (!verdict.pass) process.exitCode = 1;
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  main().catch((err) => {
    process.stderr.write(`trace-vs-cursor fatal: ${err?.stack || err}\n`);
    process.exit(1);
  });
}
