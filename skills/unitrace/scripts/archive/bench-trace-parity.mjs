#!/usr/bin/env node
// bench-trace-parity.mjs — deterministic gate for Gemini-vs-Cursor trace parity.
//
// Measures, on the newest benchmarks/*-trace-gm* directory:
//   - raw <file:path:start-end> token count per run (median per transport)
//   - unique paths per run (median per transport)
//   - citation validity: cited path exists under workspace AND span within file line bounds
//   - wire lint: each raw is wire-clean (4 SECTION blocks, no markdown fences/headings/tables)
//   - wall_s median per transport (from results.tsv)
//
// Modes:
//   quality  -> substance parity: tokens >= 0.65x cursor (depth band, accepted tradeoff),
//               paths >= 0.85x cursor (breadth), lint clean, >=95% valid spans
//   speed    -> assert gemini median wall_s < cursor median wall_s
//   report   -> print full report, exit 0 always
//
// Exit 0 if the requested mode's assertions hold, else 1.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  isExploreWireFormat,
  lintExploreWire,
  sectionScoreWire,
  TRACE_SECTIONS,
} from "./lib/explore-wire-format.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SKILL_DIR = path.resolve(__dirname, "..");
const BENCH_ROOT = path.join(SKILL_DIR, "benchmarks");

const TOKEN_RE = /<file:([^:>]+):(\d+)-(\d+)>/g;

function parseArgs(argv) {
  const a = { mode: null, bench: null };
  for (const arg of argv) {
    if (arg === "quality" || arg === "speed" || arg === "report") a.mode = arg;
    else if (arg.startsWith("--bench=")) a.bench = arg.slice(8);
    else if (arg === "--help" || arg === "-h") a.help = true;
  }
  return a;
}

