// Codex OAuth + RFC6455 WebSocket client for OpenAI Realtime API (zero-dep).
import { randomBytes } from "node:crypto";
import { readFileSync, writeFileSync, renameSync, unlinkSync } from "node:fs";
import { connect as tlsConnect } from "node:tls";
import { connect as netConnect } from "node:net";
import { homedir } from "node:os";
import { join } from "node:path";

export const REALTIME_HOST = "api.openai.com";
export const REALTIME_PATH = "/v1/realtime";
export const OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token";
export const OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann";
export const OAUTH_SCOPE = "openid profile email";
export const ORIGINATOR = "codex_cli_rs";
export const DEFAULT_MODEL = process.env.UNITRACE_RT_MODEL || "gpt-realtime-2";

export class RealtimeError extends Error {
  constructor(message) {
    super(message);
    this.name = "RealtimeError";
  }
}

function envFloat(name, fallback) {
  const v = process.env[name];
  if (v == null || v === "") return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

export const HANDSHAKE_TIMEOUT = envFloat("UNITRACE_RT_HANDSHAKE", 15);
export const REFRESH_TIMEOUT = envFloat("UNITRACE_RT_REFRESH_TIMEOUT", 15);

export function authPath(override) {
  return override || join(homedir(), ".codex", "auth.json");
}

export function jwtExpUnix(token) {
  try {
    let payload = token.split(".")[1];
    payload += "=".repeat((4 - (payload.length % 4)) % 4);
    const data = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
    return data.exp != null ? Number(data.exp) : null;
  } catch {
    return null;
  }
}

function atomicWrite(path, text) {
  const tmp = `${path}.refresh.tmp`;
  writeFileSync(tmp, text, "utf8");
  try {
    renameSync(tmp, path);
  } catch (e) {
    try { unlinkSync(tmp); } catch { /* ignore */ }
    throw e;
  }
}

async function refresh(doc, path) {
  const tokens = doc.tokens || {};
  const refreshToken = tokens.refresh_token || "";
  if (!refreshToken) throw new RealtimeError("no refresh_token in auth.json");

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REFRESH_TIMEOUT * 1000);
  let resp;
  try {
    resp = await fetch(OAUTH_TOKEN_URL, {
      method: "POST",
      headers: { "content-type": "application/json", "user-agent": ORIGINATOR },
      body: JSON.stringify({
        client_id: OAUTH_CLIENT_ID,
        grant_type: "refresh_token",
        refresh_token: refreshToken,
        scope: OAUTH_SCOPE,
      }),
      signal: controller.signal,
    });
  } finally {
    clearTimeout(timer);
  }

  const body = await resp.text();
  if (!resp.ok) {
    let detail = "";
    try {
      const j = JSON.parse(body);
      const err = typeof j.error === "object" ? j.error : j;
      detail = String(err?.code || err?.message || "");
    } catch { /* ignore */ }
    if (/reuse|already been used/i.test(detail)) {
      throw new RealtimeError(
        "codex token refresh failed (refresh_token already used); run `codex login` to re-authenticate"
      );
    }
    throw new RealtimeError(`codex token refresh failed: HTTP ${resp.status} ${detail}`.trim());
  }

  const v = JSON.parse(body);
  const access = v.access_token;
  if (!access) throw new RealtimeError("refresh response missing access_token");
  tokens.access_token = access;
  if (v.id_token) tokens.id_token = v.id_token;
  tokens.refresh_token = v.refresh_token || refreshToken;
  doc.tokens = tokens;
  doc.last_refresh = new Date().toISOString();
  atomicWrite(path, JSON.stringify(doc, null, 2));
  return doc;
}

