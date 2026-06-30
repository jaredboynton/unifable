import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";

import { daemonAsk, daemonAskBatch } from "../lib/daemon-client.mjs";
import { _invalidate, _setCandidatesForTest } from "../lib/rtinfer-client.mjs";

function startServer() {
  return new Promise((resolve) => {
    const srv = http.createServer((req, res) => {
      if (req.url === "/v1/infer/health") {
        res.end(JSON.stringify({ contract: "rtinfer/1", ready: true }));
        return;
      }
      let body = "";
      req.on("data", (d) => (body += d));
      req.on("end", () => res.end(JSON.stringify({ contract: "rtinfer/1", ok: true, object: { score: 5 } })));
    });
    srv.listen(0, "127.0.0.1", () => resolve({ srv, base: `http://127.0.0.1:${srv.address().port}` }));
  });
}

function captureStderr() {
  const orig = process.stderr.write.bind(process.stderr);
  let buf = "";
  process.stderr.write = (chunk, ...rest) => { buf += chunk; return true; };
  return { read: () => buf, restore: () => { process.stderr.write = orig; } };
}

function setup(base, { debug }) {
  process.env.UNITRACE_DAEMON_RTINFER = "1";
  process.env.CSE_RTINFER_URL = base;
  if (debug) process.env.UNITRACE_DAEMON_DEBUG = "1";
  else delete process.env.UNITRACE_DAEMON_DEBUG;
  _invalidate();
  _setCandidatesForTest(() => [base]);
}

function teardown() {
  _invalidate();
  _setCandidatesForTest(null);
  delete process.env.UNITRACE_DAEMON_RTINFER;
  delete process.env.UNITRACE_DAEMON_DEBUG;
  delete process.env.CSE_RTINFER_URL;
}

test("daemonAskBatch emits a served tally attributing rtinfer per namespace", async () => {
  const { srv, base } = await startServer();
  const cap = captureStderr();
  try {
    setup(base, { debug: true });
    const reqs = [
      { system: "s", user: "a", schema: { type: "object" } },
      { system: "s", user: "b", schema: { type: "object" } },
    ];
    const out = await daemonAskBatch("bench-ns", reqs);
    assert.equal(out.length, 2);
    assert.match(cap.read(), /\[daemon\] ns=bench-ns served rtinfer=2 uds=0/);
  } finally {
    cap.restore();
    srv.close();
    teardown();
  }
});

test("daemonAsk emits a single-request served tally", async () => {
  const { srv, base } = await startServer();
  const cap = captureStderr();
  try {
    setup(base, { debug: true });
    const obj = await daemonAsk("solo-ns", { system: "s", user: "u", schema: { type: "object" } });
    assert.deepEqual(obj, { score: 5 });
    assert.match(cap.read(), /\[daemon\] ns=solo-ns served rtinfer=1 uds=0/);
  } finally {
    cap.restore();
    srv.close();
    teardown();
  }
});

test("debug off -> no tally on stderr", async () => {
  const { srv, base } = await startServer();
  const cap = captureStderr();
  try {
    setup(base, { debug: false });
    await daemonAskBatch("quiet-ns", [{ system: "s", user: "u", schema: { type: "object" } }]);
    assert.doesNotMatch(cap.read(), /\[daemon\] ns=quiet-ns served/);
  } finally {
    cap.restore();
    srv.close();
    teardown();
  }
});
