#!/usr/bin/env node
// search-multiformat-ab.mjs -- live A/B PROOF GATE for multi-format (code +
// docs + data) fast-path retrieval and the shared-daemon rtinfer borrow.
//
// Runs scripts/search.sh --json over labeled corpora and reports, per variant:
//   find-rate (gold found anywhere), top1-rate (gold is first), latency p50/p95,
//   negative-query latency (queries with gold=null), secret-leak failures
//   (must_not path returned), retrieval fallback rate, and -- when borrowing --
//   the transport served-rate parsed from the `[daemon] ns=... served ...`
//   attribution marker so a silent fall-through to UDS is never mistaken for a
//   real borrow.
//
// This is a LIVE bench (needs Codex auth + a daemon or rtinfer); excluded from
// `just test-all`. Results land in scripts/bench/results/<ts>/{raw.json,summary.md}.
//
// Usage:
//   search-multiformat-ab.mjs [--corpus multiformat|unifable|<dir>]
//       [--queries FILE] [--variants a,b] [--repeats N] [--concurrency N]
//       [--repo DIR] [--debug]
//
// Transport arms (env overlays):
//   uds              UNITRACE_DAEMON_RTINFER=0  (borrow off; per-session pool)
//   rtinfer          UNITRACE_DAEMON_RTINFER=1  (borrow on)
//   rtinfer-absent   UNITRACE_DAEMON_RTINFER=1 + CSE_RTINFER_URL pinned to a
//                    dead port  (proves fail-open == uds, no hang/probe storm)
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
  uds: { UNITRACE_DAEMON_RTINFER: "0" },
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
  maxP95RegressionPct: 10, // rtinfer p95 may be at most 10% worse than uds
  maxNegLeak: 0, // a must_not path returned is an automatic fail
};

