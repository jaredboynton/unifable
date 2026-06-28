import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import {
  extractFileTokens,
  extractQuoteTokens,
  extractUrlTokens,
  isExploreWireFormat,
  lintExploreWire,
  parseExploreWire,
  sectionScoreWire,
  validateTraceWire,
  WEBSEARCH_SECTIONS,
} from "../lib/explore-wire-format.mjs";
import { rehydrateTraceWire, rehydrateWebsearchWire } from "../lib/rehydrate-explore-wire.mjs";

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), "../fixtures/explore-wire-samples");
const REPO = join(dirname(fileURLToPath(import.meta.url)), "../..");

function readFixture(name) {
  return readFileSync(join(FIXTURES, name), "utf8");
}

test("isExploreWireFormat detects wire tokens", () => {
  assert.equal(isExploreWireFormat("SECTION Overview\nhello"), true);
  assert.equal(isExploreWireFormat("<file:scripts/unitrace.sh:1-5>"), true);
  assert.equal(isExploreWireFormat("## Markdown only"), false);
});

test("extractFileTokens dedups spans", () => {
  const text = "<file:scripts/a.sh:1-2> and <file:scripts/a.sh:1-2>";
  assert.equal(extractFileTokens(text).length, 1);
});

test("parseExploreWire splits sections", () => {
  const parsed = parseExploreWire(readFixture("trace-wire.txt"));
  assert.ok(parsed.sections.some((s) => s.name === "Flow"));
  assert.ok(parsed.fileTokens.length >= 3);
});

test("lintExploreWire flags markdown", () => {
  const bad = lintExploreWire("## Overview\n```js\ncode\n```");
  assert.equal(bad.ok, false);
  assert.ok(bad.issues.length >= 2);
});

test("sectionScoreWire counts websearch sections", () => {
  const score = sectionScoreWire(readFixture("websearch-wire.txt"), WEBSEARCH_SECTIONS);
  assert.ok(score >= 5);
});

test("validateTraceWire checks spans", () => {
  const parsed = parseExploreWire("<file:scripts/unitrace.sh:1-9999>");
  const v = validateTraceWire(parsed, REPO);
  assert.equal(v.ok, false);
  assert.ok(v.errors.some((e) => e.includes("too large")));
});

test("rehydrateTraceWire produces fence refs", () => {
  const md = rehydrateTraceWire(readFixture("trace-wire.txt"), REPO);
  assert.match(md, /^## Overview/m);
  assert.match(md, /```\d+:\d+:scripts\/unitrace\.sh/);
  assert.match(md, /<ref\d+>/);
});

test("rehydrateWebsearchWire preserves urls and quotes", () => {
  const md = rehydrateWebsearchWire(readFixture("websearch-wire.txt"));
  assert.match(md, /^## Executive Summary/m);
  assert.match(md, /https:\/\/modelcontextprotocol\.io\/spec/);
  assert.match(md, /^> Servers expose tools/m);
});

test("extractUrlTokens from url and quote tokens", () => {
  const urls = extractUrlTokens(readFixture("websearch-wire.txt"));
  const quotes = extractQuoteTokens(readFixture("websearch-wire.txt"));
  assert.ok(urls.length >= 2);
  assert.equal(quotes.length, 1);
});
