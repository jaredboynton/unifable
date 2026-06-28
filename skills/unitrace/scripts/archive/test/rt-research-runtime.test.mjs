import test from "node:test";
import assert from "node:assert/strict";
import {
  createResearchContext,
  runResearchExec,
  shouldStopResearch,
} from "../lib/rt-research-runtime.mjs";

const originalFetch = globalThis.fetch;

test("runResearchExec exa_search and exa_fetch track grounding", async (t) => {
  process.env.EXA_API_KEY = "test-key";
  const ctx = createResearchContext();

  globalThis.fetch = async (url, init) => {
    const path = String(url);
    const body = JSON.parse(init.body);
    if (path.endsWith("/search")) {
      return {
        ok: true,
        async text() {
          return JSON.stringify({
            results: [{ url: "https://example.com/doc", title: "Doc", score: 0.9 }],
          });
        },
      };
    }
    if (path.endsWith("/contents")) {
      assert.deepEqual(body.urls, ["https://example.com/doc"]);
      return {
        ok: true,
        async text() {
          return JSON.stringify({
            results: [{ url: "https://example.com/doc", title: "Doc", text: "verified excerpt text" }],
          });
        },
      };
    }
    throw new Error(`unexpected fetch: ${path}`);
  };

  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  const code = `
const search = await tools.exa_search({ query: "mcp protocol" });
const fetch = await tools.exa_fetch({ urls: search.results.map(r => r.url) });
return { searchCount: search.resultCount, fetched: fetch.fetched };
`;

  const result = await runResearchExec(code, ctx);
  assert.equal(result.ok, true);
  assert.equal(ctx.searchCount, 1);
  assert.equal(ctx.urlsFetched.size, 1);
  assert.ok(ctx.urlsFetched.has("https://example.com/doc"));
  assert.equal(ctx.fetchLog.get("https://example.com/doc"), "verified excerpt text");
  assert.equal(ctx.toolTurns, 1);
});

test("shouldStopResearch stops after search threshold", () => {
  const ctx = createResearchContext();
  ctx.searchCount = 4;
  assert.equal(shouldStopResearch(ctx, { stopSearches: 4 }), true);
});

test("runResearchExec rejects disallowed patterns", async () => {
  process.env.EXA_API_KEY = "test-key";
  const ctx = createResearchContext();
  const result = await runResearchExec("return eval('1')", ctx);
  assert.equal(result.ok, false);
  assert.match(result.error, /disallowed pattern/);
});

test("runResearchExec fails without EXA_API_KEY", async () => {
  delete process.env.EXA_API_KEY;
  const ctx = createResearchContext();
  const result = await runResearchExec("return 1", ctx);
  assert.equal(result.ok, false);
  assert.match(result.error, /EXA_API_KEY/);
});
