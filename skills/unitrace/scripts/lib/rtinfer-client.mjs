// Thin adapter over the canonical rtinfer/1 client shipped via the
// @jaredboynton/rtinfer npm package. The canonical JS client
// (clients/rtinfer-client.mjs) is the source of truth for the wire contract,
// health gating shape, and discovery order. This adapter wraps it to:
//
//   1. Gate on UNITRACE_DAEMON_RTINFER (default ON; =0 opts out).
//   2. Apply withReasoningSteer to the user text before forwarding.
//   3. Convert the canonical fail-loud DaemonUnreachable into fail-open null.
//   4. Manage its own discovery cache so tests can reset it via _invalidate().

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { withReasoningSteer } from "./realtime_client.mjs";

export const RTINFER_CONTRACT = "rtinfer/1";
const CONTRACT_MAJOR = 1;

const HEALTH_TIMEOUT_MS = (parseFloat(process.env.CSE_RTINFER_HEALTH_TIMEOUT || "") || 0.5) * 1000;
const REQUEST_TIMEOUT_MS = (parseFloat(process.env.UNITRACE_SEARCH_RTINFER_REQUEST_TIMEOUT || "") || 20.0) * 1000;
const DISCOVERY_TTL_MS = (parseFloat(process.env.CSE_RTINFER_DISCOVERY_TTL || "") || 30.0) * 1000;

const WELL_KNOWN = path.join(os.homedir(), ".cse-rtinfer", "endpoint.json");

export function scorerModel() {
  return (process.env.UNITRACE_SEARCH_SCORER_MODEL || process.env.EXPLORE_SEARCH_SCORER_MODEL || "gpt-realtime-2").trim();
}

// Borrow is ON by default. UNITRACE_DAEMON_RTINFER=0 opts out (escape hatch
// for one release). The legacy UNITRACE_SEARCH_RTINFER alias is honored.
export function rtinferEnabled() {
  const falsy = ["0", "false", "no", "off"];
  const broad = (process.env.UNITRACE_DAEMON_RTINFER || "").trim().toLowerCase();
  if (broad) return !falsy.includes(broad);
  const legacy = (process.env.UNITRACE_SEARCH_RTINFER || "").trim().toLowerCase();
  if (legacy) return !falsy.includes(legacy);
  return true;
}

function contractMajorOk(contract) {
  if (typeof contract !== "string") return false;
  const m = /^rtinfer\/(\d+)/.exec(contract);
  return !!m && parseInt(m[1], 10) === CONTRACT_MAJOR;
}

function envBool(name) {
  return ["1", "true", "yes", "on"].includes((process.env[name] || "").trim().toLowerCase());
}

function candidates() {
  const out = [];
  const override = (process.env.CSE_RTINFER_URL || "").trim();
  if (override) out.push(override);
  if (override && envBool("CSE_RTINFER_STRICT_URL")) return out;
  try {
    const data = JSON.parse(fs.readFileSync(WELL_KNOWN, "utf8"));
    if (data && contractMajorOk(data.contract) && data.base_url) out.push(String(data.base_url).trim());
  } catch { /* no well-known file */ }
  return out;
}

// Test seam: override the candidate list.
let _candidatesFn = candidates;
export function _setCandidatesForTest(fn) {
  _candidatesFn = typeof fn === "function" ? fn : candidates;
}

async function healthOk(base) {
  try {
    const r = await fetch(`${base.replace(/\/$/, "")}/v1/infer/health`, {
      signal: AbortSignal.timeout(HEALTH_TIMEOUT_MS),
    });
    if (!r.ok) return false;
    const d = await r.json();
    return d && contractMajorOk(d.contract) && d.ready === true;
  } catch {
    return false;
  }
}

let _resolvedAt = 0;
let _resolvedBase = null;

export async function discover({ refresh = false } = {}) {
  if (!rtinferEnabled()) return null;
  const now = Date.now();
  if (!refresh && _resolvedBase !== null && now - _resolvedAt < DISCOVERY_TTL_MS) return _resolvedBase;
  for (const base of _candidatesFn()) {
    // eslint-disable-next-line no-await-in-loop
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

// Test seam: kept for backward compat (no-op, discovery is always allowed now).
export function _setDiscoveryHintForTest(_fn) {}

function steerMessages(messages, reasoningEffort) {
  if (!Array.isArray(messages)) return [];
  return messages.map((message) => {
    if (!message || typeof message !== "object") return message;
    if (message.role !== "user" || typeof message.content !== "string") return message;
    return {
      ...message,
      content: withReasoningSteer(message.content, reasoningEffort),
    };
  });
}

// One structured realtime ask via the shared daemon. Returns the parsed object
// on success, or null to signal fail-open fallback. Applies reasoning steer to
// match the per-session code path byte-for-byte.
export async function rtinferAsk({ system, user, schema, schemaName, schema_name, model, reasoningEffort }) {
  if (!rtinferEnabled()) return null;
  const base = await discover();
  if (!base) return null;
  const body = {
    contract: RTINFER_CONTRACT,
    tier: "realtime_structured",
    system,
    user: withReasoningSteer(user, reasoningEffort),
    schema,
    schema_name: schema_name || schemaName || "result",
  };
  if (model) body.model = model;
  if (reasoningEffort) body.reasoning_effort = reasoningEffort;
  let resp;
  try {
    resp = await fetch(`${base}/v1/infer`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch {
    _invalidate();
    return null;
  }
  let json = null;
  try {
    json = await resp.json();
  } catch {
    return null;
  }
  if (!resp.ok || !json || json.ok !== true) return null;
  const obj = json.object;
  return typeof obj === "object" && obj !== null ? obj : null;
}

// One chat-style realtime tool round via the shared daemon. `messages` is the
// caller-owned transcript tail (user / assistant / tool entries) and `tools`
// uses the chat-completions shape [{type:"function", function:{...}}] or the
// equivalent flat RT tool shape [{type:"function", name, ...}]. Returns
// `{ content, tool_calls }` on success, or null to signal fail-open fallback.
export async function rtinferToolRound({
  system,
  messages,
  tools,
  toolChoice,
  parallelToolCalls,
  model,
  reasoningEffort,
}) {
  if (!rtinferEnabled()) return null;
  const base = await discover();
  if (!base) return null;
  const body = {
    contract: RTINFER_CONTRACT,
    tier: "realtime_tool_round",
    system,
    messages: steerMessages(messages, reasoningEffort),
    tools,
  };
  if (toolChoice) body.tool_choice = toolChoice;
  if (typeof parallelToolCalls === "boolean") body.parallel_tool_calls = parallelToolCalls;
  if (model) body.model = model;
  if (reasoningEffort) body.reasoning_effort = reasoningEffort;
  let resp;
  try {
    resp = await fetch(`${base}/v1/infer`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch {
    _invalidate();
    return null;
  }
  let json = null;
  try {
    json = await resp.json();
  } catch {
    return null;
  }
  if (!resp.ok || !json || json.ok !== true) return null;
  return {
    content: typeof json.content === "string" ? json.content : "",
    tool_calls: Array.isArray(json.tool_calls) ? json.tool_calls : [],
  };
}
