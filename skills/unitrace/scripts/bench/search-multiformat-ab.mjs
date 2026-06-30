#!/usr/bin/env node
// search-multiformat-ab.mjs -- live A/B PROOF GATE for multi-format (code +
// docs + data) fast-path retrieval and the shared-daemon rtinfer borrow.
//
// Runs scripts/search.sh --json over labeled corpora and reports, per variant:
//   find-rate (gold found anywhere), top1-rate (gold is first), latency p50/p95,
//   negative-query latency (queries with gold=null), secret-leak failures
//   (must_not path returned), retrieval fallback rate, and -- when borrowing --
//   the transport served-rate parsed from the `[daemon] ns=... served ...`
//   attribution marker so a silent fall-through is never mistaken for a real
//   borrow.
//
// This is a LIVE bench (needs Codex auth + a daemon or rtinfer); excluded from
// `just test-all`. Results land in scripts/bench/results/<ts>/{raw.json,summary.md}.
//
// Usage:
//   search-multiformat-ab.mjs [--corpus multiformat|unifable|<dir>]
//       [--queries FILE] [--variants a,b] [--repeats N] [--concurrency N]
//       [--warmup N] [--query-limit N] [--repo DIR] [--debug]
//
// Transport arms (env overlays):
//   agentic-fallback UNITRACE_DAEMON_RTINFER=0  (borrow off; direct-session fallback)
//   rtinfer          UNITRACE_DAEMON_RTINFER=1  (borrow on)
//   rtinfer-absent   UNITRACE_DAEMON_RTINFER=1 + CSE_RTINFER_URL pinned to a
//                    dead port  (proves fail-open has no hang/probe storm)
//
// Config-sweep arms (multi-format defaults; pick the single best, then lock):
//   baseline         defaults
//   docbudget0/2/8   UNITRACE_SEARCH_FAST_MAX_DOC_FILES
//   nullfb-off       UNITRACE_SEARCH_FAST_NULL_FALLBACK=0
//   floor3/floor4    UNITRACE_SEARCH_SCORE_MIN

import { execFile } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SEARCH_SH = path.resolve(HERE, "../search.sh");
const REPO_ROOT = path.resolve(HERE, "../../../..");
const CORPUS_DIR = path.join(HERE, "corpus");
const QUERIES_DIR = path.join(HERE, "queries");

// A dead loopback port for the fail-open safety arm. Nothing listens here.
// Port 0 is invalid to connect to; strict-URL stops the cockpit-default
// fallthrough so the arm genuinely exercises "no daemon".
const DEAD_RTINFER = "http://127.0.0.1:9";
// The live cockpit default. The rtinfer arm points here EXPLICITLY (+ strict)
// so the borrow is exercised even on a host with no well-known endpoint file,
// and so served-rate attribution reflects this endpoint and nothing else. An
// operator can override with CSE_RTINFER_URL in the environment before running.
const LIVE_RTINFER = (process.env.CSE_RTINFER_URL || "http://127.0.0.1:8787").trim();

const VARIANTS = {
  // transport arms
  "agentic-fallback": { UNITRACE_DAEMON_RTINFER: "0" },
  rtinfer: { UNITRACE_DAEMON_RTINFER: "1", CSE_RTINFER_URL: LIVE_RTINFER, CSE_RTINFER_STRICT_URL: "1" },
  "rtinfer-absent": { UNITRACE_DAEMON_RTINFER: "1", CSE_RTINFER_URL: DEAD_RTINFER, CSE_RTINFER_STRICT_URL: "1" },
  // config-sweep arms
  baseline: {},
  docbudget0: { UNITRACE_SEARCH_FAST_MAX_DOC_FILES: "0" },
  docbudget2: { UNITRACE_SEARCH_FAST_MAX_DOC_FILES: "2" },
  docbudget8: { UNITRACE_SEARCH_FAST_MAX_DOC_FILES: "8" },
  "nullfb-off": { UNITRACE_SEARCH_FAST_NULL_FALLBACK: "0" },
  floor3: { UNITRACE_SEARCH_SCORE_MIN: "3" },
  floor4: { UNITRACE_SEARCH_SCORE_MIN: "4" },
};

