// rt-agent-session.mjs — Realtime agent-loop session (connection reuse, prewarm, resilient reconnect).
import {
  RealtimeConnection,
  RealtimeError,
  realtimeReasoningConfig,
  withReasoningSteer,
} from "./realtime_client.mjs";
import {
  flushFrames,
  logFrame,
  pruneExploreContext,
  trackSentItem,
  waitForResponse,
} from "./rt-session-utils.mjs";

export class RtAgentSession {
  constructor({ model, authPath, framesPath = null } = {}) {
    this.model = model;
    this.authPath = authPath;
    this.framesPath = framesPath;
    this.conn = new RealtimeConnection({ model, authPathOverride: authPath });
    this.prewarmPatch = null;
    this.alive = false;
  }

  get connection() {
    return this.conn;
  }

  async connect() {
    await this.conn.connect();
    this.alive = true;
    return this;
  }

  close() {
    this.conn.close();
    this.alive = false;
    flushFrames(this.framesPath);
  }

  send(obj) {
    this.conn.send(obj);
    logFrame(this.framesPath, "send", obj);
  }

  prewarm(sessionUpdate) {
    this.prewarmPatch = sessionUpdate;
    this.send(sessionUpdate);
  }

  freshContextMode() {
    const v = (process.env.UNISEARCH_WS_SUBMIT_FRESH_CONTEXT
      || process.env.UNITRACE_RT_SUBMIT_FRESH_CONTEXT
      || "delete").toLowerCase();
    if (v === "off" || v === "false") return "off";
    if (v === "reconnect") return "reconnect";
    return "delete";
  }

  async pruneItems(itemIds) {
    if (!itemIds?.size) return this;
    const mode = this.freshContextMode();
    if (mode === "off") return this;
    const next = await pruneExploreContext(this.conn, itemIds, this.framesPath, {
      reconnect: mode === "reconnect",
      model: this.model,
      authPath: this.authPath,
    });
    if (next !== this.conn) {
      this.conn = next;
      this.alive = true;
      if (this.prewarmPatch) this.prewarm(this.prewarmPatch);
      logFrame(this.framesPath, "recv", { type: "session.reconnected", reason: "fresh_context" });
    }
    return this;
  }

  async reconnectFresh(reason = "manual") {
    this.conn.close();
    this.conn = new RealtimeConnection({ model: this.model, authPathOverride: this.authPath });
    await this.conn.connect();
    this.alive = true;
    if (this.prewarmPatch) this.prewarm(this.prewarmPatch);
    logFrame(this.framesPath, "recv", { type: "session.reconnected", reason });
    return this;
  }

  isConnectionClosedError(err) {
    const msg = String(err?.message || err || "").toLowerCase();
    return (
      msg.includes("websocket not connected")
      || msg.includes("websocket closed")
      || msg.includes("connection closed")
      || msg.includes("econnreset")
      || msg.includes("socket hang up")
    );
  }

  async ensureAlive(reason = "recovery") {
    if (this.alive) return this;
    await this.reconnectFresh(reason);
    return this;
  }

  async runToolRound({
    system,
    userPrompt,
    tools,
    expectedToolNames = [],
    reasoningEffort,
    deadlineMs,
    maxTurns,
    ctx,
    stopWhen,
    nudgeText,
    dispatchBatch,
    parallelToolCalls = true,
    toolChoice = "auto",
    requiredFirstTurn = true,
  }) {
    const itemIds = new Set();
    const sessionUpdate = {
      type: "session.update",
      session: {
        type: "realtime",
        instructions: system,
        output_modalities: ["text"],
        tools,
        tool_choice: toolChoice,
        parallel_tool_calls: parallelToolCalls,
        ...realtimeReasoningConfig(reasoningEffort),
      },
    };
    this.prewarm(sessionUpdate);

    const userItem = {
      type: "conversation.item.create",
      item: {
        type: "message",
        role: "user",
        content: [{ type: "input_text", text: withReasoningSteer(userPrompt, reasoningEffort) }],
      },
    };
    this.send(userItem);
    trackSentItem(itemIds, userItem);

    let turns = 0;
    let maxBatch = 0;

    for (let turn = 0; turn < maxTurns; turn += 1) {
      if (Date.now() >= deadlineMs) throw new RealtimeError("round timed out");
      if (stopWhen?.()) break;

      const respCreate = {
        type: "response.create",
        response: {
          output_modalities: ["text"],
          ...(turn === 0 && requiredFirstTurn ? { tool_choice: "required" } : {}),
        },
      };

      let functionCalls = [];
      let retried = false;
      while (true) {
        try {
          this.send(respCreate);
          const pendingArgs = new Map();
          ({ functionCalls } = await waitForResponse(this.conn, {
            deadlineMs,
            framesPath: this.framesPath,
            pendingArgs,
            exploreItemIds: itemIds,
          }));
          break;
        } catch (err) {
          if (!retried && this.isConnectionClosedError(err)) {
            retried = true;
            this.alive = false;
            await this.ensureAlive("tool_round_retry");
            continue;
          }
          throw err;
        }
      }

      const toolCalls = expectedToolNames.length
        ? functionCalls.filter((c) => expectedToolNames.includes(c.name))
        : functionCalls;

      if (!toolCalls.length) {
        if (turn >= maxTurns - 1) break;
        if (nudgeText) {
          const nudge = {
            type: "conversation.item.create",
            item: {
              type: "message",
              role: "user",
              content: [{ type: "input_text", text: withReasoningSteer(nudgeText, reasoningEffort) }],
            },
          };
          this.send(nudge);
          trackSentItem(itemIds, nudge);
        }
        continue;
      }

      turns += 1;
      maxBatch = Math.max(maxBatch, toolCalls.length);
      const batch = await dispatchBatch(toolCalls, ctx);

      for (const { call, result } of batch) {
        const outputItem = {
          type: "conversation.item.create",
          item: {
            type: "function_call_output",
            call_id: call.call_id,
            output: typeof result === "string" ? result : JSON.stringify(result),
          },
        };
        this.send(outputItem);
        trackSentItem(itemIds, outputItem);
      }

      if (stopWhen?.()) break;
    }

    await this.pruneItems(itemIds);
    return { turns, maxBatch };
  }
}

export { RealtimeError };
