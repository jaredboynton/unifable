// rt-session-utils.mjs — shared Realtime session helpers for trace + websearch engines.
import { appendFileSync } from "node:fs";
import { RealtimeConnection, RealtimeError, providerError } from "./realtime_client.mjs";
import { extractFunctionCalls } from "./rt-tools.mjs";

let frameBuffer = [];

export function flushFrames(framesPath) {
  if (!framesPath || !frameBuffer.length) return;
  appendFileSync(framesPath, frameBuffer.join(""));
  frameBuffer = [];
}

export function logFrame(framesPath, direction, obj) {
  if (!framesPath) return;
  frameBuffer.push(JSON.stringify({ dir: direction, type: obj?.type || "", event: obj }) + "\n");
  if (frameBuffer.length >= 64) flushFrames(framesPath);
}

export function trackSentItem(exploreItemIds, obj) {
  if (!exploreItemIds || !obj) return;
  const item = obj.item;
  if (item?.id) exploreItemIds.add(String(item.id));
}

export async function waitForResponse(conn, { deadlineMs, framesPath, pendingArgs, exploreItemIds }) {
  const textParts = [];
  let functionCalls = [];
  let status = "";

  while (Date.now() < deadlineMs) {
    const env = await conn.recv();
    if (!env) break;
    logFrame(framesPath, "recv", env);
    const kind = env.type || "";

    if (kind === "conversation.item.added" || kind === "conversation.item.done") {
      const item = env.item;
      if (item?.id && exploreItemIds) exploreItemIds.add(String(item.id));
    } else if (kind === "response.output_text.delta") {
      if (typeof env.delta === "string") textParts.push(env.delta);
    } else if (kind === "response.function_call_arguments.delta") {
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
    } else if (kind === "error" || kind === "response.failed") {
      const err = kind === "error" ? env.error : env.response?.error;
      throw new RealtimeError(providerError(err));
    } else if (kind === "response.done" || kind === "response.completed") {
      const resp = env.response && typeof env.response === "object" ? env.response : {};
      status = String(resp.status || env.status || "");
      functionCalls = extractFunctionCalls(resp.output ? resp : env);
      if (!functionCalls.length && pendingArgs.size) {
        for (const [callId, slot] of pendingArgs) {
          if (slot.name) {
            functionCalls.push({ call_id: callId, name: slot.name, arguments: slot.arguments || "" });
          }
        }
      }
      break;
    }
  }

  return { text: textParts.join(""), functionCalls, status };
}

export async function pruneExploreContext(conn, exploreItemIds, framesPath, { reconnect = false, model, authPath } = {}) {
  if (!exploreItemIds?.size) return conn;
  if (reconnect) {
    conn.close();
    const fresh = new RealtimeConnection({ model, authPathOverride: authPath });
    await fresh.connect();
    return fresh;
  }
  for (const itemId of exploreItemIds) {
    const del = { type: "conversation.item.delete", item_id: itemId };
    conn.send(del);
    logFrame(framesPath, "send", del);
  }
  return conn;
}
