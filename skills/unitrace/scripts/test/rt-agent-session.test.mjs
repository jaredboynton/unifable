import test from "node:test";
import assert from "node:assert/strict";
import { RtAgentSession } from "../lib/rt-agent-session.mjs";

class MockConnection {
  constructor() {
    this.sent = [];
    this.closed = false;
  }

  send(obj) {
    if (this.closed) throw new Error("websocket closed");
    this.sent.push(obj);
  }

  async recv() {
    return { type: "response.done", response: { status: "completed", output: [] } };
  }

  close() {
    this.closed = true;
  }
}

test("freshContextMode defaults to delete", () => {
  const prev = process.env.UNISEARCH_WS_SUBMIT_FRESH_CONTEXT;
  delete process.env.UNISEARCH_WS_SUBMIT_FRESH_CONTEXT;
  delete process.env.UNITRACE_RT_SUBMIT_FRESH_CONTEXT;
  const session = new RtAgentSession({ model: "gpt-realtime-2" });
  assert.equal(session.freshContextMode(), "delete");
  if (prev != null) process.env.UNISEARCH_WS_SUBMIT_FRESH_CONTEXT = prev;
});

test("prewarm sends session.update first", async () => {
  const session = new RtAgentSession({ model: "gpt-realtime-2" });
  const mock = new MockConnection();
  session.conn = mock;
  session.alive = true;
  const patch = { type: "session.update", session: { type: "realtime", instructions: "x" } };
  session.prewarm(patch);
  assert.equal(mock.sent[0].type, "session.update");
  assert.equal(session.prewarmPatch.type, "session.update");
});

test("isConnectionClosedError detects closed socket messages", () => {
  const session = new RtAgentSession({ model: "gpt-realtime-2" });
  assert.equal(session.isConnectionClosedError(new Error("websocket not connected")), true);
  assert.equal(session.isConnectionClosedError(new Error("validation failed")), false);
});
