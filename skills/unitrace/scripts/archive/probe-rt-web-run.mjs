#!/usr/bin/env node
// Realtime web_run bridge: function tool -> Codex alpha/search -> function_call_output.
import {
  RealtimeConnection,
  RealtimeError,
  realtimeReasoningConfig,
  providerError,
} from "./lib/realtime_client.mjs";
import { flushFrames, logFrame, waitForResponse } from "./lib/rt-session-utils.mjs";
import {
  WEB_RUN_TOOL_NAME,
  buildWebRunToolSpec,
  callAlphaSearch,
  parseWebRunArguments,
  webRunCommandsFromArgs,
} from "./lib/rt-web-run.mjs";

const DEFAULT_PROMPT =
  "Use web_run to search the web. What is today's date (UTC) and one news headline from today? Cite the source URL in your answer.";

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
const rtModel = argValue("--model", process.env.UNITRACE_RT_MODEL || "gpt-realtime-2");
const searchModel = argValue("--search-model", process.env.UNITRACE_SEARCH_MODEL || "gpt-5.4");
const searchReasoning = argValue("--search-reasoning", process.env.UNITRACE_SEARCH_REASONING || "low");
const prompt = argValue("--prompt", DEFAULT_PROMPT);
const timeoutSec = Number(argValue("--timeout", String(envInt("UNITRACE_RT_TIMEOUT", 120))));
const maxToolRounds = envInt("UNITRACE_RT_WEB_RUN_MAX_ROUNDS", 2);
const framesPath = argValue("--frames", "");
const authPath = process.env.UNITRACE_CODEX_AUTH_PATH || null;

const HANDSHAKE_HEADERS = {
  "x-oai-web-search-eligible": "true",
  "x-codex-beta-features": "apps",
};

async function waitForSessionCreated(conn, deadlineMs) {
  while (Date.now() < deadlineMs) {
    const env = await conn.recv();
    if (!env) break;
    logFrame(framesPath, "recv", env);
    if (env.type === "session.created") return;
    if (env.type === "error") throw new RealtimeError(providerError(env.error));
  }
  throw new RealtimeError("timed out waiting for session.created");
}

async function dispatchWebRun(call) {
  const args = parseWebRunArguments(call.arguments);
  const commands = webRunCommandsFromArgs(args);
  const result = await callAlphaSearch({
    authPathOverride: authPath,
    searchModel,
    commands,
  });
  return result.output;
}

async function main() {
  if (dryRun) {
    console.log(JSON.stringify({
      ok: true,
      dryRun: true,
      rtModel,
      searchModel,
      searchReasoning,
      prompt,
      tool: buildWebRunToolSpec(),
      transport: "codex-alpha-search",
    }, null, 2));
    return;
  }

  const deadlineMs = Date.now() + timeoutSec * 1000;
  const conn = new RealtimeConnection({
    model: rtModel,
    authPathOverride: authPath,
    extraHandshakeHeaders: HANDSHAKE_HEADERS,
  });

  let outputText = "";
  let webRunCalls = 0;
  let searchErrors = [];
  const eventTypes = new Set();

  try {
    await conn.connect();
    await waitForSessionCreated(conn, deadlineMs);

    const sessionUpdate = {
      type: "session.update",
      session: {
        type: "realtime",
        instructions:
          "You are a research assistant. When asked for current information, call web_run with search_query before answering. Cite source URLs from search results.",
        output_modalities: ["text"],
        tools: [buildWebRunToolSpec()],
        tool_choice: "auto",
        parallel_tool_calls: false,
        ...realtimeReasoningConfig("low"),
      },
    };
    conn.send(sessionUpdate);
    logFrame(framesPath, "send", sessionUpdate);

    const userItem = {
      type: "conversation.item.create",
      item: {
        type: "message",
        role: "user",
        content: [{ type: "input_text", text: prompt }],
      },
    };
    conn.send(userItem);
    logFrame(framesPath, "send", userItem);

    let respCreate = { type: "response.create", response: { output_modalities: ["text"] } };
    conn.send(respCreate);
    logFrame(framesPath, "send", respCreate);

    for (let round = 0; round <= maxToolRounds; round += 1) {
      const pendingArgs = new Map();
      const { text, functionCalls, status } = await waitForResponse(conn, {
        deadlineMs,
        framesPath,
        pendingArgs,
      });
      outputText += text;
      eventTypes.add(`response.done:${status || "unknown"}`);

      const webCalls = functionCalls.filter((c) => c.name === WEB_RUN_TOOL_NAME);
      if (!webCalls.length) break;

      for (const call of webCalls) {
        webRunCalls += 1;
        let searchOutput;
        try {
          searchOutput = await dispatchWebRun(call);
        } catch (e) {
          const msg = e instanceof RealtimeError ? e.message : String(e.message || e);
          searchErrors.push(msg);
          searchOutput = JSON.stringify({ ok: false, error: msg });
        }

        const outputItem = {
          type: "conversation.item.create",
          item: {
            type: "function_call_output",
            call_id: call.call_id,
            output: searchOutput,
          },
        };
        conn.send(outputItem);
        logFrame(framesPath, "send", outputItem);
      }

      respCreate = { type: "response.create", response: { output_modalities: ["text"] } };
      conn.send(respCreate);
      logFrame(framesPath, "send", respCreate);
    }
  } catch (e) {
    const msg = e instanceof RealtimeError ? e.message : String(e.message || e);
    console.log(JSON.stringify({
      ok: false,
      error: msg,
      rtModel,
      searchModel,
      searchReasoning,
      webRunCalls,
      searchErrors,
      outputText: outputText.trim(),
    }, null, 2));
    process.exit(1);
  } finally {
    flushFrames(framesPath);
    try { conn.close(); } catch { /* ignore */ }
  }

  const ok = webRunCalls > 0 && searchErrors.length === 0 && outputText.trim().length > 0;
  console.log(JSON.stringify({
    ok,
    rtModel,
    searchModel,
    searchReasoning,
    webRunCalls,
    searchErrors,
    outputText: outputText.trim(),
    eventTypes: [...eventTypes],
  }, null, 2));
  process.exit(ok ? 0 : 1);
}

main().catch((err) => {
  console.error(JSON.stringify({ ok: false, error: err.message }, null, 2));
  process.exit(1);
});
