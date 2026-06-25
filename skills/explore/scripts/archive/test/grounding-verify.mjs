#!/usr/bin/env node
// grounding-verify.mjs — exit-proof for the grounded-citation scorer.
//
// Re-scores the existing v6 (all 1:3 / header-only citations) and v7 (real
// definition spans) trace benchmark artifacts with the grounding-aware scorer
// and asserts v7 now beats v6 on grounded quality, where the pre-grounding
// scorer ties them at qualityIndex 61.
//
// Usage:
//   node scripts/test/grounding-verify.mjs [--strategy shape|content|ast|auto]
//     [--v6 <dir>] [--v7 <dir>] [--workspace <repo root>]
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { median } from "../lib/bench-scorer-common.mjs";
import { scoreTraceOutput } from "../lib/bench-trace-scorer.mjs";

const SKILL_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

function parseArgs(argv) {
  const args = {
    strategy: "auto",
    v6: path.join(SKILL_ROOT, "benchmarks/2026-06-24-trace-rt-v6"),
    v7: path.join(SKILL_ROOT, "benchmarks/2026-06-25-trace-rt-v7"),
    workspace: SKILL_ROOT,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === "--strategy" && argv[i + 1]) args.strategy = argv[++i];
    else if (a === "--v6" && argv[i + 1]) args.v6 = path.resolve(argv[++i]);
    else if (a === "--v7" && argv[i + 1]) args.v7 = path.resolve(argv[++i]);
    else if (a === "--workspace" && argv[i + 1]) args.workspace = path.resolve(argv[++i]);
  }
  return args;
}

// Find every rt run's structured.json under a bench dir.
function findRtStructured(benchDir) {
  const out = [];
  if (!fs.existsSync(benchDir)) return out;
  const walk = (dir) => {
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const e of entries) {
      const full = path.join(dir, e.name);
      if (e.isDirectory()) walk(full);
      else if (e.name === "structured.json" && /(^|\/)rt-\d+\//.test(full)) out.push(full);
    }
  };
  walk(benchDir);
  return out;
}

function scoreBench(benchDir, { workspace, strategy }) {
  const rows = [];
  for (const sj of findRtStructured(benchDir)) {
    const outMd = path.join(path.dirname(sj), "out.md");
    const markdown = fs.existsSync(outMd) ? fs.readFileSync(outMd, "utf8") : "";
    const score = scoreTraceOutput({ markdown, structuredJsonPath: sj, workspace, grounding: strategy });
    rows.push(score);
  }
  return {
    runs: rows.length,
    medQualityIndex: median(rows.map((r) => r.qualityIndex)),
    medGrounded: median(rows.map((r) => r.groundedCitations)),
    medUniqueCitations: median(rows.map((r) => r.uniqueCitations)),
    medGroundednessRatio: median(rows.map((r) => r.groundednessRatio)),
  };
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const v6 = scoreBench(args.v6, args);
  const v7 = scoreBench(args.v7, args);

  if (!v6.runs || !v7.runs) {
    process.stderr.write(`grounding-verify: missing artifacts (v6 runs=${v6.runs}, v7 runs=${v7.runs})\n`);
    process.exit(2);
  }

  const fmt = (label, s) =>
    `${label}: runs=${s.runs} medGroundedCitations=${s.medGrounded} medQualityIndex=${s.medQualityIndex} medGroundednessRatio=${s.medGroundednessRatio.toFixed(2)}`;
  process.stdout.write(`strategy=${args.strategy} workspace=${args.workspace}\n`);
  process.stdout.write(`${fmt("v6 (all 1:3)", v6)}\n`);
  process.stdout.write(`${fmt("v7 (real spans)", v7)}\n`);

  const qiBeats = v7.medQualityIndex > v6.medQualityIndex;
  const groundedBeats = v7.medGrounded > v6.medGrounded;
  const pass = qiBeats && groundedBeats;
  process.stdout.write(
    `result: qualityIndex v7>v6=${qiBeats} groundedCitations v7>v6=${groundedBeats} -> ${pass ? "PASS" : "FAIL"}\n`,
  );
  process.exit(pass ? 0 : 1);
}

main();