export async function freshTokens(authPathOverride, { force = false } = {}) {
  const path = authPath(authPathOverride);
  let doc;
  try {
    doc = JSON.parse(readFileSync(path, "utf8"));
  } catch (e) {
    throw new RealtimeError(`cannot read ${path}: ${e.message}`);
  }
  const tokens = { ...(doc.tokens || {}) };
  const access = tokens.access_token;
  if (!access) throw new RealtimeError("auth.json missing tokens.access_token; run `codex login`");
  if (!tokens.account_id) {
    for (const key of ["account_id", "chatgpt_account_id", "chatgpt-account-id"]) {
      if (doc[key]) { tokens.account_id = doc[key]; break; }
    }
  }
  const exp = jwtExpUnix(access);
  if (force || (exp != null && exp - Math.floor(Date.now() / 1000) <= 60)) {
    doc = await refresh(doc, path);
    Object.assign(tokens, doc.tokens || {});
    if (!tokens.account_id) {
      for (const key of ["account_id", "chatgpt_account_id", "chatgpt-account-id"]) {
        if (doc[key]) { tokens.account_id = doc[key]; break; }
      }
    }
  }
  return tokens;
}

class BufferedSocket {
  constructor(sock, initial = Buffer.alloc(0)) {
    this.sock = sock;
    this.buf = initial;
    this.waiters = [];
    this.closed = false;
    this.error = null;
    sock.on("data", (chunk) => {
      this.buf = Buffer.concat([this.buf, chunk]);
      this._flush();
    });
    sock.on("error", (err) => {
      this.error = err;
      this._fail(new RealtimeError(`websocket read failed: ${err.message}`));
    });
    sock.on("end", () => {
      this.closed = true;
      this._fail(new RealtimeError("websocket closed"));
    });
  }

  _flush() {
    while (this.waiters.length && this.buf.length >= this.waiters[0].n) {
      const { n, resolve } = this.waiters.shift();
      const out = this.buf.subarray(0, n);
      this.buf = this.buf.subarray(n);
      resolve(out);
    }
  }

  _fail(err) {
    const pending = this.waiters.splice(0);
    for (const w of pending) w.reject(err);
  }

  read(n) {
    if (this.error) return Promise.reject(this.error instanceof RealtimeError ? this.error : new RealtimeError(String(this.error.message)));
    if (this.buf.length >= n) {
      const out = this.buf.subarray(0, n);
      this.buf = this.buf.subarray(n);
      return Promise.resolve(out);
    }
    if (this.closed) return Promise.reject(new RealtimeError("websocket closed mid-frame"));
    return new Promise((resolve, reject) => {
      this.waiters.push({ n, resolve, reject });
    });
  }

  write(data) {
    this.sock.write(data);
  }

  end() {
    this.sock.end();
  }
}

function encodeFrame(payload, opcode = 0x1) {
  const mask = randomBytes(4);
  const masked = Buffer.alloc(payload.length);
  for (let i = 0; i < payload.length; i++) masked[i] = payload[i] ^ mask[i % 4];
  const n = payload.length;
  let header;
  if (n < 126) header = Buffer.from([0x80 | opcode, 0x80 | n]);
  else if (n < 65536) {
    header = Buffer.alloc(4);
    header[0] = 0x80 | opcode;
    header[1] = 0x80 | 126;
    header.writeUInt16BE(n, 2);
  } else {
    header = Buffer.alloc(10);
    header[0] = 0x80 | opcode;
    header[1] = 0x80 | 127;
    header.writeBigUInt64BE(BigInt(n), 2);
  }
  return Buffer.concat([header, mask, masked]);
}

async function readFrame(reader) {
  const hdr = await reader.read(2);
  const fin = (hdr[0] & 0x80) !== 0;
  const opcode = hdr[0] & 0x0f;
  const masked = hdr[1] & 0x80;
  let length = hdr[1] & 0x7f;
  if (length === 126) length = (await reader.read(2)).readUInt16BE(0);
  else if (length === 127) length = Number((await reader.read(8)).readBigUInt64BE(0));
  const mask = masked ? await reader.read(4) : null;
  let payload = length ? await reader.read(length) : Buffer.alloc(0);
  if (mask) {
    const out = Buffer.alloc(payload.length);
    for (let i = 0; i < payload.length; i++) out[i] = payload[i] ^ mask[i % 4];
    payload = out;
  }
  return { fin, opcode, payload };
}

