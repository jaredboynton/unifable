import test from "node:test";
import assert from "node:assert/strict";
import {
  buildFetchIndex,
  buildSearchCatalogPacket,
  citationTokens,
  renderWebsearchWire,
} from "../lib/rt-rehydrate-websearch.mjs";
import { validateWebsearchPointer } from "../lib/websearch-schema.mjs";
import { rehydrateWebsearchWire } from "../lib/rehydrate-explore-wire.mjs";

const fetchLog = [
  {
    fetchIndex: 0,
    url: "https://modelcontextprotocol.io/spec",
    title: "MCP Spec",
    text: "Model Context Protocol (MCP) is an open protocol.",
    excerpts: ["Model Context Protocol (MCP) is an open protocol."],
  },
  {
    fetchIndex: 1,
    url: "https://github.com/modelcontextprotocol/servers",
    title: "Servers",
    text: "Reference implementations for MCP.",
    excerpts: ["Reference implementations for MCP."],
  },
];

test("buildSearchCatalogPacket lists catalog indices", () => {
  const catalog = [
    { catalogIndex: 0, url: "https://example.com", title: "Ex", query: "q" },
  ];
  const packet = buildSearchCatalogPacket(catalog, { goal: "test goal" });
  assert.match(packet, /\[0\] https:\/\/example.com/);
  assert.match(packet, /url_indices/);
});

test("buildFetchIndex emits url_index and excerpt previews", () => {
  const index = buildFetchIndex(fetchLog);
  assert.match(index, /\[0\] https:\/\/modelcontextprotocol.io\/spec/);
  assert.match(index, /excerpt\[0\]/);
});

test("buildFetchIndex includes all fetched URLs by default", () => {
  const log = Array.from({ length: 15 }, (_, i) => ({
    fetchIndex: i,
    url: `https://example.com/${i}`,
    title: `Page ${i}`,
    excerpts: [`excerpt ${i}`],
  }));
  const index = buildFetchIndex(log);
  assert.match(index, /\[14\] https:\/\/example.com\/14/);
  assert.doesNotMatch(index, /omitted from index/);
});

test("renderWebsearchWire injects url and quote tokens from pointers", () => {
  const pointer = {
    executive_summary: "MCP bridges LLM apps to tools.",
    in_scope_findings: "Official spec defines the protocol.",
    adjacent_out_of_scope: "None.",
    prior_art: "Reference servers repo.",
    gaps_risks: "Static docs may lag.",
    recommended_next_steps: "Read the spec.",
    citation_refs: [
      { url_index: 0, excerpt_index: 0, rationale: "spec definition" },
      { url_index: 1, excerpt_index: 0, rationale: "reference impl" },
    ],
  };
  const wire = renderWebsearchWire(pointer, fetchLog);
  assert.match(wire, /<url:https:\/\/modelcontextprotocol.io\/spec>/);
  assert.match(wire, /<quote:https:\/\/modelcontextprotocol.io\/spec\|/);
  assert.match(wire, /SECTION ExecutiveSummary/);
});

test("validateWebsearchPointer rejects raw URLs in prose", () => {
  const pointer = {
    executive_summary: "See https://evil.com",
    in_scope_findings: "x",
    adjacent_out_of_scope: "",
    prior_art: "",
    gaps_risks: "",
    recommended_next_steps: "y",
    citation_refs: [{ url_index: 0, excerpt_index: 0, rationale: "ok" }],
  };
  assert.match(validateWebsearchPointer(pointer, fetchLog), /raw URLs/);
});

test("rehydrated wire produces markdown sections", () => {
  const wire = renderWebsearchWire({
    executive_summary: "MCP summary.",
    in_scope_findings: "Findings.",
    adjacent_out_of_scope: "Adjacent.",
    prior_art: "Prior.",
    gaps_risks: "Gaps.",
    recommended_next_steps: "Next.",
    citation_refs: [{ url_index: 0, excerpt_index: 0, rationale: "spec" }],
  }, fetchLog);
  const md = rehydrateWebsearchWire(wire);
  assert.match(md, /## Executive Summary/);
  assert.match(md, /modelcontextprotocol.io/);
});

test("citationTokens dedupes url tokens", () => {
  const tokens = citationTokens([
    { url_index: 0, excerpt_index: 0, rationale: "a" },
    { url_index: 0, excerpt_index: 0, rationale: "b" },
  ], fetchLog);
  assert.equal(tokens.filter((t) => t.startsWith("<url:")).length, 1);
});
