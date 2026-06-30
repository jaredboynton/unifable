import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";

import {
  _invalidate,
  _setCandidatesForTest,
  _setDiscoveryHintForTest,
  discover,
  rtinferAsk,
  rtinferEnabled,
} from "../lib/rtinfer-client.mjs";

function startServer(handler) {
  return new Promise((resolve) => {
    const srv = http.createServer(handler);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      resolve({ srv, base: `http://127.0.0.1:${port}` });
    });
  });
}

function reset() {
  _invalidate();
  _setCandidatesForTest(null);
  _setDiscoveryHintForTest(null);
  delete process.env.UNITRACE_DAEMON_RTINFER;
  delete process.env.UNITRACE_SEARCH_RTINFER;
  delete process.env.CSE_RTINFER_URL;
  delete process.env.CSE_RTINFER_STRICT_URL;
}

test("disabled by default: no env -> not enabled, ask is a no-op", async () => {
  reset();
  assert.equal(rtinferEnabled(), false);
  assert.equal(await rtinferAsk({ system: "S", user: "U", schema: { type: "object" } }), null);
});

test("enabled but unreachable -> discover null, ask null", async () => {
  reset();
  process.env.UNITRACE_SEARCH_RTINFER = "1";
  process.env.CSE_RTINFER_URL = "http://127.0.0.1:1"; // presence hint + unreachable
  _setCandidatesForTest(() => ["http://127.0.0.1:1"]);
  assert.equal(await discover(), null);
  assert.equal(await rtinferAsk({ system: "S", user: "U", schema: { type: "object" } }), null);
  reset();
});

test("ok envelope -> returns the parsed object", async () => {
  reset();
  const { srv, base } = await startServer((req, res) => {
    if (req.url === "/v1/infer/health") {
      res.end(JSON.stringify({ contract: "rtinfer/1", ready: true }));
      return;
    }
    let body = "";
    req.on("data", (d) => (body += d));
    req.on("end", () => res.end(JSON.stringify({ contract: "rtinfer/1", ok: true, object: { score: 7 } })));
  });
  try {
    process.env.UNITRACE_SEARCH_RTINFER = "1";
    process.env.CSE_RTINFER_URL = base;
    _setCandidatesForTest(() => [base]);
    const obj = await rtinferAsk({ system: "S", user: "U", schema: { type: "object" }, schemaName: "score" });
    assert.deepEqual(obj, { score: 7 });
  } finally {
    srv.close();
    reset();
  }
});

test("non-ok envelope -> null (fail-open to fallback)", async () => {
  reset();
  const { srv, base } = await startServer((req, res) => {
    if (req.url === "/v1/infer/health") {
      res.end(JSON.stringify({ contract: "rtinfer/1", ready: true }));
      return;
    }
    res.end(JSON.stringify({ contract: "rtinfer/1", ok: false, error: { code: "x" } }));
  });
  try {
    process.env.UNITRACE_SEARCH_RTINFER = "1";
    process.env.CSE_RTINFER_URL = base;
    _setCandidatesForTest(() => [base]);
    assert.equal(await rtinferAsk({ system: "S", user: "U", schema: { type: "object" } }), null);
  } finally {
    srv.close();
    reset();
  }
});

test("broad flag UNITRACE_DAEMON_RTINFER enables; legacy alias still works", () => {
  reset();
  assert.equal(rtinferEnabled(), false);
  process.env.UNITRACE_DAEMON_RTINFER = "1";
  assert.equal(rtinferEnabled(), true);
  process.env.UNITRACE_DAEMON_RTINFER = "0";
  assert.equal(rtinferEnabled(), false, "broad flag off wins even if legacy set");
  process.env.UNITRACE_SEARCH_RTINFER = "1";
  assert.equal(rtinferEnabled(), false, "explicit broad=0 overrides legacy alias");
  delete process.env.UNITRACE_DAEMON_RTINFER;
  assert.equal(rtinferEnabled(), true, "legacy alias still enables when broad unset");
  reset();
});

