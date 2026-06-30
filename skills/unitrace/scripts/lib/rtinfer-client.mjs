// rtinfer-client.mjs -- JS mirror of scripts/gate/rtinfer_client.py.
//
// LOCKSTEP CONTRACT: the rtinfer/1 wire shape lives in THREE clients that must
// be edited together when cse-toold bumps the contract:
//   - this file (unifable search/daemon path)
//   - scripts/gate/rtinfer_client.py (unifable judge path)
//   - cse-tools plugins/cse-tools/.agents/skills/cse-sweep/scripts/lib/daemon-client.mjs
// The health gate accepts any rtinfer/1.x (major-1 match), so a minor bump does
// not dark-fail; a true rtinfer/2 cleanly falls open.
//
// TIMEOUTS: the search path uses its OWN request timeout
// (UNITRACE_SEARCH_RTINFER_REQUEST_TIMEOUT, default 20s) -- scoring calls are
// tiny and want a tight cap. The judge mirror (rtinfer_client.py) deliberately
// uses 95s via CSE_RTINFER_REQUEST_TIMEOUT for long structured synthesis. The
// defaults differ ON PURPOSE; do not "unify" them.
//
// Borrow the always-on cse-tools rtinfer daemon (`cse-toold`) for the search
// path's structured scoring/rank calls, instead of spawning the per-session
// `searchd/` WebSocket pool. Same models, one warm pool, no second auth path.
// Neither repo imports the other; discovery is purely by loopback URL + a shared
// well-known file, identical to the Python client and the cse-sweep client.
//
// PREFERRED, never required. Fails open (returns null) on disabled, no daemon,
// timeout, or non-OK envelope, so daemon-client.mjs falls through to the
// per-session UDS pool and then the agentic loop. On a host with no cse-toold,
// nothing here ever opens a socket (discovery is gated on a presence hint so a
// bare host never even probes the cockpit default).
//
// Discovery order (matches rtinfer_client.py):
//   1. $CSE_RTINFER_URL              explicit override / tests
//   2. http://127.0.0.1:8787         cse-toold cockpit default
//   3. ~/.cse-rtinfer/endpoint.json  {contract:"rtinfer/1", base_url:...}
//
// Stdlib only: node:http + node:fs.

import http from "node:http";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { withReasoningSteer } from "./realtime_client.mjs";

const CONTRACT = "rtinfer/1";
const CONTRACT_MAJOR = 1;
const COCKPIT_DEFAULT = "http://127.0.0.1:8787";

// Persistent keep-alive agent so back-to-back scoring calls reuse one warm TCP
// connection to the loopback daemon instead of paying connect + slow-start on
// every request. The daemon is the persistent host; the only per-call cost
// should be the model turn, not a new socket. maxSockets caps concurrent
// in-flight scoring calls (search hydrates a few files at once); the rest queue
// on the agent rather than fan out fresh connections.
const keepAliveAgent = new http.Agent({
  keepAlive: true,
  keepAliveMsecs: 15000,
  maxSockets: 8,
  maxFreeSockets: 4,
});

// True when `contract` is rtinfer/<CONTRACT_MAJOR>.* (major match). A minor bump
// (rtinfer/1.1) stays compatible; rtinfer/2 does not.
function contractMajorOk(contract) {
  if (typeof contract !== "string") return false;
  const m = contract.match(/^rtinfer\/(\d+)/);
  return Boolean(m) && Number(m[1]) === CONTRACT_MAJOR;
}

function debugLog(msg) {
  if ((process.env.UNITRACE_DEBUG || process.env.DEBUG || "").trim()) {
    try { process.stderr.write(`[rtinfer] ${msg}\n`); } catch { /* ignore */ }
  }
}

function wellKnownPath() {
  return path.join(os.homedir(), ".cse-rtinfer", "endpoint.json");
}

function envFloat(name, def) {
  const v = parseFloat(process.env[name] || "");
  return Number.isFinite(v) ? v : def;
}

function envBool(name) {
  return ["1", "true", "yes", "on"].includes((process.env[name] || "").trim().toLowerCase());
}

const HEALTH_TIMEOUT_MS = envFloat("CSE_RTINFER_HEALTH_TIMEOUT", 0.5) * 1000;
// Search-scoped request timeout (20s). Deliberately tighter than the judge
// mirror's 95s (CSE_RTINFER_REQUEST_TIMEOUT) -- see the TIMEOUTS note up top.
const REQUEST_TIMEOUT_MS = envFloat("UNITRACE_SEARCH_RTINFER_REQUEST_TIMEOUT", 20.0) * 1000;
const DISCOVERY_TTL_MS = envFloat("CSE_RTINFER_DISCOVERY_TTL", 30.0) * 1000;

let _resolvedAt = 0;
let _resolvedBase = null;

// Opt-in borrow of the shared daemon. Default OFF so the mature per-session
// pool stays byte-identical and tests are deterministic regardless of whether a
// cse-toold happens to be running. Flip on once the bench proves it.
//
// SCOPE: this is wired into the shared daemonAsk/daemonAskBatch, so when ON it
// reroutes EVERY daemon caller -- search, trace, websearch, nav, enhance -- not
// just search. Hence the broad name UNITRACE_DAEMON_RTINFER. The former
// UNITRACE_SEARCH_RTINFER is accepted as a deprecated alias for one release.
export function rtinferEnabled() {
  const truthy = ["1", "true", "yes", "on"];
  const broad = (process.env.UNITRACE_DAEMON_RTINFER || "").trim().toLowerCase();
  if (broad) return truthy.includes(broad);
  const legacy = (process.env.UNITRACE_SEARCH_RTINFER || "0").trim().toLowerCase();
  return truthy.includes(legacy);
}