export function sendText(reader, obj) {
  reader.write(encodeFrame(Buffer.from(JSON.stringify(obj), "utf8"), 0x1));
}

function parseEnvelope(payload) {
  try {
    const env = JSON.parse(payload.toString("utf8"));
    return typeof env === "object" && env ? env : null;
  } catch {
    return null;
  }
}

// Read one logical event, reassembling fragmented data messages (RFC 6455 5.4).
// A data message split across a non-FIN frame + continuation (0x0) frames is
// joined before parsing instead of being dropped at JSON.parse; a ping arriving
// between fragments is ponged inline so reassembly continues.
export async function readEvent(reader) {
  let chunks = null; // non-null while accumulating a fragmented data message
  while (true) {
    const { fin, opcode, payload } = await readFrame(reader);
    if (opcode === 0x8) return null; // close
    if (opcode === 0x9) { reader.write(encodeFrame(payload, 0xA)); continue; } // ping -> pong
    if (opcode === 0xA) continue; // pong
    if (opcode === 0x1 || opcode === 0x2) { // data message start
      if (fin) return parseEnvelope(payload);
      chunks = [payload];
      continue;
    }
    if (opcode === 0x0) { // continuation
      if (!chunks) continue; // stray continuation; ignore
      chunks.push(payload);
      if (fin) {
        const full = Buffer.concat(chunks);
        chunks = null;
        return parseEnvelope(full);
      }
      continue;
    }
    // any other opcode: ignore
  }
}

function wsHandshake(tokens, model, timeoutSec, extraHandshakeHeaders = null) {
  const path = `${REALTIME_PATH}?model=${encodeURIComponent(model)}`;
  const key = randomBytes(16).toString("base64");
  const lines = [
    `GET ${path} HTTP/1.1`,
    `Host: ${REALTIME_HOST}`,
    "Upgrade: websocket",
    "Connection: Upgrade",
    `Sec-WebSocket-Key: ${key}`,
    "Sec-WebSocket-Version: 13",
    `authorization: Bearer ${tokens.access_token}`,
  ];
  if (tokens.account_id) lines.push(`chatgpt-account-id: ${tokens.account_id}`);
  if (extraHandshakeHeaders && typeof extraHandshakeHeaders === "object") {
    for (const [k, v] of Object.entries(extraHandshakeHeaders)) {
      if (v != null && v !== "") lines.push(`${k}: ${v}`);
    }
  }
  lines.push(`originator: ${ORIGINATOR}`, "", "");
  const request = lines.join("\r\n");

  return new Promise((resolve, reject) => {
    const raw = netConnect({ host: REALTIME_HOST, port: 443 });
    raw.setTimeout(timeoutSec * 1000);
    raw.on("timeout", () => { raw.destroy(); reject(new RealtimeError("websocket connect timed out")); });
    raw.on("error", (e) => reject(new RealtimeError(`connect failed: ${e.message}`)));
    raw.on("connect", () => {
      const sock = tlsConnect({ socket: raw, servername: REALTIME_HOST, rejectUnauthorized: true }, () => {
        sock.write(request);
        let buf = Buffer.alloc(0);
        const onData = (chunk) => {
          buf = Buffer.concat([buf, chunk]);
          const sep = buf.indexOf("\r\n\r\n");
          if (sep === -1) {
            if (buf.length > 65536) {
              sock.destroy();
              reject(new RealtimeError("websocket handshake response too large"));
            }
            return;
          }
          sock.off("data", onData);
          const status = buf.toString("latin1").split("\r\n", 1)[0];
          if (!status.includes("101")) {
            sock.destroy();
            reject(new RealtimeError(`websocket handshake rejected: ${status}`));
            return;
          }
          const leftover = buf.subarray(sep + 4);
          resolve(new BufferedSocket(sock, leftover));
        };
        sock.on("data", onData);
      });
      sock.on("error", (e) => reject(new RealtimeError(`tls failed: ${e.message}`)));
    });
  });
}