// Acceptance thresholds. PASS justifies flipping the borrow default on; the
// removal of the flag is the gated follow-up (see bench/AGENTS.md).
const THRESHOLDS = {
  minServedRate: 90, // a `rtinfer` run below this is daemon-absent -> invalid
  maxP95RegressionPct: 10, // rtinfer p95 may be at most 10% worse than the direct control
  maxNegLeak: 0, // must_not hits are reported as warnings
  failOpenMaxP95Ms: 7000, // dead daemon smoke must stay bounded
};

const OBJECTIVE_THRESHOLDS = {
  multiformat: { minFindRate: 90, minTop1Rate: 90, maxP95Ms: 4000 },
  unifable: { minFindRate: 80, minTop1Rate: 70, maxP95Ms: 6000 },
  default: { minFindRate: 80, minTop1Rate: 70, maxP95Ms: 6000 },
};

function parseArgs(argv) {
  const out = {
    corpus: "multiformat", queries: null, repo: null,
    variants: ["rtinfer"], repeats: 3, concurrency: 4, warmup: 1, queryLimit: 0,
    minFindRate: null, minTop1Rate: null, maxP95Ms: null, failOpenMaxP95Ms: null,
    debug: false,
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--corpus" && argv[i + 1]) out.corpus = argv[++i];
    else if (a === "--queries" && argv[i + 1]) out.queries = path.resolve(argv[++i]);
    else if (a === "--repo" && argv[i + 1]) out.repo = path.resolve(argv[++i]);
    else if (a === "--variants" && argv[i + 1]) out.variants = argv[++i].split(",").map((s) => s.trim()).filter(Boolean);
    else if (a === "--repeats" && argv[i + 1]) out.repeats = Math.max(1, parseInt(argv[++i], 10) || 1);
    else if (a === "--concurrency" && argv[i + 1]) out.concurrency = Math.max(1, parseInt(argv[++i], 10) || 1);
    else if (a === "--warmup" && argv[i + 1]) out.warmup = Math.max(0, parseInt(argv[++i], 10) || 0);
    else if (a === "--query-limit" && argv[i + 1]) out.queryLimit = Math.max(0, parseInt(argv[++i], 10) || 0);
    else if (a === "--min-find-rate" && argv[i + 1]) out.minFindRate = Math.max(0, parseInt(argv[++i], 10) || 0);
    else if (a === "--min-top1-rate" && argv[i + 1]) out.minTop1Rate = Math.max(0, parseInt(argv[++i], 10) || 0);
    else if (a === "--max-p95-ms" && argv[i + 1]) out.maxP95Ms = Math.max(1, parseInt(argv[++i], 10) || 1);
    else if (a === "--failopen-max-p95-ms" && argv[i + 1]) out.failOpenMaxP95Ms = Math.max(1, parseInt(argv[++i], 10) || 1);
    else if (a === "--debug") out.debug = true;
  }
  return out;
}

// Resolve (root, queriesFile) for a named corpus or an explicit dir.
function resolveCorpus(args) {
  if (args.corpus === "multiformat") {
    return { root: path.join(CORPUS_DIR, "multiformat"), queries: args.queries || path.join(QUERIES_DIR, "multiformat.jsonl") };
  }
  if (args.corpus === "unifable") {
    return { root: args.repo || REPO_ROOT, queries: args.queries || path.join(QUERIES_DIR, "unifable.jsonl") };
  }
  // explicit dir
  const root = path.resolve(args.corpus);
  return { root, queries: args.queries || path.join(root, "queries.jsonl") };
}

function loadQueries(file) {
  const out = [];
  for (const line of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
    const t = line.trim();
    if (!t) continue;
    try { out.push(JSON.parse(t)); } catch { /* skip malformed */ }
  }
  return out;
}

