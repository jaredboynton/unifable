#!/usr/bin/env node
// build-cursor-baseline.mjs — freeze cursor-agent trace outputs into a durable
// baseline so trace-vs-cursor.mjs never has to invoke the cursor agent again.
//
// Cursor is a slow, paid external agent and its quality on these tasks is already
// established over many cached runs. This harvester reads the cursor records from
// prior bench result dirs (raw.json + the run's out.md), selects a representative
// median-centered set per task, and writes them under cursor-baseline/ with a
// manifest carrying each sample's measured wallMs and the judge score it earned
// at run time. The bench reuses that frozen judge score when the judge signature
// is unchanged, and re-judges the stored text only if the judge changed.
//
// Usage:
//   node build-cursor-baseline.mjs [--sources '<glob1>,<glob2>'] [--per-task N] [--min-composite N]
// Defaults: sources = /tmp/trace-vs-cursor-*/raw.json, per-task = 7, min-composite = 40.

import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { globSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { judgeSignature } from "./trace-vs-cursor.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT_DIR = path.join(HERE, "cursor-baseline");

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

function hashContent(s) {
  return createHash("sha256").update(String(s)).digest("hex").slice(0, 16);
}

// Collect every cursor record (with a readable out.md) across the source globs,
// grouped by task id. Each entry carries the measured wall time, the judge score
// it earned, the run's composite (used only for representative selection), and
// the markdown content.
function collectSamples(sources) {
  const byTask = new Map();
  const seenByTask = new Map(); // taskId -> Set(content hashes)
  for (const pattern of sources) {
    let files = [];
    try { files = globSync(pattern); } catch { files = []; }
    for (const rj of files) {
      let doc;
      try { doc = JSON.parse(readFileSync(rj, "utf8")); } catch { continue; }
      for (const r of doc.records || []) {
        if (r.arm !== "cursor" || !r.runDir) continue;
        const outPath = path.join(r.runDir, "out.md");
        if (!existsSync(outPath)) continue;
        let md;
        try { md = readFileSync(outPath, "utf8"); } catch { continue; }
        if (!md || md.trim().length < 200) continue;
        const tid = r.taskId;
        if (!tid) continue;
        const h = hashContent(md);
        if (!seenByTask.has(tid)) seenByTask.set(tid, new Set());
        if (seenByTask.get(tid).has(h)) continue; // dedup identical cursor output
        seenByTask.get(tid).add(h);
        if (!byTask.has(tid)) byTask.set(tid, []);
        byTask.get(tid).push({
          wallMs: Number(r.wallMs) || null,
          frozenJudge: Number.isFinite(r.judgeScore) ? r.judgeScore : null,
          sourceComposite: Number.isFinite(r.composite) ? r.composite : null,
          depth: r.depth || null,
          md,
          hash: h,
        });
      }
    }
  }
  return byTask;
}

// Pick a representative spread centered on the task's median composite, so the
// frozen cursor is a typical cursor, not a cherry-picked best or worst run.
function selectRepresentative(samples, perTask, minComposite) {
  const scored = samples.filter((s) => s.sourceComposite == null || s.sourceComposite >= minComposite);
  const pool = scored.length ? scored : samples;
  const withComp = pool.filter((s) => Number.isFinite(s.sourceComposite));
  if (withComp.length <= perTask) return pool.slice(0, Math.max(perTask, pool.length)).slice(0, perTask);
  const med = median(withComp.map((s) => s.sourceComposite));
  // Order by distance to the median composite, take the closest `perTask`, then
  // re-sort the kept set by composite ascending for stable, readable filenames.
  return [...withComp]
    .sort((a, b) => Math.abs(a.sourceComposite - med) - Math.abs(b.sourceComposite - med))
    .slice(0, perTask)
    .sort((a, b) => a.sourceComposite - b.sourceComposite);
}

function main() {
  const sources = (argValue("--sources", "/tmp/trace-vs-cursor-*/raw.json"))
    .split(",").map((s) => s.trim()).filter(Boolean);
  const perTask = Number(argValue("--per-task", "7"));
  const minComposite = Number(argValue("--min-composite", "40"));

  const byTask = collectSamples(sources);
  if (!byTask.size) {
    process.stderr.write(`no cursor samples found in sources: ${sources.join(", ")}\n`);
    process.exit(2);
  }

  // Rebuild the baseline dir from scratch so stale samples never linger.
  if (existsSync(OUT_DIR)) {
    for (const name of readdirSync(OUT_DIR)) {
      rmSync(path.join(OUT_DIR, name), { recursive: true, force: true });
    }
  }
  mkdirSync(OUT_DIR, { recursive: true });

  const manifestTasks = {};
  let totalSamples = 0;
  for (const [tid, samples] of [...byTask.entries()].sort()) {
    const kept = selectRepresentative(samples, perTask, minComposite);
    if (!kept.length) continue;
    const taskDir = path.join(OUT_DIR, tid);
    mkdirSync(taskDir, { recursive: true });
    const manifestSamples = [];
    kept.forEach((s, i) => {
      const file = `${tid}/${String(i + 1).padStart(2, "0")}.md`;
      writeFileSync(path.join(OUT_DIR, file), s.md);
      manifestSamples.push({
        file,
        wallMs: s.wallMs,
        frozenJudge: s.frozenJudge,
        sourceComposite: s.sourceComposite,
        contentHash: s.hash,
      });
      totalSamples += 1;
    });
    manifestTasks[tid] = {
      depth: kept[0].depth,
      poolSize: samples.length,
      samples: manifestSamples,
    };
  }

  const manifest = {
    generatedAt: new Date().toISOString(),
    judgeSignature: judgeSignature(),
    note: "Frozen cursor-agent trace outputs. trace-vs-cursor.mjs never invokes the cursor agent; it loads these samples, reuses each frozen judge score when judgeSignature matches the bench's current judge, and re-judges the stored markdown only if the judge changed. Regenerate with build-cursor-baseline.mjs when new cursor runs are captured.",
    perTask,
    minComposite,
    sources,
    tasks: manifestTasks,
  };
  writeFileSync(path.join(OUT_DIR, "manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`);
  process.stdout.write(
    `cursor baseline written: ${Object.keys(manifestTasks).length} tasks, ${totalSamples} samples -> ${OUT_DIR}\n`,
  );
}

main();
