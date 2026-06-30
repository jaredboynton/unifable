import test from "node:test";
import assert from "node:assert/strict";
import {
  extractUrlsFromText,
  populateFetchLogFromAlphaOutput,
  populateFetchLogFromOpenOutput,
  buildWebRunToolSchemas,
  mergeWebRunSearchQueries,
  mergeWebRunOpenUrls,
  excerptForUrl,
  queryTerms,
  selectQuerySpans,
  scoreUrlAuthority,
  rankUrlsByAuthority,
  augmentQueriesForAuthority,
  pruneFetchLogForSubmit,
  createWebsearchContext,
} from "../lib/rt-web-run-tools.mjs";
import { WEB_RUN_TOOL_NAME, webRunCommandsFromArgs } from "../lib/rt-web-run.mjs";

test("extractUrlsFromText dedupes markdown and bare URLs", () => {
  const text = "See [spec](https://modelcontextprotocol.io/spec) and https://github.com/modelcontextprotocol/servers.";
  const urls = extractUrlsFromText(text);
  assert.equal(urls.length, 2);
  assert.ok(urls[0].includes("modelcontextprotocol.io"));
});

test("excerptForUrl captures local context", () => {
  const text = "Intro text. Official spec at https://modelcontextprotocol.io/spec defines MCP. More text.";
  const excerpt = excerptForUrl(text, "https://modelcontextprotocol.io/spec");
  assert.match(excerpt, /defines MCP/);
});

test("populateFetchLogFromAlphaOutput uses per-URL excerpts", () => {
  const ctx = createWebsearchContext();
  const output = "Spec: https://modelcontextprotocol.io/spec defines MCP.\nRef: https://github.com/modelcontextprotocol/servers has examples.";
  populateFetchLogFromAlphaOutput(ctx, output, { query: "MCP" });
  assert.equal(ctx.fetchLog.length, 2);
  assert.match(ctx.fetchLog[0].text, /defines MCP/);
  assert.match(ctx.fetchLog[1].text, /examples/);
});

test("populateFetchLogFromAlphaOutput fallback when no URLs", () => {
  const ctx = createWebsearchContext();
  populateFetchLogFromAlphaOutput(ctx, "No URLs here, just prose about MCP.", { query: "MCP" });
  assert.equal(ctx.fetchLog.length, 1);
  assert.equal(ctx.fetchLog[0].url, "https://web.search/");
});

test("mergeWebRunSearchQueries dedupes across calls", () => {
  const calls = [
    { arguments: JSON.stringify({ search_query: [{ q: "MCP spec" }, { q: "MCP official" }] }) },
    { arguments: JSON.stringify({ search_query: [{ q: "MCP spec" }, { q: "MCP github" }] }) },
  ];
  const merged = mergeWebRunSearchQueries(calls);
  assert.equal(merged.length, 3);
  assert.deepEqual(merged.map((q) => q.q), ["MCP spec", "MCP official", "MCP github"]);
});

test("buildWebRunToolSchemas exposes web_run", () => {
  const schemas = buildWebRunToolSchemas();
  assert.equal(schemas.length, 1);
  assert.equal(schemas[0].name, WEB_RUN_TOOL_NAME);
});

test("buildWebRunToolSchemas allowOpen exposes the open parameter", () => {
  const plain = buildWebRunToolSchemas();
  assert.equal(plain[0].parameters.properties.open, undefined);
  const withOpen = buildWebRunToolSchemas({ allowOpen: true });
  assert.ok(withOpen[0].parameters.properties.open);
  assert.equal(withOpen[0].parameters.properties.open.type, "array");
});

test("populateFetchLogFromOpenOutput parses real page content per source", () => {
  const ctx = createWebsearchContext();
  const output = [
    'Source: open({"ref_id":"https://modelcontextprotocol.io/spec"})',
    "Total lines: 3",
    "L0: Model Context Protocol is an open protocol.",
    "L1: It connects LLMs to tools.",
    'Source: open({"ref_id":"https://github.com/modelcontextprotocol/servers"})',
    "Total lines: 2",
    "L0: Reference server implementations.",
  ].join("\n");
  const indices = populateFetchLogFromOpenOutput(ctx, output);
  assert.equal(indices.length, 2);
  assert.equal(ctx.fetchLog.length, 2);
  assert.match(ctx.fetchLog[0].text, /open protocol/);
  assert.match(ctx.fetchLog[0].text, /connects LLMs/);
  assert.match(ctx.fetchLog[1].text, /Reference server/);
  assert.equal(ctx.fetchCount, 2);
});

test("populateFetchLogFromOpenOutput upgrades snippet excerpts to page content", () => {
  const ctx = createWebsearchContext();
  populateFetchLogFromAlphaOutput(ctx, "Spec: https://modelcontextprotocol.io/spec is short snippet.", { query: "MCP" });
  const before = ctx.fetchLog[0].text.length;
  const longPage = "L0: " + "Full page body sentence. ".repeat(40);
  populateFetchLogFromOpenOutput(ctx, `Source: open({"ref_id":"https://modelcontextprotocol.io/spec"})\nTotal lines: 1\n${longPage}`);
  assert.equal(ctx.fetchLog.length, 1, "same URL should not duplicate");
  assert.ok(ctx.fetchLog[0].text.length > before, "page content should replace shorter snippet");
  assert.match(ctx.fetchLog[0].text, /Full page body/);
});

