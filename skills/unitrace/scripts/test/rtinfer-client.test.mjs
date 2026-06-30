import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";

import {
  _invalidate,
  _setCandidatesForTest,
  discover,
  rtinferAsk,
  rtinferEnabled,
  rtinferToolRound,
  scorerModel,
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
  delete process.env.UNITRACE_DAEMON_RTINFER;
  delete process.env.UNITRACE_SEARCH_RTINFER;
  delete process.env.UNITRACE_SEARCH_SCORER_MODEL;
  delete process.env.EXPLORE_SEARCH_SCORER_MODEL;
  delete process.env.CSE_RTINFER_URL;
  delete process.env.CSE_RTINFER_STRICT_URL;
}

test("enabled by default: no env -> enabled", () => {
  reset();
  assert.equal(rtinferEnabled(), true);
});

test("scorer model reads the documented UNITRACE_SEARCH_SCORER_MODEL knob first", () => {
  reset();
  assert.equal(scorerModel(), "gpt-realtime-2");
  process.env.EXPLORE_SEARCH_SCORER_MODEL = "legacy-model";
  assert.equal(scorerModel(), "legacy-model");
  process.env.UNITRACE_SEARCH_SCORER_MODEL = "gpt-realtime-mini";
  assert.equal(scorerModel(), "gpt-realtime-mini");
  reset();
});

test("UNITRACE_DAEMON_RTINFER=0 opts out", () => {
  reset();
  process.env.UNITRACE_DAEMON_RTINFER = "0";
  assert.equal(rtinferEnabled(), false);
  process.env.UNITRACE_DAEMON_RTINFER = "1";
  assert.equal(rtinferEnabled(), true);
  reset();
});

test("opt-out disables rtinferAsk", async () => {
  reset();
  process.env.UNITRACE_DAEMON_RTINFER = "0";
  assert.equal(await rtinferAsk({ system: "S", user: "U", schema: { type: "object" } }), null);
  reset();
});

test("enabled but unreachable -> discover null, ask null", async () => {
  reset();
  process.env.UNITRACE_DAEMON_RTINFER = "1";
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
    process.env.UNITRACE_DAEMON_RTINFER = "1";
    _setCandidatesForTest(() => [base]);
    const obj = await rtinferAsk({ system: "S", user: "U", schema: { type: "object" }, schemaName: "score" });
    assert.deepEqual(obj, { score: 7 });
  } finally {
    srv.close();
    reset();
  }
});

test("non-ok envelope -> null (fail-open)", async () => {
  reset();
  const { srv, base } = await startServer((req, res) => {
    if (req.url === "/v1/infer/health") {
      res.end(JSON.stringify({ contract: "rtinfer/1", ready: true }));
      return;
    }
    res.end(JSON.stringify({ contract: "rtinfer/1", ok: false, error: { code: "x" } }));
  });
  try {
    process.env.UNITRACE_DAEMON_RTINFER = "1";
    _setCandidatesForTest(() => [base]);
    assert.equal(await rtinferAsk({ system: "S", user: "U", schema: { type: "object" } }), null);
  } finally {
    srv.close();
    reset();
  }
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
    _setCandidatesForTest(() => [base]);
    assert.deepEqual(await rtinferAsk({ system: "S", user: "U", schema: { type: "object" } }), { ok: 1 });
  } finally {
    srv.close();
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
    process.env.UNITRACE_SEARCH_SCORER_MODEL = "gpt-realtime-mini";
    _setCandidatesForTest(() => [base]);
    await rtinferAsk({ system: "S", user: "rank these", schema: { type: "object" }, model: scorerModel(), reasoningEffort: "low" });
    assert.equal(seen.model, "gpt-realtime-mini", "documented scorer model knob must reach the daemon request");
    assert.equal(seen.reasoning_effort, "low", "reasoning_effort must reach the daemon");
    assert.ok(seen.user.includes("rank these"), "original user text preserved");
  } finally {
    srv.close();
    reset();
  }
});

test("tool round forwards chat messages/tools and returns tool calls", async () => {
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
      res.end(JSON.stringify({
        contract: "rtinfer/1",
        ok: true,
        content: "",
        tool_calls: [{
          id: "call_1",
          type: "function",
          function: { name: "finish", arguments: "{\"files\":\"src/app.mjs:1-4\"}" },
        }],
      }));
    });
  });
  try {
    process.env.UNITRACE_DAEMON_RTINFER = "1";
    _setCandidatesForTest(() => [base]);
    const out = await rtinferToolRound({
      system: "SYS",
      messages: [{ role: "user", content: "search now" }],
      tools: [{ type: "function", function: { name: "finish", description: "done", parameters: { type: "object" } } }],
      toolChoice: "required",
      parallelToolCalls: true,
      model: "gpt-realtime-2",
      reasoningEffort: "minimal",
    });
    assert.equal(seen.tier, "realtime_tool_round");
    assert.equal(seen.tool_choice, "required");
    assert.equal(seen.parallel_tool_calls, true);
    assert.equal(seen.model, "gpt-realtime-2");
    assert.equal(seen.reasoning_effort, "minimal");
    assert.equal(seen.messages[0].role, "user");
    assert.ok(seen.messages[0].content.includes("search now"));
    assert.equal(seen.tools[0].function.name, "finish");
    assert.equal(out.tool_calls[0].function.name, "finish");
  } finally {
    srv.close();
    reset();
  }
});