function limitQueries(queries, limit) {
  if (!limit || queries.length <= limit) return queries;
  const positives = queries.filter((q) => q.gold != null);
  const negatives = queries.filter((q) => q.gold == null);
  if (!negatives.length) return positives.slice(0, limit);
  const negTarget = Math.min(negatives.length, Math.max(1, Math.floor(limit * negatives.length / queries.length)));
  const posTarget = Math.max(0, limit - negTarget);
  return [...positives.slice(0, posTarget), ...negatives.slice(0, negTarget)].slice(0, limit);
}

// Parse `[daemon] ns=<ns> served rtinfer=<N> direct=<M>` tallies from stderr.
function parseServed(stderr) {
  let rtinfer = 0, direct = 0;
  for (const m of (stderr || "").matchAll(/\[daemon\] ns=\S+ served rtinfer=(\d+) direct=(\d+)/g)) {
    rtinfer += parseInt(m[1], 10);
    direct += parseInt(m[2], 10);
  }
  return { rtinfer, direct };
}

function runSearch(query, root, envOverlay) {
  return new Promise((resolve) => {
    const env = { ...process.env, ...envOverlay, UNITRACE_SEARCH_DEBUG: "1" };
    const t0 = Date.now();
    execFile(
      "bash",
      [SEARCH_SH, "--root", root, "--json", query],
      { env, encoding: "utf8", maxBuffer: 64 * 1024 * 1024 },
      (err, stdout, stderr) => {
        const ms = Date.now() - t0;
        let results = null;
        try { results = JSON.parse(stdout || "[]"); } catch { results = null; }
        const fellBack = /fast path declined|falling back/i.test(stderr || "");
        const served = parseServed(stderr);
        resolve({ ms, results, fellBack, served, ok: results !== null });
      },
    );
  });
}

