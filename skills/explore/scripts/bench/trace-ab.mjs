#!/usr/bin/env node
// trace-ab.mjs — empirical A/B harness for the trace fast path. Runs trace-rt.sh
// across a variant matrix (explore mode x synth model x nav count x rounds)
// against a deep-context prompt set on a large repo (kepler), then judges each
// trace 0-10 for correctness/completeness/grounding via the warm daemon
// (gpt-realtime-2). Reports median quality + median latency + quality-per-second
// per variant so defaults are chosen by evidence, not guess.
//
// Usage:
//   node scripts/bench/trace-ab.mjs \
//     --repo ~/__devlocal/kepler \
//     --prompts scripts/bench/trace-kepler-prompts.json \
//     --variants agentic-full,nav-mini,hybrid-mini \
//     --repeats 2
//
// Results land in scripts/bench/results/<timestamp>/ (raw.json + summary.md).

import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync, existsSync, readdirSync, statSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { daemonAsk, warmDaemonPool } from "../lib/daemon-client.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SCRIPTS_DIR = path.resolve(HERE, "..");
const TRACE_SH = path.join(SCRIPTS_DIR, "trace-rt.sh");
const JUDGE_NAMESPACE = "trace-ab-judge";
const JUDGE_MODEL = (process.env.EXPLORE_AB_JUDGE_MODEL || "gpt-realtime-2").trim();

function expandHome(p) {
  return p && p.startsWith("~") ? path.join(os.homedir(), p.slice(1)) : p;
}

function argValue(name, fallback) {
  const i = process.argv.indexOf(name);
  return i === -1 ? fallback : process.argv[i + 1];
}

// Named variants: each maps to the env overrides that define it. Add freely;
// the harness runs whatever --variants lists (default: all).
const VARIANTS = {
  "agentic-full": { EXPLORE_RT_EXPLORE_MODE: "agentic", EXPLORE_RT_SYNTH_MODEL: "gpt-realtime-2", EXPLORE_RT_DAEMON: "1" },
  "agentic-session": { EXPLORE_RT_EXPLORE_MODE: "agentic", EXPLORE_RT_DAEMON: "0" },
  "nav-mini-4x2": { EXPLORE_RT_EXPLORE_MODE: "nav", EXPLORE_RT_NAV_MODEL: "gpt-realtime-mini", EXPLORE_RT_SYNTH_MODEL: "gpt-realtime-2", EXPLORE_RT_NAV_COUNT: "4", EXPLORE_RT_NAV_ROUNDS: "2" },
  "nav-mini-4x1": { EXPLORE_RT_EXPLORE_MODE: "nav", EXPLORE_RT_NAV_MODEL: "gpt-realtime-mini", EXPLORE_RT_SYNTH_MODEL: "gpt-realtime-2", EXPLORE_RT_NAV_COUNT: "4", EXPLORE_RT_NAV_ROUNDS: "1" },
  "nav-mini-8x1": { EXPLORE_RT_EXPLORE_MODE: "nav", EXPLORE_RT_NAV_MODEL: "gpt-realtime-mini", EXPLORE_RT_SYNTH_MODEL: "gpt-realtime-2", EXPLORE_RT_NAV_COUNT: "8", EXPLORE_RT_NAV_ROUNDS: "1" },
  "nav-mini-8x2": { EXPLORE_RT_EXPLORE_MODE: "nav", EXPLORE_RT_NAV_MODEL: "gpt-realtime-mini", EXPLORE_RT_SYNTH_MODEL: "gpt-realtime-2", EXPLORE_RT_NAV_COUNT: "8", EXPLORE_RT_NAV_ROUNDS: "2" },
  "nav-mini-8x2-deepseed": { EXPLORE_RT_EXPLORE_MODE: "nav", EXPLORE_RT_NAV_MODEL: "gpt-realtime-mini", EXPLORE_RT_SYNTH_MODEL: "gpt-realtime-2", EXPLORE_RT_NAV_COUNT: "8", EXPLORE_RT_NAV_ROUNDS: "2", EXPLORE_RT_NAV_SEED_SPANS: "20", EXPLORE_RT_NAV_ROUND_SPANS: "12", EXPLORE_RT_EXPLORE_MAX_READS: "28" },
  "nav-mini-6x2": { EXPLORE_RT_EXPLORE_MODE: "nav", EXPLORE_RT_NAV_MODEL: "gpt-realtime-mini", EXPLORE_RT_SYNTH_MODEL: "gpt-realtime-2", EXPLORE_RT_NAV_COUNT: "6", EXPLORE_RT_NAV_ROUNDS: "2" },
  "nav-mini-synthmini": { EXPLORE_RT_EXPLORE_MODE: "nav", EXPLORE_RT_NAV_MODEL: "gpt-realtime-mini", EXPLORE_RT_SYNTH_MODEL: "gpt-realtime-mini", EXPLORE_RT_NAV_COUNT: "4", EXPLORE_RT_NAV_ROUNDS: "2" },
  "hybrid-mini-4x2": { EXPLORE_RT_EXPLORE_MODE: "hybrid", EXPLORE_RT_NAV_MODEL: "gpt-realtime-mini", EXPLORE_RT_SYNTH_MODEL: "gpt-realtime-2", EXPLORE_RT_NAV_COUNT: "4", EXPLORE_RT_NAV_ROUNDS: "2" },
};

