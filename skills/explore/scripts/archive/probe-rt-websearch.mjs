#!/usr/bin/env node
// Probe gpt-realtime-2 Realtime WebSocket for server-side { type: "web_search" } support.
import { RealtimeConnection, RealtimeError, realtimeReasoningConfig, providerError } from "./lib/realtime_client.mjs";
import { flushFrames, logFrame } from "./lib/rt-session-utils.mjs";

const DEFAULT_PROMPT =
  "Use web search. What is today's date (UTC) and one news headline from today? Cite the source URL.";
const MODES = ["session-required", "session-auto", "response-only", "mixed"];
const HEADER_PROFILES = {
  default: null,
  eligible: {
    "x-oai-web-search-eligible": "true",
    "x-codex-beta-features": "apps",
  },
};

const DUMMY_FUNCTION_TOOL = {
  type: "function",
  name: "noop",
  description: "No-op placeholder function for mixed-mode probe.",
  parameters: {
    type: "object",
    properties: {
      note: { type: "string", description: "Optional note." },
    },
    additionalProperties: false,
  },
};

function argValue(name, fallback) {
  const i = process.argv.indexOf(name);
  return i === -1 ? fallback : process.argv[i + 1];
}

function envInt(name, fallback) {
  const v = process.env[name];
  if (v == null || v === "") return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? Math.trunc(n) : fallback;
}

const dryRun = process.argv.includes("--dry-run");
const modeArg = argValue("--mode", "session-required");
const headersArg = argValue("--headers", "both");
const model = argValue("--model", process.env.EXPLORE_RT_MODEL || "gpt-realtime-2");
const prompt = argValue("--prompt", DEFAULT_PROMPT);
const timeoutSec = Number(argValue("--timeout", String(envInt("EXPLORE_RT_TIMEOUT", 90))));
const framesPath = argValue("--frames", "");
const authPath = process.env.EXPLORE_CODEX_AUTH_PATH || null;

function resolveModes() {
  if (modeArg === "all") return MODES;
  if (!MODES.includes(modeArg)) {
    throw new Error(`invalid --mode ${modeArg}; expected one of: ${MODES.join(", ")}, all`);
  }
  return [modeArg];
}

function resolveHeaderProfiles() {
  if (headersArg === "both") return ["default", "eligible"];
  if (headersArg === "default" || headersArg === "eligible") return [headersArg];
  throw new Error("invalid --headers; expected default, eligible, or both");
}

function webSearchTool() {
  return { type: "web_search" };
}

function buildSessionUpdate(mode) {
  const session = {
    type: "realtime",
    instructions: "You are a research assistant. Use web search when asked for current information.",
    output_modalities: ["text"],
    ...realtimeReasoningConfig("low"),
  };

  if (mode === "response-only") {
    session.tools = [];
    session.tool_choice = "auto";
    return { type: "session.update", session };
  }

  if (mode === "mixed") {
    session.tools = [webSearchTool(), DUMMY_FUNCTION_TOOL];
    session.tool_choice = "auto";
    return { type: "session.update", session };
  }

  session.tools = [webSearchTool()];
  session.tool_choice = mode === "session-required" ? "required" : "auto";
  return { type: "session.update", session };
}

function buildResponseCreate(mode) {
  const response = { output_modalities: ["text"] };
  if (mode === "response-only") {
    response.tools = [webSearchTool()];
    response.tool_choice = "required";
  }
  return { type: "response.create", response };
}

function buildUserItem(text) {
  return {
    type: "conversation.item.create",
    item: {
      type: "message",
      role: "user",
      content: [{ type: "input_text", text }],
    },
  };
}

function collectOutputItemTypes(item) {
  const types = [];
  if (!item || typeof item !== "object") return types;
  if (item.type) types.push(String(item.type));
  if (item.name) types.push(String(item.name));
  return types;
}

function hasWebSearchEvidence(events, outputText) {
  const webSearchEventTypes = [
    "web_search",
    "web_search_call",
    "response.web_search_call",
    "response.web_search_call.completed",
    "response.web_search_call.in_progress",
    "response.web_search_call.searching",
  ];
  for (const evt of events) {
    const kind = String(evt.type || "");
    if (webSearchEventTypes.some((t) => kind.includes(t))) return true;
    const item = evt.item;
    if (item?.type && String(item.type).includes("web_search")) return true;
    const output = evt.response?.output;
    if (Array.isArray(output)) {
      for (const out of output) {
        if (out?.type && String(out.type).includes("web_search")) return true;
      }
    }
  }
  const urlRe = /https?:\/\/[^\s)>\]]+/gi;
  const urls = [...(outputText.match(urlRe) || [])];
  return urls.length > 0;
}

function looksRejected(events, errorMessage) {
  const blob = `${errorMessage}\n${events.map((e) => JSON.stringify(e)).join("\n")}`.toLowerCase();
  return /invalid.*tool|unknown.*tool|unsupported.*tool|tool.*not.*support|web_search.*not/.test(blob);
}

function sanitizeForJson(text, maxLen = 4000) {
  if (typeof text !== "string") return "";
  let s = text;
  if (s.length > maxLen) s = `${s.slice(0, maxLen)}...[truncated]`;
  // Drop lone surrogates that break JSON.stringify.
  return s.replace(/[\uD800-\uDFFF]/g, "");
}

function printJson(obj) {
  try {
    console.log(JSON.stringify(obj, (_k, v) => (typeof v === "string" ? sanitizeForJson(v) : v), 2));
  } catch (e) {
    console.log(JSON.stringify({ ok: false, error: `JSON serialize failed: ${e.message}` }, null, 2));
  }
}