test("populateFetchLogFromOpenOutput falls back to snippet parsing without open blocks", () => {
  const ctx = createWebsearchContext();
  populateFetchLogFromOpenOutput(ctx, "Just snippets: https://example.com/a and https://example.com/b.");
  assert.equal(ctx.fetchLog.length, 2);
});

test("mergeWebRunOpenUrls dedupes open URLs across calls", () => {
  const calls = [
    { arguments: JSON.stringify({ open: ["https://a.com", "https://b.com"] }) },
    { arguments: JSON.stringify({ search_query: [{ q: "x" }], open: ["https://b.com", "https://c.com"] }) },
  ];
  const merged = mergeWebRunOpenUrls(calls);
  assert.equal(merged.length, 3);
  assert.ok(merged.every((u) => u.startsWith("https://")));
});

test("pruneFetchLogForSubmit drops low-authority snippet-only entries, keeps opened + authoritative", () => {
  const ctx = createWebsearchContext();
  // Enough high-value entries so the gate is not forced to fall back.
  ctx.fetchLog = [
    { fetchIndex: 0, url: "https://arxiv.org/abs/1", text: "x", excerpts: ["x"], opened: false },
    { fetchIndex: 1, url: "https://github.com/openai/x", text: "x", excerpts: ["x"], opened: false },
    { fetchIndex: 2, url: "https://www.swequiz.com/a", text: "snip", excerpts: ["snip"], opened: false },
    { fetchIndex: 3, url: "https://openrouter.ai/d", text: "snip", excerpts: ["snip"], opened: false },
    { fetchIndex: 4, url: "https://some-blog.example.com/p", text: "full page", excerpts: ["full page"], opened: true },
    { fetchIndex: 5, url: "https://usenix.org/p", text: "x", excerpts: ["x"], opened: false },
    { fetchIndex: 6, url: "https://tryrankly.com/b", text: "snip", excerpts: ["snip"], opened: false },
  ];
  const kept = pruneFetchLogForSubmit(ctx);
  const urls = kept.map((e) => e.url);
  assert.ok(urls.includes("https://arxiv.org/abs/1"));
  assert.ok(urls.includes("https://some-blog.example.com/p"), "opened low-authority page is kept");
  assert.ok(!urls.includes("https://www.swequiz.com/a"), "low-authority snippet dropped");
  assert.ok(!urls.includes("https://tryrankly.com/b"), "low-authority snippet dropped");
  kept.forEach((e, i) => assert.equal(e.fetchIndex, i, "fetchIndex renumbered"));
});

test("augmentQueriesForAuthority adds domain-scoped variants while keeping model queries", () => {
  const out = augmentQueriesForAuthority([{ q: "reduce websearch latency" }, { q: "citation verification" }]);
  assert.ok(out.some((q) => q.domains && q.domains.includes("arxiv.org")));
  assert.ok(out.some((q) => q.domains && q.domains.includes("github.com")));
  assert.ok(out.some((q) => q.q === "reduce websearch latency" && !q.domains));
  assert.ok(out.length <= 8);
});

test("scoreUrlAuthority ranks primary sources above aggregators", () => {
  assert.ok(scoreUrlAuthority("https://arxiv.org/abs/1234") > scoreUrlAuthority("https://apis.io/apis/x"));
  assert.ok(scoreUrlAuthority("https://github.com/openai/codex") > scoreUrlAuthority("https://medium.com/@x/post"));
  assert.ok(scoreUrlAuthority("https://platform.openai.com/docs/guides/x") > scoreUrlAuthority("https://some-blog.example.com/p"));
  assert.ok(scoreUrlAuthority("https://developers.openai.com/api/docs") >= 7);
});

test("rankUrlsByAuthority opens primary sources first and is stable on ties", () => {
  const urls = [
    "https://apis.io/apis/openai/realtime",
    "https://arxiv.org/abs/2403.05676",
    "https://medium.com/@a/b",
    "https://github.com/openai/codex",
  ];
  const ranked = rankUrlsByAuthority(urls);
  assert.equal(ranked[0], "https://arxiv.org/abs/2403.05676");
  assert.equal(ranked[1], "https://github.com/openai/codex");
  assert.equal(ranked[ranked.length - 1], "https://medium.com/@a/b");
});

test("queryTerms drops stopwords and short tokens", () => {
  const terms = queryTerms("How to reduce latency in a realtime websearch pipeline");
  assert.ok(!terms.includes("how"));
  assert.ok(!terms.includes("to"));
  assert.ok(terms.includes("reduce"));
  assert.ok(terms.includes("latency"));
  assert.ok(terms.includes("realtime"));
  assert.ok(terms.includes("websearch"));
});

