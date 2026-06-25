// realtime-search.mjs — gpt-realtime-2 callModel adapter for explore search.
//
// Conforms to the model-agnostic runSearch contract in search-lib.mjs:
//   callModel(messages, { finishOnly }) -> { content, tool_calls }
// where tool_calls is chat shape [{ id, type:"function", function:{ name, arguments } }].
//
// Drives a single hot Realtime WebSocket (Codex OAuth). The runSearch loop is
// stateless (re-passes the full messages array each turn); this adapter bridges
// that onto a stateful socket by forwarding only the newly appended messages and
// letting the model's own function_call items persist in RT context. On a dropped
// socket it reconnects once and full-replays history.
//
// Env: see realtime_client.mjs (EXPLORE_RT_*).

import { realtimeReasoningConfig } from "./lib/realtime_client.mjs";
import { RtAgentSession } from "./lib/rt-agent-session.mjs";
import { waitForResponse } from "./lib/rt-session-utils.mjs";

function debugLog(enabled, ...parts) {
  if (!enabled) return;
  process.stderr.write(`[search-rt] ${parts.join(" ")}\n`);
}

// chat TOOL_SPECS ({type,function:{name,description,parameters}}) -> RT flat shape.
function toRtTools(toolSpecs) {
  return toolSpecs.map((t) => {
    const fn = t.function || t;
    return {
      type: "function",
      name: fn.name,
      description: fn.description,
      parameters: fn.parameters,
    };
  });
}

function userItem(text) {
  return {
    type: "conversation.item.create",
    item: {
      type: "message",
      role: "user",
      content: [{ type: "input_text", text: String(text ?? "") }],
    },
  };
}

function toolOutputItem(callId, output) {
  return {
    type: "conversation.item.create",
    item: {
      type: "function_call_output",
      call_id: String(callId),
      output: typeof output === "string" ? output : JSON.stringify(output),
    },
  };
}

function functionCallItem(call) {
  return {
    type: "conversation.item.create",
    item: {
      type: "function_call",
      call_id: String(call.id),
      name: call.function.name,
      arguments: String(call.function.arguments ?? ""),
    },
  };
}

// Build the RT item events for a single chat message. Assistant messages are
// only materialized when replaying onto a fresh socket (includeAssistant); in
// the steady-state delta path the model's own emitted items already live in RT.
function chatMessageToRtItems(m, { includeAssistant }) {
  if (m.role === "user") return [userItem(m.content)];
  if (m.role === "tool") return [toolOutputItem(m.tool_call_id, m.content)];
  if (m.role === "assistant") {
    if (!includeAssistant) return [];
    const items = [];
    if (typeof m.content === "string" && m.content.trim()) {
      items.push({
        type: "conversation.item.create",
        item: {
          type: "message",
          role: "assistant",
          content: [{ type: "output_text", text: m.content }],
        },
      });
    }
    for (const tc of m.tool_calls || []) items.push(functionCallItem(tc));
    return items;
  }
  return [];
}

// RT-only behavior addendum (kept out of the shared SYSTEM_PROMPT so the
// Cerebras control baseline is untouched): suppress narration and force
// aggressive batching so the loop converges in ~3 turns.
const RT_SEARCH_ADDENDUM = [
  "",
  "Operating rules for this session:",
  "Do not narrate steps or tool calls. Perform all searching/reading silently. Emit tool calls only.",
  "Batch aggressively: issue every grep_search/glob you need in a single turn.",
  "grep_search already returns the enclosing function/class around each hit, so prefer citing finish ranges",
  "straight from that hydrated context. Only call read when the hydrated context is insufficient.",
  "Then call finish. Target 2-3 turns total. Do not investigate one file at a time when you can batch.",
].join(" ");

export function createRealtimeSearchCaller({
  model,
  authPath,
  systemPrompt,
  toolSpecs,
  finishToolName = "finish",
  framesPath = null,
  reasoningEffort,
  timeoutMs = 60000,
  debug = false,
}) {
  const rtToolsAll = toRtTools(toolSpecs);
  const rtToolsFinish = rtToolsAll.filter((t) => t.name === finishToolName);
  const instructions = `${systemPrompt}${RT_SEARCH_ADDENDUM}`;

  // Reuse the shared hot-socket session helper (prewarm, frame logging on send,
  // resilient reconnect) like the trace/websearch engines do.
  const session = new RtAgentSession({ model, authPath, framesPath });
  let connected = false;
  let sentCount = 0;

  function sessionUpdate(finishOnly) {
    return {
      type: "session.update",
      session: {
        type: "realtime",
        instructions,
        output_modalities: ["text"],
        tools: finishOnly ? rtToolsFinish : rtToolsAll,
        // finish is always in the toolset, so required is always satisfiable.
        // Forcing it prevents plain-text non-answers that cost a nudge round-trip.
        tool_choice: "required",
        parallel_tool_calls: !finishOnly,
        ...realtimeReasoningConfig(reasoningEffort),
      },
    };
  }

  // Forward messages onto the socket. delta=true sends only the unsent tail and
  // skips assistant items (already in RT context); delta=false replays all,
  // reconstructing assistant function_call items so prior outputs stay valid.
  function forward(messages, delta) {
    const slice = delta ? messages.slice(sentCount) : messages;
    for (const m of slice) {
      for (const item of chatMessageToRtItems(m, { includeAssistant: !delta })) {
        session.send(item);
      }
    }
    sentCount = messages.length;
  }

  async function runResponse(messages, { finishOnly }, replay) {
    // prewarm() records the patch so reconnectFresh re-applies it on recovery.
    session.prewarm(sessionUpdate(finishOnly));
    forward(messages, !replay);

    session.send({
      type: "response.create",
      response: {
        output_modalities: ["text"],
        tool_choice: "required",
      },
    });

    const deadlineMs = Date.now() + timeoutMs;
    const { text, functionCalls } = await waitForResponse(session.connection, {
      deadlineMs,
      framesPath,
      pendingArgs: new Map(),
      exploreItemIds: new Set(),
    });

    return {
      content: text || null,
      tool_calls: functionCalls.map((c) => ({
        id: c.call_id,
        type: "function",
        function: { name: c.name, arguments: c.arguments },
      })),
    };
  }

  let warmPromise = null;
  // Connect + prewarm once, off the first-turn critical path. Safe to call
  // eagerly (fire-and-forget); callModel awaits the in-flight warm.
  callModel.warm = () => {
    if (!warmPromise) {
      warmPromise = (async () => {
        await session.connect();
        connected = true;
        sentCount = 0;
        session.prewarm(sessionUpdate(false));
      })().catch((err) => {
        warmPromise = null;
        throw err;
      });
    }
    return warmPromise;
  };

  async function callModel(messages, meta = {}) {
    const finishOnly = Boolean(meta.finishOnly);
    if (!connected) {
      await callModel.warm();
    }
    try {
      return await runResponse(messages, { finishOnly }, false);
    } catch (err) {
      if (!session.isConnectionClosedError(err)) throw err;
      debugLog(debug, `socket dropped (${err.message}); reconnecting + replaying`);
      session.alive = false;
      await session.ensureAlive("search_retry");
      sentCount = 0;
      return await runResponse(messages, { finishOnly }, true);
    }
  }

  callModel.close = () => {
    try { session.close(); } catch { /* ignore */ }
    connected = false;
    warmPromise = null;
  };

  return callModel;
}