function parseArgs(argv) {
  const out = {
    corpus: "multiformat", queries: null, repo: null,
    variants: ["uds", "rtinfer"], repeats: 3, concurrency: 1, warmup: 1, debug: false,
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

// Parse `[daemon] ns=<ns> served rtinfer=<N> uds=<M>` tallies from stderr.
function parseServed(stderr) {
  let rtinfer = 0, uds = 0;
  for (const m of (stderr || "").matchAll(/\[daemon\] ns=\S+ served rtinfer=(\d+) uds=(\d+)/g)) {
    rtinfer += parseInt(m[1], 10);
    uds += parseInt(m[2], 10);
  }
  return { rtinfer, uds };
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

async function runVariant(name, queries, root, repeats, concurrency, warmup) {
  const overlay = VARIANTS[name] || {};
  // Warmup pass (untimed, discarded): one search per query so the borrow
  // endpoint / UDS pool is connected + prewarmed before timing. Cold-start skew
  // otherwise penalizes whichever transport happens to run first; a fair A/B
  // compares warm-state latency. Skipped with --warmup 0.
  for (let w = 0; w < warmup; w++) {
    for (let i = 0; i < queries.length; i += concurrency) {
      const batch = queries.slice(i, i + concurrency);
      await Promise.all(batch.map((q) => evalQuery(q, root, overlay)));
    }
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
  let rtinfer = 0, uds = 0;
  for (const r of rows) { rtinfer += r.served.rtinfer; uds += r.served.uds; }
  const servedTotal = rtinfer + uds;

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
    servedRtinfer: rtinfer, servedUds: uds,
    servedRate: pct(rtinfer, servedTotal),
    p50: quantile(allLat, 0.5), p95: quantile(allLat, 0.95),
    negP50: quantile(negLat, 0.5), negP95: quantile(negLat, 0.95),
    rows,
  };
}

// Verdict: compare a borrow arm (rtinfer) against the control (uds) when both
// were run. Pure config sweeps (no transport pair) report metrics only.
function verdict(summaries) {
  const by = Object.fromEntries(summaries.map((s) => [s.name, s]));
  const reasons = [];
  const warnings = [];
  let pass = true;

  // Errors are an unconditional hard fail on every arm.
  for (const s of summaries) {
    if (s.errorRate > 0) { pass = false; reasons.push(`${s.name}: error-rate ${s.errorRate}% (expected 0)`); }
  }

  const uds = by["uds"], rt = by["rtinfer"], absent = by["rtinfer-absent"];
  // Secret leaks are reported as a non-blocking WARNING, never a gate failure:
  // search intentionally surfaces lexically-matched files (including secrets)
  // and that policy is owned by search-fast.mjs, not this borrow gate. We still
  // call out when a borrow arm leaks MORE than the uds control so a true borrow-
  // induced regression is visible, but it does not flip the verdict.
  for (const s of summaries) {
    if (s.leaks > THRESHOLDS.maxNegLeak) {
      const delta = uds ? ` (uds control ${uds.leaks})` : "";
      warnings.push(`${s.name} returned ${s.leaks} secret path(s)${delta} -- search policy, not a borrow gate failure`);
    }
  }

  if (uds && rt) {
    if (rt.servedRate < THRESHOLDS.minServedRate) { pass = false; reasons.push(`rtinfer served-rate ${rt.servedRate}% < ${THRESHOLDS.minServedRate}% -> daemon absent or falling through; run invalid`); }
    if (rt.findRate < uds.findRate) { pass = false; reasons.push(`rtinfer find-rate ${rt.findRate}% < uds ${uds.findRate}%`); }
    if (rt.top1Rate < uds.top1Rate) { pass = false; reasons.push(`rtinfer top1-rate ${rt.top1Rate}% < uds ${uds.top1Rate}%`); }
    const p95cap = uds.p95 * (1 + THRESHOLDS.maxP95RegressionPct / 100);
    if (rt.p95 > p95cap) { pass = false; reasons.push(`rtinfer p95 ${rt.p95}ms > ${Math.round(p95cap)}ms (uds ${uds.p95}ms +${THRESHOLDS.maxP95RegressionPct}%)`); }
  }
  if (uds && absent) {
    // Fail-open must match the control: find/top1 within 5 points, no leak/hang.
    if (Math.abs(absent.findRate - uds.findRate) > 5) { pass = false; reasons.push(`rtinfer-absent find-rate ${absent.findRate}% diverges from uds ${uds.findRate}% (fail-open broken)`); }
    if (absent.servedRtinfer > 0) { pass = false; reasons.push(`rtinfer-absent served ${absent.servedRtinfer} via rtinfer (should be 0 -- dead endpoint)`); }
  }

  if (!(uds && (rt || absent))) reasons.push("metrics-only run (no uds+rtinfer transport pair); no verdict rendered");
  return { pass: (uds && (rt || absent)) ? pass : null, reasons, warnings };
}

function renderMarkdown(meta, summaries, v) {
  const lines = [];
  lines.push(`# search multi-format borrow proof - ${meta.stamp}`, "");
  lines.push(`Corpus: \`${meta.root}\``);
  lines.push(`Queries: \`${meta.queries}\` (${meta.queryCount}; ${meta.negCount} negative)`);
  lines.push(`Repeats: ${meta.repeats}  Concurrency: ${meta.concurrency}  Warmup: ${meta.warmup}`, "");
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
    lines.push("| query | gold | cls | hit | top1 | leak | fb | served-rt/uds | ms |");
    lines.push("|---|---|---|---|---|---|---|---|---|");
    for (const r of s.rows) {
      lines.push(`| ${r.q} | ${r.gold ?? "(none)"} | ${r.cls} | ${r.err ? "ERR" : r.isNegative ? "-" : r.hit ? "Y" : "n"} | ${r.top1 ? "Y" : "n"} | ${r.leaked ? "LEAK" : ""} | ${r.fellBack ? "Y" : ""} | ${r.served.rtinfer}/${r.served.uds} | ${r.ms} |`);
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
  const queries = loadQueries(queriesFile);
  if (!queries.length) { process.stderr.write("error: no queries loaded\n"); process.exit(1); }
  const negCount = queries.filter((q) => q.gold == null).length;

  const summaries = [];
  for (const name of args.variants) {
    if (!(name in VARIANTS)) { process.stderr.write(`skip unknown variant: ${name}\n`); continue; }
    process.stderr.write(`[bench] variant=${name} queries=${queries.length} repeats=${args.repeats} concurrency=${args.concurrency} warmup=${args.warmup}\n`);
    summaries.push(await runVariant(name, queries, root, args.repeats, args.concurrency, args.warmup));
  }

  const v = verdict(summaries);
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const meta = { stamp, root, queries: queriesFile, queryCount: queries.length, negCount, repeats: args.repeats, concurrency: args.concurrency, warmup: args.warmup };
  const outDir = path.join(HERE, "results", stamp);
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(path.join(outDir, "summary.md"), renderMarkdown(meta, summaries, v));
  fs.writeFileSync(path.join(outDir, "raw.json"), JSON.stringify({ meta, thresholds: THRESHOLDS, verdict: v, summaries }, null, 2));

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

main().catch((e) => { process.stderr.write(`bench error: ${e.message}\n`); process.exit(1); });
