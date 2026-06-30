// daemon-client.mjs -- thin shim over the rtinfer borrow adapter.
//
// The per-session UDS daemon pool (realtime_daemon.py) is retired. The
// always-on rtinferd daemon (via rtinfer-client.mjs) is the only inference
// path. When the daemon is unreachable, calls return null so the caller falls
// through to the agentic loop. There is no in-process warm-socket fallback.
//
// Public surface is preserved so nav-loop.mjs / scorer.mjs / search-fast.mjs
// change minimally: daemonAsk, daemonAskBatch, warmDaemonPool, daemonEnabled,
// daemonPoolSize, scorerModel.

import { rtinferAsk, rtinferEnabled, rtinferToolRound, scorerModel as _scorerModel } from "./rtinfer-client.mjs";

export { scorerModel } from "./rtinfer-client.mjs";

const POOL_SIZE = Math.max(1, parseInt(process.env.UNITRACE_SEARCH_DAEMON_POOL || "4", 10));

export function daemonEnabled() {
  return process.env.UNITRACE_SEARCH_DAEMON !== "0" && rtinferEnabled();
}

export function daemonPoolSize() {
  return POOL_SIZE;
}

// Warm-up is a no-op: rtinferd owns the warm pool. Kept for call-site compat.
export async function warmDaemonPool(_namespace, _size, _opts) {
  // Intentionally empty: the shared daemon pool is always-on.
}

// Transport attribution (debug-only). The live borrow bench parses these
// tallies to prove the borrow ACTUALLY served vs silently fell through.
function daemonDebugOn() {
  return process.env.UNITRACE_DAEMON_DEBUG === "1" || process.env.UNITRACE_SEARCH_DEBUG === "1";
}
function emitServed(namespace, rtinfer) {
  if (!daemonDebugOn()) return;
  try { process.stderr.write(`[daemon] ns=${namespace} served rtinfer=${rtinfer} direct=0\n`); } catch { /* ignore */ }
}

async function rtinferTry(req, model) {
  if (!rtinferEnabled()) return null;
  const obj = await rtinferAsk({
    system: req.system,
    user: req.user,
    schema: req.schema,
    schemaName: req.schemaName,
    model,
    reasoningEffort: req.reasoningEffort,
  });
  if (!obj) return null;
  return req.withUsage ? { object: obj, usage: {} } : obj;
}

// Run one structured request via the shared rtinferd daemon. Returns the parsed
// arguments object, or null to signal fail-open fallback to the agentic loop.
export async function daemonAsk(namespace, req, { model = _scorerModel() } = {}) {
  if (!daemonEnabled()) return null;
  const rt = await rtinferTry(req, model);
  emitServed(namespace, rt != null ? 1 : 0);
  return rt;
}

// Run N structured requests via the shared rtinferd daemon. Returns an array
// aligned to `requests`; any element is null on that request's failure.
export async function daemonAskBatch(namespace, requests, { model = _scorerModel() } = {}) {
  if (!daemonEnabled()) return null;
  let rtN = 0;
  const results = await Promise.all(requests.map(async (req) => {
    const rt = await rtinferTry(req, model);
    if (rt) rtN += 1;
    return rt;
  }));
  emitServed(namespace, rtN);
  return results;
}

// Run one chat-style tool round via the shared rtinferd daemon. Returns
// `{ content, tool_calls }`, or null to signal fail-open fallback.
export async function daemonToolRound(namespace, req, { model = _scorerModel() } = {}) {
  if (!daemonEnabled()) return null;
  const rt = await rtinferToolRound({
    system: req.system,
    messages: req.messages,
    tools: req.tools,
    toolChoice: req.toolChoice,
    parallelToolCalls: req.parallelToolCalls,
    model,
    reasoningEffort: req.reasoningEffort,
  });
  emitServed(namespace, rt != null ? 1 : 0);
  return rt;
}
