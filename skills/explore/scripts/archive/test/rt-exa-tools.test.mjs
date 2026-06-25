import test from "node:test";
import assert from "node:assert/strict";
import {
  createWebsearchContext,
  dispatchExaSearch,
  dispatchExaFetch,
  shouldStopSearch,
  chunkExcerpts,
} from "../lib/rt-exa-tools.mjs";

const originalFetch = globalThis.fetch;

test("dispatchExaSearch populates search catalog", async (t) => {
  process.env.EXA_API_KEY = "test-key";
  const ctx = createWebsearchContext();

  globalThis.fetch = async (url, init) => {
    assert.match(String(url), /\/search$/);
    return {
      ok: true,
      async text() {
        return JSON.stringify({
          results: [{ url: "https://example.com/doc", title: "Doc", score: 0.9 }],
        });
      },
    };
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  const result = await dispatchExaSearch({ query: "mcp protocol" }, ctx);
  assert.equal(result.ok, true);
  assert.equal(ctx.searchCount, 1);
  assert.equal(ctx.searchCatalog.length, 1);
  assert.equal(ctx.searchCatalog[0].catalogIndex, 0);
  assert.equal(result.hits[0].catalog_index, 0);
});

test("dispatchExaFetch resolves catalog indices and builds fetch log", async (t) => {
  process.env.EXA_API_KEY = "test-key";
  const ctx = createWebsearchContext();
  ctx.searchCatalog.push({
    catalogIndex: 0,
    url: "https://example.com/doc",
    title: "Doc",
    score: 0.9,
    query: "test",
  });

  globalThis.fetch = async (url) => {
    assert.match(String(url), /\/contents$/);
    return {
      ok: true,
      async text() {
        return JSON.stringify({
          results: [{ url: "https://example.com/doc", title: "Doc", text: "verified excerpt text here" }],
        });
      },
    };
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  const result = await dispatchExaFetch({ url_indices: [0] }, ctx);
  assert.equal(result.ok, true);
  assert.equal(ctx.fetchLog.length, 1);
  assert.equal(ctx.fetchLog[0].fetchIndex, 0);
  assert.ok(ctx.fetchLog[0].excerpts.length >= 1);
  assert.equal(ctx.fetchCount, 1);
});

test("dispatchExaFetch rejects invalid catalog index", async () => {
  process.env.EXA_API_KEY = "test-key";
  const ctx = createWebsearchContext();
  const result = await dispatchExaFetch({ url_indices: [99] }, ctx);
  assert.equal(result.ok, false);
  assert.match(result.error, /invalid catalog index/);
});

test("shouldStopSearch stops after search threshold", () => {
  const ctx = createWebsearchContext();
  ctx.searchCount = 4;
  assert.equal(shouldStopSearch(ctx, { stopSearches: 4 }), true);
});

test("chunkExcerpts splits long text", () => {
  const text = "word ".repeat(200);
  const chunks = chunkExcerpts(text, 50);
  assert.ok(chunks.length > 1);
});