// Cheap presence hint so a host with no cse-tools never pays a network probe:
// only discover when an explicit URL is set or the well-known file exists
// (cse-toold writes it while running). The cockpit default is then tried as a
// candidate, but only when one of these hints is already present.
function discoveryHintPresent() {
  if ((process.env.CSE_RTINFER_URL || "").trim()) return true;
  try { return fs.statSync(wellKnownPath()).isFile(); } catch { return false; }
}

function candidates() {
  const out = [];
  const override = (process.env.CSE_RTINFER_URL || "").trim();
  if (override) out.push(override);
  // Strict mode: trust ONLY the explicit override, no cockpit/well-known
  // fallback. Lets an operator pin one endpoint (and lets the borrow bench's
  // fail-open arm point at a dead port without silently borrowing the live
  // cockpit on 8787). Default off keeps the documented discovery order.
  if (override && envBool("CSE_RTINFER_STRICT_URL")) return out;
  out.push(COCKPIT_DEFAULT);
  try {
    const data = JSON.parse(fs.readFileSync(wellKnownPath(), "utf8"));
    if (data && contractMajorOk(data.contract) && data.base_url) out.push(String(data.base_url).trim());
  } catch { /* no well-known file */ }
  return out;
}

// Test seam: override the candidate list (mirrors rtinfer_client.py's
// _candidates monkeypatch) so tests do not depend on a live cockpit on the host.
let _candidatesFn = candidates;
export function _setCandidatesForTest(fn) {
  _candidatesFn = typeof fn === "function" ? fn : candidates;
}

// Test seam: override the presence hint so tests stay hermetic when a real
// ~/.cse-rtinfer/endpoint.json exists on the host.
let _discoveryHintFn = discoveryHintPresent;
export function _setDiscoveryHintForTest(fn) {
  _discoveryHintFn = typeof fn === "function" ? fn : discoveryHintPresent;
}

function httpJson(method, url, body, timeoutMs) {
  return new Promise((resolve) => {
    let u;
    try { u = new URL(url); } catch { resolve(null); return; }
    const payload = body == null ? null : Buffer.from(JSON.stringify(body), "utf8");
    const req = http.request(
      {
        hostname: u.hostname,
        port: u.port,
        path: u.pathname + u.search,
        method,
        agent: keepAliveAgent,
        headers: payload
          ? { "content-type": "application/json", "content-length": payload.length, connection: "keep-alive" }
          : { connection: "keep-alive" },
      },
      (res) => {
        const chunks = [];
        res.on("data", (d) => chunks.push(d));
        res.on("end", () => {
          // Body fully consumed above so the keep-alive socket returns to the
          // pool even on a non-200 (otherwise the agent discards the connection).
          if (res.statusCode !== 200) { resolve(null); return; }
          try { resolve(JSON.parse(Buffer.concat(chunks).toString("utf8"))); }
          catch { resolve(null); }
        });
      },
    );
    req.on("error", () => resolve(null));
    req.setTimeout(timeoutMs, () => { req.destroy(); resolve(null); });
    if (payload) req.write(payload);
    req.end();
  });
}

async function healthOk(base) {
  const data = await httpJson("GET", base.replace(/\/$/, "") + "/v1/infer/health", null, HEALTH_TIMEOUT_MS);
  if (!data) return false;
  if (!contractMajorOk(data.contract)) {
    if (data.contract) debugLog(`contract mismatch at ${base}: ${data.contract} (want rtinfer/${CONTRACT_MAJOR}.x)`);
    return false;
  }
  return data.ready === true;
}

// Resolve a ready rtinfer base URL, or null. Cached for DISCOVERY_TTL_MS.
export async function discover({ refresh = false } = {}) {
  if (!rtinferEnabled() || !_discoveryHintFn()) return null;
  const now = Date.now();
  if (!refresh && _resolvedBase !== null && now - _resolvedAt < DISCOVERY_TTL_MS) return _resolvedBase;
  for (const base of _candidatesFn()) {
    if (await healthOk(base)) {
      _resolvedBase = base.replace(/\/$/, "");
      _resolvedAt = now;
      return _resolvedBase;
    }
  }
  _resolvedBase = null;
  _resolvedAt = now;
  return null;
}

export function _invalidate() {
  _resolvedBase = null;
  _resolvedAt = 0;
}

// One structured ask over the shared daemon's realtime tier. Returns the parsed
// arguments object on success, or null to signal fail-open fallback. Accepts the
// daemon-client request shape ({system,user,schema,schemaName,model,
// reasoningEffort}); also honors schema_name for parity with the Python
// contract. Reasoning is preserved to match the UDS path byte-for-byte: the user
// text is steer-wrapped and reasoning_effort is forwarded so the borrowed daemon
// produces the same-quality answer the bench compares against.
export async function rtinferAsk({ system, user, schema, schemaName, schema_name, model, reasoningEffort }) {
  const base = await discover();
  if (base === null) return null;
  const body = {
    contract: CONTRACT,
    tier: "realtime_structured",
    system,
    user: withReasoningSteer(user, reasoningEffort),
    schema,
    schema_name: schema_name || schemaName || "result",
  };
  if (model) body.model = model;
  if (reasoningEffort) body.reasoning_effort = reasoningEffort;
  const data = await httpJson("POST", base + "/v1/infer", body, REQUEST_TIMEOUT_MS);
  if (data == null) { _invalidate(); return null; }
  if (data.ok !== true || typeof data.object !== "object" || data.object == null) return null;
  return data.object;
}
