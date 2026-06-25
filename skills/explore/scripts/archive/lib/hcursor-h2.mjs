// Zero-dependency in-process HTTP/2 Connect client for Cursor AgentService/Run.
// Pure Node (node:http2) — no h2-bridge subprocess (that exists only because
// Bun/Python/Rust can't do h2 reliably; Node can). Headers mirror the proven
// h2-bridge.mjs. Connect stream framing: [1b flags][4b BE len][payload];
// end-stream flag (bit 2) carries a JSON trailer. See connectrpc.com/docs/protocol.
import http2 from "node:http2";
import { randomUUID } from "node:crypto";

const DEFAULT_URL = "https://api2.cursor.sh";
const CLIENT_VERSION = "cli-2026.01.09-231024f";
const END_STREAM_FLAG = 0b00000010;

// Unary application/proto call (no Connect framing on the request body). Used for
// repo42.cursor.sh RepositoryService RPCs. Mirrors h2-bridge.mjs unary mode:
// content-type application/proto, raw body, te:trailers, no connect-protocol-version.
// Resolves { body, status } where body is the full raw response (may be Connect-framed;
// the caller unwraps). Rejects on transport error / timeout.
export function unaryProto({ accessToken, url, path, body, headers = {}, timeoutMs = 15000 }) {
  return new Promise((resolve, reject) => {
    const client = http2.connect(url);
    const chunks = [];
    let status = 0;
    let settled = false;
    const done = (fn, arg) => { if (settled) return; settled = true; if (timer) clearTimeout(timer); try { client.close(); } catch {} try { client.destroy(); } catch {} fn(arg); };
    const timer = setTimeout(() => done(reject, new Error("repo42 unary timeout")), timeoutMs);
    client.on("error", (e) => done(reject, e));

    const reqHeaders = {
      ":method": "POST",
      ":path": path,
      "content-type": "application/proto",
      te: "trailers",
      authorization: `Bearer ${accessToken}`,
      "x-ghost-mode": "true",
      "x-cursor-client-type": "ide",
      "x-request-id": randomUUID(),
      ...headers,
    };
    const stream = client.request(reqHeaders);
    stream.on("response", (h) => { status = h[":status"] || 0; });
    stream.on("data", (c) => chunks.push(Buffer.from(c)));
    stream.on("end", () => done(resolve, { body: Buffer.concat(chunks), status }));
    stream.on("error", (e) => done(reject, e));
    if (body && body.length) stream.end(Buffer.from(body)); else stream.end();
  });
}

export function frameConnect(payload, flags = 0) {
  const frame = Buffer.alloc(5 + payload.length);
  frame[0] = flags;
  frame.writeUInt32BE(payload.length, 1);
  Buffer.from(payload).copy(frame, 5);
  return frame;
}

// Returns a connection handle with write()/close(); invokes callbacks for each
// decoded server message and for the terminal end-stream trailer.
export function openRun({ accessToken, url = DEFAULT_URL, path = "/agent.v1.AgentService/Run", onMessage, onEnd, onError, idleMs = 120000 }) {
  const client = http2.connect(url);
  let pending = Buffer.alloc(0);
  let endTrailer = null;
  let timer = null;

  const resetTimer = () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => { try { client.destroy(); } catch {} }, idleMs);
  };

  client.on("error", (e) => { if (timer) clearTimeout(timer); onError?.(e); });

  const headers = {
    ":method": "POST",
    ":path": path,
    "content-type": "application/connect+proto",
    te: "trailers",
    authorization: `Bearer ${accessToken}`,
    "x-ghost-mode": "true",
    "x-cursor-client-version": CLIENT_VERSION,
    "x-cursor-client-type": "cli",
    "x-request-id": randomUUID(),
    "connect-protocol-version": "1",
  };
  const stream = client.request(headers);
  resetTimer();

  const parse = (chunk) => {
    pending = Buffer.concat([pending, chunk]);
    while (pending.length >= 5) {
      const flags = pending[0];
      const len = pending.readUInt32BE(1);
      if (pending.length < 5 + len) break;
      const body = pending.subarray(5, 5 + len);
      pending = pending.subarray(5 + len);
      if (flags & END_STREAM_FLAG) {
        try { endTrailer = JSON.parse(Buffer.from(body).toString("utf8")); }
        catch { endTrailer = { error: { code: "parse", message: "bad end-stream trailer" } }; }
      } else {
        onMessage?.(Buffer.from(body));
      }
    }
  };

  stream.on("data", (chunk) => { resetTimer(); parse(chunk); });
  stream.on("end", () => {
    if (timer) clearTimeout(timer);
    onEnd?.(endTrailer);
  });
  stream.on("error", (e) => { if (timer) clearTimeout(timer); onError?.(e); });

  return {
    write(payload) {
      if (!stream.closed && !stream.destroyed) {
        resetTimer();
        stream.write(frameConnect(payload));
      }
    },
    end() { if (!stream.closed && !stream.destroyed) stream.end(); },
    close() { if (timer) clearTimeout(timer); try { client.close(); } catch {} try { client.destroy(); } catch {} },
  };
}