function median(xs) {
  if (!xs.length) return 0;
  const s = [...xs].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

function newestBenchDir() {
  const dirs = fs
    .readdirSync(BENCH_ROOT, { withFileTypes: true })
    .filter((d) => d.isDirectory() && /-trace-gm/.test(d.name))
    .map((d) => ({ name: d.name, mtime: fs.statSync(path.join(BENCH_ROOT, d.name)).mtimeMs }))
    .sort((a, b) => b.mtime - a.mtime);
  return dirs.length ? path.join(BENCH_ROOT, dirs[0].name) : null;
}

function rawFilesFor(benchDir, transport) {
  const out = [];
  const tops = fs.readdirSync(benchDir, { withFileTypes: true }).filter((d) => d.isDirectory());
  for (const top of tops) {
    if (!top.name.startsWith(`${transport}-`)) continue;
    const runsDir = path.join(benchDir, top.name, "runs");
    if (!fs.existsSync(runsDir)) continue;
    const runDirs = fs.readdirSync(runsDir, { withFileTypes: true }).filter((d) => d.isDirectory());
    for (const rd of runDirs) {
      const raw = path.join(runsDir, rd.name, "raw");
      if (fs.existsSync(raw) && fs.statSync(raw).size > 0) out.push({ run: top.name, raw });
    }
  }
  return out;
}

function readRaw(p) {
  try {
    return fs.readFileSync(p, "utf8");
  } catch {
    return "";
  }
}

function tokenCount(text) {
  let n = 0;
  for (const _ of text.matchAll(TOKEN_RE)) n += 1;
  return n;
}

function uniquePaths(text) {
  const set = new Set();
  for (const m of text.matchAll(TOKEN_RE)) set.add(m[1]);
  return set.size;
}

function validRatio(text, workspace) {
  let total = 0;
  let valid = 0;
  const cache = new Map();
  for (const m of text.matchAll(TOKEN_RE)) {
    const p = m[1];
    const s = Number(m[2]);
    const e = Number(m[3]);
    total += 1;
    const abs = path.join(workspace, p);
    let lines = cache.get(abs);
    if (lines === undefined) {
      if (fs.existsSync(abs) && fs.statSync(abs).isFile()) {
        lines = fs.readFileSync(abs, "utf8").split("\n").length;
      } else {
        lines = -1;
      }
      cache.set(abs, lines);
    }
    if (lines >= 0 && s >= 1 && e >= s && e <= lines) valid += 1;
  }
  return total ? valid / total : 0;
}

// Wire lint clean: detected as wire format, no markdown (lintExploreWire), all 4 required sections present.
function wireLintClean(text) {
  if (!isExploreWireFormat(text)) return false;
  if (!lintExploreWire(text).ok) return false;
  return sectionScoreWire(text, TRACE_SECTIONS) >= TRACE_SECTIONS.length;
}

function readWalls(benchDir) {
  const tsv = path.join(benchDir, "results.tsv");
  const walls = { gemini: [], cursor: [] };
  if (!fs.existsSync(tsv)) return walls;
  const lines = fs.readFileSync(tsv, "utf8").split("\n").filter(Boolean);
  for (const line of lines.slice(1)) {
    const parts = line.split("\t");
    const transport = parts[0];
    const wall = Number(parts[1]);
    if ((transport === "gemini" || transport === "cursor") && Number.isFinite(wall)) {
      walls[transport].push(wall);
    }
  }
  return walls;
}

function measure(benchDir, workspace) {
  const out = {};
  for (const t of ["gemini", "cursor"]) {
    const files = rawFilesFor(benchDir, t);
    const perRun = files.map(({ run, raw }) => {
      const text = readRaw(raw);
      return {
        run,
        tokens: tokenCount(text),
        paths: uniquePaths(text),
        valid: validRatio(text, workspace),
        lint: wireLintClean(text),
        bytes: Buffer.byteLength(text, "utf8"),
      };
    });
    out[t] = {
      n: perRun.length,
      perRun,
      medianTokens: median(perRun.map((r) => r.tokens)),
      medianPaths: median(perRun.map((r) => r.paths)),
      minValid: perRun.length ? Math.min(...perRun.map((r) => r.valid)) : 0,
      lintClean: perRun.filter((r) => r.lint).length,
    };
  }
  const walls = readWalls(benchDir);
  out.gemini.medianWall = median(walls.gemini);
  out.cursor.medianWall = median(walls.cursor);
  return out;
}

function fmt(n) {
  return Number.isInteger(n) ? String(n) : n.toFixed(2);
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help || !args.mode) {
    process.stderr.write("usage: bench-trace-parity.mjs quality|speed|report [--bench=DIR]\n");
    process.exit(args.help ? 0 : 2);
  }
  const benchDir = args.bench ? path.resolve(args.bench) : newestBenchDir();
  if (!benchDir || !fs.existsSync(benchDir)) {
    process.stderr.write(`no bench dir found under ${BENCH_ROOT}\n`);
    process.exit(1);
  }
  const m = measure(benchDir, SKILL_DIR);
  const g = m.gemini;
  const c = m.cursor;

  const lines = [];
  lines.push(`bench: ${path.relative(SKILL_DIR, benchDir)}`);
  lines.push("");
  lines.push(
    [
      "transport".padEnd(9),
      "n".padStart(3),
      "tok(med)".padStart(9),
      "paths(med)".padStart(11),
      "minValid".padStart(9),
      "lint".padStart(7),
      "wall(med)".padStart(10),
    ].join(" "),
  );
  for (const [t, name] of [
    ["gemini", "gemini"],
    ["cursor", "cursor"],
  ]) {
    const x = m[t];
    lines.push(
      [
        name.padEnd(9),
        String(x.n).padStart(3),
        fmt(x.medianTokens).padStart(9),
        fmt(x.medianPaths).padStart(11),
        `${Math.round(x.minValid * 100)}%`.padStart(8),
        `${x.lintClean}/${x.n}`.padStart(7),
        `${fmt(x.medianWall)}s`.padStart(9),
      ].join(" "),
    );
  }
  lines.push("");

  // Parity per the agreed definition (user chose flash-lite 1-pass): substance parity =
  // breadth (paths) + validity + wire cleanliness, beating cursor on speed, with span depth
  // accepted at a measured ~77% of cursor (single-pass flash-lite ceiling; prompt-exhausted).
  // Tokens use a 0.65x band so cursor's high run-to-run variance (30-48) doesn't flunk a
  // deliberately-accepted tradeoff; paths use 0.85x for the same reason.
  const checks = {
    tokens: c.medianTokens > 0 && g.medianTokens >= 0.65 * c.medianTokens,
    paths: c.medianPaths > 0 && g.medianPaths >= 0.85 * c.medianPaths,
    lint: g.n > 0 && g.lintClean === g.n,
    validity: g.minValid >= 0.95,
    speed: g.medianWall < c.medianWall && c.medianWall > 0,
  };
  const show = {
    tokens: `gemini=${fmt(g.medianTokens)} cursor=${fmt(c.medianTokens)} (${Math.round((g.medianTokens / Math.max(c.medianTokens, 1)) * 100)}% of cursor; band >= 65%)`,
    paths: `gemini=${fmt(g.medianPaths)} cursor=${fmt(c.medianPaths)} (band >= ${Math.ceil(0.85 * c.medianPaths)})`,
    lint: `gemini=${g.lintClean}/${g.n}`,
    validity: `gemini minValid=${Math.round(g.minValid * 100)}% (need >= 95%)`,
    speed: `gemini=${fmt(g.medianWall)}s cursor=${fmt(c.medianWall)}s`,
  };
  for (const [k, v] of Object.entries(checks)) {
    lines.push(`${v ? "PASS" : "FAIL"}  ${k}: ${show[k]}`);
  }

  process.stdout.write(lines.join("\n") + "\n");

  if (args.mode === "report") process.exit(0);
  if (args.mode === "quality") {
    process.exit(checks.tokens && checks.paths && checks.lint && checks.validity ? 0 : 1);
  }
  if (args.mode === "speed") process.exit(checks.speed ? 0 : 1);
}

const isMain = process.argv[1] === fileURLToPath(import.meta.url);
if (isMain) main();
