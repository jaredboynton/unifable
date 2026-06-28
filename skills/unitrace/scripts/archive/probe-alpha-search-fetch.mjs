#!/usr/bin/env node
// Probe Codex alpha/search: search_query then commands.open (native web.run fetch).
import {
  buildAlphaSearchBody,
  postAlphaSearch,
} from "./lib/codex-alpha-search-client.mjs";
import { extractUrlsFromText } from "./lib/rt-web-run-tools.mjs";

const DEFAULT_QUERY =
  "Model Context Protocol official specification site:modelcontextprotocol.io";

function argValue(name, fallback) {
  const i = process.argv.indexOf(name);
  return i === -1 ? fallback : process.argv[i + 1];
}

function envInt(name, fallback) {
  const v = process.env[name];
  if (v == null || v === "") return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? Math.trunc(n) : fallback;
}

const dryRun = process.argv.includes("--dry-run");
const query = argValue("--query", DEFAULT_QUERY);
const searchModel = argValue("--search-model", process.env.UNITRACE_SEARCH_MODEL || "gpt-5.4");
const authPath = process.env.UNITRACE_CODEX_AUTH_PATH || null;
const openCap = envInt("UNISEARCH_ALPHA_OPEN_CAP", 3);
const mode = argValue("--mode", "search-then-open"); // search-only | open-only | search-then-open | combined

const MCP_OPEN_URLS = [
  "https://modelcontextprotocol.io/specification/2025-06-18",
  "https://github.com/modelcontextprotocol/python-sdk",
  "https://github.com/modelcontextprotocol/typescript-sdk",
];

function preview(text, max = 600) {
  const s = String(text || "").replace(/\s+/g, " ").trim();
  return s.length <= max ? s : `${s.slice(0, max)}…`;
}

function pickOpenUrls(searchOutput) {
  const fromSearch = extractUrlsFromText(searchOutput);
  const picked = [];
  const seen = new Set();
  for (const url of [...fromSearch, ...MCP_OPEN_URLS]) {
    if (seen.has(url)) continue;
    seen.add(url);
    picked.push(url);
    if (picked.length >= openCap) break;
  }
  return picked;
}

async function alphaCall(commands, label) {
  const body = buildAlphaSearchBody({
    model: searchModel,
    commands,
  });
  const started = Date.now();
  const result = await postAlphaSearch({ authPathOverride: authPath, body });
  return {
    label,
    ms: Date.now() - started,
    outputLen: result.output.length,
    outputPreview: preview(result.output),
    output: result.output,
    commands,
  };
}

async function main() {
  if (dryRun) {
    console.log(JSON.stringify({
      ok: true,
      dryRun: true,
      mode,
      query,
      searchModel,
      openCap,
      nativeFetch: "commands.open on POST alpha/search (OpenOperation.ref_id = URL)",
    }, null, 2));
    return;
  }

  const report = { ok: true, mode, query, searchModel, phases: [] };

  if (mode === "open-only") {
    const openUrls = MCP_OPEN_URLS.slice(0, openCap);
    const open = await alphaCall({ open: openUrls.map((ref_id) => ({ ref_id })) }, "open");
    report.phases.push(open);
    report.openUrls = openUrls;
  } else if (mode === "combined") {
    const combined = await alphaCall({
      search_query: [{ q: query }],
      open: MCP_OPEN_URLS.slice(0, openCap).map((ref_id) => ({ ref_id })),
    }, "search+open");
    report.phases.push(combined);
    report.openUrls = MCP_OPEN_URLS.slice(0, openCap);
  } else {
    const search = await alphaCall({ search_query: [{ q: query }] }, "search");
    report.phases.push(search);

    if (mode === "search-then-open") {
      const openUrls = pickOpenUrls(search.output);
      report.openUrls = openUrls;
      if (openUrls.length) {
        const open = await alphaCall({
          open: openUrls.map((ref_id) => ({ ref_id })),
        }, "open");
        report.phases.push(open);
      } else {
        report.openSkipped = "no URLs extracted from search output";
      }
    }
  }

  const searchPhase = report.phases.find((p) => p.label === "search");
  const openPhase = report.phases.find((p) => p.label === "open" || p.label === "search+open");
  report.summary = {
    searchMs: searchPhase?.ms ?? null,
    openMs: openPhase?.ms ?? null,
    totalMs: report.phases.reduce((s, p) => s + p.ms, 0),
    searchOutputLen: searchPhase?.outputLen ?? openPhase?.outputLen ?? null,
    openOutputLen: openPhase?.outputLen ?? null,
    openUrlCount: report.openUrls?.length ?? 0,
  };

  console.log(JSON.stringify(report, null, 2));
}

main().catch((err) => {
  console.log(JSON.stringify({ ok: false, error: err.message }, null, 2));
  process.exit(1);
});