test("selectQuerySpans prefers spans matching query terms over boilerplate", () => {
  const text = [
    "Skip to content. Navigation menu. Cookie settings and footer links.",
    "",
    "Semantic-aware knowledge caching reduces remote data access latency for LLM agents.",
    "",
    "Subscribe to our newsletter for updates and promotions.",
  ].join("\n");
  const spans = selectQuerySpans(text, "reduce latency caching LLM agents", { maxSpans: 1 });
  assert.equal(spans.length, 1);
  assert.match(spans[0], /caching reduces remote data access latency/);
});

test("selectQuerySpans skips navigation/boilerplate and quotes real content", () => {
  const text = [
    "* cite4† Overview | cite5† Quickstart | cite6† Pricing | cite7† Models | cite8† Sandboxing",
    "",
    "Request coalescing collapses duplicate in-flight fetches into a single shared request, reducing latency under load.",
  ].join("\n");
  const spans = selectQuerySpans(text, "request coalescing latency fetches", { maxSpans: 1 });
  assert.equal(spans.length, 1);
  assert.doesNotMatch(spans[0], /Quickstart|Pricing|Overview/);
  assert.match(spans[0], /coalescing collapses duplicate/);
});

test("selectQuerySpans excludes doc-index nav menus (Title-Case fragments, no sentences)", () => {
  const nav = "Home API Docs API reference Endpoints Codex ChatGPT Apps SDK Workspace Agents Commerce Resources Showcase Blog Learn";
  const real = "Pipeline parallelism overlaps retrieval and generation, cutting end-to-end latency for the agent.";
  const spans = selectQuerySpans(`${nav}\n\n${real}`, "pipeline parallelism latency retrieval codex api", { maxSpans: 2 });
  assert.ok(!spans.some((s) => /Apps SDK|Showcase|API reference/.test(s)), "nav menu should be excluded");
  assert.ok(spans.some((s) => /Pipeline parallelism overlaps/.test(s)), "real content should be kept");
});

test("selectQuerySpans returns [] when a page is entirely nav/boilerplate", () => {
  const navOnly = "Home API Docs API reference Endpoints Codex ChatGPT Apps SDK Workspace Agents Commerce Resources Showcase Blog Learn";
  assert.deepEqual(selectQuerySpans(navOnly, "pipeline latency"), []);
});

test("populateFetchLogFromOpenOutput skips all-boilerplate pages (no nav citations)", () => {
  const ctx = createWebsearchContext();
  const output = [
    'Source: open({"ref_id":"https://platform.openai.com/docs/models/x"})',
    "Total lines: 2",
    "L0: Home API Docs API reference Endpoints Codex ChatGPT Apps SDK Workspace Agents Commerce Resources Showcase Blog Learn Pricing",
    'Source: open({"ref_id":"https://arxiv.org/abs/2401.05856"})',
    "Total lines: 2",
    "L0: We present an experience report on the failure points of retrieval augmented generation systems in production.",
  ].join("\n");
  populateFetchLogFromOpenOutput(ctx, output, { query: "rag failure points retrieval" });
  const urls = ctx.fetchLog.map((e) => e.url);
  assert.ok(!urls.some((u) => u.includes("platform.openai.com/docs/models")), "all-nav page should be skipped");
  assert.ok(urls.some((u) => u.includes("arxiv.org/abs/2401.05856")), "real-content page should be kept");
});

test("selectQuerySpans falls back to leading spans when query has no signal", () => {
  const text = "First paragraph has substance here.\n\nSecond paragraph also has content here.";
  const spans = selectQuerySpans(text, "", { maxSpans: 1 });
  assert.equal(spans.length, 1);
  assert.match(spans[0], /First paragraph/);
});

test("populateFetchLogFromOpenOutput stores query-matched spans as excerpts", () => {
  const ctx = createWebsearchContext();
  const output = [
    'Source: open({"ref_id":"https://ex.com/p"})',
    "Total lines: 4",
    "L0: Cookie banner and navigation boilerplate text for the site header.",
    "L1: Request coalescing and deduplication avoid redundant fetches in crawlers.",
    "L2: Newsletter signup and unrelated footer content here.",
  ].join("\n");
  populateFetchLogFromOpenOutput(ctx, output, { query: "request coalescing deduplication fetches" });
  assert.equal(ctx.fetchLog.length, 1);
  assert.ok(ctx.fetchLog[0].excerpts.some((e) => /coalescing and deduplication/.test(e)));
});

test("webRunCommandsFromArgs accepts open-only and search+open", () => {
  const openOnly = webRunCommandsFromArgs({ open: ["https://a.com"] });
  assert.deepEqual(openOnly.open, [{ ref_id: "https://a.com" }]);
  assert.equal(openOnly.search_query, undefined);
  const both = webRunCommandsFromArgs({ search_query: [{ q: "x" }], open: ["https://a.com"] });
  assert.equal(both.search_query.length, 1);
  assert.equal(both.open.length, 1);
  assert.throws(() => webRunCommandsFromArgs({}), /at least one/);
});
