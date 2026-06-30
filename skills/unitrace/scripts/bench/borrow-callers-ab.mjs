#!/usr/bin/env node
// borrow-callers-ab.mjs -- live borrow on/off A/B for the lower-volume shared
// daemon callers (enhance-prompt, and optionally websearch), so the broad
// UNITRACE_DAEMON_RTINFER flag is proven on EVERY caller -- not just search and
// trace -- before it is removed.
//
// For each caller it runs the same prompt set twice (borrow-off vs borrow-on),
// judges output quality 0-10 via the warm daemon (gpt-realtime-2), and parses
// the `[daemon] ns=... served rtinfer=N direct=M` attribution so a borrow-on arm
// that never actually reached rtinfer is flagged invalid rather than passing.
//
// Parity bar (not "better", just "no worse"): median quality within 0.5 and
// wall latency not worse by >15%, served-rate >= 90% on borrow-on.
//
// This is a LIVE bench (needs Codex auth + the daemon/rtinfer endpoint);
// excluded from `just test-all`. Results -> bench/results/<ts>/borrow-callers/.
//
// Usage:
//   borrow-callers-ab.mjs [--callers enhance,websearch] [--repeats N]
//       [--repo DIR] [--prompts FILE] [--debug]
//   (websearch runs live web fetches; omit it for an offline-repo-only pass.)

import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync, mkdtempSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { daemonAsk, warmDaemonPool } from "../lib/daemon-client.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SCRIPTS_DIR = path.resolve(HERE, "..");
const REPO_ROOT = path.resolve(HERE, "../../../..");
const ENHANCE_MJS = path.join(SCRIPTS_DIR, "enhance-prompt.mjs");
const WEBSEARCH_MJS = path.join(SCRIPTS_DIR, "realtime-websearch.mjs");
const JUDGE_NAMESPACE = "borrow-callers-judge";
const JUDGE_MODEL = (process.env.UNITRACE_AB_JUDGE_MODEL || "gpt-realtime-2").trim();

const ARMS = {
  "borrow-off": { UNITRACE_DAEMON_RTINFER: "0", UNITRACE_DAEMON_DEBUG: "1" },
  "borrow-on": { UNITRACE_DAEMON_RTINFER: "1", UNITRACE_DAEMON_DEBUG: "1", CSE_RTINFER_URL: (process.env.CSE_RTINFER_URL || "http://127.0.0.1:8787").trim(), CSE_RTINFER_STRICT_URL: "1" },
};

const JUDGE_INSTRUCTIONS = [
  "You are a strict evaluator of an automated assistant's output for a software task.",
  "You are given the original REQUEST and the OUTPUT the tool produced.",
  "Score the OUTPUT 0-10 on usefulness: is it concrete, grounded in plausible repo/source detail, and directly responsive?",
  "0-2 empty or wrong; 3-4 vague/generic; 5-6 useful but shallow; 7-8 concrete and well-targeted; 9-10 precise, grounded, high-signal.",
  "Return a single integer score and a one-sentence reason via the tool.",
].join("\n");

const JUDGE_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["score", "reason"],
  properties: {
    score: { type: "integer", description: "0-10 usefulness of the output as an answer to the request" },
    reason: { type: "string", description: "one sentence justification" },
  },
};

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

function parseServed(stderr) {
  let rtinfer = 0, direct = 0;
  for (const m of String(stderr || "").matchAll(/\[daemon\] ns=\S+ served rtinfer=(\d+) direct=(\d+)/g)) {
    rtinfer += parseInt(m[1], 10);
    direct += parseInt(m[2], 10);
  }
  return { rtinfer, direct };
}

function spawnTracked(...args) {
  const child = spawn(...args);
  const cleanup = () => { try { child.kill("SIGTERM"); } catch { /* ignore */ } };
  process.on("SIGINT", cleanup);
  process.on("SIGTERM", cleanup);
  child.once("close", () => {
    process.off("SIGINT", cleanup);
    process.off("SIGTERM", cleanup);
  });
  return child;
}

