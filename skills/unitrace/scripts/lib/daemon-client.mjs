// cleanup-traps: not-applicable -- spawned daemons are detached + unref'd + stdio:ignore; fail-open, self-managing, no parent-child lifecycle to clean up.
// daemon-client.mjs — node client for the shared gpt-realtime-2 warm-socket
// daemon (scripts/gate/realtime_daemon.py). Erases the ~220ms per-call TLS+WS+auth
// connect from the search rank turn by reusing persistent authenticated sockets
// held by detached daemon processes.
//
// PROCESS POOL: true concurrency comes from running N independent single-worker
// daemon PROCESSES, not N threads in one process — Python's GIL serializes
// concurrent SSL read/write threads (measured: 8 threads ~1.7s vs 8 processes
// ~1.15s for 8 parallel calls). Each daemon owns its own warm socket on its own
// slot path; daemonAskBatch spreads requests across the slots so they run in
// genuine parallel.
//
// The daemon is profile-agnostic: it serves any {system,user,schema,schema_name,
// reasoning_effort} over a 4-byte-length-prefixed JSON unix socket and returns
// the single required function call's arguments object. The SEARCH profile runs
// on its own socket namespace so it never contends with the judge daemon.
//
// FAIL-OPEN: every operational failure (no daemon, spawn timeout, socket error,
// bad response) returns null. The caller MUST fall back to an in-process warm
// call and then the agentic loop. The daemon is never on the correctness path.

import { spawn } from "node:child_process";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";
import { withReasoningSteer } from "./realtime_client.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
// daemon-client is at <repo>/skills/unitrace/scripts/lib; the shared daemon lives
// at <repo>/scripts/gate. Up four: lib -> scripts -> unitrace -> skills -> <repo>.
const GATE_DIR = path.resolve(HERE, "../../../../scripts/gate");
const DAEMON_PY = path.join(GATE_DIR, "realtime_daemon.py");

function envFloat(name, def) {
  const v = parseFloat(process.env[name] || "");
  return Number.isFinite(v) ? v : def;
}

function envInt(name, def) {
  const v = parseInt(process.env[name] || "", 10);
  return Number.isFinite(v) ? v : def;
}

const CONNECT_TIMEOUT_MS = envFloat("UNITRACE_SEARCH_DAEMON_CONNECT", 0.5) * 1000;
const SPAWN_WAIT_MS = envFloat("UNITRACE_SEARCH_DAEMON_SPAWN_WAIT", 5.0) * 1000;
const REQUEST_TIMEOUT_MS = envFloat("UNITRACE_SEARCH_DAEMON_REQUEST", 20.0) * 1000;
// Process-pool size: independent warm daemon processes give true PARALLEL
// latency, but a SMALLER pool wins. Measured live (both models, N=16 fan-out;
// skills/unitrace/docs/benchmarks/realtime-concurrency.md): P=4 beat P=8 beat
// P=16 (gpt-realtime-2 1539/1725/2731ms; mini 984/1561/2617ms). More sockets
// only adds connect/handshake contention. There is NO account concurrent-session
// cap (32/32 sockets connected cleanly) -- the small-pool win is latency, not an
// API session limit. So the default is 4, not the prior folklore 8. One socket
// can still carry ~128 in-flight responses for throughput (the judge path); this
// pool optimizes latency. Tune with UNITRACE_SEARCH_DAEMON_POOL.
const POOL_SIZE = Math.max(1, envInt("UNITRACE_SEARCH_DAEMON_POOL", 4));

export function daemonEnabled() {
  return process.env.UNITRACE_SEARCH_DAEMON !== "0";
}

export function daemonPoolSize() {
  return POOL_SIZE;
}

function dataRoot() {
  const env = process.env.UNIFABLE_DATA;
  return env ? path.resolve(env.replace(/^~/, os.homedir())) : path.join(os.homedir(), ".unifable");
}

// Scorer model for the daemon pool. gpt-realtime-2 is the proven default;
// gpt-realtime-mini (2x TPS, function calling, no reasoning option) is opt-in
// for the latency-critical scoring fan-out via UNITRACE_SEARCH_SCORER_MODEL.
const SCORER_MODEL = (process.env.UNITRACE_SEARCH_SCORER_MODEL || "gpt-realtime-2").trim();

export function scorerModel() {
  return SCORER_MODEL;
}

// Per-(namespace,model,slot) socket: distinct namespaces/models get distinct
// warm pools, distinct slots are distinct processes. The namespace is the
// caller's logical pool key (search passes repoRoot; websearch passes a fixed
// "websearch"); the model separates a mini pool from a full pool. Never collides
// with the judge namespace.
function sockKey(namespace, model) {
  return crypto.createHash("sha1").update(`${model}:${namespace}`).digest("hex").slice(0, 16);
}
function sockPathForSlot(namespace, model, slot) {
  return path.join(dataRoot(), "searchd", `${sockKey(namespace, model)}-${slot}.sock`);
}

function connect(sock, timeoutMs) {
  return new Promise((resolve, reject) => {
    const c = net.createConnection(sock);
    const t = setTimeout(() => { c.destroy(); reject(new Error("connect timeout")); }, timeoutMs);
    c.once("connect", () => { clearTimeout(t); resolve(c); });
    c.once("error", (e) => { clearTimeout(t); reject(e); });
  });
}