export function closeWs(reader) {
  if (!reader) return;
  try {
    reader.write(encodeFrame(Buffer.alloc(0), 0x8));
    reader.end();
  } catch { /* ignore */ }
}

export function providerError(err) {
  if (!err || typeof err !== "object") return "provider error (no detail)";
  const code = err.code || err.type || "unknown";
  return `${code}: ${err.message || "(no detail)"}`;
}

export async function connectRealtime({
  model = DEFAULT_MODEL,
  authPathOverride,
  forceRefresh = false,
  extraHandshakeHeaders = null,
} = {}) {
  const tokens = await freshTokens(authPathOverride, { force: forceRefresh });
  return wsHandshake(tokens, model, HANDSHAKE_TIMEOUT, extraHandshakeHeaders);
}

export async function connectWithRetry(opts) {
  try {
    return await connectRealtime({ ...opts, forceRefresh: false });
  } catch (e) {
    if (!(e instanceof RealtimeError) || !String(e.message).includes("handshake rejected")) throw e;
    return await connectRealtime({ ...opts, forceRefresh: true });
  }
}

export class RealtimeConnection {
  constructor({ model = DEFAULT_MODEL, authPathOverride, extraHandshakeHeaders = null } = {}) {
    this.model = model;
    this.authPathOverride = authPathOverride;
    this.extraHandshakeHeaders = extraHandshakeHeaders;
    this.reader = null;
  }

  async connect() {
    this.reader = await connectWithRetry({
      model: this.model,
      authPathOverride: this.authPathOverride,
      extraHandshakeHeaders: this.extraHandshakeHeaders,
    });
  }

  send(obj) {
    if (!this.reader) throw new RealtimeError("websocket not connected");
    sendText(this.reader, obj);
  }

  async recv() {
    if (!this.reader) throw new RealtimeError("websocket not connected");
    return readEvent(this.reader);
  }

  close() {
    closeWs(this.reader);
    this.reader = null;
  }
}

export const DEFAULT_UNITRACE_REASONING_EFFORT = "none";
export const DEFAULT_SUBMIT_REASONING_EFFORT = "low";
export const DEFAULT_REASONING_EFFORT = DEFAULT_SUBMIT_REASONING_EFFORT;

// Prepend to user turns when reasoning is omitted (or when low + steer is desired).
// Realtime-2 ignores effort "none"; omit falls back to minimal — steering suppresses
// visible reasoning summaries and lowers TTFT (codex-api / sse_bench measured).
export const REALTIME_REASONING_STEER = "Respond quickly, do not reason.";

export function withReasoningSteer(userText, enabled = true) {
  if (!enabled || userText == null) return userText ?? "";
  const text = String(userText);
  if (!text.trim()) return text;
  if (text.trimStart().startsWith(REALTIME_REASONING_STEER)) return text;
  return `${REALTIME_REASONING_STEER}\n\n${text}`;
}

// Sentinels that disable reasoning by OMITTING the `reasoning` field entirely.
// The Realtime API has no "none"/"off" effort level; the way to run with no
// reasoning is to send no `reasoning` key at all (mirrors the Python judge's
// _realtime_reasoning_config in scripts/gate/codex_judge.py).
const REASONING_OMIT_VALUES = new Set(["", "none", "off", "omit", "false", "no", "disable", "disabled"]);

export function realtimeReasoningConfig(effort = DEFAULT_SUBMIT_REASONING_EFFORT) {
  const e = typeof effort === "string" ? effort.trim().toLowerCase() : effort;
  if (e == null || REASONING_OMIT_VALUES.has(e)) return {};
  return { reasoning: { effort: e } };
}