// Run enhance-prompt.mjs with a JSON {prompt,cwd} on stdin; returns its JSON out.
function runEnhance(prompt, repo, env) {
  return new Promise((resolve) => {
    const childEnv = { ...process.env, ...env };
    const t0 = Date.now();
    const child = spawnTracked(process.env.PYTHON_NODE || "node", [ENHANCE_MJS], { env: childEnv, stdio: ["pipe", "pipe", "pipe"] });
    let stdout = "", stderr = "";
    child.stdout.on("data", (d) => { stdout += d; });
    child.stderr.on("data", (d) => { stderr += d; });
    child.on("close", () => {
      const ms = Date.now() - t0;
      let obj = null;
      try { obj = JSON.parse(stdout || "{}"); } catch { obj = null; }
      const text = obj && obj.enhanced_prompt ? String(obj.enhanced_prompt) : (obj ? JSON.stringify(obj) : "");
      resolve({ ms, text, ok: Boolean(obj && obj.ok !== false && text), served: parseServed(stderr) });
    });
    child.stdin.write(JSON.stringify({ prompt, cwd: repo }));
    child.stdin.end();
  });
}

// Run realtime-websearch.mjs live for one goal; returns the synthesized answer.
function runWebsearch(goal, env) {
  return new Promise((resolve) => {
    const tmp = mkdtempSync(path.join(os.tmpdir(), "borrow-ws-"));
    const promptFile = path.join(tmp, "prompt.txt");
    const outFile = path.join(tmp, "out.md");
    const rawFile = path.join(tmp, "raw.json");
    const errFile = path.join(tmp, "err.log");
    writeFileSync(promptFile, `GOAL: ${goal}\n`);
    const childEnv = { ...process.env, ...env };
    const t0 = Date.now();
    const child = spawnTracked(process.env.PYTHON_NODE || "node", [
      WEBSEARCH_MJS, "--prompt-file", promptFile, "--goal", goal,
      "--out", outFile, "--raw", rawFile, "--err", errFile,
    ], { env: childEnv, stdio: ["ignore", "pipe", "pipe"] });
    let stderr = "";
    child.stderr.on("data", (d) => { stderr += d; });
    child.on("close", () => {
      const ms = Date.now() - t0;
      let text = "";
      try { text = readFileSync(outFile, "utf8"); } catch { /* ignore */ }
      let errLog = "";
      try { errLog = readFileSync(errFile, "utf8"); } catch { /* ignore */ }
      resolve({ ms, text, ok: Boolean(text && text.trim()), served: parseServed(stderr + errLog) });
    });
  });
}

async function judge(request, output) {
  if (!output || !output.trim()) return { score: 0, reason: "empty output" };
  const user = ["REQUEST:", request, "", "OUTPUT:", output.slice(0, 16000), "", "Score this output now."].join("\n");
  const res = await daemonAsk(
    JUDGE_NAMESPACE,
    { system: JUDGE_INSTRUCTIONS, user, schema: JUDGE_SCHEMA, schemaName: "judge" },
    { model: JUDGE_MODEL },
  );
  if (!res || typeof res.score !== "number") return { score: NaN, reason: "judge failed" };
  return { score: res.score, reason: String(res.reason || "") };
}

function fmt(n, dp = 0) {
  if (n == null || Number.isNaN(n)) return "-";
  return typeof n === "number" ? n.toFixed(dp) : String(n);
}

async function runCaller(caller, prompts, repo, repeats) {
  const records = [];
  for (const arm of Object.keys(ARMS)) {
    const env = ARMS[arm];
    for (const p of prompts) {
      for (let r = 0; r < repeats; r++) {
        const request = caller === "enhance" ? p.prompt : p.goal;
        process.stderr.write(`[borrow-callers] ${caller} ${arm} ${p.id} #${r}\n`);
        const res = caller === "enhance" ? await runEnhance(p.prompt, repo, env) : await runWebsearch(p.goal, env);
        const q = res.ok ? await judge(request, res.text) : { score: 0, reason: "tool failed" };
        records.push({ caller, arm, id: p.id, repeat: r, ms: res.ms, ok: res.ok, score: q.score, reason: q.reason, served: res.served });
      }
    }
  }
  return records;
}