test("health accepts rtinfer/1.x (major match); rtinfer/2 fails open", async () => {
  reset();
  const { srv, base } = await startServer((req, res) => {
    if (req.url === "/v1/infer/health") {
      res.end(JSON.stringify({ contract: "rtinfer/1.4", ready: true }));
      return;
    }
    res.end(JSON.stringify({ contract: "rtinfer/1.4", ok: true, object: { ok: 1 } }));
  });
  try {
    process.env.UNITRACE_DAEMON_RTINFER = "1";
    process.env.CSE_RTINFER_URL = base;
    _setCandidatesForTest(() => [base]);
    assert.equal(await discover(), base, "minor bump rtinfer/1.4 stays compatible");
    assert.deepEqual(await rtinferAsk({ system: "S", user: "U", schema: { type: "object" } }), { ok: 1 });
  } finally {
    srv.close();
    reset();
  }

  reset();
  const two = await startServer((req, res) => {
    res.end(JSON.stringify({ contract: "rtinfer/2", ready: true }));
  });
  try {
    process.env.UNITRACE_DAEMON_RTINFER = "1";
    process.env.CSE_RTINFER_URL = two.base;
    _setCandidatesForTest(() => [two.base]);
    assert.equal(await discover(), null, "rtinfer/2 is an incompatible major -> fall open");
  } finally {
    two.srv.close();
    reset();
  }
});

test("reasoning effort is forwarded and the user is steer-wrapped on the wire", async () => {
  reset();
  let seen = null;
  const { srv, base } = await startServer((req, res) => {
    if (req.url === "/v1/infer/health") {
      res.end(JSON.stringify({ contract: "rtinfer/1", ready: true }));
      return;
    }
    let body = "";
    req.on("data", (d) => (body += d));
    req.on("end", () => {
      seen = JSON.parse(body);
      res.end(JSON.stringify({ contract: "rtinfer/1", ok: true, object: { score: 5 } }));
    });
  });
  try {
    process.env.UNITRACE_DAEMON_RTINFER = "1";
    process.env.CSE_RTINFER_URL = base;
    _setCandidatesForTest(() => [base]);
    await rtinferAsk({ system: "S", user: "rank these", schema: { type: "object" }, reasoningEffort: "low" });
    assert.equal(seen.reasoning_effort, "low", "reasoning_effort must reach the daemon");
    assert.ok(seen.user.includes("rank these"), "original user text preserved");
  } finally {
    srv.close();
    reset();
  }
});

test("enabled with no presence hint -> discover skips probing entirely", async () => {
  reset();
  process.env.UNITRACE_SEARCH_RTINFER = "1";
  _setDiscoveryHintForTest(() => false);
  // No CSE_RTINFER_URL. Force a candidate that would 'succeed' if probed; the
  // hint gate must short-circuit before any candidate is tried.
  let probed = false;
  _setCandidatesForTest(() => { probed = true; return []; });
  const base = await discover();
  assert.equal(base, null);
  assert.equal(probed, false, "discovery must not probe without a presence hint");
  reset();
});

test("strict-URL mode: a dead override never falls through to the cockpit default", async () => {
  // A live server stands in for the cockpit default; strict mode + a dead
  // override must NOT reach it (proves the bench's fail-open arm is honest).
  reset();
  const { srv, base } = await startServer((req, res) => {
    if (req.url === "/v1/infer/health") { res.end(JSON.stringify({ contract: "rtinfer/1", ready: true })); return; }
    res.end(JSON.stringify({ contract: "rtinfer/1", ok: true, object: { ok: 1 } }));
  });
  try {
    process.env.UNITRACE_DAEMON_RTINFER = "1";
    process.env.CSE_RTINFER_URL = "http://127.0.0.1:9"; // dead
    process.env.CSE_RTINFER_STRICT_URL = "1";
    // No test seam: exercise the real candidates() so strict mode is covered.
    assert.equal(await discover(), null, "strict mode must not borrow the live cockpit default");
    // Without strict mode, the cockpit default would be a candidate. Point the
    // override at the live server to confirm strict still trusts the override.
    _invalidate();
    process.env.CSE_RTINFER_URL = base;
    assert.equal(await discover(), base, "strict mode still honors a live explicit override");
  } finally {
    srv.close();
    delete process.env.CSE_RTINFER_STRICT_URL;
    reset();
  }
});