function spawnDaemon(sock, model) {
  try {
    fs.mkdirSync(path.dirname(sock), { recursive: true });
  } catch { /* ignore */ }
  const key = path.basename(sock, ".sock");
  try {
    // Single worker per process: GIL-free parallelism comes from many processes.
    // The daemon reads its model from UNIFABLE_JUDGE_MODEL; pass the pool's model
    // so a mini pool and a full pool never share a process (or a socket).
    const child = spawn(process.env.PYTHON || "python3", [
      DAEMON_PY, "--session-key", key, "--sock", sock, "--pool", "1",
    ], {
      cwd: GATE_DIR,
      detached: true,
      stdio: "ignore",
      env: { ...process.env, UNIFABLE_JUDGE_MODEL: model },
    });
    child.unref();
  } catch { /* fail-open: connect attempts below will give up */ }
}

async function connectOrSpawn(sock, model) {
  try {
    return await connect(sock, CONNECT_TIMEOUT_MS);
  } catch { /* spawn below */ }
  spawnDaemon(sock, model);
  const deadline = Date.now() + SPAWN_WAIT_MS;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 50));
    try {
      return await connect(sock, CONNECT_TIMEOUT_MS);
    } catch { /* keep waiting for the socket to appear */ }
  }
  return null;
}

function sendFramed(conn, obj) {
  const body = Buffer.from(JSON.stringify(obj), "utf8");
  const header = Buffer.alloc(4);
  header.writeUInt32BE(body.length, 0);
  conn.write(Buffer.concat([header, body]));
}

function recvFramed(conn, timeoutMs) {
  return new Promise((resolve) => {
    let need = -1;
    let buf = Buffer.alloc(0);
    const done = (val) => { clearTimeout(t); conn.removeAllListeners("data"); resolve(val); };
    const t = setTimeout(() => done(null), timeoutMs);
    conn.on("data", (d) => {
      buf = Buffer.concat([buf, d]);
      if (need < 0 && buf.length >= 4) { need = buf.readUInt32BE(0); buf = buf.subarray(4); }
      if (need >= 0 && buf.length >= need) {
        try { done(JSON.parse(buf.subarray(0, need).toString("utf8"))); }
        catch { done(null); }
      }
    });
    conn.once("error", () => done(null));
    conn.once("close", () => done(null));
  });
}

async function askOnSlot(namespace, model, slot, { system, user, schema, schemaName = "result", reasoningEffort, withUsage }) {
  const sock = sockPathForSlot(namespace, model, slot);
  let conn;
  try {
    conn = await connectOrSpawn(sock, model);
  } catch {
    return null;
  }
  if (!conn) return null;
  try {
    const req = { v: 1, system, user: withReasoningSteer(user, reasoningEffort), schema, schema_name: schemaName };
    if (reasoningEffort) req.reasoning_effort = reasoningEffort;
    sendFramed(conn, req);
    const resp = await recvFramed(conn, REQUEST_TIMEOUT_MS);
    if (!resp || !resp.ok || typeof resp.object !== "object" || resp.object == null) return null;
    // Opt-in: return {object, usage} so callers can read cached_tokens
    // (response.done -> response.usage.input_token_details.cached_tokens) to
    // measure Realtime prompt-cache hits. Default stays the bare object so
    // existing call sites are unchanged. usage is {} when the daemon sent none.
    return withUsage ? { object: resp.object, usage: resp.usage || {} } : resp.object;
  } catch {
    return null;
  } finally {
    try { conn.destroy(); } catch { /* ignore */ }
  }
}

// Spawn + warm the whole process pool eagerly (idempotent: connectOrSpawn is a
// no-op when a daemon already owns the slot). Call this as early as possible so
// every socket is warm before the first batch. `namespace` is the logical pool
// key (search passes repoRoot; websearch passes "websearch"); `model` defaults
// to the search SCORER_MODEL so existing search call sites are unchanged.
export async function warmDaemonPool(namespace, size = POOL_SIZE, { model = SCORER_MODEL } = {}) {
  if (!daemonEnabled()) return;
  await Promise.all(
    Array.from({ length: size }, (_, slot) =>
      connectOrSpawn(sockPathForSlot(namespace, model, slot), model).then((c) => { try { c && c.destroy(); } catch { /* ignore */ } }),
    ),
  );
}

// Run one structured request over slot 0's warm socket. Returns the parsed
// arguments object, or null to signal fail-open fallback.
export async function daemonAsk(namespace, req, { model = SCORER_MODEL } = {}) {
  if (!daemonEnabled()) return null;
  return askOnSlot(namespace, model, 0, req);
}

// Run N structured requests in genuine parallel, one per pool slot (round-robin
// when N > pool size). Returns an array aligned to `requests`; any element is
// null on that request's failure (caller decides fallback). Returns null only
// when the daemon path is disabled.
export async function daemonAskBatch(namespace, requests, { model = SCORER_MODEL } = {}) {
  if (!daemonEnabled()) return null;
  return Promise.all(requests.map((req, i) => askOnSlot(namespace, model, i % POOL_SIZE, req)));
}