function summarize(records) {
  const byArm = new Map();
  for (const rec of records) {
    if (!byArm.has(rec.arm)) byArm.set(rec.arm, []);
    byArm.get(rec.arm).push(rec);
  }
  return [...byArm.entries()].map(([arm, recs]) => {
    const rt = recs.reduce((a, r) => a + r.served.rtinfer, 0);
    const direct = recs.reduce((a, r) => a + r.served.direct, 0);
    return {
      arm, runs: recs.length, fails: recs.filter((r) => !r.ok).length,
      medScore: median(recs.map((r) => r.score)), medWallMs: median(recs.map((r) => r.ms)),
      servedRtinfer: rt, servedDirect: direct, servedRate: rt + direct ? Math.round((100 * rt) / (rt + direct)) : 0,
    };
  });
}

function callerVerdict(summary) {
  const off = summary.find((s) => s.arm === "borrow-off");
  const on = summary.find((s) => s.arm === "borrow-on");
  if (!off || !on) return { pass: null, reasons: ["missing borrow-off/on pair"] };
  const reasons = [];
  let pass = true;
  if (on.servedRate < 90) { pass = false; reasons.push(`borrow-on served-rate ${on.servedRate}% < 90% (daemon absent or falling through; invalid)`); }
  if (Number.isFinite(off.medScore) && Number.isFinite(on.medScore) && on.medScore < off.medScore - 0.5) { pass = false; reasons.push(`borrow-on median score ${fmt(on.medScore, 1)} < borrow-off ${fmt(off.medScore, 1)} - 0.5`); }
  if (off.medWallMs > 0 && on.medWallMs > off.medWallMs * 1.15) { pass = false; reasons.push(`borrow-on med wall ${fmt(on.medWallMs)}ms > borrow-off ${fmt(off.medWallMs)}ms +15%`); }
  if (on.fails > 0) { pass = false; reasons.push(`borrow-on had ${on.fails} failed run(s)`); }
  return { pass, reasons };
}

async function main() {
  const callers = argValue("--callers", "enhance").split(",").map((s) => s.trim()).filter(Boolean);
  const repeats = Math.max(1, parseInt(argValue("--repeats", "2"), 10) || 2);
  const repo = path.resolve(argValue("--repo", REPO_ROOT));
  const promptsPath = argValue("--prompts", path.join(HERE, "borrow-callers-prompts.json"));
  const promptDoc = JSON.parse(readFileSync(promptsPath, "utf8"));

  warmDaemonPool(JUDGE_NAMESPACE, undefined, { model: JUDGE_MODEL }).catch(() => {});

  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const outDir = path.join(HERE, "results", stamp, "borrow-callers");
  mkdirSync(outDir, { recursive: true });

  const all = { stamp, repo, repeats, callers: {} };
  let anyFail = false;
  const md = [`# Borrow-callers A/B - ${stamp}`, "", `Repo: \`${repo}\`  Repeats: ${repeats}`, ""];
  for (const caller of callers) {
    const prompts = promptDoc[caller];
    if (!prompts || !prompts.length) { process.stderr.write(`skip caller with no prompts: ${caller}\n`); continue; }
    const records = await runCaller(caller, prompts, repo, repeats);
    const summary = summarize(records);
    const v = callerVerdict(summary);
    all.callers[caller] = { summary, verdict: v, records };
    if (v.pass === false) anyFail = true;
    md.push(`## ${caller}: ${v.pass === null ? "n/a" : v.pass ? "PASS" : "FAIL"}`, "");
    for (const r of v.reasons) md.push(`- ${r}`);
    md.push("", "| arm | runs | fails | med score | med wall (ms) | served-rt% |", "|---|---|---|---|---|---|");
    for (const s of summary) md.push(`| ${s.arm} | ${s.runs} | ${s.fails} | ${fmt(s.medScore, 1)} | ${fmt(s.medWallMs)} | ${s.servedRate}% |`);
    md.push("");
  }

  writeFileSync(path.join(outDir, "raw.json"), JSON.stringify(all, null, 2));
  writeFileSync(path.join(outDir, "summary.md"), md.join("\n"));
  process.stdout.write(`${md.join("\n")}\n\nresults: ${outDir}\n`);
  if (anyFail) process.exit(2);
}

main().catch((e) => { process.stderr.write(`borrow-callers fatal: ${e?.stack || e}\n`); process.exit(1); });