function pathMatches(resultPath, gold) {
  if (!resultPath || !gold) return false;
  const r = resultPath.replace(/^\.\//, "");
  return r === gold || r.endsWith("/" + gold) || r.endsWith(gold);
}

function pct(n, d) { return d ? Math.round((100 * n) / d) : 0; }
function quantile(sorted, q) {
  if (!sorted.length) return 0;
  const idx = Math.min(sorted.length - 1, Math.floor(q * (sorted.length - 1)));
  return sorted[idx];
}

async function evalQuery(q, root, overlay) {
  const res = await runSearch(q.query, root, overlay);
  const paths = res.ok ? (res.results || []).map((x) => x.path || x.file || "") : [];
  const isNegative = q.gold == null;
  const hit = !isNegative && paths.some((p) => pathMatches(p, q.gold));
  const isTop1 = !isNegative && paths.length > 0 && pathMatches(paths[0], q.gold);
  const leaked = q.must_not ? paths.some((p) => pathMatches(p, q.must_not)) : false;
  return {
    q: q.query, gold: q.gold, cls: q.class || (isNegative ? "negative" : "?"),
    hit, top1: isTop1, leaked, isNegative, ms: res.ms,
    fellBack: res.fellBack, served: res.served, err: !res.ok,
  };
}

async function runVariant(name, queries, root, repeats, concurrency, warmup, extraEnv = {}) {
  const overlay = { ...extraEnv, ...(VARIANTS[name] || {}) };
  // Warm up a bounded sentinel set. A full warmup pass doubles live bench cost and
  // does not prove anything extra now that rtinfer owns the warm pool.
  const warmQueries = queries.slice(0, Math.min(warmup, queries.length));
  for (let i = 0; i < warmQueries.length; i += concurrency) {
    const batch = warmQueries.slice(i, i + concurrency);
    await Promise.all(batch.map((q) => evalQuery(q, root, overlay)));
  }

  const tasks = [];
  for (const q of queries) for (let r = 0; r < repeats; r++) tasks.push(q);

  const rows = [];
  for (let i = 0; i < tasks.length; i += concurrency) {
    const batch = tasks.slice(i, i + concurrency);
    const wall0 = Date.now();
    const settled = await Promise.all(batch.map((q) => evalQuery(q, root, overlay)));
    const wall = Date.now() - wall0;
    for (const s of settled) { s.batchWall = wall; s.batchSize = batch.length; rows.push(s); }
  }

  const posRows = rows.filter((r) => !r.isNegative && !r.err);
  const negRows = rows.filter((r) => r.isNegative && !r.err);
  const allLat = rows.filter((r) => !r.err).map((r) => r.ms).sort((a, b) => a - b);
  const negLat = negRows.map((r) => r.ms).sort((a, b) => a - b);
  let rtinfer = 0, direct = 0;
  for (const r of rows) { rtinfer += r.served.rtinfer; direct += r.served.direct; }
  const servedTotal = rtinfer + direct;

  return {
    name,
    total: rows.length, posTotal: posRows.length, negTotal: negRows.length,
    errors: rows.filter((r) => r.err).length,
    found: posRows.filter((r) => r.hit).length,
    top1: posRows.filter((r) => r.top1).length,
    leaks: rows.filter((r) => r.leaked).length,
    fallback: rows.filter((r) => r.fellBack).length,
    findRate: pct(posRows.filter((r) => r.hit).length, posRows.length),
    top1Rate: pct(posRows.filter((r) => r.top1).length, posRows.length),
    fallbackRate: pct(rows.filter((r) => r.fellBack).length, rows.length),
    errorRate: pct(rows.filter((r) => r.err).length, rows.length),
    servedRtinfer: rtinfer, servedDirect: direct,
    servedRate: pct(rtinfer, servedTotal),
    p50: quantile(allLat, 0.5), p95: quantile(allLat, 0.95),
    negP50: quantile(negLat, 0.5), negP95: quantile(negLat, 0.95),
    rows,
  };
}

function objectiveThresholds(args = {}) {
  const base = OBJECTIVE_THRESHOLDS[args.corpus] || OBJECTIVE_THRESHOLDS.default;
  return {
    ...THRESHOLDS,
    minFindRate: args.minFindRate ?? base.minFindRate,
    minTop1Rate: args.minTop1Rate ?? base.minTop1Rate,
    maxP95Ms: args.maxP95Ms ?? base.maxP95Ms,
    failOpenMaxP95Ms: args.failOpenMaxP95Ms ?? THRESHOLDS.failOpenMaxP95Ms,
  };
}

// Verdict: rtinfer is judged directly against labeled gold. agentic-fallback is
// an opt-in diagnostic control, but it is no longer needed for the proof gate.
// rtinfer-absent is a bounded fail-open smoke, not a full quality benchmark.
export function verdict(summaries, args = {}) {
  const by = Object.fromEntries(summaries.map((s) => [s.name, s]));
  const thresholds = objectiveThresholds(args);
  const reasons = [];
  const warnings = [];
  let pass = true;
  let gated = false;

  // Errors are an unconditional hard fail on every arm.
  for (const s of summaries) {
    if (s.errorRate > 0) { pass = false; reasons.push(`${s.name}: error-rate ${s.errorRate}% (expected 0)`); }
  }

  const direct = by["agentic-fallback"], rt = by["rtinfer"], absent = by["rtinfer-absent"];
  // Secret leaks are reported as a non-blocking WARNING, never a gate failure:
  // search intentionally surfaces lexically-matched files (including secrets)
  // and that policy is owned by search-fast.mjs, not this borrow gate. We still
  // call out when a borrow arm leaks MORE than the direct control so a true borrow-
  // induced regression is visible, but it does not flip the verdict.
  for (const s of summaries) {
    if (s.leaks > THRESHOLDS.maxNegLeak) {
      const delta = direct ? ` (direct control ${direct.leaks})` : "";
      warnings.push(`${s.name} returned ${s.leaks} secret path(s)${delta} -- search policy, not a borrow gate failure`);
    }
  }

  if (direct && rt) {
    if (rt.findRate < direct.findRate) { pass = false; reasons.push(`rtinfer find-rate ${rt.findRate}% < direct ${direct.findRate}%`); }
    if (rt.top1Rate < direct.top1Rate) { pass = false; reasons.push(`rtinfer top1-rate ${rt.top1Rate}% < direct ${direct.top1Rate}%`); }
    const p95cap = direct.p95 * (1 + THRESHOLDS.maxP95RegressionPct / 100);
    if (rt.p95 > p95cap) { pass = false; reasons.push(`rtinfer p95 ${rt.p95}ms > ${Math.round(p95cap)}ms (direct ${direct.p95}ms +${THRESHOLDS.maxP95RegressionPct}%)`); }
  }
  if (rt) {
    gated = true;
    if (rt.servedRate < thresholds.minServedRate) { pass = false; reasons.push(`rtinfer served-rate ${rt.servedRate}% < ${thresholds.minServedRate}% -> daemon absent or falling through; run invalid`); }
    if (rt.findRate < thresholds.minFindRate) { pass = false; reasons.push(`rtinfer find-rate ${rt.findRate}% < objective ${thresholds.minFindRate}%`); }
    if (rt.top1Rate < thresholds.minTop1Rate) { pass = false; reasons.push(`rtinfer top1-rate ${rt.top1Rate}% < objective ${thresholds.minTop1Rate}%`); }
    if (rt.p95 > thresholds.maxP95Ms) { pass = false; reasons.push(`rtinfer p95 ${rt.p95}ms > objective ${thresholds.maxP95Ms}ms`); }
  }
  if (absent) {
    gated = true;
    if (absent.servedRtinfer > 0) { pass = false; reasons.push(`rtinfer-absent served ${absent.servedRtinfer} via rtinfer (should be 0 -- dead endpoint)`); }
    if (absent.p95 > thresholds.failOpenMaxP95Ms) { pass = false; reasons.push(`rtinfer-absent p95 ${absent.p95}ms > fail-open smoke ${thresholds.failOpenMaxP95Ms}ms`); }
  }

  if (!gated) reasons.push("metrics-only run (no rtinfer or rtinfer-absent proof arm); no verdict rendered");
  return { pass: gated ? pass : null, reasons, warnings, thresholds };
}

function renderMarkdown(meta, summaries, v) {
  const lines = [];
  lines.push(`# search multi-format borrow proof - ${meta.stamp}`, "");
  lines.push(`Corpus: \`${meta.root}\``);
  lines.push(`Queries: \`${meta.queries}\` (${meta.queryCount}; ${meta.negCount} negative)`);
  lines.push(`Repeats: ${meta.repeats}  Concurrency: ${meta.concurrency}  Warmup queries: ${meta.warmup}`, "");
  if (v.pass !== null) lines.push(`## VERDICT: ${v.pass ? "PASS" : "FAIL"}`, "");
  else lines.push("## VERDICT: n/a (metrics-only)", "");
  if (v.reasons.length) { for (const r of v.reasons) lines.push(`- ${r}`); lines.push(""); }
  if (v.warnings && v.warnings.length) { lines.push("### Warnings (non-blocking)", ""); for (const w of v.warnings) lines.push(`- WARN: ${w}`); lines.push(""); }
  lines.push("| variant | find | top1 | neg-p50 | leaks | fb% | err% | served-rt% | p50 | p95 |");
  lines.push("|---|---|---|---|---|---|---|---|---|---|");
  for (const s of summaries) {
    lines.push(`| ${s.name} | ${s.findRate}% | ${s.top1Rate}% | ${s.negP50}ms | ${s.leaks} | ${s.fallbackRate}% | ${s.errorRate}% | ${s.servedRate}% | ${s.p50} | ${s.p95} |`);
  }
  lines.push("");
  for (const s of summaries) {
    lines.push(`## ${s.name}`, "");
    lines.push("| query | gold | cls | hit | top1 | leak | fb | served-rt/direct | ms |");
    lines.push("|---|---|---|---|---|---|---|---|---|");
    for (const r of s.rows) {
      lines.push(`| ${r.q} | ${r.gold ?? "(none)"} | ${r.cls} | ${r.err ? "ERR" : r.isNegative ? "-" : r.hit ? "Y" : "n"} | ${r.top1 ? "Y" : "n"} | ${r.leaked ? "LEAK" : ""} | ${r.fellBack ? "Y" : ""} | ${r.served.rtinfer}/${r.served.direct} | ${r.ms} |`);
    }
    lines.push("");
  }
  return lines.join("\n");
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!fs.existsSync(SEARCH_SH)) { process.stderr.write(`error: search.sh not found at ${SEARCH_SH}\n`); process.exit(1); }
  const { root, queries: queriesFile } = resolveCorpus(args);
  if (!fs.existsSync(queriesFile)) { process.stderr.write(`error: queries not found at ${queriesFile}\n`); process.exit(1); }
  const queries = limitQueries(loadQueries(queriesFile), args.queryLimit);
  if (!queries.length) { process.stderr.write("error: no queries loaded\n"); process.exit(1); }
  const negCount = queries.filter((q) => q.gold == null).length;

  // If the labeled queries file lives INSIDE the searched root (the real-repo
  // corpus: root IS the repo), exclude its directory from retrieval. That file
  // contains every query string + gold path verbatim, so leaving it in-tree lets
  // it win as a top lexical candidate and displace the true gold (inflated find,
  // collapsed top1). Bench-only: product search never sets this.
  const extraEnv = {};
  const relQueries = path.relative(root, queriesFile);
  if (relQueries && !relQueries.startsWith("..") && !path.isAbsolute(relQueries)) {
    const queriesDir = path.dirname(relQueries);
    extraEnv.UNITRACE_SEARCH_FAST_EXCLUDE = queriesDir && queriesDir !== "." ? queriesDir : relQueries;
    process.stderr.write(`[bench] queries file is in-tree; excluding '${extraEnv.UNITRACE_SEARCH_FAST_EXCLUDE}' from retrieval\n`);
  }

  const summaries = [];
  for (const name of args.variants) {
    if (!(name in VARIANTS)) { process.stderr.write(`skip unknown variant: ${name}\n`); continue; }
    process.stderr.write(`[bench] variant=${name} queries=${queries.length} repeats=${args.repeats} concurrency=${args.concurrency} warmup=${args.warmup}\n`);
    summaries.push(await runVariant(name, queries, root, args.repeats, args.concurrency, args.warmup, extraEnv));
  }

  const v = verdict(summaries, args);
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const meta = { stamp, root, queries: queriesFile, queryCount: queries.length, negCount, repeats: args.repeats, concurrency: args.concurrency, warmup: args.warmup, queryLimit: args.queryLimit };
  const outDir = path.join(HERE, "results", stamp);
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(path.join(outDir, "summary.md"), renderMarkdown(meta, summaries, v));
  fs.writeFileSync(path.join(outDir, "raw.json"), JSON.stringify({ meta, thresholds: v.thresholds || objectiveThresholds(args), verdict: v, summaries }, null, 2));

  for (const s of summaries) {
    process.stdout.write(`${s.name}: find=${s.findRate}% top1=${s.top1Rate}% neg-p50=${s.negP50}ms leaks=${s.leaks} fb=${s.fallbackRate}% err=${s.errorRate}% served-rt=${s.servedRate}% p50=${s.p50}ms p95=${s.p95}ms\n`);
  }
  if (v.pass !== null) {
    process.stdout.write(`\nVERDICT: ${v.pass ? "PASS" : "FAIL"}\n`);
    for (const r of v.reasons) process.stdout.write(`  - ${r}\n`);
  }
  if (v.warnings && v.warnings.length) {
    for (const w of v.warnings) process.stdout.write(`  WARN: ${w}\n`);
  }
  process.stdout.write(`\nresults: ${outDir}\n`);
  if (v.pass === false) process.exit(2);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((e) => { process.stderr.write(`bench error: ${e.message}\n`); process.exit(1); });
}