const JUDGE_INSTRUCTIONS = [
  "You are a strict evaluator of codebase trace answers. You are given a QUESTION about a real repository and a TRACE that an automated tool produced to answer it.",
  "Score the TRACE 0-10 on how well it answers the QUESTION, judging three things together:",
  "- correctness: are the claims about control/data flow accurate and non-hallucinated?",
  "- completeness: does it cover the load-bearing files and the full path the question asks about?",
  "- grounding: are the cited files/spans real and relevant (not vague or generic)?",
  "Use this scale: 0-2 wrong or empty; 3-4 partially relevant but shallow or with errors; 5-6 mostly correct but missing key pieces; 7-8 correct and well-grounded with minor gaps; 9-10 thorough, precise, fully grounded.",
  "Judge only what the TRACE says. Return a single integer score and a one-sentence reason via the tool.",
].join("\n");

const JUDGE_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["score", "reason"],
  properties: {
    score: { type: "integer", description: "0-10 overall quality of the trace as an answer to the question" },
    reason: { type: "string", description: "one sentence justification" },
  },
};

function median(nums) {
  const xs = nums.filter((n) => Number.isFinite(n)).sort((a, b) => a - b);
  if (!xs.length) return NaN;
  const mid = Math.floor(xs.length / 2);
  return xs.length % 2 ? xs[mid] : (xs[mid - 1] + xs[mid]) / 2;
}

function parsePhases(errLog) {
  const out = {};
  for (const line of String(errLog || "").split("\n")) {
    const m = line.match(/phase\s+(.*)/);
    if (!m) continue;
    for (const kv of m[1].split(/\s+/)) {
      const eq = kv.indexOf("=");
      if (eq < 0) continue;
      const k = kv.slice(0, eq);
      const v = kv.slice(eq + 1);
      const n = Number(v);
      out[k] = Number.isFinite(n) ? n : v;
    }
  }
  return out;
}

function runTrace({ question, env, repo, runsDir, runId }) {
  return new Promise((resolve) => {
    const childEnv = {
      ...process.env,
      ...env,
      EXPLORE_WORKSPACE: repo,
      EXPLORE_RUNS_DIR: runsDir,
      EXPLORE_RUN_ID: runId,
    };
    const t0 = Date.now();
    const child = spawn("bash", [TRACE_SH, question], { env: childEnv, stdio: ["ignore", "pipe", "pipe"] });
    const killChild = () => { try { child.kill("SIGTERM"); } catch { /* ignore */ } };
    process.on("SIGINT", killChild);
    process.on("SIGTERM", killChild);
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => { stdout += d; });
    child.stderr.on("data", (d) => { stderr += d; });
    child.on("close", (code) => {
      process.off("SIGINT", killChild);
      process.off("SIGTERM", killChild);
      const wallMs = Date.now() - t0;
      const runDir = path.join(runsDir, runId);
      let outMd = "";
      let errLog = "";
      try { outMd = readFileSync(path.join(runDir, "out.md"), "utf8"); } catch { /* ignore */ }
      try { errLog = readFileSync(path.join(runDir, "err.log"), "utf8"); } catch { /* ignore */ }
      resolve({ code, wallMs, outMd: outMd || stdout, errLog: errLog || stderr, phases: parsePhases(errLog) });
    });
  });
}

async function judge(question, trace) {
  if (!trace || !trace.trim()) return { score: 0, reason: "empty trace" };
  const user = [
    "QUESTION:",
    question,
    "",
    "TRACE:",
    trace.slice(0, 18000),
    "",
    "Score this trace now.",
  ].join("\n");
  const res = await daemonAsk(
    JUDGE_NAMESPACE,
    { system: JUDGE_INSTRUCTIONS, user, schema: JUDGE_SCHEMA, schemaName: "judge" },
    { model: JUDGE_MODEL },
  );
  if (!res || typeof res.score !== "number") return { score: NaN, reason: "judge failed" };
  return { score: res.score, reason: String(res.reason || "") };
}

