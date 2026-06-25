import test from "node:test";
import assert from "node:assert/strict";
import { readEvent } from "../lib/realtime_client.mjs";

// Build an unmasked server->client WebSocket frame (RFC 6455 5.2).
function serverFrame(opcode, payload, fin = true) {
  const b0 = (fin ? 0x80 : 0x00) | opcode;
  const n = payload.length;
  let header;
  if (n < 126) header = Buffer.from([b0, n]);
  else if (n < 65536) { header = Buffer.alloc(4); header[0] = b0; header[1] = 126; header.writeUInt16BE(n, 2); }
  else { header = Buffer.alloc(10); header[0] = b0; header[1] = 127; header.writeBigUInt64BE(BigInt(n), 2); }
  return Buffer.concat([header, payload]);
}

// Minimal reader serving a fixed byte stream via read(n); records write() (pongs).
function mockReader(buf) {
  let off = 0;
  return {
    writes: [],
    async read(n) {
      const out = buf.subarray(off, off + n);
      off += n;
      return out;
    },
    write(b) { this.writes.push(Buffer.from(b)); },
  };
}

test("readEvent parses an unfragmented message", async () => {
  const env = await readEvent(mockReader(serverFrame(0x1, Buffer.from(JSON.stringify({ type: "response.done" })))));
  assert.deepEqual(env, { type: "response.done" });
});

test("readEvent reassembles a fragmented data message", async () => {
  const full = Buffer.from(JSON.stringify({ type: "x", arguments: '{"verdict":1}' }));
  const mid = Math.floor(full.length / 2);
  const stream = Buffer.concat([
    serverFrame(0x1, full.subarray(0, mid), false),
    serverFrame(0x0, full.subarray(mid), true),
  ]);
  const env = await readEvent(mockReader(stream));
  assert.equal(env.arguments, '{"verdict":1}');
});

test("readEvent reassembles three-way fragmentation", async () => {
  const full = Buffer.from(`{"type":"x","data":"${"A".repeat(400)}"}`);
  const a = Math.floor(full.length / 3);
  const b = Math.floor((2 * full.length) / 3);
  const stream = Buffer.concat([
    serverFrame(0x1, full.subarray(0, a), false),
    serverFrame(0x0, full.subarray(a, b), false),
    serverFrame(0x0, full.subarray(b), true),
  ]);
  const env = await readEvent(mockReader(stream));
  assert.equal(env.data.length, 400);
});

test("readEvent pongs a ping interleaved between fragments and continues", async () => {
  const full = Buffer.from(JSON.stringify({ type: "response.done", n: 7 }));
  const mid = Math.floor(full.length / 2);
  const stream = Buffer.concat([
    serverFrame(0x1, full.subarray(0, mid), false),
    serverFrame(0x9, Buffer.from("hb"), true), // ping between fragments
    serverFrame(0x0, full.subarray(mid), true),
  ]);
  const reader = mockReader(stream);
  const env = await readEvent(reader);
  assert.deepEqual(env, { type: "response.done", n: 7 });
  assert.ok(reader.writes.length >= 1, "ping between fragments was not ponged");
  assert.equal(reader.writes[0][0] & 0x0f, 0xA); // pong opcode
});

test("readEvent returns null on close frame", async () => {
  const env = await readEvent(mockReader(serverFrame(0x8, Buffer.alloc(0))));
  assert.equal(env, null);
});
