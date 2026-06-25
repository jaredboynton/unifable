// cleanup-traps: not-applicable -- spawned daemon is detached + unref'd + stdio:ignore; fail-open, self-managing, no parent-child lifecycle to clean up.
// daemon-client.mjs — node client for the shared gpt-realtime-2 warm-socket
// daemon (scripts/gate/realtime_daemon.py). Erases the ~220ms per-call TLS+WS+auth
// connect from the search rank turn by reusing a persistent authenticated
// socket held by a detached per-(session,cwd) daemon process.
//
// The daemon is profile-agnostic: it serves any {system,user,schema,schema_name,
// reasoning_effort} over a 4-byte-length-prefixed JSON unix socket and returns
// the single required function call's arguments object. We run the SEARCH
// profile on its own socket namespace so it never contends with the judge daemon.
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

const HERE = path.dirname(fileURLToPath(import.meta.url));
// daemon-client is at <repo>/skills/explore/scripts/lib; the shared daemon lives
// at <repo>/scripts/gate. Up four: lib -> scripts -> explore -> skills -> <repo>.
const GATE_DIR = path.resolve(HERE, "../../../../scripts/gate");
const DAEMON_PY = path.join(GATE_DIR, "realtime_daemon.py");

function envFloat(name, def) {
  const v = parseFloat(process.env[name] || "");
  return Number.isFinite(v) ? v : def;
}

const CONNECT_TIMEOUT_MS = envFloat("EXPLORE_SEARCH_DAEMON_CONNECT", 0.5) * 1000;
const SPAWN_WAIT_MS = envFloat("EXPLORE_SEARCH_DAEMON_SPAWN_WAIT", 3.0) * 1000;
const REQUEST_TIMEOUT_MS = envFloat("EXPLORE_SEARCH_DAEMON_REQUEST", 20.0) * 1000;

export function daemonEnabled() {
  return process.env.EXPLORE_SEARCH_DAEMON !== "0";
}

function dataRoot() {
  const env = process.env.UNIFABLE_DATA;
  return env ? path.resolve(env.replace(/^~/, os.homedir())) : path.join(os.homedir(), ".unifable");
}

// Per-(profile,cwd) socket: distinct repos get distinct warm daemons, and the
// search profile never collides with the judge profile's `judged/` namespace.
function sockPath(repoRoot) {
  const key = crypto.createHash("sha1").update(`search:${repoRoot}`).digest("hex").slice(0, 16);
  return path.join(dataRoot(), "searchd", `${key}.sock`);
}

function connect(sock, timeoutMs) {
  return new Promise((resolve, reject) => {
    const c = net.createConnection(sock);
    const t = setTimeout(() => { c.destroy(); reject(new Error("connect timeout")); }, timeoutMs);
    c.once("connect", () => { clearTimeout(t); resolve(c); });
    c.once("error", (e) => { clearTimeout(t); reject(e); });
  });
}

function spawnDaemon(repoRoot, sock) {
  try {
    fs.mkdirSync(path.dirname(sock), { recursive: true });
  } catch { /* ignore */ }
  const key = path.basename(sock, ".sock");
  try {
    const child = spawn(process.env.PYTHON || "python3", [
      DAEMON_PY, "--session-key", key, "--sock", sock,
    ], { cwd: GATE_DIR, detached: true, stdio: "ignore" });
    child.unref();
  } catch { /* fail-open: connect attempts below will give up */ }
}

async function connectOrSpawn(repoRoot, sock) {
  try {
    return await connect(sock, CONNECT_TIMEOUT_MS);
  } catch { /* spawn below */ }
  spawnDaemon(repoRoot, sock);
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
    const chunks = [];
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

// Run one structured request over the warm daemon socket. Returns the parsed
// arguments object, or null to signal fail-open fallback.
export async function daemonAsk(repoRoot, { system, user, schema, schemaName = "result", reasoningEffort }) {
  if (!daemonEnabled()) return null;
  const sock = sockPath(repoRoot);
  let conn;
  try {
    conn = await connectOrSpawn(repoRoot, sock);
  } catch {
    return null;
  }
  if (!conn) return null;
  try {
    const req = { v: 1, system, user, schema, schema_name: schemaName };
    if (reasoningEffort) req.reasoning_effort = reasoningEffort;
    sendFramed(conn, req);
    const resp = await recvFramed(conn, REQUEST_TIMEOUT_MS);
    if (!resp || !resp.ok || typeof resp.object !== "object" || resp.object == null) return null;
    return resp.object;
  } catch {
    return null;
  } finally {
    try { conn.destroy(); } catch { /* ignore */ }
  }
}