async function main() {
  const repo = expandHome(argValue("--repo", "~/__devlocal/kepler"));
  const promptsPath = expandHome(argValue("--prompts", path.join(HERE, "trace-kepler-prompts.json")));
  const repeats = Number(argValue("--repeats", "2"));
  const variantArg = argValue("--variants", "");
  const variantNames = variantArg ? variantArg.split(",").map((s) => s.trim()).filter(Boolean) : Object.keys(VARIANTS);
  const onlyPrompt = argValue("--prompt-id", "");

  if (!existsSync(repo)) { process.stderr.write(`repo not found: ${repo}\n`); process.exit(2); }
  for (const v of variantNames) if (!VARIANTS[v]) { process.stderr.write(`unknown variant: ${v}\n`); process.exit(2); }

  const promptDoc = JSON.parse(readFileSync(promptsPath, "utf8"));
  let prompts = promptDoc.prompts || [];
  if (onlyPrompt) prompts = prompts.filter((p) => p.id === onlyPrompt);
  if (!prompts.length) { process.stderr.write("no prompts to run\n"); process.exit(2); }

  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const resultsDir = path.join(HERE, "results", stamp);
  const runsDir = path.join(resultsDir, "runs");
  mkdirSync(runsDir, { recursive: true });

  // Warm the judge pool up front so judging never pays a cold connect.
  warmDaemonPool(JUDGE_NAMESPACE, undefined, { model: JUDGE_MODEL }).catch(() => {});

  const records = [];
  for (const variant of variantNames) {
    const env = VARIANTS[variant];
    for (const prompt of prompts) {
      for (let r = 0; r < repeats; r += 1) {
        const runId = `${variant}__${prompt.id}__${r}`.replace(/[^A-Za-z0-9._-]/g, "-");
        process.stderr.write(`run ${runId} ...\n`);
        const res = await runTrace({ question: prompt.question, env, repo, runsDir, runId });
        const q = await judge(prompt.question, res.outMd);
        const rec = {
          variant, promptId: prompt.id, repeat: r, code: res.code,
          wallMs: res.wallMs,
          exploreMs: res.phases.explore_ms ?? null,
          submitMs: res.phases.submit_ms ?? null,
          connectMs: res.phases.connect_ms ?? null,
          filesRead: res.phases.files_read ?? null,
          score: q.score, reason: q.reason,
        };
        records.push(rec);
        process.stderr.write(`  -> code=${res.code} wall=${res.wallMs}ms files=${rec.filesRead} score=${q.score}\n`);
      }
    }
  }

  // Aggregate per variant.
  const byVariant = new Map();
  for (const rec of records) {
    if (!byVariant.has(rec.variant)) byVariant.set(rec.variant, []);
    byVariant.get(rec.variant).push(rec);
  }
  const summary = [...byVariant.entries()].map(([variant, recs]) => {
    const medWall = median(recs.map((r) => r.wallMs));
    const medScore = median(recs.map((r) => r.score));
    const medExplore = median(recs.map((r) => r.exploreMs).filter((x) => x != null));
    const medSubmit = median(recs.map((r) => r.submitMs).filter((x) => x != null));
    const fails = recs.filter((r) => r.code !== 0).length;
    const qps = Number.isFinite(medScore) && medWall > 0 ? medScore / (medWall / 1000) : NaN;
    return { variant, runs: recs.length, fails, medWallMs: medWall, medExploreMs: medExplore, medSubmitMs: medSubmit, medScore, qualityPerSec: qps };
  }).sort((a, b) => (b.qualityPerSec || -1) - (a.qualityPerSec || -1));

  writeFileSync(path.join(resultsDir, "raw.json"), JSON.stringify({ repo, repeats, variants: variantNames, records, summary }, null, 2));

  const md = [];
  md.push(`# Trace A/B results — ${stamp}`, "");
  md.push(`Repo: \`${repo}\` · prompts: ${prompts.length} · repeats: ${repeats}`, "");
  md.push("## Variant summary (sorted by quality-per-second)", "");
  md.push("| variant | runs | fails | med wall (ms) | med explore | med submit | med score | quality/sec |");
  md.push("|---|---|---|---|---|---|---|---|");
  for (const s of summary) {
    md.push(`| ${s.variant} | ${s.runs} | ${s.fails} | ${fmt(s.medWallMs)} | ${fmt(s.medExploreMs)} | ${fmt(s.medSubmitMs)} | ${fmt(s.medScore)} | ${fmt(s.qualityPerSec, 3)} |`);
  }
  md.push("", "## Per-run detail", "");
  md.push("| variant | prompt | rep | code | wall (ms) | explore | submit | files | score | reason |");
  md.push("|---|---|---|---|---|---|---|---|---|---|");
  for (const r of records) {
    md.push(`| ${r.variant} | ${r.promptId} | ${r.repeat} | ${r.code} | ${r.wallMs} | ${fmt(r.exploreMs)} | ${fmt(r.submitMs)} | ${fmt(r.filesRead)} | ${fmt(r.score)} | ${String(r.reason || "").replace(/\|/g, "/").slice(0, 100)} |`);
  }
  md.push("");
  writeFileSync(path.join(resultsDir, "summary.md"), md.join("\n"));

  process.stdout.write(`\n${md.join("\n")}\n\nResults: ${resultsDir}\n`);
}

function fmt(n, dp = 0) {
  if (n == null || Number.isNaN(n)) return "-";
  return typeof n === "number" ? n.toFixed(dp) : String(n);
}

main().catch((e) => { process.stderr.write(`trace-ab fatal: ${e?.stack || e}\n`); process.exit(1); });