export async function askStructured(conn, {
  system,
  user,
  schema,
  schemaName = "submit_trace",
  deadlineMs,
  onSend,
  onRecv,
  reasoningEffort,
}) {
  const tool = {
    type: "function",
    name: schemaName,
    description: "Return the structured result. Call exactly once with the complete object.",
    parameters: schema,
  };

  const session = {
    type: "realtime",
    instructions: system,
    output_modalities: ["text"],
    tools: [tool],
    tool_choice: "required",
    parallel_tool_calls: false,
    ...realtimeReasoningConfig(reasoningEffort ?? DEFAULT_SUBMIT_REASONING_EFFORT),
  };
  const sessionUpdate = {
    type: "session.update",
    session,
  };
  conn.send(sessionUpdate);
  if (onSend) onSend(sessionUpdate);

  const userItem = {
    type: "conversation.item.create",
    item: {
      type: "message",
      role: "user",
      content: [{ type: "input_text", text: user }],
    },
  };
  conn.send(userItem);
  if (onSend) onSend(userItem);

  const respCreate = { type: "response.create", response: { output_modalities: ["text"] } };
  conn.send(respCreate);
  if (onSend) onSend(respCreate);

  const pendingArgs = new Map();
  let functionCalls = [];

  while (Date.now() < deadlineMs) {
    const env = await conn.recv();
    if (!env) break;
    if (onRecv) onRecv(env);
    const kind = env.type || "";

    if (kind === "response.function_call_arguments.delta") {
      const callId = env.call_id || env.item_id;
      if (callId && typeof env.delta === "string") {
        const slot = pendingArgs.get(String(callId)) || { name: "", arguments: "" };
        slot.arguments += env.delta;
        pendingArgs.set(String(callId), slot);
      }
    } else if (kind === "response.output_item.added") {
      const item = env.item;
      if (item?.type === "function_call") {
        const callId = item.call_id || item.id;
        if (callId) {
          const slot = pendingArgs.get(String(callId)) || { name: "", arguments: "" };
          if (item.name) slot.name = String(item.name);
          pendingArgs.set(String(callId), slot);
        }
      }
    } else if (kind === "response.function_call_arguments.done") {
      const callId = env.call_id || env.item_id;
      if (callId) {
        const slot = pendingArgs.get(String(callId)) || { name: "", arguments: "" };
        if (env.name) slot.name = String(env.name);
        if (typeof env.arguments === "string") slot.arguments = env.arguments;
        pendingArgs.set(String(callId), slot);
      }
    } else if (kind === "error" || kind === "response.failed") {
      const err = kind === "error" ? env.error : env.response?.error;
      throw new RealtimeError(providerError(err));
    } else if (kind === "response.done" || kind === "response.completed") {
      const resp = env.response && typeof env.response === "object" ? env.response : {};
      functionCalls = extractStructuredCalls(resp, pendingArgs);
      break;
    }
  }

  if (!functionCalls.length) {
    throw new RealtimeError("structured submit produced no function call");
  }

  const call = functionCalls.find((c) => c.name === schemaName) || functionCalls[0];
  let parsed;
  try {
    parsed = JSON.parse(call.arguments || "{}");
  } catch (e) {
    throw new RealtimeError(`structured submit JSON parse failed: ${e.message}`);
  }
  return parsed;
}

function extractStructuredCalls(resp, pendingArgs) {
  const out = [];
  const output = Array.isArray(resp.output) ? resp.output : [];
  for (const item of output) {
    if (item?.type === "function_call" && item.name) {
      out.push({
        name: String(item.name),
        arguments: typeof item.arguments === "string" ? item.arguments : JSON.stringify(item.arguments || {}),
      });
    }
  }
  if (!out.length && pendingArgs.size) {
    for (const [, slot] of pendingArgs) {
      if (slot.name) out.push({ name: slot.name, arguments: slot.arguments || "" });
    }
  }
  return out;
}