function classifyVerdict({ events, outputText, status, errorMessage, timedOut, sessionUpdated }) {
  if (errorMessage && looksRejected(events, errorMessage)) {
    return "rejected";
  }
  if (timedOut && !status) {
    return "hung";
  }
  if (status && hasWebSearchEvidence(events, outputText)) {
    return "accepted_and_searched";
  }
  if (sessionUpdated && status) {
    return "accepted_no_search";
  }
  if (status) {
    return "completed_unknown";
  }
  if (timedOut) {
    return "hung";
  }
  return "completed_unknown";
}

async function recvUntil(conn, deadlineMs) {
  const remaining = deadlineMs - Date.now();
  if (remaining <= 0) return { type: "__timeout__" };
  return Promise.race([
    conn.recv(),
    new Promise((resolve) => {
      setTimeout(() => resolve({ type: "__timeout__" }), remaining);
    }),
  ]);
}

async function waitForSessionCreated(conn, deadlineMs, framesPathLocal) {
  while (Date.now() < deadlineMs) {
    const env = await recvUntil(conn, deadlineMs);
    if (env?.type === "__timeout__") break;
    if (!env) break;
    logFrame(framesPathLocal, "recv", env);
    if (env.type === "session.created") return env;
    if (env.type === "error") {
      throw new RealtimeError(providerError(env.error));
    }
  }
  throw new RealtimeError("timed out waiting for session.created");
}

async function runProbe({ mode, headerProfile }) {
  const extraHandshakeHeaders = HEADER_PROFILES[headerProfile] ?? null;
  const sessionUpdate = buildSessionUpdate(mode);
  const userItem = buildUserItem(prompt);
  const responseCreate = buildResponseCreate(mode);
  const outbound = [sessionUpdate, userItem, responseCreate];

  if (dryRun) {
    return {
      mode,
      headerProfile,
      dryRun: true,
      outbound,
      extraHandshakeHeaders,
    };
  }

  const conn = new RealtimeConnection({
    model,
    authPathOverride: authPath,
    extraHandshakeHeaders,
  });
  const deadlineMs = Date.now() + timeoutSec * 1000;
  const events = [];
  let outputText = "";
  let status = "";
  let errorMessage = "";
  let sessionUpdated = false;
  let timedOut = false;

  try {
    await conn.connect();
    await waitForSessionCreated(conn, deadlineMs, framesPath);

    for (const frame of outbound) {
      conn.send(frame);
      logFrame(framesPath, "send", frame);
    }

    while (Date.now() < deadlineMs) {
      const env = await recvUntil(conn, deadlineMs);
      if (env?.type === "__timeout__") {
        timedOut = true;
        break;
      }
      if (!env) break;
      events.push(env);
      logFrame(framesPath, "recv", env);
      const kind = env.type || "";

      if (kind === "session.updated") {
        sessionUpdated = true;
      } else if (kind === "response.output_text.delta") {
        if (typeof env.delta === "string") outputText += env.delta;
      } else if (kind === "error") {
        errorMessage = providerError(env.error);
      } else if (kind === "response.failed") {
        errorMessage = providerError(env.response?.error);
      } else if (kind === "response.done" || kind === "response.completed") {
        status = String(env.response?.status || env.status || "completed");
        const parts = env.response?.output;
        if (Array.isArray(parts)) {
          for (const item of parts) {
            if (item?.type === "message") {
              const content = item.content;
              if (Array.isArray(content)) {
                for (const c of content) {
                  if (c?.type === "output_text" && typeof c.text === "string") {
                    outputText += c.text;
                  }
                }
              }
            }
          }
        }
        break;
      }
    }

    if (!status && !errorMessage) timedOut = true;
  } catch (e) {
    errorMessage = e instanceof RealtimeError ? e.message : String(e.message || e);
  } finally {
    flushFrames(framesPath);
    try { conn.close(); } catch { /* ignore */ }
  }

  const outputItemTypes = [];
  for (const evt of events) {
    if (evt.type === "response.output_item.added") {
      outputItemTypes.push(...collectOutputItemTypes(evt.item));
    }
    const output = evt.response?.output;
    if (Array.isArray(output)) {
      for (const item of output) outputItemTypes.push(...collectOutputItemTypes(item));
    }
  }

  const verdict = classifyVerdict({
    events,
    outputText,
    status,
    errorMessage,
    timedOut,
    sessionUpdated,
  });

  return {
    mode,
    headerProfile,
    model,
    verdict,
    sessionUpdated,
    status: status || null,
    error: errorMessage || null,
    timedOut,
    outputText: sanitizeForJson(outputText.trim()),
    outputItemTypes: [...new Set(outputItemTypes)],
    eventTypes: [...new Set(events.map((e) => String(e.type || "")))],
  };
}

async function main() {
  const modes = resolveModes();
  const headerProfiles = resolveHeaderProfiles();
  const runs = [];

  if (dryRun) {
    for (const headerProfile of headerProfiles) {
      for (const mode of modes) {
        runs.push(await runProbe({ mode, headerProfile }));
      }
    }
    console.log(JSON.stringify({ ok: true, dryRun: true, model, prompt, runs }, null, 2));
    return;
  }

  for (const headerProfile of headerProfiles) {
    for (const mode of modes) {
      runs.push(await runProbe({ mode, headerProfile }));
    }
  }

  const anySearched = runs.some((r) => r.verdict === "accepted_and_searched");
  const allHung = runs.length > 0 && runs.every((r) => r.verdict === "hung");
  const exitCode = anySearched ? 0 : allHung ? 2 : 1;

  printJson({ ok: anySearched, model, prompt, timeoutSec, runs });
  process.exit(exitCode);
}

main().catch((err) => {
  console.error(JSON.stringify({ ok: false, error: err.message }, null, 2));
  process.exit(1);
});
